from __future__ import annotations

import sqlite3
import threading
from datetime import date

from cyber_worker.flow.audit.logger import AuditEntry

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
                   (run_id, at, flow, step_id, action, status, screenshot,
                    expected_page, page_score, dry_run, suspicious, detail, platform)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.run_id,
                    entry.at.isoformat(timespec="seconds"),
                    entry.flow,
                    entry.step_id,
                    entry.action,
                    entry.status.value,
                    entry.screenshot,
                    entry.expected_page,
                    entry.page_score,
                    int(entry.dry_run),
                    int(entry.suspicious),
                    entry.detail,
                    entry.platform,
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
        flow: str | None = None,
        unreviewed_only: bool = False,
        limit: int | None = 1000,
    ) -> list[dict]:
        sql, args = "SELECT * FROM audit_log WHERE 1=1", []
        if day is not None:
            sql += " AND substr(at, 1, 10) = ?"
            args.append(day.isoformat())
        if flow:
            sql += " AND flow = ?"
            args.append(flow)
        if unreviewed_only:
            sql += " AND reviewed = 0"
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def runs(self) -> list[dict]:
        """每次运行一行，按先后：流程名、开始时间、是不是空跑、有没有出问题（中止 / 出错）；
        出了问题的，带上第一处出问题的步骤和原因（应用首页的卡片上显示）。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT run_id, flow, MAX(platform) AS platform, MIN(at) AS at, MAX(dry_run) AS dry,
                          MAX(status IN ('failed', 'aborted')) AS bad
                   FROM audit_log GROUP BY run_id ORDER BY MIN(id)"""
            ).fetchall()
            first_bad = {r["run_id"]: r for r in self._conn.execute(
                """SELECT run_id, step_id, detail FROM audit_log WHERE id IN
                   (SELECT MIN(id) FROM audit_log WHERE status IN ('failed', 'aborted') GROUP BY run_id)"""
            ).fetchall()}
        out = []
        for r in rows:
            b = first_bad.get(r["run_id"])
            out.append({"run_id": r["run_id"], "flow": r["flow"], "platform": r["platform"], "at": r["at"], "dry_run": bool(r["dry"]),
                        "bad": bool(r["bad"]), "step_id": b["step_id"] if b else None,
                        "detail": b["detail"] if b else ""})
        return out

    def rename_flow(self, old: str, new: str, platform: str | None = None) -> int:
        """流程改了名：以前的记录跟着改名（记录按流程名对应流程，不改的话卡片上「最近一次」和审计历史都对不上）。
        给了 platform 就只改这个平台的（加上没记应用的旧记录），别的应用里同名的流程不受影响。"""
        sql, args = "UPDATE audit_log SET flow = ? WHERE flow = ?", [new, old]
        if platform is not None:
            sql += " AND (platform = ? OR platform IS NULL)"
            args.append(platform)
        with self._lock, self._conn:
            cur = self._conn.execute(sql, args)
        return cur.rowcount

    def days(self, limit: int = 30) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT substr(at, 1, 10) AS d FROM audit_log ORDER BY d DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["d"] for r in rows]

    def day_summary(self, limit: int = 30) -> list[dict]:
        """最近有记录的几天，新的在前：每天跑了几次、几次出了问题（中止 / 出错）。审计页的日期下拉用。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT d, COUNT(*) AS runs, SUM(bad) AS bad FROM
                     (SELECT substr(MIN(at), 1, 10) AS d, MAX(status IN ('failed', 'aborted')) AS bad
                      FROM audit_log GROUP BY run_id)
                   GROUP BY d ORDER BY d DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [{"day": r["d"], "runs": r["runs"], "bad": r["bad"] or 0} for r in rows]

    def delete_day(self, day: date) -> int:
        """删掉这一天的全部记录，返回删了几条。截图由调用方删（ScreenshotStorage.remove_day）。"""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM audit_log WHERE substr(at, 1, 10) = ?", (day.isoformat(),))
        return cur.rowcount

    def delete_before(self, day: date) -> int:
        """删掉早于这一天的记录（保留期过了），返回删了几条。"""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM audit_log WHERE substr(at, 1, 10) < ?", (day.isoformat(),))
        return cur.rowcount

    def mark(self, entry_id: int, reviewed: int) -> bool:
        if reviewed not in (UNREVIEWED, LOOKS_RIGHT, HAS_PROBLEM):
            raise ValueError(f"reviewed 只能是 0/1/2：{reviewed}")
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE audit_log SET reviewed = ? WHERE id = ?", (reviewed, entry_id)
            )
        return cur.rowcount == 1
