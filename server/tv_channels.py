"""一起看电视直播：频道表。

从公开的 IPTV m3u 列表建表：解析列表、名称归一化和别名、把用户输入匹配到频道、
落盘（STATE_DIR/tv_channels.json）和定期刷新（启动时一次，之后每 6 小时）。
真正的探测由外部注入的 prober 完成（tv_stream.probe），本模块只负责调度和记录结果。
所有列表下载都走 tv_net.open_url，和其它出站请求一样过 ip_allowed。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
import urllib.parse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from tv_net import is_public_ip, make_session, open_url, read_capped

log = logging.getLogger("mrmeeseeks.watch")

DEFAULT_TV_LISTS = (
    "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/ipv4/result.m3u,"
    "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u,"
    "https://raw.githubusercontent.com/suxuang/myIPTV/main/ipv4.m3u"
)
REFRESH_SECONDS = 6 * 3600
MAX_LIST_BYTES = 10 * 1024 * 1024
PROBE_CONCURRENCY = 16
MAX_PARTIAL = 5

Prober = Callable[["Candidate"], Awaitable[bool]]


@dataclass
class Entry:
    name: str
    tvg_name: str | None
    group: str | None
    ua: str | None
    url: str
    list_index: int


@dataclass
class Candidate:
    url: str
    ua: str | None
    list_index: int
    is_4k: bool
    ok: bool | None = None          # None = 没探测过
    checked_at: float | None = None


@dataclass
class Channel:
    key: str                        # normalize_name() 之后的规范名
    name: str                       # 显示名
    candidates: list[Candidate]

    @property
    def must_have(self) -> bool:
        return self.key.endswith("卫视") or self.key.startswith("CCTV")

    @property
    def all_failed(self) -> bool:
        return bool(self.candidates) and all(c.ok is False for c in self.candidates)

    def ordered(self) -> list[Candidate]:
        """可播的在前，没探测过的次之，探测失败的最后；每组里 4K 靠后，再按列表序和出现序。"""
        rank = {True: 0, None: 1, False: 2}
        idx = {id(c): i for i, c in enumerate(self.candidates)}
        return sorted(self.candidates,
                      key=lambda c: (rank[c.ok], c.is_4k, c.list_index, idx[id(c)]))


@dataclass
class Match:
    exact: Channel | None
    partial: list[Channel]


def parse_sources_env(raw: str | None) -> list[str]:
    """None（环境变量没设）用默认列表；空串表示关闭电视。"""
    if raw is None:
        raw = DEFAULT_TV_LISTS
    return [s.strip() for s in raw.split(",") if s.strip()]


# ---- 解析 ----

_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')


def _url_ok(url: str) -> bool:
    if not re.match(r"https?://", url, re.I):
        return False
    try:
        netloc = urllib.parse.urlsplit(url).netloc
    except ValueError:
        return False            # 坏的 IPv6 字面量之类
    return bool(netloc) and "[" not in netloc


def parse_m3u(text: str, list_index: int) -> list[Entry]:
    entries: list[Entry] = []
    pending: tuple[str, str | None, str | None, str | None] | None = None
    for line in text.lstrip("﻿").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            body = line[len("#EXTINF"):]
            name = body.rsplit(",", 1)[1].strip() if "," in body else ""
            attrs = {k.lower(): v for k, v in _ATTR_RE.findall(body)}
            pending = (name, attrs.get("tvg-name") or None, attrs.get("group-title") or None,
                       attrs.get("http-user-agent") or None)
        elif line.startswith("#"):
            continue            # EXTVLCOPT、KODIPROP、被注释掉的地址等
        elif pending is not None:
            name, tvg, group, ua = pending
            pending = None
            if _url_ok(line):
                entries.append(Entry(name, tvg, group, ua, line, list_index))
    return entries


# ---- 名称归一化和别名 ----

_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]|（[^）]*）|【[^】]*】")
_SUFFIX = re.compile(r"(高清|超清|标清|HD|FHD|UHD|4K|8K|频道)$")
_CCTV = re.compile(r"^CCTV(\d{1,2})(\+)?")
_CCTV_K = ("CCTV4K", "CCTV8K")


def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = _BRACKETS.sub("", s)
    s = re.sub(r"[\s\-_]+", "", s).upper()
    while s not in _CCTV_K:
        m = _SUFFIX.search(s)
        if not m or m.start() == 0:
            break
        s = s[:m.start()]
    if s not in _CCTV_K:
        m = _CCTV.match(s)
        if m:
            s = "CCTV" + m.group(1) + (m.group(2) or "")
    return s


def _build_aliases() -> dict[str, str]:
    a = {
        "HUNANTV": "湖南卫视", "DRAGONTV": "东方卫视", "DRAGONTVINTERNATIONAL": "东方卫视",
        "ZHEJIANGTV": "浙江卫视", "ZHEJIANGTVINTERNATIONAL": "浙江卫视", "JIANGSUTV": "江苏卫视",
        "BRTV北京卫视": "北京卫视", "SHENZHENSATELLITETV": "深圳卫视", "芒果台": "湖南卫视",
    }
    digits = "一二三四五六七八九十"
    for n in range(1, 18):
        zh = digits[n - 1] if n <= 10 else "十" + digits[n - 11]
        a[f"央视{zh}套"] = a[f"中央{zh}套"] = f"CCTV{n}"
    return a


ALIASES: dict[str, str] = _build_aliases()


def canonical_key(name: str) -> str:
    n = normalize_name(name)
    return ALIASES.get(n, n)


def _province_aliases(key: str) -> list[str]:
    if key.endswith("卫视") and len(key) > 2:
        p = key[:-2]
        return [p, p + "台"]
    return []


def _aliases_of(key: str) -> list[str]:
    return [a for a, v in ALIASES.items() if v == key] + _province_aliases(key)


# ---- 建表 ----

def _clean_display(raw: str) -> str:
    return re.sub(r"\s+", " ", _BRACKETS.sub("", unicodedata.normalize("NFKC", raw))).strip()


def build_channels(entries: list[Entry],
                   previous: dict[str, Candidate] | None = None) -> dict[str, Channel]:
    previous = previous or {}
    channels: dict[str, Channel] = {}
    names: dict[str, Counter] = {}
    from_alias: set[str] = set()
    seen: set[str] = set()
    # 同一个地址只算一次，保留列表序号最小的；sorted 是稳定的，出现序不变
    for e in sorted(entries, key=lambda e: e.list_index):
        if e.url in seen:
            continue
        src = e.tvg_name or e.name
        key = canonical_key(src) or src
        if not key:
            continue
        seen.add(e.url)
        n = normalize_name(src)
        if n in ALIASES:
            from_alias.add(key)
        is_4k = bool(re.search(r"[48]K", unicodedata.normalize("NFKC", f"{e.name} {e.tvg_name or ''}").upper())) \
            and key not in _CCTV_K
        cand = Candidate(e.url, e.ua, e.list_index, is_4k)
        old = previous.get(e.url)
        if old is not None:
            cand.ok, cand.checked_at = old.ok, old.checked_at
        ch = channels.get(key)
        if ch is None:
            ch = channels[key] = Channel(key, key, [])
            names[key] = Counter()
        ch.candidates.append(cand)
        names[key][_clean_display(src)] += 1
    for key, ch in channels.items():
        if key in from_alias or ch.must_have:
            ch.name = key
        else:
            best = names[key].most_common(1)
            ch.name = best[0][0] if best and best[0][0] else key
    return channels


# ---- 频道表 ----

class ChannelTable:
    """path=None 时只在内存里。"""

    def __init__(self, path: Path | None, lists: list[str], prober: Prober,
                 session_factory: Callable[[], aiohttp.ClientSession] = make_session,
                 ip_allowed: Callable[[str], bool] = is_public_ip,
                 clock: Callable[[], float] = time.time):
        self.path = Path(path) if path is not None else None
        self.lists, self.prober = list(lists), prober
        self.session_factory, self.ip_allowed, self.clock = session_factory, ip_allowed, clock
        self.updated_at: float = 0.0
        self._last_entries: dict[int, list[Entry]] = {}
        self.channels: dict[str, Channel] = self._load()

    def _load(self) -> dict[str, Channel]:
        if self.path is None:
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            out: dict[str, Channel] = {}
            for c in raw["channels"]:
                cands = [Candidate(str(x["url"]), x.get("ua"), int(x["list_index"]), bool(x["is_4k"]),
                                   x.get("ok"), x.get("checked_at")) for x in c["candidates"]]
                out[str(c["key"])] = Channel(str(c["key"]), str(c["name"]), cands)
            self.updated_at = float(raw.get("updated_at", 0.0))
            return out
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            log.warning("Could not read %s; starting with an empty TV channel table", self.path)
            return {}

    def _save(self) -> None:
        if self.path is None:
            return
        folder = os.path.dirname(os.path.abspath(self.path))
        data = {"updated_at": self.updated_at, "channels": [
            {"key": ch.key, "name": ch.name, "candidates": [
                {"url": c.url, "ua": c.ua, "list_index": c.list_index, "is_4k": c.is_4k,
                 "ok": c.ok, "checked_at": c.checked_at} for c in ch.candidates]}
            for ch in self.channels.values()]}
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write TV channels to %s", self.path)

    def match(self, query: str) -> Match:
        if len(query.strip()) < 2:
            return Match(None, [])
        q = canonical_key(query)
        if not q:
            return Match(None, [])
        exact = self.channels.get(q)
        if exact is None:
            for ch in self.channels.values():
                if q in _province_aliases(ch.key):
                    exact = ch
                    break
        if exact is not None:
            return Match(exact, [])
        found = [ch for ch in self.channels.values()
                 if not ch.all_failed and (q in ch.key or any(q in a for a in _aliases_of(ch.key)))]
        found.sort(key=lambda ch: (not ch.must_have, len(ch.key), ch.key))
        return Match(None, found[:MAX_PARTIAL])

    async def _download(self, session: aiohttp.ClientSession, url: str) -> str:
        resp = await open_url(session, url, None, ip_allowed=self.ip_allowed)
        try:
            if resp.status != 200:
                raise aiohttp.ClientError(f"HTTP {resp.status}")
            return (await read_capped(resp, MAX_LIST_BYTES)).decode("utf-8", errors="replace")
        finally:
            resp.release()

    async def refresh(self) -> None:
        session = self.session_factory()
        unrecovered: set[int] = set()   # 失败且内存里没有上次条目的列表（比如刚启动）
        try:
            for i, url in enumerate(self.lists):
                try:
                    self._last_entries[i] = parse_m3u(await self._download(session, url), i)
                    unrecovered.discard(i)
                except Exception as e:
                    if i not in self._last_entries:
                        unrecovered.add(i)      # Blocked/TooLarge/ClientError/超时等，一个列表失败不影响其它
                    log.warning("TV list %d download failed (%s: %s)%s", i, type(e).__name__, e,
                                "; reusing the previous entries" if i not in unrecovered
                                else "; keeping the saved table's candidates")
        finally:
            await session.close()
        entries = [e for i in sorted(self._last_entries) if i < len(self.lists)
                   for e in self._last_entries[i]]
        previous = {c.url: c for ch in self.channels.values() for c in ch.candidates}
        old = self.channels
        self.channels = build_channels(entries, previous)
        # 启动后第一次刷新就失败的列表：沿用状态文件里该列表的候选源（含探测结果），不让表被清空
        if unrecovered:
            keep = {c.url: (key, och.name, c) for key, och in old.items() for c in och.candidates
                    if c.list_index in unrecovered}
            for ch in self.channels.values():     # 同一地址只算一次，列表序号小的优先
                ch.candidates = [c for c in ch.candidates if c.url not in keep]
            for url, (key, name, c) in keep.items():
                ch = self.channels.setdefault(key, Channel(key, name, []))
                ch.candidates.append(c)
            for key in {k for k, _, _ in keep.values()}:
                self.channels[key].candidates.sort(key=lambda c: c.list_index)    # 稳定排序
            self.channels = {k: ch for k, ch in self.channels.items() if ch.candidates}
        self.updated_at = self.clock()
        self._save()
        sem = asyncio.Semaphore(PROBE_CONCURRENCY)

        async def probe(c: Candidate) -> None:
            async with sem:
                try:
                    c.ok = bool(await self.prober(c))
                except Exception:
                    log.debug("TV probe raised for %s", c.url, exc_info=True)
                    c.ok = False
                c.checked_at = self.clock()

        await asyncio.gather(*(probe(c) for ch in self.channels.values() for c in ch.candidates))
        self._save()
        log.info("TV channel table: %d channels", len(self.channels))

    async def run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                log.exception("TV channel refresh failed")
            await asyncio.sleep(REFRESH_SECONDS)
