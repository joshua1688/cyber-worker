"""操作音效：每个操作配哪些声音、几个声音叠着响、合成的声音不爆音；设置里能单独关掉。"""

import numpy as np
from fakes import FakeIO

from cyber_worker.macro.io.effects import EffectsIO, NullOverlay
from cyber_worker.macro.io.sounds import RATE, Mixer, bank, cues


def test_each_action_gets_its_sound():
    assert cues("click", {}) == [("click", 0.0)]
    assert cues("double", {}) == [("click", 0.0)]  # 第一下是普通单击：合起来两声「嗒嗒」
    assert cues("click", {"button": "right"}) == [("click_low", 0.0)]
    assert cues("press", {}) == [("press", 0.0)] and cues("release", {}) == [("release", 0.0)]
    assert [n for n, _ in cues("scroll", {"dy": 3})] == ["tick"] * 3
    assert len(cues("scroll", {"dy": 40})) == 6  # 一下滚很多格也不会响个没完
    assert cues("type", {"text": "好"}) == [("key", 0.0)]
    assert cues("key", {"label": "Enter"}) == [("enter", 0.0)]
    assert cues("key", {"label": "清空输入框"}) == [("key_soft", 0.0)]
    assert cues("hover", {"seconds": 2}) == []


def test_sounds_overlap_instead_of_cutting_each_other_off():
    m = Mixer(volume=1.0)
    a = np.full(1000, 0.1)
    m.add(a)
    m.add(a, delay=500 / RATE)  # 前一个还没放完，后一个就来了
    out = m.mix(1600).astype(float) / 32767
    assert abs(out[100] - 0.1) < 0.01 and abs(out[700] - 0.2) < 0.02 and abs(out[1200] - 0.1) < 0.01
    assert out[1550] == 0 and not m.active


def test_synthesized_sounds_fade_in_and_out_without_clipping():
    for name, variants in bank().items():
        for v in variants:
            assert np.abs(v).max() <= 0.6, name
            assert abs(v[0]) < 0.05 and abs(v[-1]) < 0.01, name  # 开头结尾都淡过：不会「噗」一声
    keys = bank()["key"]
    assert len({round(float(np.abs(k).sum()), 3) for k in keys}) == len(keys)  # 打字声几种音调各不相同


def test_sounds_follow_their_own_switch():
    played = []

    class Rec:
        def play(self, kind, **kw):
            played.append(kind)

    fx = EffectsIO(FakeIO(), NullOverlay(), sound=Rec())
    fx.click("left")
    fx.type_char("好")
    fx.sound_enabled = False
    fx.click("left")
    assert played == ["click", "type"]

    class Broken:
        def play(self, kind, **kw):
            raise OSError("没有声卡")

    EffectsIO(FakeIO(), NullOverlay(), sound=Broken()).click("left")  # 不抛：执行不受影响


def test_typing_uses_the_recorded_blue_switch_keys():
    """打字声用 sound_assets 里的青轴录音（8 下）。"""
    from cyber_worker.macro.io.sounds import ASSETS

    assert len(bank()["key"]) == 8 == len(list(ASSETS.glob("key-blue-*.wav")))
    assert all(0.35 < np.abs(k).max() <= 0.4 for k in bank()["key"])


def test_mouse_clicks_use_the_recorded_press_and_release():
    """单击 = 录音里的按下 + 70 毫秒后的松开；长按、拖动的按下、松开就是这两声；右键是同一个声音低一点。"""
    from cyber_worker.macro.io.sounds import ASSETS, CLICK_GAP

    assert (ASSETS / "mouse-press.wav").exists() and (ASSETS / "mouse-release.wav").exists()
    press, release, click = bank()["press"][0], bank()["release"][0], bank()["click"][0]
    assert len(click) == int(RATE * CLICK_GAP) + len(release)
    assert np.allclose(click[:100], press[:100])
    assert len(bank()["click_low"][0]) > len(click)  # 变低 = 变长
