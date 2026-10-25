"""Step / Script 数据模型。

坐标一律相对窗口左上角，单位是点（point），不是像素。录制时的窗口尺寸和缩放倍率
记在 `Window` 里，执行前 runner 会先把窗口校准到同样尺寸。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr


class Window(BaseModel):
    app: str  # 进程名，如 "WeChat"（Windows 上可带或不带 .exe）
    title: str | None = None  # 可选：窗口标题包含这段文字（同一程序开了多个窗口时用）
    width: int
    height: int
    scale: float = 1.0  # 录制时的屏幕缩放倍率（Retina 为 2.0）
    # 这个窗口的参考截图，相对脚本文件；studio 画布用。主窗口的在 Script.reference，这里只给其他窗口用
    reference: str | None = None
    # 悬停弹出的窗口（资料卡、悬浮菜单）：执行时只找位置，不切前台、不改尺寸——一抢焦点它就没了
    popup: bool = False


MAIN = "main"  # 主窗口的键


class _StepBase(BaseModel):
    id: str
    # 这一步在哪个窗口里：Script.windows 的键；None = 主窗口（Script.window）。
    # 一个宏可以跨多个窗口，比如在 QQ 主窗口双击会话，再到弹出的聊天窗口里打字
    win: str | None = None
    expected_page: str | None = None  # 执行这一步之前应当所在的页面（pages.yaml 中的名字）
    delay_after: float = 0.5  # 动作完成后等待多久再截图
    note: str = ""
    # 录制时这一步动作之前的窗口画面（相对脚本文件）；studio 画布用，执行时不看。画面没变的几步共用一张
    shot: str | None = None


Button = Literal["left", "right", "middle"]
Modifier = Literal["ctrl", "shift", "alt", "cmd"]


class Target(BaseModel):
    """按图找：执行时先在窗口里找这块图，点在它上面（相对位置和录制时一样）。

    界面变了（列表多了一条、窗口内容挪了）也能点中；找不到就等，超时中止，不会退回去点旧坐标。
    """

    image: str  # 目标图，相对脚本文件（<宏名>.targets/<步骤>.png）
    # 同一个目标的另一种样子：录制时目标图截在按下那一刻，按钮带着悬停 / 按下的颜色，平时的样子另存一张。
    # 执行时两张都比，取更像的；没有就只比 image
    alt: str | None = None
    x: float  # 录制时目标所在的区域（窗口内，点）
    y: float
    w: float
    h: float
    scale: float = 1.0  # 目标图截取时的缩放倍率
    threshold: float = Field(0.8, ge=0.5, le=1.0)
    timeout: float = Field(5.0, ge=0, le=120)  # 找不到时最多等几秒

    def images(self) -> list[str]:
        return [self.image, *([self.alt] if self.alt else [])]


class Click(_StepBase):
    action: Literal["click"] = "click"
    x: float
    y: float
    button: Button = "left"
    count: Literal[1, 2] = 1  # 2 = 双击：两下之间不移动、间隔很短，系统才认作双击
    hold: float = Field(0.0, ge=0, le=60)  # > 0 = 长按这么多秒（此时 count 不起作用）
    modifiers: list[Modifier] = []  # 点击时按住的键，如 Ctrl+点击 多选
    sends: bool = False  # 这一下就是「发送」：必须校验页面，空跑时跳过
    target: Target | None = None  # 有就按图找（录制时自动截取）；没有就按固定坐标
    use_target: bool = True  # 关掉 = 有目标图也按固定坐标点

    @property
    def finds_target(self) -> bool:
        return self.target is not None and self.use_target


class Drag(_StepBase):
    """按住从 (x, y) 拖到 (x2, y2)：拖滚动条、拖文件、划选文字。"""

    action: Literal["drag"] = "drag"
    x: float
    y: float
    x2: float
    y2: float
    button: Button = "left"
    duration: float = Field(0.5, gt=0, le=30)  # 拖动过程用时（秒）
    modifiers: list[Modifier] = []


class Hover(_StepBase):
    """把鼠标移到 (x, y) 停 seconds 秒：悬停才会出现的资料卡、菜单、浮出的按钮。
    录制时只有悬停之后画面真的变了（弹出新窗口、窗口内容变了）才记这一步。"""

    action: Literal["hover"] = "hover"
    x: float
    y: float
    seconds: float = Field(1.0, ge=0, le=30)  # 停多久（录制时你实际停的时间）
    target: Target | None = None  # 同点击：有就按图找（比如头像），列表顺序变了也能停对地方
    use_target: bool = True
    delay_after: float = 0.0  # 停够 seconds 已经等过了

    @property
    def finds_target(self) -> bool:
        return self.target is not None and self.use_target


class TypeText(_StepBase):
    action: Literal["type"] = "type"
    text: str  # 支持 {变量}，变量需在 Script.variables 中声明
    # 输入前先清空输入框（全选 + 删除）：空跑或被打断后残留的文字不会跟着下一次一起发出去
    clear_first: bool = True
    # 录制时开着中文输入法：键盘监听只看得到按键（如 nihao），看不到最后选的字。
    # 用户把 text 改成实际文字后清掉；没清掉时 runner 拒绝执行，免得把拼音发出去
    typed_with_ime: bool = False


class Key(_StepBase):
    action: Literal["key"] = "key"
    combo: str  # 如 "enter"、"cmd+v"
    times: int = Field(1, ge=1, le=500)  # 连按次数：按住不放时系统的自动重复也录成这个
    sends: bool = False


class Scroll(_StepBase):
    action: Literal["scroll"] = "scroll"
    dy: int = 0  # 滚轮格数，正数向下
    dx: int = 0  # 横向格数，正数向右
    x: float | None = None  # 在哪个位置滚动；为空则在当前鼠标位置
    y: float | None = None


class Wait(_StepBase):
    action: Literal["wait"] = "wait"
    seconds: float
    delay_after: float = 0.0


class WaitPage(_StepBase):
    """等某个页面出现（页面锚点对上）再往下走，最多等 timeout 秒，超时中止。比固定延迟靠谱：电脑卡也不会点空。"""

    action: Literal["wait_page"] = "wait_page"
    page: str
    timeout: float = Field(10.0, gt=0, le=300)
    delay_after: float = 0.0


Step = Annotated[Click | Drag | Hover | TypeText | Key | Scroll | Wait | WaitPage, Field(discriminator="action")]

_VAR = re.compile(r"\{(\w+)\}")


class Script(BaseModel):
    name: str
    platform: str | None = None
    window: Window  # 主窗口
    windows: dict[str, Window] = {}  # 录制中用到的其他窗口，键如 "w2"
    pages: str | None = None  # pages.yaml 路径，相对脚本文件
    reference: str | None = None  # 参考截图（录制开始时的窗口），相对脚本文件；studio 画布用
    variables: list[str] = []
    steps: list[Step]

    _source: Path | None = PrivateAttr(default=None)

    @classmethod
    def load(cls, path: str | Path) -> Script:
        path = Path(path)
        script = cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        script._source = path
        return script

    def dump(self, path: str | Path) -> None:
        data = self.model_dump(mode="json", exclude_defaults=True)
        # action 是判别字段，必须保留
        for raw, step in zip(data["steps"], self.steps):
            raw["action"] = step.action
        Path(path).write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    def renumber(self) -> dict[str, str]:
        """按顺序重新编号：动作是 s01、s02…，延迟单独是 d01、d02…（延迟不算「步」）。
        这样界面、运行日志、审计里的「第 N 步」都是同一个数。返回 旧编号 → 新编号（没变的不在里面）。"""
        counts = {"s": 0, "d": 0}
        new_ids = []
        for step in self.steps:
            kind = "d" if isinstance(step, Wait) else "s"
            counts[kind] += 1
            new_ids.append(f"{kind}{counts[kind]:02d}")
        renamed = {s.id: n for s, n in zip(self.steps, new_ids) if s.id != n}
        for step, new in zip(self.steps, new_ids):
            step.id = new
        return renamed

    def other_titles(self, key: str | None) -> tuple[str, ...]:
        """宏里除了 key 以外的窗口的标题：找 key 这个窗口时要避开它们。"""
        key = None if key in (None, MAIN) else key
        wins = [(None, self.window), *self.windows.items()]
        return tuple(w.title for k, w in wins if k != key and w.title)

    def window_for(self, key: str | None) -> Window:
        if key in (None, MAIN):
            return self.window
        if key not in self.windows:
            raise ValueError(f"宏里没有窗口 {key}")
        return self.windows[key]

    @property
    def path(self) -> Path | None:
        """从哪个文件读进来的（没从文件读的是 None）。"""
        return self._source

    @property
    def base_dir(self) -> Path:
        return self._source.parent if self._source else Path.cwd()

    @property
    def pages_path(self) -> Path | None:
        return None if self.pages is None else self.base_dir / self.pages

    @property
    def reference_path(self) -> Path | None:
        return None if self.reference is None else self.base_dir / self.reference

    def shown_reference(self, key: str | None) -> Path | None:
        """画布上这个窗口的「录制画面」：优先用它第一步的画面（按下鼠标那一刻截的，窗口一定在），
        没有才用存下来的参考图。旧版本录的宏，参考图可能是窗口关掉后截到的乱图（资料卡点完就没了）。"""
        key = None if key in (None, MAIN) else key
        for s in self.steps:
            if s.win == key and s.shot and (self.base_dir / s.shot).exists():
                return self.base_dir / s.shot
        return self.reference_for(key)

    def reference_for(self, key: str | None) -> Path | None:
        """某个窗口的参考截图（None / MAIN = 主窗口）。"""
        ref = self.reference if key in (None, MAIN) else self.window_for(key).reference
        return None if ref is None else self.base_dir / ref

    def render_text(self, text: str, values: dict[str, str]) -> str:
        """只替换已声明的变量；未声明的 {xxx} 原样保留。"""

        def sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in self.variables:
                return m.group(0)
            return values[name]

        return _VAR.sub(sub, text)

    def missing_variables(self, values: dict[str, str]) -> list[str]:
        return [v for v in self.variables if v not in values]


def needs_page_check(step: Click | Drag | TypeText | Key | Scroll | Wait | WaitPage) -> bool:
    """输入和发送之前必须核对页面；声明了 expected_page 的其他步骤也会核对。"""
    if step.expected_page is not None:
        return True
    return isinstance(step, TypeText) or getattr(step, "sends", False)
