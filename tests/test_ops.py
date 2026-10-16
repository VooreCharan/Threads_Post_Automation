"""Alerts, Threads-token renewal, background upkeep and the Telegram webhook."""
import time

import pytest
from fastapi.testclient import TestClient

from app import alerts, article, config, drafts, feeds, maintenance, net, state, telegram_bot as tg, threads
from app.main import app


@pytest.fixture
def sent(monkeypatch):
    """Capture owner alerts instead of sending them."""
    messages = []
    monkeypatch.setattr(alerts, "notify_owner", lambda text: messages.append(text) or True)
    return messages


# ---- alerts ------------------------------------------------------------------------------------------

def test_the_same_alert_is_not_repeated_within_its_cooldown(sent):
    assert alerts.alert("x", "problem") is True
    assert alerts.alert("x", "problem") is False
    assert alerts.alert("y", "other problem") is True
    assert len(sent) == 2 and sent[0].startswith("⚠️ problem")


def test_an_alert_is_remembered_across_a_restart_unless_the_database_is_the_problem(sent):
    alerts.alert("persisted", "a")
    alerts.reset()                                             # a restart forgets memory, not the database
    assert alerts.alert("persisted", "a") is False
    alerts.alert("db", "b", persist=False)
    alerts.reset()
    assert alerts.alert("db", "b", persist=False) is True      # database alerts cannot rely on the database


def test_notify_owner_does_nothing_when_telegram_is_not_configured():
    assert alerts.notify_owner("hello") is False


def test_notify_owner_sends_to_the_owner(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")
    got = []
    monkeypatch.setattr(tg, "send", lambda chat, text, reply_markup=None: got.append((chat, text)) or {"message_id": 1})
    assert alerts.notify_owner("hi") is True and got == [("42", "hi")]


# ---- Threads token ------------------------------------------------------------------------------------

def refresh_ok(fake_response, expires_in=5184000, token="T2"):
    return lambda url, **k: fake_response(json_data={"access_token": token, "expires_in": expires_in})


def test_a_new_token_from_settings_replaces_the_stored_one(monkeypatch, fake_response):
    monkeypatch.setattr(config, "THREADS_ACCESS_TOKEN", "T1")
    monkeypatch.setattr(net, "get", refresh_ok(fake_response))
    assert threads._token() == "T2"                            # unknown expiry: renewed on first use
    assert 59 < threads.days_left() < 61
    monkeypatch.setattr(config, "THREADS_ACCESS_TOKEN", "NEW")  # the owner pasted a fresh token
    monkeypatch.setattr(threads, "_last_failed_refresh", 0.0)
    assert threads._token() == "T2"                            # (renewal of NEW returned T2 in this fake)
    assert state.get("threads_token_seed") == "NEW"


def test_a_healthy_token_is_not_renewed_again(monkeypatch):
    monkeypatch.setattr(config, "THREADS_ACCESS_TOKEN", "T1")
    state.put("threads_token_seed", "T1")
    state.put("threads_token", "T1")
    state.put("threads_token_expires_at", str(time.time() + 50 * 86400))
    assert threads._token() == "T1"                            # the blocked network was never touched
    assert round(threads.days_left()) == 50


def test_a_token_close_to_expiry_is_renewed_before_it_lapses(monkeypatch, fake_response):
    monkeypatch.setattr(config, "THREADS_ACCESS_TOKEN", "T1")
    state.put("threads_token_seed", "T1")
    state.put("threads_token", "T1")
    state.put("threads_token_expires_at", str(time.time() + 3 * 86400))
    monkeypatch.setattr(net, "get", refresh_ok(fake_response, token="T3"))
    assert threads.ensure_fresh() > 59
    assert state.get("threads_token") == "T3"


def test_failing_to_renew_a_token_about_to_expire_alerts_the_owner(monkeypatch, sent):
    monkeypatch.setattr(config, "THREADS_ACCESS_TOKEN", "T1")
    state.put("threads_token_seed", "T1")
    state.put("threads_token", "T1")
    state.put("threads_token_expires_at", str(time.time() + 3 * 86400))
    monkeypatch.setattr(threads, "_last_failed_refresh", 0.0)

    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(net, "get", boom)
    threads.ensure_fresh()
    assert len(sent) == 1 and "expires in 3 day" in sent[0] and "THREADS_ACCESS_TOKEN" in sent[0]


def test_an_already_expired_token_says_so(monkeypatch, sent):
    monkeypatch.setattr(config, "THREADS_ACCESS_TOKEN", "T1")
    state.put("threads_token_seed", "T1")
    state.put("threads_token", "T1")
    state.put("threads_token_expires_at", str(time.time() - 3600))
    monkeypatch.setattr(threads, "_last_failed_refresh", 0.0)
    monkeypatch.setattr(net, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    threads.ensure_fresh()
    assert "has expired" in sent[0]


def test_a_failed_renewal_is_not_retried_on_every_call(monkeypatch):
    monkeypatch.setattr(config, "THREADS_ACCESS_TOKEN", "T1")
    monkeypatch.setattr(threads, "_last_failed_refresh", 0.0)
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise ConnectionError("down")
    monkeypatch.setattr(net, "get", boom)
    for _ in range(5):
        threads._token()
    assert len(calls) == 1


# ---- background upkeep -------------------------------------------------------------------------------

def make_items(n, category="india", prefix="Story"):
    return [{"url": f"https://x.in/{category}/{prefix}{i}", "title": f"{prefix} {i}", "summary": "s",
             "source": "S", "category": category, "published": 9e9 - i, "image": ""} for i in range(n)]


def test_prewarm_reads_the_top_stories_of_every_category_in_advance(monkeypatch):
    warmed = []
    monkeypatch.setattr(feeds, "fetch_category", lambda c: make_items(6, c))
    monkeypatch.setattr(article, "prefetch", lambda items, workers=4: warmed.extend(i["url"] for i in items))
    n = maintenance.prewarm()
    assert n == len(warmed) == 3 * 8                            # top 3 for each of the 8 categories


def test_prewarm_skips_stories_it_would_never_read(monkeypatch):
    items = make_items(2, prefix="Ok") + [dict(make_items(1, prefix="Bad")[0], title="Man murdered in Delhi")]
    monkeypatch.setattr(feeds, "fetch_category", lambda c: [dict(i) for i in items] if c == "india" else [])
    state.record_post(items[0]["url"], "p", "india", "Ok 0")
    warmed = []
    monkeypatch.setattr(article, "prefetch", lambda its, workers=4: warmed.extend(i["url"] for i in its))
    maintenance.prewarm()
    assert warmed == [items[1]["url"]]                          # posted + sensitive stories left out


def test_a_cycle_checks_the_database_and_renews_the_token(monkeypatch, sent):
    steps = []
    monkeypatch.setattr(state, "ping", lambda: steps.append("ping"))
    monkeypatch.setattr(threads, "ensure_fresh", lambda: steps.append("token") or 40.0)
    monkeypatch.setattr(maintenance, "prewarm", lambda: steps.append("prewarm") or 0)
    maintenance.run_cycle()
    assert steps == ["ping", "token", "prewarm"] and sent == []


def test_an_unreachable_database_alerts_the_owner(monkeypatch, sent):
    def down():
        raise RuntimeError("connection refused")
    monkeypatch.setattr(state, "ping", down)
    monkeypatch.setattr(threads, "ensure_fresh", lambda: 40.0)
    monkeypatch.setattr(maintenance, "prewarm", lambda: 0)
    maintenance.run_cycle()
    assert len(sent) == 1 and "database is unreachable" in sent[0]


def test_prewarm_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(config, "PREWARM", False)
    monkeypatch.setattr(state, "ping", lambda: None)
    monkeypatch.setattr(threads, "ensure_fresh", lambda: 40.0)
    monkeypatch.setattr(maintenance, "prewarm", lambda: pytest.fail("prewarm ran"))
    maintenance.run_cycle()


# ---- Telegram webhook --------------------------------------------------------------------------------

@pytest.fixture
def webhook_client(monkeypatch):
    messages = []
    monkeypatch.setattr(config, "TELEGRAM_MODE", "webhook")
    monkeypatch.setattr(config, "ALERT_ON_START", False)
    monkeypatch.setattr(tg, "send", lambda chat, text, reply_markup=None: messages.append(text) or {"message_id": 1})
    monkeypatch.setattr(tg, "_seen_updates", type(tg._seen_updates)(maxlen=500))
    with TestClient(app) as client:
        client.sent = messages
        yield client


def update(text="hi", update_id=1, user=42):
    return {"update_id": update_id, "message": {"chat": {"id": 1}, "from": {"id": user}, "text": text}}


def headers():
    return {"X-Telegram-Bot-Api-Secret-Token": tg.webhook_secret()}


def test_the_webhook_accepts_telegram_and_answers(webhook_client):
    r = webhook_client.post("/telegram/webhook", json=update("hi"), headers=headers())
    assert r.status_code == 200 and "Commands:" in webhook_client.sent[0]


def test_the_webhook_refuses_anyone_without_the_secret(webhook_client):
    assert webhook_client.post("/telegram/webhook", json=update()).status_code == 403
    assert webhook_client.post("/telegram/webhook", json=update(),
                               headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403
    assert webhook_client.post("/telegram/webhook", json=update(),
                               headers={"X-Telegram-Bot-Api-Secret-Token": "café".encode("utf-8")}).status_code == 403
    assert webhook_client.sent == []


def test_a_retried_update_is_handled_only_once(webhook_client):
    for _ in range(3):
        webhook_client.post("/telegram/webhook", json=update("hi", update_id=77), headers=headers())
    assert len(webhook_client.sent) == 1


def test_a_button_tap_arrives_through_the_webhook_too(webhook_client):
    tap = {"update_id": 5, "callback_query": {"id": "cb", "from": {"id": 999}, "data": "ok:abc",
                                              "message": {"chat": {"id": 1}, "message_id": 2}}}
    assert webhook_client.post("/telegram/webhook", json=tap, headers=headers()).status_code == 200


def test_the_webhook_is_closed_in_polling_mode(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_MODE", "polling")
    monkeypatch.setattr(config, "ALERT_ON_START", False)
    with TestClient(app) as client:
        assert client.post("/telegram/webhook", json=update(), headers=headers()).status_code == 403


def test_the_webhook_secret_is_stable_and_not_the_bot_token(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:secret-token")
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_SECRET", "")
    one = tg.webhook_secret()
    assert one == tg.webhook_secret() and "secret-token" not in one and len(one) == 48
    monkeypatch.setattr(config, "TELEGRAM_WEBHOOK_SECRET", "custom")
    assert tg.webhook_secret() == "custom"


def test_webhook_mode_registers_the_webhook_with_telegram(monkeypatch):
    calls = []
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(config, "TELEGRAM_MODE", "webhook")
    monkeypatch.setattr(config, "PUBLIC_URL", "https://bot.example.com")
    monkeypatch.setattr(tg, "_call", lambda method, http_timeout=40, **p: calls.append((method, p)) or {"result": True})
    tg.setup_webhook()
    method, params = calls[0]
    assert method == "setWebhook" and params["url"] == "https://bot.example.com/telegram/webhook"
    assert params["secret_token"] == tg.webhook_secret() and "callback_query" in params["allowed_updates"]


def test_webhook_mode_without_a_public_url_falls_back_to_polling(monkeypatch):
    started = []
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setattr(config, "TELEGRAM_MODE", "webhook")
    monkeypatch.setattr(config, "PUBLIC_URL", "")
    monkeypatch.setattr(tg, "poll_forever", lambda: started.append("poll"))
    assert tg.start() == "polling"
    time.sleep(0.2)
    assert started == ["poll"]


def test_health_endpoints_accept_get_and_head():
    with TestClient(app) as client:
        for path in ("/healthz", "/"):
            assert client.get(path).status_code == 200 and client.head(path).status_code == 200


def test_a_dry_run_never_touches_the_real_threads_token(monkeypatch):
    monkeypatch.setattr(config, "DRY_RUN", True)
    monkeypatch.setattr(state, "ping", lambda: None)
    monkeypatch.setattr(threads, "ensure_fresh", lambda: pytest.fail("the token was renewed during a dry run"))
    monkeypatch.setattr(maintenance, "prewarm", lambda: 0)
    maintenance.run_cycle()


# ---- the read-only Threads credential check (python -m app.cli --check-threads) --------------------------

def diag(monkeypatch, fake_response, replies):
    """Run threads.diagnose() with scripted API replies (in call order); return the printed lines as one text."""
    monkeypatch.setattr(config, "THREADS_ACCESS_TOKEN", "THAA" + "x" * 30)
    monkeypatch.setattr(config, "THREADS_USER_ID", "111")
    queue = list(replies)

    def fake_get(url, **k):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, data = item
        return fake_response(json_data=data, status=status)
    monkeypatch.setattr(net, "get", fake_get)
    return "\n".join(threads.diagnose())


def test_diagnose_healthy_account(monkeypatch, fake_response):
    out = diag(monkeypatch, fake_response, [(200, {"id": "111", "username": "newnews"}),
                                            (200, {"data": [{"quota_usage": 3, "config": {"quota_total": 250}}]})])
    assert "@newnews" in out and "matches the token" in out and "3 of 250" in out and "PROBLEM" not in out


def test_diagnose_spots_a_wrong_user_id(monkeypatch, fake_response):
    out = diag(monkeypatch, fake_response, [(200, {"id": "222", "username": "other"}), (200, {})])
    assert "PROBLEM" in out and "THREADS_USER_ID=222" in out


def test_diagnose_explains_an_api_access_block(monkeypatch, fake_response):
    blocked = {"error": {"message": "API access blocked.", "type": "OAuthException", "code": 200, "fbtrace_id": "A1B2"}}
    out = diag(monkeypatch, fake_response, [(400, blocked)])
    assert "API access blocked" in out and "fbtrace_id A1B2" in out
    assert "APP or on the Threads ACCOUNT" in out and "Do not try to get around a block" in out


def test_diagnose_explains_an_expired_token(monkeypatch, fake_response):
    expired = {"error": {"message": "Error validating access token", "type": "OAuthException", "code": 190}}
    assert "expired, revoked or invalid" in diag(monkeypatch, fake_response, [(401, expired)])


def test_diagnose_survives_network_trouble(monkeypatch, fake_response):
    assert "Could not reach the Threads API" in diag(monkeypatch, fake_response, [ConnectionError("down")])


def test_diagnose_needs_settings(monkeypatch):
    monkeypatch.setattr(config, "THREADS_ACCESS_TOKEN", "")
    assert threads.diagnose() == ["THREADS_ACCESS_TOKEN and THREADS_USER_ID must both be set."]


def test_diagnose_never_prints_the_token(monkeypatch, fake_response):
    token = "THAA" + "x" * 30
    out = diag(monkeypatch, fake_response, [(400, {"error": {"message": f"bad token {token}", "code": 200}})])
    assert token not in out and "bad token ***" in out
