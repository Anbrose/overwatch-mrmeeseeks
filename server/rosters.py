"""赛事报名阵容：从 Liquipedia 赛事页面的参赛卡片解析，缓存到 herodata 卷的 rosters.json。

赛事页面（MediaWiki API action=parse，每 30 秒最多 1 次）里每支参赛队是一张
`team-participant-card`：opponent-full 里是队名和队徽（深色 / 浅色 / 通用三种图），
toggle-area-content-active 是 Main 名单（位置图标 alt 是 Tank / DPS / Support / Flex），
toggle-area-content-inactive 是教练组（member-role-right 写着 Head Coach / Coach / Manager…，只取教练）。

比赛的 tournament 形如 `…/Korea/Regular Season#Week 1`，子页面上可能没有参赛卡片，
所以依次找 `…/Korea/Regular Season`、`…/Korea`、再上一级。
这是本赛事的报名阵容，不是某一场的实际首发。
"""
from __future__ import annotations

import asyncio
import html as htmllib
import json
import logging
import os
import re
import tempfile
import time
from typing import Any, Awaitable, Callable

import esports

log = logging.getLogger("mrmeeseeks.rosters")

CACHE_TTL = 24 * 3600            # 同一赛事页面一天最多抓一次
PARSE_INTERVAL = 30.0            # Liquipedia：action=parse 每 30 秒最多 1 次
MAX_PAGES = 3
ROLES = {"tank": "tank", "dps": "dps", "support": "support", "flex": "flex"}
ROLE_ORDER = ("tank", "dps", "flex", "support", "coach")
COACH_TITLES = ("head coach", "coach")       # 主教练排在前面
LOGO_SIZE = 300                              # 队徽取 300px 缩略图，画图时缩小

_CARD = 'team-participant-card"><div>'
_TEAM = re.compile(r'team-participant-card__opponent-full.*?<span class="name"[^>]*><a [^>]*title="([^"]+)"', re.S)
_MEMBER = 'class="team-participant-card__member"'
_ROLE = re.compile(r'member-role-left">(?:<[^>]*>)*?<img[^>]*alt="([^"]*)"')
_FLAG = re.compile(r'class="flag">(?:<[^>]*>)*?<img[^>]*alt="([^"]*)"')
_PLAYER = re.compile(r'<span class="name"[^>]*><a [^>]*>([^<]+)</a>')
_STAFF_ROLE = re.compile(r'member-role-right">([^<]+)<')
_LOGO = re.compile(r'<span class="team-template-image-icon(?: team-template-(\w+))?"><a [^>]*><img[^>]*src="([^"]+)"')
_THUMB_SIZE = re.compile(r"/\d+px-")


def _cards(page_html: str) -> list[tuple[str, str]]:
    """[(队名, 卡片 HTML)]"""
    out = []
    for card in htmllib.unescape(page_html).split(_CARD)[1:]:
        team = _TEAM.search(card)
        if team:
            out.append((team.group(1).replace(" (page does not exist)", "").strip(), card))
    return out


def _coaches(staff: str) -> list[dict[str, Any]]:
    found = []
    for chunk in staff.split(_MEMBER)[1:]:
        player, title = _PLAYER.search(chunk), _STAFF_ROLE.search(chunk)
        if player and title and title.group(1).strip().lower() in COACH_TITLES:
            found.append((COACH_TITLES.index(title.group(1).strip().lower()), player.group(1).strip()))
    return [{"id": name, "role": "coach", "flag": None} for _, name in sorted(found, key=lambda x: x[0])]


def parse_rosters(page_html: str) -> dict[str, list[dict[str, Any]]]:
    """赛事页面 -> {队名: [{"id", "role", "flag"}]}：Main 名单，后面跟着教练（role 为 coach）。"""
    teams: dict[str, list[dict[str, Any]]] = {}
    for team, card in _cards(page_html):
        main, _, staff = card.partition("toggle-area-content-inactive")
        main = main.split("toggle-area-content-active", 1)[-1]
        members = []
        for chunk in main.split(_MEMBER)[1:]:
            player = _PLAYER.search(chunk)
            if not player:
                continue
            role = _ROLE.search(chunk)
            flag = _FLAG.search(chunk)
            members.append({"id": player.group(1).strip(),
                            "role": ROLES.get(role.group(1).strip().lower()) if role else None,
                            "flag": flag.group(1).strip() if flag and flag.group(1).strip() else None})
        if members:
            teams[team] = members + _coaches(staff)
    return teams


def parse_logos(page_html: str) -> dict[str, str]:
    """赛事页面 -> {队名: 队徽图片地址}；优先深色模式（画图用深色背景），其次通用、浅色。"""
    logos = {}
    for team, card in _cards(page_html):
        full = card.split("team-participant-card__opponent-full", 1)[-1]
        full = full.split('<span class="name"', 1)[0]
        found = {mode or "allmode": src for mode, src in _LOGO.findall(full)}
        src = found.get("darkmode") or found.get("allmode") or found.get("lightmode")
        if src:
            src = _THUMB_SIZE.sub(f"/{LOGO_SIZE}px-", src) if "/thumb/" in src else src
            logos[team] = src if src.startswith("http") else esports.LIQUIPEDIA + src
    return logos


def parse_page(page_html: str) -> dict[str, Any]:
    return {"teams": parse_rosters(page_html), "logos": parse_logos(page_html)}


def roster_pages(tournament: str) -> list[str]:
    """比赛的赛事路径 -> 依次尝试的赛事页面（最具体的在前，最多 MAX_PAGES 个）。"""
    parts = [p for p in tournament.split("#", 1)[0].split("/") if p]
    pages = []
    while parts and len(pages) < MAX_PAGES:
        pages.append("/".join(parts))
        parts = parts[:-1]
    return pages


def format_roster(members: list[dict[str, Any]] | None) -> str:
    """🛡️ A, B · ⚔️ C, D · 💉 E, F；没有阵容时返回 'Roster unavailable'。"""
    if not members:
        return "Roster unavailable"
    icons = {"tank": "🛡️", "dps": "⚔️", "flex": "🔀", "support": "💉", "coach": "📋", None: "❔"}
    groups: dict[str | None, list[str]] = {}
    for m in members:
        groups.setdefault(m["role"], []).append(m["id"])
    order = [r for r in ROLE_ORDER if r in groups] + ([None] if None in groups else [])
    return " · ".join(f"{icons[r]} {', '.join(groups[r])}" for r in order)


async def _fetch_page(page: str) -> dict[str, Any]:
    """{"teams": …, "logos": …}"""
    data = await esports.parse_page(page)        # 和赛程共用 action=parse 限速
    if "error" in data:          # 页面不存在
        return {"teams": {}, "logos": {}}
    return parse_page((data.get("parse") or {}).get("text") or "")


class RosterCache:
    """path 为 None 时只存在内存里（测试用）。fetch / clock / sleep 可替换，方便测试限速。"""

    def __init__(self, path: str | None,
                 fetch: Callable[[str], Awaitable[dict[str, Any]]] = _fetch_page,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.path = path
        self.fetch, self.clock, self.sleep = fetch, clock, sleep
        self.pages: dict[str, dict[str, Any]] = {}
        self._last_fetch = float("-inf")

    def load(self) -> None:
        if self.path is None:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.pages = {k: v for k, v in data.items() if isinstance(v, dict) and "teams" in v}
        except (OSError, ValueError, TypeError) as e:
            log.info("No usable roster cache at %s (%r)", self.path, e)

    def save(self) -> None:
        if self.path is None:
            return
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.pages, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write roster cache to %s", self.path)

    def _fresh(self, page: str) -> bool:
        entry = self.pages.get(page)
        return (entry is not None and "logos" in entry          # 旧版缓存没有队徽，当作过期
                and self.clock() - entry["fetched_at"] < CACHE_TTL)

    async def ensure(self, matches: list[dict[str, Any]]) -> None:
        """为这些比赛准备阵容：找到包含双方的页面就停；过期或没缓存的页面才抓，抓之间至少隔 30 秒。"""
        changed = False
        for m in matches:
            if self.lookup(m) != (None, None) and all(self._fresh(p) for p in self._pages_with(m)):
                continue
            for page in roster_pages(m.get("tournament") or ""):
                if not self._fresh(page):
                    wait = PARSE_INTERVAL - (self.clock() - self._last_fetch)
                    if wait > 0:
                        await self.sleep(wait)
                    self._last_fetch = self.clock()
                    try:
                        parsed = await self.fetch(page)
                    except Exception:
                        log.exception("Roster fetch failed for %s; keeping cached data", page)
                        if page not in self.pages:
                            continue
                    else:
                        self.pages[page] = {"fetched_at": self.clock(), "teams": parsed["teams"],
                                            "logos": parsed["logos"]}
                        changed = True
                teams = self.pages[page]["teams"]
                if m["team1"] in teams and m["team2"] in teams:
                    break
        if changed:
            self.save()

    def _pages_with(self, m: dict[str, Any]) -> list[str]:
        return [p for p in roster_pages(m.get("tournament") or "")
                if p in self.pages and (m["team1"] in self.pages[p]["teams"] or m["team2"] in self.pages[p]["teams"])]

    def _find(self, m: dict[str, Any], field: str) -> tuple[Any, Any]:
        """只读缓存：按候选页面依次找，第一个包含该队的页面就用它。"""
        out: list[Any] = []
        for team in (m["team1"], m["team2"]):
            found = None
            for page in roster_pages(m.get("tournament") or ""):
                values = (self.pages.get(page) or {}).get(field) or {}
                if team in values:
                    found = values[team]
                    break
            out.append(found)
        return out[0], out[1]

    def lookup(self, m: dict[str, Any]) -> tuple[list | None, list | None]:
        return self._find(m, "teams")

    def logos(self, m: dict[str, Any]) -> tuple[str | None, str | None]:
        return self._find(m, "logos")
