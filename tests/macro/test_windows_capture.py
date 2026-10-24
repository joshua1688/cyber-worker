"""Windows 截图：知道窗口句柄就截窗口自己的内容（被挡住也对），截不到才退回屏幕截图。"""

import sys

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 上")


@pytest.fixture
def win(monkeypatch):
    from cyber_worker.macro.io import windows as W

    grabbed = []

    class FakeGrab:
        @staticmethod
        def grab(bbox, all_screens):
            grabbed.append(bbox)
            from PIL import Image
            return Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]), (1, 2, 3))

    import PIL
    monkeypatch.setitem(sys.modules, "PIL.ImageGrab", FakeGrab)
    monkeypatch.setattr(PIL, "ImageGrab", FakeGrab, raising=False)
    return W, grabbed


def test_uses_window_content_when_handle_known(win, monkeypatch):
    W, grabbed = win
    from cyber_worker.macro.io.base import WindowGeometry

    own = np.full((300, 400, 3), 200, np.uint8)
    monkeypatch.setattr(W, "_print_window", lambda hwnd: own)
    img = W.WinScreenIO().capture(WindowGeometry(10, 20, 400, 300, 1.0, handle=123))
    assert img is own and not grabbed


def test_falls_back_to_screen_when_print_fails_or_size_differs(win, monkeypatch):
    W, grabbed = win
    from cyber_worker.macro.io.base import WindowGeometry

    io = W.WinScreenIO()
    geo = WindowGeometry(10, 20, 400, 300, 1.0, handle=123)
    monkeypatch.setattr(W, "_print_window", lambda hwnd: None)  # 全黑 / 不支持
    assert io.capture(geo).shape == (300, 400, 3) and len(grabbed) == 1
    monkeypatch.setattr(W, "_print_window", lambda hwnd: np.full((100, 100, 3), 200, np.uint8))  # 窗口变了尺寸
    io.capture(geo)
    assert len(grabbed) == 2
    io.capture(WindowGeometry(10, 20, 400, 300, 1.0))  # 不知道句柄
    assert len(grabbed) == 3
