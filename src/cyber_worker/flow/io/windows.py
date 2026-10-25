"""Windows：Win32 定位窗口（按进程名，可选按标题）、Pillow 截图、SendInput 打字，其余键鼠走 pynput。

进程开启 Per-Monitor DPI 感知后，Win32 / pynput 用的都是物理像素；
对外的「点」= 物理像素 / 窗口所在屏幕的缩放倍率（DPI / 96）。
窗口尺寸按可见边框计算（去掉 Win10 / 11 的透明阴影边）。
"""

from __future__ import annotations

import ctypes
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from ctypes import wintypes

import numpy as np

from cyber_worker.flow.io.base import (
    CalibrationError,
    WindowGeometry,
    WindowInfo,
    pick_window,
)
from cyber_worker.flow.io.pynput_input import PynputInput
from cyber_worker.flow.schema import Window


def _enable_dpi_awareness() -> None:
    # 必须在创建任何窗口 / pynput 控制器之前调用；已设置过会返回错误码，忽略即可
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        ctypes.windll.user32.SetProcessDPIAware()


_enable_dpi_awareness()

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsZoomed.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

_GW_OWNER = 4
_SW_SHOW = 5
_SW_RESTORE = 9
_SWP_NOZORDER_NOACTIVATE = 0x0004 | 0x0010
_DWMWA_EXTENDED_FRAME_BOUNDS = 9
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_VK_MENU = 0x12
_KEYEVENTF_KEYUP = 0x2
_KEYEVENTF_UNICODE = 0x4
_INPUT_KEYBOARD = 1
_SM_XVIRTUALSCREEN, _SM_YVIRTUALSCREEN = 76, 77


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        # 联合体大小必须和系统定义一致（MOUSEINPUT 最大），否则 SendInput 会拒绝
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]


def _utf16_units(ch: str) -> list[int]:
    data = ch.encode("utf-16-le")
    return [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]


def _process_name(pid: int) -> str:
    h = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value)
    finally:
        kernel32.CloseHandle(h)


def _window_title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _exe_name(app: str) -> str:
    app = app.lower()
    return app if app.endswith(".exe") else app + ".exe"


def find_window(app: str, title: str | None = None, *, size: tuple[float, float] | None = None,
                avoid: Iterable[str] = ()) -> int | None:
    """按进程名找可见主窗口，如 "WeChat"、"chrome.exe"；title 非空时要求标题包含它，取最上层的。
    title 为空时同一个程序可能开着好几个窗口：避开 avoid 里的标题，挑尺寸最接近 size（点）的（pick_window）。"""
    target = _exe_name(app)
    found: list[tuple[int, str, float, float]] = []

    @WNDENUMPROC
    def cb(hwnd, _):
        if (user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0
                and not user32.GetWindow(hwnd, _GW_OWNER)):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            text = _window_title(hwnd)
            if _process_name(pid.value).lower() == target and (not title or title in text):
                found.append((hwnd, text, *_size_points(hwnd)))
                if title:
                    return False  # 认标题：最上层的那个就是
        return True

    user32.EnumWindows(cb, 0)
    return pick_window(found, None if title else size, () if title else avoid)


def _size_points(hwnd) -> tuple[float, float]:
    """窗口尺寸（点）；最小化的按还原后的尺寸。"""
    scale = user32.GetDpiForWindow(hwnd) / 96 or 1.0
    if user32.IsIconic(hwnd):
        wp = _WINDOWPLACEMENT(length=ctypes.sizeof(_WINDOWPLACEMENT))
        user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
        r = wp.rcNormalPosition
    else:
        r = _frame(hwnd)
    return (r.right - r.left) / scale, (r.bottom - r.top) / scale


_DWMWA_CLOAKED = 14
_MIN_W, _MIN_H = 200, 150  # 太小的一般是托盘、浮动工具条


def _window_gone(hwnd) -> bool:
    """窗口已经关了、最小化了、被隐藏了：截不到它，屏幕上那块区域也不是它。"""
    return (not user32.IsWindow(hwnd) or user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd)
            or _cloaked(hwnd))


def _cloaked(hwnd) -> bool:
    """UWP 程序常有「可见」但被系统隐藏（cloaked）的窗口，不能算打开着的窗口。"""
    val = wintypes.DWORD()
    dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
    return bool(val.value)


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(_WINDOWPLACEMENT)]
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
_GA_ROOT = 2


def _info(hwnd) -> WindowInfo | None:
    """窗口 → WindowInfo；不是正常的应用窗口（桌面、隐藏窗口、太小）返回 None。"""
    if (not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) == 0
            or user32.GetWindow(hwnd, _GW_OWNER) or _cloaked(hwnd)):
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = _process_name(pid.value)
    title = _window_title(hwnd)
    if not exe or (exe.lower() == "explorer.exe" and title == "Program Manager"):
        return None
    scale = user32.GetDpiForWindow(hwnd) / 96 or 1.0
    minimized = bool(user32.IsIconic(hwnd))
    if minimized:
        # 最小化的窗口没有可见边框，用还原后的位置估算（含透明边，差几个点不影响）
        wp = _WINDOWPLACEMENT(length=ctypes.sizeof(_WINDOWPLACEMENT))
        user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
        r = wp.rcNormalPosition
    else:
        r = _frame(hwnd)
    w, h = round((r.right - r.left) / scale), round((r.bottom - r.top) / scale)
    if w < _MIN_W or h < _MIN_H:
        return None
    return WindowInfo(app=re.sub(r"\.exe$", "", exe, flags=re.I), title=title,
                      width=w, height=h, minimized=minimized)


def list_windows() -> list[WindowInfo]:
    """可见的应用窗口，最上层的在前；最小化的也算（新建流程时用户常常先把它最小化了）。"""
    out: list[WindowInfo] = []

    @WNDENUMPROC
    def cb(hwnd, _):
        info = _info(hwnd)
        if info:
            out.append(info)
        return True

    user32.EnumWindows(cb, 0)
    return out


def window_at(x: float, y: float) -> WindowInfo | None:
    """物理像素坐标处的顶层窗口。"""
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return _info(user32.GetAncestor(hwnd, _GA_ROOT)) if hwnd else None


def _geometry(hwnd) -> WindowGeometry:
    scale = user32.GetDpiForWindow(hwnd) / 96 or 1.0
    f = _frame(hwnd)
    return WindowGeometry(f.left / scale, f.top / scale, (f.right - f.left) / scale,
                          (f.bottom - f.top) / scale, scale, handle=hwnd or 0)


def _with_geometry(hwnd) -> tuple[WindowInfo, WindowGeometry] | None:
    if not hwnd:
        return None
    root = user32.GetAncestor(hwnd, _GA_ROOT)
    info = _info(root)
    if info is None or info.minimized:
        return None
    return info, _geometry(root)


def window_geometry_at(x: float, y: float) -> tuple[WindowInfo, WindowGeometry] | None:
    return _with_geometry(user32.WindowFromPoint(wintypes.POINT(int(x), int(y))))


def foreground() -> tuple[WindowInfo, WindowGeometry] | None:
    return _with_geometry(user32.GetForegroundWindow())


imm32 = ctypes.WinDLL("imm32")
imm32.ImmGetDefaultIMEWnd.argtypes = [wintypes.HWND]
imm32.ImmGetDefaultIMEWnd.restype = wintypes.HWND
user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                       wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
_WM_IME_CONTROL, _IMC_GETCONVERSIONMODE, _IMC_GETOPENSTATUS = 0x283, 0x1, 0x5
_IME_CMODE_NATIVE = 0x1
_SMTO_ABORTIFHUNG = 0x2


def ime_active() -> bool:
    """前台窗口的输入法是否打开且处于中文模式。问的是输入法的默认窗口，跨进程也能用；对方卡住时 100ms 放弃。"""
    fg = user32.GetForegroundWindow()
    ime = imm32.ImmGetDefaultIMEWnd(fg) if fg else None
    if not ime:
        return False

    def ask(what: int) -> int:
        out = ctypes.c_size_t()
        ok = user32.SendMessageTimeoutW(ime, _WM_IME_CONTROL, what, 0, _SMTO_ABORTIFHUNG, 100, ctypes.byref(out))
        return out.value if ok else 0

    return bool(ask(_IMC_GETOPENSTATUS)) and bool(ask(_IMC_GETCONVERSIONMODE) & _IME_CMODE_NATIVE)


user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
_HELD_VK = {"ctrl": (0x11,), "shift": (0x10,), "alt": (0x12,), "cmd": (0x5B, 0x5C)}


def held_modifiers() -> set[str]:
    return {name for name, vks in _HELD_VK.items() if any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in vks)}


def chime(kind: str) -> None:
    """异步播放提示音（合成好的 wav），立刻返回。"""
    import winsound

    from cyber_worker.flow.io.chime import NOTES, chime_file

    if kind not in NOTES:
        return
    try:
        winsound.PlaySound(str(chime_file(kind)),
                           winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except (RuntimeError, OSError):  # 没有声卡、临时目录写不了等：静默
        pass


def _bring_to_front(hwnd) -> None:
    """Windows 不允许后台进程随意抢前台。两种常用的绕过：
    先按一下 Alt（系统认为本进程刚有键盘输入），再挂到当前前台窗口的输入队列上切换。"""
    user32.keybd_event(_VK_MENU, 0, 0, 0)
    user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    me = kernel32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True))
    try:
        user32.ShowWindow(hwnd, _SW_SHOW)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_thread, False)


def _frame(hwnd) -> wintypes.RECT:
    rect = wintypes.RECT()
    dwmapi.DwmGetWindowAttribute(hwnd, _DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect))
    return rect


def _outer(hwnd) -> wintypes.RECT:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect


gdi32 = ctypes.WinDLL("gdi32")
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
_PW_RENDERFULLCONTENT = 2  # 硬件加速的窗口（Chromium / Electron，QQ NT 就是）也能截到


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def _print_window(hwnd) -> np.ndarray | None:
    """截窗口自己的内容（PrintWindow），被别的窗口挡住也截得到；裁掉透明边，只留可见边框以内。
    截不到、或者截出来是全黑（有的程序不支持）返回 None，由调用方退回屏幕截图。"""
    if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        return None
    outer, frame = _outer(hwnd), _frame(hwnd)
    w, h = outer.right - outer.left, outer.bottom - outer.top
    if w <= 0 or h <= 0:
        return None
    hdc = user32.GetWindowDC(hwnd)
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    try:
        gdi32.SelectObject(mem, bmp)
        if not user32.PrintWindow(hwnd, mem, _PW_RENDERFULLCONTENT):
            return None
        bmi = _BITMAPINFOHEADER(biSize=ctypes.sizeof(_BITMAPINFOHEADER), biWidth=w, biHeight=-h,
                                biPlanes=1, biBitCount=32)
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bmi), 0):
            return None
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)
    img = np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, :3]
    dx, dy = frame.left - outer.left, frame.top - outer.top
    img = img[dy:dy + frame.bottom - frame.top, dx:dx + frame.right - frame.left]
    if img.size == 0 or img.max() < 16:  # 全黑：这个程序不让 PrintWindow 截
        return None
    return img.copy()


class WinScreenIO(PynputInput):
    def __init__(self, *, settle: float = 0.4) -> None:
        super().__init__()
        self._settle = settle
        self.input_factor = 1.0

    def list_windows(self) -> list[WindowInfo]:
        return list_windows()

    def window_at(self, x: float, y: float) -> WindowInfo | None:
        return window_at(x, y)

    def window_geometry_at(self, x: float, y: float):
        return window_geometry_at(x, y)

    def foreground(self):
        return foreground()

    def ime_active(self) -> bool:
        return ime_active()

    def held_modifiers(self) -> set[str]:
        return held_modifiers()

    def chime(self, kind: str) -> None:
        chime(kind)

    def locate_window(self, window: Window, avoid: Iterable[str] = ()) -> WindowGeometry:
        hwnd = find_window(window.app, window.title, size=(window.width, window.height), avoid=avoid)
        if hwnd is None or user32.IsIconic(hwnd):
            raise CalibrationError(f"找不到 {window.title or window.app} 的窗口")
        scale = user32.GetDpiForWindow(hwnd) / 96 or 1.0
        frame = _frame(hwnd)
        self.input_factor = scale
        return WindowGeometry(frame.left / scale, frame.top / scale,
                              (frame.right - frame.left) / scale, (frame.bottom - frame.top) / scale, scale=scale,
                              handle=hwnd or 0)

    def calibrate(self, window: Window, avoid: Iterable[str] = ()) -> WindowGeometry:
        hwnd = find_window(window.app, window.title, size=(window.width, window.height), avoid=avoid)
        if hwnd is None:
            what = f"{window.app}（标题含「{window.title}」）" if window.title else window.app
            raise CalibrationError(f"找不到 {what} 的窗口，请先打开它")
        if user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
            user32.ShowWindow(hwnd, _SW_RESTORE)
            time.sleep(self._settle)

        _bring_to_front(hwnd)

        scale = user32.GetDpiForWindow(hwnd) / 96 or 1.0
        frame, outer = _frame(hwnd), _outer(hwnd)
        # 外框比可见边框多出的透明边
        extra_w = (frame.left - outer.left) + (outer.right - frame.right)
        extra_h = (frame.top - outer.top) + (outer.bottom - frame.bottom)
        want_w, want_h = round(window.width * scale), round(window.height * scale)
        user32.SetWindowPos(hwnd, None, outer.left, outer.top,
                            want_w + extra_w, want_h + extra_h, _SWP_NOZORDER_NOACTIVATE)
        time.sleep(self._settle)

        frame = _frame(hwnd)
        got_w, got_h = frame.right - frame.left, frame.bottom - frame.top
        if abs(got_w - want_w) > 1 or abs(got_h - want_h) > 1:
            raise CalibrationError(
                f"窗口尺寸校准失败：期望 {window.width}x{window.height}，"
                f"实际 {got_w / scale:.0f}x{got_h / scale:.0f}（窗口可能有最小尺寸限制）"
            )
        if user32.GetForegroundWindow() != hwnd:
            _bring_to_front(hwnd)
            time.sleep(self._settle)
            if user32.GetForegroundWindow() != hwnd:
                raise CalibrationError(f"无法把 {window.app} 切到前台")

        self.input_factor = scale
        return WindowGeometry(frame.left / scale, frame.top / scale,
                              window.width, window.height, scale=scale, handle=hwnd or 0)

    def capture(self, geo: WindowGeometry) -> np.ndarray:
        """知道窗口句柄就截窗口自己的内容（被挡住也对）；不知道、或者这个程序不让截，就截屏幕上那块区域。
        窗口已经关了 / 最小化了就抛 CalibrationError：这时屏幕上那块是别的窗口，截下来是张乱图，
        会被当成这个窗口的画面（参考图、步骤画面）。"""
        if geo.handle:
            if _window_gone(geo.handle):
                raise CalibrationError("窗口已经关了或最小化了，截不到它")
            img = _print_window(geo.handle)
            want = (round(geo.height * geo.scale), round(geo.width * geo.scale))
            if img is not None and abs(img.shape[0] - want[0]) <= 2 and abs(img.shape[1] - want[1]) <= 2:
                return img
        from PIL import ImageGrab

        s = geo.scale
        left, top = round(geo.x * s), round(geo.y * s)
        right, bottom = left + round(geo.width * s), top + round(geo.height * s)
        # all_screens=True 时 bbox 以虚拟屏幕左上角为原点（副屏可能在负坐标）
        vx = user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        img = ImageGrab.grab(bbox=(left - vx, top - vy, right - vx, bottom - vy), all_screens=True)
        return np.asarray(img.convert("RGB"))[:, :, ::-1].copy()

    def type_char(self, ch: str) -> None:
        """一律用 KEYEVENTF_UNICODE 发送：绕过中文输入法，否则英文字母会被输入法吃掉变成候选词。"""
        for unit in _utf16_units(ch):
            down = _INPUT(type=_INPUT_KEYBOARD, ki=_KEYBDINPUT(wScan=unit, dwFlags=_KEYEVENTF_UNICODE))
            up = _INPUT(type=_INPUT_KEYBOARD,
                        ki=_KEYBDINPUT(wScan=unit, dwFlags=_KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP))
            arr = (_INPUT * 2)(down, up)
            if user32.SendInput(2, arr, ctypes.sizeof(_INPUT)) != 2:
                raise OSError(ctypes.get_last_error(), "SendInput 失败")


# ---------------- 全局快捷键：系统注册（RegisterHotKey） ----------------
# 和「被动监听键盘」相比：系统保证独占（按下时前台软件收不到同一个键，不会一键两用），
# 而且别的程序已经占用时会注册失败——冲突能准确查出来。

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

_MOD_BITS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "cmd": 0x8}
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY, _WM_QUIT = 0x0312, 0x0012
_ERROR_HOTKEY_ALREADY_REGISTERED = 1409
_PROBE_ID = 0xBEEF
_NAMED_VK = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "page_up": 0x21, "page_down": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "pause": 0x13, "scroll_lock": 0x91,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}


def hotkey_spec(combo: str) -> tuple[int, int] | None:
    """"ctrl+alt+f9" → (修饰位, 虚拟键码)；认不出的键返回 None。"""
    from cyber_worker.flow.io.pynput_input import _WIN_VK, MODIFIERS, normalize_combo

    parts = normalize_combo(combo).split("+")
    mods = sum(_MOD_BITS[p] for p in parts if p in MODIFIERS)
    keys = [p for p in parts if p not in MODIFIERS]
    if len(keys) != 1:
        return None
    key = keys[0]
    vk = _NAMED_VK.get(key) or next((v for v, n in _WIN_VK.items() if n == key), None)
    return (mods, vk) if vk else None


def hotkey_free(combo: str) -> bool | None:
    """试注册一下马上注销：True 空闲，False 已被别的程序（或我们自己）注册，None 认不出这个键。"""
    spec = hotkey_spec(combo)
    if spec is None:
        return None
    if user32.RegisterHotKey(None, _PROBE_ID, spec[0] | _MOD_NOREPEAT, spec[1]):
        user32.UnregisterHotKey(None, _PROBE_ID)
        return True
    return False


class WinHotkeys:
    """在一个专用线程里注册热键、跑消息循环；改键时整体停掉重来（注册必须和消息循环在同一个线程）。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._tid = 0

    def available(self, combo: str) -> bool | None:
        return hotkey_free(combo)

    def bind(self, bindings: dict[str, Callable[[], None]]) -> dict[str, str | None]:
        """注册一组热键；返回 {组合键: None 表示成功 / 失败原因}。"""
        self.stop()
        results: dict[str, str | None] = {}
        ready = threading.Event()

        def loop() -> None:
            self._tid = kernel32.GetCurrentThreadId()
            msg = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # 先建好消息队列，stop() 才能投递退出消息
            ids: dict[int, Callable[[], None]] = {}
            for i, (combo, cb) in enumerate(bindings.items(), start=1):
                spec = hotkey_spec(combo)
                if spec is None:
                    results[combo] = "这个键不支持做全局快捷键"
                elif user32.RegisterHotKey(None, i, spec[0] | _MOD_NOREPEAT, spec[1]):
                    ids[i] = cb
                    results[combo] = None
                else:
                    err = ctypes.get_last_error()
                    results[combo] = ("已被其他程序占用" if err == _ERROR_HOTKEY_ALREADY_REGISTERED
                                      else f"注册失败（错误 {err}）")
            ready.set()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == _WM_HOTKEY and msg.wParam in ids:
                    # 回调可能要好一会儿（开始录制要校准窗口），别卡住消息循环
                    threading.Thread(target=ids[msg.wParam], daemon=True).start()
            for i in ids:
                user32.UnregisterHotKey(None, i)

        self._thread = threading.Thread(target=loop, daemon=True, name="hotkeys")
        self._thread.start()
        ready.wait(3)
        return results

    def stop(self) -> None:
        if self._thread and self._thread.is_alive():
            user32.PostThreadMessageW(self._tid, _WM_QUIT, 0, 0)
            self._thread.join(2)
        self._thread = None
