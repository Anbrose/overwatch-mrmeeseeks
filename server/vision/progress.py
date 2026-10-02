"""推车进度条：填充占比，以及按检查点菱形算第几段。

坐标以统一缩放后的屏幕高度 H 为单位。进度条截图本身是屏幕顶部一条，高约 0.18h。
只在推车阶段调用（目标文字含「运载」），占点、闪点等阶段的进度条长得不一样。

实测规律（霓虹枢纽、好莱坞、中城，以及一张纯推车图）：
- 进度条左右端位置固定，只随模式不同：混合图左边有 A 点的勾选框，进度条更靠右。
  不能靠颜色找端点：好莱坞的橙红色墙面会被当成红色进度条，起点被拉到很左边。
- 颜色按攻防反转：进攻方是亮蓝填充 + 暗红轨道 + 红色检查点；防守方是亮红填充 + 暗蓝轨道 + 蓝色检查点。
  检查点菱形永远是防守方的颜色，进攻方颜色的标记是运载目标自己的位置，终点是橙色。
"""
from __future__ import annotations

import numpy as np

from .layout import H

GEOMETRY = {                       # 进度条左右端（相对中线）
    "Hybrid": (-0.1431, 0.2451),
    "Escort": (-0.1799, 0.2076),
}
BAR_Y0, BAR_Y1 = 0.08, 0.125       # 纵向范围（会随顶部提示上下浮动，所以按行搜索）
MIN_COVER = 0.3                    # 这一行至少这么多比例是进度条颜色，才算看到了进度条
GAP = 4                            # 填充里允许的断点（被图标/特效遮挡）


def _bright_blue(b, g, r):
    return (b > 200) & (g > 120) & (r < 90)


def _bright_red(b, g, r):
    # 偏粉的红（蓝 ≥ 绿），排除好莱坞墙面那种偏黄的橙红
    return (r > 200) & (g < 100) & (b < 130) & (b >= g - 15)


def _dim_red(b, g, r):
    return (r > 130) & (g < 75) & (b < 120) & (b >= g - 15) & (r - b > 40)


def _dim_blue(b, g, r):
    return (b > 120) & (g > 85) & (g < 140) & (r < 70)


COLORS = {   # side -> (填充, 轨道, 检查点)
    "Attack": (_bright_blue, _dim_red, _bright_red),
    "Defense": (_bright_red, _dim_blue, _bright_blue),
}


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


def _fill_end(fill_cols: np.ndarray) -> int:
    """从起点开始连续（允许小断点）的填充色到哪一列为止；起点不是填充色就是 0。"""
    end = -1
    for x, filled in enumerate(fill_cols):
        if filled:
            end = x
        elif x - end > GAP:
            break
    return max(end, 0)


def read_bar(hud: np.ndarray, mode: str | None, side: str | None) -> tuple[float | None, int | None]:
    """返回 (完成比例 0~1, 第几段)。模式/攻防未知、看不到进度条（复活画面、被遮挡）时返回 (None, None)。"""
    if mode not in GEOMETRY or side not in COLORS:
        return None, None
    fill_fn, track_fn, checkpoint_fn = COLORS[side]
    gx0, gx1 = GEOMETRY[mode]
    cx = hud.shape[1] / 2
    x0, x1 = int(cx + gx0 * H), int(cx + gx1 * H)
    reg = hud[int(BAR_Y0 * H):int(BAR_Y1 * H), x0:x1].astype(int)
    if reg.size == 0:
        return None, None
    b, g, r = reg[..., 0], reg[..., 1], reg[..., 2]
    fill = fill_fn(b, g, r)
    bar = fill | track_fn(b, g, r)
    y = int(np.argmax(bar.sum(1)))
    if bar[y].sum() < MIN_COVER * reg.shape[1]:
        return None, None

    near = slice(max(0, y - 2), y + 3)
    end = _fill_end(fill[near].any(0))
    fraction = float(np.clip(end / max(1, reg.shape[1] - 1), 0, 1))

    below = reg[y + 4:y + 25]
    bb, bg, br = below[..., 0], below[..., 1], below[..., 2]
    diamonds = _runs(checkpoint_fn(bb, bg, br).any(0))
    checkpoints = [d for d in diamonds if d < reg.shape[1] - 15]   # 最右端是终点
    return fraction, 1 + sum(1 for d in checkpoints if d < end)
