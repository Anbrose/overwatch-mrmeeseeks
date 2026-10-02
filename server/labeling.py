"""认不出的头像：待标注队列 + 标注后存成模板并立刻加入匹配器。

存储布局（state_dir 挂载在服务器 /opt/mrmeeseeks/state）：
  unlabeled/<id>.png + <id>.json   待标注
  templates/<label>/<id>.png       标注后的模板
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from vision.heroes import DEAD, EMPTY, HeroMatcher

DUPLICATE_SCORE = 0.90


@dataclass
class Pending:
    id: str
    image: np.ndarray
    meta: dict[str, Any]

    def png(self) -> bytes:
        ok, buf = cv2.imencode(".png", self.image)
        return buf.tobytes()


class LabelStore:
    def __init__(self, state_dir: Path, matcher: HeroMatcher):
        self.unlabeled = Path(state_dir) / "unlabeled"
        self.templates = Path(state_dir) / "templates"
        self.unlabeled.mkdir(parents=True, exist_ok=True)
        self.templates.mkdir(parents=True, exist_ok=True)
        self.matcher = matcher

    def _items(self) -> list[Pending]:
        items = []
        for meta_file in sorted(self.unlabeled.glob("*.json")):
            img = cv2.imread(str(meta_file.with_suffix(".png")), cv2.IMREAD_COLOR)
            if img is not None:
                items.append(Pending(meta_file.stem, img, json.loads(meta_file.read_text())))
        return items

    def add(self, crop: np.ndarray, meta: dict[str, Any]) -> str | None:
        """加入队列；和已有某张几乎一样就只给那张计数加一，返回 None。"""
        for item in self._items():
            if item.image.shape == crop.shape:
                score = float(cv2.matchTemplate(crop, item.image, cv2.TM_CCOEFF_NORMED).max())
                if score >= DUPLICATE_SCORE:
                    item.meta["seen"] = item.meta.get("seen", 1) + 1
                    (self.unlabeled / f"{item.id}.json").write_text(json.dumps(item.meta))
                    return None
        pid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        cv2.imwrite(str(self.unlabeled / f"{pid}.png"), crop)
        (self.unlabeled / f"{pid}.json").write_text(json.dumps({**meta, "seen": 1}))
        return pid

    def pending(self, limit: int | None = None) -> list[Pending]:
        items = self._items()
        return items[:limit] if limit else items

    def get(self, pid: str) -> Pending | None:
        return next((i for i in self._items() if i.id == pid), None)

    def label(self, pid: str, label: str) -> bool:
        """label 是英雄 key，或 DEAD / EMPTY。存成模板并立刻生效。"""
        item = self.get(pid)
        if item is None:
            return False
        d = self.templates / label
        d.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(d / f"{pid}.png"), item.image)
        self.matcher.add(label, item.image)
        self.discard(pid)
        return True

    def discard(self, pid: str) -> None:
        for suffix in (".png", ".json"):
            (self.unlabeled / f"{pid}{suffix}").unlink(missing_ok=True)


SPECIAL_LABELS = {DEAD: "Dead (X portrait)", EMPTY: "Not picked yet (?)"}
