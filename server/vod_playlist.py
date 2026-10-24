"""一起看：采集接口的 m3u8 处理。

浏览器直接从第三方 CDN 拉 HLS 片段，我们的服务器只负责取 m3u8 文本并改写：
- fetch_playlist：取 m3u8（master 就再跟进一层最高码率的变体），改写成全绝对 https 地址。
- rewrite_media：把所有 URI 变成绝对地址，按规则去掉片源中途插入的广告，从保留的 EXTINF 累加时长。
- probe：像浏览器一样（带 Origin 的 Range 请求）试取第一个片段，确认 CDN 允许跨域且能播。
- find_playable：从指定片源线路开始，找第一条能播这一集的线路。

广告规则：按 #EXT-X-DISCONTINUITY 切块，总时长最大的块是正片；一个块是广告，当且仅当
总时长 < AD_MAX_SECONDS，并且（它的 EXTINF 序列在整份列表里作为整块出现不止一次，
或它第一个片段所在目录与正片不同）。拿不准的一律保留。

只认 https：任何改写后不是 https 的地址都当作不能播。allow_http 参数只给测试连本地
127.0.0.1 用，生产调用方不要传。
"""
import logging
import re
import urllib.parse
from dataclasses import dataclass

import aiohttp

from live_watch import USER_AGENT
from vod_search import Show

log = logging.getLogger("mrmeeseeks.watch")

AD_MAX_SECONDS = 30.0
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
NO_PLAYABLE = "这一集暂时没有能播的片源"
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=10)
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=8)

_URI_ATTR = re.compile(r'URI="([^"]*)"')
_BANDWIDTH = re.compile(r"BANDWIDTH=(\d+)")
_EXTINF = re.compile(r"#EXTINF:\s*([0-9.]+)")


class PlaylistError(Exception):
    """m3u8 取不到或不能用，message 可直接展示给用户。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Playlist:
    text: str
    duration: float
    first_segment: str | None
    removed_blocks: int


def _absolutize(ref: str, base_url: str) -> str:
    ref = ref.strip()
    if ref.startswith("//"):
        return "https:" + ref
    return urllib.parse.urljoin(base_url, ref)


def _is_https(url: str) -> bool:
    return urllib.parse.urlsplit(url).scheme == "https"


def _directory(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path.rsplit('/', 1)[0]}"


def pick_variant(text: str, base_url: str) -> str | None:
    """master 里码率最高的变体的绝对地址；不是 master 返回 None。"""
    lines = [ln.strip() for ln in text.splitlines()]
    best, best_bw = None, -1
    for i, ln in enumerate(lines):
        if not ln.startswith("#EXT-X-STREAM-INF"):
            continue
        uri = next((x for x in lines[i + 1:] if x and not x.startswith("#")), None)
        if uri is None:
            continue
        m = _BANDWIDTH.search(ln)
        bw = int(m.group(1)) if m else 0
        if bw > best_bw:
            best, best_bw = uri, bw
    return _absolutize(best, base_url) if best is not None else None


@dataclass
class _Segment:
    lines: list[str]      # EXTINF 等标签 + 已绝对化的地址行
    duration: float
    url: str


@dataclass
class _Block:
    entries: list         # str（原样保留的行）或 _Segment
    opener: str | None    # 开头的 #EXT-X-DISCONTINUITY 行，第一块没有

    @property
    def segments(self) -> list[_Segment]:
        return [e for e in self.entries if isinstance(e, _Segment)]

    @property
    def total(self) -> float:
        return sum(s.duration for s in self.segments)


def rewrite_media(text: str, base_url: str, allow_http: bool = False) -> Playlist:
    """改写媒体列表：地址全绝对、去广告、统计时长。allow_http 仅供测试。"""
    blocks = [_Block([], None)]
    pending: list[str] = []
    pending_dur = 0.0

    def tag(ln: str) -> str:
        """标签行里的 URI="..."（KEY / MAP 等）绝对化并检查 https。"""
        return _URI_ATTR.sub(lambda mm: f'URI="{_checked(_absolutize(mm.group(1), base_url), allow_http)}"', ln)

    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if ln == "#EXT-X-DISCONTINUITY":
            blocks.append(_Block([], ln))
        elif ln.startswith("#"):
            m = _EXTINF.match(ln)
            if m:
                pending, pending_dur = [ln], float(m.group(1))
            elif pending:
                pending.append(tag(ln))     # 夹在 EXTINF 和地址之间的标签（如 BYTERANGE、KEY）
            else:
                blocks[-1].entries.append(tag(ln))
        else:
            url = _checked(_absolutize(ln, base_url), allow_http)
            blocks[-1].entries.append(_Segment(pending + [url], pending_dur, url))
            pending, pending_dur = [], 0.0

    content = [b for b in blocks if b.segments]
    if not content:
        raise PlaylistError(NO_PLAYABLE)
    main = max(content, key=lambda b: b.total)
    main_dir = _directory(main.segments[0].url)
    seen: dict[tuple, int] = {}
    for b in content:
        key = tuple(s.duration for s in b.segments)
        seen[key] = seen.get(key, 0) + 1

    out, duration, first, removed = [], 0.0, None, 0
    for b in blocks:
        segs = b.segments
        is_ad = (b is not main and bool(segs) and b.total < AD_MAX_SECONDS and (
            seen[tuple(s.duration for s in segs)] > 1 or _directory(segs[0].url) != main_dir))
        if is_ad:
            removed += 1
        elif b.opener:
            out.append(b.opener)
        for e in b.entries:
            if isinstance(e, _Segment):
                if is_ad:
                    continue
                out.extend(e.lines)
                duration += e.duration
                first = first or e.url
            else:
                out.append(e)
    if not duration > 0:
        raise PlaylistError(NO_PLAYABLE)    # 没有时长会立刻“播完”，自动下一集会一路跳过
    return Playlist("\n".join(out) + "\n", duration, first, removed)


def _checked(url: str, allow_http: bool) -> str:
    if not (_is_https(url) or (allow_http and urllib.parse.urlsplit(url).scheme == "http")):
        raise PlaylistError(NO_PLAYABLE)
    return url


def _check_redirects(resp, allow_http: bool) -> str:
    """跳转链上每一跳和最终地址都必须是 https（allow_http 仅供测试）；返回最终地址。"""
    for hop in resp.history:
        _checked(str(hop.url), allow_http)
    return _checked(str(resp.url), allow_http)


async def _get_text(session: aiohttp.ClientSession, url: str, allow_http: bool = False) -> tuple[str, str]:
    """取 m3u8 文本，返回 (文本, 跳转后的最终地址)；相对地址要按最终地址解析。"""
    try:
        async with session.get(url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT) as resp:
            final_url = _check_redirects(resp, allow_http)
            if resp.status != 200:
                raise PlaylistError(NO_PLAYABLE)
            if resp.content_length is not None and resp.content_length > MAX_PLAYLIST_BYTES:
                raise PlaylistError(NO_PLAYABLE)
            body = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                body += chunk
                if len(body) > MAX_PLAYLIST_BYTES:
                    raise PlaylistError(NO_PLAYABLE)
    except PlaylistError:
        raise
    except Exception as e:
        log.warning("m3u8 fetch failed: %s", type(e).__name__)
        raise PlaylistError(NO_PLAYABLE) from e
    text = bytes(body).decode("utf-8", errors="replace").lstrip("\ufeff")
    if not text.lstrip().startswith("#EXTM3U"):
        raise PlaylistError(NO_PLAYABLE)
    return text, final_url


async def fetch_playlist(session: aiohttp.ClientSession, url: str, allow_http: bool = False) -> Playlist:
    """取 m3u8 并改写；master 只跟进一个变体。allow_http 仅供测试。"""
    _checked(url, allow_http)
    text, final = await _get_text(session, url, allow_http)
    variant = pick_variant(text, final)
    if variant is not None:
        _checked(variant, allow_http)
        text, final = await _get_text(session, variant, allow_http)
    return rewrite_media(text, final, allow_http)


async def probe(session: aiohttp.ClientSession, playlist: Playlist, origin: str, allow_http: bool = False) -> bool:
    """像浏览器一样试取第一个片段：状态 200/206、跳转链全是 https 且 CORS 允许这个 origin。永不抛异常。
    allow_http 仅供测试。"""
    try:
        if not playlist.first_segment:
            return False
        headers = {"User-Agent": USER_AGENT, "Range": "bytes=0-1023", "Origin": origin}
        async with session.get(playlist.first_segment, headers=headers, timeout=PROBE_TIMEOUT) as resp:
            _check_redirects(resp, allow_http)
            if resp.status not in (200, 206):
                return False
            await resp.content.read(1024)
            return resp.headers.get("Access-Control-Allow-Origin") in ("*", origin)
    except Exception:
        return False


async def find_playable(session: aiohttp.ClientSession, show: Show, episode: int, start_line: int,
                        origin: str, allow_http: bool = False) -> tuple[int, Playlist]:
    """从 start_line 起绕一圈，找第一条有这一集、m3u8 能取、片段能探测通的线路。"""
    n = len(show.lines)
    for k in range(n):
        idx = (start_line + k) % n
        eps = show.lines[idx].episodes
        if not 0 <= episode < len(eps):
            continue
        try:
            pl = await fetch_playlist(session, eps[episode].url, allow_http)
        except PlaylistError:
            continue
        if await probe(session, pl, origin, allow_http):
            return idx, pl
    raise PlaylistError(NO_PLAYABLE)
