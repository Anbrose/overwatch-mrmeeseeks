"""一起看：链接解析与视频来源（B 站 / YouTube）。

- find_link：从聊天文本里取第一个合法的 B 站 / YouTube 链接（只认白名单域名）。
- Sources.resolve：展开 b23.tv 短链，取标题、时长，得到 Video；B 站还会试取一次播放地址，放不了就直接拒绝。
- Sources.bili_streams：每次都重新请求 DASH 播放地址（CDN 地址带 deadline，会过期）。

B 站接口在海外服务器上的注意事项（实测）：
- 不带 buvid3/buvid4 cookie、不做 WBI 签名，x/web-interface/view 会直接 HTTP 412。
  所以先从 finger/spi 取 buvid，再用 nav 里的 wbi_img 两个 key 给请求签名。
- 大陆限定 / 下架 / 地区封锁：view 或 playurl 返回 code != 0（如 -404、-10403），
  统一报 VideoUnavailable。
- WBI key 缓存约 6 小时；遇到 -352 / -403 就刷新 key 再试一次。
- SESSDATA 只放在请求头里，绝不写日志；nav 显示未登录时在日志里报警（失效了）。
"""
import hashlib
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass

import aiohttp

from live_watch import USER_AGENT
from watch_rooms import Video

log = logging.getLogger("mrmeeseeks.watch")

QUALITY_QN = {"1080": 80, "720": 64, "480": 32}

UNAVAILABLE_MSG = "这个视频在海外服务器上无法播放"
BILI_DOWN_MSG = "B 站暂时无法访问，请稍后再试"
YT_DOWN_MSG = "YouTube 暂时无法访问，请稍后再试"
YT_NO_EMBED_MSG = "这个 YouTube 视频不允许嵌入播放"
BILI_REFERER = "https://www.bilibili.com/"
BILI_ORIGIN = "https://www.bilibili.com"
SPI_URL = "https://api.bilibili.com/x/frontend/finger/spi"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
VIEW_URL = "https://api.bilibili.com/x/web-interface/wbi/view"
PLAYURL_URL = "https://api.bilibili.com/x/player/wbi/playurl"
OEMBED_URL = "https://www.youtube.com/oembed"
WBI_TTL = 6 * 3600
URL_FALLBACK_TTL = 1800
RETRY_CODES = (-352, -403)

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39,
    12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63,
    57, 62, 11, 36, 20, 34, 44, 52,
]


class VideoUnavailable(Exception):
    """视频不能播放，message 可直接展示给用户。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class Link:
    source: str          # "bilibili" | "youtube"
    id: str
    page: int = 1
    short: bool = False  # True 表示 b23.tv 短链，还没展开


# ---------------- 链接解析 ----------------
_CANDIDATE = re.compile(r"(?<![A-Za-z0-9.-])((?:https?://)?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?::\d+)?/[^\s]*)")
_BV = re.compile(r"^/video/(BV[0-9A-Za-z]{10})")
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_BILI_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}


def _parse_candidate(raw: str) -> Link | None:
    url = raw if "://" in raw else "https://" + raw
    try:
        u = urllib.parse.urlparse(url)
        host = (u.hostname or "").lower()
        query = urllib.parse.parse_qs(u.query)
    except ValueError:
        return None
    path = u.path
    if host in _BILI_HOSTS:
        m = _BV.match(path)
        if not m:
            return None
        try:
            page = max(1, int(query.get("p", ["1"])[0]))
        except ValueError:
            page = 1
        return Link("bilibili", m.group(1), page)
    if host == "b23.tv":
        m = re.match(r"^/([A-Za-z0-9]+)", path)
        return Link("bilibili", m.group(1), short=True) if m else None
    if host == "youtu.be":
        vid = path.lstrip("/").split("/")[0]
        return Link("youtube", vid) if _YT_ID.match(vid) else None
    if host in _YT_HOSTS:
        vid = ""
        if path == "/watch":
            vid = query.get("v", [""])[0]
        else:
            m = re.match(r"^/shorts/([^/]+)", path)
            vid = m.group(1) if m else ""
        return Link("youtube", vid) if _YT_ID.match(vid) else None
    return None


def find_link(text: str) -> Link | None:
    """取文本里第一个合法链接；不认识的域名一律忽略。"""
    for m in _CANDIDATE.finditer(text or ""):
        link = _parse_candidate(m.group(1).rstrip(".,;)）】>\"'"))
        if link:
            return link
    return None


# ---------------- WBI 签名 ----------------
def mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def wbi_sign(params: dict, mixin: str, now: int) -> str:
    """给参数加 wts 并签名，返回完整 query string（含 w_rid）。"""
    p = {k: v for k, v in params.items()}
    p["wts"] = int(now)
    items = sorted((k, "".join(c for c in str(v) if c not in "!'()*")) for k, v in p.items())
    query = urllib.parse.urlencode(items)
    return query + "&w_rid=" + hashlib.md5((query + mixin).encode()).hexdigest()


# ---------------- 接口返回解析 ----------------
def _pick(d: dict, *names, default=None):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


def parse_view(resp: dict, page: int) -> tuple[str, int, float]:
    """wbi/view 的完整返回 → (标题, cid, 时长)。"""
    if resp.get("code") != 0:
        raise VideoUnavailable(UNAVAILABLE_MSG)
    data = resp.get("data") or {}
    pages = data.get("pages") or []
    for p in pages:
        if p.get("page") == page:
            return data.get("title", ""), int(p["cid"]), float(p.get("duration") or data.get("duration") or 0)
    if not pages and page == 1 and data.get("cid"):
        return data.get("title", ""), int(data["cid"]), float(data.get("duration") or 0)
    raise VideoUnavailable(f"这个视频没有第 {page} P")


@dataclass
class DashStreams:
    video_url: str
    video_backup: list[str]
    audio_url: str
    audio_backup: list[str]
    video_codecs: str
    audio_codecs: str
    width: int
    height: int
    frame_rate: str
    video_bandwidth: int
    audio_bandwidth: int
    duration: float
    video_init: str
    video_index: str
    audio_init: str
    audio_index: str
    expires_at: float


def _seg(entry: dict) -> tuple[str, str]:
    sb = _pick(entry, "segment_base", "SegmentBase", default={}) or {}
    return (str(_pick(sb, "initialization", "Initialization", default="")),
            str(_pick(sb, "index_range", "indexRange", default="")))


def _deadline(url: str) -> float | None:
    try:
        v = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("deadline")
        return float(v[0]) if v else None
    except (ValueError, IndexError):
        return None


def parse_playurl(resp: dict, max_qn: int, now: float) -> DashStreams:
    """wbi/playurl 的完整返回 → 选 ≤max_qn 的最高档，同档优先 AVC（codecid 7）。"""
    if resp.get("code") != 0:
        raise VideoUnavailable(UNAVAILABLE_MSG)
    dash = (resp.get("data") or {}).get("dash") or {}
    videos = [v for v in dash.get("video") or [] if v.get("id", 0) <= max_qn]
    audios = dash.get("audio") or []
    if not videos or not audios:
        raise VideoUnavailable(UNAVAILABLE_MSG)
    v = max(videos, key=lambda x: (x["id"], x.get("codecid") == 7, x.get("bandwidth", 0)))
    a = max(audios, key=lambda x: x.get("bandwidth", 0))
    v_url = _pick(v, "baseUrl", "base_url", default="")
    a_url = _pick(a, "baseUrl", "base_url", default="")
    v_init, v_index = _seg(v)
    a_init, a_index = _seg(a)
    expires = _deadline(v_url) or now + URL_FALLBACK_TTL
    return DashStreams(
        video_url=v_url, video_backup=list(_pick(v, "backupUrl", "backup_url", default=[]) or []),
        audio_url=a_url, audio_backup=list(_pick(a, "backupUrl", "backup_url", default=[]) or []),
        video_codecs=v.get("codecs", ""), audio_codecs=a.get("codecs", ""),
        width=int(v.get("width") or 0), height=int(v.get("height") or 0),
        frame_rate=str(_pick(v, "frameRate", "frame_rate", default="")),
        video_bandwidth=int(v.get("bandwidth") or 0), audio_bandwidth=int(a.get("bandwidth") or 0),
        duration=float(dash.get("duration") or 0),
        video_init=v_init, video_index=v_index, audio_init=a_init, audio_index=a_index,
        expires_at=expires)


# ---------------- 网络 ----------------
def _default_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=20))


def bili_url(bvid: str, page: int) -> str:
    return f"https://www.bilibili.com/video/{bvid}?p={page}"


class Sources:
    def __init__(self, sessdata: str | None, max_qn: int = 80, session_factory=_default_session):
        self._sessdata = sessdata or None
        self.max_qn = max_qn
        self._session_factory = session_factory
        self._buvid: tuple[str, str] | None = None
        self._wbi: tuple[str, float] | None = None    # (mixin key, 获取时间)

    # -- 内部 --
    def _bili_headers(self) -> dict:
        cookies = []
        if self._buvid:
            cookies += [f"buvid3={self._buvid[0]}", f"buvid4={self._buvid[1]}"]
        if self._sessdata:
            cookies.append(f"SESSDATA={self._sessdata}")
        h = {"User-Agent": USER_AGENT, "Referer": BILI_REFERER, "Origin": BILI_ORIGIN}
        if cookies:
            h["Cookie"] = "; ".join(cookies)
        return h

    async def _get_json(self, session, url: str, headers: dict | None = None) -> dict:
        async with session.get(url, headers=headers or {"User-Agent": USER_AGENT}) as r:
            if r.status != 200:
                raise VideoUnavailable(BILI_DOWN_MSG)
            try:
                data = await r.json(content_type=None)
            except ValueError as e:
                raise VideoUnavailable(BILI_DOWN_MSG) from e
        if not isinstance(data, dict):
            raise VideoUnavailable(BILI_DOWN_MSG)
        return data

    async def _ensure_buvid(self, session):
        if self._buvid is not None:
            return
        try:
            resp = await self._get_json(session, SPI_URL, {"User-Agent": USER_AGENT, "Referer": BILI_REFERER})
            d = resp.get("data") or {}
            if d.get("b_3") and d.get("b_4"):
                self._buvid = (d["b_3"], d["b_4"])    # 只缓存有效值，失败下次再取
            else:
                log.warning("取 buvid 失败：返回里没有 b_3/b_4")
        except (VideoUnavailable, aiohttp.ClientError, TimeoutError) as e:
            log.warning("取 buvid 失败：%s", type(e).__name__)

    async def _ensure_wbi(self, session, force: bool = False) -> str:
        if not force and self._wbi and time.time() - self._wbi[1] < WBI_TTL:
            return self._wbi[0]
        resp = await self._get_json(session, NAV_URL, self._bili_headers())
        data = resp.get("data") or {}
        if self._sessdata and not data.get("isLogin"):
            # 每次刷新 key（约 6 小时）最多报一次；不写 SESSDATA 的值
            log.warning("BILIBILI_SESSDATA 已失效，B 站将使用未登录画质")
        img = data.get("wbi_img") or {}

        def stem(u):
            return (u or "").rsplit("/", 1)[-1].split(".")[0]
        img_key, sub_key = stem(img.get("img_url")), stem(img.get("sub_url"))
        if not img_key or not sub_key or len(img_key + sub_key) < 64:
            raise VideoUnavailable(BILI_DOWN_MSG)
        key = mixin_key(img_key, sub_key)
        self._wbi = (key, time.time())
        return key

    async def _bili_api(self, session, url: str, params: dict) -> dict:
        await self._ensure_buvid(session)
        resp: dict = {}
        for attempt in (0, 1):
            key = await self._ensure_wbi(session, force=attempt == 1)
            query = wbi_sign(params, key, int(time.time()))
            resp = await self._get_json(session, f"{url}?{query}", self._bili_headers())
            if resp.get("code") not in RETRY_CODES:
                break
        return resp

    async def _expand_short(self, session, code: str) -> Link | None:
        async with session.get(f"https://b23.tv/{code}", allow_redirects=False,
                               headers={"User-Agent": USER_AGENT}) as r:
            if not 300 <= r.status < 400:
                return None
            loc = r.headers.get("Location", "")
        link = find_link(loc)
        return link if link and link.source == "bilibili" and not link.short else None

    async def _playurl(self, session, bvid: str, cid: int) -> DashStreams:
        resp = await self._bili_api(session, PLAYURL_URL, {
            "bvid": bvid, "cid": cid, "qn": self.max_qn, "fnval": 16, "fourk": 0})
        return parse_playurl(resp, self.max_qn, time.time())

    # -- 对外 --
    async def resolve(self, text: str) -> Video:
        link = find_link(text)
        if not link:
            raise VideoUnavailable("没认出视频链接，目前支持 B 站和 YouTube")
        try:
            async with self._session_factory() as session:
                if link.short:
                    link = await self._expand_short(session, link.id)
                    if not link:
                        raise VideoUnavailable("这个短链打不开")
                if link.source == "bilibili":
                    resp = await self._bili_api(session, VIEW_URL, {"bvid": link.id})
                    title, cid, duration = parse_view(resp, link.page)
                    # view 能取到不代表能播（地区限制常常只在 playurl 上报错），建房 / 换视频前先试一次
                    await self._playurl(session, link.id, cid)
                    return Video("bilibili", link.id, link.page, title, duration, bili_url(link.id, link.page))
                watch = f"https://www.youtube.com/watch?v={link.id}"
                query = urllib.parse.urlencode({"url": watch, "format": "json"})
                async with session.get(f"{OEMBED_URL}?{query}", headers={"User-Agent": USER_AGENT}) as r:
                    if r.status != 200:
                        raise VideoUnavailable(YT_NO_EMBED_MSG)
                    try:
                        data = await r.json(content_type=None)
                    except ValueError as e:
                        raise VideoUnavailable(YT_NO_EMBED_MSG) from e
                if not isinstance(data, dict):
                    raise VideoUnavailable(YT_NO_EMBED_MSG)
                title = data.get("title")
                return Video("youtube", link.id, 1, title if isinstance(title, str) else "", None, watch)
        except VideoUnavailable:
            raise
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("解析视频失败：%s", type(e).__name__)
            raise VideoUnavailable(YT_DOWN_MSG if link.source == "youtube" else BILI_DOWN_MSG) from e

    async def bili_streams(self, video: Video) -> DashStreams:
        try:
            async with self._session_factory() as session:
                resp = await self._bili_api(session, VIEW_URL, {"bvid": video.id})
                _t, cid, _d = parse_view(resp, video.page)
                return await self._playurl(session, video.id, cid)
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("取播放地址失败：%s", type(e).__name__)
            raise VideoUnavailable(BILI_DOWN_MSG) from e
