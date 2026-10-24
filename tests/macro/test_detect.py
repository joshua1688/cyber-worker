import numpy as np

from cyber_worker.macro.vision.detect import changed_ratio, frame_changed
from fakes import blank
from helpers import pattern, screen_with


def test_small_changes_do_not_count():
    a = screen_with(pattern())
    b = a.copy()
    b[5:9, 5:12] = 0  # 输入框里多了几个字
    assert not frame_changed(a, a.copy())
    assert not frame_changed(a, b)


def test_new_page_counts():
    a = screen_with(pattern())
    b = screen_with(pattern(80, 60, seed=2), at=(200, 150))
    assert frame_changed(a, b)


def test_resized_window_counts_as_changed():
    assert changed_ratio(blank(400, 300), blank(300, 200)) == 1.0
    assert changed_ratio(blank(), np.full((300, 400, 3), 255, np.uint8)) == 0.0
