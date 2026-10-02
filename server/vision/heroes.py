"""英雄头像模板匹配。

模板目录结构：<dir>/<label>/*.png，label 是 OverFast 的英雄 key（如 kiriko），
或特殊类别 _dead（阵亡 X）、_empty（未选英雄 ?）。同一英雄可以有多张（皮肤、我方/敌方底色）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np

THRESHOLD = 0.70   # 低于这个分数一律 unknown，宁可留空也不猜
DEAD, EMPTY = "_dead", "_empty"


@dataclass
class Match:
    label: str | None
    score: float
    x: int        # 最佳位置左上角（绝对坐标）
    y: int


class HeroMatcher:
    def __init__(self, dirs: Iterable[Path] = ()):
        self.templates: list[tuple[str, np.ndarray]] = []
        for d in dirs:
            d = Path(d)
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*/*.png")):
                img = cv2.imread(str(f), cv2.IMREAD_COLOR)
                if img is not None:
                    self.templates.append((f.parent.name, img))

    @property
    def labels(self) -> set[str]:
        return {label for label, _ in self.templates}

    def add(self, label: str, img: np.ndarray) -> None:
        self.templates.append((label, img))

    def match(self, band: np.ndarray, ox: int = 0, oy: int = 0) -> Match:
        best = Match(None, -1.0, ox, oy)
        for label, t in self.templates:
            if t.shape[0] > band.shape[0] or t.shape[1] > band.shape[1]:
                continue
            r = cv2.matchTemplate(band, t, cv2.TM_CCOEFF_NORMED)
            _, score, _, loc = cv2.minMaxLoc(r)
            if score > best.score:
                best = Match(label, float(score), ox + loc[0], oy + loc[1])
        return best
