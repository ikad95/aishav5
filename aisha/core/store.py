"""SQLite connection + schema migrations.

Single source of truth for the database connection. WAL mode for
concurrent readers (Slack listener + REPL + dump script can all touch
the file at once without stepping on each other).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

from .config import DB_PATH, MIGRATIONS_DIR

log = logging.getLogger(__name__)

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


class StoreError(Exception):
    pass


def connect() -> sqlite3.Connection:
    """Return the process-wide SQLite connection, creating it if needed."""
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(DB_PATH),
            check_same_thread=False,
            isolation_level=None,
            timeout=30.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        _conn = conn
        _run_migrations(conn)
        log.info("store: connected at %s", DB_PATH)
        return conn


def _run_migrations(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    files = sorted(p for p in MIGRATIONS_DIR.glob("*.sql"))
    for path in files:
        version = _version_of(path)
        if version <= current:
            continue
        log.info("store: applying migration %s", path.name)
        sql = path.read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {version}")
    final = conn.execute("PRAGMA user_version").fetchone()[0]
    log.info("store: schema at version %d", final)


def _version_of(path: Path) -> int:
    stem = path.stem
    try:
        return int(stem.split("_", 1)[0])
    except (ValueError, IndexError):
        raise StoreError(f"migration filename must start with a number: {path.name}")


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error:
                pass
            _conn = None
