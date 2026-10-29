"""一起看电视直播：选清晰度、识别 H.265、探测直播源是否可播 测试。

运行：python tests/test_tv_stream.py
不联网：用本地 aiohttp 假服务器，ip_allowed 只放行 127.0.0.1。
"""
import asyncio
import os
import sys

from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import tv_net  # noqa: E402
import tv_stream as ts  # noqa: E402
from tv_channels import Candidate  # noqa: E402

results = []
ALLOW = lambda ip: ip == "127.0.0.1"  # noqa: E731


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def make_ts(stream_type):
    """一个 PAT 包 + 一个 PMT 包（PMT PID 0x1000），各补满 188 字节。"""
    pat_body = bytes([0x00, 0xB0, 0x0D, 0x00, 0x01, 0xC1, 0x00, 0x00,
                      0x00, 0x01, 0xF0, 0x00])  # table_id.. program 1 -> PID 0x1000
    pat_body += b"\x00\x00\x00\x00"  # CRC（解析不校验）
    pat = bytes([0x47, 0x40, 0x00, 0x10, 0x00]) + pat_body
    pat += b"\xff" * (188 - len(pat))
    pmt_body = bytes([0x02, 0xB0, 0x12, 0x00, 0x01, 0xC1, 0x00, 0x00,
                      0xE1, 0x00, 0xF0, 0x00,
                      stream_type, 0xE1, 0x01, 0xF0, 0x00])
    pmt_body += b"\x00\x00\x00\x00"
    pmt = bytes([0x47, 0x50, 0x00, 0x10, 0x00]) + pmt_body
    pmt += b"\xff" * (188 - len(pmt))
    return pat + pmt


def pad(data, n):
    return data + b"\xff" * (n - len(data))


MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2000000,CODECS="avc1.64001f,mp4a.40.2"
a.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=6000000,CODECS="avc1.640028"
b.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=12000000,CODECS="avc1.640033"
c.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=7000000,CODECS="hvc1.1.6.L120"
d.m3u8
"""
BASE = "http://h/live/x.m3u8"


def test_variants():
    vs = ts.parse_master(MASTER, BASE)
    check("master 4 variants", vs is not None and len(vs) == 4)
    best = ts.choose_variant(vs)
    check("choose 6 Mbps h264", best is not None and best.url == "http://h/live/b.m3u8")
    check("variant bandwidth/codecs", vs[0].bandwidth == 2000000 and vs[0].codecs == "avc1.64001f,mp4a.40.2")
    only_hevc = ts.parse_master("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS=\"hev1.1\"\nx.m3u8\n", BASE)
    check("only hevc -> None", ts.choose_variant(only_hevc) is None)
    nobw = ts.parse_master("#EXTM3U\n#EXT-X-STREAM-INF:PROGRAM-ID=1\nx.m3u8\n#EXT-X-STREAM-INF:PROGRAM-ID=1\ny.m3u8\n", BASE)
    check("no bandwidth -> first", ts.choose_variant(nobw).url == "http://h/live/x.m3u8")
    big = ts.parse_master("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=12000000\nx.m3u8\n", BASE)
    check("only 12 Mbps -> None", ts.choose_variant(big) is None)
    check("media playlist -> None", ts.parse_master("#EXTM3U\n#EXTINF:5,\n1.ts\n", BASE) is None)
    check("empty list -> None", ts.choose_variant([]) is None)


def test_segments():
    text = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:6414426\n#EXTINF:5,\n1.ts\n#EXTINF:5,\n/seg/1.ts\n" \
           "#EXTINF:5,\n//cdn/x.ts\n#EXTINF:5,\nhttps://o/2.ts\n"
    check("segment_urls", ts.segment_urls(text, "http://h/a/b.m3u8")
          == ["http://h/a/1.ts", "http://h/seg/1.ts", "http://cdn/x.ts", "https://o/2.ts"])
    check("media_sequence", ts.media_sequence("#EXT-X-MEDIA-SEQUENCE:6414426") == 6414426)
    check("media_sequence missing", ts.media_sequence("#EXTM3U\n") is None)
    check("absolutize //", ts.absolutize("//cdn/x.ts", "https://h/a.m3u8") == "https://cdn/x.ts")


def test_hevc():
    check("h264 ts", ts.ts_has_hevc(make_ts(0x1B)) is False)
    check("hevc ts", ts.ts_has_hevc(make_ts(0x24)) is True)
    check("hevc ts with trailing data", ts.ts_has_hevc(pad(make_ts(0x24), 5000)) is True)
    check("zeros", ts.ts_has_hevc(b"\x00" * 2000) is False)
    check("empty", ts.ts_has_hevc(b"") is False)


async def test_probe():
    seg = {"ok": pad(make_ts(0x1B), 5000), "small": pad(make_ts(0x1B), 500),
           "hevc": pad(make_ts(0x24), 5000)}
    expect_ua = {"v": None}

    @web.middleware
    async def ua_check(request, handler):
        if expect_ua["v"] and request.headers.get("User-Agent") != expect_ua["v"]:
            return web.Response(status=403)
        return await handler(request)

    def media(kind):
        async def h(request):
            return web.Response(text=f"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:5\n#EXTINF:5,\nold.ts\n"
                                     f"#EXTINF:5,\nseg_{kind}.ts\n")
        return h

    def segment(kind):
        async def h(request):
            return web.Response(body=seg[kind])
        return h

    async def master(request):
        return web.Response(text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000\nmedia_ok.m3u8\n")

    async def html(request):
        return web.Response(text="<html>nope</html>", content_type="text/html")

    async def redir(request):
        raise web.HTTPFound("/master.m3u8")

    async def hang(request):
        await asyncio.sleep(5)
        return web.Response(text="#EXTM3U\n")

    app = web.Application(middlewares=[ua_check])
    app.router.add_get("/master.m3u8", master)
    for k in seg:
        app.router.add_get(f"/media_{k}.m3u8", media(k))
        app.router.add_get(f"/seg_{k}.ts", segment(k))
    app.router.add_get("/html.m3u8", html)
    app.router.add_get("/redir", redir)
    app.router.add_get("/hang.m3u8", hang)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    base = f"http://127.0.0.1:{server.port}"
    session = tv_net.make_session(ip_allowed=ALLOW)

    def cand(path, ua=None):
        return Candidate(url=base + path, ua=ua, list_index=0, is_4k=False)

    try:
        check("master->media->segment ok", await ts.probe(session, cand("/master.m3u8"), ip_allowed=ALLOW) is True)
        check("direct media ok", await ts.probe(session, cand("/media_ok.m3u8"), ip_allowed=ALLOW) is True)
        check("small segment", await ts.probe(session, cand("/media_small.m3u8"), ip_allowed=ALLOW) is False)
        check("hevc segment", await ts.probe(session, cand("/media_hevc.m3u8"), ip_allowed=ALLOW) is False)
        check("html playlist", await ts.probe(session, cand("/html.m3u8"), ip_allowed=ALLOW) is False)
        check("404", await ts.probe(session, cand("/nothing.m3u8"), ip_allowed=ALLOW) is False)
        check("redirect", await ts.probe(session, cand("/redir"), ip_allowed=ALLOW) is True)
        check("blocked address", await ts.probe(session, cand("/master.m3u8")) is False)
        check("timeout", await ts.probe(session, cand("/hang.m3u8"), ip_allowed=ALLOW, timeout=0.5) is False)
        expect_ua["v"] = "AptvPlayer-UA"
        check("wrong ua -> 403 -> False", await ts.probe(session, cand("/master.m3u8", None), ip_allowed=ALLOW) is False)
        check("correct ua", await ts.probe(session, cand("/master.m3u8", "AptvPlayer-UA"), ip_allowed=ALLOW) is True)
        expect_ua["v"] = None

        url, text = await ts.resolve_media(session, base + "/redir", None, ip_allowed=ALLOW)
        check("resolve_media final url", url == base + "/media_ok.m3u8" and "seg_ok.ts" in text)
        try:
            await ts.resolve_media(session, base + "/html.m3u8", None, ip_allowed=ALLOW)
            check("resolve_media non-m3u8 raises", False)
        except ts.StreamError:
            check("resolve_media non-m3u8 raises", True)

        task = asyncio.ensure_future(ts.probe(session, cand("/hang.m3u8"), ip_allowed=ALLOW, timeout=5))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
            check("cancel propagates", False)
        except asyncio.CancelledError:
            check("cancel propagates", True)
    finally:
        await session.close()
        await server.close()


if __name__ == "__main__":
    test_variants()
    test_segments()
    test_hevc()
    asyncio.run(test_probe())
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
