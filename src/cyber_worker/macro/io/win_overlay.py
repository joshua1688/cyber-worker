"""Windows：把 EffectBoard 的特效画到屏幕上。

每个特效一个小的分层窗口（WS_EX_LAYERED，UpdateLayeredWindow 逐像素 alpha），置顶、
鼠标穿透（WS_EX_TRANSPARENT）、不抢焦点（WS_EX_NOACTIVATE）、不进任务栏（WS_EX_TOOLWINDOW），
并且不进截图（SetWindowDisplayAffinity WDA_EXCLUDEFROMCAPTURE，Win10 2004 起）：
页面核对、按图找、审计截图都拍不到这些圆圈和标签。窗口没有标题，list_windows 也不会列出它。

所有窗口都在一个专门的线程里创建、更新、销毁（Win32 窗口只能由创建它的线程操作）。
没有特效时这个线程只是等事件，不占 CPU。

    uv run python -m cyber_worker.macro.io.win_overlay    # 演示：屏幕中间依次播放各种特效和音效，不碰键鼠
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from collections.abc import Callable
from ctypes import wintypes

import numpy as np

from cyber_worker.macro.io.effects import EffectBoard

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                       wintypes.COLORREF, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFOHEADER), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

_WS_POPUP = 0x80000000
_WS_EX_LAYERED = 0x00080000
_WS_EX = 0x00000020 | 0x00000008 | 0x00000080 | 0x08000000  # TRANSPARENT|TOPMOST|TOOLWINDOW|NOACTIVATE
_GWL_EXSTYLE = -20
_WDA_EXCLUDEFROMCAPTURE = 0x11
_SW_SHOWNOACTIVATE = 4
_HWND_TOPMOST = wintypes.HWND(-1)
_SWP_NOMOVE, _SWP_NOSIZE, _SWP_NOACTIVATE = 0x2, 0x1, 0x10
_ULW_ALPHA = 2
_CLASS = "CyberWorkerEffect"
FRAME_SEC = 1 / 60


def cursor_pos() -> tuple[float, float]:
    """鼠标此刻在哪（物理像素；进程已开 DPI 感知）。"""
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return float(pt.x), float(pt.y)


def _system_scale() -> float:
    try:
        return user32.GetDpiForSystem() / 96 or 1.0
    except AttributeError:
        return 1.0


class WinOverlay:
    """Overlay 的 Windows 实现。show() 只是把事件放进队列，画图都在自己的线程里。"""

    def __init__(self, cursor: Callable[[], tuple[float, float]] = cursor_pos) -> None:
        self._cursor = cursor
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._wndproc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))

    def show(self, kind: str, **kw) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="effects", daemon=True)
                self._thread.start()
        self._q.put((kind, kw, time.monotonic()))

    def close(self) -> None:
        self._q.put(None)

    # ---------------- 绘制线程 ----------------

    def _loop(self) -> None:
        board = EffectBoard(_system_scale())
        windows: dict[int, wintypes.HWND] = {}
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW(cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=self._wndproc,
                         hInstance=hinst, lpszClassName=_CLASS)
        user32.RegisterClassExW(ctypes.byref(wc))  # 已注册过（第二个 WinOverlay）会失败，不要紧
        try:
            while True:
                items = drain(self._q, FRAME_SEC if board.active else 0.5)
                _pump()
                if None in items:
                    return
                for kind, kw, t in (i for i in items if i):
                    board.handle(kind, kw, self._cursor(), t)
                if board.active:
                    sprites = board.frames(time.monotonic(), self._cursor())
                    for key in [k for k in windows if k not in sprites]:
                        user32.DestroyWindow(windows.pop(key))
                    for key, (x, y, img) in sprites.items():
                        if key not in windows:
                            windows[key] = self._create(hinst)
                        _update(windows[key], x, y, img)
                elif windows:
                    for hwnd in windows.values():
                        user32.DestroyWindow(hwnd)
                    windows.clear()
        finally:
            for hwnd in windows.values():
                user32.DestroyWindow(hwnd)

    def _create(self, hinst) -> wintypes.HWND:
        # 先设「不进截图」再加分层样式：已经是分层窗口时 SetWindowDisplayAffinity 会失败（错误 8）
        hwnd = user32.CreateWindowExW(_WS_EX, _CLASS, "", _WS_POPUP, 0, 0, 1, 1, None, None, hinst, None)
        user32.SetWindowDisplayAffinity(hwnd, _WDA_EXCLUDEFROMCAPTURE)  # 旧系统不支持就算了
        user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, user32.GetWindowLongPtrW(hwnd, _GWL_EXSTYLE) | _WS_EX_LAYERED)
        user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
        return hwnd


def drain(q: queue.Queue, timeout: float) -> list:
    """等最多 timeout 秒拿到第一个事件，再把排着的一起拿走；没有就是空列表。
    每次都返回新列表：等不到事件时绝不能把上一批再处理一遍（输入的字会在标签上重复）。"""
    items = []
    try:
        items.append(q.get(timeout=timeout))
        while True:
            items.append(q.get_nowait())
    except queue.Empty:
        pass
    return items


def _pump() -> None:
    msg = wintypes.MSG()
    while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def _update(hwnd, x: int, y: int, img: np.ndarray) -> bool:
    """把预乘 alpha 的 BGRA 图贴到窗口上，并把窗口放到 (x, y)。第一次贴完再显示，免得闪一下黑框。"""
    h, w = img.shape[:2]
    screen = user32.GetDC(None)
    mem = gdi32.CreateCompatibleDC(screen)
    bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w, biHeight=-h,  # 负数 = 自上而下
                           biPlanes=1, biBitCount=32, biCompression=0)
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(mem, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
    ok = False
    if bmp and bits:
        data = np.ascontiguousarray(img)
        ctypes.memmove(bits, data.ctypes.data, data.nbytes)
        old = gdi32.SelectObject(mem, bmp)
        blend = BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER，按像素 alpha
        ok = bool(user32.UpdateLayeredWindow(hwnd, screen, ctypes.byref(wintypes.POINT(x, y)),
                                             ctypes.byref(wintypes.SIZE(w, h)), mem,
                                             ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), _ULW_ALPHA))
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bmp)
        user32.ShowWindow(hwnd, _SW_SHOWNOACTIVATE)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(None, screen)
    return ok


def demo() -> None:
    """在屏幕中间依次播放各种特效和音效；只画、只出声，不移动鼠标、不点击、不按键。"""
    import cyber_worker.macro.io.windows  # noqa: F401 —— 开 DPI 感知，坐标和真实执行时一致

    w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    pos = [w / 2, h / 2]
    from cyber_worker.macro.io.win_sound import WinSound

    screen, sound = WinOverlay(cursor=lambda: (pos[0], pos[1])), WinSound()

    class Both:  # 特效和音效一起演示（真正执行时 EffectsIO 也是这样同时触发两者）
        def show(self, kind, **kw):
            screen.show(kind, **kw)
            sound.play(kind, **kw)

        def close(self):
            screen.close()
            sound.close()

    overlay = Both()

    def at(dx, dy):
        pos[0], pos[1] = w / 2 + dx, h / 2 + dy

    steps = [
        ("单击", lambda: (at(-300, -120), overlay.show("click", button="left"))),
        ("右键", lambda: (at(-150, -120), overlay.show("click", button="right"))),
        ("双击", lambda: (at(0, -120), overlay.show("click", button="left"), time.sleep(0.09),
                         overlay.show("double", button="left"))),
    ]
    for name, fn in steps:
        print(name)
        fn()
        time.sleep(1.0)
    print("长按")
    at(150, -120)
    overlay.show("press", button="left")
    time.sleep(1.3)
    overlay.show("release", button="left")
    time.sleep(1.0)
    print("拖动")
    at(-300, 60)
    overlay.show("press", button="left")
    for i in range(40):
        at(-300 + i * 12, 60 + 40 * np.sin(i / 6))
        time.sleep(0.02)
    overlay.show("release", button="left")
    time.sleep(1.0)
    print("滚动")
    at(250, 60)
    for _ in range(3):
        overlay.show("scroll", dy=1)
        time.sleep(0.15)
    time.sleep(1.0)
    print("按键 / 输入")
    at(-100, 200)
    overlay.show("key", label="清空输入框")
    time.sleep(0.3)
    for ch in "你好，这是一条测试消息":
        overlay.show("type", text=ch)
        time.sleep(0.08)
    time.sleep(0.4)
    overlay.show("key", label="Enter")
    time.sleep(1.5)
    print("悬停")
    at(200, 200)
    overlay.show("hover", seconds=2.0)
    time.sleep(2.6)
    overlay.close()


if __name__ == "__main__":
    demo()
