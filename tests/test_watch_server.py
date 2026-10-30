"""一起看网页服务测试：页面、WebSocket 同步协议、B 站 DASH 转流。

运行：python tests/test_watch_server.py
不联网：B 站上游用本地 aiohttp 假服务器模拟，Sources 用假实现。
"""
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import watch_server as ws_mod  # noqa: E402
from geoip import BlockLog, GeoDB  # noqa: E402
from watch_requests import KIND_NO_RESULTS, KIND_NOT_LISTED, RequestStore  # noqa: E402
from watch_rooms import RoomManager, Video  # noqa: E402
from watch_sources import DashStreams, VideoUnavailable  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def yt(vid="dQw4w9WgXcQ", title="YT", duration=None):
    return Video("youtube", vid, 1, title, duration, f"https://www.youtube.com/watch?v={vid}")


def bili(bvid="BV1xx411c7mD", title="B站视频", duration=100.0):
    return Video("bilibili", bvid, 1, title, duration, f"https://www.bilibili.com/video/{bvid}?p=1")


def streams(video_url, audio_url, backup=(), expires_in=3600.0):
    return DashStreams(
        video_url=video_url, video_backup=list(backup), audio_url=audio_url, audio_backup=[],
        video_codecs="avc1.640032", audio_codecs="mp4a.40.2", width=1920, height=1080, frame_rate="29.970",
        video_bandwidth=2000000, audio_bandwidth=128000, duration=100.0,
        video_init="0-927", video_index="928-1203", audio_init="0-817", audio_index="818-1000",
        expires_at=time.time() + expires_in)


class FakeSources:
    def __init__(self):
        self.calls = 0
        self.queue = []          # bili_streams 依次返回
        self.resolve_calls = 0

    async def resolve(self, text):
        self.resolve_calls += 1
        if "slow" in text:
            await asyncio.sleep(0.5)
        if "bad" in text:
            raise VideoUnavailable("这个视频在海外服务器上无法播放")
        if "bili" in text:
            return bili("BV1yy411c7mD", "另一个B站视频")
        return yt("abcdefghijk", "新视频", 200.0)

    async def bili_streams(self, video):
        self.calls += 1
        s = self.queue[0] if len(self.queue) == 1 else self.queue.pop(0)
        return s


# ---------------- 假上游 ----------------
class Upstream:
    def __init__(self):
        self.requests = []
        self.app = web.Application()
        self.app.router.add_get("/{name}", self.handle)

    async def handle(self, request):
        name = request.match_info["name"]
        self.requests.append((name, dict(request.headers)))
        if name == "expired":
            return web.Response(status=403, text="forbidden")
        body = bytes(range(256)) * 20   # 5120 字节
        rng = request.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, b = rng[6:].split("-")
            a, b = int(a), int(b)
            part = body[a:b + 1]
            return web.Response(status=206, body=part, content_type="video/mp4",
                                headers={"Content-Range": f"bytes {a}-{b}/{len(body)}"})
        return web.Response(body=body, content_type="video/mp4")


# ---------------- 工具 ----------------
async def recv(ws, timeout=2.0):
    msg = await ws.receive(timeout=timeout)
    return json.loads(msg.data)


async def recv_type(ws, kind, timeout=2.0):
    """读到指定类型为止（跳过其它类型）；超时返回 None。"""
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        try:
            msg = await ws.receive(timeout=left)
        except asyncio.TimeoutError:
            return None
        if msg.type.name != "TEXT":
            return None
        data = json.loads(msg.data)
        if data.get("type") == kind:
            return data


async def drain(ws, timeout=0.3):
    """收集 timeout 内到达的所有消息。"""
    out = []
    while True:
        try:
            msg = await ws.receive(timeout=timeout)
        except asyncio.TimeoutError:
            return out
        if msg.type.name != "TEXT":
            out.append({"type": "__" + msg.type.name})
            return out
        out.append(json.loads(msg.data))


async def hello(client, room_id, viewer_id=None, key=None):
    ws = await client.ws_connect(f"/ws/{room_id}")
    await ws.send_json({"type": "hello", "viewer_id": viewer_id, "key": key})
    welcome = await recv_type(ws, "welcome")
    return ws, welcome


async def make(rooms=None, sources=None):
    rooms = rooms or RoomManager()
    sources = sources or FakeSources()
    server = ws_mod.WatchServer(rooms, sources, "127.0.0.1", 0)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    return rooms, sources, server, client


# ---------------- 测试 ----------------
async def test_ws_flow():
    rooms, sources, server, client = await make()
    try:
        room = rooms.create(1, 2, "发起人", yt(duration=300.0))
        rid = room.room_id
        a, wa = await hello(client, rid, key=room.owner_key)
        check("房主 welcome", wa and wa["is_host"] and wa["can_control"] and wa["name"] and wa["viewer_id"])
        sa = await recv_type(a, "state")
        check("进房收到 state", sa and sa["video"]["source"] == "youtube" and sa["video"]["mpd"] is None
              and sa["video"]["version"] == 0 and sa["playing"] is False and sa["position"] == 0
              and isinstance(sa["server_time"], float) and sa["video"]["duration"] == 300.0
              and sa["video"]["id"] == "dQw4w9WgXcQ" and sa["video"]["title"] == "YT")

        b, wb = await hello(client, rid)
        check("观众 welcome 只能看", wb and not wb["is_host"] and not wb["can_control"])
        bid = wb["viewer_id"]
        check("观众进房也收到 state", (await recv_type(b, "state")) is not None)
        ma0 = await recv_type(a, "members")
        ma = await recv_type(a, "members")
        check("房主进房先收到空 members", ma0 and ma0["members"] == [])
        bpub = rooms.get(rid).members[bid].public_id
        check("members 发给房主且不含房主", ma and [m["id"] for m in ma["members"]] == [bpub]
              and ma["members"][0]["can_control"] is False and ma["members"][0]["name"] == wb["name"])
        check("members 不泄露 viewer_id", bid not in json.dumps(ma) and wa["viewer_id"] not in json.dumps(ma)
              and "viewer_id" not in ma["members"][0])

        # 无权限 control → 状态不变、只给发送者回一条 state（撤回乐观更新）、连接仍开着
        await b.send_json({"type": "control", "action": "play", "position": 5})
        got_a, got_b = await drain(a), await drain(b)
        sb = [m for m in got_b if m["type"] == "state"]
        check("无权限 control 无效", not any(m["type"] == "state" for m in got_a) and not room.playback.playing)
        check("被拒绝的 control 给发送者回 state", len(sb) == 1 and sb[0]["playing"] is False and sb[0]["position"] == 0)
        await b.send_json({"type": "ping", "t": 1.5})
        pong = await recv_type(b, "pong")
        check("无权限 control 后连接仍开着", pong and pong["t"] == 1.5 and isinstance(pong["server_time"], float))

        # 非 JSON / 未知类型 / 字段类型错误：忽略
        await b.send_str("garbage")
        await b.send_json({"type": "nope"})
        await b.send_json(["list"])
        await b.send_json({"type": "ping", "t": "x"})
        await b.send_json({"type": "ping", "t": 2})
        pong = await recv_type(b, "pong")
        check("非 JSON 被忽略", pong and pong["t"] == 2)

        # 房主授权
        await a.send_json({"type": "grant", "target": bid, "allowed": True})     # viewer_id 不是公开 id：无效
        got = await drain(b)
        check("grant 用 viewer_id 无效", not any(m["type"] == "you" for m in got)
              and not rooms.get(rid).members[bid].can_control)
        await a.send_json({"type": "grant", "target": bpub, "allowed": True})
        yb = await recv_type(b, "you")
        check("被授权者收到 you", yb and yb["can_control"] is True and yb["is_host"] is False)
        ma = await recv_type(a, "members")
        check("授权后房主收到 members", ma and ma["members"][0]["can_control"] is True)

        await b.send_json({"type": "control", "action": "play", "position": 10})
        s1, s2 = await recv_type(a, "state"), await recv_type(b, "state")
        check("被授权者 play → 两边 state(playing)", s1 and s2 and s1["playing"] and s2["playing"]
              and 10 <= s1["position"] < 11)

        # NaN / 负数 / 超时长
        await b.send_json({"type": "control", "action": "seek", "position": "NaN"})
        await b.send_str('{"type":"control","action":"seek","position":NaN}')
        await b.send_str('{"type":"control","action":"seek","position":Infinity}')
        await b.send_json({"type": "control", "action": "seek", "position": True})
        await b.send_json({"type": "control", "action": "jump", "position": 3})
        got = await drain(a)
        check("NaN/Infinity/类型错误的 position 被忽略", not any(m["type"] == "state" for m in got)
              and room.playback.playing and room.playback.position == 10)
        await b.send_json({"type": "control", "action": "pause", "position": -5})
        s = await recv_type(a, "state")
        check("负数 position 夹到 0", s and s["position"] == 0 and s["playing"] is False)
        await b.send_json({"type": "control", "action": "seek", "position": 9999})
        s = await recv_type(a, "state")
        check("超过时长夹到时长", s and s["position"] == 300.0)

        # 房主交接
        aid = wa["viewer_id"]
        apub = rooms.get(rid).members[aid].public_id
        await b.send_json({"type": "transfer", "target": apub})
        got = await drain(a)
        check("非房主发 transfer 无效", not any(m["type"] in ("you", "notice") for m in got) and room.host_id == aid)
        await a.send_json({"type": "transfer", "target": bpub})
        yb = await recv_type(b, "you")
        ya = await recv_type(a, "you")
        mb = await recv_type(b, "members")     # host 事件先发 you/members，notice 在后面
        nb = await recv_type(b, "notice")
        check("transfer：b 成房主、a 不再是房主但有控制权", yb and yb["is_host"] is True and ya and ya["is_host"] is False
              and ya["can_control"] is True and room.host_id == bid)
        check("所有人收到房主变更提示、新房主收到 members", nb and nb["message"] == "房主现在是「" + wb["name"] + "」"
              and mb and [x["name"] for x in mb["members"]] == [wa["name"]])
        await b.send_json({"type": "transfer", "target": apub})
        ya = await recv_type(a, "you")
        check("交回来", ya and ya["is_host"] is True and room.host_id == aid)
        await drain(a)
        await drain(b)

        # 普通观众发 grant 无效
        c, wc = await hello(client, rid)
        cid = wc["viewer_id"]
        await drain(c)
        await c.send_json({"type": "grant", "target": rooms.get(rid).members[cid].public_id, "allowed": True})
        got = await drain(c)
        check("普通观众发 grant 无效", not rooms.get(rid).members[cid].can_control
              and not any(m["type"] == "you" for m in got))
        check("members 只发给房主", not any(m["type"] == "members" for m in got + await drain(b)))

        # 已有时长时 duration 不接受
        await a.send_json({"type": "duration", "value": 12.0})
        await drain(a)
        check("已有时长时 duration 忽略", room.video.duration == 300.0)

        # 断开 → rooms.disconnect
        await c.close()
        ma = await recv_type(a, "members")
        await asyncio.sleep(0.05)
        check("断开后 rooms.disconnect", rooms.get(rid).members[cid].connections == 0 and ma is not None)

        # 同一 viewer_id 重连保持身份
        b2, wb2 = await hello(client, rid, viewer_id=bid)
        check("重连同一 viewer_id", wb2["viewer_id"] == bid and wb2["can_control"] is True
              and rooms.get(rid).members[bid].connections == 2)
        await b2.close()

        # 房间关闭 → 所有人收到 ended 并断开
        room.closed = True
        for fn in list(rooms.listeners):
            fn(room, "closed")
        ea, eb = await recv_type(a, "ended"), await recv_type(b, "ended")
        check("关闭时所有人收到 ended", ea is not None and eb is not None)
        msg = await a.receive(timeout=2)
        check("关闭后连接被断开", msg.type.name in ("CLOSE", "CLOSED", "CLOSING"))
        await a.close()
        await b.close()

        # 不存在的房间：发 ended 再关
        x = await client.ws_connect("/ws/zzzzzz")
        await x.send_json({"type": "hello", "viewer_id": None, "key": None})
        e = await recv_type(x, "ended")
        msg = await x.receive(timeout=2)
        check("不存在的房间收到 ended 并关闭", e is not None and msg.type.name in ("CLOSE", "CLOSED", "CLOSING"))
        await x.close()

        # 第一条不是 hello → 关闭
        room2 = rooms.create(1, 2, "x", yt())
        y = await client.ws_connect(f"/ws/{room2.room_id}")
        await y.send_json({"type": "ping", "t": 1})
        msg = await y.receive(timeout=2)
        check("第一条不是 hello 就关闭", msg.type.name in ("CLOSE", "CLOSED", "CLOSING")
              and not rooms.get(room2.room_id).members)
        await y.close()
    finally:
        await client.close()


async def test_duration_and_change_video():
    rooms, sources, server, client = await make()
    try:
        room = rooms.create(1, 2, "发起人", yt(duration=None))
        rid = room.room_id
        a, _ = await hello(client, rid, key=room.owner_key)
        b, wb = await hello(client, rid)
        await drain(a)
        await drain(b)
        await b.send_json({"type": "duration", "value": 99.0})        # 没有控制权：不接受
        got = await drain(a)
        check("没有控制权的人报时长无效", room.video.duration is None and not any(m["type"] == "state" for m in got))
        await a.send_json({"type": "duration", "value": "x"})
        await a.send_json({"type": "duration", "value": 212.5})
        s = await recv_type(a, "state")
        check("时长未知时接受房主的 duration", s and s["video"]["duration"] == 212.5 and room.video.duration == 212.5)
        await drain(b)

        await b.send_json({"type": "change_video", "url": "https://youtu.be/abcdefghijk"})
        got = await drain(b)
        check("普通观众换视频无效且不去解析", room.video.id == "dQw4w9WgXcQ" and sources.resolve_calls == 0
              and not any(m["type"] == "state" for m in got))

        await a.send_json({"type": "change_video", "url": "https://youtu.be/abcdefghijk"})
        s1, s2 = await recv_type(a, "state"), await recv_type(b, "state")
        check("房主换视频两边一起切换", s1 and s2 and s1["video"]["id"] == "abcdefghijk"
              and s2["video"]["version"] == 1 and s2["playing"] is False and s2["position"] == 0)

        await drain(b)
        await a.send_json({"type": "change_video", "url": "https://youtu.be/slowslowslo"})
        await a.send_json({"type": "change_video", "url": "https://youtu.be/abcdefghijk"})
        e = await recv_type(a, "error")
        s = await recv_type(a, "state")
        got_b = await drain(b)
        check("换视频进行中再换 → 提示请稍候", e and e["message"] == "正在换视频，请稍候"
              and s and s["video"]["version"] == 2 and room.version == 2 and sources.resolve_calls == 2
              and not any(m["type"] == "error" for m in got_b))
    finally:
        await client.close()


async def test_change_video_errors():
    rooms = RoomManager(max_bili_rooms=1)
    rooms, sources, server, client = await make(rooms)
    try:
        rooms.create(9, 9, "别人", bili())          # 占满 B 站名额
        room = rooms.create(1, 2, "发起人", yt())
        rid = room.room_id
        a, _ = await hello(client, rid, key=room.owner_key)
        b, _ = await hello(client, rid)
        await drain(a)
        await drain(b)

        await a.send_json({"type": "change_video", "url": "https://bad.example/x"})
        e = await recv_type(a, "error")
        got = await drain(b)
        check("放不了的视频只回给发起人且原视频不变", e and e["message"] == "这个视频在海外服务器上无法播放"
              and not got and room.video.id == "dQw4w9WgXcQ" and room.version == 0)

        await a.send_json({"type": "change_video", "url": "https://www.bilibili.com/video/BV1yy411c7mD"})
        e = await recv_type(a, "error")
        got = await drain(b)
        check("B 站满了提示", e and e["message"] == "B 站房间已满（最多 1 个），请稍后再试或换 YouTube 视频"
              and not got and room.video.source == "youtube")
        await a.send_json({"type": "change_video", "url": 5})
        await a.send_json({"type": "ping", "t": 3})
        check("url 类型错误被忽略", (await recv_type(a, "pong")) is not None and sources.resolve_calls == 2)
    finally:
        await client.close()


class _Resp:
    def __init__(self, payload):
        self.status, self._payload, self.headers = 200, payload, {}

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    """按 URL 片段返回假响应的 aiohttp.ClientSession 替身（给真的 Sources 用）。"""

    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **kw):
        for key, payload in self.routes:
            if key in url:
                return _Resp(payload)
        return _Resp({"code": -404})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def test_change_video_playurl_rejected():
    """真的 Sources：view 正常、playurl 报地区限制 → 换视频被拒，原视频不变，提示发给房主。"""
    from watch_sources import Sources
    with open(os.path.join(ROOT, "tests", "fixtures", "watch", "bili_view.json"), encoding="utf-8") as f:
        view = json.load(f)
    nav = {"code": -101, "data": {"wbi_img": {
        "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
        "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"}}}
    routes = [("finger/spi", {"code": 0, "data": {"b_3": "x", "b_4": "y"}}), ("web-interface/nav", nav),
              ("wbi/view", view), ("wbi/playurl", {"code": -10403, "message": "地区限制"})]
    sources = Sources(None, session_factory=lambda: _Session(routes))
    rooms, sources, server, client = await make(sources=sources)
    try:
        room = rooms.create(1, 2, "发起人", yt())
        a, _ = await hello(client, room.room_id, key=room.owner_key)
        await drain(a)
        await a.send_json({"type": "change_video", "url": "https://www.bilibili.com/video/BV1xrnz6gEVD"})
        e = await recv_type(a, "error")
        check("playurl 不可用 → 换视频被拒", e and e["message"] == "这个视频在海外服务器上无法播放"
              and room.video.source == "youtube" and room.version == 0)
    finally:
        await client.close()


async def test_bili_proxy():
    up = Upstream()
    up_client = TestClient(TestServer(up.app))
    await up_client.start_server()
    base = str(up_client.make_url("/")).rstrip("/")
    rooms, sources, server, client = await make()
    try:
        room = rooms.create(1, 2, "发起人", bili())
        rid = room.room_id
        sources.queue = [streams(base + "/v.m4s", base + "/a.m4s")]

        # state 里带 mpd 地址
        a, _ = await hello(client, rid, key=room.owner_key)
        s = await recv_type(a, "state")
        check("B 站 state 带 mpd", s and s["video"]["mpd"] == f"/bili/{rid}/0/manifest.mpd")
        await a.close()

        r = await client.get(f"/bili/{rid}/0/manifest.mpd")
        mpd = await r.text()
        check("MPD 指向本服务器", r.status == 200 and "/bili/" + rid + "/0/video" in mpd and "SegmentBase" in mpd
              and "/bili/" + rid + "/0/audio" in mpd and base not in mpd)
        check("MPD 带 indexRange/Initialization/时长", 'indexRange="928-1203"' in mpd
              and 'range="0-927"' in mpd and "mediaPresentationDuration" in mpd and 'codecs="avc1.640032"' in mpd)

        r = await client.get(f"/bili/{rid}/0/video", headers={"Range": "bytes=0-1023", "Cookie": "SESSDATA=leak"})
        body = await r.read()
        check("Range 透传 206", r.status == 206 and r.headers["Content-Range"].startswith("bytes 0-1023/")
              and len(body) == 1024 and r.headers["Content-Length"] == "1024"
              and r.headers["Content-Type"].startswith("video/mp4"))
        name, hdrs = up.requests[-1]
        check("上游带 Referer/UA、只转发 Range", name == "v.m4s" and hdrs.get("Referer") == "https://www.bilibili.com/"
              and "Mozilla" in hdrs.get("User-Agent", "") and "Cookie" not in hdrs and hdrs.get("Range") == "bytes=0-1023")
        r = await client.get(f"/bili/{rid}/0/audio", headers={"Range": "bytes=0-99"})
        await r.read()
        check("audio 走 audio_url 且复用缓存", r.status == 206 and up.requests[-1][0] == "a.m4s" and sources.calls == 1)
        check("转流字节数被计数", server.bili_bytes >= 1124)

        # 上游 403 → 重新取地址并重试一次
        sources.calls = 0
        server._streams.clear()
        sources.queue = [streams(base + "/expired", base + "/a.m4s"), streams(base + "/v.m4s", base + "/a.m4s")]
        r = await client.get(f"/bili/{rid}/0/video", headers={"Range": "bytes=0-1023"})
        await r.read()
        check("上游 403 后重新取地址重试一次", r.status == 206 and sources.calls == 2)

        # 一直 403 → 只重试一次
        sources.calls = 0
        server._streams.clear()
        sources.queue = [streams(base + "/expired", base + "/a.m4s")]
        r = await client.get(f"/bili/{rid}/0/video", headers={"Range": "bytes=0-1023"})
        await r.read()
        check("持续 403 只重试一次", r.status == 403 and sources.calls == 2)

        # 快过期 → 提前重新取地址
        sources.calls = 0
        server._streams.clear()
        sources.queue = [streams(base + "/v.m4s", base + "/a.m4s", expires_in=30)]
        for _ in range(2):
            r = await client.get(f"/bili/{rid}/0/video", headers={"Range": "bytes=0-9"})
            await r.read()
        check("expires_at - 60 之后重新取地址", r.status == 206 and sources.calls == 2)

        # 主地址连不上 → backup
        sources.calls = 0
        server._streams.clear()
        sources.queue = [streams("http://127.0.0.1:1/v.m4s", base + "/a.m4s", backup=[base + "/v.m4s"])]
        r = await client.get(f"/bili/{rid}/0/video", headers={"Range": "bytes=0-1023"})
        await r.read()
        check("主地址连不上时用 backup", r.status == 206 and up.requests[-1][0] == "v.m4s")

        # 取地址失败 → 502
        server._streams.clear()

        async def boom(video):
            raise VideoUnavailable("B 站暂时无法访问，请稍后再试")
        orig = sources.bili_streams
        sources.bili_streams = boom
        r = await client.get(f"/bili/{rid}/0/manifest.mpd")
        check("取地址失败 502，正文是提示文字", r.status == 502 and (await r.text()) == "B 站暂时无法访问，请稍后再试")
        sources.bili_streams = orig

        check("旧版本号 404", (await client.get(f"/bili/{rid}/99/video")).status == 404)
        check("版本号非数字 404", (await client.get(f"/bili/{rid}/x/video")).status == 404)
        check("未知 kind 404", (await client.get(f"/bili/{rid}/0/..%2Fetc")).status == 404)
        check("未知 kind 404（普通名字）", (await client.get(f"/bili/{rid}/0/subtitle")).status == 404)
        check("不存在的房间 404", (await client.get("/bili/zzzzzz/0/video")).status == 404)
        yt_room = rooms.create(1, 2, "x", yt())
        check("YouTube 房间没有转流", (await client.get(f"/bili/{yt_room.room_id}/0/manifest.mpd")).status == 404)
        room.closed = True
        check("关闭的房间 404", (await client.get(f"/bili/{rid}/0/video")).status == 404)
    finally:
        await client.close()
        await up_client.close()


async def test_pages():
    rooms, sources, server, client = await make()
    try:
        room = rooms.create(1, 2, "发起人", yt())
        r = await client.get(f"/r/{room.room_id}")
        text = await r.text()
        check("房间页 200", r.status == 200 and "/static/player.js" in text and r.content_type == "text/html")
        check("房间页带投屏脚本和按钮", "/static/cast.js" in text and 'id="cast"' in text)
        check("房间页有「在电视上看」入口", 'id="bs-btn"' in text and 'id="bs-panel"' in text)
        r = await client.get("/")
        check("首页是输房间号的页面", r.status == 200 and 'action="/j"' in await r.text()
              and r.headers.get("Cache-Control") == "no-store")
        r = await client.get("/j", params={"c": f" {room.room_id.upper()[:3]}-{room.room_id[3:]} "},
                             allow_redirects=False)
        check("/j 规范化房间号后跳到房间页", r.status == 303 and r.headers.get("Location") == f"/r/{room.room_id}")
        r = await client.get("/j", params={"c": "zzzzzz"})
        check("/j 不存在的房间 404 并提示", r.status == 404 and "没有这个房间" in await r.text())
        check("/j 不带房间号就是首页", (await client.get("/j")).status == 200)
        check("/j 全是杂字符当没输", (await client.get("/j", params={"c": "- -"})).status == 200)
        r = await client.get("/r/zzzzzz")
        text = await r.text()
        check("不存在的房间 404 文案", r.status == 404 and "房间不存在或已经结束" in text)
        r = await client.get("/static/player.js")
        check("static 白名单内 200", r.status == 200 and "javascript" in r.content_type)
        r = await client.get("/static/style.css")
        check("static css 类型", r.status == 200 and r.content_type == "text/css")
        r = await client.get("/static/cast.js")
        check("cast.js 在 static 白名单", r.status == 200 and "javascript" in r.content_type
              and "WatchCast" in await r.text())
        check("static 白名单外 404", (await client.get("/static/../watch_server.py")).status == 404)
        check("static 编码穿越 404", (await client.get("/static/..%2Fwatch_server.py")).status == 404)
        check("static 未列名文件 404", (await client.get("/static/player-v2.html")).status == 404)
    finally:
        await client.close()


def test_build_mpd():
    s = streams("https://cdn/v?a=1&b=2", "https://cdn/a")
    s.video_codecs = 'avc1"<x>'
    mpd = ws_mod.build_mpd(s, "abc234", 3)
    import xml.etree.ElementTree as ET
    tree = ET.fromstring(mpd)
    codecs = [r.get("codecs") for r in tree.iter("{urn:mpeg:dash:schema:mpd:2011}Representation")]
    check("build_mpd 转义且不含上游地址", "cdn" not in mpd and "&lt;x&gt;" in mpd and codecs[0] == 'avc1"<x>')
    check("build_mpd 两个 AdaptationSet", mpd.count("<AdaptationSet") == 2 and mpd.count("<Representation") == 2
          and "/bili/abc234/3/video" in mpd and "/bili/abc234/3/audio" in mpd and 'type="static"' in mpd
          and "PT100.000S" in mpd)


async def test_lifecycle():
    rooms = RoomManager()

    class FakeRecorder:
        ticks = 0

        def tick(self):
            FakeRecorder.ticks += 1
    server = ws_mod.WatchServer(rooms, FakeSources(), "127.0.0.1", 0, recorder=FakeRecorder())
    await server.start()
    ticks = []
    rooms.tick = lambda: ticks.append(1)
    ws_mod.TICK_SECONDS = 0.05
    task = asyncio.create_task(server.run())
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await server.close()
    check("run 定时 tick", len(ticks) >= 2)
    check("run 定时调用 recorder.tick", FakeRecorder.ticks >= 2)

    # 有打开的 WebSocket 时 close() 也要很快结束（docker stop 只给 10 秒）
    rooms = RoomManager()
    server = ws_mod.WatchServer(rooms, FakeSources(), "127.0.0.1", 18766)
    await server.start()
    room = rooms.create(1, 2, "x", yt())
    import aiohttp
    import logging

    class Grab(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)
    grab = Grab()
    access = logging.getLogger("aiohttp.access")
    old_level = access.level
    access.setLevel(logging.INFO)
    access.addHandler(grab)
    try:
        async with aiohttp.ClientSession() as s:
            r = await s.get(f"http://127.0.0.1:18766/r/{room.room_id}?k={room.owner_key}")
            await r.read()
    finally:
        access.removeHandler(grab)
        access.setLevel(old_level)
    check("没有 access log（不记房主密钥）", r.status == 200 and not grab.records)
    async with aiohttp.ClientSession() as s:
        a = await s.ws_connect(f"http://127.0.0.1:18766/ws/{room.room_id}")
        await a.send_json({"type": "hello", "viewer_id": None, "key": None})
        await recv_type(a, "welcome")
        pending = await s.ws_connect(f"http://127.0.0.1:18766/ws/{room.room_id}")   # 还没发 hello
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        await server.close()
        took = time.monotonic() - t0
        msg = await a.receive(timeout=2)
        while msg.type.name == "TEXT":
            msg = await a.receive(timeout=2)
        check(f"有打开的 WebSocket 时 close() < 3 秒（{took:.2f}s）", took < 3)
        check("关闭时客户端连接被断开", msg.type.name in ("CLOSE", "CLOSED", "CLOSING"))
        check("关闭后成员已断开", room.members and all(m.connections == 0 for m in room.members.values()))
        await a.close()
        await pending.close()


# ---------------- vod（按片名搜索的剧） ----------------
FIX = os.path.join(ROOT, "tests", "fixtures", "watch")
AD_SEGMENT = "9abd76c66643cf6bbfbda0a81923d523.ts"


def _fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


class VodUpstream:
    """假资源站 CDN：/{line}/{ep}/index.m3u8 → master，/…/1000k/hls/mixed.m3u8 → 带广告的变体（带一把 AES 密钥
    enc.key），片段带 ACAO *。"""

    def __init__(self):
        self.master = _fixture("vod_master.m3u8")
        self.variant = _fixture("vod_variant_ads.m3u8").replace(
            "#EXTM3U\n", '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="enc.key",IV=0x0\n', 1)
        self.key = b"0123456789abcdef"
        self.dead_keys = set()       # 这些线路的密钥返回 403
        self.key_requests = []
        self.dead_lines = set()      # 这些线路的 m3u8 返回 404
        self.delays = {}             # 集数 → 取 master 前等几秒
        self.m3u8_requests = []
        self.app = web.Application()
        self.app.router.add_get("/{line}/{ep}/{tail:.*}", self.handle)

    async def handle(self, request):
        line, ep, tail = request.match_info["line"], request.match_info["ep"], request.match_info["tail"]
        if tail.endswith(".ts"):
            return web.Response(status=206, body=b"x" * 1024, headers={"Access-Control-Allow-Origin": "*"})
        if tail.endswith(".key"):
            self.key_requests.append(request.path)
            if line in self.dead_keys:
                return web.Response(status=403, text="no")
            return web.Response(body=self.key)
        self.m3u8_requests.append(request.path)
        if line in self.dead_lines:
            return web.Response(status=404, text="gone")
        if tail == "index.m3u8":
            delay = self.delays.get(int(ep), 0)
            if delay:
                await asyncio.sleep(delay)
            return web.Response(text=self.master)
        if tail == "1000k/hls/mixed.m3u8":
            return web.Response(text=self.variant)
        return web.Response(status=404)


def vod_show(base, lines=("线路A", "线路B", "线路C"), episodes=3):
    from vod_search import Episode, Line, Show
    return Show(key="测试剧|2024", title="测试剧", year=2024, type_name="剧集", lines=[
        Line(name, [Episode(f"第{e + 1}集", f"{base}/l{i}/{e}/index.m3u8") for e in range(episodes)])
        for i, name in enumerate(lines)])


async def make_vod(rooms=None, **kw):
    up = VodUpstream()
    up_client = TestClient(TestServer(up.app))
    await up_client.start_server()
    base = str(up_client.make_url("/")).rstrip("/")
    rooms = rooms or RoomManager()
    server = ws_mod.WatchServer(rooms, FakeSources(), "127.0.0.1", 0, origin="https://watch.example.com",
                                allow_http=True, **kw)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    return up, up_client, base, rooms, server, client


async def vod_room(rooms, server, show, episode=0, line=0, resume=None):
    video, pl = await server.prepare_vod(show, episode, line, resume)
    room = rooms.create(1, 2, "发起人", video)
    server.attach_playlist(room, pl)
    return room


async def test_vod_playlist_route():
    up, up_client, base, rooms, server, client = await make_vod()
    try:
        show = vod_show(base)
        room = await vod_room(rooms, server, show)
        rid = room.room_id
        check("prepare_vod 生成 vod 视频", room.video.source == "vod" and room.video.vod.episode == 0
              and room.video.vod.line == 0 and abs(room.video.duration - 3486.48) < 0.5
              and room.video.url == f"{base}/l0/0/index.m3u8")
        n_req = len(up.m3u8_requests)
        r = await client.get(f"/vod/{rid}/0/index.m3u8")
        body = await r.text()
        check("vod 播放列表 200 且类型对", r.status == 200 and r.content_type == "application/vnd.apple.mpegurl"
              and r.headers.get("Cache-Control") == "no-store" and body.startswith("#EXTM3U"))
        check("vod 播放列表放开 CORS（Chromecast 接收端跨域取）", r.headers.get("Access-Control-Allow-Origin") == "*")
        check("播放列表去掉了广告、地址绝对", AD_SEGMENT not in body and f"{base}/l0/0/1000k/hls/" in body)
        check("命中缓存不再取上游", len(up.m3u8_requests) == n_req)
        check("密钥地址换成本服务器的 key0", 'URI="key0"' in body and "enc.key" not in body)
        r = await client.get(f"/vod/{rid}/0/key0")
        check("密钥由本服务器中转", r.status == 200 and await r.read() == up.key
              and r.content_type == "application/octet-stream" and r.headers.get("Cache-Control") == "no-store"
              and up.key_requests == ["/l0/0/1000k/hls/enc.key"])
        r = await client.get(f"/vod/{rid}/0/key0")
        await r.read()
        check("密钥缓存，不再取上游", r.status == 200 and len(up.key_requests) == 1)
        check("没有的密钥序号 404", (await client.get(f"/vod/{rid}/0/key1")).status == 404)
        check("密钥旧版本号 404", (await client.get(f"/vod/{rid}/1/key0")).status == 404)
        check("密钥序号非数字 404", (await client.get(f"/vod/{rid}/0/keyx")).status == 404)
        check("不存在的房间没有密钥", (await client.get("/vod/zzzzzz/0/key0")).status == 404)
        server._keys.clear()
        up.dead_keys.add("l0")
        r = await client.get(f"/vod/{rid}/0/key0")
        check("密钥取不到 502 带原因", r.status == 502
              and (await r.text()) == f"这一集暂时没有能播的片源（密钥 HTTP 403（{base.split('//')[1]}））")
        up.dead_keys.clear()
        check("vod 旧版本号 404", (await client.get(f"/vod/{rid}/1/index.m3u8")).status == 404)
        check("vod 版本号非数字 404", (await client.get(f"/vod/{rid}/x/index.m3u8")).status == 404)
        check("vod 不存在的房间 404", (await client.get("/vod/zzzzzz/0/index.m3u8")).status == 404)
        yt_room = rooms.create(1, 2, "x", yt())
        check("YouTube 房间没有 vod 播放列表", (await client.get(f"/vod/{yt_room.room_id}/0/index.m3u8")).status == 404)

        # 缓存被清掉 → 按房间当前地址取一次
        server._playlists.clear()
        r = await client.get(f"/vod/{rid}/0/index.m3u8")
        body = await r.text()
        check("缓存缺失时按当前集地址重取", r.status == 200 and AD_SEGMENT not in body
              and up.m3u8_requests[n_req:] == ["/l0/0/index.m3u8", "/l0/0/1000k/hls/mixed.m3u8"])
        r = await client.get(f"/vod/{rid}/0/index.m3u8")
        await r.read()
        check("重取后缓存", len(up.m3u8_requests) == n_req + 2)
        server._playlists.clear()
        up.dead_lines.add("l0")
        r = await client.get(f"/vod/{rid}/0/index.m3u8")
        body = await r.text()
        check("重取失败 502 带提示和原因", r.status == 502 and body.startswith("这一集暂时没有能播的片源（")
              and "HTTP 404" in body and body.endswith("）"))
        up.dead_lines.clear()

        bad = vod_show(base)
        up.dead_lines.update({"l0", "l1", "l2"})
        try:
            await server.prepare_vod(bad, 0)
            raised = None
        except Exception as e:
            raised = e
        check("所有线路都不能播 → prepare_vod 抛 PlaylistError", raised is not None
              and type(raised).__name__ == "PlaylistError" and raised.message == "这一集暂时没有能播的片源")
        check("PlaylistError 按线路带原因", raised is not None and raised.detail.count("HTTP 404") == 3
              and raised.detail.count("；") == 2)
        up.dead_lines.clear()
        up.dead_lines.add("l0")
        video, _ = await server.prepare_vod(show, 1, 0)
        check("prepare_vod 跳过不能播的线路", video.vod.line == 1 and video.vod.episode == 1)
        up.dead_lines.clear()

        rooms.close(rid)
        check("关房后清掉播放列表缓存", not any(k[0] == rid for k in server._playlists))
    finally:
        await client.close()
        await up_client.close()


async def test_vod_state_message():
    from watch_progress import Progress, ProgressRecorder, ProgressStore
    rooms = RoomManager()
    store = ProgressStore(None)
    recorder = ProgressRecorder(rooms, store)
    n_listeners = len(rooms.listeners)
    up, up_client, base, rooms, server, client = await make_vod(rooms, recorder=recorder)
    try:
        check("server 不替 recorder 重复注册监听", len(rooms.listeners) == n_listeners + 1
              and rooms.listeners.count(recorder.on_event) == 1)
        show = vod_show(base)
        room = await vod_room(rooms, server, show)
        a, _ = await hello(client, room.room_id, key=room.owner_key)
        s = await recv_type(a, "state")
        v = s and s["video"]
        check("vod state.video 字段", v and v["source"] == "vod" and v["hls"] == f"/vod/{room.room_id}/0/index.m3u8"
              and v["episode"] == 0 and v["episode_count"] == 3 and v["line"] == 0 and "line_name" not in v
              and v["line_count"] == 3 and v["show_title"] == "测试剧" and v["mpd"] is None
              and v["title"] == "测试剧 · 第 1 集")
        check("没有进度快照 → resume 是 null", s and "resume" in s and s["resume"] is None)
        await a.close()

        snap = Progress("测试剧", 2, 2052.0, 1761300000.0)
        room2 = await vod_room(rooms, server, show, resume=snap)
        b, _ = await hello(client, room2.room_id)
        s = await recv_type(b, "state")
        r = s and s["resume"]
        check("有进度快照 → resume 带集数、位置、文字", r and r["episode"] == 2 and r["position"] == 2052.0
              and r["text"].startswith("上次一起看到 第 3 集 34:12（"))
        await b.close()

        yt_room = rooms.create(1, 2, "x", yt())
        c, _ = await hello(client, yt_room.room_id)
        s = await recv_type(c, "state")
        check("YouTube state 没有 hls、resume 为 null", s and s["video"]["hls"] is None and s["resume"] is None)
        await c.close()
    finally:
        await client.close()
        await up_client.close()


async def test_vod_episode_switch():
    up, up_client, base, rooms, server, client = await make_vod()
    try:
        room = await vod_room(rooms, server, vod_show(base))
        rid = room.room_id
        a, _ = await hello(client, rid, key=room.owner_key)
        b, wb = await hello(client, rid)
        await drain(a)
        await drain(b)

        await b.send_json({"type": "episode", "index": 1})
        got = await drain(b, 0.5)
        check("非房主选集无效", room.version == 0 and not any(m["type"] == "state" for m in got))
        for bad in (3, -1, "1", 1.0, True, None):
            await a.send_json({"type": "episode", "index": bad})
        got = await drain(a, 0.5)
        check("集数越界/类型错误被忽略", room.version == 0 and not any(m["type"] in ("state", "error") for m in got))

        await a.send_json({"type": "episode", "index": 1})
        s1, s2 = await recv_type(a, "state"), await recv_type(b, "state")
        check("房主选集 → 所有人切到第 2 集", s1 and s2 and s1["video"]["episode"] == 1 and s2["video"]["episode"] == 1
              and s2["video"]["version"] == 1 and s2["video"]["hls"] == f"/vod/{rid}/1/index.m3u8"
              and s2["playing"] is False and s2["position"] == 0 and s2["video"]["line"] == 0)
        check("新版本播放列表已缓存、旧的被清掉", (rid, 1) in server._playlists and (rid, 0) not in server._playlists)
        n_req = len(up.m3u8_requests)
        r = await client.get(f"/vod/{rid}/1/index.m3u8")
        check("新版本播放列表直接可取", r.status == 200 and AD_SEGMENT not in await r.text()
              and len(up.m3u8_requests) == n_req)

        yt_room = rooms.create(1, 2, "x", yt())
        c, _ = await hello(client, yt_room.room_id, key=yt_room.owner_key)
        await drain(c)
        for msg in ({"type": "episode", "index": 0}, {"type": "next_line"}, {"type": "resume"}):
            await c.send_json(msg)
        got = await drain(c, 0.5)
        check("不是 vod 房间时 vod 消息被忽略", yt_room.version == 0 and not got)
    finally:
        await client.close()
        await up_client.close()


async def test_vod_next_line():
    up, up_client, base, rooms, server, client = await make_vod()
    try:
        room = await vod_room(rooms, server, vod_show(base))
        rid = room.room_id
        a, wa = await hello(client, rid, key=room.owner_key)
        b, wb = await hello(client, rid)
        await drain(a)
        await drain(b)
        rooms.control(rid, wa["viewer_id"], "seek", 300.0)
        await drain(a)
        await drain(b)

        await b.send_json({"type": "next_line"})
        got = await drain(b, 0.5)
        check("非房主换片源无效", room.version == 0 and not any(m["type"] == "state" for m in got))

        await a.send_json({"type": "next_line"})
        s = await recv_type(b, "state")
        check("换片源 → 下一条线路、同一集、位置不变、暂停", s and s["video"]["line"] == 1
              and s["video"]["episode"] == 0 and s["position"] == 300.0 and s["playing"] is False
              and s["video"]["version"] == 1 and room.video.url == f"{base}/l1/0/index.m3u8")
        check("换片源后缓存新版本", (rid, 1) in server._playlists)
        await drain(a)

        up.dead_lines.update({"l0", "l1", "l2"})
        await a.send_json({"type": "next_line"})
        e = await recv_type(a, "error")
        got = await drain(b)
        check("其它线路都不能播 → 只告诉房主", e and e["message"] == "这一集暂时没有能播的片源"
              and not got and room.version == 1 and room.video.vod.line == 1)
        up.dead_lines.clear()

        # 最后一条线路再换 → 绕回第一条
        await a.send_json({"type": "next_line"})
        s = await recv_type(a, "state")
        await a.send_json({"type": "next_line"})
        s = await recv_type(a, "state")
        check("最后一条线路换片源绕回第一条", s and s["video"]["line"] == 0 and room.video.vod.line == 0)

        # 播放中换片源 → 从当前位置继续播
        rooms.control(rid, wa["viewer_id"], "play", 600.0)
        await drain(a)
        await a.send_json({"type": "next_line"})
        s = await recv_type(a, "state")
        check("播放中换片源 → 保持播放、从当前位置继续", s and s["video"]["line"] == 1
              and s["playing"] is True and room.playback.playing is True and 600.0 <= s["position"] < 605.0)
    finally:
        await client.close()
        await up_client.close()


async def test_vod_resume():
    from watch_progress import Progress
    up, up_client, base, rooms, server, client = await make_vod()
    try:
        show = vod_show(base)
        snap = Progress("测试剧", 2, 2052.0, 1761300000.0)
        room = await vod_room(rooms, server, show, resume=snap)
        rid = room.room_id
        a, wa = await hello(client, rid, key=room.owner_key)
        b, wb = await hello(client, rid)
        c, wc = await hello(client, rid)
        await drain(a)
        await drain(c)
        await c.send_json({"type": "resume"})
        got = await drain(c, 0.5)
        check("没有控制权的人续看无效", room.version == 0 and not any(m["type"] == "state" for m in got))

        rooms.set_permission(rid, wa["viewer_id"], room.members[wb["viewer_id"]].public_id, True)
        await drain(b)
        await b.send_json({"type": "resume"})
        s = await recv_type(c, "state")
        check("有控制权的人续看 → 第 3 集 2052 秒暂停", s and s["video"]["episode"] == 2 and s["position"] == 2052.0
              and s["playing"] is False and s["video"]["version"] == 1 and s["resume"]["episode"] == 2)
        check("续看后缓存新版本", (rid, 1) in server._playlists and room.video.vod.line == 0)

        # 已在快照那一集：不重新取，只跳到位置
        rooms.control(rid, wa["viewer_id"], "seek", 10.0)
        await drain(c)
        n_req = len(up.m3u8_requests)
        await a.send_json({"type": "resume"})
        s = await recv_type(c, "state")
        check("同一集续看只跳位置、不取播放列表", s and s["position"] == 2052.0 and s["video"]["version"] == 2
              and len(up.m3u8_requests) == n_req and (rid, 2) in server._playlists
              and (rid, 1) not in server._playlists)

        room2 = await vod_room(rooms, server, show)
        d, _ = await hello(client, room2.room_id, key=room2.owner_key)
        await drain(d)
        await d.send_json({"type": "resume"})
        got = await drain(d, 0.5)
        check("没有快照时续看被忽略", room2.version == 0 and not got)
    finally:
        await client.close()
        await up_client.close()


async def test_vod_switch_race():
    up, up_client, base, rooms, server, client = await make_vod()
    try:
        room = await vod_room(rooms, server, vod_show(base))
        rid = room.room_id
        a, wa = await hello(client, rid, key=room.owner_key)
        await drain(a)
        up.delays = {1: 0.3}
        await a.send_json({"type": "episode", "index": 1})
        await a.send_json({"type": "episode", "index": 2})
        e = await recv_type(a, "error")
        s = await recv_type(a, "state")
        await asyncio.sleep(0.4)
        check("连点两集 → 第二次提示请稍候、只切一次", e and e["message"] == "正在换视频，请稍候"
              and s and s["video"]["episode"] == 1 and room.version == 1 and room.video.vod.episode == 1)
        await drain(a)

        # 自动下一集（目标第 3 集，慢）与房主手动选第 1 集（快）赛跑：房主先换，自动下一集被版本号丢弃
        advances = []
        orig_advance = rooms.advance

        def spy(room_id, expected_version, video):
            ok = orig_advance(room_id, expected_version, video)
            advances.append(ok)
            return ok
        rooms.advance = spy
        up.delays = {2: 0.5, 0: 0.1}
        server._on_room_event(room, "finished")
        await a.send_json({"type": "episode", "index": 0})
        await asyncio.sleep(0.9)
        check("自动下一集与手动切换赛跑 → 手动的赢", advances == [False] and room.video.vod.episode == 0
              and room.version == 2 and room.playback.playing is False)
        check("输掉的自动下一集不留播放列表", set(k for k in server._playlists if k[0] == rid) == {(rid, 2)})
    finally:
        await client.close()
        await up_client.close()


async def test_vod_prepare_timeout():
    up, up_client, base, rooms, server, client = await make_vod(vod_prepare_timeout=0.3)
    try:
        check("默认准备时限 45 秒", ws_mod.VOD_PREPARE_TIMEOUT == 45
              and ws_mod.WatchServer(RoomManager(), FakeSources(), "127.0.0.1", 0).vod_prepare_timeout == 45)
        show = vod_show(base)
        up.delays = {1: 2.0}
        t0 = time.monotonic()
        try:
            await server.prepare_vod(show, 1)
            raised = None
        except Exception as e:
            raised = e
        took = time.monotonic() - t0
        check(f"片源太慢 → 超时按没有能播的片源（{took:.2f}s）", raised is not None
              and type(raised).__name__ == "PlaylistError" and raised.message == "这一集暂时没有能播的片源"
              and took < 1.5)
        check("超时的原因说明时限", raised is not None and raised.detail == "0.3 秒内没找到能播的线路，资源站太慢")

        # 网页上的选集也走同一个时限
        room = await vod_room(rooms, server, show)
        a, _ = await hello(client, room.room_id, key=room.owner_key)
        await drain(a)
        await a.send_json({"type": "episode", "index": 1})
        e = await recv_type(a, "error")
        check("选集超时 → 提示没有能播的片源、不换集", e and e["message"] == "这一集暂时没有能播的片源"
              and room.version == 0 and room.video.vod.episode == 0)
        check("error 消息带 detail", e and e["detail"] == "0.3 秒内没找到能播的线路，资源站太慢")
    finally:
        await client.close()
        await up_client.close()


async def test_client_report():
    """网页端播放失败的回报只进日志：带房间、成员公开 id、视频地址；每个连接最多 MAX_REPORTS 条。"""
    rooms, sources, server, client = await make()
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = Grab()
    ws_mod.log.addHandler(handler)
    try:
        room = rooms.create(1, 2, "发起人", yt(duration=300.0))
        rid = room.room_id
        a, wa = await hello(client, rid)
        await drain(a)
        pub = rooms.get(rid).members[wa["viewer_id"]].public_id
        await a.send_json({"type": "report", "detail": "视频加载失败：片段 HTTP 403 · cdn.example.com [fragLoadError]\n第二行",
                           "url": "/vod/x/0/index.m3u8", "env": "hls.js 1.5.20; Mozilla/5.0 (Android)"})
        await a.send_json({"type": "ping", "t": 1})
        check("回报后连接仍开着", (await recv_type(a, "pong")) is not None)
        msgs = [r.getMessage() for r in records if r.levelno == logging.WARNING and "playback failed" in r.getMessage()]
        check("回报记成一条 WARNING，带房间、成员、视频、原因（压成一行）", len(msgs) == 1 and f"room={rid}" in msgs[0]
              and f"member={pub}" in msgs[0] and "video=https://www.youtube.com/watch?v=dQw4w9WgXcQ" in msgs[0]
              and "src=/vod/x/0/index.m3u8" in msgs[0] and "片段 HTTP 403 · cdn.example.com [fragLoadError] 第二行" in msgs[0]
              and wa["viewer_id"] not in msgs[0])
        check("日志带网页报的播放器模式和连接的 User-Agent", "env=hls.js 1.5.20; Mozilla/5.0 (Android)" in msgs[0]
              and "ua=Python/" in msgs[0] and "aiohttp" in msgs[0])
        check("房间状态没变", rooms.get(rid).version == 0 and not rooms.get(rid).playback.playing)
        await a.send_json({"type": "report", "detail": 123})
        await a.send_json({"type": "report"})
        for i in range(10):
            await a.send_json({"type": "report", "detail": f"again {i}"})
        await a.send_json({"type": "ping", "t": 2})
        await recv_type(a, "pong")
        n = len([r for r in records if "playback failed" in r.getMessage()])
        check("非字符串被忽略；每个连接最多记 MAX_REPORTS 条", n == ws_mod.MAX_REPORTS == 5)
        await a.close()
    finally:
        ws_mod.log.removeHandler(handler)
        await client.close()


async def test_vod_auto_next():
    offset = [0.0]
    rooms = RoomManager(clock=lambda: time.time() + offset[0])
    up, up_client, base, rooms, server, client = await make_vod(rooms)
    try:
        show = vod_show(base)
        room = await vod_room(rooms, server, show)
        rid = room.room_id
        a, wa = await hello(client, rid, key=room.owner_key)
        await drain(a)
        rooms.control(rid, wa["viewer_id"], "play", room.video.duration - 2)
        await drain(a)
        offset[0] += 5
        rooms.tick()
        s = await recv_type(a, "state")
        check("播完自动下一集：第 2 集、播放中、从 0 开始", s and s["video"]["episode"] == 1 and s["playing"] is True
              and s["position"] == 0 and s["video"]["version"] == 1 and room.video.vod.line == 0)
        check("自动下一集后缓存新版本", (rid, 1) in server._playlists and (rid, 0) not in server._playlists)

        last = await vod_room(rooms, server, show, episode=2)
        b, wb = await hello(client, last.room_id, key=last.owner_key)
        await drain(b)
        rooms.control(last.room_id, wb["viewer_id"], "play", last.video.duration - 2)
        await drain(b)
        offset[0] += 5
        rooms.tick()
        await asyncio.sleep(0.3)
        got = await drain(b)
        check("最后一集播完停住", last.version == 0 and last.video.vod.episode == 2
              and not any(m["type"] == "state" for m in got))
    finally:
        await client.close()
        await up_client.close()


# ---------------- 管理后台 ----------------
class FakeStats:
    def __init__(self):
        self.calls = 0

    def sample(self):
        self.calls += 1
        return {"cpu_percent": 12.5, "mem": {"total": 100, "available": 60, "used": 40}}


async def test_admin():
    rooms = RoomManager()
    reqs = RequestStore(None)
    reqs.add("绝命毒师", KIND_NOT_LISTED, 1, "小明", 555)
    reqs.add("老友记", KIND_NO_RESULTS, 2, "小红", 556)
    stats = FakeStats()
    server = ws_mod.WatchServer(rooms, FakeSources(), "127.0.0.1", 0, admin_key="JihyosDog", admin_password="Nwcambrosia4",
                                requests=reqs, stats=stats, channel_name=lambda cid: "general" if cid == 1 else None)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    token = server.admin_token
    auth = {"Cookie": f"watch_admin={token}"}
    try:
        room = rooms.create(1, 2, "发起人", yt(duration=300.0))
        rooms.create(2, 3, "另一个", bili())
        a, wa = await hello(client, room.room_id, key=room.owner_key)
        b, wb = await hello(client, room.room_id)
        rooms.control(room.room_id, wa["viewer_id"], "play", 10.0)

        r = await client.get("/admin")
        text = await r.text()
        check("没登录：登录页（密钥 + 密码两个框）", r.status == 200 and 'name="key"' in text and 'name="password"' in text
              and "/static/admin.js" not in text and r.headers.get("Cache-Control") == "no-store")
        check("没登录 api 401", (await client.get("/admin/api")).status == 401)
        check("令牌不是密钥本身，?k= 不再生效", token != "JihyosDog" and len(token) == 64
              and "/static/admin.js" not in await (await client.get("/admin?k=JihyosDog")).text()
              and (await client.get("/admin", headers={"Cookie": "watch_admin=JihyosDog"})).status == 200
              and "/static/admin.js" not in await (await client.get("/admin", headers={"Cookie": "watch_admin=JihyosDog"})).text())
        r = await client.post("/admin/login", data={"key": "JihyosDog", "password": "wrong"})
        check("密码错：401 + 提示", r.status == 401 and "密钥或密码不对" in await r.text() and "Set-Cookie" not in r.headers)
        r = await client.post("/admin/login", data={"key": "wrong", "password": "Nwcambrosia4"})
        check("密钥错：401", r.status == 401)
        r = await client.post("/admin/login", data={"key": "JihyosDog"})
        check("缺密码：401", r.status == 401)
        r = await client.post("/admin/login", data={"key": "JihyosDog", "password": "Nwcambrosia4"}, allow_redirects=False)
        cookie = r.headers.get("Set-Cookie", "")
        check("都对：303 回 /admin、写 cookie", r.status == 303 and r.headers.get("Location") == "/admin"
              and f"watch_admin={token}" in cookie and "HttpOnly" in cookie and "Path=/admin" in cookie)
        r = await client.get("/admin", headers=auth)
        check("带 cookie 开后台页", r.status == 200 and "/static/admin.js" in await r.text())
        check("admin.js 在 static 白名单", (await client.get("/static/admin.js")).status == 200)
        r = await client.get("/admin/api", headers=auth)
        body = await r.text()
        d = json.loads(body)
        check("api 200 JSON 不缓存", r.status == 200 and r.content_type == "application/json"
              and r.headers.get("Cache-Control") == "no-store")
        client.session.cookie_jar.clear()     # TestClient 的 jar 会记下登录 cookie，先清掉再测头
        check("api 也认 X-Admin-Token 头", (await client.get("/admin/api", headers={"X-Admin-Token": token})).status == 200
              and (await client.get("/admin/api", headers={"X-Admin-Token": "JihyosDog"})).status == 401)
        r = await client.get("/admin/logout", allow_redirects=False)
        check("退出：清 cookie 回登录页", r.status == 303 and "watch_admin=" in r.headers.get("Set-Cookie", "")
              and ("Max-Age=0" in r.headers.get("Set-Cookie", "") or "expires" in r.headers.get("Set-Cookie", "").lower()))
        check("概览", d["summary"] == {"rooms": 2, "viewers": 2, "bili_rooms": 1, "bili_bytes_today": 0}
              and isinstance(d["server_time"], float) and d["started_at"] <= d["server_time"])
        r0 = d["rooms"][0]
        check("房间信息", r0["room_id"] == room.room_id and r0["url"] == f"/r/{room.room_id}"
              and r0["channel"] == {"id": "1", "name": "general"} and r0["title"] == "YT" and r0["source"] == "youtube"
              and r0["source_label"] == "YouTube" and r0["playing"] is True and r0["finished"] is False
              and 10.0 <= r0["position"] < 12.0 and r0["duration"] == 300.0 and r0["host"] == "发起人"
              and r0["creator"] == "发起人" and r0["age"] >= 0 and r0["episode"] is None)
        names = {m["name"]: m for m in r0["viewers"]}
        check("观众名单含房主标记", len(r0["viewers"]) == 2 and names[wa["name"]]["is_host"] and not names[wb["name"]]["is_host"]
              and names[wb["name"]]["online"] and names[wb["name"]]["can_control"] is False)
        check("没名字的频道只给 id", d["rooms"][1]["channel"] == {"id": "2", "name": None} and d["rooms"][1]["host"] is None
              and d["rooms"][1]["viewers"] == [] and d["rooms"][1]["source_label"] == "B 站")
        check("不泄露房主密钥、viewer_id、后台密钥", room.owner_key not in body and wa["viewer_id"] not in body
              and wb["viewer_id"] not in body and "JihyosDog" not in body and "Nwcambrosia4" not in body)
        check("请求记录", [q["query"] for q in d["requests"]] == ["绝命毒师", "老友记"]
              and d["requests"][0]["kind_label"] == "搜到了但没有想看的" and d["requests"][1]["kind"] == "no_results"
              and d["requests"][0]["channel"] == {"id": "555", "name": None} and d["requests"][0]["key"] == "绝命毒师"
              and d["request_limit"] == 5)
        check("资源用量来自 stats、短时间内不重采", d["stats"]["cpu_percent"] == 12.5 and stats.calls == 1)
        await client.get("/admin/api", headers=auth)
        check("2 秒内复用采样", stats.calls == 1)
        check("没开地区限制", d["geo"] == {"enabled": False} and d["blocked"] == [])

        # 连续输错锁 IP
        old = ws_mod.ADMIN_LOGIN_MAX_FAILURES
        ws_mod.ADMIN_LOGIN_MAX_FAILURES = 3
        try:
            hdr = {"X-Forwarded-For": "8.8.8.8"}
            for _ in range(3):
                await client.post("/admin/login", data={"key": "x", "password": "y"}, headers=hdr)
            r = await client.post("/admin/login", data={"key": "JihyosDog", "password": "Nwcambrosia4"}, headers=hdr,
                                  allow_redirects=False)
            check("输错 3 次后即使对了也是 429", r.status == 429 and "输错太多次" in await r.text())
            r = await client.post("/admin/login", data={"key": "JihyosDog", "password": "Nwcambrosia4"},
                                  headers={"X-Forwarded-For": "8.8.4.4"}, allow_redirects=False)
            check("别的 IP 不受影响", r.status == 303)
        finally:
            ws_mod.ADMIN_LOGIN_MAX_FAILURES = old
        await a.close()
        await b.close()
    finally:
        await client.close()

    # 只配密钥没配密码：登录页只有密钥框，密钥对就进
    server = ws_mod.WatchServer(rooms, FakeSources(), "127.0.0.1", 0, admin_key="onlykey")
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        text = await (await client.get("/admin")).text()
        r = await client.post("/admin/login", data={"key": "onlykey"}, allow_redirects=False)
        check("没配密码：只要密钥", 'name="password"' not in text and r.status == 303 and server.admin_password is None)
    finally:
        await client.close()

    # 没有 admin_key：一律 404；没有 requests / stats 也能出快照
    server = ws_mod.WatchServer(rooms, FakeSources(), "127.0.0.1", 0, admin_password="pw")
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        check("没配密钥：/admin 全 404", (await client.get("/admin")).status == 404
              and (await client.get("/admin/api")).status == 404
              and (await client.post("/admin/login", data={"key": "", "password": "pw"})).status == 404
              and server.admin_key is None and server.admin_token is None)
        snap = server.admin_snapshot()
        check("无 requests / stats 的快照", snap["requests"] == [] and snap["stats"] is None and snap["request_limit"] is None)
    finally:
        await client.close()

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "watch_admin_key")
        k1 = ws_mod.load_admin_key(p)
        k2 = ws_mod.load_admin_key(p)
        with open(p, encoding="utf-8") as f:
            saved = f.read().strip()
        check("自动生成的密钥存文件、重启不变", len(k1) >= 24 and k1 == k2 and saved == k1)
        check("配置了就用配置的", ws_mod.load_admin_key(p, " mykey ") == "mykey" and ws_mod.load_admin_key(None, "") != "")


async def test_geo_block():
    import ipaddress

    def ip4(s):
        return int(ipaddress.IPv4Address(s))
    geo = GeoDB(None, allowed=("AU", "NZ"))
    geo.set_ranges([(ip4("1.0.0.0"), ip4("1.0.0.255"), "AU"), (ip4("8.8.8.0"), ip4("8.8.8.255"), "US")], [], fetched_at=1.0)
    blocks = BlockLog(None)
    rooms = RoomManager()
    sources = FakeSources()
    server = ws_mod.WatchServer(rooms, sources, "127.0.0.1", 0, admin_key="k", geo=geo, blocks=blocks)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        room = rooms.create(1, 2, "发起人", yt(duration=300.0))
        rid = room.room_id
        check("房间页不拦", (await client.get(f"/r/{rid}", headers={"X-Forwarded-For": "8.8.8.8"})).status == 200)

        ws = await client.ws_connect(f"/ws/{rid}", headers={"X-Forwarded-For": "8.8.8.8"})
        await ws.send_json({"type": "hello", "viewer_id": None, "key": room.owner_key})
        got = await drain(ws, timeout=1.0)
        blocked = got[0] if got else {}
        check("美国 IP：只收到 blocked 然后断开", blocked.get("type") == "blocked" and blocked.get("country") == "US"
              and len(blocked.get("id", "")) == 6 and blocked.get("message") == ws_mod.BLOCKED_MSG
              and got[-1]["type"].startswith("__"))
        check("被拦的人不进房、房主密钥没被消耗", rooms.get(rid).members == {} and not room.key_used)
        entry = blocks.all()[0]
        check("记录 IP、编号、国家、房间", entry.ip == "8.8.8.8" and entry.id == blocked["id"] and entry.country == "US"
              and entry.rooms == [rid] and entry.hits == 1)

        ws2, w2 = await hello(client, rid)
        check("没有 XFF（本机）放行", w2 is not None and w2["name"])
        ws3 = await client.ws_connect(f"/ws/{rid}", headers={"X-Forwarded-For": "8.8.8.8, 1.0.0.9"})
        await ws3.send_json({"type": "hello", "viewer_id": None, "key": None})
        w3 = await recv_type(ws3, "welcome")
        check("XFF 以最后一段为准（伪造的前段无效）：澳洲放行", w3 is not None)
        ws4 = await client.ws_connect(f"/ws/{rid}", headers={"X-Forwarded-For": "1.0.0.9, 8.8.8.8"})
        await ws4.send_json({"type": "hello", "viewer_id": None, "key": None})
        got4 = await drain(ws4, timeout=1.0)
        check("最后一段是美国 → 拦", got4 and got4[0].get("type") == "blocked" and blocks.entries["8.8.8.8"].hits == 2)
        ws5 = await client.ws_connect(f"/ws/{rid}", headers={"X-Forwarded-For": "9.9.9.9"})
        await ws5.send_json({"type": "hello", "viewer_id": None, "key": None})
        got5 = await drain(ws5, timeout=1.0)
        check("不在分配表里的 IP 也拦、国家为 None", got5 and got5[0].get("type") == "blocked" and got5[0]["country"] is None
              and blocks.entries["9.9.9.9"].country is None)
        ws6 = await client.ws_connect("/ws/zzzzzz", headers={"X-Forwarded-For": "8.8.8.8"})
        got6 = await drain(ws6, timeout=1.0)
        check("不存在的房间不记", got6 and got6[0]["type"] == "ended" and blocks.entries["8.8.8.8"].hits == 2)

        broom = rooms.create(1, 2, "发起人", bili())
        r = await client.get(f"/bili/{broom.room_id}/0/manifest.mpd", headers={"X-Forwarded-For": "8.8.8.8"})
        check("B 站媒体路由 403", r.status == 403 and sources.calls == 0 and blocks.entries["8.8.8.8"].hits == 3
              and broom.room_id in blocks.entries["8.8.8.8"].rooms)

        snap = server.admin_snapshot()
        check("后台快照里有地区状态和拦截记录", snap["geo"]["enabled"] and snap["geo"]["loaded"] and snap["geo"]["allowed"] == ["AU", "NZ"]
              and snap["geo"]["ranges"] == 2 and {b["ip"] for b in snap["blocked"]} == {"8.8.8.8", "9.9.9.9"}
              and snap["blocked"][0]["hits"] >= 1 and "id" in snap["blocked"][0])
        await ws2.close()
        await ws3.close()
    finally:
        await client.close()

    # 数据还没加载：不拦
    server = ws_mod.WatchServer(rooms, sources, "127.0.0.1", 0, geo=GeoDB(None), blocks=BlockLog(None))
    client = TestClient(TestServer(server.app))
    await client.start_server()
    try:
        room = rooms.create(1, 2, "发起人", yt(duration=300.0))
        ws = await client.ws_connect(f"/ws/{room.room_id}", headers={"X-Forwarded-For": "8.8.8.8"})
        await ws.send_json({"type": "hello", "viewer_id": None, "key": None})
        check("地区数据没加载时放行", (await recv_type(ws, "welcome")) is not None and server.blocks.all() == []
              and server.admin_snapshot()["geo"]["loaded"] is False)
        await ws.close()
    finally:
        await client.close()

# ---------------- 电视直播 ----------------
TV_ALLOW = lambda ip: ip == "127.0.0.1"  # noqa: E731


class TvUpstream:
    """假直播源：/{name}/index.m3u8 每次请求序号 +1（分片在往前走），/{name}/seg{n}.ts 返回 2 KB；
    /bad/ 和 fail 里的名字一律 500；delay 让每个请求先等一会（制造“正在换源”的窗口）。"""

    def __init__(self):
        self.hits = Counter()
        self.seq = Counter()
        self.fail = set()
        self.delay = 0.0
        self.server = None

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.port}"

    def total(self):
        return sum(self.hits.values())

    async def start(self):
        @web.middleware
        async def count(request, handler):
            self.hits[request.path] += 1
            return await handler(request)

        async def handle(request):
            name, file = request.match_info["name"], request.match_info["file"]
            if self.delay:
                await asyncio.sleep(self.delay)
            if name == "bad" or name in self.fail:
                return web.Response(status=500, text="boom")
            if file == "index.m3u8":
                self.seq[name] += 1
                s = self.seq[name]
                body = "".join(f"#EXTINF:2,\nseg{s + k}.ts\n" for k in range(3))
                return web.Response(text=f"#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:{s}\n{body}",
                                    content_type="application/vnd.apple.mpegurl")
            if file.startswith("seg") and file.endswith(".ts"):
                return web.Response(body=tv_seg_bytes(name, file), content_type="video/mp2t")
            return web.Response(status=404)

        app = web.Application(middlewares=[count])
        app.router.add_get("/{name}/{file}", handle)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()


def tv_seg_bytes(name, file):
    return (f"{name}/{file};".encode() * 200)[:2048]


def tv_channel(base, names=("bad", "ok1", "ok2")):
    from tv_channels import Candidate, Channel
    return Channel(key="测试台", name="测试台",
                   candidates=[Candidate(f"{base}/{n}/index.m3u8", None, 0, False) for n in names])


async def make_tv(**kw):
    import tv_net
    up = TvUpstream()
    await up.start()
    rooms = RoomManager(max_tv_rooms=5)
    server = ws_mod.WatchServer(rooms, FakeSources(), "127.0.0.1", 0, ip_allowed=TV_ALLOW,
                                tv_session_factory=lambda: tv_net.make_session(TV_ALLOW),
                                tv_prepare_timeout=kw.pop("tv_prepare_timeout", 5.0), **kw)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    return up, rooms, server, client


async def wait_until(cond, timeout=5.0, step=0.02):
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(step)
    return True


def count_handled(server, kinds):
    """包一层 server._handle，记下处理过的指定类型消息数（_handle 是同步的，记完就说明已经做完决定）。"""
    seen = Counter()
    orig = server._handle

    def spy(conn, data):
        orig(conn, data)
        if data.get("type") in kinds:
            seen[data["type"]] += 1
    server._handle = spy
    return seen


def tv_idle(server, rid):
    return rid not in server._tv_switching and not server._tasks


def tv_headers_ok(r):
    return (r.headers.get("X-Content-Type-Options") == "nosniff"
            and r.headers.get("Cache-Control") == "no-store")


async def test_tv_routes():
    up, rooms, server, client = await make_tv()
    try:
        check("电视常量", ws_mod.NO_TV_SOURCE == "这个台暂时没有能播的源" and ws_mod.TV_SWITCHED == "已切换片源"
              and ws_mod.TV_PREPARE_TIMEOUT == 20.0 and ws_mod.STALL_COOLDOWN == 15.0
              and ws_mod.WatchServer(RoomManager(), FakeSources(), "127.0.0.1", 0).tv_prepare_timeout == 20.0)
        ch = tv_channel(up.base)
        video = await server.prepare_tv(ch)
        info = video.tv
        check("prepare_tv 跳过坏源，选 ok1（下标 1）", video.source == "tv" and info.index == 1
              and video.url == f"{up.base}/ok1/index.m3u8" and info.bad == {0}
              and [c.url for c in info.order] == [c.url for c in ch.ordered()])
        check("prepare_tv 探测过坏源", up.hits["/bad/index.m3u8"] >= 1)
        room = rooms.create(1, 2, "发起人", video)
        rid = room.room_id

        r = await client.get(f"/tv/{rid}/0/index.m3u8")
        text = await r.text()
        segs = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
        check("tv 播放列表 200、类型、头", r.status == 200 and r.content_type == "application/vnd.apple.mpegurl"
              and tv_headers_ok(r))
        check("tv 播放列表只含本站分片路径", segs and all(ln.startswith(f"/tv/{rid}/0/s/") for ln in segs)
              and "127.0.0.1" not in text)
        r = await client.get(segs[-1])
        body = await r.read()
        check("tv 分片 200、上游内容、头", r.status == 200 and body.startswith(b"ok1/seg")
              and len(body) == 2048 and r.content_type == "video/mp2t" and tv_headers_ok(r))

        n = up.total()
        r = await client.get(f"/tv/{rid}/0/s/999")
        check("没发过的分片编号 404、不碰上游", r.status == 404 and up.total() == n and tv_headers_ok(r))
        for path in (f"/tv/{rid}/0/s/abc", f"/tv/{rid}/0/s/-1", f"/tv/{rid}/1/index.m3u8",
                     f"/tv/{rid}/-1/index.m3u8", f"/tv/{rid}/x/index.m3u8", "/tv/zzzzzz/0/index.m3u8",
                     "/tv/zzzzzz/0/s/0"):
            r = await client.get(path)
            check(f"{path} → 404", r.status == 404 and tv_headers_ok(r))
        yt_room = rooms.create(1, 2, "x", yt())
        r = await client.get(f"/tv/{yt_room.room_id}/0/index.m3u8")
        r2 = await client.get(f"/tv/{yt_room.room_id}/0/s/0")
        check("别的房间（不是 tv）→ 404", r.status == 404 and r2.status == 404)
        check("404 们都没碰上游", up.total() == n)

        # 另一个 tv 房间发出去的分片编号，拿到本房间来要 → 404、不碰上游
        import tv_relay
        ids1 = {int(ln.rsplit("/", 1)[1]) for ln in segs}
        other = rooms.create(1, 2, "x", await server.prepare_tv(ch))
        orig_coalesce = tv_relay.COALESCE_SECONDS
        tv_relay.COALESCE_SECONDS = 0.0      # 让另一个房间多取几次列表，编号走到本房间没发过的范围
        try:
            ids2 = set()
            for _ in range(3):
                r = await client.get(f"/tv/{other.room_id}/0/index.m3u8")
                ids2 |= {int(ln.rsplit("/", 1)[1]) for ln in (await r.text()).splitlines()
                         if ln.startswith(f"/tv/{other.room_id}/0/s/")}
        finally:
            tv_relay.COALESCE_SECONDS = orig_coalesce
        foreign = sorted(ids2 - ids1)
        n = up.total()
        statuses = []
        for k in foreign:
            r = await client.get(f"/tv/{rid}/0/s/{k}")
            statuses.append(r.status)
        check(f"别的 tv 房间发出的编号 {foreign} 在本房间 → 404、不碰上游",
              foreign and all(st == 404 for st in statuses) and up.total() == n)
        rooms.close(other.room_id)

        a, _ = await hello(client, rid, key=room.owner_key)
        s = await recv_type(a, "state")
        v = s and s["video"]
        check("tv state.video 字段", v and v["source"] == "tv" and v["live"] is True
              and v["hls"] == f"/tv/{rid}/0/index.m3u8" and v["line_count"] == 3 and v["line"] == 1
              and v["mpd"] is None and s["position"] == 0)
        await a.send_json({"type": "control", "action": "play", "position": 0})
        s = await recv_type(a, "state")
        await asyncio.sleep(0.05)
        a2, _ = await hello(client, rid)
        s2 = await recv_type(a2, "state")
        check("tv 播放中 position 仍是 0", s and s["playing"] is True and s2 and s2["position"] == 0)
        c, _ = await hello(client, yt_room.room_id)
        s = await recv_type(c, "state")
        check("非 tv 的 state：live 为 False", s and s["video"]["live"] is False)
        await a.close()
        await a2.close()
        await c.close()

        rooms.close(rid)
        check("关房后清掉直播流", not any(k[0] == rid for k in server._tv_streams))
    finally:
        await client.close()
        await up.server.close()


async def test_tv_switch():
    up, rooms, server, client = await make_tv()
    orig_cooldown = ws_mod.STALL_COOLDOWN
    try:
        # 四个源：坏、ok1、ok2、ok3（多一个好源，赛跑时不管谁赢都还有源可换）
        room = rooms.create(1, 2, "发起人", await server.prepare_tv(tv_channel(up.base, ("bad", "ok1", "ok2", "ok3"))))
        rid = room.room_id
        a, _ = await hello(client, rid, key=room.owner_key)
        b, _ = await hello(client, rid)
        c, _ = await hello(client, rid)
        for w in (a, b, c):
            await drain(w)
        r = await client.get(f"/tv/{rid}/0/index.m3u8")
        old_segs = [ln for ln in (await r.text()).splitlines() if ln.startswith(f"/tv/{rid}/0/s/")]
        seen = count_handled(server, ("tv_next", "tv_stall"))

        await b.send_json({"type": "tv_next"})
        await wait_until(lambda: seen["tv_next"] == 1)
        check("非房主 tv_next 被忽略", room.version == 0 and tv_idle(server, rid))

        await a.send_json({"type": "tv_next"})
        s = await recv_type(a, "state", 5)
        n = await recv_type(a, "notice", 5)
        nb = await recv_type(b, "notice", 5)
        check("房主 tv_next → 下一个源（下标 2）、版本 +1", s and s["video"]["version"] == 1
              and s["video"]["line"] == 2 and s["video"]["hls"] == f"/tv/{rid}/1/index.m3u8"
              and s["position"] == 0)
        check("换源后所有人收到“已切换片源”", n and n["message"] == "已切换片源" and nb and nb["message"] == "已切换片源")
        check("换源后旧版本的直播流被清掉", (rid, 0) not in server._tv_streams)
        r = await client.get(f"/tv/{rid}/0/index.m3u8")
        check("旧版本号 404", r.status == 404)
        n_up = up.total()
        statuses = []
        for path in old_segs:
            r = await client.get(path)
            statuses.append(r.status)
        check("换源后旧版本真实发过的分片 → 404、不碰上游", old_segs and all(st == 404 for st in statuses)
              and up.total() == n_up)

        await a.send_json({"type": "tv_next"})
        s = await recv_type(a, "state", 5)
        check("再 tv_next → 最后一个源（下标 3）", s and s["video"]["version"] == 2 and s["video"]["line"] == 3)
        bad_hits = up.hits["/bad/index.m3u8"]
        await a.send_json({"type": "tv_next"})
        s = await recv_type(a, "state", 5)
        check("最后一个源再 tv_next → 绕回、跳过探测失败的 0", s and s["video"]["version"] == 3
              and s["video"]["line"] == 1 and room.video.tv.index == 1 and room.video.tv.bad == {0}
              and up.hits["/bad/index.m3u8"] > bad_hits)
        await wait_until(lambda: tv_idle(server, rid))
        for w in (a, b, c):
            await drain(w)

        # tv_stall 和房主 tv_next 赛跑 → 只换一次
        ws_mod.STALL_COOLDOWN = 0.0
        up.delay = 0.2
        v = room.version
        stalls, nexts = seen["tv_stall"], seen["tv_next"]
        await b.send_json({"type": "tv_stall", "version": v})
        await a.send_json({"type": "tv_next"})
        ok = await wait_until(lambda: seen["tv_stall"] == stalls + 1 and seen["tv_next"] == nexts + 1
                              and tv_idle(server, rid))
        check("tv_stall 与 tv_next 赛跑 → 版本只 +1", ok and room.version == v + 1 and room.video.tv.index == 2)
        up.delay = 0.0
        await wait_until(lambda: tv_idle(server, rid))

        # 两个人同时 tv_stall → 只换一次
        up.delay = 0.2
        v = room.version
        stalls = seen["tv_stall"]
        await b.send_json({"type": "tv_stall", "version": v})
        await c.send_json({"type": "tv_stall", "version": v})
        ok = await wait_until(lambda: seen["tv_stall"] == stalls + 2 and tv_idle(server, rid))
        check("两个人同时 tv_stall → 版本只 +1", ok and room.version == v + 1)
        check("自动换源跳过当前源（记为坏）", room.video.tv.index == 3 and 2 in room.video.tv.bad)
        up.delay = 0.0
        for w in (a, b, c):
            await drain(w)

        # 冷却：刚换过 15 秒内的 tv_stall 不换
        ws_mod.STALL_COOLDOWN = orig_cooldown
        v = room.version
        await b.send_json({"type": "tv_stall", "version": v})
        ok = await wait_until(lambda: seen["tv_stall"] == stalls + 3)
        check("15 秒内第三次 tv_stall → 不换", ok and room.version == v and tv_idle(server, rid))

        ws_mod.STALL_COOLDOWN = 0.0
        for bad_v in (v - 1, v + 1, str(v), None):
            await b.send_json({"type": "tv_stall", "version": bad_v})
        ok = await wait_until(lambda: seen["tv_stall"] == stalls + 7)
        check("版本号不对的 tv_stall 被忽略", ok and room.version == v and tv_idle(server, rid))

        # 所有源都坏了：自动换源 → 所有人收到错误、版本不变
        for w in (a, b, c):
            await drain(w)
        up.fail = {"ok1", "ok2", "ok3"}
        ws_mod.STALL_COOLDOWN = orig_cooldown
        server._tv_last_switch.pop(rid, None)     # 让这次 tv_stall 不被前面成功换源的冷却挡住
        await b.send_json({"type": "tv_stall", "version": v})
        ea = await recv_type(a, "error", 5)
        ec = await recv_type(c, "error", 5)
        await wait_until(lambda: tv_idle(server, rid))
        check("全坏时自动换源 → 所有人收到“这个台暂时没有能播的源”、版本不变",
              ea and ea["message"] == "这个台暂时没有能播的源" and ec and ec["message"] == "这个台暂时没有能播的源"
              and room.version == v)
        eb = [m for m in await drain(b, 0.2) if m["type"] == "error"]
        await drain(a, 0.05)
        await drain(c, 0.05)
        check("第一次全坏只广播一次错误", len(eb) == 1)
        n_up, n_stall = up.total(), seen["tv_stall"]
        await b.send_json({"type": "tv_stall", "version": v})
        ok = await wait_until(lambda: seen["tv_stall"] == n_stall + 1)
        got = [m for w in (a, b, c) for m in await drain(w, 0.3)]
        check("全坏后冷却内再 tv_stall → 不再探测、不再广播错误", ok and tv_idle(server, rid)
              and up.total() == n_up and not any(m["type"] == "error" for m in got) and room.version == v)
        ws_mod.STALL_COOLDOWN = 0.0
        try:
            await server.prepare_tv(tv_channel(up.base))
            raised = None
        except Exception as e:
            raised = e
        check("全坏时 prepare_tv 抛 TvUnavailable", isinstance(raised, ws_mod.TvUnavailable)
              and raised.message == "这个台暂时没有能播的源")
        up.fail = set()

        # 准备超时 → TvUnavailable
        server.tv_prepare_timeout = 0.3
        up.delay = 2.0
        t0 = time.monotonic()
        try:
            await server.prepare_tv(tv_channel(up.base))
            raised = None
        except Exception as e:
            raised = e
        took = time.monotonic() - t0
        check(f"prepare_tv 超时 → TvUnavailable（{took:.2f}s）", isinstance(raised, ws_mod.TvUnavailable)
              and took < 1.5)
        up.delay = 0.0
        server.tv_prepare_timeout = 5.0

        # 只有一个源的台：tv_next 被忽略
        solo = rooms.create(1, 2, "x", await server.prepare_tv(tv_channel(up.base, ("ok1",))))
        d, _ = await hello(client, solo.room_id, key=solo.owner_key)
        await drain(d)
        n_next = seen["tv_next"]
        await d.send_json({"type": "tv_next"})
        ok = await wait_until(lambda: seen["tv_next"] == n_next + 1)
        await wait_until(lambda: tv_idle(server, solo.room_id))
        got = await drain(d, 0.2)
        check("只有一个源时 tv_next 被忽略", ok and solo.version == 0 and not any(m["type"] == "state" for m in got))

        yt_room = rooms.create(1, 2, "x", yt())
        e, _ = await hello(client, yt_room.room_id, key=yt_room.owner_key)
        await drain(e)
        await e.send_json({"type": "tv_next"})
        await e.send_json({"type": "tv_stall", "version": 0})
        got = await drain(e, 0.3)
        check("不是 tv 房间时 tv 消息被忽略", yt_room.version == 0 and not got)
    finally:
        ws_mod.STALL_COOLDOWN = orig_cooldown
        await client.close()
        await up.server.close()


async def test_tv_dead_auto_switch():
    import tv_relay
    up, rooms, server, client = await make_tv()
    orig_dead = tv_relay.DEAD_SECONDS
    try:
        tv_relay.DEAD_SECONDS = 0.3
        room = rooms.create(1, 2, "发起人", await server.prepare_tv(tv_channel(up.base)))
        rid = room.room_id
        a, _ = await hello(client, rid, key=room.owner_key)
        await drain(a)
        r = await client.get(f"/tv/{rid}/0/index.m3u8")
        check("断流前播放列表 200", r.status == 200)
        await r.read()

        up.fail = {"ok1"}
        statuses = []

        async def poll():
            while room.version == 0:
                r = await client.get(f"/tv/{rid}/0/index.m3u8")
                statuses.append(r.status)
                await r.read()
                await asyncio.sleep(0.1)
        try:
            await asyncio.wait_for(poll(), 6)
        except asyncio.TimeoutError:
            pass
        check("上游失败期间播放列表 502", 502 in statuses)
        check("断流超过 DEAD_SECONDS → 自动换源（版本 +1，换到 ok2）", room.version == 1
              and room.video.tv.index == 2 and 1 in room.video.tv.bad)
        n = await recv_type(a, "notice", 3)
        check("自动换源后也提示“已切换片源”", n and n["message"] == "已切换片源")
        await wait_until(lambda: tv_idle(server, rid))
        r = await client.get(f"/tv/{rid}/1/index.m3u8")
        check("换源后新版本播放列表 200", r.status == 200 and "/tv/" in await r.text())
        check("自动换源后版本不再变", room.version == 1)
    finally:
        tv_relay.DEAD_SECONDS = orig_dead
        await client.close()
        await up.server.close()


async def test_tv_stale_error_and_duration():
    up, rooms, server, client = await make_tv()
    orig_cooldown = ws_mod.STALL_COOLDOWN
    try:
        ws_mod.STALL_COOLDOWN = 0.0
        # 三个源：坏源在建房时已记进 bad，自动换源还得真去探 ok2，才有“正在找源”的窗口
        room = rooms.create(1, 2, "发起人", await server.prepare_tv(tv_channel(up.base, ("bad", "ok1", "ok2"))))
        rid = room.room_id
        a, wa = await hello(client, rid, key=room.owner_key)
        b, _ = await hello(client, rid)
        await drain(a)
        await drain(b)

        seen = count_handled(server, ("duration",))
        await a.send_json({"type": "duration", "value": 120.0})
        ok = await wait_until(lambda: seen["duration"] == 1)
        got = await drain(b, 0.3)
        check("直播房间不接受 duration", ok and room.video.duration is None and room.version == 0
              and not any(m["type"] == "state" for m in got))

        # 自动换源找源期间房主换成了 YouTube：失败后不发过时的错误、不记冷却
        up.fail = {"ok1", "ok2"}
        up.delay = 0.3
        await b.send_json({"type": "tv_stall", "version": 0})
        await wait_until(lambda: rid in server._tv_switching)
        check("换成 YouTube", rooms.change_video(rid, wa["viewer_id"], yt()))
        await wait_until(lambda: tv_idle(server, rid))
        got = [m for w in (a, b) for m in await drain(w, 0.3)]
        check("视频已换走 → 换源失败不广播错误、不记冷却", room.video.source == "youtube"
              and not any(m["type"] == "error" for m in got) and rid not in server._tv_last_switch)

        # 找源期间房间被关：同样静默
        up.delay = 0.0
        up.fail = set()
        room2 = rooms.create(1, 2, "发起人", await server.prepare_tv(tv_channel(up.base, ("bad", "ok2", "ok1"))))
        rid2 = room2.room_id
        c, _ = await hello(client, rid2)
        await drain(c)
        up.fail = {"ok1", "ok2"}
        up.delay = 0.3
        await c.send_json({"type": "tv_stall", "version": 0})
        await wait_until(lambda: rid2 in server._tv_switching)
        rooms.close(rid2)
        await wait_until(lambda: tv_idle(server, rid2))
        check("房间已关 → 换源失败不记冷却", rid2 not in server._tv_last_switch)
    finally:
        ws_mod.STALL_COOLDOWN = orig_cooldown
        up.delay = 0.0
        await client.close()
        await up.server.close()


async def test_tv_bad_memory():
    """prepare_tv 探测失败的源记进 bad：自动换源不再先探；手动绕回开头清空 bad 后会再试。"""
    up, rooms, server, client = await make_tv()
    orig_cooldown = ws_mod.STALL_COOLDOWN
    try:
        ws_mod.STALL_COOLDOWN = 0.0
        room = rooms.create(1, 2, "发起人", await server.prepare_tv(tv_channel(up.base, ("bad", "ok1", "ok2"))))
        rid = room.room_id
        check("建房时探测失败的源记进 bad", room.video.tv.index == 1 and room.video.tv.bad == {0})
        a, _ = await hello(client, rid, key=room.owner_key)
        b, _ = await hello(client, rid)
        await drain(a)
        await drain(b)

        up.fail = {"ok2"}
        bad_hits = up.hits["/bad/index.m3u8"]
        await b.send_json({"type": "tv_stall", "version": 0})
        await wait_until(lambda: rid in server._tv_last_switch and tv_idle(server, rid))
        check("自动换源不再探测已知坏源", up.hits["/bad/index.m3u8"] == bad_hits and room.version == 0
              and room.video.tv.index == 1)
        await drain(a)
        await drain(b)

        # 手动：ok1 → ok2（bad 保持 {0}），再手动就绕回开头
        up.fail = set()
        await a.send_json({"type": "tv_next"})
        s = await recv_type(a, "state", 5)
        check("手动换到 ok2", s and s["video"]["line"] == 2 and room.video.tv.bad == {0})
        bad_hits = up.hits["/bad/index.m3u8"]
        await a.send_json({"type": "tv_next"})
        s = await recv_type(a, "state", 5)
        check("手动绕回开头：清空 bad、重新探测坏源", s and s["video"]["line"] == 1
              and up.hits["/bad/index.m3u8"] > bad_hits and room.video.tv.bad == {0})
    finally:
        ws_mod.STALL_COOLDOWN = orig_cooldown
        await client.close()
        await up.server.close()


async def test_tv_manual_same():
    """手动换源别的源全坏、绕回当前源：不换版本、不发“已切换片源”，只告诉房主没有能播的源。"""
    up, rooms, server, client = await make_tv()
    try:
        room = rooms.create(1, 2, "发起人", await server.prepare_tv(tv_channel(up.base, ("ok1", "bad", "ok2"))))
        rid = room.room_id
        a, _ = await hello(client, rid, key=room.owner_key)
        b, _ = await hello(client, rid)
        await drain(a)
        await drain(b)
        up.fail = {"ok2"}
        ok2_hits = up.hits["/ok2/index.m3u8"]
        await a.send_json({"type": "tv_next"})
        e = await recv_type(a, "error", 5)
        await wait_until(lambda: tv_idle(server, rid))
        got_a = await drain(a, 0.3)
        got_b = await drain(b, 0.3)
        check("绕回当前源 → 房主收到“这个台暂时没有能播的源”", e and e["message"] == "这个台暂时没有能播的源")
        check("绕回当前源 → 版本不变、源不变", room.version == 0 and room.video.tv.index == 0
              and up.hits["/ok2/index.m3u8"] > ok2_hits)
        check("绕回当前源 → 没人收到“已切换片源”或新 state",
              not any(m["type"] in ("notice", "state") for m in got_a + got_b)
              and not any(m["type"] == "error" for m in got_b))
    finally:
        await client.close()
        await up.server.close()


async def main():
    await test_ws_flow()
    await test_duration_and_change_video()
    await test_change_video_errors()
    await test_change_video_playurl_rejected()
    await test_bili_proxy()
    await test_pages()
    test_build_mpd()
    await test_lifecycle()
    await test_vod_playlist_route()
    await test_vod_state_message()
    await test_vod_episode_switch()
    await test_vod_next_line()
    await test_vod_resume()
    await test_vod_switch_race()
    await test_vod_prepare_timeout()
    await test_vod_auto_next()
    await test_admin()
    await test_geo_block()
    await test_tv_routes()
    await test_tv_switch()
    await test_tv_dead_auto_switch()
    await test_tv_stale_error_and_duration()
    await test_tv_bad_memory()
    await test_tv_manual_same()


if __name__ == "__main__":
    asyncio.run(main())
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
