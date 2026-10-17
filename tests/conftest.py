"""Shared test setup: every test gets a fresh temporary database, fixed settings, and NO network and
NO AI access (any attempt fails the test), so the suite is fast, free and safe to run anywhere."""
import logging
import threading

import pytest

from app import alerts, article, config, drafts, llm, net, state

logging.disable(logging.CRITICAL)
_REAL_NET = (net.get, net.post, net.delete)


def _blocked(*args, **kwargs):
    raise AssertionError("a test tried to use the network")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "TURSO_URL", "")
    monkeypatch.setattr(config, "TURSO_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")      # alerts and messages are not sent anywhere
    monkeypatch.setattr(config, "TELEGRAM_OWNER_ID", "42")
    monkeypatch.setattr(config, "THREADS_USER_ID", "acct-1")
    monkeypatch.setattr(config, "REQUIRE_APPROVAL", True)
    monkeypatch.setattr(config, "DAILY_POST_LIMIT", 10)
    monkeypatch.setattr(config, "MIN_SECONDS_BETWEEN_POSTS", 300)
    monkeypatch.setattr(config, "MAX_PENDING_DRAFTS", 3)
    monkeypatch.setattr(config, "DRAFT_TTL_MINUTES", 60)
    monkeypatch.setattr(config, "MIN_ARTICLE_CHARS", 250)
    monkeypatch.setattr(config, "SKIP_SENSITIVE", True)
    monkeypatch.setattr(config, "INDIA_ONLY", True)
    monkeypatch.setattr(config, "IMAGE_MODE", "auto")
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(net, "get", _blocked)
    monkeypatch.setattr(net, "post", _blocked)
    monkeypatch.setattr(net, "delete", _blocked)
    state.init()
    alerts.reset()
    llm.clear_caches()
    llm._cooldown_until.clear()
    llm._fail_streak = 0
    article._cache.clear()
    yield
    # let this test's own background threads (draft preparation, publishing) finish, so that none of
    # them is still running, and talking to the next test's fakes, when the next test starts
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.name.startswith("Thread-"):
            t.join(timeout=10)


@pytest.fixture
def real_net(monkeypatch):
    """Allow real HTTP (only for tests that talk to a local fake server)."""
    monkeypatch.setattr(net, "get", _REAL_NET[0])
    monkeypatch.setattr(net, "post", _REAL_NET[1])
    monkeypatch.setattr(net, "delete", _REAL_NET[2])


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, body: str | bytes = "", status: int = 200, content_type: str = "text/html", json_data=None):
        self.content = body.encode() if isinstance(body, str) else body
        self.status_code = status
        self.headers = {"content-type": content_type}
        self._json = json_data
        self.text = self.content.decode("utf-8", "replace")

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def fake_response():
    return FakeResponse
