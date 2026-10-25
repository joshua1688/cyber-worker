"""按可疑度排序、抽样复核。纯逻辑，输入是审计记录（dict），不碰数据库和 HTTP。

可疑度（0–1，越高越该先看）：
- failed 执行出错                  1.0
- aborted 页面不符 / 被打断中止      0.9
- ok 但页面得分不够高（< 0.95）      0.4–0.7（得分越低越可疑）
- ok 且没有做页面校验               0.2（点击、滚动这类没声明页面的步骤，最容易「点歪了还以为成功」）
- ok 且页面得分很高                 0.05
- ok 但按图找的得分离门槛很近（< 0.85）、又没能自动记住新样子  至少 0.5（下次可能就找不到）
- ok 但页面锚点挪了位置（界面布局变了，在整个窗口里才找到）  至少 0.45（看一眼截图，确认做在了对的地方）
- skipped 空跑跳过 / cancelled 急停 / 执行时你动了键鼠被打断  0.0（你自己知道发生了什么，程序也没做出会错的事）
"""

from __future__ import annotations

import random
import re
from collections.abc import Iterable, Mapping

from cyber_worker.flow.audit.reporter import outcome

HIGH_CONFIDENCE = 0.95  # 页面得分高于这个值视为确定在正确页面
LOW_PASS = 0.85  # 锚点默认阈值
TARGET_WEAK = 0.85  # 按图找的得分低于这个（目标图默认门槛 0.8）：找是找到了，但差一点就找不到
_TARGET_SCORE = re.compile(r"按图找到目标.*（得分 ([\d.]+)）")  # runner._found_text 写的


def target_score(entry: Mapping) -> float | None:
    """这一步按图找到目标时的得分；不是按图找的步骤返回 None。"""
    m = _TARGET_SCORE.search(entry.get("detail") or "")
    return float(m.group(1)) if m else None


def suspicion(entry: Mapping) -> float:
    status = outcome(entry)  # 「你在用电脑」的中止按急停算
    if status == "failed":
        return 1.0
    if status == "aborted":
        return 0.9
    if status in ("skipped", "cancelled"):
        return 0.0
    page = _page_suspicion(entry)
    target = target_score(entry)
    detail = entry.get("detail") or ""
    learned = "已自动记住新样子" in detail  # runner.LEARNED_NOTE：已经自己修好了
    if target is not None and target < TARGET_WEAK and not learned:
        page = max(page, 0.5)
    if "页面锚点挪了位置" in detail:  # runner.SHIFT_NOTE
        page = max(page, 0.45)
    return page


def _page_suspicion(entry: Mapping) -> float:
    score = entry.get("page_score")
    if score is None:
        return 0.2
    if score >= HIGH_CONFIDENCE:
        return 0.05
    # 通过了阈值（默认 0.85）但不够高：得分 0.95 → 0.4，得分 0.85 及以下 → 0.7
    gap = min(1.0, (HIGH_CONFIDENCE - score) / (HIGH_CONFIDENCE - LOW_PASS))
    return round(0.4 + 0.3 * gap, 3)


def rank(entries: Iterable[Mapping]) -> list[dict]:
    """按可疑度降序，同分按时间倒序；每条附上 suspicion 字段。"""
    out = [{**e, "suspicion": suspicion(e)} for e in entries]
    out.sort(key=lambda e: e["at"], reverse=True)
    out.sort(key=lambda e: e["suspicion"], reverse=True)  # 稳定排序：同分保持时间倒序
    return out


def sample(entries: Iterable[Mapping], k: int, rng: random.Random | None = None) -> list[dict]:
    """从「看起来成功」的记录里随机抽 k 条做抽查：可疑的会被优先看到，成功的也不能完全不看。"""
    ok = [dict(e) for e in entries if e["status"] == "ok" and not e.get("reviewed")]
    rng = rng or random.Random()
    return rng.sample(ok, min(k, len(ok)))
