""".cwpack 平台包：整个平台目录打成一个 zip，分享给别人导入。

这里只管格式本身：导出成字节、校验后解压到一个空目录。导入时的重名处理（宏改名、
修正引用）由调用方用 relocate_macro 做，和保存草稿是同一套逻辑。

导出自动去掉每个宏的参考截图（整窗截图，只给编辑器画布显示，最容易拍到别人的隐私）。
导入不问用户任何问题，但会先在内存里把整个包校验完，有一处不对就整包拒绝，不写半个包。
"""

from __future__ import annotations

import io
import json
import platform as _platform
import posixpath
import re
import zipfile
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from cyber_worker.macro.schema import Click, Script
from cyber_worker.macro.vision.fingerprint import PageBook

FORMAT = 1
EXT = ".cwpack"
MANIFEST = "manifest.json"
ALLOWED = {".yaml", ".png", ".json"}
MAX_FILES = 2000
MAX_BYTES = 200 * 1024 * 1024  # 解压后总大小
_PNG = b"\x89PNG\r\n\x1a\n"
# 平台目录名：和 studio.state.platform_slug 生成的一致（允许 notepad++ 这种）
_PLATFORM = re.compile(r'^[^\\/:*?"<>|#%&\s.][^\\/:*?"<>|#%&\s]*$')


class PackError(ValueError):
    """包坏了或不安全：整包拒绝，消息直接给用户看。"""


class PackMacro(BaseModel):
    file: str  # 相对平台目录，如 templates/QQ.yaml
    name: str


class Manifest(BaseModel):
    format: int
    platform: str
    apps: list[str] = []  # 包里的宏会操作哪些程序
    macros: list[PackMacro] = []
    exported_at: str = ""
    os: str = ""


def is_macro(rel: str) -> bool:
    """templates/ 下的 *.yaml 是宏；*.pages.yaml 是宏自己的页面锚点文件。"""
    parts = rel.split("/")
    return len(parts) == 2 and parts[0] == "templates" and rel.endswith(".yaml") \
        and not rel.endswith(".pages.yaml")


# ---- 导出 ----

def export_pack(platform_dir: Path, *, screenshots: bool = True) -> bytes:
    """把一个平台目录打成 .cwpack（字节）。

    screenshots：带上录制时的整窗口截图（每个窗口的参考图、每一步的画面），导入的人打开宏就能看到
    当时在哪点的。截图里可能有聊天内容；关掉时这些图不打进去，宏里对它们的引用也去掉（执行不受影响，
    执行只看锚点和目标图）。"""
    platform_dir = platform_dir.resolve()
    files = sorted(p for p in platform_dir.rglob("*") if p.is_file() and p.suffix.lower() in ALLOWED)
    skip: set[Path] = set()
    macros: list[PackMacro] = []
    apps: set[str] = set()
    texts: dict[Path, bytes] = {}
    for p in files:
        rel = p.relative_to(platform_dir).as_posix()
        if not is_macro(rel):
            continue
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        script = Script.model_validate(raw)
        if not screenshots:
            if script.reference:
                skip.add((p.parent / script.reference).resolve())
                raw.pop("reference", None)
            for key, win in script.windows.items():
                if win.reference:
                    skip.add((p.parent / win.reference).resolve())
                    raw["windows"][key].pop("reference", None)
            for raw_step, step in zip(raw.get("steps", []), script.steps):
                if step.shot:
                    skip.add((p.parent / step.shot).resolve())
                    raw_step.pop("shot", None)
        texts[p] = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False).encode("utf-8")
        macros.append(PackMacro(file=rel, name=script.name))
        apps.update(w.app for w in (script.window, *script.windows.values()))

    manifest = Manifest(
        format=FORMAT, platform=platform_dir.name, apps=sorted(apps), macros=macros,
        exported_at=datetime.now().isoformat(timespec="seconds"), os=_platform.system(),
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST, manifest.model_dump_json(indent=2))
        for p in files:
            if p in skip:
                continue
            rel = p.relative_to(platform_dir).as_posix()
            if rel == MANIFEST:
                continue
            z.writestr(rel, texts.get(p) or p.read_bytes())
    return buf.getvalue()


# ---- 导入 ----

def _safe_member(name: str) -> bool:
    """zip 里的路径必须是干净的相对路径：不能跳出目录（zip-slip）、不能是绝对路径或盘符。"""
    if not name or "\\" in name or ":" in name or name.startswith("/"):
        return False
    if posixpath.normpath(name) != name:  # 拒绝 a/../b、./a、a//b
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


def _inside(base: str, ref: str | None) -> bool:
    """文件里引用的路径（相对 base 所在目录）不能指到平台目录外面去。"""
    if ref is None:
        return True
    if "\\" in ref or ":" in ref or ref.startswith("/"):
        return False
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base), ref))
    return not (joined == ".." or joined.startswith("../"))


def read_pack(data: bytes) -> tuple[Manifest, dict[str, bytes]]:
    """校验整个包，返回清单和 {相对平台目录的路径: 内容}。不对就抛 PackError。"""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise PackError("不是有效的 .cwpack 文件") from None
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > MAX_FILES:
            raise PackError("包里的文件太多")
        if sum(i.file_size for i in infos) > MAX_BYTES:
            raise PackError("包太大")
        files: dict[str, bytes] = {}
        for info in infos:
            name = info.filename
            if not _safe_member(name):
                raise PackError(f"包里有不安全的路径：{name}")
            if Path(name).suffix.lower() not in ALLOWED:
                raise PackError(f"包里有不支持的文件：{name}")
            files[name] = z.read(info)

    if MANIFEST not in files:
        raise PackError("包里缺少 manifest.json，不是 .cwpack 文件")
    try:
        manifest = Manifest.model_validate_json(files.pop(MANIFEST))
    except ValidationError:
        raise PackError("manifest.json 格式不对") from None
    if manifest.format > FORMAT:
        raise PackError("这个包是用更新版本导出的，请先升级赛博打工人")
    if not _PLATFORM.match(manifest.platform):
        raise PackError(f"平台名不合法：{manifest.platform}")

    macros = 0
    for name, content in files.items():
        _check_file(name, content)
        macros += is_macro(name)
    if not macros:
        raise PackError("包里没有宏")
    return manifest, files


def _check_file(name: str, content: bytes) -> None:
    suffix = Path(name).suffix.lower()
    if suffix == ".png":
        if not content.startswith(_PNG):
            raise PackError(f"{name} 不是 PNG 图片")
        return
    try:
        raw = (yaml.safe_load if suffix == ".yaml" else json.loads)(content.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError, ValueError):
        raise PackError(f"{name} 读不出来") from None
    try:
        if is_macro(name):
            script = Script.model_validate(raw or {})
            refs = [script.pages, script.reference, *(w.reference for w in script.windows.values())]
            refs += [s.shot for s in script.steps]
            refs += [s.target.image for s in script.steps if isinstance(s, Click) and s.target]
        elif name.endswith(".pages.yaml"):
            book = PageBook.model_validate(raw or {})
            refs = [a.image for fp in [*book.pages.values(), *book.retired.values()] for a in fp.anchors]
        else:
            return
    except ValidationError:
        raise PackError(f"{name} 不是有效的宏文件") from None
    for ref in refs:
        if not _inside(name, ref):
            raise PackError(f"{name} 引用了包外面的文件：{ref}")


def unpack(data: bytes, dest: Path) -> Manifest:
    """校验通过后把整个包解压到 dest（空目录）下，dest 就相当于一个平台目录。"""
    manifest, files = read_pack(data)
    for name, content in files.items():
        path = dest / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return manifest
