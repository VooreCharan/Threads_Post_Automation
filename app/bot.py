"""The agent: find the latest unposted Indian story in a category, write a post, publish to Threads."""
import logging
import random
import threading
import time

from . import config, feeds, images, llm, state, threads
from .sources import FEEDS

logger = logging.getLogger(__name__)
_run_lock = threading.Lock()


def is_busy() -> bool:
    return _run_lock.locked()


def blocked_reason() -> str | None:
    """Why posting is not allowed right now (daily cap or minimum pause), or None if it is fine.
    Protects the account from a flood of commands."""
    n, oldest = state.posts_in_window(24 * 3600)
    if n >= config.DAILY_POST_LIMIT and oldest is not None:
        resume = time.strftime("%H:%M", time.localtime(oldest + 24 * 3600 + 60))
        return (f"Daily limit reached ({n}/{config.DAILY_POST_LIMIT} posts in the last 24 hours). "
                f"Try again after about {resume}.")
    last = state.last_post_time()
    if last is not None:
        wait = config.MIN_SECONDS_BETWEEN_POSTS - (time.time() - last)
        if wait > 0:
            return f"Please wait {int(wait) + 1} more seconds before the next post."
    return None


def image_label(r: dict) -> str:
    return {"article": "🖼 article image", "stock": "📷 stock photo"}.get(r.get("image"), "📝 text only")


def format_results(results: list[dict]) -> str:
    lines = []
    for r in results:
        status = r.get("status", "")
        if status == "posted":
            lines.append(f"✅ [{r['category']}] {image_label(r)} {r['url']}")
        elif status == "dry_run":
            lines.append(f"📝 (dry run) [{r['category']}] {image_label(r)}\n{r['text'][:300]}"
                         + (f"\nimage: {r['image_url']}" if r.get("image_url") else ""))
        elif status == "no_india_news":
            lines.append("Only non-India stories were available this round.")
        elif status == "no_new_articles":
            lines.append("No new articles right now.")
        elif status == "busy":
            lines.append("A run is already in progress.")
        else:
            lines.append(f"⚠️ {r.get('url', '')} {status}".strip())
    return "\n".join(lines) or "Nothing to do."


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
    """Newest-first candidates. With several categories they are interleaved (shuffled order)."""
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
             dry_run: bool | None = None) -> list[dict]:
    """Post `count` (default 1) stories: the newest unposted ones in the given categories.

    Returns one result dict per attempt; the successful ones have status "posted"."""
    if not _run_lock.acquire(blocking=False):
        logger.info("A run is already in progress")
        return [{"status": "busy"}]
    try:
        categories = [c for c in (categories or config.CATEGORIES or list(FEEDS)) if c in FEEDS]
        count = count or 1
        dry_run = config.DRY_RUN if dry_run is None else dry_run
        results = []
        done = 0
        skipped = 0
        # Spare candidates: a story the AI rejects (e.g. not about India) must not use up the request
        for item in _pick(categories, count + 8):
            if done >= count:
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
            results.append({"url": item["url"], "category": item["category"], "status": "posted",
                            "post_id": out["post_id"], "text": out["text"], "image": out["image"]})
        if not results:
            results.append({"status": "no_india_news" if skipped else "no_new_articles"})
        return results
    finally:
        _run_lock.release()
