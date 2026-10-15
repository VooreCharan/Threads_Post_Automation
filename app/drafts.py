"""Drafts waiting for the owner's approval.

Kept in the app's state store (not just memory) so a restart or redeploy does not lose them, and
expired after DRAFT_TTL_MINUTES so a stale story is never published by an old button.
"""
import json
import secrets
import threading
import time

from . import config, state

_KEY = "drafts"
_lock = threading.RLock()


def _load() -> dict:
    try:
        data = json.loads(state.get(_KEY) or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _purge(drafts: dict) -> dict:
    cutoff = time.time() - config.DRAFT_TTL_MINUTES * 60
    return {k: v for k, v in drafts.items() if v.get("created_at", 0) > cutoff}


def _save(drafts: dict) -> None:
    state.put(_KEY, json.dumps(drafts, ensure_ascii=False))


def add(draft: dict) -> str:
    """Store a draft and return its short id (used in the Telegram button data)."""
    with _lock:
        drafts = _purge(_load())
        draft_id = secrets.token_hex(4)
        drafts[draft_id] = dict(draft, id=draft_id, created_at=time.time())
        _save(drafts)
        return draft_id


def get(draft_id: str) -> dict | None:
    with _lock:
        return _purge(_load()).get(draft_id)


def update(draft_id: str, **fields) -> None:
    with _lock:
        drafts = _purge(_load())
        if draft_id in drafts:
            drafts[draft_id].update(fields)
            _save(drafts)


def pop(draft_id: str) -> dict | None:
    """Remove and return a draft (None if it is gone or expired). Taking it out is what stops a
    double tap from publishing twice."""
    with _lock:
        drafts = _purge(_load())
        draft = drafts.pop(draft_id, None)
        _save(drafts)
        return draft


def clear() -> int:
    with _lock:
        n = len(_purge(_load()))
        _save({})
        return n


def all_drafts() -> list[dict]:
    with _lock:
        return sorted(_purge(_load()).values(), key=lambda d: d["created_at"])


def count() -> int:
    return len(all_drafts())


def pending_urls() -> set[str]:
    """Stories already waiting for approval, so the next command proposes a different one."""
    return {d["url"] for d in all_drafts()}
