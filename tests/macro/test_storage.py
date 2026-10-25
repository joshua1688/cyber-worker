from datetime import date, datetime, timedelta

import numpy as np

from cyber_worker.macro.screenshot.storage import ScreenshotStorage, read_image


def test_saves_partitioned_by_date(tmp_path):
    st = ScreenshotStorage(tmp_path / "截图")  # 非 ASCII 路径
    img = np.zeros((10, 10, 3), dtype=np.uint8)
    at = datetime(2026, 10, 22, 9, 12, 3)
    p1 = st.save(img, at=at, step_id="s03", action="like")
    p2 = st.save(img, at=at, step_id="s03", action="like")
    assert p1.name == "091203_s03_like.webp" and p1.parent.name == "2026-10-22"
    assert p2 != p1 and p2.exists() and p2.suffix == ".webp"
    assert read_image(p1).shape == (10, 10, 3)


def test_audit_shots_are_lossless_webp(tmp_path):
    """审计截图存成无损 WebP：读回来和原图逐像素一样，比 PNG 小。"""
    rng = np.random.default_rng(0)
    img = np.full((200, 300, 3), 240, np.uint8)
    img[20:60, 20:280] = rng.integers(0, 256, size=(40, 260, 3), dtype=np.uint8)
    img[100:180, 40:120] = (30, 140, 230)
    p = ScreenshotStorage(tmp_path).save(img, at=datetime(2026, 10, 25, 12, 0), step_id="s01", action="click")
    assert np.array_equal(read_image(p), img)
    png = tmp_path / "same.png"
    from cyber_worker.macro.screenshot.storage import write_image
    write_image(png, img)
    assert p.stat().st_size < png.stat().st_size


def test_cleanup_keeps_recent_days(tmp_path):
    st = ScreenshotStorage(tmp_path)
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    today = date(2026, 10, 23)
    for days_ago in (1, 25, 31, 40):
        day = datetime.combine(today - timedelta(days=days_ago), datetime.min.time())
        st.save(img, at=day, step_id="s", action="a")
    removed = st.cleanup(today=today)
    assert {p.name for p in removed} == {"2026-09-22", "2026-09-13"}
