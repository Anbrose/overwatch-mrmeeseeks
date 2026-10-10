"""官方补丁说明：抓取 overwatch.blizzard.com 的补丁页并解析成结构化数据。

补丁页只显示当月的补丁，所以同时抓上个月的归档页（/live/YYYY/MM/），保证月初也能看到最近几次补丁。

每个补丁：
  {"id": "patch-2026-10-06", "date": "2026-10-06", "title": "...", "url": "...#patch-2026-10-06",
   "text": "整篇补丁的纯文本（## 分区、### 条目、- 改动）",
   "heroes": {"Cassidy": ["[Damage]", "Developer Comments: ...", "Deadeye:", "- Now prevents ..."], ...},
   "blocks": [{"hero": "Roadhog", "icon": "...", "entries": [{"name": "Chain Hook", "icon": "..."}, ...]}, ...]}

blocks 按页面顺序记录每个英雄条目（同一英雄可能出现在几个分区里）和其中的技能/威能名，
用来和韩服、国服页面逐项对齐，拿到官方的韩文、简体中文名称（见 translations()）。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Any

import aiohttp

log = logging.getLogger("mrmeeseeks.patchnotes")

BASE_URL = "https://overwatch.blizzard.com/en-us/news/patch-notes/"
USER_AGENT = "mrmeeseeks-discord-bot/1.0 (https://github.com/Anbrose/overwatch-mrmeeseeks)"
KEEP_PATCHES = 8          # 只保留最近几次补丁，给问答和推送用
KO_URL = "https://overwatch.blizzard.com/ko-kr/news/patch-notes/"
CN_URL = "https://ow.blizzard.cn/news/patch-notes/"   # 国服（网易）：简体中文官方译名

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
        self.block: dict[str, Any] | None = None
        self.icon: dict[str, str | None] = {"hero": None, "ability": None}   # 下一个英雄/技能名前面的图标
        self.general_depth: int | None = None    # 在 PatchNotesHeroUpdate-generalUpdates 里时是它的深度

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
            self.section, self.hero, self.in_ability, self.block = text, None, False, None
            self._line(f"## {text}")
        elif kind == "hero":
            self.hero, self.in_ability = text, False
            self.block = {"hero": text, "icon": self.icon["hero"], "entries": []}
            self.patch["blocks"].append(self.block)
            self.patch["lines"].append(f"### {text}")
            self.patch["heroes"].setdefault(text, []).append(f"[{self.section}]" if self.section else "[General]")
        elif kind == "item":
            self.hero, self.in_ability, self.block = None, False, None
            self._line(f"### {text}")
        elif kind == "ability":
            self.in_ability = True
            if self.block is not None:
                self.block["entries"].append({"name": text, "icon": self.icon["ability"]})
            self._line(f"{text}:")
        elif kind == "perk":     # 英雄条目里不带图标的小标题，通常是威能（"Scrap Hook – Minor Perk"）
            self.in_ability = False
            if self.block is not None:
                self.block["entries"].append({"name": text, "icon": None})
            self._line(text)
        elif kind == "li":
            self._line(("  - " if self.in_ability else "- ") + text)
        else:   # 段落
            self._line(text)

    # ---------- HTMLParser ----------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        cls = dict(attrs).get("class") or ""
        if tag == "img":
            if "PatchNotesHeroUpdate-icon" in cls.split():
                self.icon["hero"] = dict(attrs).get("src")
            elif "PatchNotesAbilityUpdate-icon" in cls.split():
                self.icon["ability"] = dict(attrs).get("src")
            return
        if tag in ("br", "hr", "input", "meta", "link", "source"):
            return
        self.depth += 1
        if "PatchNotesHeroUpdate-generalUpdates" in cls.split():
            self.general_depth = self.depth
        if "PatchNotesHeroUpdate-header" in cls.split():
            self.icon["hero"] = None
        if "PatchNotesAbilityUpdate" in cls.split():
            self.icon["ability"] = None
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
        if tag == "li":
            self.capture, self.buf = ("li", self.depth), []
        elif tag == "p":
            self.capture, self.buf = ("perk" if self.general_depth is not None else "p", self.depth), []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("img", "br", "hr", "input", "meta", "link", "source"):
            return
        if self.capture is not None and self.depth == self.capture[1]:
            kind = self.capture[0]
            self.capture = None
            self._emit(kind, "".join(self.buf))
        if self.general_depth == self.depth:
            self.general_depth = None
        self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.capture is not None:
            self.buf.append(data)

    def _start_patch(self) -> None:
        self.patch = {"id": None, "date": None, "title": "", "date_text": "", "lines": [], "heroes": {},
                      "blocks": []}
        self.patches.append(self.patch)
        self.section, self.hero, self.in_ability, self.block = "", None, False, None
        self.general_depth = None


def _title_date(title: str) -> str | None:
    """没有锚点的页面（国服）从标题里取日期："2026年10月7日"、"October 6, 2026"、"2026년 10월 6일"。"""
    m = re.search(r"(\d{4})\s*[年년]\s*(\d{1,2})\s*[月월]\s*(\d{1,2})\s*[日일]", title)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"([A-Z][a-z]+) (\d{1,2}), (\d{4})", title)
    if m:
        try:
            return datetime.strptime(" ".join(m.groups()), "%B %d %Y").date().isoformat()
        except ValueError:
            return None
    return None


def parse_page(page_html: str) -> list[dict[str, Any]]:
    """补丁页 HTML -> 补丁列表（页面上的顺序，通常新的在前）。没有锚点就从标题取日期，都没有的块丢弃。"""
    parser = _Parser()
    parser.feed(page_html)
    patches = []
    for p in parser.patches:
        if not p["id"]:
            day = _title_date(p["title"])
            if not day:
                continue
            p["id"], p["date"] = f"patch-{day}", day
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


# ---------- 三语名称对照（英文 / 简体中文 / 韩文） ----------
_PERK = re.compile(r"\s*(?:–|-|——|—)\s*((?:Minor|Major) Perk|[次主]要威能|次级威能|(?:보조|주요) 특전)\s*$")


def split_perk(name: str) -> tuple[str, str | None]:
    """"Scrap Hook – Minor Perk" -> ("Scrap Hook", "Minor Perk")；不是威能时第二项为 None。"""
    m = _PERK.search(name)
    return (name[:m.start()].strip(), m.group(1)) if m else (name, None)


def _align(en_blocks: list[dict], other: list[dict] | None, by_icon: bool) -> list[dict | None]:
    """给每个英文英雄条目找到另一种语言里对应的条目。

    by_icon：韩服和美服用同一套图标，按图标对齐最可靠；国服图标换了托管地址，只能按顺序对齐，
    条目总数不一致时整篇放弃（宁可留空，也不张冠李戴）。"""
    if not other:
        return [None] * len(en_blocks)
    if by_icon:
        pool = {}
        for b in other:
            pool.setdefault(b["icon"], []).append(b)
        return [pool[b["icon"]].pop(0) if b["icon"] and pool.get(b["icon"]) else None for b in en_blocks]
    if len(other) != len(en_blocks):
        return [None] * len(en_blocks)
    return list(other)


def _icon_mismatch(mine: dict[str, Any], theirs: dict[str, Any], lang: str) -> bool:
    """韩服技能有图标时必须和美服是同一张；国服图标地址不同，不比较。"""
    return lang == "ko" and bool(mine["icon"]) and mine["icon"] != theirs["icon"]


def translations(en: dict[str, Any], ko: dict[str, Any] | None = None,
                 cn: dict[str, Any] | None = None) -> dict[str, Any]:
    """英文补丁 + 韩服、国服同一补丁 -> 名称对照表：
    {"heroes": {"Roadhog": {"zh": "路霸", "ko": "로드호그"}},
     "names": {"Roadhog": {"Chain Hook": {"zh": "链钩", "ko": "사슬 갈고리"}, ...}}}
    威能名去掉 "– Minor Perk" 这类后缀。找不到的语言是空字符串。"""
    heroes: dict[str, dict[str, str]] = {}
    names: dict[str, dict[str, dict[str, str]]] = {}
    pairs = {"ko": _align(en["blocks"], ko and ko["blocks"], by_icon=True),
             "zh": _align(en["blocks"], cn and cn["blocks"], by_icon=False)}
    for i, block in enumerate(en["blocks"]):
        hero = heroes.setdefault(block["hero"], {"zh": "", "ko": ""})
        table = names.setdefault(block["hero"], {})
        for lang, matches in pairs.items():
            other = matches[i]
            if other is None:
                continue
            hero[lang] = hero[lang] or other["hero"]
            if len(other["entries"]) != len(block["entries"]):
                continue     # 条目数对不上时不猜
            for mine, theirs in zip(block["entries"], other["entries"]):
                if _icon_mismatch(mine, theirs, lang):
                    continue
                name, kind = split_perk(mine["name"])
                other_name, other_kind = split_perk(theirs["name"])
                if (kind is None) != (other_kind is None):
                    continue     # 一边是威能一边不是：顺序错位了
                entry = table.setdefault(name, {"zh": "", "ko": ""})
                entry[lang] = entry[lang] or other_name
        for entry in block["entries"]:
            table.setdefault(split_perk(entry["name"])[0], {"zh": "", "ko": ""})
    return {"heroes": heroes, "names": names}


def find_match(patches: list[dict[str, Any]], en: dict[str, Any]) -> dict[str, Any] | None:
    """在另一种语言的补丁里找同一个补丁：同 id，或者日期晚 0–1 天（国服按北京时间）且英雄条目数相同。"""
    same = next((p for p in patches if p["id"] == en["id"]), None)
    if same:
        return same
    day = date.fromisoformat(en["date"])
    near = [p for p in patches if timedelta(0) <= date.fromisoformat(p["date"]) - day <= timedelta(days=1)]
    return next((p for p in near if len(p["blocks"]) == len(en["blocks"])), near[0] if near else None)


def _month_url(base: str, day: date) -> str:
    return f"{base}live/{day.year}/{day.month:02d}/"


async def fetch_translations(en: dict[str, Any]) -> dict[str, Any]:
    """抓韩服和国服里同一补丁所在月份的页面，生成名称对照表。某一边失败时那种语言留空。"""
    day = date.fromisoformat(en["date"])
    found: dict[str, dict[str, Any] | None] = {"ko": None, "cn": None}
    async with _session() as session:
        for lang, base, days in (("ko", KO_URL, (day,)), ("cn", CN_URL, (day, day + timedelta(days=1)))):
            patches: list[dict[str, Any]] = []
            for url in dict.fromkeys(_month_url(base, d) for d in days):
                try:
                    patches += parse_page(await _page(session, url))
                except (aiohttp.ClientError, TimeoutError):
                    log.warning("Could not fetch %s patch notes from %s", lang, url)
            found[lang] = find_match(patches, en)
            if found[lang] is None:
                log.warning("No %s patch notes found for %s", lang, en["id"])
    return translations(en, found["ko"], found["cn"])
