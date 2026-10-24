"""录制开始 / 停止的提示音：程序自己合成的柔和「叮咚」，不用任何现成产品的音效文件。

两个音符叠在一起：开始是上行（叮咚↑），停止是下行（咚叮↓）。每个音符是正弦波加少量泛音，
5 毫秒淡入、指数衰减的余音，听起来像铃，不像蜂鸣器。合成一次存成 wav，Windows 和 macOS 播同一个文件。
"""

from __future__ import annotations

import io
import math
import tempfile
import wave
from array import array
from pathlib import Path

RATE = 44100
VERSION = 2  # 改了音色就加一，换个文件名，不会播到旧缓存

# 放在 500–800 Hz：再高（1 kHz 以上，加上泛音到 3 kHz）正好落在人耳最敏感的频段，听着刺耳
C5, G5 = 523.25, 783.99
# 每个音：(频率, 开始时间 秒)
NOTES = {"start": ((C5, 0.0), (G5, 0.11)), "stop": ((G5, 0.0), (C5, 0.11))}
PARTIALS = ((1.0, 1.0), (2.0, 0.08))  # (倍频, 相对音量)：只留一点二倍泛音，干净、圆润
NOTE_SEC = 0.45
ATTACK_SEC = 0.005
RELEASE_SEC = 0.03  # 结尾淡出：余音还没衰减完就截断会有「咔」的一声
DECAY_SEC = 0.16  # 余音衰减的时间常数：太短只剩「叮」的一下，显得硬
PEAK = 0.32  # 不要太响


def synthesize(kind: str) -> bytes:
    """合成一段提示音，返回 16 位单声道 wav 的字节。"""
    notes = NOTES[kind]
    total = int(RATE * (max(t for _, t in notes) + NOTE_SEC))
    buf = [0.0] * total
    for freq, start in notes:
        first = int(RATE * start)
        for i in range(int(RATE * NOTE_SEC)):
            t = i / RATE
            env = min(1.0, t / ATTACK_SEC, (NOTE_SEC - t) / RELEASE_SEC) * math.exp(-t / DECAY_SEC)
            buf[first + i] += env * sum(a * math.sin(2 * math.pi * freq * m * t) for m, a in PARTIALS)
    loudest = max(abs(v) for v in buf) or 1.0
    samples = array("h", (int(v / loudest * PEAK * 32767) for v in buf))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())
    return out.getvalue()


def chime_file(kind: str) -> Path:
    """提示音文件（第一次用时合成，放在系统临时目录里缓存）。"""
    path = Path(tempfile.gettempdir()) / f"cyber-worker-chime-{kind}-v{VERSION}.wav"
    if not path.exists():
        path.write_bytes(synthesize(kind))
    return path
