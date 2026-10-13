"""Tiny SQLite store: which articles were posted, and the current (refreshed) Threads token."""
import sqlite3
import time
from contextlib import contextmanager

from . import config


@contextmanager
def _conn():
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    with _conn() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS posted (
                url TEXT PRIMARY KEY, post_id TEXT, category TEXT, posted_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS skipped (
                url TEXT PRIMARY KEY, reason TEXT, skipped_at REAL NOT NULL);
        """)


def was_posted(url: str) -> bool:
    with _conn() as con:
        return con.execute("SELECT 1 FROM posted WHERE url=?", (url,)).fetchone() is not None


def record_post(url: str, post_id: str, category: str) -> None:
    with _conn() as con:
        con.execute("INSERT OR IGNORE INTO posted VALUES (?, ?, ?, ?)",
                    (url, post_id, category, time.time()))


def was_skipped(url: str) -> bool:
    with _conn() as con:
        return con.execute("SELECT 1 FROM skipped WHERE url=?", (url,)).fetchone() is not None


def mark_skipped(url: str, reason: str) -> None:
    """Remember a story we decided not to post (e.g. not about India) so it is not re-checked."""
    with _conn() as con:
        con.execute("INSERT OR IGNORE INTO skipped VALUES (?, ?, ?)", (url, reason, time.time()))


def recent_posts(limit: int = 10) -> list[tuple]:
    with _conn() as con:
        return con.execute("SELECT url, post_id, category, posted_at FROM posted "
                           "ORDER BY posted_at DESC LIMIT ?", (limit,)).fetchall()


def posts_in_window(seconds: float) -> tuple[int, float | None]:
    """(number of posts in the last `seconds`, timestamp of the oldest of them)."""
    with _conn() as con:
        n, oldest = con.execute("SELECT COUNT(*), MIN(posted_at) FROM posted WHERE posted_at > ?",
                                (time.time() - seconds,)).fetchone()
    return n, oldest


def get(key: str) -> str | None:
    with _conn() as con:
        row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def put(key: str, value: str) -> None:
    with _conn() as con:
        con.execute("INSERT INTO kv VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))
