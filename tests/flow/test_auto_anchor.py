import numpy as np
from helpers import pattern

from cyber_worker.flow.vision.auto_anchor import drop_odd_frames, pick_stable_region


def chat_window(seed_msgs: int, typed: bool) -> np.ndarray:
    """合成一个聊天窗口：顶部有聊天对象名字（纹理），中间消息区随时间变化，底部输入框打字后变化，
    还有两个长得一模一样的工具栏图标。"""
    img = np.full((400, 600, 3), 245, np.uint8)
    img[20:52, 40:200] = pattern(160, 32, seed=1)  # 聊天对象名字：稳定、独一无二
    img[80:280, 20:580] = pattern(560, 200, seed=10 + seed_msgs)  # 消息区：每帧都在变
    icon = pattern(30, 30, seed=3)
    img[300:330, 30:60] = icon  # 两个一样的图标：不唯一
    img[300:330, 90:120] = icon
    if typed:
        img[340:380, 20:400] = pattern(380, 40, seed=7)  # 输入框里有字了
    img[8:40, 470:590] = pattern(120, 32, seed=5)  # 右上角窗口按钮
    return img


def test_picks_stable_unique_title_region():
    frames = [chat_window(0, False), chat_window(1, True), chat_window(2, False)]
    region = pick_stable_region(frames, 1.0, avoid=[(500, 360)])
    assert region is not None
    x, y, w, h = region
    # 落在聊天对象名字那块里
    assert 40 - 8 <= x and x + w <= 200 + 8 and 20 - 8 <= y and y + h <= 52 + 8


def test_avoids_clicked_places():
    frames = [chat_window(0, False), chat_window(1, False)]
    x, y, w, h = pick_stable_region(frames, 1.0, avoid=[(120, 36)])
    assert not (x - 40 <= 120 <= x + w + 40 and y - 40 <= 36 <= y + h + 40)


def test_returns_none_when_nothing_is_stable_or_textured():
    blank = np.full((300, 400, 3), 255, np.uint8)
    assert pick_stable_region([blank, blank], 1.0) is None


def test_retina_frames():
    frame = chat_window(0, False)
    big = np.kron(frame, np.ones((2, 2, 1), np.uint8))
    region = pick_stable_region([big, big], 2.0)
    assert region is not None and region[2] == 140 and region[3] == 32


def test_one_bad_frame_does_not_spoil_the_pick():
    """后台截到一张坏帧（窗口没画完、截成了整个屏幕）：剔掉它，照样选得出来。"""
    good = [chat_window(0, False), chat_window(1, True), chat_window(2, False)]
    junk = np.random.default_rng(9).integers(0, 256, size=good[0].shape, dtype=np.uint8)
    assert pick_stable_region([*good, junk], 1.0, avoid=[(500, 360)]) is not None
    kept = drop_odd_frames([*good, junk])
    assert len(kept) == 3 and not any(f is junk for f in kept)


def test_frames_of_another_size_are_dropped():
    frame = chat_window(0, False)
    small = frame[:200, :300]
    assert [f.shape for f in drop_odd_frames([frame, small, frame])] == [frame.shape, frame.shape]
