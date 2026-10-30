"""一起看的地区限制：按 IP 判断国家/地区，只放行 WATCH_ALLOWED_COUNTRIES（默认澳大利亚、新西兰）。

数据来自五大 RIR 公开的分配表（delegated-*-extended-latest，免费、不用账号，每周刷新）：
每行 `registry|cc|ipv4|1.0.0.0|256|20110811|allocated|...`，ipv4 的 value 是地址数，ipv6 的 value 是前缀长度。
解析后合并相邻同国家的段，缓存成 JSON（GEOIP 文件，默认 herodata 目录下 geoip.json），启动时先读缓存，
缓存不存在或超过 REFRESH_DAYS 天就后台重新下载。数据没加载好时不拦截（fail-open），后台页面会提示。

- 内网 / 回环地址算 LOCAL，放行（反向代理没传 X-Forwarded-For 时所有人都是 127.0.0.1，这时等于不限制，后台会显示 LOCAL）。
- 不在任何分配表里的 IP 国家为 None，按不放行处理。
- BlockLog 记被拦的 IP：每个 IP 一个 6 位编号（页面上给用户看，方便对照）、推测的国家、去过的房间、次数、首末时间。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import secrets
import tempfile
import time
from array import array
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import aiohttp

from live_watch import USER_AGENT

log = logging.getLogger("mrmeeseeks.geoip")

DEFAULT_SOURCES = (
    "https://ftp.apnic.net/stats/apnic/delegated-apnic-extended-latest",
    "https://ftp.arin.net/pub/stats/arin/delegated-arin-extended-latest",
    "https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-extended-latest",
    "https://ftp.lacnic.net/pub/stats/lacnic/delegated-lacnic-extended-latest",
    "https://ftp.afrinic.net/pub/stats/afrinic/delegated-afrinic-extended-latest",
)
DEFAULT_ALLOWED = ("AU", "NZ")
REFRESH_DAYS = 7
CHECK_INTERVAL = 3600          # 多久检查一次要不要刷新（秒）
RETRY_INTERVAL = 600           # 下载失败后多久重试（秒）
FETCH_TIMEOUT = 120            # 单个文件的下载时限（秒）
MAX_BODY = 64 * 1024 * 1024    # 单个文件最大 64 MiB（目前最大的 RIPE 约 18 MiB）
LOCAL = "LOCAL"
BLOCK_LOG_MAX = 500
BLOCKED_MSG = "你所在的地区暂时不能观看（目前只对澳大利亚、新西兰开放）"
_SKIP_STATUS = {"available", "reserved"}

Range = tuple[int, int, str]   # (start, end, cc)，闭区间


def parse_allowed(raw: str | None) -> tuple[str, ...]:
    """WATCH_ALLOWED_COUNTRIES="AU, nz" -> ("AU", "NZ")；None 用默认；空字符串 -> () 表示不限制。"""
    if raw is None:
        return DEFAULT_ALLOWED
    return tuple(dict.fromkeys(c.strip().upper() for c in raw.split(",") if c.strip()))


def parse_delegated(text: str) -> tuple[list[Range], list[Range]]:
    """一份 delegated 文件 -> (ipv4 段, ipv6 段)，未排序、未合并。坏行跳过。"""
    v4: list[Range] = []
    v6: list[Range] = []
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        parts = line.split("|")
        if len(parts) < 7 or parts[0].endswith("*") or parts[2] == "*":
            continue       # 文件头和汇总行
        cc, kind, start, value, status = parts[1].strip().upper(), parts[2], parts[3], parts[4], parts[6].strip().lower()
        if status in _SKIP_STATUS or len(cc) != 2 or not cc.isalpha() or cc == "ZZ":
            continue
        try:
            if kind == "ipv4":
                s = int(ipaddress.IPv4Address(start))
                n = int(value)
                if n <= 0:
                    continue
                v4.append((s, s + n - 1, cc))
            elif kind == "ipv6":
                s = int(ipaddress.IPv6Address(start))
                prefix = int(value)
                if not 0 <= prefix <= 128:
                    continue
                v6.append((s, s + (1 << (128 - prefix)) - 1, cc))
        except (ValueError, ipaddress.AddressValueError):
            continue
    return v4, v6


def merge_ranges(ranges: Iterable[Range]) -> list[Range]:
    """排序，去掉重叠，相邻同国家的段合并。重叠时保留先出现（更靠前）的那段。"""
    out: list[Range] = []
    for s, e, cc in sorted(ranges, key=lambda r: (r[0], r[1])):
        if out:
            ps, pe, pcc = out[-1]
            if s <= pe:                      # 重叠：只补上多出来的部分
                if e <= pe:
                    continue
                s = pe + 1
            if pcc == cc and s == pe + 1:
                out[-1] = (ps, e, cc)
                continue
        out.append((s, e, cc))
    return out


@dataclass
class Verdict:
    allowed: bool | None      # None = 数据还没加载，调用方自行决定（放行）
    country: str | None       # 两位国家码、LOCAL，或 None（不在任何分配表里 / IP 非法）


class _Table:
    """一类地址（v4 或 v6）的查表：starts 有序，用 bisect。v4 用 array('Q') 省内存，v6 的数超过 64 位只能用 list。"""

    def __init__(self, ranges: list[Range], packed: bool):
        self.starts = array("Q", (r[0] for r in ranges)) if packed else [r[0] for r in ranges]
        self.ends = array("Q", (r[1] for r in ranges)) if packed else [r[1] for r in ranges]
        self.cc = "".join(r[2] for r in ranges)

    def __len__(self) -> int:
        return len(self.starts)

    def lookup(self, n: int) -> str | None:
        i = bisect_right(self.starts, n) - 1
        if i >= 0 and n <= self.ends[i]:
            return self.cc[2 * i:2 * i + 2]
        return None

    def ranges(self) -> list[Range]:
        return [(int(self.starts[i]), int(self.ends[i]), self.cc[2 * i:2 * i + 2]) for i in range(len(self.starts))]


def _default_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(headers={"User-Agent": USER_AGENT},
                                 timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT))


class GeoDB:
    def __init__(self, path: Path | None, allowed: Iterable[str] = DEFAULT_ALLOWED,
                 sources: Iterable[str] = DEFAULT_SOURCES, refresh_days: float = REFRESH_DAYS,
                 clock: Callable[[], float] = time.time, session_factory=_default_session):
        self.path = Path(path) if path is not None else None
        self.allowed = frozenset(c.upper() for c in allowed)
        self.sources = tuple(sources)
        self.refresh_days = refresh_days
        self.clock = clock
        self._session_factory = session_factory
        self.fetched_at: float | None = None
        self.last_error: str | None = None
        self._v4 = _Table([], packed=True)
        self._v6 = _Table([], packed=False)
        self._refreshing = False

    # ---------------- 数据 ----------------
    @property
    def loaded(self) -> bool:
        return self.fetched_at is not None and (len(self._v4) > 0 or len(self._v6) > 0)

    def set_ranges(self, v4: list[Range], v6: list[Range], fetched_at: float) -> None:
        self._v4 = _Table(merge_ranges(v4), packed=True)
        self._v6 = _Table(merge_ranges(v6), packed=False)
        self.fetched_at = fetched_at

    def load(self) -> bool:
        """读缓存；没有或坏了返回 False（保持空表）。"""
        if self.path is None:
            return False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            v4 = [(int(s), int(e), str(cc)) for s, e, cc in raw["v4"]]
            v6 = [(int(s), int(e), str(cc)) for s, e, cc in raw["v6"]]
            fetched_at = float(raw["fetched_at"])
        except FileNotFoundError:
            return False
        except (OSError, ValueError, TypeError, KeyError):
            log.warning("Could not read %s; geoip data will be downloaded again", self.path)
            return False
        self.set_ranges(v4, v6, fetched_at)
        return self.loaded

    def save(self) -> None:
        if self.path is None:
            return
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": self.fetched_at, "v4": self._v4.ranges(), "v6": self._v6.ranges()}, f)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write geoip data to %s", self.path)

    def stale(self) -> bool:
        return self.fetched_at is None or self.clock() - self.fetched_at >= self.refresh_days * 86400

    # ---------------- 查询 ----------------
    def country(self, ip: str | None) -> str | None:
        try:
            addr = ipaddress.ip_address((ip or "").strip())
        except ValueError:
            return None
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            return LOCAL
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        if isinstance(addr, ipaddress.IPv4Address):
            return self._v4.lookup(int(addr))
        return self._v6.lookup(int(addr))

    def check(self, ip: str | None) -> Verdict:
        cc = self.country(ip)
        if cc == LOCAL:
            return Verdict(True, cc)
        if not self.loaded:
            return Verdict(None, cc)
        return Verdict(cc is not None and cc in self.allowed, cc)

    def status(self) -> dict:
        return {"allowed": sorted(self.allowed), "loaded": self.loaded, "fetched_at": self.fetched_at,
                "ranges": len(self._v4) + len(self._v6), "last_error": self.last_error, "refreshing": self._refreshing}

    # ---------------- 下载 ----------------
    async def _fetch(self, session: aiohttp.ClientSession, url: str) -> str:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status >= 400:
                raise ValueError(f"HTTP {resp.status}")
            buf = bytearray()
            async for chunk in resp.content.iter_chunked(256 * 1024):
                buf += chunk
                if len(buf) > MAX_BODY:
                    raise ValueError("response too large")
            return buf.decode("utf-8", "replace")

    async def refresh(self) -> bool:
        """下载全部来源并重建表；任何一个失败就保留旧数据、记 last_error。"""
        if self._refreshing:
            return False
        self._refreshing = True
        try:
            v4: list[Range] = []
            v6: list[Range] = []
            async with self._session_factory() as session:
                for url in self.sources:          # 依次下载、解析完就丢掉原文，省内存（单个文件最大约 18 MiB）
                    text = await self._fetch(session, url)
                    a, b = await asyncio.to_thread(parse_delegated, text)
                    del text
                    v4.extend(a)
                    v6.extend(b)
            if not v4:
                raise ValueError("no ranges parsed")
            self.set_ranges(v4, v6, self.clock())
            self.save()
            self.last_error = None
            log.info("geoip data refreshed: %d ranges", len(self._v4) + len(self._v6))
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  网络、解析都只记日志
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            log.warning("geoip refresh failed: %s", self.last_error)
            return False
        finally:
            self._refreshing = False

    async def run(self, sleep: Callable[[float], "asyncio.Future"] = asyncio.sleep) -> None:
        while True:
            if self.stale():
                ok = await self.refresh()
                await sleep(CHECK_INTERVAL if ok else RETRY_INTERVAL)
            else:
                await sleep(CHECK_INTERVAL)


# ---------------- 被拦记录 ----------------
def client_ip(request) -> str | None:
    """aiohttp 请求的客户端 IP：直连就是对端；对端是本机 / 内网（反向代理）且带 X-Forwarded-For 时取最后一段
    （Caddy 把真实 IP 追加在最后，前面的可能是客户端伪造的）。"""
    peer = request.remote
    xff = request.headers.get("X-Forwarded-For")
    if xff and peer:
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        if addr.is_private or addr.is_loopback:
            last = xff.split(",")[-1].strip()
            return last or peer
    return peer


@dataclass
class BlockEntry:
    ip: str
    id: str                    # 6 位编号，页面上给被拦的人看
    country: str | None
    rooms: list[str] = field(default_factory=list)
    hits: int = 0
    first_at: float = 0.0
    last_at: float = 0.0


class BlockLog:
    """STATE_DIR/watch_blocked.json：按 IP 一条，最多 BLOCK_LOG_MAX 条（按最近一次），path=None 只在内存里。"""

    def __init__(self, path: Path | None, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path is not None else None
        self.clock = clock
        self.entries: dict[str, BlockEntry] = {}
        self._load()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for r in raw["entries"]:
                e = BlockEntry(str(r["ip"]), str(r["id"]), r.get("country"), [str(x) for x in r.get("rooms", [])],
                               int(r.get("hits", 0)), float(r.get("first_at", 0)), float(r.get("last_at", 0)))
                self.entries[e.ip] = e
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, KeyError):
            log.warning("Could not read %s; starting with no block log", self.path)
            self.entries = {}

    def _save(self) -> None:
        if self.path is None:
            return
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"entries": [asdict(e) for e in self.entries.values()]}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write block log to %s", self.path)

    def record(self, ip: str | None, country: str | None, room_id: str) -> BlockEntry:
        ip = (ip or "?").strip() or "?"
        now = self.clock()
        e = self.entries.get(ip)
        if e is None:
            e = self.entries[ip] = BlockEntry(ip, secrets.token_hex(3).upper(), country, first_at=now)
        e.country = country or e.country
        e.hits += 1
        e.last_at = now
        if room_id in e.rooms:
            e.rooms.remove(room_id)
        e.rooms.append(room_id)
        del e.rooms[:-5]
        if len(self.entries) > BLOCK_LOG_MAX:
            for old in sorted(self.entries.values(), key=lambda x: x.last_at)[:len(self.entries) - BLOCK_LOG_MAX]:
                del self.entries[old.ip]
        self._save()
        return e

    def all(self) -> list[BlockEntry]:
        return sorted(self.entries.values(), key=lambda e: -e.last_at)
