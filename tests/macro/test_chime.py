"""录制提示音：合成出来的是合法 wav，开始上行、停止下行，音量不刺耳。"""

import io
import tempfile
import wave
from array import array

import numpy as np

from cyber_worker.macro.io import chime


def samples(kind):
    with wave.open(io.BytesIO(chime.synthesize(kind))) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, chime.RATE)
        return np.array(array("h", w.readframes(w.getnframes())), dtype=float)


def pitch(x):
    """一段声音里最响的频率。"""
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return np.fft.rfftfreq(len(x), 1 / chime.RATE)[spec.argmax()]


def test_start_rises_stop_falls_and_is_soft():
    n = int(0.08 * chime.RATE)  # 只看第一个音还没被第二个音盖过的那一小段
    for kind, first, second in (("start", chime.C5, chime.G5), ("stop", chime.G5, chime.C5)):
        x = samples(kind)
        assert 0.4 < len(x) / chime.RATE < 0.8  # 短促，不拖沓
        assert abs(x).max() <= chime.PEAK * 32767 + 1
        assert abs(x[:20]).max() < 2000  # 淡入：开头没有「咔」的一声
        assert abs(pitch(x[:n]) - first) < 30
        late = x[int(0.12 * chime.RATE):int(0.12 * chime.RATE) + n]
        assert abs(pitch(late) - second) < 30


def test_chime_file_is_rebuilt_once_per_run(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(chime, "_fresh", set())
    stale = tmp_path / "cyber-worker-chime-start.wav"
    stale.write_bytes(b"old sound")  # 上一个版本留下的
    p = chime.chime_file("start")
    assert p == stale and p.read_bytes() == chime.synthesize("start")  # 这次运行第一次用：重新合成
    p.write_bytes(b"cached")
    assert chime.chime_file("start").read_bytes() == b"cached"  # 之后直接用，不再合成
