"""Get the real text of a news article, so posts are written (and fact-checked) from the full story
and not from a one-line feed summary.

Order of attempts (first good one wins):
1. `articleBody` from the page's JSON-LD (most Indian publishers embed the whole story there);
2. the main block of paragraphs in the HTML, with navigation, ads, related links and captions removed;
3. the feed summary plus the page's meta description.
"""
import html
import json
import logging
import re
import threading
import time

import requests

from . import net
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsThreadsBot/1.0)", "Accept-Language": "en-IN,en;q=0.9"}
MAX_CHARS = 4500
CACHE_TTL = 30 * 60
_cache: dict[str, tuple[float, tuple[str, str]]] = {}
_lock = threading.Lock()

# paragraphs that are page furniture, not story text
BOILERPLATE = re.compile(
    r"^(also read|read more|read also|also see|related|advertis|sponsored|subscribe|follow us|follow\b.*\bon\b|"
    r"click here|download the|download app|join our|join us|sign up|log ?in|copyright|©|all rights reserved|"
    r"disclaimer|first published|last updated|updated:|published:|photo:|image:|credit:|\(with inputs|\(inputs from|"
    r"story continues|continue reading|catch all the|get latest|stay updated|for more|watch:|"
    r"share this|tags?:|topics?:|next story|previous story|trending|recommended|most read|"
    r"your (browser|privacy)|we use cookies|this (site|website) uses|by continuing|terms of use)", re.I)
# containers that hold furniture
FURNITURE = re.compile(
    r"(related|recommend|trending|newsletter|promo|advert|(^|[-_ ])ads?([-_ ]|$)|social|share|comment|footer|"
    r"sidebar|breadcrumb|subscribe|widget|popup|modal|more[-_]?stories|also[-_]?read|read[-_]?more|"
    r"taboola|outbrain|signin|login|cookie|byline|author|tags|topics|nav|menu|header)", re.I)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def _jsonld_body(soup: BeautifulSoup) -> str:
    """articleBody from any NewsArticle-like JSON-LD block."""
    best = ""

    def walk(node):
        nonlocal best
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            body = node.get("articleBody")
            if isinstance(body, str) and len(body) > len(best):
                best = body
            for key in ("@graph", "mainEntity", "mainEntityOfPage"):
                if key in node:
                    walk(node[key])

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or tag.get_text() or ""))
        except (ValueError, TypeError):
            continue
    if not best:
        return ""
    # the body often contains HTML tags or entities
    text = BeautifulSoup(best, "html.parser").get_text("\n") if "<" in best else best
    return "\n".join(p for p in (_clean(x) for x in text.split("\n")) if p)


def _is_furniture(tag) -> bool:
    if tag.name in ("script", "style", "nav", "header", "footer", "aside", "form", "figure", "figcaption",
                    "noscript", "iframe", "svg", "button", "select"):
        return True
    ident = " ".join(tag.get("class", []) if isinstance(tag.get("class"), list) else [str(tag.get("class") or "")])
    ident += " " + str(tag.get("id") or "")
    return bool(ident.strip() and FURNITURE.search(ident))


def _paragraphs(container) -> list[str]:
    out: list[str] = []
    for p in container.find_all("p"):
        if any(_is_furniture(a) for a in [p, *p.parents] if a is not container and getattr(a, "name", None)):
            continue
        text = _clean(p.get_text(" "))
        if len(text) >= 40 and not BOILERPLATE.search(text) and text not in out:
            out.append(text)
    return out


def _html_body(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "form"]):
        tag.decompose()
    candidates = []
    # preferred explicit containers, then <article>, then the parent that holds the most paragraph text
    for sel in ('[itemprop="articleBody"]', '[class*="article-body"]', '[class*="story-body"]',
                '[class*="articlebody"]', '[class*="story_details"]', '[class*="full-details"]',
                '[class*="article-content"]', '[class*="story-content"]', '[class*="content-body"]',
                "article", "main"):
        for node in soup.select(sel):
            paras = _paragraphs(node)
            if paras:
                candidates.append(paras)
    if not candidates:
        parents: dict[int, list] = {}
        for p in soup.find_all("p"):
            parents.setdefault(id(p.parent), [p.parent, 0])[1] += len(_clean(p.get_text()))
        if parents:
            best = max(parents.values(), key=lambda v: v[1])[0]
            paras = _paragraphs(best)
            if paras:
                candidates.append(paras)
    if not candidates:
        return ""
    best = max(candidates, key=lambda ps: sum(len(x) for x in ps))
    return "\n".join(best)


def _meta_description(soup: BeautifulSoup) -> str:
    for attrs in ({"property": "og:description"}, {"name": "description"}, {"name": "twitter:description"}):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            return _clean(tag["content"])
    return ""


# inline promos that publishers leave inside the article text (e.g. "Also Read | <headline>")
_RELATED = re.compile(
    r"(?:also\s+read|read\s+more|read\s+also|also\s+see|also\s+watch|watch\s+also|see\s+also)\s*[|:–—-]\s*"
    r"[^.?!\n]*[.?!]?", re.I)
_PROMO_SENTENCE = re.compile(
    r"(?:get the latest [^.]*?(?:news|updates)[^.]*\.\s*)?"
    r"(?:download the [A-Za-z0-9 ]{2,25} app[^.]*\.|follow us on [^.]*\.|join (?:our|us on) [^.]*\.|"
    r"subscribe to [^.]*\.|click here [^.]*\.|for more [^.]*\bvisit\b[^.]*\.|sign up for [^.]*\.|"
    r"stay (?:tuned|updated)[^.]*\.|\(with inputs from [^)]*\)|\(inputs from [^)]*\)|"
    r"this story has been published[^.]*\.|disclaimer:[^.]*\.)", re.I)


def _polish(text: str) -> str:
    """Remove promos and related-article headlines, and repair missing spaces after full stops."""
    text = _RELATED.sub(" ", text)
    text = _PROMO_SENTENCE.sub(" ", text)
    text = re.sub(r"(?<=[a-z0-9)])([.?!])(?=[A-Z][a-z])", r"\1 ", text)  # "Thursday.The" -> "Thursday. The"
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _trim(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = cut.rfind("\n")
    return cut[:end] if end > limit * 0.6 else cut


def fetch(url: str, summary: str = "") -> tuple[str, str]:
    """(text, how) where how is "jsonld", "html" or "summary". Never raises."""
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    result = _fetch(url, summary)
    with _lock:
        _cache[url] = (now, result)
        if len(_cache) > 300:  # keep memory small
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                del _cache[k]
    return result


def _fetch(url: str, summary: str) -> tuple[str, str]:
    summary = _clean(summary)
    try:
        resp = net.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200 or "html" not in resp.headers.get("content-type", "html").lower():
            raise RuntimeError(f"HTTP {resp.status_code}")
        soup = BeautifulSoup(resp.content, "html.parser")
    except Exception as e:
        logger.info("Could not read the article page (%s): %s", url[-50:], str(e)[:80])
        return summary, "summary"

    body = _polish(_jsonld_body(soup))
    if len(body) >= 400:
        return _trim(body), "jsonld"
    desc = _meta_description(soup)
    html_text = _polish(_html_body(soup))
    if len(html_text) >= 300:
        return _trim(html_text), "html"
    # thin page: combine everything we have, without duplicating
    parts: list[str] = []
    for chunk in (body, html_text, summary, desc):
        if chunk and not any(chunk in p or p in chunk for p in parts):
            parts.append(chunk)
    return _trim("\n".join(parts)), "summary"


def prefetch(items: list[dict], workers: int = 4) -> None:
    """Read several articles in parallel; the results land in the cache so later fetch() calls are instant."""
    from concurrent.futures import ThreadPoolExecutor
    if not items:
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda i: fetch(i["url"], i.get("summary", "")), items))
