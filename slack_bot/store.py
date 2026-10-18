"""SQLite state: Slack thread -> Claude session id, plus a request log.

Same shape as repair-bot's ``slack_pending_bug_fixes`` table idea: a tiny
table so a restart keeps thread continuity. Thread-local connections, a busy
timeout, nothing fancy.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  thread_key  TEXT PRIMARY KEY,           -- "<channel>:<thread_ts>"
  session_id  TEXT NOT NULL,
  skill       TEXT,
  updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_key  TEXT NOT NULL,
  user_id     TEXT,
  skill       TEXT,
  prompt      TEXT,
  status      TEXT,                       -- running | ok | error | cancelled | timeout
  session_id  TEXT,
  cost_usd    REAL,
  duration_s  REAL,
  started_at  REAL NOT NULL,
  finished_at REAL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    # -- threads --------------------------------------------------------
    def get_session(self, thread_key: str) -> str | None:
        row = self._conn().execute("SELECT session_id FROM threads WHERE thread_key=?", (thread_key,)).fetchone()
        return row[0] if row else None

    def set_session(self, thread_key: str, session_id: str, skill: str | None) -> None:
        self._conn().execute(
            "INSERT INTO threads(thread_key, session_id, skill, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(thread_key) DO UPDATE SET session_id=excluded.session_id, skill=excluded.skill, updated_at=excluded.updated_at",
            (thread_key, session_id, skill, time.time()),
        )

    def forget_session(self, thread_key: str) -> None:
        self._conn().execute("DELETE FROM threads WHERE thread_key=?", (thread_key,))

    # -- requests -------------------------------------------------------
    def start_request(self, thread_key: str, user_id: str, skill: str | None, prompt: str) -> int:
        cur = self._conn().execute(
            "INSERT INTO requests(thread_key, user_id, skill, prompt, status, started_at) VALUES(?,?,?,?,'running',?)",
            (thread_key, user_id, skill, prompt[:4000], time.time()),
        )
        return int(cur.lastrowid)

    def finish_request(self, req_id: int, status: str, session_id: str, cost_usd: float | None, duration_s: float) -> None:
        self._conn().execute(
            "UPDATE requests SET status=?, session_id=?, cost_usd=?, duration_s=?, finished_at=? WHERE id=?",
            (status, session_id, cost_usd, duration_s, time.time(), req_id),
        )

    def mark_orphans(self) -> int:
        """On startup: anything still 'running' died with the previous process."""
        cur = self._conn().execute("UPDATE requests SET status='orphaned', finished_at=? WHERE status='running'", (time.time(),))
        return cur.rowcount

    def recent(self, limit: int = 20) -> list[tuple]:
        return self._conn().execute(
            "SELECT id, thread_key, user_id, skill, status, cost_usd, duration_s, started_at FROM requests ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
