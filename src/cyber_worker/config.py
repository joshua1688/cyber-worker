"""读取 config.toml。目前只覆盖最小闭环用到的段落，其余段落按路线图逐步补上。"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel


class ExecutorConfig(BaseModel):
    pause_on_user_input: bool = True
    resume_after_idle_sec: float = 120
    # Windows 上 cmd 是 Win 键，带 Win 的组合常被系统占用，换一组
    panic_hotkey: str = "ctrl+alt+cmd+." if sys.platform == "darwin" else "ctrl+alt+shift+."
    dry_run: bool = False


class StudioConfig(BaseModel):
    # 全局快捷键：开始 / 停止录制（同一个键切换）。在任何窗口里都能按，按键本身不会被录进宏
    record_hotkey: str = "alt+r"
    theme: Literal["dark", "light", "system"] = "dark"
    record_delays: bool = True  # 录制时把操作之间的间隔录成延迟行
    record_append: bool = False  # 录制时接在已有步骤后面；默认替换（重新录一遍就是要这一遍）
    run_idle_sec: float = 2.0  # 在 studio 里点空跑 / 执行后，等键鼠停下几秒才开始


class PathsConfig(BaseModel):
    user_data: Path = Path("user_data")


class Config(BaseModel, extra="allow"):
    executor: ExecutorConfig = ExecutorConfig()
    studio: StudioConfig = StudioConfig()
    paths: PathsConfig = PathsConfig()

    @classmethod
    def load(cls, path: str | Path = "config.toml") -> Config:
        path = Path(path)
        if not path.exists():
            return cls()
        with path.open("rb") as f:
            return cls.model_validate(tomllib.load(f))


def _toml_value(value: str | bool | float) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_value(path: str | Path, section: str, key: str, value: str | bool | float) -> None:
    """改 config.toml 里的一项，保留其余内容和注释；没有这一段 / 这一项就补上。"""
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    new = f"{key} = {_toml_value(value)}"
    start = next((i for i, ln in enumerate(lines) if ln.split("#", 1)[0].strip() == f"[{section}]"), None)
    if start is None:
        lines += ["", f"[{section}]", new] if lines else [f"[{section}]", new]
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        set_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
        commented_re = re.compile(rf"^\s*#\s*{re.escape(key)}\s*=")
        hit = next((i for i in range(start + 1, end) if set_re.match(lines[i])), None)
        if hit is None:
            hit = next((i for i in range(start + 1, end) if commented_re.match(lines[i])), None)
        if hit is None:
            lines.insert(start + 1, new)
        else:
            # 保留行尾注释（# 后面的说明）；值里不会出现 #，可以直接按 # 切
            body = lines[hit].lstrip().lstrip("#").strip()
            comment = body.split("#", 1)[1].strip() if "#" in body else ""
            lines[hit] = f"{new}  # {comment}" if comment else new
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
