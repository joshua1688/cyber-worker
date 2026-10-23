import math
import random
import statistics

from cyber_worker.macro.act import keyboard, mouse, scroll
from fakes import FakeIO

no_sleep = lambda s: None


def test_bezier_ends_exactly_on_target():
    rng = random.Random(0)
    for _ in range(50):
        end = (rng.uniform(0, 1000), rng.uniform(0, 1000))
        path = mouse.bezier_path((10, 10), end, rng)
        assert path[-1] == end
        assert len(path) >= 8


def test_bezier_is_not_a_straight_line():
    path = mouse.bezier_path((0, 0), (1000, 0), random.Random(3))
    assert max(abs(y) for _, y in path) > 5


def test_move_lands_on_target_even_with_overshoot():
    io = FakeIO()
    mouse.move(io, (800, 600), random.Random(0), no_sleep, overshoot_prob=1.0)
    assert io.pos == (800, 600)


def test_click_jitter_is_small():
    rng = random.Random(1)
    for _ in range(20):
        io = FakeIO()
        mouse.click(io, (300, 200), "left", rng, no_sleep, jitter=1.0)
        (_, (x, y), _), = io.calls
        assert abs(x - 300) <= 1 and abs(y - 200) <= 1


def test_typing_intervals_are_lognormal_around_median():
    rng = random.Random(0)
    xs = [keyboard.interval(rng, median=0.12) for _ in range(5000)]
    assert math.isclose(statistics.median(xs), 0.12, rel_tol=0.05)
    assert min(xs) > 0


def test_typos_are_corrected():
    io = FakeIO()
    keyboard.type_text(io, "Hello world, 你好", random.Random(0), no_sleep, typo_prob=1.0)
    assert io.typed() == "Hello world, 你好"
    assert ("combo", "backspace") in io.calls


def test_scroll_segments_sum_to_total():
    rng = random.Random(0)
    for dy in (1, 5, 17, -9):
        segs = scroll.segments(dy, rng, back_prob=0.5)
        assert sum(segs) == dy


def test_scroll_emits_ticks():
    io = FakeIO()
    scroll.scroll(io, 6, random.Random(0), no_sleep)
    assert sum(c[1] for c in io.calls) == 6
