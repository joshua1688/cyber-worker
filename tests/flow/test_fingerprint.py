from helpers import make_book, pattern, screen_with

from cyber_worker.flow.vision.fingerprint import match_page


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
    from cyber_worker.flow.screenshot.storage import write_image
    from cyber_worker.flow.vision.fingerprint import Anchor

    book = make_book(tmp_path)
    other = pattern(seed=5)
    write_image(tmp_path / "anchors" / "b.png", other)
    book.pages["compose"].anchors.append(Anchor(image="anchors/b.png", x=200, y=200, w=40, h=30))
    shot = screen_with(pattern())
    assert not match_page(book, "compose", shot, 1.0).matched
    shot[200:230, 200:240] = other
    assert match_page(book, "compose", shot, 1.0).matched


def test_anchor_that_moved_far_is_found_and_reported(tmp_path):
    """界面布局变了（左边多出一栏，内容整体右移）：原位置附近没有，在整个窗口里找到唯一的一处，照样算对上，并报告挪了多少。"""
    book = make_book(tmp_path)
    m = match_page(book, "compose", screen_with(pattern(), at=(250, 40)), 1.0)
    assert m.matched and m.score > 0.99 and m.shift == (200, 0)
    assert match_page(book, "compose", screen_with(pattern()), 1.0).shift is None  # 在原处：不算挪


def test_moved_anchor_with_a_look_alike_is_not_trusted(tmp_path):
    """挪了位置、窗口里又有两处都像它：分不清是哪个，宁可不认（页面核对是防止把字打错地方的）。"""
    book = make_book(tmp_path)
    shot = screen_with(pattern(), at=(250, 40))
    shot[200:230, 300:340] = pattern()
    assert not match_page(book, "compose", shot, 1.0).matched
