import random

from cyber_worker.macro.audit.reporter import daily_report
from cyber_worker.macro.audit.reviewer import rank, sample, suspicion


def e(id, status, score=None, at="2026-10-23T09:00:00", run="r1", reviewed=0, step="s01"):
    return {"id": id, "status": status, "page_score": score, "at": at, "run_id": run,
            "reviewed": reviewed, "script": "comment", "step_id": step,
            "suspicious": int(status in ("aborted", "failed"))}


def test_suspicion_order():
    assert suspicion(e(1, "failed")) > suspicion(e(1, "aborted")) > suspicion(e(1, "ok", 0.86)) \
        > suspicion(e(1, "ok", 0.94)) > suspicion(e(1, "ok")) > suspicion(e(1, "ok", 0.99)) \
        > suspicion(e(1, "skipped"))


def test_rank_puts_suspicious_first_then_newest():
    rows = [e(1, "ok", 0.99, at="09:00"), e(2, "aborted", at="09:01"),
            e(3, "ok", 0.99, at="09:02"), e(4, "failed", at="08:00")]
    assert [r["id"] for r in rank(rows)] == [4, 2, 3, 1]


def test_sample_only_unreviewed_ok():
    rows = [e(1, "ok"), e(2, "ok", reviewed=1), e(3, "aborted"), e(4, "ok")]
    picked = sample(rows, 10, random.Random(0))
    assert {r["id"] for r in picked} == {1, 4}


def test_daily_report():
    rows = [
        e(1, "ok", run="a"), e(2, "ok", run="a"),
        e(3, "ok", run="b"), e(4, "aborted", run="b", step="s02"),
        e(5, "ok", run="c"), e(6, "skipped", run="c"),
        e(7, "cancelled", run="d"),
    ]
    r = daily_report(rows)
    assert r["runs"] == 4
    assert r["run_status"] == {"ok": 1, "aborted": 1, "skipped": 1, "cancelled": 1}
    assert r["success_rate"] == round(2 / 3, 3)  # 急停不计入分母
    assert r["top_failures"] == [{"script": "comment", "step_id": "s02", "status": "aborted", "count": 1}]
    assert r["suspicious_unreviewed"] == 1
