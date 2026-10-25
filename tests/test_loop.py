"""最小闭环：磁盘上的流程 + pages.yaml → 执行器排队 → runner 执行 → 截图落盘 → 审计进 state.db。"""

import random
from datetime import datetime

import yaml
from fakes import FakeActivity, FakeIO
from helpers import make_book, pattern, screen_with

from cyber_worker.flow.executor import Executor
from cyber_worker.flow.runner import Runner, RunStatus
from cyber_worker.flow.schema import Flow
from cyber_worker.flow.screenshot.storage import ScreenshotStorage
from cyber_worker.flow.vision.fingerprint import PageBook
from cyber_worker.infra.sqlite.audit_store import SqliteAuditStore
from cyber_worker.infra.sqlite.db import connect


def test_minimal_loop(tmp_path):
    pack = tmp_path / "wechat"
    make_book(pack)  # 写出 pack/pages.yaml + pack/anchors/compose.png
    (pack / "moments_comment.yaml").write_text(yaml.safe_dump({
        "name": "moments_comment",
        "window": {"app": "WeChat", "width": 400, "height": 300},
        "pages": "pages.yaml",
        "variables": ["comment_draft"],
        "steps": [
            {"id": "s01", "action": "click", "x": 20, "y": 30},
            {"id": "s02", "action": "type", "text": "{comment_draft}", "expected_page": "compose"},
            {"id": "s03", "action": "key", "combo": "enter", "sends": True, "expected_page": "compose"},
        ],
    }, allow_unicode=True), encoding="utf-8")

    flow = Flow.load(pack / "moments_comment.yaml")
    pages = PageBook.load(flow.pages_path)
    io = FakeIO(lambda: screen_with(pattern()))
    audit = SqliteAuditStore(connect(tmp_path / "user_data" / "state.db"))
    runner = Runner(io, ScreenshotStorage(tmp_path / "user_data" / "screenshots"), audit,
                    rng=random.Random(0), sleep=lambda s: None,
                    now=lambda: datetime(2026, 10, 23, 9, 30))
    executor = Executor(FakeActivity(), poll_interval=0.01)

    def submit(dry_run):
        return executor.submit(lambda gate: runner.run(
            flow, pages=pages, variables={"comment_draft": "拍得真好"}, dry_run=dry_run, gate=gate,
        )).result(timeout=5)

    dry = submit(dry_run=True)
    assert dry.status is RunStatus.DRY_RUN_STOPPED
    assert ("combo", "enter") not in io.calls

    live = submit(dry_run=False)
    assert live.status is RunStatus.OK
    assert ("combo", "enter") in io.calls

    rows = audit.run(live.run_id)
    assert [(r["step_id"], r["status"]) for r in rows] == [("s01", "ok"), ("s02", "ok"), ("s03", "ok")]
    shots = list((tmp_path / "user_data" / "screenshots" / "2026-10-23").glob("*.webp"))
    assert shots and all(r["screenshot"] for r in rows)
    executor.shutdown()
