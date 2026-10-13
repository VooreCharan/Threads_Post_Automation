"""Telegram remote control (long polling, no webhook or public URL needed).

Chat commands (only from TELEGRAM_OWNER_ID):
  start posting - post continuously with a random gap until stopped
  stop posting  - turn it off
  post now      - publish one round immediately
  status        - show on/off, interval and last posts
"""
import logging
import re
import threading
import time

import requests

from . import bot, config, state

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
HELP = ("Commands:\n"
        "• start posting – post continuously (30-60 s apart) until you stop it\n"
        "• stop posting – stop automatic posting\n"
        "• post now – publish one round now\n"
        "• status – current state")


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


def notify(text: str) -> None:
    """Message the owner (used for scheduled-run reports). Silent no-op if not configured."""
    if enabled() and config.TELEGRAM_OWNER_ID:
        send(config.TELEGRAM_OWNER_ID, text)


def _command(text: str) -> str:
    t = text.strip().lower()
    t = re.sub(r"@\w+$", "", t)          # /start_posting@MyBot
    t = t.lstrip("/").replace("_", " ")
    t = re.sub(r"\s+", " ", t)
    aliases = {"startposting": "start posting", "stopposting": "stop posting", "postnow": "post now"}
    return aliases.get(t, t)


def _status_text() -> str:
    on = bot.posting_enabled()
    lines = [f"Auto-posting: {'ON ✅' if on else 'OFF ⏸'}",
             f"Posts this session: {bot.session_posts()}",
             f"Gap between posts: {config.POST_GAP_MIN_SECONDS}-{config.POST_GAP_MAX_SECONDS}s (random)",
             f"Posts in last 24h: {state.posts_in_window(86400)[0]} / {config.THREADS_DAILY_LIMIT}",
             f"Categories: {', '.join(config.CATEGORIES) or 'all'}"]
    last = state.recent_posts(3)
    if last:
        lines.append("Last posts:")
        lines += [f"• [{c}] {u}" for u, _, c, _ in last]
    return "\n".join(lines)


def _run_in_background(chat_id, fn) -> None:
    def work():
        send(chat_id, bot.format_results(fn()))
    threading.Thread(target=work, daemon=True).start()


def _handle(chat_id: int, user_id: int, text: str) -> None:
    if not config.TELEGRAM_OWNER_ID:
        send(chat_id, f"Your Telegram user id is {user_id}. Put it in TELEGRAM_OWNER_ID and restart "
                      "the app to control the bot.")
        return
    if str(user_id) != config.TELEGRAM_OWNER_ID:
        logger.warning("Ignored Telegram message from non-owner %s", user_id)
        return

    cmd = _command(text)
    if cmd == "start posting":
        bot.set_posting(True)
        send(chat_id, "Posting started ✅ — posting continuously with a random "
                      f"{config.POST_GAP_MIN_SECONDS}-{config.POST_GAP_MAX_SECONDS}s gap between posts. "
                      "Say 'stop posting' to stop.")
    elif cmd == "stop posting":
        was_on = bot.posting_enabled()
        bot.set_posting(False)
        if not was_on and not bot.is_busy():
            send(chat_id, f"Posting is already stopped. Last session: {bot.session_posts()} post(s).")
            return
        if bot.is_busy():
            send(chat_id, "Stop received ⏸ — finishing the post in progress, then stopping…")

        def finish():
            bot.wait_until_idle()  # the in-flight post completes first
            n = bot.session_posts()
            send(chat_id, f"⏹ Posting stopped. {n} post{'s' if n != 1 else ''} published this session.")
        threading.Thread(target=finish, daemon=True).start()
    elif cmd == "post now":
        send(chat_id, "Posting one round…")
        _run_in_background(chat_id, bot.run_once)
    elif cmd == "status":
        send(chat_id, _status_text())
    else:
        send(chat_id, HELP)


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
