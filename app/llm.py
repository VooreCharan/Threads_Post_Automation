"""Write a Threads post for a news item using a chain of free LLM providers.

Providers are tried in LLM_CHAIN order (Gemini, Groq, OpenRouter, Hugging Face, GitHub Models).
A provider/model that is overloaded, rate-limited or misconfigured goes on cooldown, so later posts
skip it instead of waiting on it again.
"""
import json
import logging
import re
import threading
import time
from dataclasses import dataclass

import requests
from google import genai

from . import config, state

logger = logging.getLogger(__name__)

THREADS_LIMIT = 500
HTTP_TIMEOUT = 60

PROMPT = """You run a news account on Threads. Write a post about the article below, using ONLY
facts stated in it. Put it in your own words (do not copy sentences).
Be exact about WHO said or did WHAT: never give a quote, opinion or action to the wrong person (the
subject of a story is often not the person speaking). If you are unsure who said something, state the
facts without attributing them, and do not add details that the article does not contain.

Return ONLY a JSON object:
{{"india": true, "duplicate": false, "post": "...", "topic": "...", "photo_query": "..."}}
- duplicate: true if this story reports the SAME news event or announcement as any title in the
  "Recently posted" list below, even when worded differently or published by another outlet. false for
  a different event, even on a similar subject (a different match, company, person or announcement).
- india: true ONLY if the story is mainly about India: events or places in India, the Indian government
  or politics, Indian people, companies, markets, sport or culture, or foreign news that directly
  affects India. false if it is mainly about another country or region (even if an Indian outlet
  reported it) or is global news with no Indian angle.
- post: 1-3 short sentences, at most 330 characters, clear and neutral, no URL, no hashtags, no emoji spam
- topic: ONE word or short phrase for a Threads topic tag (letters/numbers/spaces only)
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
    resp = requests.post(f"{cfg['base']}/chat/completions",
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

def _parse(raw: str) -> tuple[str, str, str, bool, bool]:
    """Pull {"post", "topic", "photo_query", "india"} out of a model reply (tolerates fences)."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in reply")
    data = json.loads(match.group())
    post = str(data["post"]).strip().strip('"')
    if not post:
        raise ValueError("empty post")
    topic = re.sub(r"[^A-Za-z0-9 ]", "", str(data.get("topic", ""))).strip()
    query = re.sub(r"[^A-Za-z0-9 ]", "", str(data.get("photo_query", "") or "")).strip()[:60]
    india = data.get("india", True)
    if isinstance(india, str):
        india = india.strip().lower() in ("true", "yes", "1")
    duplicate = data.get("duplicate", False)
    if isinstance(duplicate, str):
        duplicate = duplicate.strip().lower() in ("true", "yes", "1")
    return post, topic, query, bool(india), bool(duplicate)


def _ask(prompt: str, parse, what: str, ref: str):
    """Run a prompt through the provider chain; return parse(reply) from the first that works."""
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
        return result
    logger.error("All LLM providers failed or are cooling down (%s) for %s", what, ref)
    return None


def write_post(item: dict, content: str) -> tuple[str, str, str, bool, bool] | None:
    """Return (post, topic, photo_query, is_india, is_duplicate) from the first working provider."""
    recent = "\n".join(f"- {t[:140]}" for t in state.recent_titles(30)) or "(nothing yet)"
    prompt = PROMPT.format(category=item["category"], title=item["title"], content=content[:2500],
                           recent=recent)
    return _ask(prompt, _parse, "Post", item["url"])


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

    choice = _ask(prompt, parse, "Photo check", item["url"])
    return choice - 1 if choice and 1 <= choice <= len(candidates) else None


def generate_post(item: dict, content: str) -> str | None:
    """Convenience: write the post and return the final text (no image, no link in the text)."""
    written = write_post(item, content)
    return compose(written[0], written[1], item["source"]) if written else None


def compose(post: str, topic: str, source: str, link: str | None = None,
            photo_credit: str | None = None) -> str:
    """Final post text within the 500-char Threads limit.

    post + [photo credit] + source credit and one topic tag + [article link].
    The link goes in the text only for image posts: Threads cannot show a link preview on them."""
    parts = []
    if photo_credit:
        parts.append(f"\U0001F4F7 Photo: {photo_credit}")
    parts.append(f"Source: {source}" + (f"  #{topic}" if topic else ""))
    if link:
        parts.append(link)
    tail = "\n\n" + "\n".join(parts)
    room = THREADS_LIMIT - len(tail)
    if len(post) > room:
        cut = post[:room]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
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
