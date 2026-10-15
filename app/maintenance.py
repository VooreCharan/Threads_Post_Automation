"""Background upkeep, run every few minutes while the app is awake:

- refresh the news feeds and read the top stories of every category in advance, so a "post ..." command
  finds everything already fetched and answers in seconds (no AI calls: no free-tier quota is used);
- renew the Threads token before it expires (otherwise it is only renewed when you post, so an idle bot
  could let it lapse) and warn you on Telegram if that fails;
- check that the database is reachable and warn you if it is not.
"""
import logging
import threading
import time

from . import alerts, article, config, drafts, feeds, safety, state, threads
from .sources import FEEDS

logger = logging.getLogger(__name__)
TOP_PER_CATEGORY = 3


def _next_candidates(category: str, n: int = TOP_PER_CATEGORY) -> list[dict]:
    """The newest stories a command would consider, minus the ones it would skip without reading."""
    reserved = drafts.pending_urls()
    out = []
    for item in feeds.fetch_category(category):
        if state.was_posted(item["url"]) or state.was_skipped(item["url"]) or item["url"] in reserved:
            continue
        if config.SKIP_SENSITIVE and safety.sensitive_match(item["title"], item["summary"]):
            continue
        out.append(item)
        if len(out) >= n:
            break
    return out


def prewarm() -> int:
    """Refresh every category's feed and pre-read its top stories. Returns how many stories were warmed."""
    stories: list[dict] = []
    for category in FEEDS:
        try:
            stories += _next_candidates(category)
        except Exception as e:
            logger.warning("Prewarm skipped %s: %s", category, str(e)[:100])
    article.prefetch(stories)
    return len(stories)


def run_cycle() -> None:
    try:
        state.ping()
    except Exception as e:
        alerts.alert("db_down", f"The database is unreachable ({str(e)[:120]}). The bot keeps working from "
                                "memory, but history and settings are not being saved.", every_hours=2, persist=False)
    if config.DRY_RUN:  # a rehearsal must never touch (renew) the real Threads token
        days = None
    else:
        days = threads.ensure_fresh()  # renews the token when under 10 days are left; alerts if it cannot
    if days is not None and days < 0:
        logger.error("Threads token has expired")
    if config.PREWARM:
        n = prewarm()
        logger.info("Maintenance: %d stories pre-read; Threads token %s", n,
                    f"valid {days:.0f} more days" if days is not None else "expiry not known yet")


def _loop() -> None:
    time.sleep(20)  # let the app finish starting
    while True:
        try:
            run_cycle()
        except Exception:
            logger.exception("Maintenance cycle failed")
        time.sleep(config.MAINTENANCE_MINUTES * 60)


def start() -> None:
    threading.Thread(target=_loop, daemon=True, name="maintenance").start()
