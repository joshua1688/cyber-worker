"""studio 的共享状态：流程目录、最新截图帧、录制会话。路由只通过这里访问文件系统。"""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from cyber_worker.adapters import pack
from cyber_worker.bootstrap import App
from cyber_worker.flow.anchors import (
    anchors_dir,
    default_pages_ref,
    refs_dir,
    shots_dir,
    targets_dir,
)
from cyber_worker.flow.io.base import stable_title
from cyber_worker.flow.recorder import RecordingBuilder
from cyber_worker.flow.schema import (
    MAIN,
    Click,
    Flow,
    Hover,
    Step,
    TypeText,
    Wait,
    WaitPage,
    needs_page_check,
)
from cyber_worker.flow.screenshot.storage import write_image
from cyber_worker.flow.vision.fingerprint import PageBook
from cyber_worker.studio.ws import Hub

_NAME = re.compile(r"^[\w\-]+$")
LEGACY_DIR = "templates"  # 流程目录以前的名字
DRAFT = "_draft/"  # 草稿流程 id 的前缀
_VAR_USE = re.compile(r"\{(\w+)\}")


def check_name(value: str, what: str) -> str:
    if not _NAME.match(value):
        raise HTTPException(400, f"{what}只能包含文字、数字、下划线和连字符")
    return value


# 文件名里不能出现的字符（Windows 最严），以及会破坏 URL 的字符
_UNSAFE = re.compile(r'[\\/:*?"<>|#%&\s]+')


def safe_stem(name: str) -> str:
    """用户起的流程名（可以是中文、带空格）→ 文件名。"""
    stem = _UNSAFE.sub("_", name.strip()).strip("._")
    return stem[:60] or "flow"


# 「文档名 - 程序名」这类标题：前半截跟着打开的文件、网页变，按它认窗口以后会对不上
_CHANGING_TITLE = re.compile(r" [-–—|] ")


def window_json(w, shared_apps: set[str]) -> dict:
    """窗口 → 前端需要的一切，包括替用户想好的流程名和用来认窗口的标题。"""
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
    flow_id: str
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
    latest: dict[str, dict[str, Frame]] = field(default_factory=dict)  # 流程 → 窗口键 → 这个窗口最新的一帧
    recording: Recording | None = None
    current_id: str | None = None  # 界面上当前打开的流程：按录制 / 空跑快捷键时用的就是它
    current_vars: dict[str, str] = field(default_factory=dict)  # 界面上给它填的变量（空跑快捷键用）
    # 报告当前流程的那个页面（每个标签页一个随机编号）、它有没有没保存的修改：
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

    # ---- 流程 ----

    def flow_path(self, flow_id: str) -> Path:
        flow_id = flow_id.replace(f"/{LEGACY_DIR}/", "/flows/")  # 改名以前的链接（qq/templates/群聊天.yaml）
        root, rel = (self.drafts, flow_id[len(DRAFT):]) if flow_id.startswith(DRAFT) else (self.root, flow_id)
        path = (root / rel).resolve()
        if not path.is_relative_to(root.resolve()) or path.suffix != ".yaml":
            raise HTTPException(400, "非法的流程路径")
        if not path.exists():
            raise HTTPException(404, f"流程不存在：{flow_id}")
        return path

    def flow_id(self, path: Path) -> str:
        path = path.resolve()
        if path.is_relative_to(self.drafts.resolve()):
            return DRAFT + path.relative_to(self.drafts.resolve()).as_posix()
        return path.relative_to(self.root.resolve()).as_posix()

    def list_flows(self) -> list[dict]:
        out = []
        for root, draft in ((self.drafts, True), (self.root, False)):
            for p in sorted(root.glob("*/flows/*.yaml")):
                if p.name.endswith(".pages.yaml"):  # 流程自己的页面锚点文件，不是流程
                    continue
                out.append({"id": self.flow_id(p), "platform": p.parent.parent.name,
                            "name": _display_name(p), "draft": draft})
        return out

    def update_meta(self, flow_id: str, *, name: str | None = None, description: str | None = None) -> dict:
        """应用首页卡片上改名、写说明。名字和同一个应用里的别的流程重了就加 -2（和新建时一样，不弹窗），
        改了名把以前的审计记录也改过来。返回改完的名字和说明。"""
        path = self.flow_path(flow_id)
        flow = Flow.load(path)
        if description is not None:
            flow.description = description.strip()
        if name is not None and name.strip() and name.strip() != flow.name:
            taken = {_display_name(p) for p in path.parent.glob("*.yaml")
                     if p != path and not p.name.endswith(".pages.yaml")}
            new, n = name.strip(), 2
            while new in taken:
                new, n = f"{name.strip()}-{n}", n + 1
            self.rename_runs(flow.name, new, path.parent.parent.name)
            flow.name = new
        flow.dump(path)
        return {"name": flow.name, "description": flow.description}

    def rename_runs(self, old: str, new: str, platform: str | None = None) -> None:
        """流程改名后，审计里以前的记录跟着改名（只改这个应用的）。"""
        if old != new and hasattr(self.app.audit, "rename_flow"):
            self.app.audit.rename_flow(old, new, platform)

    def _library(self) -> list[tuple[str, str, list[tuple[Path, Flow]]]]:
        """流程库里的每个应用：(平台目录名, 显示名, [(流程文件, 流程)])。显示名是主窗口标题里最常见的那个
        （Windows PowerShell），没有标题用程序名，一个流程都没有用目录名。读不出来的流程跳过，别的照样显示。"""
        out = []
        for d in sorted(p for p in self.root.glob("*") if p.is_dir()):
            flows, titles = [], Counter()
            for p in sorted(d.glob("flows/*.yaml")):
                if p.name.endswith(".pages.yaml"):
                    continue
                try:
                    s = Flow.load(p)
                except Exception:  # noqa: BLE001, S112 —— 一个流程读不出来，别的照样显示
                    continue
                titles[s.window.title or s.window.app] += 1
                flows.append((p, s))
            out.append((d.name, titles.most_common(1)[0][0] if titles else d.name, flows))
        return out

    @staticmethod
    def _owners(library) -> dict[str, str]:
        """流程名 → 应用，只收只在一个应用里出现的名字：没记应用的旧审计记录靠它认应用，重名的不猜。"""
        seen: dict[str, set[str]] = {}
        for platform, _, flows in library:
            for _, s in flows:
                seen.setdefault(s.name, set()).add(platform)
        return {name: next(iter(ps)) for name, ps in seen.items() if len(ps) == 1}

    def audit_apps(self) -> tuple[dict[str, str], dict[tuple[str, str], str], dict[str, str]]:
        """审计页要的：应用显示名 {平台: 名字}、流程 id {(平台, 流程名): id}（「去修改」用）、旧记录认应用用的 {流程名: 平台}。"""
        library = self._library()
        titles = {platform: title for platform, title, _ in library}
        ids = {(platform, s.name): self.flow_id(p) for platform, _, flows in library for p, s in flows}
        return titles, ids, self._owners(library)

    def apps(self, today: date) -> list[dict]:
        """应用总览 / 应用首页要的东西：每个应用（流程库里的一个平台目录）和它的流程，流程带上说明、几步、大概多久、
        最近一次运行的结果（卡片上只放要紧的：变量、窗口这些细节进了编辑器再看）；应用带上今天运行了几次、几次出了问题。
        运行记录按（应用, 流程名）对；没记应用的旧记录按流程名认（名字只在一个应用里有才算）。草稿不算：打开就会存进流程库。"""
        library = self._library()
        owners = self._owners(library)
        runs = self.app.audit.runs() if hasattr(self.app.audit, "runs") else []
        for r in runs:
            r["platform"] = r.get("platform") or owners.get(r["flow"])
        last = {(r["platform"], r["flow"]): r for r in runs}  # 按先后排的，后面的覆盖前面的：留下的就是最近一次
        today_runs = [r for r in runs if r["at"][:10] == today.isoformat()]
        out = []
        for platform, title, entries in library:
            flows = []
            for p, s in entries:
                run = last.get((platform, s.name))
                flows.append({
                    "id": self.flow_id(p), "name": s.name, "description": s.description,
                    "steps": sum(not isinstance(st, Wait) for st in s.steps),
                    "seconds": round(sum(st.seconds if isinstance(st, Wait) else st.delay_after for st in s.steps), 1),
                    "last": run and {k: run[k] for k in ("at", "dry_run", "bad", "step_id", "detail")},
                })
            mine = [r for r in today_runs if r["platform"] == platform]
            runs_at = [m["last"]["at"] for m in flows if m["last"]]
            out.append({"platform": platform, "title": title,
                        "flows": flows, "last_at": max(runs_at, default=None),  # 「最近用过」排序用
                        "today": {"runs": len(mine), "bad": sum(r["bad"] for r in mine)}})
        return out

    def unique_stem(self, platform: str, stem: str) -> str:
        """在流程库和草稿里都不重名。"""
        taken = lambda s: any((r / platform / "flows" / f"{s}.yaml").exists()  # noqa: E731
                              for r in (self.root, self.drafts))
        if stem.endswith(".pages"):  # 会和页面文件同名
            stem += "_"
        out, n = stem, 2
        while taken(out):
            out, n = f"{stem}-{n}", n + 1
        return out

    def commit(self, flow_id: str) -> str:
        """把草稿搬进流程库（连同参考图、页面锚点），返回新的 id。"""
        src = self.flow_path(flow_id)
        if not flow_id.startswith(DRAFT):
            raise HTTPException(400, "这个流程已经在流程库里了")
        platform = src.parent.parent.name
        dst_dir = self.root / platform / "flows"
        stem = src.stem
        while (dst_dir / f"{stem}.yaml").exists():  # 草稿期间别处建了同名流程
            stem = self.unique_stem(platform, src.stem)
        dst = relocate_flow(src, dst_dir, stem)
        new_id = self.flow_id(dst)
        if flow_id in self.latest:
            self.latest[new_id] = self.latest.pop(flow_id)
        return new_id

    def delete(self, flow_id: str) -> None:
        """删除流程及它自己的参考图、页面锚点（共用的页面文件不动）。"""
        path = self.flow_path(flow_id)
        _remove_flow_files(path)
        self.latest.pop(flow_id, None)

    # ---- 删除应用：整个挪进回收站，能撤销 ----

    @property
    def trash(self) -> Path:
        return self.user_data / ".trash"

    def app_dir(self, platform: str) -> Path:
        d = (self.root / platform).resolve()
        if d.parent != self.root.resolve() or not d.is_dir():
            raise HTTPException(404, "这个应用已经不在了")
        return d

    def delete_app(self, platform: str, now: datetime | None = None) -> dict:
        """删除一个应用（它的全部流程、锚点、截图）：整个目录挪进 user_data/.trash/<平台>-<时间>，
        撤销就原样挪回来；过了 TRASH_DAYS 天的在 studio 启动时清掉。审计记录不动。"""
        d = self.app_dir(platform)
        if self.recording and self.flow_path(self.recording.flow_id).is_relative_to(d):
            raise HTTPException(409, "正在录制这个应用里的流程，先停止录制")
        if self.app.executor.busy:
            raise HTTPException(409, "正在执行，稍后再删")
        flows = [p for p in d.glob("flows/*.yaml") if not p.name.endswith(".pages.yaml")]
        name = f"{platform}-{(now or datetime.now()):%Y%m%d-%H%M%S}"
        self.trash.mkdir(parents=True, exist_ok=True)
        shutil.move(str(d), str(self.trash / name))
        for sid in [k for k in self.latest if k.startswith(f"{platform}/")]:
            self.latest.pop(sid, None)
        return {"trash": name, "platform": platform, "flows": len(flows)}

    def restore_app(self, trash: str) -> str:
        """撤销删除：把回收站里的应用挪回流程库。流程库里已经又有同名的应用了就不覆盖。返回平台名。"""
        src = (self.trash / trash).resolve()
        if src.parent != self.trash.resolve() or not src.is_dir():
            raise HTTPException(404, "回收站里没有这个应用了")
        platform = _TRASH_SUFFIX.sub("", trash)
        dst = self.root / platform
        if dst.exists():
            raise HTTPException(409, "已经又有一个同名的应用了，没法放回去")
        self.root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return platform

    def _purge_trash(self, now: datetime) -> None:
        """回收站里放了超过 TRASH_DAYS 天的应用真正删掉（看目录名里的时间）。"""
        for d in self.trash.glob("*") if self.trash.is_dir() else []:
            m = _TRASH_SUFFIX.search(d.name)
            when = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S") if m else None
            if when is None or (now - when).days >= TRASH_DAYS:
                shutil.rmtree(d, ignore_errors=True)

    def load(self, flow_id: str) -> Flow:
        return Flow.load(self.flow_path(flow_id))

    def migrate_layout(self) -> int:
        """「宏」改叫「流程」以前，每个应用的流程放在 templates/ 下：挪成 flows/（宏库、草稿、回收站里的都挪）。
        flows/ 已经有了就一个个挪过去，同名的留在 templates/ 里不覆盖。返回挪了几个目录。studio 启动时调用。"""
        moved = 0
        for root in (self.drafts, self.root, self.trash):
            for old in root.glob(f"*/{LEGACY_DIR}") if root.is_dir() else []:
                new = old.parent / "flows"
                if not new.exists():
                    old.rename(new)
                else:
                    for child in old.iterdir():
                        if not (new / child.name).exists():
                            child.rename(new / child.name)
                    if not any(old.iterdir()):
                        old.rmdir()
                moved += 1
        return moved

    def collect_garbage(self, now: float | None = None) -> int:
        """所有流程（含草稿）各清一遍没人用的旧图，返回删了几个文件。studio 启动时调用。
        还会清掉上次留下的「最新截图」（只记在内存里，重启后没人再读它们；删掉的流程、用不到的窗口的也一直留着），
        以及流程全删光后剩下的空平台目录（里面一个文件都没有才删）。"""
        removed = 0
        for root in (self.drafts, self.root):
            for p in root.glob("*/flows/*.yaml"):
                if not p.name.endswith(".pages.yaml"):
                    try:
                        removed += collect_garbage(p, now=now)
                    except Exception:  # noqa: BLE001 —— 一个流程读不出来不影响启动和别的流程
                        continue
        if not self.latest:
            for f in (self.user_data / "frames").glob("*.png"):
                f.unlink(missing_ok=True)
                removed += 1
        for root in (self.drafts, self.root):
            for d in root.glob("*") if root.is_dir() else []:
                if d.is_dir() and not any(f.is_file() for f in d.rglob("*")):
                    shutil.rmtree(d, ignore_errors=True)
        self._purge_trash(datetime.fromtimestamp(now) if now is not None else datetime.now())
        return removed

    def set_latest(self, flow_id: str, key: str | None, img, scale: float) -> None:
        """记下某个窗口的「最新截图」：每个窗口各一张，截别的窗口不会把它覆盖掉。"""
        key = key or MAIN
        name = flow_id.replace("/", "__").removesuffix(".yaml") + ("" if key == MAIN else f"__{key}")
        out = self.user_data / "frames" / f"{name}.png"
        write_image(out, img)
        self.latest.setdefault(flow_id, {})[key] = Frame(out, scale, None if key == MAIN else key)

    # ---- 平台包 ----

    def export_pack(self, platform: str, *, screenshots: bool = True) -> bytes:
        """流程库里的一个平台 → .cwpack（草稿不算）。"""
        path = (self.root / platform).resolve()
        if not path.is_relative_to(self.root.resolve()) or path == self.root.resolve():
            raise HTTPException(400, "非法的平台名")
        if not any(pack.is_flow(p.relative_to(path).as_posix()) for p in path.glob("flows/*.yaml")):
            raise HTTPException(404, f"平台 {platform} 里还没有保存的流程")
        return pack.export_pack(path, screenshots=screenshots)

    def import_pack(self, data: bytes) -> dict:
        """导入 .cwpack：先解压到临时目录，再一个个流程搬进流程库，重名的自动加 -2，不问用户。"""
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
            for src in sorted(staging.glob("flows/*.yaml")):
                if src.name.endswith(".pages.yaml"):
                    continue
                stem = self.unique_stem(platform, src.stem)
                ids.append(self.flow_id(relocate_flow(src, dst / "flows", stem)))
            # 流程自己的文件已经跟着搬走了；剩下的是平台级文件，本机已有的不覆盖
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

    def describe(self, flow_id: str) -> dict:
        """前端需要的一切：流程、页面锚点、参考图、问题列表。"""
        flow = self.load(flow_id)
        if flow.renumber():  # 以前的流程延迟也占「步」号：打开时统一改成 动作 s01…、延迟 d01…
            flow.dump(self.flow_path(flow_id))
        book = PageBook.load(flow.pages_path) if flow.pages_path else PageBook()
        pages = {
            name: [
                {**a.model_dump(), "url": self.file_url(flow.pages_path.parent / a.image)}
                for a in fp.anchors
            ]
            for name, fp in book.pages.items()
        }
        # 画面：每个窗口各有参考图和最新截图，键是窗口键（主窗口 = "main"）
        refs = {}
        for key, win in [(MAIN, flow.window), *flow.windows.items()]:
            ref = flow.shown_reference(key)
            if ref is not None and ref.exists():
                refs[key] = {"url": self.file_url(ref), "scale": win.scale}
        latest = {key: {"url": self.file_url(f.path), "scale": f.scale}
                  for key, f in self.latest.get(flow_id, {}).items() if f.path.exists()}
        return {
            "id": flow_id,
            "draft": flow_id.startswith(DRAFT),
            "flow": flow.model_dump(mode="json"),
            "pages": pages,
            "frames": {"reference": refs, "latest": latest},
            # 每一步录制时的画面：步骤编号 → 图（选中这一步时画布显示它）
            "shots": {
                s.id: {"url": self.file_url(flow.base_dir / s.shot), "scale": flow.window_for(s.win).scale}
                for s in flow.steps if s.shot and (flow.base_dir / s.shot).exists()
            },
            "problems": problems(flow, {name: {a.win for a in fp.anchors} for name, fp in book.pages.items()}),
            # 按图找的目标图：步骤编号 → 图片地址（步骤参数里显示缩略图）
            "targets": {
                s.id: self.file_url(flow.base_dir / s.target.image)
                for s in flow.steps if isinstance(s, (Click, Hover)) and s.target is not None
            },
        }


def _own_files(path: Path, flow: Flow) -> tuple[Path | None, Path | None, Path | None]:
    """流程自己的参考图、页面文件、锚点目录（页面文件是平台共用的就返回 None）。"""
    ref = flow.reference_path
    own_pages = flow.pages == default_pages_ref(path)
    pages = flow.pages_path if own_pages else None
    anchors = path.parent / anchors_dir(path) if own_pages else None
    return ref, pages, anchors


TRASH_DAYS = 7  # 删除的应用在回收站里留这么多天，期间能撤销
_TRASH_SUFFIX = re.compile(r"-(\d{8}-\d{6})$")  # 回收站目录名：<平台>-<删除时间>
GARBAGE_AGE = 3 * 24 * 3600  # 秒：没人用的旧图过了这么久才删（期间 Ctrl+Z 还能用上）


def collect_garbage(path: Path, *, now: float | None = None, min_age: float = GARBAGE_AGE) -> int:
    """删掉这个流程目录里没有任何地方引用、而且很久没动过的图片：重新录制、替换录制画面时为了能撤销留下的旧图
    （s01-2.png、main-3.png…），以及录制清出去很久的页面和它们的锚点图。刚产生的不删，撤销还要用。返回删了几个文件。"""
    import time

    now = time.time() if now is None else now
    flow = Flow.load(path)
    base = path.parent
    refs = {flow.reference, *(w.reference for w in flow.windows.values()), *(s.shot for s in flow.steps)}
    refs |= {img for s in flow.steps if isinstance(s, (Click, Hover)) and s.target is not None
             for img in s.target.images()}
    _, pages, anchors = _own_files(path, flow)
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


def _remove_flow_files(path: Path) -> None:
    flow = Flow.load(path)
    ref, pages, anchors = _own_files(path, flow)
    # <流程名>.png 也删：参考图换过（存到 .refs/ 里）以后，它就不再是 flow.reference 了
    for f in (ref, pages, path.with_suffix(".png")):
        if f is not None:
            f.unlink(missing_ok=True)
    for d in (anchors, path.parent / targets_dir(path), path.parent / refs_dir(path), path.parent / shots_dir(path)):
        if d is not None and d.exists():
            shutil.rmtree(d)
    path.unlink()


def relocate_flow(src: Path, dst_dir: Path, stem: str) -> Path:
    """把流程连同自己的参考图、页面锚点搬到 dst_dir 下、改名为 stem，修正文件里的引用。"""
    from cyber_worker.flow.vision.fingerprint import PageBook

    flow = Flow.load(src)
    flow.name = renamed(flow.name, src.stem, stem)
    ref, pages, anchors = _own_files(src, flow)
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"{stem}.yaml"
    if ref is not None and ref.exists():
        shutil.move(ref, dst_dir / f"{stem}{ref.suffix}")
        flow.reference = f"{stem}{ref.suffix}"
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
        flow.pages = default_pages_ref(dst)
    old_r, new_r = refs_dir(src), refs_dir(dst)
    if (src.parent / old_r).exists():
        shutil.move(src.parent / old_r, dst_dir / new_r)
    for win in flow.windows.values():
        if win.reference and win.reference.startswith(old_r + "/"):
            win.reference = new_r + win.reference[len(old_r):]
    old_s, new_s = shots_dir(src), shots_dir(dst)
    if (src.parent / old_s).exists():
        shutil.move(src.parent / old_s, dst_dir / new_s)
    for step in flow.steps:
        if step.shot and step.shot.startswith(old_s + "/"):
            step.shot = new_s + step.shot[len(old_s):]
    old_t, new_t = targets_dir(src), targets_dir(dst)
    if (src.parent / old_t).exists():
        shutil.move(src.parent / old_t, dst_dir / new_t)
    for step in flow.steps:  # 点击和悬停都可能有目标图，两种样子都要改路径
        t = step.target if isinstance(step, (Click, Hover)) else None
        if t is not None:
            t.image = new_t + t.image[len(old_t):] if t.image.startswith(old_t + "/") else t.image
            t.alt = new_t + t.alt[len(old_t):] if t.alt and t.alt.startswith(old_t + "/") else t.alt
    flow.dump(dst)
    src.unlink()
    return dst


def _display_name(path: Path) -> str:
    """列表里显示流程名（流程里的 name，可能是中文），读不出来就用文件名。"""
    try:
        return Flow.load(path).name
    except Exception:  # noqa: BLE001 —— 坏文件也要能在列表里看到并删除
        return path.stem


OFF_TARGET_MARGIN = 24  # 点，差不多半个按钮


def off_target(step: Step) -> bool:
    """按图找的点击 / 悬停：点击位置离目标框太远。执行时找到目标后按录制时的相对位置点，会点到目标外面。"""
    if not isinstance(step, (Click, Hover)) or not step.finds_target:
        return False
    t, m = step.target, OFF_TARGET_MARGIN
    return not (t.x - m <= step.x <= t.x + t.w + m and t.y - m <= step.y <= t.y + t.h + m)


def problems(flow: Flow, pages: dict[str, set[str | None]]) -> list[dict]:
    """执行前就能发现的问题；runner 遇到这些也会拒绝执行。pages：页面名 → 它的锚点截自哪些窗口（None = 主窗口）。"""
    out = []
    seen: set[str] = set()
    for step in flow.steps:
        def add(msg: str, sid: str = step.id) -> None:  # 绑定这一步的编号（循环变量不能在闭包里直接用）
            out.append({"step": sid, "message": msg})

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
                if name not in flow.variables:
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
