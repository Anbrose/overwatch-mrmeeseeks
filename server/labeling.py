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
REPROMPT_SECONDS = 180    # 同一个未知头像多久后可以再弹一次（菜单可能被刷走，或 bot 重启后失效）


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

    def _save_meta(self, pid: str, meta: dict[str, Any]) -> None:
        (self.unlabeled / f"{pid}.json").write_text(json.dumps(meta))

    def add(self, crop: np.ndarray, meta: dict[str, Any]) -> tuple[str, bool]:
        """加入队列，返回 (id, 现在要不要弹标注菜单)。

        和队列里某张几乎一样时不重复入队，只计数并更新为最新的昵称/位置；
        这张最近 REPROMPT_SECONDS 内没弹过就再弹（之前的菜单可能错过或已失效）。"""
        for item in self._items():
            if item.image.shape == crop.shape:
                score = float(cv2.matchTemplate(crop, item.image, cv2.TM_CCOEFF_NORMED).max())
                if score >= DUPLICATE_SCORE:
                    seen = item.meta.get("seen", 1) + 1
                    item.meta.update({k: v for k, v in meta.items() if v is not None}, seen=seen)
                    self._save_meta(item.id, item.meta)
                    return item.id, time.time() - item.meta.get("prompted_at", 0) >= REPROMPT_SECONDS
        pid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        cv2.imwrite(str(self.unlabeled / f"{pid}.png"), crop)
        self._save_meta(pid, {**meta, "seen": 1})
        return pid, True

    def mark_prompted(self, pid: str) -> None:
        item = self.get(pid)
        if item is not None:
            item.meta["prompted_at"] = time.time()
            self._save_meta(pid, item.meta)

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
