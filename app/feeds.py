"""Fetch and normalise items from the RSS feeds in sources.py."""
import calendar
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests
from bs4 import BeautifulSoup

from . import article, config, net
from .sources import FEEDS, INDIA_HINTS

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsThreadsBot/1.0)"}
CACHE_TTL = 15 * 60
INDIA_RE = re.compile(r"\b(" + INDIA_HINTS + r")\b|₹", re.IGNORECASE)
_cache: dict[str, tuple[float, list[dict]]] = {}


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _text(html: str) -> str:
    if "<" not in (html or ""):
        return (html or "").strip()  # plain text; skips BeautifulSoup's "looks like a filename" warning
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def _entry_image(e) -> str:
    """Image URL declared in the RSS entry itself (media:content / media:thumbnail / enclosure)."""
    for key in ("media_content", "media_thumbnail"):
        for m in e.get(key, []) or []:
            if m.get("url"):
                return m["url"]
    for link in e.get("links", []) or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image/"):
            return link.get("href", "")
    return ""


def _fetch_feed(category: str, source: str, url: str, mixed: bool = False) -> list[dict]:
    try:
        resp = net.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:  # any failure of one feed must not break the category
        logger.warning("Feed failed (%s / %s): %s", category, source, e)
        return []
    items = []
    for e in feedparser.parse(resp.content).entries:
        link, title = e.get("link"), _text(e.get("title", ""))
        if not link or not title:
            continue
        if mixed and not INDIA_RE.search(f"{title} {_text(e.get('summary', ''))}"):
            continue  # foreign story from a feed that mixes in world news
        ts = e.get("published_parsed") or e.get("updated_parsed")
        items.append({
            "url": normalize_url(link),
            "title": title,
            "summary": _text(e.get("summary", "")),
            "source": source,
            "category": category,
            "image": _entry_image(e),
            "published": calendar.timegm(ts) if ts else time.time(),
        })
    return items


def fetch_category(category: str) -> list[dict]:
    """Fresh items for a category, newest first (cached for a few minutes)."""
    cached = _cache.get(category)
    if cached and time.time() - cached[0] < CACHE_TTL:
        return cached[1]
    feeds = FEEDS.get(category, [])
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches = pool.map(lambda f: _fetch_feed(category, f[0], f[1], mixed=len(f) > 2 and f[2] == "mixed"), feeds)
    cutoff = time.time() - config.MAX_AGE_HOURS * 3600
    seen, items = set(), []
    for item in sorted((i for b in batches for i in b), key=lambda i: -i["published"]):
        if item["published"] >= cutoff and item["url"] not in seen:
            seen.add(item["url"])
            items.append(item)
    _cache[category] = (time.time(), items)
    logger.info("Category %s: %d fresh items", category, len(items))
    return items


def article_text(item: dict) -> str:
    """The article's readable text (see article.py); falls back to the feed summary."""
    return article.fetch(item["url"], item["summary"])[0]
