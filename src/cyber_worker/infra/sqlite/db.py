"""state.db 连接与建表。待确认队列、行为记忆后续也放这里。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    at            TEXT NOT NULL,
    flow          TEXT NOT NULL,
    step_id       TEXT NOT NULL,
    action        TEXT NOT NULL,
    status        TEXT NOT NULL,
    screenshot    TEXT,
    expected_page TEXT,
    page_score    REAL,
    dry_run       INTEGER NOT NULL,
    suspicious    INTEGER NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    reviewed      INTEGER NOT NULL DEFAULT 0,
    platform      TEXT
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
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """旧库补上后来加的列（CREATE TABLE IF NOT EXISTS 不会改已有的表）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(audit_log)")}
    if "script" in cols and "flow" not in cols:  # 「宏 / 脚本」改叫「流程」以前的库：列名跟着改
        with conn:
            conn.execute("ALTER TABLE audit_log RENAME COLUMN script TO flow")
    if "app" in cols and "platform" not in cols:  # 这一列起初叫 app，和 Window.app（程序名）撞名
        with conn:
            conn.execute("ALTER TABLE audit_log RENAME COLUMN app TO platform")
    elif "platform" not in cols:  # 以前只记流程名，不知道是哪个平台的：旧记录留空，显示时按流程名去找
        with conn:
            conn.execute("ALTER TABLE audit_log ADD COLUMN platform TEXT")
