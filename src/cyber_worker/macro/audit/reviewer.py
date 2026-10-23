"""按可疑度排序、抽样复核。纯逻辑，输入是审计记录（dict），不碰数据库和 HTTP。

可疑度（0–1，越高越该先看）：
- failed 执行出错                  1.0
- aborted 页面不符 / 被打断中止      0.9
- ok 但页面得分不够高（< 0.95）      0.4–0.7（得分越低越可疑）
- ok 且没有做页面校验               0.2（点击、滚动这类没声明页面的步骤，最容易「点歪了还以为成功」）
- ok 且页面得分很高                 0.05
- skipped 空跑跳过 / cancelled 急停  0.0（你自己知道发生了什么）
"""

from __future__ import annotations

import random
from collections.abc import Iterable, Mapping

HIGH_CONFIDENCE = 0.95  # 页面得分高于这个值视为确定在正确页面
LOW_PASS = 0.85  # 锚点默认阈值


def suspicion(entry: Mapping) -> float:
    status = entry["status"]
    if status == "failed":
        return 1.0
    if status == "aborted":
        return 0.9
    if status in ("skipped", "cancelled"):
        return 0.0
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
