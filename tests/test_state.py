import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app import config, state


# ---- local SQLite backend --------------------------------------------------------------------------

def test_posted_and_skipped_are_remembered_across_a_restart():
    state.record_post("https://a/1", "p1", "sports", "Kohli hits century")
    state.mark_skipped("https://a/2", "not about India")
    state.init()                                  # a restart: memory is rebuilt from the database
    assert state.was_posted("https://a/1") and state.was_skipped("https://a/2")
    assert not state.was_posted("https://a/zzz")


def test_titles_enable_duplicate_headline_detection():
    state.record_post("https://a/1", "p1", "sports", "Kohli hits century as India beat Australia in Perth")
    assert state.near_identical_title("Kohli hits century as India beat Australia in Perth!")
    assert state.near_identical_title("India beat Pakistan in Asia Cup opener at Dubai") is None
    assert state.recent_titles(5) == ["Kohli hits century as India beat Australia in Perth"]


def test_posts_in_window_and_last_post_time():
    assert state.last_post_time() is None
    state.record_post("https://a/1", "p1", "india", "t")
    n, oldest = state.posts_in_window(86400)
    assert n == 1 and abs(oldest - time.time()) < 5
    assert state.posts_in_window(0)[0] == 0 and state.posts_in_window(0)[1] is None   # an empty window
    assert abs(state.last_post_time() - time.time()) < 5


def test_settings_roundtrip_and_cache():
    assert state.get("missing") is None
    state.put("k", "v1")
    assert state.get("k") == "v1"
    state.put("k", "v2")
    assert state.get("k") == "v2"
    state.init()
    assert state.get("k") == "v2"


def test_an_old_database_without_titles_is_upgraded_in_place(tmp_path, monkeypatch):
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.executescript("""CREATE TABLE posted (url TEXT PRIMARY KEY, post_id TEXT, category TEXT, posted_at REAL NOT NULL);
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE skipped (url TEXT PRIMARY KEY, reason TEXT, skipped_at REAL NOT NULL);
        INSERT INTO posted VALUES ('https://old/1','p','india',1.0);""")
    con.commit()
    con.close()
    monkeypatch.setattr(config, "DB_PATH", str(old))
    state.init()
    state.record_post("https://new/1", "p2", "sports", "A title")
    columns = [r[1] for r in sqlite3.connect(old).execute("PRAGMA table_info(posted)")]
    assert "title" in columns and state.was_posted("https://old/1") and state.recent_titles(3) == ["A title"]


def test_ping_works_on_a_healthy_database():
    state.ping()


# ---- Turso over HTTP (a fake server speaking its /v2/pipeline format) ----------------------------------

class FakeTurso(BaseHTTPRequestHandler):
    db = None
    calls = 0

    def log_message(self, *a):
        pass

    @staticmethod
    def cell(v):
        if v is None:
            return {"type": "null"}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": v}
        return {"type": "text", "value": v}

    @staticmethod
    def arg(a):
        t = a["type"]
        return None if t == "null" else int(a["value"]) if t == "integer" else float(a["value"]) if t == "float" else a["value"]

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer good-token" or self.path != "/v2/pipeline":
            self.send_response(401)
            self.end_headers()
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        results = []
        for req in body["requests"]:
            if req["type"] != "execute":
                results.append({"type": "ok", "response": {"type": "close"}})
                continue
            type(self).calls += 1
            stmt = req["stmt"]
            try:
                cur = self.db.execute(stmt["sql"], [self.arg(a) for a in stmt.get("args", [])])
                self.db.commit()
                rows = [[self.cell(c) for c in r] for r in cur.fetchall()]
                results.append({"type": "ok", "response": {"type": "execute", "result": {"cols": [], "rows": rows}}})
            except Exception as e:
                results.append({"type": "error", "error": {"message": str(e)}})
        out = json.dumps({"results": results}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture
def turso(real_net, monkeypatch):
    FakeTurso.db = sqlite3.connect(":memory:", check_same_thread=False)
    FakeTurso.calls = 0
    server = HTTPServer(("127.0.0.1", 0), FakeTurso)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(config, "TURSO_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(config, "TURSO_TOKEN", "good-token")
    state.init()
    yield FakeTurso
    server.shutdown()


def test_turso_backend_roundtrip_survives_a_restart(turso):
    assert state.use_turso()
    state.record_post("https://a/1", "p1", "sports", "Title one")
    state.mark_skipped("https://a/2", "x")
    state.put("posting", "1")
    state.init()                                  # a new Render instance: everything comes back from "Turso"
    assert state.was_posted("https://a/1") and state.was_skipped("https://a/2")
    assert state.get("posting") == "1" and state.recent_titles(3) == ["Title one"]
    assert state.recent_posts(1)[0][0] == "https://a/1"


def test_turso_reads_are_cached(turso):
    state.put("k", "v")
    before = turso.calls
    for _ in range(50):
        state.get("k")
        state.was_posted("https://a/1")
    assert turso.calls == before


def test_a_database_that_rejects_us_never_crashes_a_post(turso, monkeypatch):
    monkeypatch.setattr(config, "TURSO_TOKEN", "wrong-token")
    monkeypatch.setattr(time, "sleep", lambda s: None)       # skip the retry back-off
    state.record_post("https://a/9", "p", "sports", "Title")  # must not raise
    assert state.was_posted("https://a/9")                    # in-memory state is still right


def test_migrating_a_local_database_into_turso(turso, tmp_path):
    local = tmp_path / "old.db"
    con = sqlite3.connect(local)
    con.executescript("""CREATE TABLE posted (url TEXT PRIMARY KEY, post_id TEXT, category TEXT, posted_at REAL NOT NULL);
        CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE skipped (url TEXT PRIMARY KEY, reason TEXT, skipped_at REAL NOT NULL);
        INSERT INTO posted VALUES ('https://old/1','p','india',1.0);
        INSERT INTO skipped VALUES ('https://old/2','foreign',2.0);
        INSERT INTO kv VALUES ('threads_token','THAAxyz');""")
    con.commit()
    con.close()
    assert state.import_from_sqlite(str(local)) == {"posted": 1, "skipped": 1, "kv": 1}
    state.init()
    assert state.was_posted("https://old/1") and state.was_skipped("https://old/2")
    assert state.get("threads_token") == "THAAxyz"
