"""The agent: find the latest unposted Indian story in a category, write and check a post, then either
hand it to the owner as a draft (preview and approve) or publish it to Threads."""
import logging
import random
import threading

from . import article, config, drafts, feeds, images, llm, safety, state, threads
from .sources import FEEDS

logger = logging.getLogger(__name__)
_run_lock = threading.Lock()

# The FIRST hashtag is the Threads topic tag, so it always matches the category asked for. Single words only.
TAG_BY_CATEGORY = {"india": "IndiaNews", "business": "Business", "technology": "Technology",
                   "sports": "Sports", "entertainment": "Entertainment", "health": "Health",
                   "science": "Science", "environment": "Environment"}

# the order in which "why was nothing found" reasons win a tie
_REASON_ORDER = ["all_duplicates", "sensitive", "no_india_news", "unverified", "thin_text"]


def final_tags(category: str, story_tags: list[str]) -> list[str]:
    """Category tag first, then up to two story-specific tags that passed the relevance check."""
    tags = [TAG_BY_CATEGORY[category]] if category in TAG_BY_CATEGORY else []
    for t in story_tags:
        if t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    return tags[:3]


def is_busy() -> bool:
    return _run_lock.locked()


# limits live in safety.py; re-exported so callers have one place to ask
blocked_reason = safety.blocked_reason


def image_label(r: dict) -> str:
    return {"article": "🖼 article image", "stock": "📷 stock photo"}.get(r.get("image"), "📝 text only")


REASON_TEXT = {
    "no_new_articles": "No new stories right now.",
    "all_duplicates": "The latest stories repeat topics already posted.",
    "sensitive": "The latest stories were about sensitive topics (violence, deaths, tragedies), which I skip.",
    "no_india_news": "The latest stories were not about India.",
    "unverified": "I couldn't write a reliably accurate post for the latest stories.",
    "thin_text": "The latest stories had too little readable text to write a reliable post.",
    "generation_failed": "The AI writers failed just now.",
}


def format_results(results: list[dict]) -> str:
    lines = []
    for r in results:
        status = r.get("status", "")
        if status == "posted":
            lines.append(f"✅ [{r['category']}] {image_label(r)} {r['url']}")
        elif status == "dry_run":
            lines.append(f"📝 (dry run) [{r['category']}] {image_label(r)}\n{r['text'][:400]}"
                         + (f"\nimage: {r['image_url']}" if r.get("image_url") else ""))
        elif status == "busy":
            lines.append("A run is already in progress.")
        elif status in REASON_TEXT:
            lines.append(REASON_TEXT[status])
        else:
            lines.append(f"⚠️ {r.get('url', '')} {status}".strip())
    return "\n".join(lines) or "Nothing to do."


# ---- choosing candidates ----------------------------------------------------------------------

def _pick(categories: list[str], count: int) -> list[dict]:
    """Newest-first candidates (stories already posted, skipped or waiting for approval are left out).
    With several categories they are interleaved in shuffled order."""
    reserved = drafts.pending_urls()
    queues = []
    for cat in categories:
        items = [i for i in feeds.fetch_category(cat)
                 if not state.was_posted(i["url"]) and not state.was_skipped(i["url"])
                 and i["url"] not in reserved]
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


def _skip(item: dict, reason: str, counters: dict, key: str) -> None:
    state.mark_skipped(item["url"], reason)
    counters[key] = counters.get(key, 0) + 1
    logger.info("Skipped (%s): %s", reason, item["title"][:80])


def _build(categories: list[str], count: int) -> tuple[list[dict], str]:
    """Prepare up to `count` drafts: the newest stories that pass every check. Must hold _run_lock.

    Returns (drafts, status); status explains why nothing was found when the list is empty."""
    counters: dict[str, int] = {}
    built: list[dict] = []
    failures = 0
    # spare candidates: a story rejected by a check must not use up the request
    candidates = _pick(categories, count + 12)
    # read the first few likely candidates in parallel (the background upkeep has often done it already)
    article.prefetch([i for i in candidates
                      if not (config.SKIP_SENSITIVE and safety.sensitive_match(i["title"], i["summary"]))][:6])
    for item in candidates:
        if len(built) >= count:
            break
        same = state.near_identical_title(item["title"])
        if same:  # word-for-word the same headline as a recent post: no AI call needed
            _skip(item, f"duplicate of: {same[:80]}", counters, "all_duplicates")
            continue
        if config.SKIP_SENSITIVE:
            word = safety.sensitive_match(item["title"], item["summary"])
            if word:
                _skip(item, f"sensitive topic ({word})", counters, "sensitive")
                continue
        text = feeds.article_text(item)
        if len(text) < config.MIN_ARTICLE_CHARS:  # cannot write or fact-check from a one-line summary
            _skip(item, "not enough readable article text", counters, "thin_text")
            continue
        written = llm.write_post(item, text)
        if written is llm.UNVERIFIED:  # the AI's draft kept failing its own fact-check
            _skip(item, "post could not be verified against the article", counters, "unverified")
            continue
        if not written:
            failures += 1
            continue
        if written.is_duplicate:  # the same news event another outlet / an earlier post already covered
            _skip(item, "same event as a recent post", counters, "all_duplicates")
            continue
        if config.INDIA_ONLY and not written.is_india:
            _skip(item, "not about India", counters, "no_india_news")
            continue
        if config.SKIP_SENSITIVE and written.is_sensitive:
            _skip(item, "sensitive topic (flagged by the AI)", counters, "sensitive")
            continue
        tags = final_tags(item["category"], written.tags)
        image_url, kind, credit = images.pick_image(item, written.post, written.photo_query)
        built.append({
            "url": item["url"], "title": item["title"], "category": item["category"], "source": item["source"],
            "post": written.post, "tags": tags, "photo_query": written.photo_query,
            "image_url": image_url, "image": kind, "credit": credit,
            "text": llm.compose(written.post, tags, item["source"],
                                link=item["url"] if image_url else None, photo_credit=credit),
        })
    if built:
        return built, "ok"
    if counters:
        return [], min(counters, key=lambda k: (-counters[k], _REASON_ORDER.index(k)))
    return [], "generation_failed" if failures else "no_new_articles"


# ---- publishing -------------------------------------------------------------------------------

def publish_draft(draft: dict) -> dict:
    """Publish a draft, falling back so a post is never lost: the chosen image -> a relevant stock
    photo -> text only. Returns {"post_id", "image", "text"}; raises if even text-only fails."""
    options: list[tuple[str | None, str | None, str | None]] = []
    if draft.get("image_url"):
        options.append((draft["image_url"], draft["image"], draft.get("credit")))
    if draft.get("image") == "article" and config.IMAGE_MODE == "auto" and images.stock_available():
        options.append(("<stock>", "stock", None))  # looked up only if Threads rejects the article image
    options.append((None, None, None))

    item = {"url": draft["url"], "title": draft["title"], "category": draft["category"]}
    last_error: Exception | None = None
    for image_url, kind, credit in options:
        if image_url == "<stock>":
            found = images.find_stock_image(item, draft["post"], draft.get("photo_query", ""))
            if not found:
                continue
            image_url, credit = found
        text = llm.compose(draft["post"], draft["tags"], draft["source"],
                           link=draft["url"] if image_url else None, photo_credit=credit)
        try:
            post_id = threads.publish(text, link=None if image_url else draft["url"], image_url=image_url)
            return {"post_id": post_id, "image": kind, "text": text}
        except Exception as e:
            last_error = e
            logger.warning("Publish with %s image failed (%s): %s", kind or "no", draft["url"][-40:], e)
    raise last_error or RuntimeError("could not publish")


def make_draft(category: str) -> tuple[dict | None, str]:
    """Prepare ONE draft for a category. Returns (draft, "ok") or (None, reason)."""
    if category not in FEEDS:
        return None, "unknown_category"
    if not _run_lock.acquire(blocking=False):
        return None, "busy"
    try:
        built, status = _build([category], 1)
        return (built[0], "ok") if built else (None, status)
    finally:
        _run_lock.release()


def approve(draft_id: str) -> dict:
    """Publish a draft the owner approved. Returns a result dict with a "status":
    posted | expired | blocked | busy | already_posted | publish_failed."""
    if drafts.get(draft_id) is None:
        return {"status": "expired"}
    reason = safety.blocked_reason()
    if reason:
        return {"status": "blocked", "reason": reason}
    if not _run_lock.acquire(blocking=False):
        return {"status": "busy"}
    try:
        draft = drafts.pop(draft_id)  # taking it out is what stops a double tap publishing twice
        if draft is None:
            return {"status": "expired"}
        if state.was_posted(draft["url"]):
            return {"status": "already_posted", "url": draft["url"]}
        if config.DRY_RUN:  # a rehearsal: nothing is published and nothing is recorded as posted
            return {"status": "dry_run", "url": draft["url"], "category": draft["category"], "text": draft["text"],
                    "image": draft.get("image")}
        try:
            out = publish_draft(draft)
        except Exception as e:
            logger.error("Publish failed for %s: %s", draft["url"], e)
            return {"status": "publish_failed", "error": str(e), "url": draft["url"]}
        state.record_post(draft["url"], out["post_id"], draft["category"], draft["title"])
        return {"status": "posted", "url": draft["url"], "category": draft["category"],
                "post_id": out["post_id"], "image": out["image"], "text": out["text"]}
    finally:
        _run_lock.release()


def reject(draft_id: str, reason: str = "rejected by owner") -> dict | None:
    """Discard a draft; the story is remembered as skipped so it is not proposed again."""
    draft = drafts.pop(draft_id)
    if draft:
        state.mark_skipped(draft["url"], reason)
    return draft


# ---- immediate mode and the command line --------------------------------------------------------

def run_once(categories: list[str] | None = None, count: int | None = None,
             dry_run: bool | None = None) -> list[dict]:
    """Publish (or, with dry_run, only prepare) `count` (default 1) stories at once, without approval.
    Used when REQUIRE_APPROVAL is off, by the command line, and for dry runs."""
    if not _run_lock.acquire(blocking=False):
        logger.info("A run is already in progress")
        return [{"status": "busy"}]
    try:
        categories = [c for c in (categories or config.CATEGORIES or list(FEEDS)) if c in FEEDS]
        dry_run = config.DRY_RUN if dry_run is None else dry_run
        built, status = _build(categories, count or 1)
        if not built:
            return [{"status": status}]
        results = []
        for d in built:
            if dry_run:
                results.append({"url": d["url"], "category": d["category"], "status": "dry_run",
                                "text": d["text"], "image": d["image"], "image_url": d["image_url"]})
                continue
            try:
                out = publish_draft(d)
            except Exception as e:
                logger.error("Publish failed for %s: %s", d["url"], e)
                results.append({"url": d["url"], "status": f"publish_failed: {e}"})
                break
            state.record_post(d["url"], out["post_id"], d["category"], d["title"])
            results.append({"url": d["url"], "category": d["category"], "status": "posted",
                            "post_id": out["post_id"], "text": out["text"], "image": out["image"]})
        return results
    finally:
        _run_lock.release()
