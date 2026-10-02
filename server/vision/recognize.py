"""把两张截图识别成结构化 facts：英雄（模板匹配）、昵称/地图/攻防（OCR）、推车进度（像素）。"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from . import layout, progress, text
from .heroes import DEAD, EMPTY, THRESHOLD, HeroMatcher
from .text import Line, MapInfo

OcrFn = Callable[[np.ndarray], list[Line]]

HEADER_Y0, HEADER_Y1, HEADER_SCALE = 0.02, 0.055, 2.0    # 右上角「攻击/护送 | 霓虹枢纽」，避开上方 FPS 叠层
OBJECTIVE_Y0, OBJECTIVE_Y1, OBJECTIVE_HALF_W = 0.02, 0.075, 0.2   # 进度条截图里的目标文字


@dataclass
class Slot:
    team: str
    row: int
    label: str | None        # 英雄 key / _dead / _empty / None(unknown)
    score: float
    player: str | None = None
    crop: np.ndarray | None = field(default=None, repr=False)   # 内缩后的头像，供标注

    @property
    def status(self) -> str:
        if self.label is None:
            return "unknown"
        return {DEAD: "dead", EMPTY: "empty"}.get(self.label, "alive")


@dataclass
class Recognition:
    table_found: bool
    slots: list[Slot] = field(default_factory=list)
    map: MapInfo | None = None
    header_text: str = ""
    objective_text: str = ""
    side: str | None = None
    stage: str | None = None          # 混合图 A 点阶段为 "A"，推车阶段为检查点序号字符串
    progress: float | None = None
    elapsed: float = 0.0

    @property
    def unknowns(self) -> list[Slot]:
        return [s for s in self.slots if s.status == "unknown"]


class Recognizer:
    def __init__(self, matcher: HeroMatcher, maps: list[MapInfo], ocr: OcrFn | None = None):
        self.matcher = matcher
        self.maps = maps
        if ocr is None:
            from .ocr import read as ocr
        self.ocr = ocr

    def recognize(self, scoreboard: bytes, hud: bytes) -> Recognition:
        t0 = time.time()
        sb = layout.decode(scoreboard)
        k = layout.H / sb.shape[0]
        sb = layout.scale_to(sb, k)
        hud_img = layout.scale_to(layout.decode(hud), k)

        rec = self._heroes(sb)
        if rec.table_found:
            self._header(sb, rec)
            self._names(sb, rec)
        self._objective(hud_img, rec)
        rec.elapsed = time.time() - t0
        return rec

    # ---------- 英雄 ----------
    def _heroes(self, sb: np.ndarray) -> Recognition:
        w = sb.shape[1]
        matches = {}
        for team in layout.TEAMS:
            for row in layout.ROWS:
                band, ox, oy = layout.search_band(sb, team, row)
                matches[(team, row)] = self.matcher.match(band, ox, oy)
        confident = [(key, m) for key, m in matches.items() if m.label and m.score >= THRESHOLD]
        if not confident:
            return Recognition(table_found=False)

        self._dx = statistics.median(layout.dx_of(w, team, row, m.x) for (team, row), m in confident)
        slots = []
        for (team, row), m in matches.items():
            ok = m.label is not None and m.score >= THRESHOLD
            x0, y0, x1, y1 = layout.portrait_box(w, team, row, self._dx)
            slots.append(Slot(team, row, m.label if ok else None, m.score,
                              crop=None if ok else sb[y0:y1, x0:x1].copy()))
        return Recognition(table_found=True, slots=slots)

    # ---------- OCR ----------
    def _header(self, sb: np.ndarray, rec: Recognition) -> None:
        head = sb[int(HEADER_Y0 * layout.H):int(HEADER_Y1 * layout.H), sb.shape[1] // 2:]
        lines = self.ocr(layout.scale_to(head, HEADER_SCALE))
        rec.header_text = "".join(l.text for l in sorted(lines, key=lambda l: l.cx))
        rec.map = text.match_map(rec.header_text, self.maps)

    def _names(self, sb: np.ndarray, rec: Recognition) -> None:
        col, _, oy = layout.name_column(sb, self._dx)
        lines = self.ocr(col)
        keys = [(s.team, s.row) for s in rec.slots]
        centers = [layout.row_center_y(team, row) - oy for team, row in keys]
        names = text.assign_names(lines, centers, tol=layout.PITCH * layout.H / 2)
        for slot, name in zip(rec.slots, names):
            slot.player = name

    def _objective(self, hud: np.ndarray, rec: Recognition) -> None:
        cx = hud.shape[1] / 2
        region = hud[int(OBJECTIVE_Y0 * layout.H):int(OBJECTIVE_Y1 * layout.H),
                     int(cx - OBJECTIVE_HALF_W * layout.H):int(cx + OBJECTIVE_HALF_W * layout.H)]
        rec.objective_text = "".join(l.text for l in self.ocr(region))
        rec.side = text.parse_side(rec.objective_text)
        point = text.point_phase(rec.objective_text)
        if point:
            rec.stage = point
            return
        # 只有推车阶段（护送/阻止运载目标）才读进度条；闪点、加时等阶段的进度条长得不一样，读出来是错的
        if "运载" not in rec.objective_text:
            return
        fraction, checkpoint = progress.read_bar(hud, rec.map.mode if rec.map else None, rec.side)
        if fraction is not None:
            rec.progress = fraction
            rec.stage = str(checkpoint)


def to_facts(rec: Recognition, hero_name: Callable[[str], str]) -> dict[str, Any]:
    """转成 analyzer / format_facts 使用的 facts 结构。"""
    def person(s: Slot) -> dict[str, Any]:
        alive = s.status == "alive"
        return {"player": s.player, "hero": hero_name(s.label) if alive else None,
                "hero_key": s.label if alive else None, "status": s.status,
                "confidence": round(s.score, 2)}

    unreadable = []
    if rec.map is None:
        unreadable.append(f"map (OCR: {rec.header_text!r})")
    if rec.side is None:
        unreadable.append(f"side (OCR: {rec.objective_text!r})")
    return {
        "map": {"name": rec.map.en if rec.map else None, "zh": rec.map.zh if rec.map else None,
                "confidence": 1.0 if rec.map else 0.0},
        "mode": rec.map.mode if rec.map else None,
        "side": rec.side,
        "segment": {"checkpoint": rec.stage,
                    "progress": f"~{rec.progress * 100:.0f}%" if rec.progress is not None else None,
                    "detail": rec.objective_text or None,
                    "confidence": 1.0 if rec.stage else 0.0},
        "allies": [person(s) for s in rec.slots if s.team == "ally"],
        "enemies": [person(s) for s in rec.slots if s.team == "enemy"],
        "unreadable": unreadable,
    }
