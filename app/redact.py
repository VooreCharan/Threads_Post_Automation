"""Keeps secrets out of logs, alerts and Telegram messages.

When a network call fails, the error text contains the full web address, and some of our addresses carry
a secret (Telegram's has /bot<token>/ in the path; Threads and Pixabay put a key in the query string).
Every log line and every message sent to Telegram is passed through redact() first."""
import logging
import re

from . import config

_PATTERNS = [
    (re.compile(r"(access_token=)[^&\s'\")]+", re.I), r"\1***"),
    (re.compile(r"([?&](?:key|api_key|apikey|token|client_secret)=)[^&\s'\")]+", re.I), r"\1***"),
    (re.compile(r"(/bot)\d+:[A-Za-z0-9_-]+"), r"\1***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1***"),
    (re.compile(r"THAA[A-Za-z0-9_-]{20,}"), "***"),
]


def _secret_values() -> list[str]:
    """The actual secret values in use (read at call time so changed settings are honoured)."""
    values = [config.TELEGRAM_BOT_TOKEN, config.THREADS_ACCESS_TOKEN, config.GEMINI_API_KEY, config.TURSO_TOKEN,
              config.PEXELS_API_KEY, config.PIXABAY_API_KEY, config.UNSPLASH_ACCESS_KEY,
              config.TELEGRAM_WEBHOOK_SECRET]
    values += [p["key"] for p in config.OPENAI_COMPAT.values()]
    return sorted({v for v in values if v and len(v) >= 8}, key=len, reverse=True)  # longest first


def redact(text) -> str:
    text = str(text)
    for secret in _secret_values():
        text = text.replace(secret, "***")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactFilter(logging.Filter):
    """Rewrites each log record (message and traceback) with secrets removed."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
        except Exception:  # logging must never fail because of the filter
            pass
        return True


def install() -> None:
    """Attach the filter to every handler of the root logger (all of this app's logs go through them)."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactFilter) for f in handler.filters):
            handler.addFilter(RedactFilter())
