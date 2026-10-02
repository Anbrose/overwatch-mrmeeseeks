"""OverFast API（https://overfast-api.tekrop.fr）玩家查询。

只用两个接口：
  /players/{id}/summary        名字、称号、各职责竞技段位
  /players/{id}/stats/summary  总体和每个英雄的场次、胜率、KDA、时长（默认合并快速和竞技）
BattleTag 里的 # 要换成 -，大小写敏感。生涯私密时 stats 为空。
"""
from __future__ import annotations

import re
from typing import Any

import aiohttp

BASE_URL = "https://overfast-api.tekrop.fr"
TOP_HEROES = 5

# 只允许 BattleTag 里会出现的字符（各语言字母、数字，加可选的 -数字），避免把用户输入拼成别的 API 路径
_TAG_RE = re.compile(r"^\w+(-\d+)?$")

# OverFast 的英雄 key 是 slug，少数英雄的显示名不能靠首字母大写还原
_HERO_NAMES = {
    "dva": "D.Va", "lucio": "Lúcio", "torbjorn": "Torbjörn", "soldier-76": "Soldier: 76",
    "wrecking-ball": "Wrecking Ball", "junker-queen": "Junker Queen",
}


class PlayerNotFound(Exception):
    pass


class OverFastUnavailable(Exception):
    """限流、维护或网络错误，稍后重试即可。"""


def normalize_battletag(raw: str) -> str | None:
    tag = raw.strip().replace("#", "-")
    return tag if tag and _TAG_RE.match(tag) else None


def hero_name(key: str) -> str:
    return _HERO_NAMES.get(key) or key.replace("-", " ").title()


class OverFast:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 20.0):
        self.base_url = base_url
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def _get(self, path: str) -> tuple[int, Any]:
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as s:
                async with s.get(self.base_url + path) as r:
                    return r.status, await r.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise OverFastUnavailable(str(e) or e.__class__.__name__) from e

    async def _get_ok(self, path: str) -> Any:
        status, data = await self._get(path)
        if status == 404:
            raise PlayerNotFound(path)
        if status != 200:
            raise OverFastUnavailable(f"HTTP {status}")
        return data

    async def heroes(self) -> list[dict[str, Any]]:
        """全部英雄：[{key, name, role, ...}]。"""
        return await self._get_ok("/heroes")

    async def player(self, battletag: str) -> tuple[dict[str, Any], dict[str, Any]]:
        summary = await self._get_ok(f"/players/{battletag}/summary")
        stats = await self._get_ok(f"/players/{battletag}/stats/summary")
        return summary, stats or {}


def _ranks(summary: dict[str, Any]) -> str | None:
    comp = summary.get("competitive") or {}
    for platform, label in (("pc", "PC"), ("console", "Console")):
        p = comp.get(platform)
        if not p:
            continue
        parts = []
        for role in ("tank", "damage", "support", "open"):
            if role not in p:
                continue
            r = p.get(role)
            name = "Open Queue" if role == "open" else role.title()
            if r:
                parts.append(f"{name} {r['division'].title()} {r['tier']}")
            elif role != "open":
                parts.append(f"{name} unranked")
        season = p.get("season")
        return f"Competitive ({label}{f', season {season}' if season else ''}): " + " · ".join(parts)
    return None


def _hours(seconds: float) -> str:
    return f"{seconds / 3600:,.0f}h" if seconds >= 3600 else f"{seconds / 60:.0f}m"


def format_player(battletag: str, summary: dict[str, Any], stats: dict[str, Any]) -> str:
    title = summary.get("title")
    endorsement = (summary.get("endorsement") or {}).get("level")
    head = f"**{summary.get('username') or battletag}**"
    if title:
        head += f" · {title}"
    if endorsement:
        head += f" · Endorsement {endorsement}"
    lines = [head]

    ranks = _ranks(summary)
    lines.append(ranks or "Competitive: no rank data")

    general = stats.get("general")
    heroes = stats.get("heroes") or {}
    if not general or not heroes:
        lines.append("No stats available. The career profile may be private "
                     "(Overwatch → Options → Social → Career Profile Visibility → Public).")
        return "\n".join(lines)

    lines.append(f"Overall (all modes): {general['games_played']:,} games · {general['winrate']:.0f}% win · "
                 f"KDA {general['kda']:.2f} · {_hours(general['time_played'])}")
    lines.append(f"Most played heroes:")
    top = sorted(heroes.items(), key=lambda kv: kv[1].get("time_played", 0), reverse=True)[:TOP_HEROES]
    for key, h in top:
        lines.append(f"- **{hero_name(key)}** {_hours(h['time_played'])} · {h['games_played']:,} games · "
                     f"{h['winrate']:.0f}% win · KDA {h['kda']:.2f}")
    return "\n".join(lines)[:2000]
