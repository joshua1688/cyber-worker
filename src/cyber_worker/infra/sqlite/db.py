"""state.db 连接与建表。待确认队列、行为记忆后续也放这里。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    at            TEXT NOT NULL,
    script        TEXT NOT NULL,
    step_id       TEXT NOT NULL,
    action        TEXT NOT NULL,
    status        TEXT NOT NULL,
    screenshot    TEXT,
    expected_page TEXT,
    page_score    REAL,
    dry_run       INTEGER NOT NULL,
    suspicious    INTEGER NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    reviewed      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS audit_log_run ON audit_log (run_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # 执行器在后台线程写，studio 在请求线程读：关掉线程检查，由调用方加锁
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn
