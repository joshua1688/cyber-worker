"""Windows：用 waveOut 把 sounds.Mixer 混好的声音一块块送进声卡。

系统的 PlaySound 一次只能放一个声音，新的会把旧的切断（打字时一个字的声音还没完，下一个字就来了）；
这里自己混音，几个声音可以叠在一起响，录制 / 执行开始的提示音（PlaySound）也不会被打断——两者是不同的音频流。

播放在专门的线程里：第一次出声时打开声卡，安静一会儿就关掉，不一直占着声卡、不空转。
"""

from __future__ import annotations

import ctypes
import queue
import random
import threading
import time
from ctypes import wintypes

from cyber_worker.macro.io.sounds import RATE, Mixer, cues, pick

winmm = ctypes.WinDLL("winmm")


class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD), ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD), ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD), ("cbSize", wintypes.WORD)]


class WAVEHDR(ctypes.Structure):
    pass


WAVEHDR._fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD),
                    ("dwBytesRecorded", wintypes.DWORD), ("dwUser", ctypes.c_size_t), ("dwFlags", wintypes.DWORD),
                    ("dwLoops", wintypes.DWORD), ("lpNext", ctypes.POINTER(WAVEHDR)), ("reserved", ctypes.c_size_t)]

HWAVEOUT = wintypes.HANDLE
winmm.waveOutOpen.argtypes = [ctypes.POINTER(HWAVEOUT), wintypes.UINT, ctypes.POINTER(WAVEFORMATEX),
                              ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
winmm.waveOutPrepareHeader.argtypes = [HWAVEOUT, ctypes.POINTER(WAVEHDR), wintypes.UINT]
winmm.waveOutUnprepareHeader.argtypes = [HWAVEOUT, ctypes.POINTER(WAVEHDR), wintypes.UINT]
winmm.waveOutWrite.argtypes = [HWAVEOUT, ctypes.POINTER(WAVEHDR), wintypes.UINT]
winmm.waveOutReset.argtypes = [HWAVEOUT]
winmm.waveOutClose.argtypes = [HWAVEOUT]

_WAVE_MAPPER = 0xFFFFFFFF
_WHDR_DONE = 0x1
BLOCK = 512  # 每块采样数（约 12 毫秒）：越小声音跟操作越同步
BLOCKS = 4  # 同时排在声卡里的块数：太少会断断续续
IDLE_CLOSE = 1.5  # 安静这么久就关掉声卡（秒）


class WinSound:
    """Sound 的 Windows 实现。play() 只把事件放进队列，混音、出声都在自己的线程里。"""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._rng = random.Random()

    def play(self, kind: str, **kw) -> None:
        todo = cues(kind, kw)
        if not todo:
            return
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="sounds", daemon=True)
                self._thread.start()
        for name, delay in todo:
            self._q.put((pick(name, self._rng), delay))

    def close(self) -> None:
        self._q.put(None)

    def _loop(self) -> None:
        mixer = Mixer()
        while True:
            item = self._q.get()  # 安静时就在这里等，不占声卡
            if item is None:
                return
            mixer.add(*item)
            if not self._stream(mixer):
                return

    def _stream(self, mixer: Mixer) -> bool:
        """打开声卡一直送声音，直到安静了 IDLE_CLOSE 秒。返回 False = 收到了 close()。"""
        fmt = WAVEFORMATEX(1, 1, RATE, RATE * 2, 2, 16, 0)  # PCM，单声道，16 位
        dev = HWAVEOUT()
        if winmm.waveOutOpen(ctypes.byref(dev), _WAVE_MAPPER, ctypes.byref(fmt), 0, 0, 0) != 0:
            _drain(self._q)  # 没有声卡、被独占：这次的声音就不放了
            return True
        bufs = [ctypes.create_string_buffer(BLOCK * 2) for _ in range(BLOCKS)]
        hdrs = [WAVEHDR(lpData=ctypes.cast(b, ctypes.c_void_p), dwBufferLength=BLOCK * 2) for b in bufs]
        for h in hdrs:
            winmm.waveOutPrepareHeader(dev, ctypes.byref(h), ctypes.sizeof(WAVEHDR))
            h.dwFlags |= _WHDR_DONE  # 都当作「空闲」，下面先填满
        quiet_since = None
        keep = True
        try:
            while True:
                try:
                    while True:
                        item = self._q.get_nowait()
                        if item is None:
                            keep = False
                            break
                        mixer.add(*item)
                except queue.Empty:
                    pass
                if not keep:
                    return False
                wrote = False
                for b, h in zip(bufs, hdrs):
                    if h.dwFlags & _WHDR_DONE:
                        data = mixer.mix(BLOCK)
                        ctypes.memmove(b, data.ctypes.data, data.nbytes)
                        h.dwFlags &= ~_WHDR_DONE
                        winmm.waveOutWrite(dev, ctypes.byref(h), ctypes.sizeof(WAVEHDR))
                        wrote = True
                if mixer.active:
                    quiet_since = None
                elif quiet_since is None:
                    quiet_since = time.monotonic()
                elif time.monotonic() - quiet_since > IDLE_CLOSE and self._q.empty():
                    return True
                if not wrote:
                    time.sleep(0.003)
        finally:
            winmm.waveOutReset(dev)
            for h in hdrs:
                winmm.waveOutUnprepareHeader(dev, ctypes.byref(h), ctypes.sizeof(WAVEHDR))
            winmm.waveOutClose(dev)


def _drain(q: queue.Queue) -> None:
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass

