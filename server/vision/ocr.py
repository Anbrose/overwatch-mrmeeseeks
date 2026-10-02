"""RapidOCR 封装：返回 text.Line 列表。模型首次使用时加载（约 1 秒），之后常驻内存。"""
from __future__ import annotations

import threading

import numpy as np

from .text import Line

_engine = None
_lock = threading.Lock()


def _get():
    global _engine
    with _lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR
            # 默认会把检测输入的短边放大到 736：标题条（约 100px 高）会被放大到几万像素宽，
            # 服务器上单次 2–3 秒、峰值近 1GB。短边 96 + 关掉方向分类：准确率不变，0.9 秒、约 420MB。
            _engine = RapidOCR(det_limit_type="min", det_limit_side_len=96, use_cls=False)
        return _engine


def read(img: np.ndarray) -> list[Line]:
    if img.size == 0:
        return []
    res, _ = _get()(img)
    lines = []
    for box, text, _score in res or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append(Line(str(text), (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2))
    return lines
