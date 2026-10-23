from __future__ import annotations

import sqlite3
import threading
from datetime import date

from cyber_worker.macro.audit.logger import AuditEntry

# reviewed 列的取值
UNREVIEWED, LOOKS_RIGHT, HAS_PROBLEM = 0, 1, 2


class SqliteAuditStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    def write(self, entry: AuditEntry) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO audit_log
                   (run_id, at, script, step_id, action, status, screenshot,
                    expected_page, page_score, dry_run, suspicious, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.run_id,
                    entry.at.isoformat(timespec="seconds"),
                    entry.script,
                    entry.step_id,
                    entry.action,
                    entry.status.value,
                    entry.screenshot,
                    entry.expected_page,
                    entry.page_score,
                    int(entry.dry_run),
                    int(entry.suspicious),
                    entry.detail,
                ),
            )

    def run(self, run_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM audit_log WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()

    def entries(
        self,
        *,
        day: date | None = None,
        script: str | None = None,
        unreviewed_only: bool = False,
        limit: int = 1000,
    ) -> list[dict]:
        sql, args = "SELECT * FROM audit_log WHERE 1=1", []
        if day is not None:
            sql += " AND substr(at, 1, 10) = ?"
            args.append(day.isoformat())
        if script:
            sql += " AND script = ?"
            args.append(script)
        if unreviewed_only:
            sql += " AND reviewed = 0"
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def days(self, limit: int = 30) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT substr(at, 1, 10) AS d FROM audit_log ORDER BY d DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["d"] for r in rows]

    def mark(self, entry_id: int, reviewed: int) -> bool:
        if reviewed not in (UNREVIEWED, LOOKS_RIGHT, HAS_PROBLEM):
            raise ValueError(f"reviewed 只能是 0/1/2：{reviewed}")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE audit_log SET reviewed = ? WHERE id = ?", (reviewed, entry_id)
            )
        return cur.rowcount == 1
