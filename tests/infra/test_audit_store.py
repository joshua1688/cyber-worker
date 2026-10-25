from datetime import datetime

from cyber_worker.infra.sqlite.audit_store import SqliteAuditStore
from cyber_worker.infra.sqlite.db import connect
from cyber_worker.macro.audit.logger import AuditEntry, StepStatus


def test_roundtrip(tmp_path):
    store = SqliteAuditStore(connect(tmp_path / "state.db"))
    at = datetime(2026, 10, 23)
    store.write(AuditEntry("r1", at, "comment", "s02", "type", StepStatus.ABORTED,
                           expected_page="compose", page_score=0.4))
    store.write(AuditEntry("r1", at, "comment", "s03", "click", StepStatus.OK))
    rows = store.run("r1")
    assert [r["step_id"] for r in rows] == ["s02", "s03"]
    assert rows[0]["suspicious"] == 1 and rows[1]["suspicious"] == 0


def test_delete_day_and_retention(tmp_path):
    """清空某一天；保留期以前的记录和截图一起删。"""
    from datetime import date

    from cyber_worker.bootstrap import App
    from cyber_worker.config import Config
    from cyber_worker.macro.screenshot.storage import ScreenshotStorage

    store = SqliteAuditStore(connect(tmp_path / "state.db"))
    for day in (1, 10, 20, 23):
        store.write(AuditEntry(f"r{day}", datetime(2026, 10, day, 9), "comment", "s01", "click", StepStatus.OK))
        (tmp_path / "shots" / f"2026-10-{day:02d}").mkdir(parents=True)
    assert store.delete_day(date(2026, 10, 20)) == 1
    assert store.days() == ["2026-10-23", "2026-10-10", "2026-10-01"]

    cfg = Config()
    cfg.audit.keep_days = 7
    app = App(cfg, None, None, ScreenshotStorage(tmp_path / "shots"), store, None, None, None, None)
    assert app.prune_audit(date(2026, 10, 24)) == 2  # 早于 10-17 的
    assert store.days() == ["2026-10-23"]
    assert sorted(p.name for p in (tmp_path / "shots").iterdir()) == ["2026-10-20", "2026-10-23"]


def test_a_busy_day_is_not_cut_off(tmp_path):
    """一天超过 1000 条：按天取不截断（不然概况里的成功率会算错）。"""
    from datetime import date

    store = SqliteAuditStore(connect(tmp_path / "state.db"))
    for i in range(1005):
        store.write(AuditEntry(f"r{i}", datetime(2026, 10, 23, 9), "comment", "s01", "click", StepStatus.OK))
    assert len(store.entries(day=date(2026, 10, 23), limit=None)) == 1005
    assert len(store.entries()) == 1000


def test_runs_summarise_each_run(tmp_path):
    """每次运行一行：成功的只有时间；出了问题的带上第一处出问题的步骤和原因（应用首页的卡片上显示）。"""
    store = SqliteAuditStore(connect(tmp_path / "state.db"))
    store.write(AuditEntry("r1", datetime(2026, 10, 25, 9), "群聊天", "s01", "click", StepStatus.OK))
    store.write(AuditEntry("r2", datetime(2026, 10, 25, 10), "查看资料", "s01", "click", StepStatus.OK))
    store.write(AuditEntry("r2", datetime(2026, 10, 25, 10), "查看资料", "s02", "hover", StepStatus.ABORTED,
                           detail="s02 没找到要点的目标（等了 5 秒）"))
    ok, bad = store.runs()
    assert ok["script"] == "群聊天" and not ok["bad"] and ok["step_id"] is None
    assert bad["script"] == "查看资料" and bad["bad"] and bad["step_id"] == "s02" and "没找到" in bad["detail"]


def test_old_database_gets_the_app_column(tmp_path):
    """旧库没有 app 列：打开时补上，旧记录的应用是空的；改名给了应用就只改这个应用的（加上没记应用的旧记录）。"""
    import sqlite3

    path = tmp_path / "state.db"
    old = sqlite3.connect(path)
    old.execute("""CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, at TEXT NOT NULL,
        script TEXT NOT NULL, step_id TEXT NOT NULL, action TEXT NOT NULL, status TEXT NOT NULL, screenshot TEXT,
        expected_page TEXT, page_score REAL, dry_run INTEGER NOT NULL, suspicious INTEGER NOT NULL,
        detail TEXT NOT NULL DEFAULT '', reviewed INTEGER NOT NULL DEFAULT 0)""")
    old.execute("""INSERT INTO audit_log (run_id, at, script, step_id, action, status, dry_run, suspicious)
                   VALUES ('r0', '2026-10-25T09:00:00', 'chat', 's01', 'click', 'ok', 0, 0)""")
    old.commit()
    old.close()

    store = SqliteAuditStore(connect(path))
    at = datetime(2026, 10, 25, 10)
    store.write(AuditEntry("r1", at, "chat", "s01", "click", StepStatus.OK, app="qq"))
    store.write(AuditEntry("r2", at, "chat", "s01", "click", StepStatus.OK, app="wx"))
    assert [(r["run_id"], r["app"]) for r in store.runs()] == [("r0", None), ("r1", "qq"), ("r2", "wx")]

    assert store.rename_script("chat", "群聊天", "qq") == 2
    assert [(r["run_id"], r["script"]) for r in store.runs()] == [("r0", "群聊天"), ("r1", "群聊天"), ("r2", "chat")]
