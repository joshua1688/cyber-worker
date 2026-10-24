"""平台包导出 / 导入的格式与安全校验（不涉及 studio）。"""

import io
import json
import zipfile

import numpy as np
import pytest
import yaml

from cyber_worker.adapters.pack import MANIFEST, PackError, export_pack, read_pack, unpack
from cyber_worker.macro.anchors import add_anchor, save_target_crop
from cyber_worker.macro.schema import Click, Script, TypeText, Window
from cyber_worker.macro.screenshot.storage import write_image
from helpers import pattern


def make_platform(root):
    """一个带参考截图、页面锚点、按图找目标的 qq 平台目录。"""
    tpl = root / "qq" / "templates"
    tpl.mkdir(parents=True)
    path = tpl / "聊天.yaml"
    Script(
        name="聊天", platform="qq", reference="聊天.png",
        window=Window(app="QQ", width=320, height=740),
        windows={"w2": Window(app="QQ", title="My Sky", width=680, height=613)},
        steps=[Click(id="s01", x=10, y=10), TypeText(id="s02", text="hi", expected_page="页面1")],
    ).dump(path)
    write_image(tpl / "聊天.png", np.zeros((20, 20, 3), np.uint8))
    frame = np.zeros((200, 200, 3), np.uint8)
    frame[40:72, 50:82] = pattern(32, 32)
    add_anchor(path, "页面1", (50, 40, 32, 32), frame, 1.0)
    script = Script.load(path)
    script.steps[0].target = save_target_crop(path, "s01", frame[40:72, 50:82], (50, 40, 32, 32), 1.0)
    script.dump(path)
    return root / "qq"


def make_zip(files: dict[str, bytes | str], manifest: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if manifest is not False:
            z.writestr(MANIFEST, json.dumps(manifest or {"format": 1, "platform": "qq"}))
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


MACRO = yaml.safe_dump({"name": "m", "window": {"app": "QQ", "width": 10, "height": 10}, "steps": []})


def test_round_trip_keeps_macro_anchors_and_targets(tmp_path):
    src = make_platform(tmp_path / "src")
    manifest = unpack(export_pack(src), tmp_path / "dst")

    assert manifest.platform == "qq"
    assert manifest.apps == ["QQ"]
    assert [m.name for m in manifest.macros] == ["聊天"]
    tpl = tmp_path / "dst" / "templates"
    script = Script.load(tpl / "聊天.yaml")
    assert [s.id for s in script.steps] == ["s01", "s02"]
    assert (tpl / script.steps[0].target.image).exists()
    assert (tpl / "聊天.anchors" / "页面1_1.png").exists()
    assert (tpl / "聊天.pages.yaml").exists()


def test_export_drops_reference_screenshot(tmp_path):
    src = make_platform(tmp_path / "src")
    with zipfile.ZipFile(io.BytesIO(export_pack(src))) as z:
        names = z.namelist()
        raw = yaml.safe_load(z.read("templates/聊天.yaml"))
    assert "templates/聊天.png" not in names
    assert "reference" not in raw


@pytest.mark.parametrize("name", ["../evil.yaml", "/abs.yaml", "templates/../../x.png", "C:/x.png",
                                  "templates\\x.png", "./templates/a.yaml"])
def test_rejects_unsafe_paths(name):
    with pytest.raises(PackError):
        read_pack(make_zip({"templates/m.yaml": MACRO, name: MACRO}))


def test_rejects_garbage_and_missing_manifest():
    with pytest.raises(PackError, match="不是有效"):
        read_pack(b"not a zip")
    with pytest.raises(PackError, match="manifest"):
        read_pack(make_zip({"templates/m.yaml": MACRO}, manifest=False))


@pytest.mark.parametrize("files", [
    {"templates/m.yaml": MACRO, "templates/run.exe": b"MZ"},  # 不支持的类型
    {"templates/m.yaml": MACRO, "templates/a.png": b"not png"},  # 冒充 png
    {"templates/m.yaml": "steps: [1, 2"},  # 坏 yaml
    {"templates/m.yaml": yaml.safe_dump({"name": "m"})},  # 不是宏
    {"templates/m.yaml": yaml.safe_dump({**yaml.safe_load(MACRO), "pages": "../../../secret.yaml"})},
    {"templates/m.pages.yaml": yaml.safe_dump(
        {"pages": {"p": {"anchors": [{"image": "../../x.png", "x": 0, "y": 0, "w": 4, "h": 4}]}}}),
     "templates/m.yaml": MACRO},
    {"pages.yaml": "{}"},  # 没有宏
])
def test_rejects_bad_files(files):
    with pytest.raises(PackError):
        read_pack(make_zip(files))


@pytest.mark.parametrize("manifest", [{"format": 99, "platform": "qq"}, {"format": 1, "platform": "../x"},
                                      {"format": 1, "platform": ".."}, {"platform": "qq"}])
def test_rejects_bad_manifest(manifest):
    with pytest.raises(PackError):
        read_pack(make_zip({"templates/m.yaml": MACRO}, manifest))


def test_bad_pack_writes_nothing(tmp_path):
    with pytest.raises(PackError):
        unpack(make_zip({"templates/m.yaml": MACRO, "templates/bad.png": b"x"}), tmp_path / "dst")
    assert not (tmp_path / "dst").exists()
