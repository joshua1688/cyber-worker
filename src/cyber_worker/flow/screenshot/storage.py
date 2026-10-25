"""截图存档：按日期分层、文件名带时间和步骤，30 天清理。"""

from __future__ import annotations

import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path

import cv2
import numpy as np


def read_image(path: str | Path) -> np.ndarray:
    # cv2.imread 不支持非 ASCII 路径，统一走 imdecode
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"无法读取图片：{path}")
    return img


def write_image(path: str | Path, img: np.ndarray) -> None:
    """按扩展名编码：.webp 存无损 WebP（和 PNG 逐像素一样，小六成左右），其余存 PNG。"""
    if Path(path).suffix.lower() == ".webp":
        ok, buf = cv2.imencode(".webp", img, [cv2.IMWRITE_WEBP_QUALITY, 101])  # 质量 > 100 = 无损
    else:
        ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise ValueError(f"无法编码图片：{path}")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    buf.tofile(str(path))


_SAFE = re.compile(r"[^\w\-]+")


class ScreenshotStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def save(self, img: np.ndarray, *, at: datetime, step_id: str, action: str) -> Path:
        # 审计截图存无损 WebP：每次运行每步一张，PNG 一个月能攒到上 GB
        name = f"{at:%H%M%S}_{_SAFE.sub('_', step_id)}_{_SAFE.sub('_', action)}.webp"
        path = self.root / f"{at:%Y-%m-%d}" / name
        # 同一秒内重复的步骤（如重试）加序号，避免覆盖
        n = 1
        while path.exists():
            path = path.with_name(f"{path.stem.split('~')[0]}~{n}{path.suffix}")
            n += 1
        write_image(path, img)
        return path

    def remove_day(self, day: date) -> None:
        """删掉这一天的全部截图。"""
        d = self.root / day.isoformat()
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)

    def cleanup(self, *, today: date, keep_days: int = 30) -> list[Path]:
        """删除早于 keep_days 天的日期目录，返回被删的目录。"""
        cutoff = today - timedelta(days=keep_days)
        removed = []
        if not self.root.exists():
            return removed
        for d in self.root.iterdir():
            try:
                day = date.fromisoformat(d.name)
            except ValueError:
                continue
            if d.is_dir() and day < cutoff:
                shutil.rmtree(d)
                removed.append(d)
        return removed
