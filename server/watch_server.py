"""一起看网页服务：房间页、WebSocket 同步协议、B 站 DASH 转流、资源站剧集的 m3u8。

路由：
- GET /r/{room_id}                          房间页（index.html）；房间不存在 → 404 页面
- GET /static/{name}                        watch_web 下白名单里的文件
- GET /ws/{room_id}                         WebSocket（协议见 watch_web/player.js）
- GET /bili/{room_id}/{version}/manifest.mpd   本服务器生成的 MPD
- GET /bili/{room_id}/{version}/{video|audio}  转发 B 站 CDN（只请求 Sources 给的地址）
- GET /vod/{room_id}/{version}/index.m3u8      改写过（去广告、地址绝对）的 vod 播放列表；
                                               片段由浏览器直接从资源站 CDN 取，本服务器只取房间当前集的 m3u8

vod 房间额外的客户端消息（当前视频不是 vod 时一律忽略）：
- {"type": "episode", "index": int}   房主选集（从 0 开始），停在开头、暂停，优先用当前片源线路
- {"type": "next_line"}               房主换一个片源：同一集、下一条线路（绕圈），保持当前位置和播放/暂停状态
- {"type": "resume"}                  房主或有控制权的人：跳到建房时的进度快照（需要时先换集），暂停
播完（rooms 的 "finished" 事件）自动换下一集并开始播放；期间有人手动换过就作废（按版本号）。
准备一集（找线路、取 m3u8、探测片段）整体最多 VOD_PREPARE_TIMEOUT 秒，超时按“没有能播的片源”处理。

电视直播（source == "tv"）：
- GET /tv/{room_id}/{version}/index.m3u8      改写过的直播媒体列表（只含本站路径，见 tv_relay）
- GET /tv/{room_id}/{version}/s/{n}           按编号转发分片；没发过的编号 404，不碰上游
  房间不存在 / 不是 tv / 版本号不是当前的 → 404；上游失败 → 502；所有 /tv 响应都带 nosniff、no-store。
  每次请求后流若已判死（dead_reason），后台自动换源（按版本号，换过就作废）。
- {"type": "tv_next"}                 房主手动换下一个源（绕圈；绕回开头时清空坏源记录）
- {"type": "tv_stall", "version": v}  在场的人报告卡住；v 必须是当前版本，每个房间 STALL_COOLDOWN 秒内最多换一次
换源每个房间同一时刻只跑一个；成功后所有人收到 notice（TV_SWITCHED），自动换源找不到源时所有人收到 NO_TV_SOURCE。
"""
from __future__ import annotations

import asyncio
import datetime
import functools
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape, quoteattr

import aiohttp
from aiohttp import web

import tv_net
import tv_relay
import tv_stream
from live_watch import USER_AGENT
from tv_channels import Candidate, Channel
from vod_playlist import NO_PLAYABLE, Playlist, PlaylistError, fetch_playlist, find_playable
from vod_search import Show
from watch_progress import ProgressRecorder, resume_line
from watch_rooms import Room, RoomManager, RoomsFull, TvInfo, Video, VodInfo, tv_video, vod_video
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
    "style.css": "text/css",
}
NOT_FOUND_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>一起看</title>
<style>body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0f1115;color:#e6e6e6;font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}</style>
</head><body><p>房间不存在或已经结束</p></body></html>
"""
CHANGING_MSG = "正在换视频，请稍候"
ROOMS_FULL_MSG = "B 站房间已满（最多 {limit} 个），请稍后再试或换 YouTube 视频"
NO_TV_SOURCE = "这个台暂时没有能播的源"
TV_SWITCHED = "已切换片源"
TV_PREPARE_TIMEOUT = 20.0    # 开一个台（依次探测候选源）的总时限（秒）
STALL_COOLDOWN = 15.0        # tv_stall 引起的换源：每个房间至少隔这么久（秒）
TV_HEADERS = {"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"}


class TvUnavailable(Exception):
    """这个台没有能播的源（都探测失败，或超过准备时限）。"""

    def __init__(self, message: str = NO_TV_SOURCE):
        super().__init__(message)
        self.message = message


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
                 vod_prepare_timeout: float = VOD_PREPARE_TIMEOUT,
                 tv_session_factory: Callable[[], aiohttp.ClientSession] = tv_net.make_session,
                 ip_allowed=tv_net.is_public_ip, tv_prepare_timeout: float = TV_PREPARE_TIMEOUT):
        """origin = WATCH_PUBLIC_URL（探测片段跨域用）；recorder 自己注册监听，这里只定时调 tick()。
        allow_http 仅供测试（连本地 http 假上游），生产不要传；vod_prepare_timeout 测试里调小。
        tv_session_factory / ip_allowed 仅供测试放行 127.0.0.1，生产用默认（只连公网地址）。"""
        self.rooms = rooms
        self.sources = sources
        self.host = host
        self.port = port
        self.web_dir = Path(web_dir)
        self.origin = origin
        self.recorder = recorder
        self.allow_http = allow_http
        self.vod_prepare_timeout = vod_prepare_timeout
        self.tv_session_factory = tv_session_factory
        self.ip_allowed = ip_allowed
        self.tv_prepare_timeout = tv_prepare_timeout
        self.conns: dict[str, set[_Conn]] = {}
        self._streams: dict[tuple[str, int], DashStreams] = {}
        self._stream_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._playlists: dict[tuple[str, int], Playlist] = {}
        self._playlist_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()     # 自动下一集、直播换源的后台任务
        self._tv_streams: dict[tuple[str, int], tv_relay.LiveStream] = {}
        self._tv_dead_seen: set[tuple[str, int]] = set()   # 已经因为断流发起过自动换源的 (房间, 版本)
        self._tv_switching: set[str] = set()                # 正在换源的房间
        self._tv_last_switch: dict[str, float] = {}         # 房间 → 上次换源成功的时间（monotonic）
        self._session: aiohttp.ClientSession | None = None
        self._vod_session: aiohttp.ClientSession | None = None
        self._tv_session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self.bili_bytes = 0          # 当天（UTC）转流字节数
        rooms.listeners.append(self._on_room_event)

        self.app = web.Application()
        self.app.router.add_get("/r/{room_id}", self._page)
        self.app.router.add_get("/static/{name}", self._static)
        self.app.router.add_get("/ws/{room_id}", self._ws)
        self.app.router.add_get("/bili/{room_id}/{version}/{name}", self._bili)
        self.app.router.add_get("/vod/{room_id}/{version}/index.m3u8", self._vod)
        self.app.router.add_get("/tv/{room_id}/{version}/index.m3u8", self._tv_playlist)
        self.app.router.add_get("/tv/{room_id}/{version}/s/{n}", self._tv_segment)
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
        for name in ("_session", "_vod_session", "_tv_session"):
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

    def _tv_http(self) -> aiohttp.ClientSession:
        """直播探测和中继共用；解析器只放行 ip_allowed 的地址（见 tv_net）。"""
        if self._tv_session is None or self._tv_session.closed:
            self._tv_session = self.tv_session_factory()
        return self._tv_session

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

    # ---------------- 推送 ----------------
    def _state_msg(self, room: Room) -> dict:
        v = room.video
        now = time.time()
        video = {"source": v.source, "id": v.id, "title": v.title, "duration": v.duration,
                 "version": room.version,
                 "mpd": f"/bili/{room.room_id}/{room.version}/manifest.mpd" if v.source == "bilibili" else None,
                 "hls": f"/vod/{room.room_id}/{room.version}/index.m3u8" if v.vod is not None else None,
                 "live": False}
        position = room.playback.position_at(now, v.duration)
        if v.tv is not None:
            # 直播没有进度：位置永远是 0
            video.update(hls=f"/tv/{room.room_id}/{room.version}/index.m3u8", live=True,
                         line_count=len(v.tv.order), line_index=v.tv.index)
            position = 0
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
            "position": position,
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
        """清掉这个房间除 keep 版本以外的 B 站地址、vod 播放列表和直播流缓存；keep 为 None（关房）全清。"""
        for cache in (self._streams, self._stream_locks, self._playlists, self._playlist_locks,
                      self._tv_streams):
            for key in [k for k in cache if k[0] == room_id and k[1] != keep]:
                cache.pop(key, None)
        self._tv_dead_seen = {k for k in self._tv_dead_seen if k[0] != room_id or k[1] == keep}
        if keep is None:
            self._tv_last_switch.pop(room_id, None)

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
            return await self._ws_session(ws, room_id)
        finally:
            self._sockets.discard(ws)

    async def _ws_session(self, ws: web.WebSocketResponse, room_id: str) -> web.WebSocketResponse:
        async def end():
            try:
                await ws.send_str(json.dumps({"type": "ended"}))
                await ws.close()
            except (ConnectionResetError, aiohttp.ClientError):
                pass   # 客户端已经走了

        if self.rooms.get(room_id) is None:
            await end()
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
        elif kind in ("tv_next", "tv_stall"):
            self._handle_tv(conn, kind, data)

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
        return web.Response(text=playlist.text, content_type="application/vnd.apple.mpegurl",
                            headers={"Cache-Control": "no-store"})

    # ---------------- 电视直播 ----------------
    async def prepare_tv(self, channel: Channel, order: list[Candidate] | None = None,
                         start: int = 0, bad: set[int] = frozenset()) -> Video:
        """从 order[start] 起依次（绕圈、跳过 bad、每个最多一次）探测，第一个能播的做成 tv 视频；
        都不能播或超过 tv_prepare_timeout 秒抛 TvUnavailable。建房、手动换源、自动换源都走这里。"""
        order = list(channel.ordered() if order is None else order)
        bad = set(bad)

        async def find() -> int | None:
            probe = functools.partial(tv_stream.probe, self._tv_http(), ip_allowed=self.ip_allowed)
            n = len(order)
            for k in range(n):
                i = (start + k) % n
                if i not in bad and await probe(order[i]):
                    return i
            return None

        try:
            index = await asyncio.wait_for(find(), self.tv_prepare_timeout)
        except asyncio.TimeoutError:
            log.info("watch tv prepare timed out (%r)", channel.name)
            index = None
        if index is None:
            raise TvUnavailable()
        return tv_video(TvInfo(channel, order, index, bad))

    def _tv_conns(self, room_id: str, host_only: bool = False) -> list[_Conn]:
        room = self.rooms.get(room_id)
        conns = list(self.conns.get(room_id, ()))
        if host_only:
            conns = [c for c in conns if room is not None and c.viewer_id == room.host_id]
        return conns

    def _handle_tv(self, conn: _Conn, kind: str, data: dict) -> None:
        room = self.rooms.get(conn.room_id)
        if room is None or room.video.tv is None:
            return
        if kind == "tv_next":
            if self._you(room, conn.viewer_id)[0] and len(room.video.tv.order) > 1:
                if not self._start_tv_switch(room.room_id, room.version, manual=True):
                    conn.push({"type": "error", "message": CHANGING_MSG})
            return
        version = data.get("version")
        if type(version) is not int or version != room.version or conn.viewer_id not in room.members:
            return
        last = self._tv_last_switch.get(room.room_id)
        if last is not None and time.monotonic() - last < STALL_COOLDOWN:
            return
        self._start_tv_switch(room.room_id, version, manual=False)

    def _start_tv_switch(self, room_id: str, version: int, manual: bool) -> bool:
        """后台换源；同一房间已经在换就不再开（返回 False）。占位在同步代码里做，两条消息挨着到也只开一个。"""
        if room_id in self._tv_switching:
            return False
        self._tv_switching.add(room_id)
        task = asyncio.get_running_loop().create_task(self._tv_switch(room_id, version, manual))
        self._tasks.add(task)

        def done(t: asyncio.Task) -> None:
            # 用回调而不是 finally 清占位：任务还没开始就被取消时 finally 不会跑
            self._tasks.discard(t)
            self._tv_switching.discard(room_id)
        task.add_done_callback(done)
        return True

    async def _tv_switch(self, room_id: str, version: int, manual: bool) -> None:
        room = self.rooms.get(room_id)
        if room is None or room.video.tv is None or room.version != version:
            return
        info = room.video.tv
        if manual:
            if len(info.order) <= 1:
                return
            start = info.index + 1
            if start == len(info.order):
                start, bad = 0, set()      # 绕回开头：之前判坏的源也再给一次机会
            else:
                bad = set(info.bad)
        else:
            start, bad = info.index + 1, info.bad | {info.index}
        try:
            if len(bad) >= len(info.order):
                raise TvUnavailable()
            video = await self.prepare_tv(info.channel, info.order, start=start, bad=bad)
        except TvUnavailable as e:
            log.info("watch tv switch: no playable source (room=%s, manual=%s)", room_id, manual)
            room = self.rooms.get(room_id)
            if room is None or room.version != version or room.video.tv is None:
                return      # 找源期间房间关了或视频换了：这条错误已经过时
            if not manual:
                # 失败的自动换源也计入冷却，否则全坏的台每次 tv_stall 都会重新探测、重复广播错误
                self._tv_last_switch[room_id] = time.monotonic()
            msg = {"type": "error", "message": e.message}
            for c in self._tv_conns(room_id, host_only=manual):
                c.push(msg)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watch tv switch failed (room=%s)", room_id)
            return
        if self.rooms.switch_tv(room_id, version, video):
            self._tv_last_switch[room_id] = time.monotonic()
            log.info("watch tv switched (room=%s, manual=%s): line %d → %d",
                     room_id, manual, info.index, video.tv.index)
            msg = {"type": "notice", "message": TV_SWITCHED}
            for c in self._tv_conns(room_id):
                c.push(msg)

    def _tv_stream(self, request: web.Request) -> tuple[Room, int, tv_relay.LiveStream]:
        """校验房间和版本，取（没有就建）这个版本的直播流；不合法一律 404。"""
        room = self.rooms.get(request.match_info["room_id"])
        try:
            version = int(request.match_info["version"])
        except ValueError:
            raise web.HTTPNotFound(headers=TV_HEADERS)
        if room is None or room.video.tv is None or version != room.version:
            raise web.HTTPNotFound(headers=TV_HEADERS)
        key = (room.room_id, version)
        stream = self._tv_streams.get(key)
        if stream is None:
            info = room.video.tv
            stream = self._tv_streams[key] = tv_relay.LiveStream(
                self._tv_http(), info.order[info.index], f"/tv/{room.room_id}/{version}",
                ip_allowed=self.ip_allowed)
        return room, version, stream

    def _check_dead(self, room: Room, version: int, stream: tv_relay.LiveStream) -> None:
        """流判死了：每个 (房间, 版本) 只发起一次自动换源（不受 tv_stall 冷却限制）。"""
        key = (room.room_id, version)
        if stream.dead_reason is None or key in self._tv_dead_seen:
            return
        if room.closed or room.version != version:
            return
        if self._start_tv_switch(room.room_id, version, manual=False):
            self._tv_dead_seen.add(key)

    async def _tv_playlist(self, request: web.Request) -> web.StreamResponse:
        room, version, stream = self._tv_stream(request)
        try:
            text = await stream.playlist()
        except tv_stream.StreamError:
            raise web.HTTPBadGateway(headers=TV_HEADERS)
        finally:
            self._check_dead(room, version, stream)
        return web.Response(text=text, content_type="application/vnd.apple.mpegurl", headers=TV_HEADERS)

    async def _tv_segment(self, request: web.Request) -> web.StreamResponse:
        room, version, stream = self._tv_stream(request)
        try:
            n = int(request.match_info["n"])
        except ValueError:
            raise web.HTTPNotFound(headers=TV_HEADERS)
        try:
            body, ctype = await stream.segment(n)
        except KeyError:
            raise web.HTTPNotFound(headers=TV_HEADERS)
        except tv_stream.StreamError:
            raise web.HTTPBadGateway(headers=TV_HEADERS)
        finally:
            self._check_dead(room, version, stream)
        return web.Response(body=body, content_type=ctype, headers=TV_HEADERS)

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
