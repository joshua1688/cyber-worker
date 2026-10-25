"""操作日志：每一步（包括被中止、被跳过的）都留一条记录。存储由 infra 实现。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol


class StepStatus(str, Enum):
    OK = "ok"
    ABORTED = "aborted"  # 页面校验没通过，主动中止——审计里最该看的
    SKIPPED = "skipped"  # 空跑跳过了「发送」
    FAILED = "failed"  # 执行出错
    CANCELLED = "cancelled"  # 急停


@dataclass
class AuditEntry:
    run_id: str
    at: datetime
    script: str
    step_id: str
    action: str
    status: StepStatus
    screenshot: str | None = None
    expected_page: str | None = None
    page_score: float | None = None
    dry_run: bool = False
    detail: str = ""
    app: str | None = None  # 宏所在的应用（平台目录名）：宏名只在同一个应用里不重复，审计靠它分清是哪个应用
    suspicious: bool = field(init=False)

    def __post_init__(self) -> None:
        self.suspicious = self.status in (StepStatus.ABORTED, StepStatus.FAILED)


class AuditSink(Protocol):
    def write(self, entry: AuditEntry) -> None: ...
