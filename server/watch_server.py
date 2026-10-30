"""一起看网页服务：房间页、WebSocket 同步协议、B 站 DASH 转流、资源站剧集的 m3u8。

路由：
- GET /r/{room_id}                          房间页（index.html）；房间不存在 → 404 页面
- GET /static/{name}                        watch_web 下白名单里的文件
- GET /ws/{room_id}                         WebSocket（协议见 watch_web/player.js）
- GET /bili/{room_id}/{version}/manifest.mpd   本服务器生成的 MPD
- GET /bili/{room_id}/{version}/{video|audio}  转发 B 站 CDN（只请求 Sources 给的地址）
- GET /vod/{room_id}/{version}/index.m3u8      改写过（去广告、地址绝对）的 vod 播放列表；
                                               片段由浏览器直接从资源站 CDN 取，本服务器只取房间当前集的 m3u8
- GET /admin                                   管理后台：没登录显示输入密钥的页面，登录后是 admin.html（房间、观众、
                                               「想看但没有」的片、被拦的 IP、服务器资源）。没配密钥时 404
- POST /admin/login（表单字段 key、password）  密钥 + 密码都对 → 写 cookie（值是两者派生的令牌）、303 回 /admin；
                                               错 → 登录页带提示（401）；同一 IP 错太多次 429
- GET /admin/logout                            清 cookie
- GET /admin/api                               后台页面轮询的 JSON（令牌放 cookie 或 X-Admin-Token 头），没登录 401

vod 房间额外的客户端消息（当前视频不是 vod 时一律忽略）：
- {"type": "episode", "index": int}   房主选集（从 0 开始），停在开头、暂停，优先用当前片源线路
- {"type": "next_line"}               房主换一个片源：同一集、下一条线路（绕圈），保持当前位置和播放/暂停状态
- {"type": "resume"}                  房主或有控制权的人：跳到建房时的进度快照（需要时先换集），暂停
播完（rooms 的 "finished" 事件）自动换下一集并开始播放；期间有人手动换过就作废（按版本号）。
准备一集（找线路、取 m3u8、探测片段）整体最多 VOD_PREPARE_TIMEOUT 秒，超时按“没有能播的片源”处理。

地区限制（geo 不为 None 时）：房间页照常打开；WebSocket 连上后先按客户端 IP 查国家，不在放行名单里就只发一条
{"type": "blocked", "id", "country", "message"} 然后关闭（不进房、不算人数），/bili、/vod 的媒体路由返回 403；
被拦的 IP、编号、推测国家记在 BlockLog，管理后台能看。地区数据还没加载好时不拦。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import hashlib
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape, quoteattr

import aiohttp
from aiohttp import web

from geoip import BLOCKED_MSG, BlockLog, GeoDB, client_ip
from live_watch import USER_AGENT
from sysstats import SystemStats
from vod_playlist import NO_PLAYABLE, Playlist, PlaylistError, fetch_playlist, find_playable
from vod_search import Show
from watch_progress import ProgressRecorder, resume_line
from watch_requests import KIND_LABEL, RequestStore
from watch_rooms import Room, RoomManager, RoomsFull, Video, VodInfo, vod_video
from watch_sources import BILI_REFERER, DashStreams, Sources, VideoUnavailable

log = logging.getLogger(__name__)

TICK_SECONDS = 5
HELLO_TIMEOUT = 10
REFRESH_MARGIN = 60          # 地址过期前多少秒就重新取
CHUNK = 64 * 1024
VOD_PREPARE_TIMEOUT = 45     # 准备一集的总时限（秒）；网页端的“正在准备…”要比它久
SEND_QUEUE_MAX = 256         # 单个连接积压的待发消息上限，超过就断开
MAX_URL_LEN = 2000
STATIC_FILES = {
    "index.html": "text/html",
    "player.js": "application/javascript",
    "cast.js": "application/javascript",
    "style.css": "text/css",
    "admin.js": "application/javascript",
}
ADMIN_COOKIE = "watch_admin"
ADMIN_LOGIN_MAX_FAILURES = 10        # 同一 IP 连续输错这么多次就锁
ADMIN_LOGIN_LOCK_SECONDS = 900       # 锁 15 分钟
ADMIN_LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>一起看 · 管理后台</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f1115;color:#e6e6e6;
font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
form{background:#1a1d24;padding:24px 28px;border-radius:10px;display:flex;flex-direction:column;gap:12px;min-width:260px}
h1{font-size:16px;margin:0;font-weight:600}input{font:inherit;color:inherit;background:#0f1115;border:1px solid #2a2f3a;
border-radius:6px;padding:8px 10px}input:focus{border-color:#7c8cff;outline:none}
button{font:inherit;background:#7c8cff;color:#0f1115;border:0;border-radius:6px;padding:8px;cursor:pointer;font-weight:600}
.err{color:#e5676b;font-size:13px;margin:0}</style></head>
<body><form method="post" action="/admin/login"><h1>🎬 一起看 · 管理后台</h1>
<input type="text" name="key" placeholder="密钥" autofocus autocomplete="username" required>
{password_field}<button type="submit">进入</button>{error}</form></body></html>
"""
ADMIN_PASSWORD_FIELD = ('<input type="password" name="password" placeholder="密码" '
                        'autocomplete="current-password" required>' + chr(10))
ADMIN_WRONG_KEY = '<p class="err">密钥或密码不对</p>'
ADMIN_LOCKED = '<p class="err">输错太多次了，请 15 分钟后再试</p>'
STATS_MIN_INTERVAL = 2.0     # 资源用量采样的最短间隔（秒）
SOURCE_LABEL = {"bilibili": "B 站", "youtube": "YouTube", "vod": "片源"}
NOT_FOUND_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>一起看</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1115;color:#e6e6e6;font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}</style>
</head><body><p>房间不存在或已经结束</p></body></html>
"""
CHANGING_MSG = "正在换视频，请稍候"
ROOMS_FULL_MSG = "B 站房间已满（最多 {limit} 个），请稍后再试或换 YouTube 视频"


def build_mpd(s: DashStreams, room_id: str, version: int) -> str:
    """静态 MPD：video、audio 各一个 Representation，BaseURL 指向本服务器的转流地址。"""
    base = f"/bili/{room_id}/{version}"
    dur = f"PT{max(0.0, float(s.duration or 0)):.3f}S"

    def rep(kind, mime, attrs, init, index):
        attr_s = " ".join(f"{k}={quoteattr(str(v))}" for k, v in attrs.items())
        return (f'  <AdaptationSet contentType="{kind}" mimeType="{mime}" segmentAlignment="true" startWithSAP="1">\n'
                f'   <Representation {attr_s}>\n'
                f'    <BaseURL>{escape(base + "/" + kind)}</BaseURL>\n'
                f'    <SegmentBase indexRange={quoteattr(index)}>'
                f'<Initialization range={quoteattr(init)}/></SegmentBase>\n'
                f'   </Representation>\n'
                f'  </AdaptationSet>\n')

    video = rep("video", "video/mp4", {
        "id": "video", "bandwidth": s.video_bandwidth, "codecs": s.video_codecs,
        "width": s.width, "height": s.height, "frameRate": s.frame_rate,
    }, s.video_init, s.video_index)
    audio = rep("audio", "audio/mp4", {
        "id": "audio", "bandwidth": s.audio_bandwidth, "codecs": s.audio_codecs,
    }, s.audio_init, s.audio_index)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" profiles="urn:mpeg:dash:profile:isoff-on-demand:2011"'
            f' type="static" mediaPresentationDuration="{dur}" minBufferTime="PT1.5S">\n'
            ' <Period start="PT0S">\n' + video + audio + ' </Period>\n</MPD>\n')


def _finite(v) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def load_admin_key(path: Path | None, configured: str | None = None) -> str:
    """管理后台密钥：.env 里的 WATCH_ADMIN_KEY 优先；没配就生成一个存在 path（重启后链接不变），存不了就只在内存里。"""
    if configured and configured.strip():
        return configured.strip()
    if path is not None:
        try:
            key = Path(path).read_text(encoding="utf-8").strip()
            if key:
                return key
        except OSError:
            pass
    key = secrets.token_urlsafe(24)
    if path is not None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            Path(path).write_text(key, encoding="utf-8")
        except OSError:
            log.warning("Could not save the watch admin key to %s; it will change on restart", path)
    return key


def _key_matches(given, expected: str) -> bool:
    return (isinstance(given, str) and bool(expected)
            and secrets.compare_digest(given.encode("utf-8", "surrogatepass"),
                                       expected.encode("utf-8", "surrogatepass")))


@dataclass(eq=False)
class _Conn:
    """一个 WebSocket 连接；消息经队列由单独的任务按顺序发出，监听回调里不阻塞。"""
    ws: web.WebSocketResponse
    room_id: str
    viewer_id: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(SEND_QUEUE_MAX))
    last_you: tuple | None = None
    writer: asyncio.Task | None = None
    busy: asyncio.Task | None = None     # 正在处理的换视频（change_video / episode / next_line / resume）

    def push(self, msg: dict | None) -> None:
        try:
            self.queue.put_nowait(msg)
        except asyncio.QueueFull:
            log.warning("watch ws send queue full, dropping connection (room=%s)", self.room_id)
            # 清空后只留关闭信号
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(None)


class WatchServer:
    def __init__(self, rooms: RoomManager, sources: Sources, host: str, port: int,
                 web_dir: Path = Path(__file__).parent / "watch_web", origin: str = "",
                 recorder: ProgressRecorder | None = None, allow_http: bool = False,
                 vod_prepare_timeout: float = VOD_PREPARE_TIMEOUT, admin_key: str | None = None,
                 requests: RequestStore | None = None, stats: SystemStats | None = None,
                 channel_name: Callable[[int], str | None] | None = None,
                 geo: GeoDB | None = None, blocks: BlockLog | None = None, admin_password: str | None = None):
        """origin = WATCH_PUBLIC_URL（探测片段跨域用）；recorder 自己注册监听，这里只定时调 tick()。
        allow_http 仅供测试（连本地 http 假上游），生产不要传；vod_prepare_timeout 测试里调小。
        admin_key 为空时没有管理后台（/admin 一律 404）；admin_password 配了就要密钥 + 密码一起输。
        requests、stats、channel_name 只给管理后台用。
        geo 为 None 时不限制地区；blocks 记被拦的 IP。"""
        self.geo = geo
        self.blocks = blocks
        self.rooms = rooms
        self.sources = sources
        self.host = host
        self.port = port
        self.web_dir = Path(web_dir)
        self.origin = origin
        self.recorder = recorder
        self.allow_http = allow_http
        self.vod_prepare_timeout = vod_prepare_timeout
        self.admin_key = admin_key or None
        self.admin_password = (admin_password or None) if self.admin_key else None
        # 登录后 cookie 里存的令牌：由密钥和密码派生，只知道密钥拼不出来
        self.admin_token = (hashlib.sha256((self.admin_key + "\n" + (self.admin_password or ""))
                                           .encode("utf-8", "surrogatepass")).hexdigest()
                            if self.admin_key else None)
        self.requests = requests
        self.stats = stats
        self.channel_name = channel_name
        self.started_at = time.time()
        self._stats_cache: dict | None = None
        self._stats_at = float("-inf")
        self.conns: dict[str, set[_Conn]] = {}
        self._streams: dict[tuple[str, int], DashStreams] = {}
        self._stream_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._playlists: dict[tuple[str, int], Playlist] = {}
        self._playlist_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()     # 自动下一集的后台任务
        self._session: aiohttp.ClientSession | None = None
        self._vod_session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self.bili_bytes = 0          # 当天（UTC）转流字节数
        rooms.listeners.append(self._on_room_event)

        self.app = web.Application()
        self.app.router.add_get("/r/{room_id}", self._page)
        self.app.router.add_get("/static/{name}", self._static)
        self.app.router.add_get("/ws/{room_id}", self._ws)
        self.app.router.add_get("/bili/{room_id}/{version}/{name}", self._bili)
        self.app.router.add_get("/vod/{room_id}/{version}/index.m3u8", self._vod)
        self.app.router.add_get("/admin", self._admin_page)
        self.app.router.add_post("/admin/login", self._admin_login)
        self.app.router.add_get("/admin/logout", self._admin_logout)
        self.app.router.add_get("/admin/api", self._admin_api)
        self._login_failures: dict[str, list[float]] = {}     # ip -> 最近失败时间
        self._sockets: set[web.WebSocketResponse] = set()
        self.app.on_shutdown.append(self._shutdown)
        self.app.on_cleanup.append(self._cleanup)

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        # 不要 access log：每个转流分片都会记一行，而且房主链接的 ?k= 密钥会被写进日志
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("watch server listening on %s:%s", self.host, self.port)

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()     # 依次触发 on_shutdown、等处理函数结束、on_cleanup
            self._runner = None

    async def _shutdown(self, _app) -> None:
        """先关掉所有 WebSocket，否则 AppRunner.cleanup() 会等到 shutdown_timeout 才继续。"""
        for conns in list(self.conns.values()):
            for c in list(conns):
                c.push(None)
        sockets = [ws for ws in self._sockets if not ws.closed]
        if sockets:
            await asyncio.wait([asyncio.ensure_future(ws.close(code=aiohttp.WSCloseCode.GOING_AWAY,
                                                               message=b"server shutdown"))
                                for ws in sockets], timeout=2)

    async def _cleanup(self, _app) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        for name in ("_session", "_vod_session"):
            session = getattr(self, name)
            if session is not None:
                await session.close()
                setattr(self, name, None)

    async def run(self) -> None:
        """每 TICK_SECONDS 秒 rooms.tick() 和 recorder.tick()；每天 UTC 0 点记一次 B 站转流字节数。"""
        day = datetime.datetime.now(datetime.timezone.utc).date()
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                self.rooms.tick()
            except Exception:
                log.exception("watch rooms tick failed")
            if self.recorder is not None:
                try:
                    self.recorder.tick()
                except Exception:
                    log.exception("watch progress tick failed")
            try:
                self._stats_now()      # 定期采样，后台页面打开时 CPU、网速马上有数
            except Exception:
                log.exception("system stats sample failed")
            today = datetime.datetime.now(datetime.timezone.utc).date()
            if today != day:
                log.info("一起看 B 站转流 %s：%d 字节（%.1f MiB）", day.isoformat(), self.bili_bytes,
                         self.bili_bytes / 1048576)
                self.bili_bytes = 0
                day = today

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                auto_decompress=False,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30))
        return self._session

    def _vod_http(self) -> aiohttp.ClientSession:
        """取 m3u8 用：要自动解压（_http() 为了原样转流关掉了解压）；超时由 vod_playlist 按请求设置。"""
        if self._vod_session is None or self._vod_session.closed:
            self._vod_session = aiohttp.ClientSession()
        return self._vod_session

    # ---------------- 页面 ----------------
    async def _page(self, request: web.Request) -> web.StreamResponse:
        if self.rooms.get(request.match_info["room_id"]) is None:
            return web.Response(status=404, text=NOT_FOUND_HTML, content_type="text/html")
        return web.Response(body=(self.web_dir / "index.html").read_bytes(), content_type="text/html",
                            charset="utf-8", headers={"Cache-Control": "no-store"})

    async def _static(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        ctype = STATIC_FILES.get(name)
        if ctype is None:
            raise web.HTTPNotFound()
        return web.Response(body=(self.web_dir / name).read_bytes(), content_type=ctype, charset="utf-8",
                            headers={"Cache-Control": "no-cache"})

    # ---------------- 地区限制 ----------------
    def _geo_block(self, request: web.Request, room_id: str) -> dict | None:
        """被拦返回 {"id", "country"}（并记一笔），放行 / 没开限制 / 数据没加载返回 None。"""
        if self.geo is None:
            return None
        ip = client_ip(request)
        verdict = self.geo.check(ip)
        if verdict.allowed is not False:
            return None
        entry_id = ""
        if self.blocks is not None:
            entry_id = self.blocks.record(ip, verdict.country, room_id).id
        log.info("watch geo-blocked ip=%s country=%s room=%s id=%s", ip, verdict.country, room_id, entry_id)
        return {"id": entry_id, "country": verdict.country}

    # ---------------- 管理后台 ----------------
    def _admin_ok(self, request: web.Request) -> bool:
        if self.admin_token is None:
            return False
        for given in (request.headers.get("X-Admin-Token"), request.cookies.get(ADMIN_COOKIE)):
            if _key_matches(given, self.admin_token):
                return True
        return False

    def _credentials_ok(self, key, password) -> bool:
        if not _key_matches(key, self.admin_key):
            return False
        return self.admin_password is None or _key_matches(password, self.admin_password)

    def _set_admin_cookie(self, resp: web.StreamResponse) -> None:
        resp.set_cookie(ADMIN_COOKIE, self.admin_token, path="/admin", httponly=True, samesite="Strict",
                        secure=self.origin.startswith("https"), max_age=30 * 86400)

    def _login_locked(self, ip: str) -> bool:
        now = time.time()
        fails = [t for t in self._login_failures.get(ip, ()) if now - t < ADMIN_LOGIN_LOCK_SECONDS]
        if fails:
            self._login_failures[ip] = fails
        else:
            self._login_failures.pop(ip, None)
        return len(fails) >= ADMIN_LOGIN_MAX_FAILURES

    def _login_page(self, error: str = "", status: int = 200) -> web.Response:
        # 模板里有 CSS 的花括号，不能用 str.format
        html = (ADMIN_LOGIN_HTML.replace("{error}", error)
                .replace("{password_field}", ADMIN_PASSWORD_FIELD if self.admin_password else ""))
        return web.Response(text=html, status=status, content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store"})

    async def _admin_page(self, request: web.Request) -> web.StreamResponse:
        if self.admin_key is None:
            raise web.HTTPNotFound()
        if not self._admin_ok(request):
            return self._login_page()
        return web.Response(body=(self.web_dir / "admin.html").read_bytes(), content_type="text/html",
                            charset="utf-8", headers={"Cache-Control": "no-store"})

    async def _admin_login(self, request: web.Request) -> web.StreamResponse:
        if self.admin_key is None:
            raise web.HTTPNotFound()
        ip = client_ip(request) or "?"
        if self._login_locked(ip):
            return self._login_page(ADMIN_LOCKED, status=429)
        form = await request.post()
        if not self._credentials_ok(form.get("key"), form.get("password")):
            self._login_failures.setdefault(ip, []).append(time.time())
            log.info("watch admin login failed from %s", ip)
            return self._login_page(ADMIN_WRONG_KEY, status=401)
        self._login_failures.pop(ip, None)
        resp = web.HTTPSeeOther("/admin")
        self._set_admin_cookie(resp)
        return resp

    async def _admin_logout(self, request: web.Request) -> web.StreamResponse:
        resp = web.HTTPSeeOther("/admin")
        resp.del_cookie(ADMIN_COOKIE, path="/admin")
        return resp

    async def _admin_api(self, request: web.Request) -> web.StreamResponse:
        if self.admin_key is None:
            raise web.HTTPNotFound()
        if not self._admin_ok(request):
            raise web.HTTPUnauthorized()
        return web.json_response(self.admin_snapshot(), headers={"Cache-Control": "no-store"},
                                 dumps=lambda o: json.dumps(o, ensure_ascii=False))

    def _stats_now(self) -> dict | None:
        if self.stats is None:
            return None
        now = time.monotonic()
        if self._stats_cache is None or now - self._stats_at >= STATS_MIN_INTERVAL:
            self._stats_cache, self._stats_at = self.stats.sample(), now
        return self._stats_cache

    def _channel(self, channel_id: int) -> dict:
        name = None
        if self.channel_name is not None:
            try:
                name = self.channel_name(channel_id)
            except Exception:
                log.exception("channel_name lookup failed for %s", channel_id)
        return {"id": str(channel_id), "name": name}

    def _room_info(self, room: Room, now: float) -> dict:
        v, vod = room.video, room.video.vod
        pos = room.playback.position_at(now, v.duration)
        if vod is not None and 0 <= vod.line < len(vod.show.lines):
            source = f"片源：{vod.show.lines[vod.line].name}"
        else:
            source = SOURCE_LABEL.get(v.source, v.source)
        return {
            "room_id": room.room_id,
            "url": f"/r/{room.room_id}",
            "channel": self._channel(room.channel_id),
            "title": v.title,
            "source": v.source,
            "source_label": source,
            "show_title": vod.show.title if vod is not None else None,
            "episode": vod.episode if vod is not None else None,
            "episode_count": vod.show.episode_count if vod is not None else None,
            "playing": room.playback.playing,
            "finished": bool(v.duration and pos >= v.duration),
            "position": pos,
            "duration": v.duration,
            "viewers": [{"name": m.name, "is_host": m.viewer_id == room.host_id, "can_control": m.can_control,
                         "online": m.connections > 0} for m in room.present()],
            "host": room.host_name() if room.present() else None,
            "creator": room.creator_name,
            "created_at": room.created_at,
            "age": max(0.0, self.rooms.clock() - room.created_at),
            "version": room.version,
        }

    def admin_snapshot(self) -> dict:
        """后台页面轮询的全部内容。房主密钥和 viewer_id 都不在里面。"""
        now = time.time()
        rooms = [self._room_info(r, now) for r in sorted(self.rooms.active(), key=lambda r: r.created_at)]
        requests = []
        if self.requests is not None:
            requests = [{"query": r.query, "key": r.key, "kind": r.kind, "kind_label": KIND_LABEL.get(r.kind, r.kind),
                         "user_name": r.user_name, "channel": self._channel(r.channel_id), "at": r.at}
                        for r in self.requests.all()]
        return {
            "server_time": now,
            "started_at": self.started_at,
            "summary": {"rooms": len(rooms), "viewers": sum(len(r["viewers"]) for r in rooms),
                        "bili_rooms": sum(1 for r in rooms if r["source"] == "bilibili"),
                        "bili_bytes_today": self.bili_bytes},
            "rooms": rooms,
            "requests": requests,
            "request_limit": self.requests.limit if self.requests is not None else None,
            "stats": self._stats_now(),
            "geo": {"enabled": True, **self.geo.status()} if self.geo is not None else {"enabled": False},
            "blocked": [{"ip": e.ip, "id": e.id, "country": e.country, "rooms": list(e.rooms), "hits": e.hits,
                         "first_at": e.first_at, "last_at": e.last_at}
                        for e in self.blocks.all()] if self.blocks is not None else [],
        }

    # ---------------- 推送 ----------------
    def _state_msg(self, room: Room) -> dict:
        v = room.video
        now = time.time()
        video = {"source": v.source, "id": v.id, "title": v.title, "duration": v.duration,
                 "version": room.version,
                 "mpd": f"/bili/{room.room_id}/{room.version}/manifest.mpd" if v.source == "bilibili" else None,
                 "hls": f"/vod/{room.room_id}/{room.version}/index.m3u8" if v.vod is not None else None}
        resume = None
        if v.vod is not None:
            show = v.vod.show
            video.update(episode=v.vod.episode, episode_count=show.episode_count,
                         line_name=show.lines[v.vod.line].name, line_count=len(show.lines), show_title=show.title)
            snap = v.vod.resume
            if snap is not None:
                resume = {"episode": snap.episode, "position": snap.position,
                          "text": resume_line(snap, show.episode_count)}
        return {
            "type": "state",
            "video": video,
            "playing": room.playback.playing,
            "position": room.playback.position_at(now, v.duration),
            "server_time": now,
            "resume": resume,
        }

    def _you(self, room: Room, viewer_id: str) -> tuple[bool, bool]:
        member = room.members.get(viewer_id)
        is_host = room.host_id is not None and room.host_id == viewer_id
        return is_host, bool(is_host or (member is not None and member.can_control))

    def _members_msg(self, room: Room) -> dict:
        """只用公开 id；viewer_id 是重连凭据，只在 welcome 里发给本人。"""
        return {"type": "members", "members": [
            {"id": m.public_id, "name": m.name, "can_control": m.can_control}
            for m in room.present() if m.viewer_id != room.host_id]}

    def _send_you(self, room: Room, conn: _Conn, force: bool = False) -> None:
        you = self._you(room, conn.viewer_id)
        if force or you != conn.last_you:
            conn.last_you = you
            conn.push({"type": "you", "is_host": you[0], "can_control": you[1]})

    def _send_members_to_host(self, room: Room) -> None:
        hosts = [c for c in self.conns.get(room.room_id, ()) if c.viewer_id == room.host_id]
        if hosts:
            msg = self._members_msg(room)
            for c in hosts:
                c.push(msg)

    def _on_room_event(self, room: Room, kind: str) -> None:
        """RoomManager 的同步回调：只往各连接的发送队列里放消息，不阻塞。"""
        conns = list(self.conns.get(room.room_id, ()))
        if kind in ("playback", "video"):
            if kind == "video":
                self._evict(room.room_id, keep=room.version)
            msg = self._state_msg(room)
            for c in conns:
                c.push(msg)
        elif kind in ("permissions", "host"):
            for c in conns:
                self._send_you(room, c)
            self._send_members_to_host(room)
        elif kind == "members":
            self._send_members_to_host(room)
        elif kind == "closed":
            for c in conns:
                c.push({"type": "ended"})
                c.push(None)
            self.conns.pop(room.room_id, None)
            self._evict(room.room_id)
        elif kind == "finished":
            self._start_auto_next(room)

    def _evict(self, room_id: str, keep: int | None = None) -> None:
        """清掉这个房间除 keep 版本以外的 B 站地址和 vod 播放列表缓存。"""
        for cache, locks in ((self._streams, self._stream_locks), (self._playlists, self._playlist_locks)):
            for key in [k for k in cache if k[0] == room_id and k[1] != keep]:
                cache.pop(key, None)
            for key in [k for k in locks if k[0] == room_id and k[1] != keep]:
                locks.pop(key, None)

    async def _writer(self, conn: _Conn) -> None:
        try:
            while True:
                msg = await conn.queue.get()
                if msg is None:
                    break
                await conn.ws.send_str(json.dumps(msg, ensure_ascii=False))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass   # 连接已断开
        finally:
            if not conn.ws.closed:
                try:
                    await conn.ws.close()
                except Exception:
                    pass

    # ---------------- WebSocket ----------------
    async def _ws(self, request: web.Request) -> web.StreamResponse:
        room_id = request.match_info["room_id"]
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024)
        await ws.prepare(request)
        self._sockets.add(ws)
        try:
            blocked = self._geo_block(request, room_id) if self.rooms.get(room_id) is not None else None
            return await self._ws_session(ws, room_id, blocked)
        finally:
            self._sockets.discard(ws)

    async def _ws_session(self, ws: web.WebSocketResponse, room_id: str,
                          blocked: dict | None = None) -> web.WebSocketResponse:
        async def end(msg: dict | None = None):
            try:
                await ws.send_str(json.dumps(msg or {"type": "ended"}, ensure_ascii=False))
                await ws.close()
            except (ConnectionResetError, aiohttp.ClientError):
                pass   # 客户端已经走了

        if self.rooms.get(room_id) is None:
            await end()
            return ws
        if blocked is not None:
            # 地区不在放行名单：告诉他编号和原因就断开，不进房、不发房间状态
            await end({"type": "blocked", "id": blocked["id"], "country": blocked["country"], "message": BLOCKED_MSG})
            return ws
        try:
            first = await ws.receive(timeout=HELLO_TIMEOUT)
        except asyncio.TimeoutError:
            await ws.close()
            return ws
        hello = self._parse(first)
        if hello is None or hello.get("type") != "hello":
            await ws.close()
            return ws
        viewer_id = hello.get("viewer_id")
        key = hello.get("key")
        room = self.rooms.get(room_id)
        if room is None:
            await end()
            return ws
        member = self.rooms.connect(room_id, viewer_id if isinstance(viewer_id, str) else None,
                                    key if isinstance(key, str) else None)
        conn = _Conn(ws, room_id, member.viewer_id)
        try:
            self.conns.setdefault(room_id, set()).add(conn)
            conn.writer = asyncio.get_running_loop().create_task(self._writer(conn))
            is_host, can_control = self._you(room, member.viewer_id)
            conn.last_you = (is_host, can_control)
            conn.push({"type": "welcome", "viewer_id": member.viewer_id, "name": member.name,
                       "is_host": is_host, "can_control": can_control})
            conn.push(self._state_msg(room))
            if is_host:
                conn.push(self._members_msg(room))
            async for msg in ws:
                data = self._parse(msg)
                if data is not None:
                    self._handle(conn, data)
        finally:
            room_conns = self.conns.get(room_id)
            if room_conns is not None:
                room_conns.discard(conn)
                if not room_conns:
                    self.conns.pop(room_id, None)
            if conn.busy is not None:
                conn.busy.cancel()
            conn.push(None)
            self.rooms.disconnect(room_id, member.viewer_id)
            if conn.writer is not None:
                try:
                    await asyncio.wait_for(conn.writer, timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    conn.writer.cancel()
        return ws

    @staticmethod
    def _parse(msg) -> dict | None:
        if msg.type != aiohttp.WSMsgType.TEXT:
            return None
        try:
            data = json.loads(msg.data)
        except ValueError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("type"), str):
            return None
        return data

    def _handle(self, conn: _Conn, data: dict) -> None:
        kind = data["type"]
        rid, vid = conn.room_id, conn.viewer_id
        if kind == "ping":
            if _finite(data.get("t")):
                conn.push({"type": "pong", "t": data["t"], "server_time": time.time()})
        elif kind == "control":
            action, pos = data.get("action"), data.get("position")
            if action in ("play", "pause", "seek") and _finite(pos):
                if not self.rooms.control(rid, vid, action, float(pos)):
                    room = self.rooms.get(rid)
                    if room is not None:
                        conn.push(self._state_msg(room))     # 被拒绝：让发送者撤回乐观更新
        elif kind == "duration":
            room = self.rooms.get(rid)
            if room is not None and self._you(room, vid)[1] and _finite(data.get("value")):
                self.rooms.set_duration(rid, float(data["value"]))
        elif kind == "grant":
            target, allowed = data.get("target"), data.get("allowed")
            if isinstance(target, str) and isinstance(allowed, bool):
                self.rooms.set_permission(rid, vid, target, allowed)
        elif kind == "change_video":
            url = data.get("url")
            room = self.rooms.get(rid)
            if not (isinstance(url, str) and 0 < len(url) <= MAX_URL_LEN and room is not None
                    and room.host_id == vid):
                return
            self._start_busy(conn, self._change_video(conn, url))
        elif kind in ("episode", "next_line", "resume"):
            self._handle_vod(conn, kind, data)

    def _start_busy(self, conn: _Conn, coro) -> None:
        """每个连接同一时刻只换一次视频；正在换就提示请稍候。"""
        if conn.busy is not None and not conn.busy.done():
            coro.close()
            conn.push({"type": "error", "message": CHANGING_MSG})
        else:
            conn.busy = asyncio.get_running_loop().create_task(coro)

    def _handle_vod(self, conn: _Conn, kind: str, data: dict) -> None:
        room = self.rooms.get(conn.room_id)
        if room is None or room.video.vod is None:
            return
        info = room.video.vod
        is_host, can_control = self._you(room, conn.viewer_id)
        if kind == "episode":
            index = data.get("index")
            if not (is_host and type(index) is int and 0 <= index < info.show.episode_count):
                return
            self._start_busy(conn, self._vod_switch(conn, kind, index))
        elif kind == "next_line":
            if is_host:
                self._start_busy(conn, self._vod_switch(conn, kind))
        elif kind == "resume":
            snap = info.resume
            if (can_control and snap is not None and 0 <= snap.episode < info.show.episode_count
                    and _finite(snap.position)):
                self._start_busy(conn, self._vod_switch(conn, kind))

    async def _change_video(self, conn: _Conn, url: str) -> None:
        try:
            video = await self.sources.resolve(url)
            self.rooms.change_video(conn.room_id, conn.viewer_id, video)
        except VideoUnavailable as e:
            conn.push({"type": "error", "message": e.message if hasattr(e, "message") else str(e)})
        except RoomsFull as e:
            conn.push({"type": "error", "message": ROOMS_FULL_MSG.format(limit=e.limit)})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watch change_video failed")
            conn.push({"type": "error", "message": "换视频失败，请稍后再试"})

    # ---------------- vod ----------------
    async def prepare_vod(self, show: Show, episode: int, start_line: int = 0,
                          resume: object | None = None) -> tuple[Video, Playlist]:
        """从 start_line 起找能播这一集的线路，返回 (vod 视频, 改写过的播放列表)；
        都不能播或超过 vod_prepare_timeout 秒抛 PlaylistError。建房、选集、换片源、续看、自动下一集都走这里。"""
        try:
            line, playlist = await asyncio.wait_for(
                find_playable(self._vod_http(), show, episode, start_line, self.origin, self.allow_http),
                self.vod_prepare_timeout)
        except asyncio.TimeoutError:
            log.info("watch vod prepare timed out (%r episode %d)", show.title, episode)
            raise PlaylistError(NO_PLAYABLE) from None
        return vod_video(VodInfo(show, episode, line, resume), playlist.duration), playlist

    def attach_playlist(self, room: Room, playlist: Playlist) -> None:
        """把播放列表缓存在房间当前版本下；每次换视频成功后都要调用。"""
        self._playlists[(room.room_id, room.version)] = playlist

    async def _vod_switch(self, conn: _Conn, kind: str, index: int | None = None) -> None:
        rid, vid = conn.room_id, conn.viewer_id
        try:
            room = self.rooms.get(rid)
            if room is None or room.video.vod is None:
                return
            info, version = room.video.vod, room.version
            if kind == "resume" and info.resume is None:
                return
            if kind == "resume" and info.resume.episode == info.episode:
                # 已经在快照那一集：不重新取播放列表，沿用当前版本的缓存
                playlist = self._playlists.get((rid, version))
                if self.rooms.resume(rid, vid, room.video, info.resume.position) and playlist is not None:
                    self.attach_playlist(room, playlist)
                return
            if kind == "episode":
                video, playlist = await self.prepare_vod(info.show, index, info.line, info.resume)
            elif kind == "next_line":
                video, playlist = await self.prepare_vod(info.show, info.episode, info.line + 1, info.resume)
            else:
                video, playlist = await self.prepare_vod(info.show, info.resume.episode, info.line, info.resume)
            room = self.rooms.get(rid)
            if room is None or room.video.vod is None:
                return
            if kind == "episode":
                ok = self.rooms.change_video(rid, vid, video)
            elif kind == "next_line":
                if room.version != version:
                    return      # 取片源期间已经换了集，这条线路的播放列表已经不对了
                pos = room.playback.position_at(self.rooms.clock(), room.video.duration)
                ok = self.rooms.change_video(rid, vid, video, position=pos, playing=room.playback.playing)
            else:
                ok = self.rooms.resume(rid, vid, video, info.resume.position)
            if ok:
                self.attach_playlist(room, playlist)
        except PlaylistError as e:
            conn.push({"type": "error", "message": e.message})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watch vod %s failed", kind)
            conn.push({"type": "error", "message": "换视频失败，请稍后再试"})

    def _start_auto_next(self, room: Room) -> None:
        """rooms 的同步回调里调用：记下当前版本号，后台准备下一集。"""
        info = room.video.vod
        if info is None or info.episode + 1 >= info.show.episode_count:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log.warning("watch auto-next skipped: no running event loop (room=%s)", room.room_id)
            return
        task = loop.create_task(self._auto_next(room.room_id, room.version, info))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _auto_next(self, room_id: str, expected_version: int, info: VodInfo) -> None:
        try:
            video, playlist = await self.prepare_vod(info.show, info.episode + 1, info.line, info.resume)
        except PlaylistError:
            log.info("watch auto-next: no playable source for next episode (room=%s)", room_id)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watch auto-next failed (room=%s)", room_id)
            return
        room = self.rooms.get(room_id)
        if room is not None and self.rooms.advance(room_id, expected_version, video):
            self.attach_playlist(room, playlist)

    async def _vod(self, request: web.Request) -> web.StreamResponse:
        room = self.rooms.get(request.match_info["room_id"])
        try:
            version = int(request.match_info["version"])
        except ValueError:
            raise web.HTTPNotFound()
        if room is None or room.video.vod is None or version != room.version:
            raise web.HTTPNotFound()
        if self._geo_block(request, room.room_id) is not None:
            raise web.HTTPForbidden(text=BLOCKED_MSG)
        key = (room.room_id, version)
        playlist = self._playlists.get(key)
        if playlist is None:
            lock = self._playlist_locks.setdefault(key, asyncio.Lock())
            async with lock:
                playlist = self._playlists.get(key)
                if playlist is None:
                    try:
                        playlist = await fetch_playlist(self._vod_http(), room.video.url, self.allow_http)
                    except PlaylistError as e:
                        log.warning("watch vod playlist unavailable (room=%s)", room.room_id)
                        raise web.HTTPBadGateway(text=e.message)
                    if room.closed or room.version != version:
                        raise web.HTTPNotFound()
                    self._playlists[key] = playlist
        # Chromecast 的接收端（Google 的页面）跨域来取这份列表，所以要放开 CORS；
        # 列表里没有任何秘密，片段地址本来就在资源站 CDN 上。
        return web.Response(text=playlist.text, content_type="application/vnd.apple.mpegurl",
                            headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})

    # ---------------- B 站转流 ----------------
    async def _get_streams(self, room: Room, version: int, stale: DashStreams | None = None) -> DashStreams:
        """取 (room, version) 的播放地址缓存；过期（或是 stale 那份）就重新取，同一时刻只取一次。"""
        key = (room.room_id, version)
        lock = self._stream_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._streams.get(key)
            if cached is not None and cached is not stale and time.time() < cached.expires_at - REFRESH_MARGIN:
                return cached
            fresh = await self.sources.bili_streams(room.video)
            if room.closed or room.version != version:
                raise VideoUnavailable("视频已更换")
            self._streams[key] = fresh
            return fresh

    async def _bili(self, request: web.Request) -> web.StreamResponse:
        room = self.rooms.get(request.match_info["room_id"])
        name = request.match_info["name"]
        try:
            version = int(request.match_info["version"])
        except ValueError:
            raise web.HTTPNotFound()
        if (room is None or room.video.source != "bilibili" or version != room.version
                or name not in ("manifest.mpd", "video", "audio")):
            raise web.HTTPNotFound()
        if self._geo_block(request, room.room_id) is not None:
            raise web.HTTPForbidden(text=BLOCKED_MSG)
        try:
            streams = await self._get_streams(room, version)
        except VideoUnavailable as e:
            log.warning("watch bili streams unavailable (room=%s): %s", room.room_id, e)
            raise web.HTTPBadGateway(text=e.message)
        if name == "manifest.mpd":
            return web.Response(text=build_mpd(streams, room.room_id, version), content_type="application/dash+xml",
                                headers={"Cache-Control": "no-store"})
        return await self._proxy(request, room, version, name, streams)

    async def _open_upstream(self, streams: DashStreams, kind: str, headers: dict):
        urls = ([streams.video_url, *streams.video_backup] if kind == "video"
                else [streams.audio_url, *streams.audio_backup])
        session = self._http()
        for url in urls:
            if not url:
                continue
            try:
                return await session.get(url, headers=headers, allow_redirects=True)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.info("watch bili upstream connect failed (%s), trying next", type(e).__name__)
        return None

    async def _proxy(self, request: web.Request, room: Room, version: int, kind: str,
                     streams: DashStreams) -> web.StreamResponse:
        headers = {"Referer": BILI_REFERER, "User-Agent": USER_AGENT}
        rng = request.headers.get("Range")
        if rng:
            headers["Range"] = rng
        upstream = await self._open_upstream(streams, kind, headers)
        if upstream is not None and upstream.status == 403:
            upstream.release()
            try:
                streams = await self._get_streams(room, version, stale=streams)
            except VideoUnavailable:
                raise web.HTTPBadGateway()
            upstream = await self._open_upstream(streams, kind, headers)
        if upstream is None:
            raise web.HTTPBadGateway()
        try:
            resp = web.StreamResponse(status=upstream.status)
            for h in ("Content-Range", "Content-Length", "Content-Type", "Accept-Ranges"):
                if h in upstream.headers:
                    resp.headers[h] = upstream.headers[h]
            await resp.prepare(request)
            async for chunk in upstream.content.iter_chunked(CHUNK):
                await resp.write(chunk)
                self.bili_bytes += len(chunk)
            await resp.write_eof()
            return resp
        except (ConnectionResetError, aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.debug("watch bili proxy aborted: %s", type(e).__name__)
            return resp
        finally:
            upstream.release()
