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
