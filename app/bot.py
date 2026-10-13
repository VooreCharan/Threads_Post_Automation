"""The agent: pick fresh unposted news across categories, write posts, publish to Threads."""
import logging
import random
import threading
import time

from . import config, feeds, images, llm, state, threads
from .sources import FEEDS

logger = logging.getLogger(__name__)
_run_lock = threading.Lock()


def posting_enabled() -> bool:
    flag = state.get("posting_enabled")
    return config.AUTO_START if flag is None else flag == "1"


def set_posting(on: bool) -> None:
    was_on = posting_enabled()
    state.put("posting_enabled", "1" if on else "0")
    if on:
        state.put("next_post_at", "0")  # post as soon as the next heartbeat runs
        if not was_on:
            state.put("limit_notified", "0")
            state.put("session_posts", "0")  # new session: count posts from zero


def session_posts() -> int:
    """Posts published by the current/last start-posting session."""
    return int(state.get("session_posts") or 0)


def is_busy() -> bool:
    return _run_lock.locked()


def wait_until_idle() -> None:
    """Block until any in-progress run has finished its current post."""
    with _run_lock:
        pass


def _image_label(r: dict) -> str:
    return {"article": "🖼 article image", "stock": "📷 stock photo"}.get(r.get("image"), "📝 text only")


def format_results(results: list[dict]) -> str:
    lines = []
    for r in results:
        status = r.get("status", "")
        if status == "posted":
            lines.append(f"✅ [{r['category']}] {_image_label(r)} {r['url']}")
        elif status == "dry_run":
            lines.append(f"📝 (dry run) [{r['category']}] {_image_label(r)}\n{r['text'][:300]}"
                         + (f"\nimage: {r['image_url']}" if r.get("image_url") else ""))
        elif status == "no_india_news":
            lines.append("Only non-India stories were available this round.")
        elif status == "no_new_articles":
            lines.append("No new articles right now.")
        elif status == "stopped":
            lines.append("Stopped before posting.")
        elif status == "busy":
            lines.append("A run is already in progress.")
        else:
            lines.append(f"⚠️ {r.get('url', '')} {status}".strip())
    return "\n".join(lines) or "Nothing to do."


def tick_now() -> list[dict]:
    """Publish exactly one post (spare articles are tried if one fails)."""
    return run_once(count=1, scheduled=True)


def _daily_wait() -> float:
    """Seconds to wait before the rolling-24h Threads posting limit frees a slot (0 = fine)."""
    n, oldest = state.posts_in_window(24 * 3600)
    if n < config.THREADS_DAILY_LIMIT or oldest is None:
        return 0
    return max(60.0, oldest + 24 * 3600 - time.time() + 60)


def tick(notify=None) -> None:
    """Heartbeat (every few seconds): while posting is ON, publish one post whenever the random
    gap since the last one has elapsed. Runs until "stop posting"."""
    notify = notify or (lambda _msg: None)
    if not posting_enabled() or time.time() < float(state.get("next_post_at") or 0):
        return

    wait = _daily_wait()
    if wait:
        state.put("next_post_at", str(time.time() + wait))
        if state.get("limit_notified") != "1":
            state.put("limit_notified", "1")
            resume = time.strftime("%H:%M", time.localtime(time.time() + wait))
            notify(f"⏳ Reached the {config.THREADS_DAILY_LIMIT} posts / 24h limit. Pausing; "
                   f"will resume automatically around {resume}. (Say 'stop posting' to end.)")
        return
    state.put("limit_notified", "0")

    results = tick_now()
    if results == [{"status": "busy"}]:
        return  # another run holds the lock; try again at the next heartbeat

    if any(r.get("status") == "posted" for r in results):
        gap = random.uniform(config.POST_GAP_MIN_SECONDS, config.POST_GAP_MAX_SECONDS)
        n = session_posts()
        if n == 1 or n % config.NOTIFY_EVERY == 0:
            posted = next(r for r in results if r.get("status") == "posted")
            notify(f"✅ Post #{n} published [{posted['category']}] {_image_label(posted)}\n{posted['url']}")
    else:
        gap = config.IDLE_RETRY_SECONDS if results and results[0].get("status") in (
            "no_new_articles", "no_india_news", "stopped") else 120
        quiet = results and results[0].get("status") == "stopped"
        if not quiet and time.time() - float(state.get("last_error_notice") or 0) > 900:
            state.put("last_error_notice", str(time.time()))  # at most one warning per 15 minutes
            notify(f"⚠️ Nothing posted: {format_results(results)}\nRetrying in {int(gap)}s.")
    state.put("next_post_at", str(time.time() + gap))


def _publish(item: dict, post: str, topic: str, photo_query: str, dry_run: bool) -> dict:
    """Publish with the best image available, falling back so a post is never lost:
    article image -> relevant stock photo -> text only. Returns {"post_id", "image", "text", ...}."""
    url, kind, credit = images.pick_image(item, post, photo_query)
    options: list[tuple[str | None, str | None, str | None]] = [(url, kind, credit)] if url else []
    if kind == "article" and config.IMAGE_MODE == "auto" and images.stock_available():
        options.append(("<stock>", "stock", None))  # looked up only if Threads rejects the article image
    options.append((None, None, None))

    last_error: Exception | None = None
    for image_url, image_kind, image_credit in options:
        if image_url == "<stock>":
            found = images.find_stock_image(item, post, photo_query)
            if not found:
                continue
            image_url, image_credit = found
        text = llm.compose(post, topic, item["source"], link=item["url"] if image_url else None,
                           photo_credit=image_credit)
        if dry_run:
            return {"post_id": None, "image": image_kind, "image_url": image_url, "text": text}
        try:
            post_id = threads.publish(text, link=None if image_url else item["url"], image_url=image_url)
            return {"post_id": post_id, "image": image_kind, "image_url": image_url, "text": text}
        except Exception as e:
            last_error = e
            logger.warning("Publish with %s image failed (%s): %s", image_kind or "no", item["url"][-40:], e)
    raise last_error or RuntimeError("could not publish")


def _pick(categories: list[str], count: int) -> list[dict]:
    """Round-robin over (shuffled) categories so posts stay varied."""
    queues = []
    for cat in categories:
        items = [i for i in feeds.fetch_category(cat)
                 if not state.was_posted(i["url"]) and not state.was_skipped(i["url"])]
        if items:
            queues.append(items)
    random.shuffle(queues)
    picked: list[dict] = []
    while queues and len(picked) < count:
        for q in list(queues):
            picked.append(q.pop(0))
            if not q:
                queues.remove(q)
            if len(picked) >= count:
                break
    return picked


def run_once(categories: list[str] | None = None, count: int | None = None,
             dry_run: bool | None = None, scheduled: bool = False) -> list[dict]:
    """Run the agent once. Returns one result dict per attempted post.

    scheduled=True (start-posting sessions): successful posts are counted toward the session, and
    a "stop posting" is honoured between posts - the post already in progress still finishes."""
    if not _run_lock.acquire(blocking=False):
        logger.info("A run is already in progress")
        return [{"status": "busy"}]
    try:
        categories = [c for c in (categories or config.CATEGORIES or list(FEEDS)) if c in FEEDS]
        count = count or config.POSTS_PER_RUN
        dry_run = config.DRY_RUN if dry_run is None else dry_run
        results = []
        done = 0
        skipped = 0
        # Pull spare candidates so a failed article does not use up one of the requested posts
        for item in _pick(categories, count + 8):
            if done >= count:
                break
            if scheduled and not posting_enabled():
                if not results:
                    results.append({"status": "stopped"})
                break
            written = llm.write_post(item, feeds.article_text(item))
            if not written:
                results.append({"url": item["url"], "status": "generation_failed"})
                continue
            post, topic, photo_query, is_india = written
            if config.INDIA_ONLY and not is_india:
                state.mark_skipped(item["url"], "not about India")
                logger.info("Skipped (not about India): %s", item["title"][:80])
                skipped += 1
                continue
            try:
                out = _publish(item, post, topic, photo_query, dry_run=dry_run)
            except Exception as e:
                logger.error("Publish failed for %s: %s", item["url"], e)
                results.append({"url": item["url"], "status": f"publish_failed: {e}"})
                break  # likely a config/token problem: stop instead of burning AI quota
            done += 1
            if dry_run:
                results.append({"url": item["url"], "category": item["category"], "status": "dry_run",
                                "text": out["text"], "image": out["image"], "image_url": out["image_url"]})
                continue
            state.record_post(item["url"], out["post_id"], item["category"])
            if scheduled:
                state.put("session_posts", str(session_posts() + 1))
            results.append({"url": item["url"], "category": item["category"], "status": "posted",
                            "post_id": out["post_id"], "text": out["text"], "image": out["image"]})
        if not results:
            results.append({"status": "no_india_news" if skipped else "no_new_articles"})
        return results
    finally:
        _run_lock.release()
