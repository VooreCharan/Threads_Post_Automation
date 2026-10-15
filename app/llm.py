"""Write a Threads post for a news item using a chain of free LLM providers.

Providers are tried in LLM_CHAIN order (Gemini, Groq, OpenRouter, Hugging Face, GitHub Models).
A provider/model that is overloaded, rate-limited or misconfigured goes on cooldown, so later posts
skip it instead of waiting on it again.
"""
import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import NamedTuple

import requests
from google import genai

from . import alerts, cache, config, net, state

logger = logging.getLogger(__name__)

THREADS_LIMIT = 500
HTTP_TIMEOUT = 60

PROMPT = """You run a news account on Threads. Write a post about the article below, using ONLY
facts stated in it. Put it in your own words (do not copy sentences).
Be exact about WHO said or did WHAT: never give a quote, opinion or action to the wrong person (the
subject of a story is often not the person speaking). If you are unsure who said something, state the
facts without attributing them, and do not add details that the article does not contain.
NEVER use outside knowledge. Do not say who a person is (profession, nationality, past roles, titles,
relationships) unless the article text itself says so: many different people share a name, and a famous
namesake is probably NOT the person in this story. If the article only gives a name, use just the name.

Return ONLY a JSON object:
{{"india": true, "duplicate": false, "sensitive": false, "post": "...", "hashtags": ["...", "..."], "photo_query": "..."}}
- duplicate: true if this story reports the SAME news event or announcement as any title in the
  "Recently posted" list below, even when worded differently or published by another outlet. false for
  a different event, even on a similar subject (a different match, company, person or announcement).
- sensitive: true if the story is mainly about violence or crime against people, sexual offences,
  suicide, deaths, accidents with casualties, disasters, terrorism, communal tension or similar tragedy,
  or involves minors in a harmful situation. false otherwise (including ordinary politics, business,
  sport and entertainment news).
- india: true ONLY if the story is mainly about India: events or places in India, the Indian government
  or politics, Indian people, companies, markets, sport or culture, or foreign news that directly
  affects India. false if it is mainly about another country or region (even if an Indian outlet
  reported it) or is global news with no Indian angle.
- post: 1-3 short sentences, at most 280 characters (hashtags and a link are added after it), clear and
  neutral, no URL, no hashtags, no emoji spam
- hashtags: 2 or 3 hashtags for THIS story, written WITHOUT the # sign, each a single word
  (CamelCase for phrases, e.g. TeamIndia, SabaAzad, MonsoonRains). Each must name something the article
  and your post are actually about: a person, place, organisation, event or specific theme that appears
  in the text. Prefer PROPER NAMES: people, teams, places, organisations, tournaments, films, schemes
  (e.g. ViratKohli, RanjiTrophy, NDMC, Odisha). Avoid generic or time words (DayThree, Match, Report,
  Announcement), filler (Trending, Viral, News, Breaking, Latest, Update) and anything the article
  does not mention. If the story has fewer than two good names, return fewer hashtags.
- photo_query: 2-4 words for a stock-photo search that would give a fitting, GENERIC illustration of the
  story's main subject, place or theme (e.g. "basketball arena", "video game console", "pharmacy
  shelves", "solar panels", "hospital ward", "coral reef", "stock exchange floor"). It must never imply
  that the photo shows the actual people or event. Use "" (empty) ONLY when a stock photo would be
  inappropriate or misleading: the story is mainly about named people, elections or politicians, crime,
  war or conflict, accidents, disasters, deaths or other tragedy, or is too abstract to picture.

Recently posted on this account:
{recent}

Category: {category}
Title: {title}
Article: {content}"""


class Written(NamedTuple):
    """What the AI writer returns for one story."""
    post: str
    tags: list
    photo_query: str
    is_india: bool
    is_duplicate: bool
    is_sensitive: bool


@dataclass(frozen=True)
class Slot:
    provider: str
    model: str

    def __str__(self) -> str:
        return f"{self.provider}/{self.model}"


def build_slots() -> list[Slot]:
    """Every (provider, model) that has credentials, in chain order."""
    slots: list[Slot] = []
    for provider in config.LLM_CHAIN:
        if provider == "gemini":
            if config.GEMINI_API_KEY:
                slots += [Slot("gemini", m) for m in [config.GEMINI_MODEL, *config.GEMINI_FALLBACK_MODELS]]
        elif provider in config.OPENAI_COMPAT:
            cfg = config.OPENAI_COMPAT[provider]
            if cfg["key"]:
                slots += [Slot(provider, m) for m in cfg["models"]]
        else:
            logger.warning("Unknown provider in LLM_CHAIN: %s", provider)
    return slots


# ---- calling a single slot -------------------------------------------------------------------

_gemini_client: genai.Client | None = None


def _call_gemini(model: str, prompt: str) -> str:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=config.GEMINI_API_KEY, http_options={"timeout": HTTP_TIMEOUT * 1000})
    resp = _gemini_client.models.generate_content(
        model=model, contents=prompt,
        config={"response_mime_type": "application/json", "temperature": 0.6})
    return resp.text


def _call_openai_compat(provider: str, model: str, prompt: str) -> str:
    cfg = config.OPENAI_COMPAT[provider]
    body = {"model": model, "temperature": 0.6, "max_tokens": 1200,  # room for hidden reasoning
            "messages": [{"role": "user", "content": prompt}]}
    if provider == "groq" and model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = "low"  # short answer needed; skip long deliberation
    resp = net.post(f"{cfg['base']}/chat/completions",
                         headers={"Authorization": f"Bearer {cfg['key']}"},
                         json=body, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"] or ""


def _call(slot: Slot, prompt: str) -> str:
    if slot.provider == "gemini":
        return _call_gemini(slot.model, prompt)
    return _call_openai_compat(slot.provider, slot.model, prompt)


# ---- pacing and cooldowns --------------------------------------------------------------------

_pace_lock = threading.Lock()
_last_call: dict[str, float] = {}
_cooldown_until: dict[Slot, float] = {}


def _interval(provider: str) -> float:
    if provider == "gemini":
        return config.GEMINI_MIN_INTERVAL
    return config.OPENAI_COMPAT[provider]["interval"]


def _pace(provider: str) -> None:
    """Keep calls to one provider spaced out so free-tier requests-per-minute limits hold."""
    with _pace_lock:
        wait = _last_call.get(provider, 0) + _interval(provider) - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[provider] = time.time()


def _cooldown_seconds(error: Exception) -> float:
    """How long to skip a slot after this error (0 = don't, e.g. just a bad answer)."""
    text = str(error)
    hint = re.search(r"retry in ([\d.]+)s", text)
    if "429" in text or "RESOURCE_EXHAUSTED" in text or "rate limit" in text.lower():
        return min(float(hint.group(1)) + 2, 300) if hint else 90
    if any(c in text for c in ("503", "504", "UNAVAILABLE", "DEADLINE", "timed out", "Timeout", "Connection")):
        return 120
    if any(c in text for c in ("400", "401", "403", "404", "NOT_FOUND", "PERMISSION", "API key")):
        return 3600  # wrong model name or key: will not fix itself
    if isinstance(error, (ValueError, KeyError)):
        return 300  # model keeps answering without usable JSON (e.g. a slow "thinking" model)
    return 0


# ---- result parsing --------------------------------------------------------------------------

GENERIC_TAGS = {"news", "trending", "viral", "breaking", "breakingnews", "latest", "update", "updates",
                "today", "topnews", "newsupdate", "newsupdates", "fyp", "follow", "foryou", "headlines",
                "worldnews", "dailynews", "mustread", "share", "daythree", "dayone", "daytwo", "dayfour",
                "dayfive", "match", "report", "story", "announcement", "statement", "reaction", "controversy"}


def clean_tags(raw_tags, limit: int = 3) -> list[str]:
    """Threads tags are single words (letters, digits, underscore), 2-30 chars, not generic filler."""
    tags: list[str] = []
    for t in raw_tags or []:
        t = re.sub(r"[^A-Za-z0-9_]", "", str(t))
        if 2 <= len(t) <= 30 and not t.isdigit() and t.lower() not in GENERIC_TAGS \
                and t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    return tags[:limit]


def _parse(raw: str) -> Written:
    """Pull {"post", "hashtags", "photo_query", "india", "duplicate"} out of a model reply."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in reply")
    data = json.loads(match.group())
    post = str(data["post"]).strip().strip('"')
    if not post:
        raise ValueError("empty post")
    tags = clean_tags(data.get("hashtags") if isinstance(data.get("hashtags"), list) else [], limit=4)
    query = re.sub(r"[^A-Za-z0-9 ]", "", str(data.get("photo_query", "") or "")).strip()[:60]
    india = data.get("india", True)
    if isinstance(india, str):
        india = india.strip().lower() in ("true", "yes", "1")
    duplicate = data.get("duplicate", False)
    if isinstance(duplicate, str):
        duplicate = duplicate.strip().lower() in ("true", "yes", "1")
    sensitive = data.get("sensitive", False)
    if isinstance(sensitive, str):
        sensitive = sensitive.strip().lower() in ("true", "yes", "1")
    return Written(post, tags, query, bool(india), bool(duplicate), bool(sensitive))


_fail_streak = 0  # consecutive requests that no provider could answer


def _ask(prompt: str, parse, what: str, ref: str):
    """Run a prompt through the provider chain; return parse(reply) from the first that works."""
    global _fail_streak
    slots = build_slots()
    if not slots:
        logger.error("No LLM provider configured: set GEMINI_API_KEY and/or GROQ_API_KEY etc.")
        return None
    for slot in slots:
        if _cooldown_until.get(slot, 0) > time.time():
            continue
        try:
            _pace(slot.provider)
            result = parse(_call(slot, prompt))
        except Exception as e:
            cool = _cooldown_seconds(e)
            if cool:
                _cooldown_until[slot] = time.time() + cool
            logger.warning("%s failed (%s)%s: %s", slot, ref[-40:],
                           f", skipping it for {cool:.0f}s" if cool else "", str(e)[:200])
            continue
        logger.info("%s done with %s", what, slot)
        _fail_streak = 0
        return result
    logger.error("All LLM providers failed or are cooling down (%s) for %s", what, ref)
    _fail_streak += 1
    if _fail_streak >= 3:
        alerts.alert("llm_down", "No AI provider could answer the last 3 requests, so drafts can't be written "
                                 "right now. Check the API keys and free-tier quotas (python -m app.cli --check-llm).",
                     every_hours=3)
    return None


VERIFY_PROMPT = """You are a strict fact-checker for a news account. Compare the POST with the ARTICLE.

List every claim in the POST that the ARTICLE text does not clearly support. Pay special attention to:
who people are (profession, nationality, titles, relationships), numbers, dates, places, and who said or
did what. A description of a person must come from the ARTICLE itself, not from general knowledge, and a
famous namesake must not be assumed. Minor rewording is fine; invented or changed facts are not.

Also judge the HASHTAGS: a hashtag is irrelevant if it is not clearly about the main subject of this
article and post (a person, place, organisation, event or theme the text really covers). Generic or
misleading hashtags are irrelevant.

Return ONLY JSON: {{"unsupported": ["claim 1"], "irrelevant_hashtags": ["Tag"]}}
(use empty lists when everything is supported / every hashtag is relevant)

ARTICLE:
{content}

POST:
{post}

HASHTAGS: {hashtags}"""

# Returned by write_post when the AI could not produce a post that its own fact-check accepts
UNVERIFIED = object()

# Results are cached so the same story is never written or checked twice (saves free-tier AI quota),
# e.g. when a draft is skipped and the next command meets the same candidate again.
_write_cache = cache.TTLCache(2 * 3600)
_verify_cache = cache.TTLCache(6 * 3600)
_judge_cache = cache.TTLCache(6 * 3600)


def _digest(*parts: str) -> str:
    return hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()


def clear_caches() -> None:
    for c in (_write_cache, _verify_cache, _judge_cache):
        c.clear()


def _verify_post(content: str, post: str, tags: list[str] | None = None):
    """Fact-check `post` against the article and judge the hashtags.

    Returns (unsupported_claims, irrelevant_hashtags); both empty means all good. None if no AI
    provider could answer."""
    def parse(raw: str) -> tuple[list[str], list[str]]:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("no JSON object in reply")
        data = json.loads(match.group())
        claims = [str(c).strip() for c in data["unsupported"] if str(c).strip()]
        bad = [re.sub(r"[^A-Za-z0-9_]", "", str(t)).lower() for t in data.get("irrelevant_hashtags") or []]
        return claims, [t for t in bad if t]

    shown = " ".join("#" + t for t in tags or []) or "(none)"
    return _ask(VERIFY_PROMPT.format(content=content[:config.ARTICLE_CHARS], post=post, hashtags=shown), parse,
                "Fact-check", post[:40])


def verify_post(content: str, post: str, tags: list[str] | None = None):
    """Cached fact-check (see _verify_post)."""
    key = _digest(content[:config.ARTICLE_CHARS], post, ",".join(tags or []))
    hit = _verify_cache.get(key)
    if hit is not cache.MISSING:
        return hit
    result = _verify_post(content, post, tags)
    if result is not None:  # never cache "no provider answered"
        _verify_cache.set(key, result)
    return result


def write_post(item: dict, content: str):
    """Write the post and fact-check it against the article (cached per story and recent-post list).

    Returns a Written(post, tags, photo_query, is_india, is_duplicate, is_sensitive) whose tags are the
    story-specific hashtags that survived the relevance check; None if the AI providers failed; or
    UNVERIFIED if even a second, corrected attempt still contained claims the article does not support."""
    recent = "\n".join(f"- {t[:140]}" for t in state.recent_titles(30)) or "(nothing yet)"
    key = _digest(item["url"], content[:config.ARTICLE_CHARS], recent, item["category"])
    hit = _write_cache.get(key)
    if hit is not cache.MISSING:
        return hit._replace(tags=list(hit.tags)) if isinstance(hit, Written) else hit
    result = _write_post(item, content, recent)
    if result is not None:  # never cache "the AI providers failed"
        _write_cache.set(key, result)
    return result


def _write_post(item: dict, content: str, recent: str):
    prompt = PROMPT.format(category=item["category"], title=item["title"], content=content[:config.ARTICLE_CHARS],
                           recent=recent)
    for attempt in (1, 2):
        written = _ask(prompt, _parse, "Post", item["url"])
        if not written:
            return None
        if not written.is_india or written.is_duplicate or written.is_sensitive:
            return written  # it will not be posted: no need to fact-check
        checked = verify_post(content, written[0], written[1])
        if checked is None:
            return None  # cannot fact-check right now: do not publish unchecked text
        unsupported, irrelevant = checked
        if not unsupported:
            keep = [t for t in written[1] if t.lower() not in irrelevant]
            if len(keep) < len(written[1]):
                logger.info("Dropped irrelevant hashtags: %s", ", ".join(t for t in written[1] if t not in keep))
            return written._replace(tags=keep)
        logger.warning("Fact-check rejected a draft (%s): %s", item["url"][-40:], "; ".join(unsupported)[:300])
        if attempt == 2:
            return UNVERIFIED
        prompt += ("\n\nYour previous draft contained claims the article does NOT support:\n- "
                   + "\n- ".join(unsupported[:5])
                   + "\nWrite the post again using only what the article states. Do not describe who "
                     "anyone is unless the article says so.")
    return UNVERIFIED


JUDGE_PROMPT = """You choose a stock photo for a news post. Be STRICT: a wrong photo is worse than none.

Story title: {title}
Post: {post}

Candidate photos (only their descriptions are known):
{options}

Pick the ONE photo whose description clearly shows the main subject of the story. Answer 0 if none
does: generic or loosely related photos, a different subject, or photos of people that could be mistaken
for the people in the story must all get 0.
Return ONLY JSON: {{"choice": <number>}}"""


def judge_photo(item: dict, post: str, candidates: list[dict]) -> int | None:
    """Index into `candidates` of a clearly relevant photo, or None if none is good enough."""
    options = "\n".join(f"{i}. {c['desc'][:160]}" for i, c in enumerate(candidates, 1))
    prompt = JUDGE_PROMPT.format(title=item["title"], post=post, options=options)

    def parse(raw: str) -> int:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("no JSON object in reply")
        return int(json.loads(match.group())["choice"])

    key = _digest(item["url"], item["title"], post, options)
    choice = _judge_cache.get(key)
    if choice is cache.MISSING:
        choice = _ask(prompt, parse, "Photo check", item["url"])
        if choice is not None:  # 0 means "none fits": a real answer, cached too
            _judge_cache.set(key, choice)
    return choice - 1 if choice and 1 <= choice <= len(candidates) else None


def generate_post(item: dict, content: str) -> str | None:
    """Convenience: write the post and return the final text."""
    written = write_post(item, content)
    return compose(written[0], written[1], item["source"], link=item["url"]) if written else None


_ABBREVIATIONS = {"dr", "mr", "mrs", "ms", "prof", "sr", "jr", "st", "no", "rs", "vs", "inc", "ltd", "co", "etc",
                  "gen", "col", "capt", "sri", "smt", "hon", "govt", "dept", "approx", "est", "mt", "ft"}


def _last_sentence_end(text: str) -> int:
    """Index of the last real sentence end (. ! ?) in `text`, or -1. A full stop after an abbreviation
    ("Dr.", "Rs.", "vs.") or a single initial ("B.", "U.S.") is not a sentence end."""
    for m in reversed(list(re.finditer(r"[.!?](?=\s)", text))):
        word = re.search(r"([A-Za-z]+)$", text[: m.start()])
        if m.group() == "." and word and (word.group(1).lower() in _ABBREVIATIONS or len(word.group(1)) == 1):
            continue
        return m.start()
    return -1


def compose(post: str, tags: list[str], source: str, link: str | None = None,
            photo_credit: str | None = None) -> str:
    """Final post text, within the 500-character Threads limit. The layout is:

        the post text
        (blank line)
        #Hashtags
        the article link
        ONE attribution line

    The attribution line is "📷 Image: <publisher>" when the picture is the article's own image, and
    otherwise (a stock photo, or no picture) "Source: <publisher>". It is never both, and a stock photo is
    never credited. Threads allows ONE topic tag per post: the first #tag in the text is the one it uses,
    so the body contains no '#' and the category tag comes first."""
    post = post.replace("#", "").strip()
    parts = []
    if tags:
        parts.append(" ".join(f"#{t}" for t in tags))
    if link:
        parts.append(link)
    parts.append(f"\U0001F4F7 {photo_credit}" if photo_credit else f"Source: {source}")
    tail = "\n\n" + "\n".join(parts)
    room = THREADS_LIMIT - len(tail)
    if len(post) > room:
        cut = post[:room]
        end = _last_sentence_end(cut)
        if end >= len(cut) * 0.4:
            post = cut[: end + 1]  # keep whole sentences
        else:
            post = cut[: room - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "\u2026"
    return post + tail


def check_slots() -> list[tuple[Slot, bool, str, float]]:
    """Send one tiny request to every configured slot (ignores cooldowns). For --check-llm."""
    results = []
    for slot in build_slots():
        start = time.time()
        try:
            _pace(slot.provider)
            reply = _call(slot, 'Reply with only this JSON: {"post": "ok", "topic": "test"}')
            _parse(reply)
            results.append((slot, True, "OK", time.time() - start))
        except Exception as e:
            results.append((slot, False, str(e)[:160], time.time() - start))
    return results
