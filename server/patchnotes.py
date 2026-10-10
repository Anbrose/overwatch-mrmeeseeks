"""官方补丁说明：抓取 overwatch.blizzard.com 的补丁页并解析成结构化数据。

补丁页只显示当月的补丁，所以同时抓上个月的归档页（/live/YYYY/MM/），保证月初也能看到最近几次补丁。

每个补丁：
  {"id": "patch-2026-10-06", "date": "2026-10-06", "title": "...", "url": "...#patch-2026-10-06",
   "text": "整篇补丁的纯文本（## 分区、### 条目、- 改动）",
   "heroes": {"Cassidy": ["[Damage]", "Developer Comments: ...", "Deadeye:", "- Now prevents ..."], ...}}
"""
from __future__ import annotations

import logging
import re
from datetime import date
from html.parser import HTMLParser
from typing import Any

import aiohttp

log = logging.getLogger("mrmeeseeks.patchnotes")

BASE_URL = "https://overwatch.blizzard.com/en-us/news/patch-notes/"
USER_AGENT = "mrmeeseeks-discord-bot/1.0 (https://github.com/Anbrose/overwatch-mrmeeseeks)"
KEEP_PATCHES = 8          # 只保留最近几次补丁，给问答和推送用

_PATCH_ID = re.compile(r"patch-(\d{4})-(\d{2})-(\d{2})")


class PatchNotesUnavailable(Exception):
    """页面抓到了却解析不出补丁：多半是官网改版。"""


class _Parser(HTMLParser):
    """按出现顺序把补丁页转成行。只认几种 class，其余标签只取文字。"""

    # class -> 行的格式
    HEADINGS = {
        "PatchNotes-patchTitle": "title",
        "PatchNotes-date": "date",
        "PatchNotes-sectionTitle": "section",
        "PatchNotesHeroUpdate-name": "hero",
        "PatchNotesGeneralUpdate-title": "item",
        "PatchNotesMapUpdate-name": "item",
        "PatchNotesAbilityUpdate-name": "ability",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.patches: list[dict[str, Any]] = []
        self.patch: dict[str, Any] | None = None
        self.depth = 0
        self.capture: tuple[str, int] | None = None   # (种类, 开始时的深度)
        self.buf: list[str] = []
        self.section = ""
        self.hero: str | None = None
        self.in_ability = False

    # ---------- 行 ----------
    def _line(self, text: str, hero_line: str | None = None) -> None:
        if self.patch is None or not text:
            return
        self.patch["lines"].append(text)
        if self.hero is not None:
            self.patch["heroes"].setdefault(self.hero, []).append(hero_line or text)

    def _emit(self, kind: str, text: str) -> None:
        text = re.sub(r"\s+", " ", text).strip()
        if self.patch is None or not text:
            return
        if kind == "title":
            self.patch["title"] = text
        elif kind == "date":
            self.patch["date_text"] = text
        elif kind == "section":
            self.section, self.hero, self.in_ability = text, None, False
            self._line(f"## {text}")
        elif kind == "hero":
            self.hero, self.in_ability = text, False
            self.patch["lines"].append(f"### {text}")
            self.patch["heroes"].setdefault(text, []).append(f"[{self.section}]" if self.section else "[General]")
        elif kind == "item":
            self.hero, self.in_ability = None, False
            self._line(f"### {text}")
        elif kind == "ability":
            self.in_ability = True
            self._line(f"{text}:")
        elif kind == "li":
            self._line(("  - " if self.in_ability else "- ") + text)
        else:   # 段落
            self._line(text)

    # ---------- HTMLParser ----------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("img", "br", "hr", "input", "meta", "link", "source"):
            return
        self.depth += 1
        cls = dict(attrs).get("class") or ""
        if tag == "div" and "PatchNotes-patch " in cls + " ":
            self._start_patch()
        if tag == "div" and dict(attrs).get("class") == "anchor" and self.patch is not None:
            m = _PATCH_ID.fullmatch(dict(attrs).get("id") or "")
            if m:
                self.patch["id"] = m.group(0)
                self.patch["date"] = "-".join(m.groups())
        if self.capture is not None:
            return
        for name, kind in self.HEADINGS.items():
            if name in cls.split():
                self.capture, self.buf = (kind, self.depth), []
                return
        if tag in ("li", "p"):
            self.capture, self.buf = (tag if tag == "li" else "p", self.depth), []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("img", "br", "hr", "input", "meta", "link", "source"):
            return
        if self.capture is not None and self.depth == self.capture[1]:
            kind = self.capture[0]
            self.capture = None
            self._emit(kind, "".join(self.buf))
        self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.capture is not None:
            self.buf.append(data)

    def _start_patch(self) -> None:
        self.patch = {"id": None, "date": None, "title": "", "date_text": "", "lines": [], "heroes": {}}
        self.patches.append(self.patch)
        self.section, self.hero, self.in_ability = "", None, False


def parse_page(page_html: str) -> list[dict[str, Any]]:
    """补丁页 HTML -> 补丁列表（页面上的顺序，通常新的在前）。没有 id 的块丢弃。"""
    parser = _Parser()
    parser.feed(page_html)
    patches = []
    for p in parser.patches:
        if not p["id"]:
            continue
        lines = p.pop("lines")
        p["text"] = "\n".join(lines)
        p["url"] = f"{BASE_URL}#{p['id']}"
        p.pop("date_text", None)
        patches.append(p)
    return patches


def previous_month(today: date) -> tuple[int, int]:
    return (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)


def merge(*pages: list[dict[str, Any]], keep: int = KEEP_PATCHES) -> list[dict[str, Any]]:
    """多页合并、按 id 去重，新的在前，最多 keep 个。"""
    seen: dict[str, dict[str, Any]] = {}
    for page in pages:
        for p in page:
            seen.setdefault(p["id"], p)
    return sorted(seen.values(), key=lambda p: p["date"], reverse=True)[:keep]


def _session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
                                 timeout=aiohttp.ClientTimeout(total=30))


async def _page(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.text()


async def fetch_recent(today: date | None = None) -> list[dict[str, Any]]:
    """当月 + 上个月的补丁，新的在前。当月页有内容却一个补丁都解析不出来时抛 PatchNotesUnavailable。"""
    today = today or date.today()
    year, month = previous_month(today)
    async with _session() as session:
        current_html = await _page(session, BASE_URL)
        current = parse_page(current_html)
        if not current:
            raise PatchNotesUnavailable(f"no patches parsed from {BASE_URL} ({len(current_html)} chars)")
        try:
            older = parse_page(await _page(session, f"{BASE_URL}live/{year}/{month:02d}/"))
        except aiohttp.ClientError:
            log.warning("Could not fetch patch notes for %d-%02d; using the current month only", year, month)
            older = []
    return merge(current, older)
