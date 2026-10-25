"""执行时的操作音效：单击 = 鼠标按下 + 松开（右键、中键低一点），长按 / 拖动的按下、松开分开响；
滚轮「咔」；打字是青轴机械键盘（8 下随机用）；回车是机械键盘的回车大键。

鼠标和打字用 sound_assets/ 里的录音，读不到就用合成的顶上；滚轮、回车是合成的（正弦、衰减、噪声）。
- bank()：所有音效，启动后第一次用时准备好。
- cues()：一个操作事件（和屏幕特效同一套：click / double / press / release / scroll / key / type / hover）
  该放哪几个声音、各晚多少秒。
- Mixer：把同时在响的几个声音叠在一起（打字很快，一个声音还没完下一个就来了；不能像系统 PlaySound 那样互相切断）。
- 真正出声的是平台的 Sound（Windows 见 win_sound）；其他平台暂时不出声（NullSound）。
"""

from __future__ import annotations

import random
from functools import cache
from pathlib import Path
from typing import Protocol

import numpy as np

RATE = 44100
KEY_VARIANTS = 6  # 打字录音读不到时，合成几种音调的青轴声顶上
ASSETS = Path(__file__).with_name("sound_assets")  # 录音：key-blue-N.wav（打字）、mouse-press / mouse-release.wav
VOLUME = 0.55  # 总音量：执行时人可能就坐在旁边，不要太响
MAX_SCROLL_TICKS = 6  # 一下滚很多格时最多响几声
CLICK_GAP = 0.07  # 单击：按下后多久响松开（执行时是瞬间按下松开的，声音上留点间隔才像真的鼠标）


class Sound(Protocol):
    def play(self, kind: str, **kw) -> None:
        """为一个操作事件出声，不阻塞。kind 和屏幕特效一样。"""
        ...

    def close(self) -> None: ...


class NullSound:
    def play(self, kind: str, **kw) -> None:
        pass

    def close(self) -> None:
        pass


# ---------------- 合成 ----------------


def _t(sec: float) -> np.ndarray:
    return np.arange(int(RATE * sec)) / RATE


def _fade(x: np.ndarray, attack: float = 0.0002, release: float = 0.004) -> np.ndarray:
    """开头、结尾各淡一点：硬切会有「噗」的爆音。淡入只有 0.2 毫秒：再长就把「咔」那一下最脆的音头削掉了。"""
    n = len(x)
    env = np.ones(n)
    a, r = min(n, int(RATE * attack)), min(n, int(RATE * release))
    if a:
        env[:a] = np.linspace(0, 1, a)
    if r:
        env[-r:] = np.minimum(env[-r:], np.linspace(1, 0, r))
    return x * env


def _tone(freq: float, sec: float, tau: float, partials=((1.0, 1.0),)) -> np.ndarray:
    t = _t(sec)
    wave = sum(a * np.sin(2 * np.pi * freq * m * t) for m, a in partials)
    return wave * np.exp(-t / tau)


def _noise(sec: float, tau: float, seed: int, bright: int = 1, smooth: int = 6) -> np.ndarray:
    """衰减的噪声。bright 次差分 = 去掉低频，越大越「脆」；再用 smooth 个采样的滑动平均去掉最高的那段
    （大约 RATE / smooth 以上）：不然全是 8 kHz 以上的「嘶」，刺耳。"""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(int(RATE * sec) + bright)
    for _ in range(bright):
        x = np.diff(x)
    if smooth > 1:  # 滑动平均做两遍（三角窗）：只做一遍高频的旁瓣还很大
        kernel = np.ones(smooth) / smooth
        x = np.convolve(np.convolve(x, kernel, mode="same"), kernel, mode="same")
    x /= np.abs(x).max() or 1
    return x * np.exp(-_t(sec) / tau)


def _norm(x: np.ndarray, peak: float) -> np.ndarray:
    return _fade(x / (np.abs(x).max() or 1) * peak)


def _mix(*parts: np.ndarray) -> np.ndarray:
    out = np.zeros(max(len(p) for p in parts))
    for p in parts:
        out[:len(p)] += p
    return out


def _at(x: np.ndarray, sec: float) -> np.ndarray:
    """把一段声音往后挪 sec 秒（前面补静音），好用 _mix 拼成一个完整的动作。"""
    return np.concatenate([np.zeros(int(RATE * sec)), x])


def _snap(freq: float, seed: int, tau: float = 0.0025) -> np.ndarray:
    """一下很脆的「咔」：极短的亮噪声（触点 / 簧片弹开的那一下）+ 一个很快衰减的共振（外壳、轴体的回响）。"""
    sec = 0.03
    hit = _noise(sec, 0.0005, seed=seed, bright=1, smooth=2)
    return _mix(hit, 0.7 * _tone(freq, sec, tau))


def _mouse(freq: float, seed: int, *, release: bool) -> np.ndarray:
    """合成的鼠标微动开关（录音读不到时用）：按下是清脆的「哒」带一点塑料外壳的闷响，松开是轻一点、高一点的「嗒」。"""
    if release:
        return 0.45 * _snap(freq * 1.15, seed, tau=0.0018)
    return _mix(_snap(freq, seed), 0.35 * _tone(freq * 0.42, 0.03, 0.005))


def _blue(pitch: float, seed: int) -> np.ndarray:
    """青轴机械键盘的一次按键：按到触发点时卡扣弹开的清脆「咔」（青轴最有特点的那声），
    十几毫秒后键帽撞到底的「哒」（低一点、厚一点），松开时卡扣再「咔」一声（轻）。"""
    click = _snap(4200 * pitch, seed, tau=0.003)
    bottom = _mix(_noise(0.05, 0.002, seed=seed + 50, bright=1, smooth=5),
                  0.8 * _tone(1100 * pitch, 0.05, 0.006), 0.6 * _tone(330 * pitch, 0.06, 0.012))
    up = 0.4 * _snap(4600 * pitch, seed + 90, tau=0.002)
    return _mix(click, _at(0.85 * bottom, 0.012), _at(up, 0.11))


def _big_key(seed: int) -> np.ndarray:
    """机械键盘的回车大键：比普通键更厚的「哒」，加上平衡杆的一点金属「嗒啦」。"""
    k = _blue(0.88, seed)
    rattle = _mix(*(_at(0.15 * _tone(5200 + 300 * i, 0.012, 0.0012), 0.016 + 0.006 * i) for i in range(4)))
    thud = _at(0.5 * _tone(220, 0.08, 0.02), 0.012)
    return _mix(k, rattle, thud)


def _wav(path: Path) -> np.ndarray | None:
    """读一个 16 位单声道 44.1 kHz 的 wav，float、峰值 1；读不到返回 None。"""
    import wave

    try:
        with wave.open(str(path)) as w:
            if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
                return None
            x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(float)
    except (OSError, EOFError, wave.Error):
        return None
    return x / (np.abs(x).max() or 1) if x.size else None


def _recorded(prefix: str) -> list[np.ndarray]:
    """素材目录里的 <prefix>-N.wav，按文件名排好。"""
    return [x for f in sorted(ASSETS.glob(f"{prefix}-*.wav")) if (x := _wav(f)) is not None]


def _pitch(x: np.ndarray, factor: float) -> np.ndarray:
    """变调（同时变长短）：factor < 1 更低更长。右键、中键用低一点的同一个声音。"""
    n = int(len(x) / factor)
    return np.interp(np.arange(n) * factor, np.arange(len(x)), x)


@cache
def bank() -> dict[str, tuple[np.ndarray, ...]]:
    """所有音效（每种可能有几个变体），float，峰值已经按各自的响度定好。"""
    recorded = _recorded("key-blue")
    if recorded:
        keys = tuple(_fade(k * 0.4) for k in recorded)
        soft = tuple(_fade(k * 0.22) for k in recorded[:2])  # 退格、清空：同样的键，轻一点
    else:
        rng = random.Random(7)
        keys = tuple(_norm(_blue(rng.uniform(0.94, 1.08), seed=10 + 7 * i), 0.4) for i in range(KEY_VARIANTS))
        soft = tuple(_norm(_blue(1.0, seed=60 + i), 0.24) for i in range(2))
    press, release = _wav(ASSETS / "mouse-press.wav"), _wav(ASSETS / "mouse-release.wav")
    if press is not None and release is not None:
        press, release = _fade(press * 0.45), _fade(release * 0.45)
    else:
        press = _norm(_mouse(3600, seed=5, release=False), 0.42)
        release = _norm(_mouse(3600, seed=7, release=True), 0.3)
    click = _mix(press, _at(release, CLICK_GAP))
    return {
        "click": (click,),
        "click_low": (_pitch(click, 0.85),),
        "press": (press,),
        "release": (release,),
        "tick": (_norm(_noise(0.012, 0.0015, seed=3, bright=1, smooth=5), 0.22),),
        "key": keys,
        "key_soft": soft,
        "enter": (_norm(_big_key(seed=300), 0.45),),
    }


def cues(kind: str, kw: dict) -> list[tuple[str, float]]:
    """这个操作放哪些声音：[(音效名, 晚几秒)]。"""
    button = kw.get("button", "left")
    if kind in ("click", "double"):  # 双击的第一下是普通单击事件，第二下是 double：合起来两次点击声
        return [("click" if button == "left" else "click_low", 0.0)]
    if kind == "press":
        return [("press", 0.0)]
    if kind == "release":
        return [("release", 0.0)]
    if kind == "scroll":
        n = min(MAX_SCROLL_TICKS, abs(int(kw.get("dy", 0))) + abs(int(kw.get("dx", 0))))
        return [("tick", i * 0.035) for i in range(n)]
    if kind == "key":
        label = kw.get("label", "")
        if label in ("Enter", "Return"):
            return [("enter", 0.0)]
        return [("key_soft" if label in ("Backspace", "Delete", "清空输入框") else "key", 0.0)]
    if kind == "type":
        return [("enter" if kw.get("text") in ("\n", "\r") else "key", 0.0)]
    return []  # hover：悬停本来就是安静地等


def pick(name: str, rng: random.Random | None = None) -> np.ndarray:
    variants = bank()[name]
    return variants[(rng or random).randrange(len(variants))]


# ---------------- 混音 ----------------


class Mixer:
    """正在响的几个声音叠在一起，一块一块地交给声卡。不是线程安全的：只在播放线程里用。"""

    def __init__(self, volume: float = VOLUME) -> None:
        self.volume = volume
        self._voices: list[list] = []  # [采样, 下一块从第几个采样开始（负数 = 还要等几个采样才开始）]

    @property
    def active(self) -> bool:
        return bool(self._voices)

    def add(self, samples: np.ndarray, delay: float = 0.0) -> None:
        self._voices.append([samples, -round(delay * RATE)])

    def mix(self, frames: int) -> np.ndarray:
        """下一块 frames 个采样，16 位整数。"""
        out = np.zeros(frames)
        alive = []
        for voice in self._voices:
            s, pos = voice
            src = max(pos, 0)
            dst = src - pos
            count = min(frames - dst, len(s) - src)
            if count > 0:
                out[dst:dst + count] += s[src:src + count]
            voice[1] = pos + frames
            if voice[1] < len(s):
                alive.append(voice)
        self._voices = alive
        out = np.tanh(out * self.volume * 1.2) / 1.2  # 叠得太响时柔和地压住，不会削波爆音
        return (np.clip(out, -1, 1) * 32767).astype(np.int16)

