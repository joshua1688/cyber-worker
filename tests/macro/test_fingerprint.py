import numpy as np

from cyber_worker.macro.vision.fingerprint import match_page
from helpers import make_book, pattern, screen_with


def test_matches_when_anchor_present(tmp_path):
    book = make_book(tmp_path)
    m = match_page(book, "compose", screen_with(pattern()), scale=1.0)
    assert m.matched and m.score > 0.99


def test_tolerates_small_shift_within_margin(tmp_path):
    book = make_book(tmp_path)
    assert match_page(book, "compose", screen_with(pattern(), at=(56, 36)), 1.0).matched


def test_rejects_other_page(tmp_path):
    book = make_book(tmp_path)
    m = match_page(book, "compose", screen_with(pattern(seed=2)), 1.0)
    assert not m.matched


def test_rejects_blank_screen(tmp_path):
    book = make_book(tmp_path)
    assert not match_page(book, "compose", screen_with(None), 1.0).matched


def test_unknown_page_never_matches(tmp_path):
    book = make_book(tmp_path)
    assert not match_page(book, "feed", screen_with(pattern()), 1.0).matched


def test_retina_screenshot_matches_1x_reference(tmp_path):
    book = make_book(tmp_path)
    shot = screen_with(pattern(), scale=2)
    assert match_page(book, "compose", shot, scale=2.0).matched


def test_all_anchors_must_match(tmp_path):
    from cyber_worker.macro.screenshot.storage import write_image
    from cyber_worker.macro.vision.fingerprint import Anchor

    book = make_book(tmp_path)
    other = pattern(seed=5)
    write_image(tmp_path / "anchors" / "b.png", other)
    book.pages["compose"].anchors.append(Anchor(image="anchors/b.png", x=200, y=200, w=40, h=30))
    shot = screen_with(pattern())
    assert not match_page(book, "compose", shot, 1.0).matched
    shot[200:230, 200:240] = other
    assert match_page(book, "compose", shot, 1.0).matched
