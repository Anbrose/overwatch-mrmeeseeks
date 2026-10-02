"""推车进度条：蓝色填充占比，以及按红色菱形检查点标记算第几段。

坐标以统一缩放后的屏幕高度 H 为单位。进度条截图本身是屏幕顶部一条，高约 0.18h。
"""
from __future__ import annotations

import numpy as np

from .layout import H

BAR_Y0, BAR_Y1 = 0.08, 0.125     # 进度条所在纵向范围
BAR_HALF_W = 0.30                # 中线左右各搜多宽
MIN_BAR_PX = 0.10 * H            # 至少这么长的彩色像素才算看到了进度条


def _runs(mask: np.ndarray, gap: int = 3, min_len: int = 4) -> list[int]:
    idx = np.where(mask)[0]
    if not len(idx):
        return []
    runs = [[idx[0], idx[0]]]
    for i in idx[1:]:
        if i - runs[-1][1] <= gap:
            runs[-1][1] = i
        else:
            runs.append([i, i])
    return [(a + b) // 2 for a, b in runs if b - a >= min_len]


def read_bar(hud: np.ndarray) -> tuple[float | None, int | None]:
    """返回 (完成比例 0~1, 第几段)。看不到进度条（复活画面、被遮挡、非推车阶段）时返回 (None, None)。"""
    cx = hud.shape[1] / 2
    reg = hud[int(BAR_Y0 * H):int(BAR_Y1 * H), int(cx - BAR_HALF_W * H):int(cx + BAR_HALF_W * H)].astype(int)
    if reg.size == 0:
        return None, None
    b, g, r = reg[..., 0], reg[..., 1], reg[..., 2]
    blue = (b > 200) & (g > 150) & (r < 80)
    red = (r > 140) & (g < 90) & (b < 120) & (r - b > 40)
    bar = blue | red
    y = int(np.argmax(bar.sum(1)))
    if bar[y].sum() < MIN_BAR_PX:
        return None, None

    near = slice(max(0, y - 2), y + 3)
    xs = np.where(bar[near].any(0))[0]
    start, end = xs.min(), xs.max()
    xs_blue = np.where(blue[near].any(0))[0]
    fill = xs_blue.max() if len(xs_blue) else start
    fraction = float(np.clip((fill - start) / max(1, end - start), 0, 1))

    below = reg[y + 4:y + 25]
    diamonds = _runs(((below[..., 2] > 200) & (below[..., 1] < 80) & (below[..., 0] < 120)).any(0))
    checkpoint = 1 + sum(1 for d in diamonds if d < fill)
    return fraction, checkpoint
