from datetime import date, datetime, timedelta

import numpy as np

from cyber_worker.macro.screenshot.storage import ScreenshotStorage, read_image


def test_saves_partitioned_by_date(tmp_path):
    st = ScreenshotStorage(tmp_path / "截图")  # 非 ASCII 路径
    img = np.zeros((10, 10, 3), dtype=np.uint8)
    at = datetime(2026, 10, 22, 9, 12, 3)
    p1 = st.save(img, at=at, step_id="s03", action="like")
    p2 = st.save(img, at=at, step_id="s03", action="like")
    assert p1.name == "091203_s03_like.png" and p1.parent.name == "2026-10-22"
    assert p2 != p1 and p2.exists()
    assert read_image(p1).shape == (10, 10, 3)


def test_cleanup_keeps_recent_days(tmp_path):
    st = ScreenshotStorage(tmp_path)
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    today = date(2026, 10, 23)
    for days_ago in (1, 25, 31, 40):
        day = datetime.combine(today - timedelta(days=days_ago), datetime.min.time())
        st.save(img, at=day, step_id="s", action="a")
    removed = st.cleanup(today=today)
    assert {p.name for p in removed} == {"2026-09-22", "2026-09-13"}
