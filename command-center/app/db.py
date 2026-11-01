"""SQLite storage. One file, WAL mode, FTS5 for note search."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses (
    code TEXT PRIMARY KEY,
    name TEXT,
    color TEXT,
    canvas_id TEXT,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uid TEXT UNIQUE,                -- "<source>:<external id>"; NULL for manual
    source TEXT NOT NULL,           -- canvas | canvas_ics | gmail | manual
    course TEXT,
    title TEXT NOT NULL,
    kind TEXT DEFAULT 'assignment', -- assignment | quiz | exam | reading | task | lab | project
    due_at TEXT,                    -- ISO8601 UTC
    url TEXT,
    points REAL,
    effort_hours REAL,
    status TEXT DEFAULT 'open',     -- open | done | dismissed
    submitted INTEGER DEFAULT 0,
    details TEXT,
    pinned INTEGER DEFAULT 0,
    gcal_event_id TEXT,
    created_at TEXT,
    updated_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_due ON items(status, due_at);

CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER REFERENCES items(id) ON DELETE CASCADE,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    gcal_event_id TEXT,
    status TEXT DEFAULT 'planned'   -- planned | done | cancelled
);
CREATE INDEX IF NOT EXISTS idx_blocks_start ON blocks(start_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gcal_id TEXT UNIQUE,
    calendar TEXT,
    title TEXT,
    start_at TEXT,
    end_at TEXT,
    all_day INTEGER DEFAULT 0,
    location TEXT,
    owned INTEGER DEFAULT 0          -- 1 if on the Command Center calendar
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_at);

CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    course TEXT,
    name TEXT NOT NULL,
    notes TEXT,
    source TEXT DEFAULT 'manual',
    struggle_count INTEGER DEFAULT 1,
    ease REAL DEFAULT 2.5,
    interval_days REAL DEFAULT 0,
    reps INTEGER DEFAULT 0,
    lapses INTEGER DEFAULT 0,
    due_at TEXT,
    last_reviewed TEXT,
    archived INTEGER DEFAULT 0,
    exam_date TEXT,
    created_at TEXT,
    UNIQUE(course, name)
);

CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer TEXT,
    source TEXT DEFAULT 'manual',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    card_id INTEGER,
    grade INTEGER,                  -- 0 again, 1 hard, 2 good, 3 easy
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE,
    root TEXT,
    course TEXT,
    title TEXT,
    ext TEXT,
    mtime REAL,
    size INTEGER,
    indexed_at TEXT,
    error TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    content, title, course, doc_id UNINDEXED, chunk_no UNINDEXED, page UNINDEXED,
    tokenize = 'porter unicode61'
);

CREATE TABLE IF NOT EXISTS mail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_id TEXT UNIQUE,
    sender TEXT,
    subject TEXT,
    snippet TEXT,
    received_at TEXT,
    course TEXT,
    url TEXT,
    archived INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quick_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    body TEXT NOT NULL,
    course TEXT,
    created_at TEXT,
    done INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT,
    started_at TEXT,
    finished_at TEXT,
    ok INTEGER,
    count INTEGER,
    message TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def now_iso() -> str:
    return iso(utcnow())  # type: ignore[return-value]


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple | dict = ()) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
            return dict(r) if r else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        with self.tx() as c:
            cur = c.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    # --- kv -----------------------------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        from .config import get_config

        _db = Database(get_config().db_path)
    return _db


def set_db(db: Database | None) -> None:
    global _db
    _db = db
