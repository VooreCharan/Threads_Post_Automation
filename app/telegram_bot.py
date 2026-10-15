"""Telegram remote control (long polling, no webhook or public URL needed).

Only the owner (TELEGRAM_OWNER_ID) is answered. Say "hi" for the command list, then e.g. "post sports":
the bot prepares ONE draft from the latest sports story and shows it with buttons. Nothing reaches
Threads until you tap "Post to Threads" (turn that off with REQUIRE_APPROVAL=false).
"""
import logging
import re
import threading
import time

import requests

from . import bot, config, drafts, safety, state, threads
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

# what to tell the user when a category has nothing postable
NOTHING_TEXT = {
    "no_new_articles": "No new {c} stories right now (the recent ones are already posted). Try another category.",
    "all_duplicates": "The latest {c} stories are about topics I've already posted. Try again a bit later, or pick another category.",
    "no_india_news": "The latest {c} stories weren't about India, so I skipped them. Try again later or pick another category.",
    "sensitive": "The latest {c} stories were about sensitive topics (violence, deaths, tragedies), which I skip to keep the account safe. Try another category.",
    "unverified": "I couldn't write a reliably accurate post for the latest {c} stories (my fact-check rejected them), so I drafted nothing. Try again later or pick another category.",
    "thin_text": "The latest {c} stories didn't have enough readable text to write a reliable post. Try again later or pick another category.",
    "generation_failed": "⚠️ The AI writers failed just now. Please try again in a minute.",
}


def enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN)


def _call(method: str, http_timeout: int = 40, **params) -> dict:
    resp = requests.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method=method),
                         json=params, timeout=http_timeout)
    resp.raise_for_status()
    return resp.json()


def send(chat_id: int | str, text: str, reply_markup: dict | None = None) -> dict | None:
    """Send a text message; returns Telegram's message object (or None on failure)."""
    params = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
    if reply_markup:
        params["reply_markup"] = reply_markup
    try:
        return _call("sendMessage", **params).get("result")
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return None


def commands_text() -> str:
    lines = [f"• post {c} – latest {DESCRIPTIONS.get(c, c)} update" for c in FEEDS]
    return ("Commands:\n" + "\n".join(lines) +
            "\n• status – today's posts, limits and waiting drafts"
            "\n• cancel – discard all waiting drafts"
            "\n• help – show this list again")


def greeting_text() -> str:
    how = ("I prepare a draft of ONE latest Indian news update and show it to you. Nothing is posted to "
           "Threads until you tap “Post to Threads”." if config.REQUIRE_APPROVAL else
           "I post ONE latest Indian news update to your Threads account whenever you ask.")
    return f"👋 Hi! {how}\n\n{commands_text()}\n\nExample: send  post sports"


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
    limit = safety.daily_limit_now()
    age = safety.account_age_days()
    lines = [f"Posts in the last 24h: {n} / {limit}"
             + (f" (new-account warm-up, day {int(age) + 1}; normal limit {config.DAILY_POST_LIMIT})"
                if limit < config.DAILY_POST_LIMIT else ""),
             f"Minimum pause between posts: {config.MIN_SECONDS_BETWEEN_POSTS // 60} min",
             f"Approval before posting: {'ON' if config.REQUIRE_APPROVAL else 'OFF'}"
             f" | Waiting drafts: {drafts.count()}",
             f"Images: {config.IMAGE_MODE}"]
    last = state.recent_posts(3)
    if last:
        lines.append("Latest posts:")
        lines += [f"• [{c}] {u}" for u, _, c, _ in last]
    return "\n".join(lines)


# ---- drafts ------------------------------------------------------------------------------------

def _keyboard(draft_id: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ Post to Threads", "callback_data": f"ok:{draft_id}"}],
        [{"text": "🔄 Another story", "callback_data": f"re:{draft_id}"},
         {"text": "❌ Skip", "callback_data": f"no:{draft_id}"}]]}


def _preview_text(draft: dict) -> str:
    label = bot.image_label(draft)
    return (f"📝 DRAFT [{draft['category']}] {label}\n"
            f"Nothing is posted until you tap the button below.\n"
            f"──────────\n{draft['text']}\n──────────\n"
            f"Article: {draft['url']}\n"
            f"Expires in {config.DRAFT_TTL_MINUTES} min.")


def _send_draft(chat_id, draft: dict) -> None:
    """Show a draft with its buttons; the image is attached as a photo when Telegram can fetch it."""
    draft_id = drafts.add(draft)
    markup = _keyboard(draft_id)
    text = _preview_text(draft)
    message = None
    if draft.get("image_url") and len(text) <= 1000:
        try:
            message = _call("sendPhoto", chat_id=chat_id, photo=draft["image_url"], caption=text,
                            reply_markup=markup).get("result")
        except Exception as e:
            logger.info("Could not attach the photo to the preview (%s)", str(e)[:100])
    if message is None:
        message = send(chat_id, text + (f"\nImage: {draft['image_url']}" if draft.get("image_url") else ""),
                       reply_markup=markup)
    if message:
        drafts.update(draft_id, chat_id=chat_id, message_id=message.get("message_id"))


def _report_posted(chat_id, result: dict) -> None:
    link = threads.permalink(result["post_id"])
    send(chat_id, f"✅ Posted to Threads [{result['category']}] {bot.image_label(result)}\n"
                  + (f"Threads post: {link}\n" if link else "") + f"Article: {result['url']}")


def _do_draft(chat_id, category: str) -> None:
    """Background thread: prepare a draft, then preview it (approval on) or publish it (approval off)."""
    try:
        draft, status = bot.make_draft(category)
        if draft is None:
            if status == "busy":
                send(chat_id, "⏳ I'm still working on the previous request. Try again in a moment.")
            else:
                send(chat_id, NOTHING_TEXT.get(status, "⚠️ Something went wrong: " + status).format(c=category))
            return
        if config.REQUIRE_APPROVAL:
            _send_draft(chat_id, draft)
            return
        # approval off: publish straight away (same safety limits apply)
        reason = bot.blocked_reason()
        if reason:
            send(chat_id, f"⏸ {reason}")
            return
        drafts_id = drafts.add(draft)
        result = bot.approve(drafts_id)
        _report_result(chat_id, result)
    except Exception as e:  # never leave the user without an answer
        logger.exception("post command failed")
        send(chat_id, f"⚠️ Something went wrong: {str(e)[:300]}")


def _report_result(chat_id, result: dict) -> None:
    status = result.get("status")
    if status == "posted":
        _report_posted(chat_id, result)
    elif status == "expired":
        send(chat_id, "This draft has expired or was already handled. Send the post command again.")
    elif status == "blocked":
        send(chat_id, f"⏸ {result['reason']}")
    elif status == "busy":
        send(chat_id, "⏳ I'm still working on something else. Tap again in a moment.")
    elif status == "already_posted":
        send(chat_id, "That story has already been posted.")
    else:
        send(chat_id, f"⚠️ Couldn't publish to Threads.\n{result.get('error', '')[:400]}")


# ---- incoming messages and button taps ------------------------------------------------------------

def _is_owner(user_id) -> bool:
    return bool(config.TELEGRAM_OWNER_ID) and str(user_id) == config.TELEGRAM_OWNER_ID


def _handle(chat_id: int, user_id: int, text: str) -> None:
    if not config.TELEGRAM_OWNER_ID:
        send(chat_id, f"Your Telegram user id is {user_id}. Put it in TELEGRAM_OWNER_ID and restart "
                      "the app to control the bot.")
        return
    if not _is_owner(user_id):
        logger.warning("Ignored Telegram message from non-owner %s", user_id)
        return

    cmd = _normalise(text)
    if cmd in GREETINGS:
        send(chat_id, greeting_text())
    elif cmd == "status":
        send(chat_id, _status_text())
    elif cmd == "cancel":
        n = drafts.clear()
        send(chat_id, f"Discarded {n} waiting draft{'s' if n != 1 else ''}." if n else "No drafts are waiting.")
    elif cmd == "post" or cmd.startswith("post "):
        word = cmd[5:].strip()
        category = _parse_category(word) if word else None
        if category is None:
            send(chat_id, ("Which category? " if not word else f"I don't know the category “{word}”. ")
                 + "\n\n" + commands_text())
            return
        reason = bot.blocked_reason(check_pause=False)  # the pause is enforced when you approve
        if reason:
            send(chat_id, f"⏸ {reason}")
            return
        if config.REQUIRE_APPROVAL and drafts.count() >= config.MAX_PENDING_DRAFTS:
            send(chat_id, f"You already have {drafts.count()} drafts waiting. Approve or skip them first "
                          "(or send “cancel” to discard them).")
            return
        if not config.REQUIRE_APPROVAL:
            reason = bot.blocked_reason()
            if reason:
                send(chat_id, f"⏸ {reason}")
                return
        if bot.is_busy():
            send(chat_id, "⏳ I'm still working on the previous request. Try again in a moment.")
            return
        send(chat_id, f"⏳ Finding the latest {category} update…")
        threading.Thread(target=_do_draft, args=(chat_id, category), daemon=True).start()
    else:
        send(chat_id, "I didn't understand that.\n\n" + commands_text())


def _answer(callback_id: str, text: str = "", alert: bool = False) -> None:
    try:
        _call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190], show_alert=alert)
    except Exception as e:
        logger.info("answerCallbackQuery failed: %s", str(e)[:100])


def _remove_buttons(chat_id, message_id) -> None:
    try:
        _call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup={"inline_keyboard": []})
    except Exception as e:
        logger.info("Could not remove the buttons: %s", str(e)[:100])


def _approve_in_background(chat_id, draft_id: str) -> None:
    try:
        _report_result(chat_id, bot.approve(draft_id))
    except Exception as e:
        logger.exception("approve failed")
        send(chat_id, f"⚠️ Something went wrong: {str(e)[:300]}")


def _handle_callback(cb: dict) -> None:
    callback_id = cb["id"]
    if not _is_owner(cb.get("from", {}).get("id")):
        _answer(callback_id)
        return
    message = cb.get("message") or {}
    chat_id, message_id = message.get("chat", {}).get("id"), message.get("message_id")
    action, _, draft_id = (cb.get("data") or "").partition(":")
    draft = drafts.get(draft_id)
    if draft is None:
        _answer(callback_id, "This draft has expired or was already handled.", alert=True)
        if chat_id and message_id:
            _remove_buttons(chat_id, message_id)
        return

    if action == "ok":
        reason = bot.blocked_reason()
        if reason:  # keep the draft and its buttons: you can approve later, before it expires
            _answer(callback_id, reason, alert=True)
            return
        _answer(callback_id, "Posting…")
        _remove_buttons(chat_id, message_id)
        threading.Thread(target=_approve_in_background, args=(chat_id, draft_id), daemon=True).start()
    elif action in ("no", "re"):
        bot.reject(draft_id, "rejected by owner" if action == "no" else "owner asked for another story")
        _remove_buttons(chat_id, message_id)
        if action == "no":
            _answer(callback_id, "Skipped")
            send(chat_id, "❌ Skipped. I won't propose that story again. Send a post command for another.")
        else:
            _answer(callback_id, "Finding another story…")
            send(chat_id, f"⏳ Finding another {draft['category']} update…")
            threading.Thread(target=_do_draft, args=(chat_id, draft["category"]), daemon=True).start()
    else:
        _answer(callback_id)


def poll_forever() -> None:
    offset = None
    logger.info("Telegram bot polling started")
    while True:
        try:
            params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            for update in _call("getUpdates", http_timeout=45, **params).get("result", []):
                offset = update["update_id"] + 1
                if update.get("callback_query"):
                    _handle_callback(update["callback_query"])
                    continue
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
