"""对阵图：两队队徽和队名左右对称，中间一列是职责（坦克 / 输出 / 全能 / 支援 / 教练），
左队名字靠右、右队名字靠左，朝向中线。宽度固定，手机和电脑上排版一样。

图里只有不会变的内容（阵容、队徽），票数在按钮上，所以每场只画一次。
字体是仓库里的 Noto Sans 可变字体（assets/fonts，OFL 许可）。
"""
from __future__ import annotations

import functools
import io
import logging
import os
from typing import Any

from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger("mrmeeseeks.roster_card")

FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts", "NotoSans.ttf")
WIDTH, HEADER, ROW, GAP, FOOTER = 1000, 300, 54, 22, 70
LEFT, CENTER, RIGHT = 270, 500, 730            # 左队、职责、右队的中心 x
NAME_INSET = 130                               # 选手名字离中线的距离
NAME_MAX = 300                                 # 选手名字最宽
TEAM_MAX = 420                                 # 队名最宽
LOGO_AREA, LOGO_MAX_W, LOGO_MAX_H = 140 * 140, 240, 160
BG, PANEL, LINE = (30, 31, 34), (43, 45, 49), (64, 66, 72)
TEXT, MUTED = (240, 241, 243), (150, 154, 162)
ROLES = {"tank": ("TANK", (88, 166, 255)), "dps": ("DAMAGE", (255, 107, 107)), "flex": ("FLEX", (255, 196, 87)),
         "support": ("SUPPORT", (87, 214, 141)), "coach": ("COACH", (180, 180, 190)), None: ("PLAYER", MUTED)}
ROLE_ORDER = ("tank", "dps", "flex", "support", None, "coach")
FOOTNOTE = "Registered event roster — the starting lineup may differ · Data: Liquipedia (CC-BY-SA)"
UNAVAILABLE = "Roster unavailable"


@functools.lru_cache(maxsize=64)
def font(size: int, weight: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(FONT_PATH, size)
    f.set_variation_by_axes([weight, 100])       # 轴顺序：字重、宽度
    return f


def fit_text(text: str, max_width: int, size: int, weight: int, min_size: int = 20) -> tuple[str, ImageFont.FreeTypeFont]:
    """字号从 size 往下减，直到 text 放得进 max_width；最小字号还放不下就截断加 "…"。"""
    while size > min_size and font(size, weight).getlength(text) > max_width:
        size -= 2
    f = font(size, weight)
    if f.getlength(text) > max_width:
        while text and f.getlength(text + "…") > max_width:
            text = text[:-1]
        text = text.rstrip() + "…"
    return text, f


def role_rows(roster1: list[dict[str, Any]] | None, roster2: list[dict[str, Any]] | None) -> list[tuple[str | None, int]]:
    """[(职责, 行数)]：两队都没有的职责不画，行数取两队人数较多的一方。"""
    def count(roster, role):
        return sum(1 for m in roster or [] if m["role"] == role)
    return [(r, max(count(roster1, r), count(roster2, r))) for r in ROLE_ORDER
            if count(roster1, r) or count(roster2, r)]


def height_for(rows: list[tuple[str | None, int]]) -> int:
    body = sum(n * ROW + GAP for _, n in rows) if rows else ROW + GAP     # 没有阵容时一行说明
    return HEADER + body + GAP + FOOTER


def logo_size(w: int, h: int) -> tuple[int, int]:
    """按面积缩放，让横条、方形、竖长的队徽看起来一样大；再限制最大宽高。"""
    s = (LOGO_AREA / (w * h)) ** 0.5
    s = min(s, LOGO_MAX_W / w, LOGO_MAX_H / h)
    return round(w * s), round(h * s)


def initials(name: str) -> str:
    words = [w for w in name.split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def _logo(path: str | None, team: str) -> Image.Image:
    if path:
        try:
            im = Image.open(path).convert("RGBA")
            im = im.crop(im.getchannel("A").getbbox() or (0, 0, im.width, im.height))   # 去掉透明留白
            return im.resize(logo_size(im.width, im.height), Image.LANCZOS)
        except Exception:
            log.warning("Unreadable logo for %s at %s; using initials", team, path)
    badge = Image.new("RGBA", (130, 130), (0, 0, 0, 0))
    d = ImageDraw.Draw(badge)
    d.ellipse((0, 0, 129, 129), fill=LINE)
    d.text((65, 65), initials(team), font=font(48, 800), fill=TEXT, anchor="mm")
    return badge


def render_card(team1: str, team2: str, roster1: list[dict[str, Any]] | None, roster2: list[dict[str, Any]] | None,
                logo1: str | None, logo2: str | None) -> bytes:
    """画对阵图，返回 PNG。"""
    rows = role_rows(roster1, roster2)
    height = height_for(rows)
    img = Image.new("RGB", (WIDTH, height), BG)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((16, 16, WIDTH - 16, height - 16), 24, fill=PANEL)

    for cx, team, logo in ((LEFT, team1, logo1), (RIGHT, team2, logo2)):
        im = _logo(logo, team)
        img.paste(im, (cx - im.width // 2, 120 - im.height // 2), im)
        name, f = fit_text(team, TEAM_MAX, 46, 800)
        d.text((cx, 240), name, font=f, fill=TEXT, anchor="mm")
    d.text((CENTER, 150), "VS", font=font(52, 900), fill=MUTED, anchor="mm")
    d.line((60, HEADER - 12, WIDTH - 60, HEADER - 12), fill=LINE, width=2)

    y = HEADER + GAP // 2
    if not rows:
        d.text((CENTER, y + ROW / 2), UNAVAILABLE, font=font(34, 500), fill=MUTED, anchor="mm")
        y += ROW + GAP
    for i, (role, n) in enumerate(rows):
        label, color = ROLES[role]
        mid = y + n * ROW / 2
        d.rounded_rectangle((CENTER - 92, mid - 22, CENTER + 92, mid + 22), 22, outline=color, width=3)
        d.text((CENTER, mid), label, font=font(26, 800), fill=color, anchor="mm")
        coach = role == "coach"
        size, weight, fill = (34, 500, MUTED) if coach else (40, 600, TEXT)
        for roster, x, anchor in ((roster1, CENTER - NAME_INSET, "rm"), (roster2, CENTER + NAME_INSET, "lm")):
            if roster is None:
                if i == 0:
                    d.text((x, y + ROW / 2), UNAVAILABLE, font=font(30, 500), fill=MUTED, anchor=anchor)
                continue
            for j, player in enumerate(m["id"] for m in roster if m["role"] == role):
                name, f = fit_text(player, NAME_MAX, size, weight)
                d.text((x, y + j * ROW + ROW / 2), name, font=f, fill=fill, anchor=anchor)
        y += n * ROW + GAP
    d.line((60, y, WIDTH - 60, y), fill=LINE, width=2)
    note, f = fit_text(FOOTNOTE, WIDTH - 100, 22, 500, min_size=16)
    d.text((CENTER, y + FOOTER / 2), note, font=f, fill=MUTED, anchor="mm")

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
