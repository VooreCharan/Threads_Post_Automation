"""Publish text posts through the official Threads API (graph.threads.net)."""
import logging
import time

import requests

from . import alerts, config, net, redact, state

logger = logging.getLogger(__name__)

API = "https://graph.threads.net"
REFRESH_WHEN_LEFT = 10 * 24 * 3600  # long-lived tokens last ~60 days; refresh with 10 days left
_last_failed_refresh = 0.0


def _token() -> str:
    """Current access token, refreshed (and persisted) when close to expiry."""
    if not config.THREADS_ACCESS_TOKEN:
        raise RuntimeError("THREADS_ACCESS_TOKEN is not set")
    if state.get("threads_token_seed") != config.THREADS_ACCESS_TOKEN:
        # First run, or a new token was put in .env: it replaces any older stored/refreshed token
        state.put("threads_token", config.THREADS_ACCESS_TOKEN)
        state.put("threads_token_seed", config.THREADS_ACCESS_TOKEN)
        state.put("threads_token_expires_at", "0")
    global _last_failed_refresh
    token = state.get("threads_token")
    expires_at = float(state.get("threads_token_expires_at") or 0)
    now = time.time()
    if expires_at and expires_at - now > REFRESH_WHEN_LEFT:
        return token
    if now - _last_failed_refresh < 3600:  # a refresh just failed: do not hammer the API on every call
        return token
    try:
        resp = net.get(f"{API}/refresh_access_token",
                       params={"grant_type": "th_refresh_token", "access_token": token}, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        token = data["access_token"]
        state.put("threads_token", token)
        state.put("threads_token_expires_at", str(now + int(data["expires_in"])))
        _last_failed_refresh = 0.0
        logger.info("Threads token refreshed")
    except Exception as e:
        # Refresh only works for tokens older than 24h; keep using the current one otherwise.
        _last_failed_refresh = now
        logger.warning("Threads token refresh skipped: %s", e)
        left = expires_at - now if expires_at else None
        if left is not None and left < 7 * 86400:  # the expiry date is known and close
            days = max(left, 0) / 86400
            alerts.alert("token_expiring",
                         f"The Threads access token {'has expired' if left <= 0 else f'expires in {days:.0f} day(s)'} "
                         f"and could not be renewed automatically ({str(e)[:100]}). Generate a new long-lived token "
                         "and update THREADS_ACCESS_TOKEN (Render and .env).", every_hours=12)
    return token


def days_left() -> float | None:
    """Days until the stored token expires, or None if the expiry is not known yet."""
    expires_at = float(state.get("threads_token_expires_at") or 0)
    return (expires_at - time.time()) / 86400 if expires_at else None


def ensure_fresh() -> float | None:
    """Renew the token if it is close to expiry (so it never lapses while idle). Returns days left."""
    try:
        _token()
    except Exception as e:
        logger.warning("Token check skipped: %s", e)
    return days_left()


def _check(resp: requests.Response) -> dict:
    if resp.status_code >= 400:
        raise RuntimeError(f"Threads API {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _wait_ready(container: str, token: str, timeout: int = 90) -> None:
    """Image containers are processed asynchronously: wait until Threads has fetched the image."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        info = _check(net.get(f"{API}/v1.0/{container}",
                                   params={"fields": "status,error_message", "access_token": token},
                                   timeout=20))
        status = info.get("status")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Threads could not process the image ({status}): {info.get('error_message')}")
    raise RuntimeError("Threads did not finish processing the image in time")


def publish(text: str, link: str | None = None, image_url: str | None = None) -> str:
    """Publish a post and return its id.

    With image_url it is an image post (the image must be a public JPEG/PNG URL); otherwise a text
    post, optionally with a link preview."""
    token = _token()
    if image_url:
        params = {"media_type": "IMAGE", "image_url": image_url, "text": text, "access_token": token}
    else:
        params = {"media_type": "TEXT", "text": text, "access_token": token}
        if link:
            params["link_attachment"] = link
    container = _check(net.post(f"{API}/v1.0/{config.THREADS_USER_ID}/threads",
                                     data=params, timeout=30))["id"]
    if image_url:
        _wait_ready(container, token)
    else:
        time.sleep(5)  # Meta recommends a short wait between create and publish
    published = _check(net.post(f"{API}/v1.0/{config.THREADS_USER_ID}/threads_publish",
                                     data={"creation_id": container, "access_token": token},
                                     timeout=30))
    return published["id"]


def permalink(post_id: str) -> str | None:
    """Public URL of a published post, or None if it cannot be looked up."""
    try:
        info = _check(net.get(f"{API}/v1.0/{post_id}",
                                   params={"fields": "permalink", "access_token": _token()}, timeout=20))
        return info.get("permalink")
    except Exception as e:
        logger.warning("Could not fetch the post link: %s", str(e)[:150])
        return None


def diagnose() -> list[str]:
    """Read-only health check of the Threads credentials, in plain English. It posts nothing, renews
    nothing and changes nothing, so it is safe to run at any time (python -m app.cli --check-threads)."""
    seeded = state.get("threads_token_seed") == config.THREADS_ACCESS_TOKEN
    token = (state.get("threads_token") if seeded else None) or config.THREADS_ACCESS_TOKEN
    uid = config.THREADS_USER_ID
    if not token or not uid:
        return ["THREADS_ACCESS_TOKEN and THREADS_USER_ID must both be set."]
    try:
        resp = net.get(f"{API}/v1.0/me", params={"fields": "id,username", "access_token": token}, timeout=20)
        data = resp.json()
    except Exception as e:
        return [f"Could not reach the Threads API: {redact.redact(e)[:150]}", "Check the internet connection and try again."]

    if resp.status_code == 200:
        lines = [f"OK: the token belongs to @{data.get('username')} (id {data.get('id')})."]
        if str(data.get("id")) != str(uid):
            lines.append(f"PROBLEM: THREADS_USER_ID is {uid}, but the token belongs to account {data.get('id')}. "
                         f"Set THREADS_USER_ID={data.get('id')}.")
        else:
            lines.append("OK: THREADS_USER_ID matches the token.")
        try:
            q = net.get(f"{API}/v1.0/{uid}/threads_publishing_limit",
                        params={"fields": "quota_usage,config", "access_token": token}, timeout=20).json()
            usage = (q.get("data") or [{}])[0]
            lines.append(f"OK: publishing quota used {usage.get('quota_usage')} of "
                         f"{(usage.get('config') or {}).get('quota_total', 250)} in the last 24 hours.")
        except Exception:
            pass
        return lines

    error = data.get("error", {}) if isinstance(data, dict) else {}
    code, message = error.get("code"), redact.redact(error.get("message", ""))
    lines = [f"Meta refused the request (HTTP {resp.status_code}, error {code}): {message}",
             f"Reference for Meta support: fbtrace_id {error.get('fbtrace_id', 'n/a')}"]
    if code == 190:
        lines.append("MEANING: the token is expired, revoked or invalid. Generate a new long-lived token and set "
                     "THREADS_ACCESS_TOKEN.")
    elif code == 200 and "block" in message.lower():
        lines += ["MEANING: Meta is blocking API access for this token. Because even reading your own profile is "
                  "refused, the block is on the Meta APP or on the Threads ACCOUNT, not on posting and not on this bot.",
                  "CHECK 1: open the Threads account in the Threads app: is it working normally, with no notice?",
                  "CHECK 2: developers.facebook.com -> your app -> look for restriction or alert banners; confirm this "
                  "account is a Threads Tester that has accepted the invite.",
                  "CHECK 3: generate a brand-new token in the app's User Token Generator and run this check again: "
                  "if only the old token is blocked, the new one works.",
                  "If the app or account is restricted, use the review or appeal option Meta shows. Do not try to get "
                  "around a block with another app or account."]
    elif code in (10, 200):
        lines.append("MEANING: the app lacks a permission. Re-create the token with threads_basic and "
                     "threads_content_publish, and make sure the account is a Threads Tester.")
    return lines
