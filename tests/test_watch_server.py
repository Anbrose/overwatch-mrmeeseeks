"""一起看网页服务测试：页面、WebSocket 同步协议、B 站 DASH 转流。

运行：python tests/test_watch_server.py
不联网：B 站上游用本地 aiohttp 假服务器模拟，Sources 用假实现。
"""
import asyncio
import json
import os
import sys
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import watch_server as ws_mod  # noqa: E402
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
        check("members 发给房主且不含房主", ma and [m["viewer_id"] for m in ma["members"]] == [bid]
              and ma["members"][0]["can_control"] is False and ma["members"][0]["name"] == wb["name"])

        # 无权限 control → 状态不变、连接仍开着
        await b.send_json({"type": "control", "action": "play", "position": 5})
        got = await drain(a) + await drain(b)
        check("无权限 control 无效", not any(m["type"] == "state" for m in got) and not room.playback.playing)
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
        await a.send_json({"type": "grant", "target": bid, "allowed": True})
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

        # 普通观众发 grant 无效
        c, wc = await hello(client, rid)
        cid = wc["viewer_id"]
        await drain(c)
        await c.send_json({"type": "grant", "target": cid, "allowed": True})
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
        await b.send_json({"type": "duration", "value": "x"})
        await b.send_json({"type": "duration", "value": 212.5})
        s = await recv_type(a, "state")
        check("时长未知时接受 duration", s and s["video"]["duration"] == 212.5 and room.video.duration == 212.5)
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
        check("取地址失败 502", r.status == 502)
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
        r = await client.get("/r/zzzzzz")
        text = await r.text()
        check("不存在的房间 404 文案", r.status == 404 and "房间不存在或已经结束" in text)
        r = await client.get("/static/player.js")
        check("static 白名单内 200", r.status == 200 and "javascript" in r.content_type)
        r = await client.get("/static/style.css")
        check("static css 类型", r.status == 200 and r.content_type == "text/css")
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
    server = ws_mod.WatchServer(rooms, FakeSources(), "127.0.0.1", 0)
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

    # 有打开的 WebSocket 时 close() 也要很快结束（docker stop 只给 10 秒）
    rooms = RoomManager()
    server = ws_mod.WatchServer(rooms, FakeSources(), "127.0.0.1", 18766)
    await server.start()
    room = rooms.create(1, 2, "x", yt())
    import aiohttp
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


async def main():
    await test_ws_flow()
    await test_duration_and_change_video()
    await test_change_video_errors()
    await test_bili_proxy()
    await test_pages()
    test_build_mpd()
    await test_lifecycle()


if __name__ == "__main__":
    asyncio.run(main())
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
