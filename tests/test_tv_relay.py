"""一起看电视直播：直播中继（改写媒体列表、按编号转发分片、判断断流）测试。

运行：python tests/test_tv_relay.py
不联网：用本地 aiohttp 假服务器，ip_allowed 只放行 127.0.0.1；计时用假时钟。
"""
import asyncio
import os
import re
import sys
from collections import Counter

from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import tv_net  # noqa: E402
import tv_relay  # noqa: E402
from tv_channels import Candidate  # noqa: E402
from tv_stream import StreamError  # noqa: E402

results = []
ALLOW = lambda ip: ip == "127.0.0.1"  # noqa: E731
PREFIX = "/tv/R/1"


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Upstream:
    """假上游：主列表 -> live/media.m3u8；媒体列表内容、序号、失败次数都可在调用之间改。"""

    def __init__(self):
        self.hits = Counter()
        self.seq = 100
        self.body = None            # 媒体列表正文（不含头），None 用默认
        self.media_fail = 0         # 接下来这么多次媒体列表返回 404
        self.media_status = None    # 持续返回这个状态码
        self.delay = 0.05
        self.server = None

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.port}"

    def default_body(self):
        return ("#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n"
                "#EXTINF:5,\na/1.ts\n"
                "#EXTINF:5,\n/abs/2.ts\n"
                f"#EXTINF:5,\n{self.base}/other/3.ts\n")

    async def start(self):
        @web.middleware
        async def count(request, handler):
            self.hits[request.path] += 1
            return await handler(request)

        async def master(request):
            return web.Response(text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000\nlive/media.m3u8\n")

        async def media(request):
            await asyncio.sleep(self.delay)
            if self.media_status:
                return web.Response(status=self.media_status)
            if self.media_fail:
                self.media_fail -= 1
                return web.Response(status=404)
            body = self.body if self.body is not None else self.default_body()
            return web.Response(text=f"#EXTM3U\n#EXT-X-TARGETDURATION:5\n"
                                     f"#EXT-X-MEDIA-SEQUENCE:{self.seq}\n{body}")

        def seg(data, ctype):
            async def h(request):
                await asyncio.sleep(self.delay)
                return web.Response(body=data, content_type=ctype)
            return h

        async def bad(request):
            await asyncio.sleep(self.delay)
            return web.Response(status=500)

        async def big(request):
            return web.Response(body=b"\x47" * (21 * 1024 * 1024), content_type="video/mp2t")

        app = web.Application(middlewares=[count])
        app.router.add_get("/master.m3u8", master)
        app.router.add_get("/live/media.m3u8", media)
        app.router.add_get("/live/a/1.ts", seg(b"SEG1" * 500, "video/mp2t"))
        app.router.add_get("/abs/2.ts", seg(b"SEG2" * 500, "video/mp2t"))
        app.router.add_get("/other/3.ts", seg(b"SEG3" * 500, "video/mp2t"))
        app.router.add_get("/live/key.bin", seg(b"K" * 16, "application/octet-stream"))
        app.router.add_get("/live/bad{n}.ts", bad)
        app.router.add_get("/live/big.ts", big)
        self.server = TestServer(app, host="127.0.0.1")
        await self.server.start_server()

    def total(self):
        return sum(self.hits.values())


def seg_ids(text):
    return [int(m) for m in re.findall(r"^/tv/R/1/s/(\d+)$", text, re.M)]


def key_id(text):
    m = re.search(r'#EXT-X-KEY:.*URI="/tv/R/1/s/(\d+)"', text)
    return int(m.group(1)) if m else None


async def run():
    up = Upstream()
    await up.start()
    session = tv_net.make_session(ip_allowed=ALLOW)

    def stream(clock=None):
        cand = Candidate(url=up.base + "/master.m3u8", ua=None, list_index=0, is_4k=False)
        return tv_relay.LiveStream(session, cand, PREFIX, ip_allowed=ALLOW, clock=clock or FakeClock())

    try:
        await test_rewrite(up, stream)
        await test_coalesce(up, stream)
        await test_reresolve(up, stream)
        await test_segments(up, stream)
        await test_cap(up, stream)
        await test_dead(up, stream)
    finally:
        await session.close()
        await up.server.close()


def reset(up):
    up.hits.clear()
    up.seq = 100
    up.body = None
    up.media_fail = 0
    up.media_status = None


async def test_rewrite(up, stream):
    reset(up)
    clk = FakeClock()
    s = stream(clk)
    text = await s.playlist()
    check("rewrite: no upstream host", "127.0.0.1" not in text and up.base not in text)
    check("rewrite: no .ts left", ".ts" not in text)
    check("rewrite: 3 segment lines", len(seg_ids(text)) == 3)
    check("rewrite: one key URI rewritten", text.count('URI="/tv/R/1/s/') == 1 and key_id(text) is not None)
    check("rewrite: only /tv paths in non-# lines",
          all(ln.startswith(PREFIX + "/s/") for ln in text.splitlines() if ln and not ln.startswith("#")))
    check("rewrite: media sequence kept", "#EXT-X-MEDIA-SEQUENCE:100" in text)
    ids1, k1 = seg_ids(text), key_id(text)
    check("rewrite: ids distinct", len(set(ids1 + [k1])) == 4)
    clk.t += 1.1
    text2 = await s.playlist()
    check("rewrite: same segments same ids", seg_ids(text2) == ids1 and key_id(text2) == k1)
    check("rewrite: second fetch hit upstream", up.hits["/live/media.m3u8"] == 2)

    # 换片：旧的编号不变，新分片拿新编号
    up.seq = 101
    up.body = ("#EXT-X-MAP:URI=\"init.mp4\"\n#EXT-X-STREAM-INF:BANDWIDTH=1\n"
               "#EXT-X-PART:DURATION=1,URI=\"part.ts\"\n# http://evil.example/x.ts\n"
               "#EXTINF:5,\n/abs/2.ts\n#EXTINF:5,\nnew.ts\n")
    clk.t += 1.1
    text3 = await s.playlist()
    ids3 = seg_ids(text3)
    check("rewrite: kept segment keeps id", ids3[0] == ids1[1])
    check("rewrite: new segment new id", ids3[1] not in ids1 + [k1])
    check("rewrite: MAP URI rewritten", re.search(r'#EXT-X-MAP:URI="/tv/R/1/s/\d+"', text3) is not None)
    check("rewrite: STREAM-INF dropped", "STREAM-INF" not in text3)
    check("rewrite: no upstream names at all",
          "init.mp4" not in text3 and "part.ts" not in text3 and "evil" not in text3)

    # 编号表有上限：比最新编号小 200 以上的忘掉
    for i in range(250):
        up.seq = 200 + i
        up.body = f"#EXTINF:5,\nroll{i}.ts\n"
        clk.t += 1.1
        await s.playlist()
    check("rewrite: id map bounded", len(s._ids) <= 202)
    try:
        await s.segment(ids1[0])
        check("rewrite: forgotten id -> KeyError", False)
    except KeyError:
        check("rewrite: forgotten id -> KeyError", True)


async def test_coalesce(up, stream):
    reset(up)
    clk = FakeClock()
    s = stream(clk)
    texts = await asyncio.gather(*(s.playlist() for _ in range(5)))
    check("coalesce: 5 concurrent -> 1 media hit", up.hits["/live/media.m3u8"] == 1)
    check("coalesce: all same text", len(set(texts)) == 1)
    clk.t += 0.5
    await s.playlist()
    check("coalesce: within 1 s cached", up.hits["/live/media.m3u8"] == 1)
    clk.t += 0.6
    await s.playlist()
    check("coalesce: after 1.1 s refetch", up.hits["/live/media.m3u8"] == 2)
    tv_relay.COALESCE_SECONDS, saved = 5.0, tv_relay.COALESCE_SECONDS
    try:
        clk.t += 1.1
        await s.playlist()
        check("coalesce: constant read at use time", up.hits["/live/media.m3u8"] == 2)
    finally:
        tv_relay.COALESCE_SECONDS = saved


async def test_reresolve(up, stream):
    reset(up)
    clk = FakeClock()
    s = stream(clk)
    for _ in range(5):
        await s.playlist()
        clk.t += 1.1
    check("resolve: master fetched once", up.hits["/master.m3u8"] == 1)
    check("resolve: media fetched each time", up.hits["/live/media.m3u8"] == 5)
    up.media_fail = 1
    try:
        await s.playlist()
        check("resolve: 404 raises StreamError", False)
    except StreamError:
        check("resolve: 404 raises StreamError", True)
    clk.t += 0.1
    text = await s.playlist()
    check("resolve: next call re-resolves master", up.hits["/master.m3u8"] == 2)
    check("resolve: and succeeds", len(seg_ids(text)) == 3)
    check("resolve: failure did not set dead", s.dead_reason is None)


async def test_segments(up, stream):
    reset(up)
    s = stream()
    text = await s.playlist()
    ids = seg_ids(text)
    before = up.total()
    (b1, c1), (b2, c2) = await asyncio.gather(s.segment(ids[0]), s.segment(ids[0]))
    check("segment: bytes", b1 == b"SEG1" * 500 and b2 == b1)
    check("segment: content type", c1 == "video/mp2t" and c2 == c1)
    check("segment: concurrent -> one hit", up.hits["/live/a/1.ts"] == 1)
    await s.segment(ids[0])
    check("segment: cached -> no hit", up.hits["/live/a/1.ts"] == 1)
    body, ctype = await s.segment(ids[2])
    check("segment: other host relayed", body == b"SEG3" * 500 and up.hits["/other/3.ts"] == 1)
    kbody, kctype = await s.segment(key_id(text))
    check("segment: key served with upstream type", kbody == b"K" * 16 and kctype == "application/octet-stream")
    check("segment: total hits", up.total() == before + 3)

    before = up.total()
    for n in (99999, -1, max(ids) + 1):
        try:
            await s.segment(n)
            check(f"segment: unknown {n} -> KeyError", False)
        except KeyError:
            check(f"segment: unknown {n} -> KeyError", True)
    other = stream()
    try:
        await other.segment(ids[0])
        check("segment: id from another stream -> KeyError", False)
    except KeyError:
        check("segment: id from another stream -> KeyError", True)
    check("segment: unknown ids make zero upstream requests", up.total() == before)

    # 共享的失败：每个等待者都拿到错误，而且不缓存
    up.body = "#EXTINF:5,\nbad1.ts\n"
    s2 = stream()
    n = seg_ids(await s2.playlist())[0]
    res = await asyncio.gather(s2.segment(n), s2.segment(n), return_exceptions=True)
    check("segment: shared failure reaches every waiter",
          all(isinstance(r, StreamError) for r in res) and up.hits["/live/bad1.ts"] == 1)
    try:
        await s2.segment(n)
    except StreamError:
        pass
    check("segment: failure not cached", up.hits["/live/bad1.ts"] == 2)

    # LRU：SEGMENT_CACHE 读的是模块常量
    saved = tv_relay.SEGMENT_CACHE
    tv_relay.SEGMENT_CACHE = 1
    try:
        reset(up)
        s3 = stream()
        ids = seg_ids(await s3.playlist())
        await s3.segment(ids[0])
        await s3.segment(ids[1])
        await s3.segment(ids[0])
        check("segment: LRU evicts", up.hits["/live/a/1.ts"] == 2)
    finally:
        tv_relay.SEGMENT_CACHE = saved


async def test_cap(up, stream):
    reset(up)
    up.body = "#EXTINF:5,\nbig.ts\n#EXTINF:5,\na/1.ts\n"
    s = stream()
    ids = seg_ids(await s.playlist())
    try:
        await s.segment(ids[0])
        check("cap: 21 MB segment -> StreamError", False)
    except StreamError:
        check("cap: 21 MB segment -> StreamError", True)
    saved = tv_relay.MAX_SEGMENT_BYTES
    tv_relay.MAX_SEGMENT_BYTES = 100
    try:
        try:
            await s.segment(ids[1])
            check("cap: monkeypatched cap applies", False)
        except StreamError:
            check("cap: monkeypatched cap applies", True)
    finally:
        tv_relay.MAX_SEGMENT_BYTES = saved


async def test_dead(up, stream):
    # 序号 10 s 不动 -> stalled
    reset(up)
    clk = FakeClock()
    s = stream(clk)
    await s.playlist()
    clk.t = 1005
    await s.playlist()
    check("dead: 5 s stalled -> None", s.dead_reason is None)
    clk.t = 1009
    await s.playlist()
    check("dead: 9 s stalled -> None", s.dead_reason is None)
    clk.t = 1010
    await s.playlist()
    check("dead: 10 s stalled -> stalled", s.dead_reason == "stalled")

    # 序号在走 -> 不算死
    reset(up)
    clk = FakeClock()
    s = stream(clk)
    for _ in range(10):
        up.seq += 1
        await s.playlist()
        clk.t += 3
    check("dead: advancing sequence -> None", s.dead_reason is None)

    # DEAD_SECONDS 在用的时候读
    saved = tv_relay.DEAD_SECONDS
    tv_relay.DEAD_SECONDS = 2.0
    try:
        reset(up)
        clk = FakeClock()
        s = stream(clk)
        await s.playlist()
        clk.t += 2
        await s.playlist()
        check("dead: DEAD_SECONDS read at use time", s.dead_reason == "stalled")
    finally:
        tv_relay.DEAD_SECONDS = saved

    # 列表连续失败 10 s（从第一次失败算）-> playlist
    reset(up)
    clk = FakeClock()
    s = stream(clk)
    await s.playlist()
    up.media_status = 500

    async def fail_at(t):
        clk.t = t
        try:
            await s.playlist()
            return False
        except StreamError:
            return True

    check("dead: 500 raises", await fail_at(1002))
    check("dead: 500 for 5 s -> None", await fail_at(1007) and s.dead_reason is None)
    check("dead: 500 9.9 s after first failure -> None", await fail_at(1011.9) and s.dead_reason is None)
    check("dead: 500 for 10 s -> playlist", await fail_at(1012) and s.dead_reason == "playlist")
    up.media_status = None
    clk.t += 1.1
    await s.playlist()
    check("dead: reason never cleared", s.dead_reason == "playlist")

    # 列表失败后又成功，计时重来
    reset(up)
    clk = FakeClock()
    s = stream(clk)
    up.media_status = 500
    for t in (1000, 1006):
        clk.t = t
        try:
            await s.playlist()
        except StreamError:
            pass
    up.media_status = None
    up.seq += 1
    clk.t = 1008
    await s.playlist()
    up.media_status = 500
    clk.t = 1013
    try:
        await s.playlist()
    except StreamError:
        pass
    check("dead: success resets playlist timer", s.dead_reason is None)

    # 分片连续失败 3 次 -> segments
    reset(up)
    up.body = "".join(f"#EXTINF:5,\nbad{i}.ts\n" for i in range(4)) + "#EXTINF:5,\na/1.ts\n"
    s = stream()
    ids = seg_ids(await s.playlist())
    bad, good = ids[:4], ids[4]

    async def seg(n):
        try:
            await s.segment(n)
            return True
        except StreamError:
            return False

    for n in bad[:2]:
        await seg(n)
    check("dead: 2 segment failures -> None", s.dead_reason is None)
    await seg(bad[2])
    check("dead: 3 segment failures -> segments", s.dead_reason == "segments")

    # fail, ok, fail, fail -> None
    s = stream()
    ids = seg_ids(await s.playlist())
    bad, good = ids[:4], ids[4]
    ok = [await seg(bad[0]), await seg(good), await seg(bad[1]), await seg(bad[2])]
    check("dead: fail ok fail fail outcomes", ok == [False, True, False, False])
    check("dead: fail ok fail fail -> None", s.dead_reason is None)


if __name__ == "__main__":
    asyncio.run(run())
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
