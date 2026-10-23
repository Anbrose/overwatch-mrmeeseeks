"""一起看网页服务：房间页、WebSocket 同步协议、B 站 DASH 转流。

路由：
- GET /r/{room_id}                          房间页（index.html）；房间不存在 → 404 页面
- GET /static/{name}                        watch_web 下白名单里的文件
- GET /ws/{room_id}                         WebSocket（协议见 watch_web/player.js）
- GET /bili/{room_id}/{version}/manifest.mpd   本服务器生成的 MPD
- GET /bili/{room_id}/{version}/{video|audio}  转发 B 站 CDN（只请求 Sources 给的地址）
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import aiohttp
from aiohttp import web

from live_watch import USER_AGENT
from watch_rooms import Room, RoomManager, RoomsFull
from watch_sources import BILI_REFERER, DashStreams, Sources, VideoUnavailable

log = logging.getLogger(__name__)

TICK_SECONDS = 5
HELLO_TIMEOUT = 10
REFRESH_MARGIN = 60          # 地址过期前多少秒就重新取
CHUNK = 64 * 1024
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
    busy: asyncio.Task | None = None     # 正在处理的 change_video

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
                 web_dir: Path = Path(__file__).parent / "watch_web"):
        self.rooms = rooms
        self.sources = sources
        self.host = host
        self.port = port
        self.web_dir = Path(web_dir)
        self.conns: dict[str, set[_Conn]] = {}
        self._streams: dict[tuple[str, int], DashStreams] = {}
        self._stream_locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self.bili_bytes = 0          # 当天（UTC）转流字节数
        rooms.listeners.append(self._on_room_event)

        self.app = web.Application()
        self.app.router.add_get("/r/{room_id}", self._page)
        self.app.router.add_get("/static/{name}", self._static)
        self.app.router.add_get("/ws/{room_id}", self._ws)
        self.app.router.add_get("/bili/{room_id}/{version}/{name}", self._bili)
        self._sockets: set[web.WebSocketResponse] = set()
        self.app.on_shutdown.append(self._shutdown)
        self.app.on_cleanup.append(self._cleanup)

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
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
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def run(self) -> None:
        """每 TICK_SECONDS 秒 rooms.tick()；每天 UTC 0 点记一次 B 站转流字节数。"""
        day = datetime.datetime.now(datetime.timezone.utc).date()
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                self.rooms.tick()
            except Exception:
                log.exception("watch rooms tick failed")
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
        return {
            "type": "state",
            "video": {"source": v.source, "id": v.id, "title": v.title, "duration": v.duration,
                      "version": room.version,
                      "mpd": f"/bili/{room.room_id}/{room.version}/manifest.mpd" if v.source == "bilibili" else None},
            "playing": room.playback.playing,
            "position": room.playback.position_at(now, v.duration),
            "server_time": now,
        }

    def _you(self, room: Room, viewer_id: str) -> tuple[bool, bool]:
        member = room.members.get(viewer_id)
        is_host = room.host_id is not None and room.host_id == viewer_id
        return is_host, bool(is_host or (member is not None and member.can_control))

    def _members_msg(self, room: Room) -> dict:
        return {"type": "members", "members": [
            {"viewer_id": m.viewer_id, "name": m.name, "can_control": m.can_control}
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
                for key in [k for k in self._streams if k[0] == room.room_id and k[1] != room.version]:
                    self._streams.pop(key, None)
                    self._stream_locks.pop(key, None)
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
            for key in [k for k in self._streams if k[0] == room.room_id]:
                self._streams.pop(key, None)
                self._stream_locks.pop(key, None)

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
                self.rooms.control(rid, vid, action, float(pos))
        elif kind == "duration":
            if _finite(data.get("value")):
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
            if conn.busy is not None and not conn.busy.done():
                conn.push({"type": "error", "message": CHANGING_MSG})
            else:
                conn.busy = asyncio.get_running_loop().create_task(self._change_video(conn, url))

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
