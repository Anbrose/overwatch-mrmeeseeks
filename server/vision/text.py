"""OCR 文本解析：地图/模式、攻防与阶段、昵称分配到行。纯函数，便于测试。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path


@dataclass(frozen=True)
class Line:
    text: str
    cx: float    # 中心点（所在图的绝对坐标）
    cy: float


@dataclass(frozen=True)
class MapInfo:
    en: str
    zh: str
    mode: str


def load_maps(path: Path) -> list[MapInfo]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [MapInfo(m["en"], m["zh"], mode) for mode, maps in data.items() for m in maps]


MAP_MIN_RATIO = 0.6


def match_map(text: str, maps: list[MapInfo]) -> MapInfo | None:
    """先找完整中文名；找不到再在文本里滑动同长度窗口做模糊比对，取唯一最高且 ≥0.6 的。"""
    text = re.sub(r"\s", "", text)
    for m in maps:
        if m.zh in text:
            return m
    scored = []
    for m in maps:
        n = len(m.zh)
        windows = [text[i:i + n] for i in range(max(1, len(text) - n + 1))]
        scored.append((max(SequenceMatcher(None, m.zh, w).ratio() for w in windows), m))
    scored.sort(key=lambda s: s[0], reverse=True)
    if scored and scored[0][0] >= MAP_MIN_RATIO and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    return None


_ATTACK = ("护送", "进攻", "攻击", "占领", "推进")
_DEFENSE = ("阻止", "防守", "防御")


def parse_side(objective: str) -> str | None:
    """目标文字（如「护送运载目标」「进攻目标点A」「准备进攻」）判断攻防。"""
    if any(k in objective for k in _DEFENSE):
        return "Defense"
    if any(k in objective for k in _ATTACK):
        return "Attack"
    return None


def point_phase(objective: str) -> str | None:
    """混合图第一阶段：「进攻目标点A」→ "A"。"""
    m = re.search(r"目标点\s*([A-Z])", objective)
    return m.group(1) if m else None


_NAME_RE = re.compile(r"^[A-Z0-9_]*[A-Z][A-Z0-9_]*$")


def assign_names(lines: list[Line], centers: list[float], tol: float) -> list[str | None]:
    """把 OCR 行分配给最近的行中心（纵向距离 ≤ tol）。只认大写拉丁昵称，过滤等级数字和中文称号。"""
    out: list[str | None] = [None] * len(centers)
    best: list[float] = [tol] * len(centers)
    for line in lines:
        token = re.sub(r"\s", "", line.text).upper()
        if len(token) < 2 or not _NAME_RE.match(token):
            continue
        i = min(range(len(centers)), key=lambda k: abs(centers[k] - line.cy))
        d = abs(centers[i] - line.cy)
        if d <= best[i]:
            out[i], best[i] = token, d
    return out
