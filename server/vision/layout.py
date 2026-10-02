"""计分板几何：统一缩放到高 1440，所有坐标都以屏幕高度 h 为单位、相对屏幕水平中线。

计分板始终居中、按高度缩放，所以带鱼屏和 16:9 用同一组比例。
行的纵向位置固定；表格横向位置会整体偏移（实测 0 或 +0.0195h），所以头像用滑动窗口搜索。
"""
from __future__ import annotations

import cv2
import numpy as np

H = 1440

ALLY_Y, ENEMY_Y = 0.2047, 0.581      # 第 1 行头像中心
PITCH = 0.0611                       # 行距
HALF = 0.029                         # 头像半高
PORTRAIT_X0, PORTRAIT_X1 = -0.592, -0.528   # 表格无偏移时头像左右边
INSET = 0.12                         # 模板四周内缩比例：避开边框和左侧职责色条
SEARCH_X0, SEARCH_X1 = -0.66, -0.47  # 头像横向搜索范围
SEARCH_V = 0.015                     # 纵向容差
NAME_W = 0.28                        # 昵称列宽（从头像右边起）

TEAMS = ("ally", "enemy")
ROWS = range(1, 6)


def decode(data: bytes) -> np.ndarray:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("could not decode image")
    return img


def scale_to(img: np.ndarray, k: float) -> np.ndarray:
    if abs(k - 1) < 1e-3:
        return img
    return cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC)


def row_center_y(team: str, row: int) -> float:
    return ((ALLY_Y if team == "ally" else ENEMY_Y) + (row - 1) * PITCH) * H


def search_band(img: np.ndarray, team: str, row: int) -> tuple[np.ndarray, int, int]:
    """返回 (搜索区域, 区域左上角 x, y)。"""
    cx = img.shape[1] / 2
    yc = row_center_y(team, row)
    x0, x1 = int(cx + SEARCH_X0 * H), int(cx + SEARCH_X1 * H)
    y0, y1 = int(yc - (HALF + SEARCH_V) * H), int(yc + (HALF + SEARCH_V) * H)
    return img[y0:y1, x0:x1], x0, y0


def portrait_box(img_w: int, team: str, row: int, dx: float) -> tuple[int, int, int, int]:
    """表格横向偏移 dx（单位 h）时，内缩后的头像区域 (x0, y0, x1, y1)。模板就是按这个框裁的。"""
    cx = img_w / 2
    yc = row_center_y(team, row)
    x0, x1 = cx + (PORTRAIT_X0 + dx) * H, cx + (PORTRAIT_X1 + dx) * H
    y0, y1 = yc - HALF * H, yc + HALF * H
    ix, iy = (x1 - x0) * INSET, (y1 - y0) * INSET
    return int(x0 + ix), int(y0 + iy), int(x1 - ix), int(y1 - iy)


def dx_of(img_w: int, team: str, row: int, x: int) -> float:
    """模板匹配到的左上角 x（绝对坐标）反推表格横向偏移。"""
    x0_at_zero = portrait_box(img_w, team, row, 0.0)[0]
    return (x - x0_at_zero) / H


def name_column(img: np.ndarray, dx: float) -> tuple[np.ndarray, int, int]:
    """整列昵称区域（双方 10 行），返回 (图, 左上角 x, y)。"""
    cx = img.shape[1] / 2
    x0 = int(cx + (PORTRAIT_X1 + dx) * H)
    x1 = int(x0 + NAME_W * H)
    y0 = int(row_center_y("ally", 1) - HALF * H)
    y1 = int(row_center_y("enemy", 5) + HALF * H)
    return img[y0:y1, x0:x1], x0, y0
