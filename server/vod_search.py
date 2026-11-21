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

# 顺序就是线路顺序（见 merge_shows），所以把观众在中国和澳洲都能直接连上 CDN 的放前面。
# 2026-10-30 实测（港剧/TVB 片目，国内 8 地 + 悉尼 4 地探测片段）：创享影视、极速、魔都、百度、iKun 两边都通，
# 且都有粤语线路；1080资源 和 量子 的 CDN 对澳洲 IP 返回 403/404（国内正常），所以排最后，
# 只在前面的都没有这部剧时才会用到。服务器在新加坡，探测通不代表观众那边通。
DEFAULT_VOD_SOURCES = (
    "创享影视=https://ys.cxkjg.cn/api/provide/vod,"
    "极速=https://jszyapi.com/api.php/provide/vod,"
    "魔都=https://caiji.moduapi.cc/api.php/provide/vod,"
    "百度=https://api.apibdzy.com/api.php/provide/vod,"
    "iKun=https://ikunzyapi.com/api.php/provide/vod,"
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
    pic: str | None = None        # 海报（资源站的 vod_pic，只认 https；Discord 消息里当缩略图）

    @property
    def episode_count(self) -> int:
        return max((len(l.episodes) for l in self.lines), default=0)

    @property
    def numbered(self) -> bool:
        """每条线路的每一集都是按顺序编号的（第 1 集、第 2 集…），或者整部只有一集。
        这时「第几集」在所有线路上是同一个意思，可以按序号对应。
        否则（综艺的「第3期上 / 沉浸版 / 加更版」这类带名字的分段）各线路的数量和顺序都可能不同，
        序号只在一条线路内有意义，跨线路要按副标题对应。"""
        if self.episode_count <= 1:
            return True
        return all(episode_number(e.label) == i + 1 for l in self.lines for i, e in enumerate(l.episodes))

    def episode_total(self, line: int) -> int:
        """房间在这条线路上能选的集数。"""
        if self.numbered or not 0 <= line < len(self.lines):
            return self.episode_count
        return len(self.lines[line].episodes)

    def episode_labels(self, line: int) -> list[str] | None:
        """这条线路每一集的副标题；按顺序编号的剧返回 None（界面上直接写数字）。"""
        if self.numbered or not 0 <= line < len(self.lines):
            return None
        return [e.label for e in self.lines[line].episodes]

    def episode_name(self, line: int, episode: int) -> str:
        """给人看的「哪一集」：编号的剧是「第 N 集」，带名字的分段是资源站给的副标题。"""
        if not self.numbered and 0 <= line < len(self.lines) and 0 <= episode < len(self.lines[line].episodes):
            return self.lines[line].episodes[episode].label
        return f"第 {episode + 1} 集"

    def episode_id(self, line: int, episode: int) -> str:
        """换线路也不变的「这一集」的标识（网页端用它记住这一集已经自动试过哪些线路）。"""
        if not self.numbered and 0 <= line < len(self.lines) and 0 <= episode < len(self.lines[line].episodes):
            return episode_key(self.lines[line].episodes[episode].label) or str(episode)
        return str(episode)

    def locate(self, from_line: int, episode: int, to_line: int) -> int | None:
        """from_line 上的第 episode 个，在 to_line 上是第几个；to_line 没有这一集返回 None。"""
        if not 0 <= to_line < len(self.lines):
            return None
        target = self.lines[to_line].episodes
        if self.numbered or to_line == from_line:
            return episode if 0 <= episode < len(target) else None
        if not (0 <= from_line < len(self.lines) and 0 <= episode < len(self.lines[from_line].episodes)):
            return None
        return self.find_label(to_line, self.lines[from_line].episodes[episode].label)

    def find_label(self, line: int, label: str) -> int | None:
        """line 上副标题和 label 对得上的那一集（见 episode_key）；没有返回 None。"""
        if not 0 <= line < len(self.lines):
            return None
        key = episode_key(label)
        if not key:
            return None
        return next((i for i, e in enumerate(self.lines[line].episodes) if episode_key(e.label) == key), None)

    def start_line(self) -> int:
        """建房时先试哪条线路。编号的剧按线路顺序；带名字的分段选集表是跟着线路走的，
        所以从「别的线路最能接上」的那条开始：它的分段在其他线路上按副标题对得上的次数最多，
        这样这条放不出来时换来源才有地方可换；一样多时分段多的、靠前的优先。
        （各站命名不统一：有的按日期「20261029上」，有的按期数「第3期上」，两种互相对不上。）"""
        if self.numbered:
            return 0
        keys = [{episode_key(e.label) for e in l.episodes} - {""} for l in self.lines]

        def score(i: int):
            partners = sum(len(keys[i] & keys[j]) for j in range(len(keys)) if j != i)
            return partners, len(self.lines[i].episodes), -i
        return max(range(len(self.lines)), key=score)


LABEL_MAX = 40
_NUMBERED = re.compile(r"(?:第|ep|e)?0*(\d{1,4})(?:集|话|話|回)?")
_KEY_NOISE = re.compile(r"[\s\-_.:,·()\[\]【】《》第期集版]")


def episode_number(label: str) -> int | None:
    """「第03集」「3」「EP3」→ 3；带别的字（「第3期上」「20261029沉浸版」）或不是集数 → None。"""
    m = _NUMBERED.fullmatch(re.sub(r"\s+", "", unicodedata.normalize("NFKC", label or "")).casefold())
    return int(m.group(1)) if m else None


def episode_key(label: str) -> str:
    """跨线路对应带名字的分段：去掉空白、括号和「第 / 期 / 集 / 版」这些各站写法不一的字再比。
    「第20261029期上」和「20261029上」算同一集；按日期命名和按期数命名的（「第3期上」）对不上，
    宁可认为那条线路没有这一集，也不猜。"""
    return _KEY_NOISE.sub("", unicodedata.normalize("NFKC", label or "").casefold())


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
        label = re.sub(r"\s+", " ", label).strip()[:LABEL_MAX] if sep else ""
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
        pic = it.get("vod_pic")
        pic = pic.strip() if isinstance(pic, str) else ""
        shows.append(Show(
            key=normalize_title(name) + "|" + str(year or ""),
            title=title, year=year,
            type_name=type_name.strip() if isinstance(type_name, str) else "",
            lines=lines,
            pic=pic if pic.startswith("https://") and len(pic) <= 500 else None))
    return shows


def merge_shows(groups: list[list[Show]]) -> list[Show]:
    merged: dict[str, Show] = {}
    for group in groups:
        for s in group:
            if s.key in merged:
                merged[s.key].lines.extend(s.lines)
                if merged[s.key].pic is None:
                    merged[s.key].pic = s.pic
            else:
                merged[s.key] = Show(s.key, s.title, s.year, s.type_name, list(s.lines), s.pic)
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
