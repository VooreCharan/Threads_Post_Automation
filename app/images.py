"""Pick an image for a post: the article's own image, or a relevant free stock photo.

Threads only accepts an image as a public URL (it fetches the file itself) and wants JPEG/PNG up to
8 MB. A stock photo is used only when the story has a concrete, photographable subject AND a check
says the photo's description clearly matches the story; otherwise the post is text only.
"""
import io
import logging
import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from PIL import Image

from . import config, llm, net

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsThreadsBot/1.0)"}
MAX_BYTES = 8 * 1024 * 1024
OK_TYPES = ("image/jpeg", "image/png")
# Words in a URL that usually mean a site-wide placeholder rather than a photo for this story
BAD_HINTS = ("logo", "placeholder", "default", "sprite", "favicon", "avatar", "blank", "fallback")


def _download(url: str, timeout: int = 20) -> bytes | None:
    """Fetch an image, enforcing type and size. Returns the bytes or None."""
    try:
        with net.get(url, headers=HEADERS, timeout=timeout, stream=True) as r:
            if r.status_code != 200:
                return None
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype not in OK_TYPES:
                return None
            data = b""
            for chunk in r.iter_content(65536):
                data += chunk
                if len(data) > MAX_BYTES:
                    return None
            return data
    except Exception:  # an unreachable image or page just means "no image"
        return None


def _usable(url: str) -> bool:
    """True if the URL serves a JPEG/PNG that is big enough and not an odd shape."""
    data = _download(url)
    if not data:
        return False
    try:
        w, h = Image.open(io.BytesIO(data)).size
    except Exception:
        return False
    return w >= config.IMAGE_MIN_WIDTH and h > 0 and 0.5 <= w / h <= 2.0


# ---- 1) the article's own image ---------------------------------------------------------------

def _page_images(page_url: str) -> list[str]:
    """Candidate image URLs from the article page's social-preview meta tags."""
    try:
        resp = net.get(page_url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.content, "html.parser")
    except Exception:  # an unreachable image or page just means "no image"
        return []
    found = []
    for attrs in ({"property": "og:image"}, {"property": "og:image:secure_url"},
                  {"name": "twitter:image"}, {"name": "twitter:image:src"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            found.append(urljoin(page_url, tag["content"]))
    link = soup.find("link", rel="image_src")
    if link and link.get("href"):
        found.append(urljoin(page_url, link["href"]))
    return found


def find_article_image(item: dict) -> str | None:
    """The article's own main image, if it has a real one."""
    seen = set()
    for url in [*_page_images(item["url"]), item.get("image", "")]:
        if not url or url in seen:
            continue
        seen.add(url)
        if any(h in url.lower() for h in BAD_HINTS):
            continue
        if _usable(url):
            return url
    return None


# ---- 2) free stock photos (Pexels, Pixabay, Unsplash) ------------------------------------------
# Each search returns candidates: {"url", "desc", "credit"}. Candidates without a description are
# dropped because relevance cannot be judged without one.

def _search_pexels(query: str) -> list[dict]:
    r = net.get("https://api.pexels.com/v1/search", timeout=20,
                     headers={"Authorization": config.PEXELS_API_KEY},
                     params={"query": query, "per_page": 12, "orientation": "landscape"})
    r.raise_for_status()
    return [{"url": p["src"]["large2x"], "desc": p.get("alt") or "",
             "credit": f"{p['photographer']} / Pexels"} for p in r.json().get("photos", [])]


def _search_pixabay(query: str) -> list[dict]:
    r = net.get("https://pixabay.com/api/", timeout=20,
                     params={"key": config.PIXABAY_API_KEY, "q": query, "image_type": "photo",
                             "orientation": "horizontal", "safesearch": "true", "per_page": 12})
    r.raise_for_status()
    return [{"url": h["largeImageURL"], "desc": h.get("tags", ""),
             "credit": f"{h.get('user', 'unknown')} / Pixabay"} for h in r.json().get("hits", [])]


def _search_unsplash(query: str) -> list[dict]:
    r = net.get("https://api.unsplash.com/search/photos", timeout=20,
                     headers={"Authorization": f"Client-ID {config.UNSPLASH_ACCESS_KEY}"},
                     params={"query": query, "per_page": 12, "orientation": "landscape",
                             "content_filter": "high"})
    r.raise_for_status()
    return [{"url": p["urls"]["regular"], "desc": p.get("alt_description") or p.get("description") or "",
             "credit": f"{p['user']['name']} / Unsplash"} for p in r.json().get("results", [])]


STOCK = {
    "pexels": (lambda: config.PEXELS_API_KEY, _search_pexels),
    "pixabay": (lambda: config.PIXABAY_API_KEY, _search_pixabay),
    "unsplash": (lambda: config.UNSPLASH_ACCESS_KEY, _search_unsplash),
}


# Stories where a generic stock photo would be inappropriate or misleading: politics and elections,
# crime and courts, conflict, accidents, disasters, deaths. Enforced here, not left to the AI.
SENSITIVE = re.compile(
    r"\b(murder\w*|kill\w*|shot|shoot\w*|stabb\w*|attack\w*|war|wars|warfare|troops|missile\w*|bomb\w*|"
    r"terror\w*|hostage\w*|dead|death\w*|died|dies|dying|funeral|victim\w*|abuse\w*|rape\w*|assault\w*|"
    r"trafficking|trial|sentence\w*|jail\w*|prison\w*|convict\w*|arrest\w*|court\w*|lawsuit\w*|sue[sd]?|"
    r"crash\w*|collision|explosion\w*|fire|disaster\w*|earthquake\w*|flood\w*|hurricane\w*|"
    r"election\w*|vote[sd]?|voters?|ballot\w*|poll|polls|mps?|minister\w*|president\w*|chancellor\w*|"
    r"senator\w*|parliament\w*|congress\w*|governor\w*|trump|biden|putin|starmer|netanyahu|zelensky\w*|"
    r"sanction\w*|diplomat\w*|embassy|ambassador\w*)\b", re.IGNORECASE)
NO_STOCK_CATEGORIES = {"politics"}


def _stock_ok(item: dict, post: str) -> bool:
    if item.get("category") in NO_STOCK_CATEGORIES:
        return False
    return not SENSITIVE.search(f"{item.get('title', '')} {post}")


def stock_available() -> bool:
    return any(name in STOCK and STOCK[name][0]() for name in config.STOCK_PROVIDERS)


def find_stock_image(item: dict, post: str, query: str) -> tuple[str, str] | None:
    """A free stock photo that clearly matches the story: (url, credit), or None.

    Needs a photo search phrase from the writer (empty means a generic photo would not honestly
    illustrate the story). Each provider's candidates are judged by text description."""
    query = (query or "").strip()
    if not query or not _stock_ok(item, post):
        return None
    for name in config.STOCK_PROVIDERS:
        if name not in STOCK or not STOCK[name][0]():
            continue
        try:
            candidates = [c for c in STOCK[name][1](query) if c["url"] and c["desc"].strip()]
        except Exception as e:
            logger.warning("Stock search on %s failed: %s", name, str(e)[:150])
            continue
        if not candidates:
            continue
        choice = llm.judge_photo(item, post, candidates[:8])
        if choice is None:
            logger.info("No %s photo matched %r closely enough", name, query)
            continue
        picked = candidates[choice]
        if _usable(picked["url"]):
            return picked["url"], None  # stock photos are shown without any credit
    return None


def pick_image(item: dict, post: str = "", photo_query: str = "") -> tuple[str | None, str | None, str | None]:
    """(image_url, kind, credit): kind is "article" or "stock"; (None, None, None) = text only."""
    mode = config.IMAGE_MODE
    if mode == "off":
        return None, None, None
    if mode in ("auto", "article"):
        url = find_article_image(item)
        if url:
            return url, "article", f"Image: {item['source']}"  # credit the publisher
    if mode in ("auto", "stock") and stock_available():
        found = find_stock_image(item, post, photo_query)
        if found:
            return found[0], "stock", found[1]
    return None, None, None
