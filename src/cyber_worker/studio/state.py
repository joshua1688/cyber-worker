"""studio 的共享状态：脚本目录、最新截图帧、录制会话。路由只通过这里访问文件系统。"""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from cyber_worker.adapters import pack
from cyber_worker.bootstrap import App
from cyber_worker.macro.anchors import anchors_dir, default_pages_ref, refs_dir, shots_dir, targets_dir
from cyber_worker.macro.io.base import stable_title
from cyber_worker.macro.recorder import RecordingBuilder
from cyber_worker.macro.schema import MAIN, Click, Hover, Script, Step, TypeText, WaitPage, needs_page_check
from cyber_worker.macro.vision.fingerprint import PageBook
from cyber_worker.macro.screenshot.storage import write_image
from cyber_worker.studio.ws import Hub

_NAME = re.compile(r"^[\w\-]+$")
DRAFT = "_draft/"  # 草稿宏 id 的前缀
_VAR_USE = re.compile(r"\{(\w+)\}")


def check_name(value: str, what: str) -> str:
    if not _NAME.match(value):
        raise HTTPException(400, f"{what}只能包含文字、数字、下划线和连字符")
    return value


# 文件名里不能出现的字符（Windows 最严），以及会破坏 URL 的字符
_UNSAFE = re.compile(r'[\\/:*?"<>|#%&\s]+')


def safe_stem(name: str) -> str:
    """用户起的宏名（可以是中文、带空格）→ 文件名。"""
    stem = _UNSAFE.sub("_", name.strip()).strip("._")
    return stem[:60] or "macro"


# 「文档名 - 程序名」这类标题：前半截跟着打开的文件、网页变，按它认窗口以后会对不上
_CHANGING_TITLE = re.compile(r" [-–—|] ")


def window_json(w, shared_apps: set[str]) -> dict:
    """窗口 → 前端需要的一切，包括替用户想好的宏名和用来认窗口的标题。"""
    title = stable_title(w.title)
    shared = w.app.lower() in shared_apps
    return {
        "app": w.app, "title": w.title, "width": w.width, "height": w.height,
        "minimized": getattr(w, "minimized", False),
        "platform": platform_slug(w.app),
        "shared": shared,
        # 按标题认窗口：标题固定（「QQ」「AIGC研究社」）就记下来，以后同一个程序再开别的窗口也认得出主窗口。
        # 带文件名、网页名这种会变的标题（「a.txt - 记事本」）只在同时开着好几个窗口时才不得不用
        "match_title": title if title and (shared or not _CHANGING_TITLE.search(title)) else None,
        "suggested_name": title if title and title.lower() != w.app.lower() else w.app,
    }


_SEQ = re.compile(r"-\d+$")


def renamed(name: str, old_stem: str, new_stem: str) -> str:
    """文件名因重名加了序号（chat → chat-2），显示名也加上同样的序号，列表里分得清。"""
    seq = _SEQ.search(new_stem[len(old_stem):]) if new_stem.startswith(old_stem) else None
    return name + seq.group(0) if seq else name


def platform_slug(app: str) -> str:
    """程序名 → 平台目录名：WeChat.exe → wechat。"""
    app = re.sub(r"\.(exe|app)$", "", app.strip(), flags=re.I)
    return safe_stem(app).lower() or "app"


@dataclass
class Frame:
    path: Path
    scale: float
    win: str | None = None  # 截的是哪个窗口


@dataclass
class Recording:
    script_id: str
    builder: RecordingBuilder
    stop: threading.Event
    thread: threading.Thread
    geo: object = None  # WindowGeometry
    frames: list = field(default_factory=list)  # 录制期间定时截的图 (窗口键, 缩放, 图)，录完用来自动选锚点
    sampler: threading.Thread | None = None
    # 每次鼠标按下时截的目标图：按下序号 → (缩放, 目标区域, 目标图)；认不出来的点击不在里面
    press_targets: dict = field(default_factory=dict)
    press_threads: list = field(default_factory=list)
    # 每一步动作之前的窗口画面：点击 / 拖动按按下序号存，其他步骤按步骤编号存，值是 (窗口键, 缩放, 图)
    press_frames: dict = field(default_factory=dict)
    step_frames: dict = field(default_factory=dict)
    # 悬停后画面变了的那几次：鼠标停到那里的时间 → (窗口键, 缩放, 悬停前的画面, 新弹出的窗口标题)
    hover_info: dict = field(default_factory=dict)
    hover_thread: threading.Thread | None = None
    append: bool = False  # 录到的步骤接在已有步骤后面（否则替换）


@dataclass
class Studio:
    app: App
    hub: Hub = field(default_factory=Hub)
    # studio 里手动点「执行」后只需空闲这么久就开始（默认配置是 120 秒，面向无人值守）
    run_idle_sec: float = 2.0  # 设置里可改（[studio] run_idle_sec）
    latest: dict[str, dict[str, Frame]] = field(default_factory=dict)  # 宏 → 窗口键 → 这个窗口最新的一帧
    recording: Recording | None = None
    current_id: str | None = None  # 界面上当前打开的宏：按录制 / 空跑快捷键时用的就是它
    current_vars: dict[str, str] = field(default_factory=dict)  # 界面上给它填的变量（空跑快捷键用）
    # 报告当前宏的那个页面（每个标签页一个随机编号）、它有没有没保存的修改：
    # 有的话空跑快捷键交给那个页面去跑（先写回再跑，和点按钮一样），不然跑的是磁盘上的旧版本
    current_page: str | None = None
    current_dirty: bool = False
    record_delays: bool = True  # 上次录制时「录制延迟」的选择，快捷键开始录制时沿用
    record_append: bool = False  # 录制时接在已有步骤后面，而不是替换（[studio] record_append）
    # 开始 / 停止录制互斥：停止要好几秒，期间再按快捷键不能又开一次录制
    record_lock: threading.Lock = field(default_factory=threading.Lock)
    stopping: bool = False  # 正在停止（保存录到的步骤）
    stopped_at: float = float("-inf")  # 上次停止录制完成的时间（time.monotonic）

    @property
    def user_data(self) -> Path:
        return self.app.user_data.resolve()

    @property
    def root(self) -> Path:
        return self.user_data / "platforms"

    @property
    def drafts(self) -> Path:
        return self.user_data / "drafts"

    # ---- 脚本 ----

    def script_path(self, script_id: str) -> Path:
        root, rel = (self.drafts, script_id[len(DRAFT):]) if script_id.startswith(DRAFT) else (self.root, script_id)
        path = (root / rel).resolve()
        if not path.is_relative_to(root.resolve()) or path.suffix != ".yaml":
            raise HTTPException(400, "非法的脚本路径")
        if not path.exists():
            raise HTTPException(404, f"脚本不存在：{script_id}")
        return path

    def script_id(self, path: Path) -> str:
        path = path.resolve()
        if path.is_relative_to(self.drafts.resolve()):
            return DRAFT + path.relative_to(self.drafts.resolve()).as_posix()
        return path.relative_to(self.root.resolve()).as_posix()

    def list_scripts(self) -> list[dict]:
        out = []
        for root, draft in ((self.drafts, True), (self.root, False)):
            for p in sorted(root.glob("*/templates/*.yaml")):
                if p.name.endswith(".pages.yaml"):  # 宏自己的页面锚点文件，不是宏
                    continue
                out.append({"id": self.script_id(p), "platform": p.parent.parent.name,
                            "name": _display_name(p), "draft": draft})
        return out

    def unique_stem(self, platform: str, stem: str) -> str:
        """在宏库和草稿里都不重名。"""
        taken = lambda s: any((r / platform / "templates" / f"{s}.yaml").exists()  # noqa: E731
                              for r in (self.root, self.drafts))
        if stem.endswith(".pages"):  # 会和页面文件同名
            stem += "_"
        out, n = stem, 2
        while taken(out):
            out, n = f"{stem}-{n}", n + 1
        return out

    def commit(self, script_id: str) -> str:
        """把草稿搬进宏库（连同参考图、页面锚点），返回新的 id。"""
        src = self.script_path(script_id)
        if not script_id.startswith(DRAFT):
            raise HTTPException(400, "这个宏已经在宏库里了")
        platform = src.parent.parent.name
        dst_dir = self.root / platform / "templates"
        stem = src.stem
        while (dst_dir / f"{stem}.yaml").exists():  # 草稿期间别处建了同名宏
            stem = self.unique_stem(platform, src.stem)
        dst = relocate_macro(src, dst_dir, stem)
        new_id = self.script_id(dst)
        if script_id in self.latest:
            self.latest[new_id] = self.latest.pop(script_id)
        return new_id

    def delete(self, script_id: str) -> None:
        """删除宏及它自己的参考图、页面锚点（共用的页面文件不动）。"""
        path = self.script_path(script_id)
        _remove_macro_files(path)
        self.latest.pop(script_id, None)

    def load(self, script_id: str) -> Script:
        return Script.load(self.script_path(script_id))

    def collect_garbage(self, now: float | None = None) -> int:
        """所有宏（含草稿）各清一遍没人用的旧图，返回删了几个文件。studio 启动时调用。
        还会清掉上次留下的「最新截图」（只记在内存里，重启后没人再读它们；删掉的宏、用不到的窗口的也一直留着），
        以及宏全删光后剩下的空平台目录（里面一个文件都没有才删）。"""
        removed = 0
        for root in (self.drafts, self.root):
            for p in root.glob("*/templates/*.yaml"):
                if not p.name.endswith(".pages.yaml"):
                    try:
                        removed += collect_garbage(p, now=now)
                    except Exception:  # noqa: BLE001 —— 一个宏读不出来不影响启动和别的宏
                        continue
        if not self.latest:
            for f in (self.user_data / "frames").glob("*.png"):
                f.unlink(missing_ok=True)
                removed += 1
        for root in (self.drafts, self.root):
            for d in root.glob("*") if root.is_dir() else []:
                if d.is_dir() and not any(f.is_file() for f in d.rglob("*")):
                    shutil.rmtree(d, ignore_errors=True)
        return removed

    def set_latest(self, script_id: str, key: str | None, img, scale: float) -> None:
        """记下某个窗口的「最新截图」：每个窗口各一张，截别的窗口不会把它覆盖掉。"""
        key = key or MAIN
        name = script_id.replace("/", "__").removesuffix(".yaml") + ("" if key == MAIN else f"__{key}")
        out = self.user_data / "frames" / f"{name}.png"
        write_image(out, img)
        self.latest.setdefault(script_id, {})[key] = Frame(out, scale, None if key == MAIN else key)

    # ---- 平台包 ----

    def export_pack(self, platform: str, *, screenshots: bool = True) -> bytes:
        """宏库里的一个平台 → .cwpack（草稿不算）。"""
        path = (self.root / platform).resolve()
        if not path.is_relative_to(self.root.resolve()) or path == self.root.resolve():
            raise HTTPException(400, "非法的平台名")
        if not any(pack.is_macro(p.relative_to(path).as_posix()) for p in path.glob("templates/*.yaml")):
            raise HTTPException(404, f"平台 {platform} 里还没有保存的宏")
        return pack.export_pack(path, screenshots=screenshots)

    def import_pack(self, data: bytes) -> dict:
        """导入 .cwpack：先解压到临时目录，再一个个宏搬进宏库，重名的自动加 -2，不问用户。"""
        self.user_data.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".import-", dir=self.user_data))
        try:
            try:
                manifest = pack.unpack(data, staging)
            except pack.PackError as e:
                raise HTTPException(400, str(e)) from None
            platform = manifest.platform
            dst = self.root / platform
            ids = []
            for src in sorted(staging.glob("templates/*.yaml")):
                if src.name.endswith(".pages.yaml"):
                    continue
                stem = self.unique_stem(platform, src.stem)
                ids.append(self.script_id(relocate_macro(src, dst / "templates", stem)))
            # 宏自己的文件已经跟着搬走了；剩下的是平台级文件，本机已有的不覆盖
            for p in sorted(staging.rglob("*")):
                target = dst / p.relative_to(staging)
                if p.is_file() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return {"platform": platform, "ids": ids}

    def file_url(self, path: Path | None) -> str | None:
        if path is None or not path.exists():
            return None
        try:
            rel = path.resolve().relative_to(self.user_data).as_posix()
        except ValueError:  # 不在 user_data 里（数据目录搬过家）：这一张不显示，别让整个页面报错
            return None
        return f"/api/file?path={quote(rel)}&v={path.stat().st_mtime_ns}"

    def describe(self, script_id: str) -> dict:
        """前端需要的一切：脚本、页面锚点、参考图、问题列表。"""
        script = self.load(script_id)
        if script.renumber():  # 以前的宏延迟也占「步」号：打开时统一改成 动作 s01…、延迟 d01…
            script.dump(self.script_path(script_id))
        book = PageBook.load(script.pages_path) if script.pages_path else PageBook()
        pages = {
            name: [
                {**a.model_dump(), "url": self.file_url(script.pages_path.parent / a.image)}
                for a in fp.anchors
            ]
            for name, fp in book.pages.items()
        }
        # 画面：每个窗口各有参考图和最新截图，键是窗口键（主窗口 = "main"）
        refs = {}
        for key, win in [(MAIN, script.window), *script.windows.items()]:
            ref = script.shown_reference(key)
            if ref is not None and ref.exists():
                refs[key] = {"url": self.file_url(ref), "scale": win.scale}
        latest = {key: {"url": self.file_url(f.path), "scale": f.scale}
                  for key, f in self.latest.get(script_id, {}).items() if f.path.exists()}
        return {
            "id": script_id,
            "draft": script_id.startswith(DRAFT),
            "script": script.model_dump(mode="json"),
            "pages": pages,
            "frames": {"reference": refs, "latest": latest},
            # 每一步录制时的画面：步骤编号 → 图（选中这一步时画布显示它）
            "shots": {
                s.id: {"url": self.file_url(script.base_dir / s.shot), "scale": script.window_for(s.win).scale}
                for s in script.steps if s.shot and (script.base_dir / s.shot).exists()
            },
            "problems": problems(script, {name: {a.win for a in fp.anchors} for name, fp in book.pages.items()}),
            # 按图找的目标图：步骤编号 → 图片地址（步骤参数里显示缩略图）
            "targets": {
                s.id: self.file_url(script.base_dir / s.target.image)
                for s in script.steps if isinstance(s, (Click, Hover)) and s.target is not None
            },
        }


def _own_files(path: Path, script: Script) -> tuple[Path | None, Path | None, Path | None]:
    """宏自己的参考图、页面文件、锚点目录（页面文件是平台共用的就返回 None）。"""
    ref = script.reference_path
    own_pages = script.pages == default_pages_ref(path)
    pages = script.pages_path if own_pages else None
    anchors = path.parent / anchors_dir(path) if own_pages else None
    return ref, pages, anchors


GARBAGE_AGE = 3 * 24 * 3600  # 秒：没人用的旧图过了这么久才删（期间 Ctrl+Z 还能用上）


def collect_garbage(path: Path, *, now: float | None = None, min_age: float = GARBAGE_AGE) -> int:
    """删掉这个宏目录里没有任何地方引用、而且很久没动过的图片：重新录制、替换录制画面时为了能撤销留下的旧图
    （s01-2.png、main-3.png…），以及录制清出去很久的页面和它们的锚点图。刚产生的不删，撤销还要用。返回删了几个文件。"""
    import time

    now = time.time() if now is None else now
    script = Script.load(path)
    base = path.parent
    refs = {script.reference, *(w.reference for w in script.windows.values()), *(s.shot for s in script.steps)}
    refs |= {img for s in script.steps if isinstance(s, (Click, Hover)) and s.target is not None
             for img in s.target.images()}
    _, pages, anchors = _own_files(path, script)
    dirs = [base / targets_dir(path), base / refs_dir(path), base / shots_dir(path)]
    if pages is not None and pages.exists():
        book = PageBook.load(pages)
        old = [n for n, t in book.retired_at.items() if now - t >= min_age]
        old += [n for n in book.retired if n not in book.retired_at]
        for name in old:
            book.retired.pop(name, None)
            book.retired_at.pop(name, None)
        if old:
            book.dump(pages)
        refs |= {a.image for fp in [*book.pages.values(), *book.retired.values()] for a in fp.anchors}
        dirs.append(anchors)
    keep = {(base / r).resolve() for r in refs if r}
    files = [f for d in dirs if d.is_dir() for f in d.glob("*.png")]
    files += [path.with_suffix(".png")] if path.with_suffix(".png").exists() else []
    removed = 0
    for f in files:
        if f.resolve() not in keep and now - f.stat().st_mtime >= min_age:
            f.unlink(missing_ok=True)
            removed += 1
    return removed


def _remove_macro_files(path: Path) -> None:
    script = Script.load(path)
    ref, pages, anchors = _own_files(path, script)
    # <宏名>.png 也删：参考图换过（存到 .refs/ 里）以后，它就不再是 script.reference 了
    for f in (ref, pages, path.with_suffix(".png")):
        if f is not None:
            f.unlink(missing_ok=True)
    for d in (anchors, path.parent / targets_dir(path), path.parent / refs_dir(path), path.parent / shots_dir(path)):
        if d is not None and d.exists():
            shutil.rmtree(d)
    path.unlink()


def relocate_macro(src: Path, dst_dir: Path, stem: str) -> Path:
    """把宏连同自己的参考图、页面锚点搬到 dst_dir 下、改名为 stem，修正文件里的引用。"""
    from cyber_worker.macro.vision.fingerprint import PageBook

    script = Script.load(src)
    script.name = renamed(script.name, src.stem, stem)
    ref, pages, anchors = _own_files(src, script)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{stem}.yaml"
    if ref is not None and ref.exists():
        shutil.move(ref, dst_dir / f"{stem}{ref.suffix}")
        script.reference = f"{stem}{ref.suffix}"
    if pages is not None and pages.exists():
        book = PageBook.load(pages)
        old_dir, new_dir = anchors_dir(src), anchors_dir(dst)
        if anchors is not None and anchors.exists():
            shutil.move(anchors, dst_dir / new_dir)
        for fp in [*book.pages.values(), *book.retired.values()]:
            for a in fp.anchors:
                if a.image.startswith(old_dir + "/"):
                    a.image = new_dir + a.image[len(old_dir):]
        book.dump(dst_dir / default_pages_ref(dst))
        pages.unlink()
        script.pages = default_pages_ref(dst)
    old_r, new_r = refs_dir(src), refs_dir(dst)
    if (src.parent / old_r).exists():
        shutil.move(src.parent / old_r, dst_dir / new_r)
    for win in script.windows.values():
        if win.reference and win.reference.startswith(old_r + "/"):
            win.reference = new_r + win.reference[len(old_r):]
    old_s, new_s = shots_dir(src), shots_dir(dst)
    if (src.parent / old_s).exists():
        shutil.move(src.parent / old_s, dst_dir / new_s)
    for step in script.steps:
        if step.shot and step.shot.startswith(old_s + "/"):
            step.shot = new_s + step.shot[len(old_s):]
    old_t, new_t = targets_dir(src), targets_dir(dst)
    if (src.parent / old_t).exists():
        shutil.move(src.parent / old_t, dst_dir / new_t)
    for step in script.steps:  # 点击和悬停都可能有目标图，两种样子都要改路径
        t = step.target if isinstance(step, (Click, Hover)) else None
        if t is not None:
            t.image = new_t + t.image[len(old_t):] if t.image.startswith(old_t + "/") else t.image
            t.alt = new_t + t.alt[len(old_t):] if t.alt and t.alt.startswith(old_t + "/") else t.alt
    script.dump(dst)
    src.unlink()
    return dst


def _display_name(path: Path) -> str:
    """列表里显示宏名（脚本里的 name，可能是中文），读不出来就用文件名。"""
    try:
        return Script.load(path).name
    except Exception:  # noqa: BLE001 —— 坏文件也要能在列表里看到并删除
        return path.stem


OFF_TARGET_MARGIN = 24  # 点，差不多半个按钮


def off_target(step: Step) -> bool:
    """按图找的点击 / 悬停：点击位置离目标框太远。执行时找到目标后按录制时的相对位置点，会点到目标外面。"""
    if not isinstance(step, (Click, Hover)) or not step.finds_target:
        return False
    t, m = step.target, OFF_TARGET_MARGIN
    return not (t.x - m <= step.x <= t.x + t.w + m and t.y - m <= step.y <= t.y + t.h + m)


def problems(script: Script, pages: dict[str, set[str | None]]) -> list[dict]:
    """执行前就能发现的问题；runner 遇到这些也会拒绝执行。pages：页面名 → 它的锚点截自哪些窗口（None = 主窗口）。"""
    out = []
    seen: set[str] = set()
    for step in script.steps:
        def add(msg: str) -> None:
            out.append({"step": step.id, "message": msg})

        if step.id in seen:
            add("步骤编号重复")
        seen.add(step.id)
        if needs_page_check(step) and not step.expected_page:
            add("还没设「在哪个画面执行」：选中这一步，点「自动选一个」或「我自己框」")
        if step.expected_page and step.expected_page not in pages:
            add(f"页面 {step.expected_page} 还没有锚点")
        elif step.expected_page and step.win not in pages[step.expected_page]:
            add(f"页面 {step.expected_page} 的锚点截自别的窗口，在这一步的窗口里永远对不上：换一个页面，或在这个窗口里框一个锚点")
        if off_target(step):
            add("点击位置不在目标图上，执行时会点到目标外面：把编号标记拖回目标框里")
        if isinstance(step, TypeText) and step.typed_with_ime:
            add(f"录制时开着中文输入法，录到的只是按键「{step.text}」：请改成实际要输入的文字")
        if isinstance(step, TypeText):
            for name in _VAR_USE.findall(step.text):
                if name not in script.variables:
                    add(f"{{{name}}} 没有声明为变量，会按原文输入")
        if isinstance(step, WaitPage):
            if not step.page:
                add("选一个要等的页面")
            elif step.page not in pages:
                add(f"页面 {step.page} 还没有锚点")
            elif step.win not in pages[step.page]:
                add(f"页面 {step.page} 的锚点截自别的窗口，在这一步的窗口里永远对不上：换一个页面，或在这个窗口里框一个锚点")
    return out


def next_step_number(steps: list[Step]) -> int:
    nums = [int(s.id[1:]) for s in steps if s.id[:1] == "s" and s.id[1:].isdigit()]
    return max(nums, default=0) + 1
