"""Small persistent store: posted/skipped stories, settings, and the refreshed Threads token.

Two backends with the same SQL:
- local SQLite file (DB_PATH): the default, fine on a PC or a VM with a disk;
- Turso over its HTTP API (TURSO_URL + TURSO_TOKEN): for hosts with no persistent disk (Render free).

"Which URLs were posted/skipped" and the 24h post times are kept in memory (loaded at start-up) so
the posting loop never makes one database call per story; writes go straight through to the database.
Settings (kv) are cached for a short time so the 10 s heartbeat does not hit a remote database.
"""
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager

import requests

from . import config

logger = logging.getLogger(__name__)

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS posted (url TEXT PRIMARY KEY, post_id TEXT, category TEXT, posted_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS skipped (url TEXT PRIMARY KEY, reason TEXT, skipped_at REAL NOT NULL)",
]

KV_TTL = 30.0  # seconds a cached setting is trusted before re-reading from the database
KEEP_POST_TIMES = 8 * 24 * 3600

_lock = threading.RLock()
_posted: set[str] = set()
_skipped: set[str] = set()
_post_times: list[float] = []
_kv: dict[str, tuple[float, str | None]] = {}  # key -> (time cached, value)


# ---- backends ---------------------------------------------------------------------------------

def use_turso() -> bool:
    return bool(config.TURSO_URL and config.TURSO_TOKEN)


def _encode(value) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}  # integers travel as strings
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _decode(cell: dict):
    kind = cell.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(cell["value"])
    if kind == "float":
        return float(cell["value"])
    return cell.get("value")  # text (blobs are not used)


def _turso(sql: str, args: tuple = ()) -> list[list]:
    body = {"requests": [{"type": "execute", "stmt": {"sql": sql, "args": [_encode(a) for a in args]}},
                         {"type": "close"}]}
    resp = requests.post(f"{config.TURSO_URL}/v2/pipeline", json=body, timeout=20,
                         headers={"Authorization": f"Bearer {config.TURSO_TOKEN}"})
    if resp.status_code != 200:
        raise RuntimeError(f"Turso HTTP {resp.status_code}: {resp.text[:200]}")
    first = resp.json()["results"][0]
    if first.get("type") != "ok":
        raise RuntimeError(f"Turso error: {first.get('error')}")
    return [[_decode(c) for c in row] for row in first["response"]["result"]["rows"]]


@contextmanager
def _sqlite():
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _run(sql: str, args: tuple = ()) -> list:
    """Run one statement on the active backend; returns the rows (empty for writes)."""
    if use_turso():
        return _turso(sql, args)
    with _sqlite() as con:
        return con.execute(sql, args).fetchall()


def _write(sql: str, args: tuple = ()) -> None:
    """A write that retries, and never raises: callers (the posting loop) must not crash after a
    post has already gone out. In-memory state stays correct even if the database is unreachable."""
    for attempt in range(1, 4):
        try:
            _run(sql, args)
            return
        except Exception as e:
            logger.warning("Database write failed (attempt %d/3): %s", attempt, str(e)[:200])
            time.sleep(attempt)
    logger.error("Database write gave up; in-memory state is kept for this run: %s", sql[:60])


# ---- setup ------------------------------------------------------------------------------------

def init() -> None:
    """Create tables and load the posted/skipped sets and recent post times into memory."""
    for statement in SCHEMA:
        _run(statement)
    with _lock:
        _posted.clear()
        _skipped.clear()
        _post_times.clear()
        _posted.update(r[0] for r in _run("SELECT url FROM posted"))
        _skipped.update(r[0] for r in _run("SELECT url FROM skipped"))
        _post_times.extend(float(r[0]) for r in _run(
            "SELECT posted_at FROM posted WHERE posted_at > ?", (time.time() - KEEP_POST_TIMES,)))
        _kv.clear()
    logger.info("State ready (%s): %d posted, %d skipped",
                "Turso" if use_turso() else f"SQLite {config.DB_PATH}", len(_posted), len(_skipped))


# ---- posted / skipped stories -----------------------------------------------------------------

def was_posted(url: str) -> bool:
    return url in _posted


def record_post(url: str, post_id: str, category: str) -> None:
    now = time.time()
    with _lock:
        _posted.add(url)
        _post_times.append(now)
    _write("INSERT OR IGNORE INTO posted VALUES (?, ?, ?, ?)", (url, post_id, category, now))


def was_skipped(url: str) -> bool:
    return url in _skipped


def mark_skipped(url: str, reason: str) -> None:
    """Remember a story we decided not to post (e.g. not about India) so it is not re-checked."""
    with _lock:
        _skipped.add(url)
    _write("INSERT OR IGNORE INTO skipped VALUES (?, ?, ?)", (url, reason, time.time()))


def recent_posts(limit: int = 10) -> list[tuple]:
    try:
        return [tuple(r) for r in _run("SELECT url, post_id, category, posted_at FROM posted "
                                       "ORDER BY posted_at DESC LIMIT ?", (limit,))]
    except Exception as e:
        logger.warning("Could not read recent posts: %s", str(e)[:150])
        return []


def last_post_time() -> float | None:
    with _lock:
        return max(_post_times) if _post_times else None


def posts_in_window(seconds: float) -> tuple[int, float | None]:
    """(number of posts in the last `seconds`, timestamp of the oldest of them)."""
    cutoff = time.time() - seconds
    with _lock:
        recent = [t for t in _post_times if t > cutoff]
    return len(recent), (min(recent) if recent else None)


# ---- settings ---------------------------------------------------------------------------------

def get(key: str) -> str | None:
    now = time.time()
    with _lock:
        cached = _kv.get(key)
        if cached and now - cached[0] < KV_TTL:
            return cached[1]
    try:
        rows = _run("SELECT value FROM kv WHERE key=?", (key,))
        value = rows[0][0] if rows else None
    except Exception as e:
        logger.warning("Could not read setting %s: %s", key, str(e)[:150])
        return cached[1] if cached else None  # fall back to the last known value
    with _lock:
        _kv[key] = (now, value)
    return value


def put(key: str, value: str) -> None:
    with _lock:
        _kv[key] = (time.time(), value)
    _write("INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ---- one-off migration ------------------------------------------------------------------------

def import_from_sqlite(path: str) -> dict:
    """Copy posted/skipped/kv rows from a local SQLite file into the active (Turso) database."""
    counts = {"posted": 0, "skipped": 0, "kv": 0}
    src = sqlite3.connect(path)
    try:
        for url, post_id, category, at in src.execute("SELECT url, post_id, category, posted_at FROM posted"):
            _run("INSERT OR IGNORE INTO posted VALUES (?, ?, ?, ?)", (url, post_id, category, at))
            counts["posted"] += 1
        for url, reason, at in src.execute("SELECT url, reason, skipped_at FROM skipped"):
            _run("INSERT OR IGNORE INTO skipped VALUES (?, ?, ?)", (url, reason, at))
            counts["skipped"] += 1
        for key, value in src.execute("SELECT key, value FROM kv"):
            _run("INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            counts["kv"] += 1
    finally:
        src.close()
    return counts
