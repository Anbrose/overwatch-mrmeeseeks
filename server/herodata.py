"""英雄数据：从 Overwatch fandom Wiki 抓取、缓存到本地 JSON、定时刷新、按名字查英雄。

快照只在「看起来正常」时才替换旧的（防止 Wiki 改版导致解析出一堆空数据），
写文件用临时文件 + rename，进程中途被杀也不会留下半个文件。
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from typing import Any, Awaitable, Callable

import aiohttp

from heroparse import parse_hero

log = logging.getLogger("mrmeeseeks.herodata")

WIKI_API = "https://overwatch.fandom.com/api.php"
USER_AGENT = "mrmeeseeks-discord-bot/1.0 (hero stats lookup)"
REQUEST_INTERVAL = 1.0
MIN_HERO_RATIO = 0.9          # 新快照英雄数至少是旧快照的 90%
MAX_WEAPON_RATIO_DROP = 0.10  # 有武器数据的英雄占比最多下降 10 个百分点
ALIASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aliases.json")


def _norm(s: str) -> str:
    """忽略大小写、空格、标点和重音：'Soldier: 76' -> 'soldier76'，'Lúcio' -> 'lucio'。"""
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "", s)


def load_aliases(path: str = ALIASES_PATH) -> dict[str, list[str]]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _weapon_ratio(heroes: dict[str, dict]) -> float:
    return sum(1 for h in heroes.values() if h["weapons"]) / len(heroes) if heroes else 0.0


class HeroStore:
    def __init__(self, path: str, aliases: dict[str, list[str]] | None = None):
        self.path = path
        self.aliases = aliases or {}
        self.heroes: dict[str, dict[str, Any]] = {}
        self.fetched_at: str | None = None

    @property
    def ready(self) -> bool:
        return bool(self.heroes)

    # ---------- 缓存 ----------
    def load(self) -> bool:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            heroes, fetched_at = data["heroes"], data["fetched_at"]
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.info("No usable hero cache at %s (%r)", self.path, e)
            return False
        self.heroes, self.fetched_at = heroes, fetched_at
        log.info("Loaded %d heroes from cache (fetched %s)", len(self.heroes), self.fetched_at)
        return True

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(self.path)), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": self.fetched_at, "heroes": self.heroes}, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ---------- 刷新 ----------
    def accept(self, new: dict[str, dict]) -> bool:
        """新快照是否可以替换当前快照。"""
        if not new:
            return False
        if not self.heroes:
            return True
        if len(new) < MIN_HERO_RATIO * len(self.heroes):
            return False
        return _weapon_ratio(new) >= _weapon_ratio(self.heroes) - MAX_WEAPON_RATIO_DROP

    async def refresh(self, fetch: Callable[[], Awaitable[dict[str, dict]]]) -> bool:
        try:
            new = await fetch()
        except Exception:
            log.exception("Hero data refresh failed; keeping the current snapshot")
            return False
        if not self.accept(new):
            log.warning("Rejected hero snapshot: %d heroes (%.0f%% with weapons) vs current %d (%.0f%%)",
                        len(new), 100 * _weapon_ratio(new), len(self.heroes), 100 * _weapon_ratio(self.heroes))
            return False
        self.heroes = new
        self.fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            self.save()
        except OSError:  # 缓存写不进去（只读文件系统、权限）也继续用内存里的新数据
            log.exception("Could not write hero cache to %s", self.path)
        log.info("Hero data refreshed: %d heroes", len(new))
        return True

    # ---------- 查英雄 ----------
    def resolve(self, name: str) -> dict[str, Any] | list[str]:
        """唯一命中返回英雄 dict；否则返回候选名列表（最多 5 个，可能为空）。"""
        q = _norm(name)
        if not q:
            return []
        keys: dict[str, set[str]] = {}
        for hero in self.heroes:
            for k in [hero, *self.aliases.get(hero, [])]:
                keys.setdefault(_norm(k), set()).add(hero)
        if q in keys and len(keys[q]) == 1:
            return self.heroes[next(iter(keys[q]))]
        for match in (lambda k: k.startswith(q), lambda k: q in k):
            hits = sorted({h for k, hs in keys.items() if match(k) for h in hs})
            if len(hits) == 1 and len(q) >= 2:
                return self.heroes[hits[0]]
            if hits:
                return hits[:5]
        close = difflib.get_close_matches(q, keys.keys(), n=5, cutoff=0.6)
        return sorted({h for k in close for h in keys[k]})[:5]


# ---------- 抓取 ----------
async def _get(session: aiohttp.ClientSession, **params: str) -> dict[str, Any]:
    params = {"format": "json", "formatversion": "2", **params}
    async with session.get(WIKI_API, params=params) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_all(interval: float = REQUEST_INTERVAL) -> dict[str, dict]:
    """抓取全部英雄并解析。单个英雄失败只记日志并跳过。"""
    headers = {"User-Agent": USER_AGENT}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        data = await _get(session, action="query", list="categorymembers",
                          cmtitle="Category:Heroes", cmnamespace="0", cmlimit="500")
        titles = [m["title"] for m in data["query"]["categorymembers"] if m["title"] != "Heroes"]
        heroes: dict[str, dict] = {}
        for title in titles:
            await asyncio.sleep(interval)
            try:
                page = await _get(session, action="parse", page=title, prop="wikitext")
                hero = parse_hero(title, page["parse"]["wikitext"])
            except Exception:
                log.exception("Failed to fetch or parse hero page %s", title)
                continue
            if hero:
                heroes[hero["name"]] = hero
        return heroes


async def refresh_loop(store: HeroStore, hours: float,
                       fetch: Callable[[], Awaitable[dict[str, dict]]] = fetch_all) -> None:
    while True:
        await store.refresh(fetch)
        await asyncio.sleep(hours * 3600)
