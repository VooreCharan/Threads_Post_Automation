"""Telegram remote control (long polling, no webhook or public URL needed).

Only the owner (TELEGRAM_OWNER_ID) is answered. Say "hi" for the command list, then e.g.
"post sports": the bot publishes ONE latest update from that category to Threads and replies with the
Threads link. Other commands: "status", "help".
"""
import logging
import re
import threading
import time

import requests

from . import bot, config, state, threads
from .sources import FEEDS

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

DESCRIPTIONS = {
    "india": "India national news",
    "business": "business and markets",
    "technology": "technology and gadgets",
    "sports": "sports and cricket",
    "entertainment": "movies and entertainment",
    "health": "health",
    "science": "science and space",
    "environment": "environment and climate",
}
# Alternative words accepted after "post"
ALIASES = {
    "tech": "technology", "gadgets": "technology", "sport": "sports", "cricket": "sports",
    "news": "india", "national": "india", "politics": "india", "biz": "business", "finance": "business",
    "market": "business", "markets": "business", "movies": "entertainment", "movie": "entertainment",
    "bollywood": "entertainment", "env": "environment", "climate": "environment", "space": "science",
}
GREETINGS = {"hi", "hii", "hiii", "hello", "hey", "hello there", "hi there", "start", "help", "menu",
             "commands", "list", "?"}


def enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN)


def _call(method: str, http_timeout: int = 40, **params) -> dict:
    resp = requests.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method=method),
                         json=params, timeout=http_timeout)
    resp.raise_for_status()
    return resp.json()


def send(chat_id: int | str, text: str) -> None:
    try:
        _call("sendMessage", chat_id=chat_id, text=text[:4000], disable_web_page_preview=True)
    except Exception as e:
        logger.error("Telegram send failed: %s", e)


def commands_text() -> str:
    cats = [c for c in FEEDS]
    lines = [f"• post {c} – latest {DESCRIPTIONS.get(c, c)} update" for c in cats]
    return ("Commands:\n" + "\n".join(lines) +
            "\n• status – today's posts and the latest ones\n• help – show this list again")


def greeting_text() -> str:
    return ("👋 Hi! I post ONE latest Indian news update to your Threads account whenever you ask.\n\n"
            + commands_text() + "\n\nExample: send  post sports")


def _normalise(text: str) -> str:
    t = text.strip().lower()
    t = re.sub(r"@\w+$", "", t)          # /post_sports@MyBot
    t = t.lstrip("/").replace("_", " ")
    return re.sub(r"\s+", " ", t).strip()


def _parse_category(word: str) -> str | None:
    word = word.strip()
    if word in FEEDS:
        return word
    return ALIASES.get(word) if ALIASES.get(word) in FEEDS else None


def _status_text() -> str:
    n = state.posts_in_window(86400)[0]
    lines = [f"Posts in the last 24h: {n} / {config.DAILY_POST_LIMIT}",
             f"Minimum pause between posts: {config.MIN_SECONDS_BETWEEN_POSTS}s",
             f"Categories: {', '.join(FEEDS)}"]
    last = state.recent_posts(3)
    if last:
        lines.append("Latest posts:")
        lines += [f"• [{c}] {u}" for u, _, c, _ in last]
    return "\n".join(lines)


def _do_post(chat_id, category: str) -> None:
    """Runs in a background thread: publish one story and report back."""
    try:
        results = bot.run_once([category], 1)
        posted = next((r for r in results if r.get("status") == "posted"), None)
        if posted:
            link = threads.permalink(posted["post_id"])
            send(chat_id, f"✅ Posted to Threads [{category}] {bot.image_label(posted)}\n"
                          + (f"Threads post: {link}\n" if link else "")
                          + f"Article: {posted['url']}")
            return
        first = results[0] if results else {}
        status = first.get("status", "")
        if status == "dry_run":
            send(chat_id, bot.format_results(results))
        elif status == "busy":
            send(chat_id, "⏳ I'm still working on the previous post. Try again in a moment.")
        elif status == "no_new_articles":
            send(chat_id, f"No new {category} stories right now (the recent ones are already posted). "
                          "Try another category.")
        elif status == "all_duplicates":
            send(chat_id, f"The latest {category} stories are about topics I've already posted. "
                          "Try again a bit later, or pick another category.")
        elif status == "no_india_news":
            send(chat_id, f"The latest {category} stories weren't about India, so I skipped them. "
                          "Try again later or pick another category.")
        elif status.startswith("publish_failed"):
            send(chat_id, f"⚠️ Couldn't publish to Threads.\n{status[:400]}")
        else:
            send(chat_id, "⚠️ The AI writers failed just now. Please try again in a minute.")
    except Exception as e:  # never leave the user without an answer
        logger.exception("post command failed")
        send(chat_id, f"⚠️ Something went wrong: {str(e)[:300]}")


def _handle(chat_id: int, user_id: int, text: str) -> None:
    if not config.TELEGRAM_OWNER_ID:
        send(chat_id, f"Your Telegram user id is {user_id}. Put it in TELEGRAM_OWNER_ID and restart "
                      "the app to control the bot.")
        return
    if str(user_id) != config.TELEGRAM_OWNER_ID:
        logger.warning("Ignored Telegram message from non-owner %s", user_id)
        return

    cmd = _normalise(text)
    if cmd in GREETINGS:
        send(chat_id, greeting_text())
    elif cmd == "status":
        send(chat_id, _status_text())
    elif cmd == "post" or cmd.startswith("post "):
        word = cmd[5:].strip()
        category = _parse_category(word) if word else None
        if category is None:
            send(chat_id, ("Which category? " if not word else f"I don't know the category “{word}”. ")
                 + "\n\n" + commands_text())
            return
        reason = bot.blocked_reason()
        if reason:
            send(chat_id, f"⏸ {reason}")
            return
        if bot.is_busy():
            send(chat_id, "⏳ I'm still working on the previous post. Try again in a moment.")
            return
        send(chat_id, f"⏳ Finding the latest {category} update…")
        threading.Thread(target=_do_post, args=(chat_id, category), daemon=True).start()
    else:
        send(chat_id, "I didn't understand that.\n\n" + commands_text())


def poll_forever() -> None:
    offset = None
    logger.info("Telegram bot polling started")
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            for update in _call("getUpdates", http_timeout=45, **params).get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or {}
                if msg.get("text") and msg.get("from"):
                    _handle(msg["chat"]["id"], msg["from"]["id"], msg["text"])
        except Exception as e:
            logger.error("Telegram polling error: %s", e)
            time.sleep(5)


def start_polling_thread() -> None:
    if enabled():
        threading.Thread(target=poll_forever, daemon=True, name="telegram-poller").start()
    else:
        logger.info("TELEGRAM_BOT_TOKEN not set; Telegram control disabled")
