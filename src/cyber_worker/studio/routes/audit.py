"""审计页：按可疑度排序回看每一步的截图和预期状态，标记「没问题 / 有问题」。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from cyber_worker.flow.audit.reporter import daily_report
from cyber_worker.flow.audit.reviewer import rank, sample
from cyber_worker.studio.routes.edit import studio

router = APIRouter(prefix="/api/audit")


class Mark(BaseModel):
    reviewed: int  # 0 未审 / 1 没问题 / 2 有问题


@router.get("/days")
def days(request: Request):
    app = studio(request).app
    # 保留期内的每一天都能选；每天带上跑了几次、几次出了问题（日期下拉里显示）
    return app.audit.day_summary(limit=app.config.audit.keep_days + 1)


@router.get("")
def entries(request: Request, day: date | None = None, platform: str | None = None, flow: str | None = None,
            unreviewed: bool = False, spot_check: int = 5):
    """platform / flow 筛选：只给 platform 是这个平台（应用）的全部流程，两个都给是这个应用里的这个流程。
    每条记录带上 platform（没记应用的旧记录按流程名认，认不出是 None）、platform_title 和 flow_id（流程已删除是 None）。"""
    st = studio(request)
    titles, ids, owners = st.audit_apps()
    # 按天看时不设上限：截断了的话概况（成功率、失败数）会算错，而且不会有任何提示
    rows = st.app.audit.entries(day=day, unreviewed_only=unreviewed, limit=None if day is not None else 1000)
    for r in rows:
        r["platform"] = r.get("platform") or owners.get(r["flow"])
        r["platform_title"] = titles.get(r["platform"], r["platform"])  # 应用删了（在回收站里）：显示目录名
        r["flow_id"] = ids.get((r["platform"], r["flow"]))
    # 下拉里的选项：这一天出现过的应用和流程、各跑了几次，不受当前筛选影响（选了一个流程，别的还能切回去）
    runs: dict[tuple[str, str, str], set[str]] = {}
    for r in rows:
        runs.setdefault((r["platform"] or "", r["platform_title"] or "", r["flow"]), set()).add(r["run_id"])
    if platform is not None:
        rows = [r for r in rows if (r["platform"] or "") == platform and (flow is None or r["flow"] == flow)]
    elif flow:
        rows = [r for r in rows if r["flow"] == flow]
    for r in rows:
        r["screenshot_url"] = st.file_url(Path(r["screenshot"])) if r["screenshot"] else None
    ranked = rank(rows)
    spot_ids = {r["id"] for r in sample(rows, spot_check)}
    for r in ranked:
        r["spot_check"] = r["id"] in spot_ids
    return {"report": daily_report(rows), "entries": ranked,
            "flows": [{"platform": a or None, "platform_title": t or None, "flow": m, "runs": len(ids)}
                      for (a, t, m), ids in sorted(runs.items())]}


@router.delete("/days/{day}")
def delete_day(day: date, request: Request):
    """清空这一天：记录和截图一起删。"""
    app = studio(request).app
    n = app.audit.delete_day(day)
    app.storage.remove_day(day)
    return {"deleted": n}


@router.post("/{entry_id}")
def mark(entry_id: int, body: Mark, request: Request):
    try:
        ok = studio(request).app.audit.mark(entry_id, body.reviewed)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not ok:
        raise HTTPException(404, "记录不存在")
    return {"ok": True}
