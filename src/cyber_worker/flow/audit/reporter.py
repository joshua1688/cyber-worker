"""日报：成功率、失败分布。纯逻辑，输入是审计记录（dict）。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping

USER_INTERRUPT = "检测到你在用电脑"  # runner 里 UserInterrupted 的中止原因


def outcome(entry: Mapping) -> str:
    """算成功率时这一步算什么：执行时你动了键鼠、程序让给你（记成 aborted）和急停一样是人为打断，
    不是流程失败，按 cancelled 算（不进成功率的分母，也不进失败排行）。"""
    if entry["status"] == "aborted" and USER_INTERRUPT in (entry.get("detail") or ""):
        return "cancelled"
    return entry["status"]


def daily_report(entries: Iterable[Mapping]) -> dict:
    entries = list(entries)
    by_status = Counter(e["status"] for e in entries)
    runs: dict[str, str] = {}
    for e in entries:
        # 一次运行的结果 = 它最「坏」的那一步
        worst = runs.get(e["run_id"], "ok")
        runs[e["run_id"]] = max(worst, outcome(e), key=_SEVERITY.__getitem__)
    run_status = Counter(runs.values())
    finished = sum(n for s, n in run_status.items() if s != "cancelled")
    succeeded = run_status["ok"] + run_status["skipped"]  # 空跑正常停在发送前也算成功

    failures = Counter(
        (e["flow"], e["step_id"], e["status"])
        for e in entries if outcome(e) in ("failed", "aborted")
    )
    return {
        "steps": len(entries),
        "by_status": dict(by_status),
        "runs": len(runs),
        "run_status": dict(run_status),
        "success_rate": round(succeeded / finished, 3) if finished else None,
        "top_failures": [
            {"flow": s, "step_id": step, "status": st, "count": n}
            for (s, step, st), n in failures.most_common(10)
        ],
        "suspicious_unreviewed": sum(
            1 for e in entries if e["suspicious"] and not e.get("reviewed") and outcome(e) != "cancelled"
        ),
    }


_SEVERITY = {"ok": 0, "skipped": 1, "cancelled": 2, "aborted": 3, "failed": 4}
