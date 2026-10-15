"""Warnings sent to the owner on Telegram when something needs attention (all AI providers failing,
the database unreachable, the Threads token about to expire). Each kind is sent at most once per
cooldown, so a persistent problem does not flood the chat."""
import logging
import time

from . import config, state

logger = logging.getLogger(__name__)
_last: dict[str, float] = {}


def notify_owner(text: str) -> bool:
    """Send a message to the owner. False if Telegram is not configured or the send failed."""
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_OWNER_ID):
        return False
    from . import telegram_bot  # imported here: telegram_bot depends on modules that raise alerts
    return telegram_bot.send(config.TELEGRAM_OWNER_ID, text) is not None


def alert(kind: str, text: str, every_hours: float = 6.0, persist: bool = True) -> bool:
    """Warn the owner about `kind` unless the same kind was sent within `every_hours`.

    persist=True remembers the time in the database too (so a restart does not repeat the alert);
    use persist=False for database problems, where the database cannot be relied on."""
    now = time.time()
    window = every_hours * 3600
    if now - _last.get(kind, 0) < window:
        return False
    key = f"alert:{kind}"
    if persist:
        try:
            last = float(state.get(key) or 0)
        except Exception:
            last = 0
        if now - last < window:
            _last[kind] = last
            return False
    _last[kind] = now
    if persist:
        try:
            state.put(key, str(now))
        except Exception:
            pass
    logger.warning("ALERT %s: %s", kind, text)
    return notify_owner(f"⚠️ {text}")


def reset() -> None:
    """Forget in-memory cooldowns (used by tests)."""
    _last.clear()
