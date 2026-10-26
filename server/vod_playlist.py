"""一起看：采集接口的 m3u8 处理。

浏览器直接从第三方 CDN 拉 HLS 片段，我们的服务器只负责取 m3u8 文本并改写：
- fetch_playlist：取 m3u8（master 就再跟进一层最高码率的变体），改写成全绝对 https 地址。
- rewrite_media：把所有 URI 变成绝对地址，按规则去掉片源中途插入的广告，从保留的 EXTINF 累加时长。
  #EXT-X-KEY 的地址例外：改写成相对播放列表的 key{n}，原地址记在 Playlist.keys 里，由本服务器中转
  （fetch_key）。密钥只有 16 字节，而有些资源站的密钥域名在部分地区（如上海电信/联通的 CDN 边缘节点）
  直接拒绝连接，浏览器拿不到密钥就整集播不了；片段域名通常不受影响，仍由浏览器直接取。
- fetch_key：取一把密钥（最多 MAX_KEY_BYTES 字节），跳转链同样只认 https。
- probe：像浏览器一样（带 Origin 的 Range 请求）试取第一个片段，确认 CDN 允许跨域且能播；不能播返回原因。
- find_playable：从指定片源线路开始，找第一条能播这一集的线路；每条线路跳过的原因进日志，
  全都不能播时按线路列在 PlaylistError.detail 里（用户看到「这一集暂时没有能播的片源（来源1：原因；…）」，
  线路名只进日志）。

广告规则：按 #EXT-X-DISCONTINUITY 切块，总时长最大的块是正片；一个块是广告，当且仅当
总时长 < AD_MAX_SECONDS，并且（它的 EXTINF 序列在整份列表里作为整块出现不止一次，
或它第一个片段所在目录与正片不同）。拿不准的一律保留。

只认 https：任何改写后不是 https 的地址都当作不能播。allow_http 参数只给测试连本地
127.0.0.1 用，生产调用方不要传。
"""
import asyncio
import logging
import re
import urllib.parse
from dataclasses import dataclass, field

import aiohttp

from live_watch import USER_AGENT
from vod_search import Show

log = logging.getLogger("mrmeeseeks.watch")

AD_MAX_SECONDS = 30.0
MAX_PLAYLIST_BYTES = 2 * 1024 * 1024
MAX_KEY_BYTES = 1024                 # AES-128 密钥是 16 字节；留余量但别让人拿这个口子下载别的
NO_PLAYABLE = "这一集暂时没有能播的片源"
FETCH_TIMEOUT = aiohttp.ClientTimeout(total=10)
PROBE_TIMEOUT = aiohttp.ClientTimeout(total=8)
DETAIL_MAX_CHARS = 400

_URI_ATTR = re.compile(r'URI="([^"]*)"')
_BANDWIDTH = re.compile(r"BANDWIDTH=(\d+)")
_EXTINF = re.compile(r"#EXTINF:\s*([0-9.]+)")


class PlaylistError(Exception):
    """m3u8 取不到或不能用。message 可直接展示给用户；detail 是具体原因（哪条线路、哪一步、HTTP 码、域名），
    给排查用，也可以跟在 message 后面展示。"""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail

    @property
    def text(self) -> str:
        """message，有 detail 时带括号附上。"""
        return f"{self.message}（{self.detail}）" if self.detail else self.message


@dataclass
class Playlist:
    text: str
    duration: float
    first_segment: str | None
    removed_blocks: int
    keys: list[str] = field(default_factory=list)    # #EXT-X-KEY 的原地址；text 里第 n 把写成 key{n}


def _absolutize(ref: str, base_url: str) -> str:
    ref = ref.strip()
    if ref.startswith("//"):
        return "https:" + ref
    return urllib.parse.urljoin(base_url, ref)


def _is_https(url: str) -> bool:
    return urllib.parse.urlsplit(url).scheme == "https"


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url[:60]


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
    keys: list[str] = []

    def tag(ln: str) -> str:
        """标签行里的 URI="..."（KEY / MAP 等）绝对化并检查 https；KEY 的地址换成 key{n}，由本服务器中转。"""
        def sub(mm):
            url = _checked(_absolutize(mm.group(1), base_url), allow_http)
            if not ln.startswith("#EXT-X-KEY:"):
                return f'URI="{url}"'
            if url not in keys:
                keys.append(url)
            return f'URI="key{keys.index(url)}"'
        return _URI_ATTR.sub(sub, ln)

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
        raise PlaylistError(NO_PLAYABLE, "m3u8 里没有片段")
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
        raise PlaylistError(NO_PLAYABLE, "m3u8 总时长为 0")    # 没有时长会立刻“播完”，自动下一集会一路跳过
    return Playlist("\n".join(out) + "\n", duration, first, removed, keys)


def _checked(url: str, allow_http: bool) -> str:
    if not (_is_https(url) or (allow_http and urllib.parse.urlsplit(url).scheme == "http")):
        raise PlaylistError(NO_PLAYABLE, f"地址不是 https：{_host(url)}")
    return url


def _check_redirects(resp, allow_http: bool) -> str:
    """跳转链上每一跳和最终地址都必须是 https（allow_http 仅供测试）；返回最终地址。"""
    for hop in resp.history:
        _checked(str(hop.url), allow_http)
    return _checked(str(resp.url), allow_http)


async def _get_text(session: aiohttp.ClientSession, url: str, allow_http: bool = False) -> tuple[str, str]:
    """取 m3u8 文本，返回 (文本, 跳转后的最终地址)；相对地址要按最终地址解析。"""
    host = _host(url)
    try:
        async with session.get(url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT) as resp:
            final_url = _check_redirects(resp, allow_http)
            if resp.status != 200:
                raise PlaylistError(NO_PLAYABLE, f"m3u8 HTTP {resp.status}（{host}）")
            if resp.content_length is not None and resp.content_length > MAX_PLAYLIST_BYTES:
                raise PlaylistError(NO_PLAYABLE, f"m3u8 超过 {MAX_PLAYLIST_BYTES // 1024 // 1024} MB（{host}）")
            body = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                body += chunk
                if len(body) > MAX_PLAYLIST_BYTES:
                    raise PlaylistError(NO_PLAYABLE, f"m3u8 超过 {MAX_PLAYLIST_BYTES // 1024 // 1024} MB（{host}）")
    except PlaylistError:
        raise
    except Exception as e:
        log.warning("m3u8 fetch failed: %s (%s)", _error_name(e), host)
        raise PlaylistError(NO_PLAYABLE, f"m3u8 取不到：{_error_name(e)}（{host}）") from e
    text = bytes(body).decode("utf-8", errors="replace").lstrip("\ufeff")
    if not text.lstrip().startswith("#EXTM3U"):
        raise PlaylistError(NO_PLAYABLE, f"m3u8 内容不是 M3U 格式（{host}）")
    return text, final_url


async def fetch_key(session: aiohttp.ClientSession, url: str, allow_http: bool = False) -> bytes:
    """取一把 AES 密钥（最多 MAX_KEY_BYTES 字节）。失败抛 PlaylistError，detail 说明原因。allow_http 仅供测试。"""
    host = _host(url)
    try:
        _checked(url, allow_http)
        async with session.get(url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT) as resp:
            _check_redirects(resp, allow_http)
            if resp.status != 200:
                raise PlaylistError(NO_PLAYABLE, f"密钥 HTTP {resp.status}（{host}）")
            body = await resp.content.read(MAX_KEY_BYTES + 1)
    except PlaylistError:
        raise
    except Exception as e:
        log.warning("key fetch failed: %s (%s)", _error_name(e), host)
        raise PlaylistError(NO_PLAYABLE, f"密钥取不到：{_error_name(e)}（{host}）") from e
    if not body or len(body) > MAX_KEY_BYTES:
        raise PlaylistError(NO_PLAYABLE, f"密钥大小不对（{host}）")
    return body


def _error_name(e: BaseException) -> str:
    """异常的简短名字；超时最常见，翻成中文。不带 url 之类的细节。"""
    if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
        return "超时"
    return type(e).__name__


async def fetch_playlist(session: aiohttp.ClientSession, url: str, allow_http: bool = False) -> Playlist:
    """取 m3u8 并改写；master 只跟进一个变体。allow_http 仅供测试。"""
    _checked(url, allow_http)
    text, final = await _get_text(session, url, allow_http)
    variant = pick_variant(text, final)
    if variant is not None:
        _checked(variant, allow_http)
        text, final = await _get_text(session, variant, allow_http)
    return rewrite_media(text, final, allow_http)


async def probe(session: aiohttp.ClientSession, playlist: Playlist, origin: str,
                allow_http: bool = False) -> str | None:
    """像浏览器一样试取第一个片段：状态 200/206、跳转链全是 https 且 CORS 允许这个 origin。
    能播返回 None，否则返回原因（给日志和提示用）。永不抛异常。allow_http 仅供测试。"""
    if not playlist.first_segment:
        return "m3u8 里没有片段"
    host = _host(playlist.first_segment)
    try:
        _checked(playlist.first_segment, allow_http)
        headers = {"User-Agent": USER_AGENT, "Range": "bytes=0-1023", "Origin": origin}
        async with session.get(playlist.first_segment, headers=headers, timeout=PROBE_TIMEOUT) as resp:
            _check_redirects(resp, allow_http)
            if resp.status not in (200, 206):
                return f"片段 HTTP {resp.status}（{host}）"
            await resp.content.read(1024)
            acao = resp.headers.get("Access-Control-Allow-Origin")
            if acao in ("*", origin):
                return None
            return f"片段不允许跨域（CORS {acao or '无'}，{host}）"
    except PlaylistError as e:
        return e.detail
    except Exception as e:
        return f"片段取不到：{_error_name(e)}（{host}）"


async def find_playable(session: aiohttp.ClientSession, show: Show, episode: int, start_line: int,
                        origin: str, allow_http: bool = False) -> tuple[int, Playlist]:
    """从 start_line 起绕一圈，找第一条有这一集、m3u8 能取、片段能探测通的线路。
    每条线路跳过的原因记进日志（带线路名）；全都不能播时，原因按线路列在 PlaylistError.detail 里，
    线路只写成「来源N」（N 是线路序号，从 1 起），用户看不到资源站的名字。"""
    n = len(show.lines)
    reasons: list[str] = []
    for k in range(n):
        idx = (start_line + k) % n
        line = show.lines[idx]
        eps = line.episodes
        if not 0 <= episode < len(eps):
            reason = f"没有第 {episode + 1} 集"
        else:
            try:
                pl = await fetch_playlist(session, eps[episode].url, allow_http)
            except PlaylistError as e:
                reason = e.detail or "m3u8 不能用"
            else:
                reason = await probe(session, pl, origin, allow_http)
                if reason is None:
                    log.info("watch vod %r ep %d: line %s playable", show.title, episode + 1, line.name)
                    return idx, pl
        log.info("watch vod %r ep %d: line %s skipped: %s", show.title, episode + 1, line.name, reason)
        reasons.append(f"来源{idx + 1}：{reason}")
    raise PlaylistError(NO_PLAYABLE, join_reasons(reasons))


def join_reasons(reasons: list[str], limit: int = DETAIL_MAX_CHARS) -> str:
    """按线路列出原因，太长就截断（Discord 消息和网页提示都不宜太长）。"""
    text = "；".join(reasons) if reasons else "没有片源线路"
    return text if len(text) <= limit else text[:limit - 1] + "…"
