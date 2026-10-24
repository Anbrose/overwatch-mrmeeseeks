"""一起看：按片名搜索资源站（采集接口，MacCMS 风格 ?ac=detail&wd=）。

- parse_sources_env：解析 WATCH_VOD_SOURCES（名字=接口地址，逗号分隔）。
- parse_detail：把一个接口的 JSON 变成 Show 列表，只留 https 的 .m3u8 片源。
- merge_shows / rank_shows：多个接口的结果按「片名+年份」合并，再按和搜索词的贴近程度排序。
- VodSearch.search：并发查询所有接口；单个接口出错只记日志，全部出错才报 SearchUnavailable。
"""
import asyncio
import json
import logging
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field

import aiohttp

from live_watch import USER_AGENT

log = logging.getLogger("mrmeeseeks.watch")

DEFAULT_VOD_SOURCES = (
    "创享影视=https://ys.cxkjg.cn/api/provide/vod,"
    "1080资源=https://api.1080zyku.com/inc/apijson.php,"
    "量子=https://cj.lziapi.com/api.php/provide/vod"
)
MAX_RESULTS = 10
UNAVAILABLE_MSG = "资源站暂时无法访问，请稍后再试"


class SearchUnavailable(Exception):
    """所有资源站都查不了，message 可直接展示给用户。"""

    def __init__(self, message: str = UNAVAILABLE_MSG):
        super().__init__(message)
        self.message = message


@dataclass
class Episode:
    label: str
    url: str


@dataclass
class Line:
    name: str
    episodes: list[Episode]


@dataclass
class Show:
    key: str
    title: str
    year: int | None
    type_name: str
    lines: list[Line] = field(default_factory=list)

    @property
    def episode_count(self) -> int:
        return max((len(l.episodes) for l in self.lines), default=0)


def parse_sources_env(raw: str | None) -> list[tuple[str, str]]:
    if raw is None:
        raw = DEFAULT_VOD_SOURCES
    out = []
    for part in raw.split(","):
        name, sep, url = part.partition("=")
        name, url = name.strip(), url.strip()
        if not sep or not name:
            continue
        try:
            scheme = urllib.parse.urlparse(url).scheme.lower()
            host = urllib.parse.urlparse(url).hostname
        except ValueError:
            continue
        if scheme not in ("http", "https") or not host:
            continue
        out.append((name, url))
    return out


def normalize_title(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s or "")).casefold()


def _playable(url: str) -> bool:
    try:
        u = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return u.scheme == "https" and bool(u.hostname) and u.path.lower().endswith(".m3u8")


def _parse_episodes(raw: str) -> list[Episode]:
    eps = []
    for n, seg in enumerate((s for s in raw.split("#") if s.strip()), 1):
        label, sep, url = seg.rpartition("$")
        url = url.strip()
        label = label.strip() if sep else ""
        eps.append(Episode(label or f"第{n:02d}集", url))
    return eps


def parse_detail(resp: dict, api_name: str) -> list[Show]:
    items = resp.get("list") if isinstance(resp, dict) else None
    if not isinstance(items, list):
        return []
    shows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        name, play_url = it.get("vod_name"), it.get("vod_play_url")
        if not isinstance(name, str) or not isinstance(play_url, str) or not name.strip():
            continue
        play_from = it.get("vod_play_from")
        froms = play_from.split("$$$") if isinstance(play_from, str) else []
        urls = play_url.split("$$$")
        lines = []
        for i, raw in enumerate(urls):
            eps = _parse_episodes(raw)
            if not eps or not all(_playable(e.url) for e in eps):
                continue
            src = froms[i].strip() if i < len(froms) else ""
            lines.append(Line(f"{api_name}/{src}" if len(urls) > 1 and src else api_name, eps))
        if not lines:
            continue
        m = re.search(r"\d{4}", str(it.get("vod_year")))
        year = int(m.group()) if m else None
        title = re.sub(r"\s+", " ", name).strip()
        type_name = it.get("type_name")
        shows.append(Show(
            key=normalize_title(name) + "|" + str(year or ""),
            title=title, year=year,
            type_name=type_name.strip() if isinstance(type_name, str) else "",
            lines=lines))
    return shows


def merge_shows(groups: list[list[Show]]) -> list[Show]:
    merged: dict[str, Show] = {}
    for group in groups:
        for s in group:
            if s.key in merged:
                merged[s.key].lines.extend(s.lines)
            else:
                merged[s.key] = Show(s.key, s.title, s.year, s.type_name, list(s.lines))
    return list(merged.values())


def rank_shows(shows: list[Show], query: str) -> list[Show]:
    q = normalize_title(query)

    def key(s: Show):
        t = normalize_title(s.title)
        year = -(s.year or 0)  # 没有年份的排最后
        if t == q:
            return (0, 0, year)
        if t.startswith(q):
            return (1, len(t), year)  # 越短越像正片，「片名[电影解说]」这类自然靠后
        return (2, 0, year)

    # sorted 是稳定排序：完全同分时保持合并顺序
    return sorted(shows, key=key)


def _default_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})


class VodSearch:
    def __init__(self, sources: list[tuple[str, str]], session_factory=_default_session, timeout: float = 8.0):
        self._sources = sources
        self._session_factory = session_factory
        self._timeout = timeout

    async def _fetch(self, session, name: str, url: str, query: str) -> list[Show] | None:
        """None 表示这个接口失败了；[] 表示正常但没有结果。"""
        async def go():
            async with session.get(url, params={"ac": "detail", "wd": query}, allow_redirects=True) as resp:
                if resp.status >= 400:
                    raise ValueError(f"HTTP {resp.status}")
                data = json.loads(await resp.text())
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise ValueError("unexpected payload")
            return parse_detail(data, name)

        try:
            return await asyncio.wait_for(go(), self._timeout)
        except Exception as e:  # noqa: BLE001  任何错误都只意味着这个接口不可用
            log.warning("vod source %s failed: %s", name, type(e).__name__)
            return None

    async def search(self, query: str) -> list[Show]:
        if not self._sources:
            return []
        try:
            async with self._session_factory() as session:
                results = await asyncio.gather(*(self._fetch(session, n, u, query) for n, u in self._sources))
        except Exception as e:  # noqa: BLE001
            log.warning("vod search session failed: %s", type(e).__name__)
            raise SearchUnavailable() from e
        ok = [r for r in results if r is not None]
        if not ok:
            raise SearchUnavailable()
        return rank_shows(merge_shows(ok), query)[:MAX_RESULTS]
