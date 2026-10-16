"""Secrets must never reach a log line, an alert or a Telegram message."""
import logging

import pytest
import requests

from app import config, redact, telegram_bot as tg

TG_TOKEN = "8925174062:AAFekMQEHE3irziotOfhRdZKrY7HxQEx2TI"
THREADS_TOKEN = "THAAHPd4mNlfxQ3mLZbk5v7Zz9aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789abcdef"
SECRETS = [TG_TOKEN, THREADS_TOKEN]


@pytest.fixture(autouse=True)
def secrets(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TG_TOKEN)
    monkeypatch.setattr(config, "THREADS_ACCESS_TOKEN", THREADS_TOKEN)
    monkeypatch.setitem(config.OPENAI_COMPAT["groq"], "key", "gsk_exampleGroqKey1234567890abcdef")
    monkeypatch.setattr(config, "PIXABAY_API_KEY", "12345678-pixabaykeyabcdef")


def assert_clean(text: str):
    for secret in SECRETS + ["gsk_exampleGroqKey1234567890abcdef", "12345678-pixabaykeyabcdef"]:
        assert secret not in text, f"leaked: {secret[:12]}... in {text!r}"


def test_secret_values_from_the_settings_are_removed():
    text = f"calling with {TG_TOKEN} and {THREADS_TOKEN} and gsk_exampleGroqKey1234567890abcdef and 12345678-pixabaykeyabcdef"
    assert_clean(redact.redact(text))


@pytest.mark.parametrize("message", [
    "400 Client Error: Bad Request for url: https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token&access_token=THAAxxxxOTHERTOKENxxxxxxxxxxxxxxxxxxxx",
    "409 Client Error: Conflict for url: https://api.telegram.org/bot111222333:ZZZ_otherBotTokenValue-123/getUpdates",
    "403 for url: https://pixabay.com/api/?key=SOMEOTHERKEY1234567890&q=solar&image_type=photo",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
    "token THAAunknownTokenThatIsNotInSettings0123456789abcdefghij leaked",
])
def test_secrets_in_unknown_places_are_removed_by_pattern(message):
    out = redact.redact(message)
    for fragment in ("OTHERTOKEN", "otherBotTokenValue", "SOMEOTHERKEY", "abcdefghijklmnopqrstuvwxyz0123456789",
                     "unknownTokenThatIsNotInSettings"):
        assert fragment not in out
    assert "***" in out


def test_normal_text_is_left_alone():
    text = "Kohli hit a century as India won the match by six wickets at https://www.thehindu.com/sport/a1.ece"
    assert redact.redact(text) == text


def logged(logger_name, level, message, *args, exc=None):
    """Run a record through a handler that has the redact filter; return the final formatted output."""
    out = []

    class Capture(logging.Handler):
        def emit(self, record):
            out.append(self.format(record))

    handler = Capture()
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(redact.RedactFilter())
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logging.disable(logging.NOTSET)
    try:
        if exc is not None:
            logger.log(level, message, *args, exc_info=exc)
        else:
            logger.log(level, message, *args)
    finally:
        logging.disable(logging.CRITICAL)
        logger.removeHandler(handler)
    return out[0]


def test_log_messages_and_arguments_are_scrubbed():
    out = logged("test.redact", logging.WARNING, "Telegram polling error: %s", f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates")
    assert_clean(out)
    assert "***" in out


def test_a_failed_request_error_never_exposes_the_token_in_a_traceback():
    try:
        raise requests.HTTPError(f"400 Client Error for url: https://graph.threads.net/x?access_token={THREADS_TOKEN}")
    except requests.HTTPError as e:
        out = logged("test.redact", logging.ERROR, "refresh failed", exc=e.__traceback__ and (type(e), e, e.__traceback__))
    assert_clean(out)
    assert "HTTPError" in out and "***" in out


def test_messages_sent_to_telegram_are_scrubbed(monkeypatch):
    sent = []
    monkeypatch.setattr(tg, "_call", lambda method, http_timeout=40, **p: sent.append(p) or {"result": {"message_id": 1}})
    tg.send(1, f"Something went wrong: 401 for url https://graph.threads.net/me?access_token={THREADS_TOKEN}")
    assert_clean(sent[0]["text"])
    assert "***" in sent[0]["text"]


def test_install_adds_the_filter_once_to_the_root_handlers():
    root = logging.getLogger()
    handler = logging.StreamHandler()
    root.addHandler(handler)
    try:
        redact.install()
        redact.install()
        assert sum(isinstance(f, redact.RedactFilter) for f in handler.filters) == 1
    finally:
        root.removeHandler(handler)
