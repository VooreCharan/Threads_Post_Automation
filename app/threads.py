"""Publish text posts through the official Threads API (graph.threads.net)."""
import logging
import time

import requests

from . import config, state

logger = logging.getLogger(__name__)

API = "https://graph.threads.net"
REFRESH_WHEN_LEFT = 10 * 24 * 3600  # long-lived tokens last ~60 days; refresh with 10 days left


def _token() -> str:
    """Current access token, refreshed (and persisted) when close to expiry."""
    if not config.THREADS_ACCESS_TOKEN:
        raise RuntimeError("THREADS_ACCESS_TOKEN is not set")
    if state.get("threads_token_seed") != config.THREADS_ACCESS_TOKEN:
        # First run, or a new token was put in .env: it replaces any older stored/refreshed token
        state.put("threads_token", config.THREADS_ACCESS_TOKEN)
        state.put("threads_token_seed", config.THREADS_ACCESS_TOKEN)
        state.put("threads_token_expires_at", "0")
    token = state.get("threads_token")
    expires_at = float(state.get("threads_token_expires_at") or 0)
    if expires_at and expires_at - time.time() > REFRESH_WHEN_LEFT:
        return token
    try:
        resp = requests.get(f"{API}/refresh_access_token",
                            params={"grant_type": "th_refresh_token", "access_token": token},
                            timeout=20)
        resp.raise_for_status()
        data = resp.json()
        token = data["access_token"]
        state.put("threads_token", token)
        state.put("threads_token_expires_at", str(time.time() + int(data["expires_in"])))
        logger.info("Threads token refreshed")
    except Exception as e:
        # Refresh only works for tokens older than 24h; keep using the current one otherwise.
        logger.warning("Threads token refresh skipped: %s", e)
    return token


def _check(resp: requests.Response) -> dict:
    if resp.status_code >= 400:
        raise RuntimeError(f"Threads API {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def _wait_ready(container: str, token: str, timeout: int = 90) -> None:
    """Image containers are processed asynchronously: wait until Threads has fetched the image."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        info = _check(requests.get(f"{API}/v1.0/{container}",
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
    container = _check(requests.post(f"{API}/v1.0/{config.THREADS_USER_ID}/threads",
                                     data=params, timeout=30))["id"]
    if image_url:
        _wait_ready(container, token)
    else:
        time.sleep(5)  # Meta recommends a short wait between create and publish
    published = _check(requests.post(f"{API}/v1.0/{config.THREADS_USER_ID}/threads_publish",
                                     data={"creation_id": container, "access_token": token},
                                     timeout=30))
    return published["id"]


def permalink(post_id: str) -> str | None:
    """Public URL of a published post, or None if it cannot be looked up."""
    try:
        info = _check(requests.get(f"{API}/v1.0/{post_id}",
                                   params={"fields": "permalink", "access_token": _token()}, timeout=20))
        return info.get("permalink")
    except Exception as e:
        logger.warning("Could not fetch the post link: %s", str(e)[:150])
        return None
