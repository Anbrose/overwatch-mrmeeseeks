"""一起看 采集接口 m3u8 测试：选清晰度、改写地址、去广告、拉取、探测可播。

运行：python tests/test_vod_playlist.py
不联网：用本地 aiohttp 假服务器；allow_http=True 只为连 127.0.0.1。
"""
import asyncio
import os
import sys

from types import SimpleNamespace as NS

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import vod_playlist as vp  # noqa: E402
from vod_playlist import NO_PLAYABLE, PlaylistError  # noqa: E402
from vod_search import Episode, Line, Show  # noqa: E402

results = []
FIX = os.path.join(ROOT, "tests", "fixtures", "watch")
MASTER_BASE = "https://yzzy.play-cdn6.com/20220913/14552_d0e2fb6b/index.m3u8"
VARIANT_BASE = "https://yzzy.play-cdn6.com/20220913/14552_d0e2fb6b/1000k/hls/mixed.m3u8"
VARIANT_DIR = "https://yzzy.play-cdn6.com/20220913/14552_d0e2fb6b/1000k/hls/"


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def load(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def media(blocks, ends=True):
    """blocks: [(每段秒数列表, 地址前缀)]，用 DISCONTINUITY 隔开。"""
    out = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for i, (durs, pre) in enumerate(blocks):
        if i:
            out.append("#EXT-X-DISCONTINUITY")
        for j, d in enumerate(durs):
            out += [f"#EXTINF:{d:.3f},", f"{pre}b{i}s{j}.ts"]
    if ends:
        out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n"


def uris(text):
    return [ln for ln in text.splitlines() if ln and not ln.startswith("#")]


def test_pick_variant():
    check("pick_variant 夹具", vp.pick_variant(load("vod_master.m3u8"), MASTER_BASE)
          == "https://yzzy.play-cdn6.com/20220913/14552_d0e2fb6b/1000k/hls/mixed.m3u8")
    m = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nlow.m3u8\n"
         "#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=2000000\nhigh.m3u8\n")
    check("pick_variant 取最高码率", vp.pick_variant(m, "https://h.example.com/a/i.m3u8") == "https://h.example.com/a/high.m3u8")
    check("pick_variant 媒体列表返回 None", vp.pick_variant(load("vod_variant_ads.m3u8"), VARIANT_BASE) is None)


def test_rewrite_ads():
    text = load("vod_variant_ads.m3u8")
    pl = vp.rewrite_media(text, VARIANT_BASE)
    check("去掉 4 个广告块", pl.removed_blocks == 4)
    check("时长扣掉广告", abs(pl.duration - 3486.48) < 0.5)
    check("广告片段不在", not any(ln.endswith("9abd76c66643cf6bbfbda0a81923d523.ts") for ln in pl.text.splitlines()))
    check("地址全是绝对", all(u.startswith(VARIANT_DIR) for u in uris(pl.text)))
    check("保留头尾", pl.text.startswith("#EXTM3U") and ("#EXT-X-ENDLIST" in pl.text) == ("#EXT-X-ENDLIST" in text))
    check("first_segment", pl.first_segment and pl.first_segment.endswith("527ede660a2a7ccd1187bf31ca80a15e.ts"))


def test_rewrite_keeps_unsure():
    base = "https://h.example.com/p/index.m3u8"
    long_ = [10.0] * 10
    p1 = vp.rewrite_media(media([(long_, ""), ([5.0] * 3, ""), (long_, "")]), base)
    check("同目录只出现一次的短块不删", p1.removed_blocks == 0)
    p2 = vp.rewrite_media(media([(long_, ""), ([5.0] * 3, "https://ads.example.com/"), (long_, "")]), base)
    check("换了域名的短块删掉", p2.removed_blocks == 1 and "ads.example.com" not in p2.text)
    check("删块后时长", abs(p2.duration - 200.0) < 0.01)
    p3 = vp.rewrite_media(media([(long_ * 3, ""), ([10.0] * 4, ""), ([10.0] * 4, "")]), base)
    check("超过 30 秒的重复块不删", p3.removed_blocks == 0)
    p4 = vp.rewrite_media(media([(long_ * 3, ""), ([5.0] * 3, ""), ([5.0] * 3, "")]), base)
    check("重复短块删掉", p4.removed_blocks == 2)


def test_discontinuity_sequence():
    text = ("#EXTM3U\n#EXT-X-DISCONTINUITY-SEQUENCE:3\n" + "#EXTINF:5,\na.ts\n" * 3
            + "#EXT-X-DISCONTINUITY\n" + "#EXTINF:100,\nb.ts\n")
    pl = vp.rewrite_media(text, "https://h.example.com/i.m3u8")
    check("DISCONTINUITY-SEQUENCE 不当作分块", pl.removed_blocks == 0 and "DISCONTINUITY-SEQUENCE:3" in pl.text)


def test_rewrite_uris():
    text = ("#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI=\"key.key\"\n#EXT-X-MAP:URI=\"init.mp4\"\n"
            "#EXTINF:5,\na.ts\n#EXTINF:5,\n/root/b.ts\n#EXTINF:5,\n//cdn.example.com/c.ts\n"
            "#EXTINF:5,\nhttps://x.example.com/d.ts?t=1\n#EXT-X-ENDLIST\n")
    out = vp.rewrite_media(text, "https://h.example.com/p/q/index.m3u8").text
    check("相对 / 根 / 协议相对 / 带参数",
          uris(out) == ["https://h.example.com/p/q/a.ts", "https://h.example.com/root/b.ts",
                        "https://cdn.example.com/c.ts", "https://x.example.com/d.ts?t=1"])
    check("KEY URI", 'URI="https://h.example.com/p/q/key.key"' in out)
    check("MAP URI", 'URI="https://h.example.com/p/q/init.mp4"' in out)
    check("METHOD 保留", "#EXT-X-KEY:METHOD=AES-128," in out)
    try:
        vp.rewrite_media("#EXTM3U\n#EXTINF:5,\nhttp://h.example.com/a.ts\n", "https://h.example.com/i.m3u8")
        check("http 片段被拒", False)
    except PlaylistError as e:
        check("http 片段被拒", e.message == NO_PLAYABLE)
    try:
        vp.rewrite_media("#EXTM3U\n#EXT-X-ENDLIST\n", "https://h.example.com/i.m3u8")
        check("没有片段被拒", False)
    except PlaylistError:
        check("没有片段被拒", True)


def test_rewrite_tag_between_extinf_and_uri():
    text = ("#EXTM3U\n#EXTINF:10,\n#EXT-X-KEY:METHOD=AES-128,URI=\"k/key.key\"\na.ts\n"
            "#EXTINF:10,\n#EXT-X-BYTERANGE:100@0\nb.ts\n#EXT-X-ENDLIST\n")
    out = vp.rewrite_media(text, "https://h.example.com/p/index.m3u8").text
    check("EXTINF 和地址之间的 KEY URI 也绝对化", 'URI="https://h.example.com/p/k/key.key"' in out
          and 'URI="k/key.key"' not in out and "#EXT-X-BYTERANGE:100@0" in out)
    try:
        vp.rewrite_media("#EXTM3U\n#EXTINF:10,\n#EXT-X-KEY:METHOD=AES-128,URI=\"http://h.example.com/k.key\"\na.ts\n",
                         "https://h.example.com/p/index.m3u8")
        check("EXTINF 和地址之间的 http KEY 被拒", False)
    except PlaylistError as e:
        check("EXTINF 和地址之间的 http KEY 被拒", e.message == NO_PLAYABLE)


def test_rewrite_zero_duration():
    for name, text in [("EXTINF 全是 0", "#EXTM3U\n#EXTINF:0,\na.ts\n#EXTINF:0,\nb.ts\n#EXT-X-ENDLIST\n"),
                       ("没有 EXTINF", "#EXTM3U\na.ts\nb.ts\n#EXT-X-ENDLIST\n")]:
        try:
            vp.rewrite_media(text, "https://h.example.com/p/index.m3u8")
            check(name + " → 不能播", False)
        except PlaylistError as e:
            check(name + " → 不能播", e.message == NO_PLAYABLE)


class _FakeResp:
    def __init__(self, url, history=(), status=200, body=b"#EXTM3U\n#EXTINF:5,\na.ts\n", headers=None):
        from yarl import URL
        self.url = URL(url)
        self.history = tuple(NS(url=URL(h)) for h in history)   # 跳转链上之前每一跳的地址
        self.status = status
        self.content_length = len(body)
        self.headers = headers or {"Access-Control-Allow-Origin": "*"}
        self._body = body
        resp = self

        class _Content:
            async def iter_chunked(self, n):
                yield resp._body

            async def read(self, n=-1):
                return resp._body
        self.content = _Content()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """session.get 直接返回预设的“跳转后”响应。"""

    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.resp


async def test_redirect_checks():
    start = "https://h.example.com/p/index.m3u8"
    for name, resp in [("m3u8 跳到 http → 报错", _FakeResp("http://evil.example.com/x.m3u8", [start])),
                       ("中间一跳是 http → 报错", _FakeResp("https://h2.example.com/x.m3u8",
                                                     [start, "http://mid.example.com/x.m3u8"]))]:
        try:
            await vp.fetch_playlist(_FakeSession(resp), start)
            check(name, False)
        except PlaylistError as e:
            check(name, e.message == NO_PLAYABLE)
    ok = await vp.fetch_playlist(_FakeSession(_FakeResp("https://h2.example.com/q/x.m3u8", [start])), start)
    check("全程 https 的跳转可以，按最终地址解析", uris(ok.text) == ["https://h2.example.com/q/a.ts"])
    seg = vp.Playlist("", 5.0, "https://h.example.com/p/a.ts", 0)
    origin = "https://watch.example.com"
    check("片段跳到 http → probe 给出原因",
          await vp.probe(_FakeSession(_FakeResp("http://evil.example.com/a.ts", [seg.first_segment], 206)), seg, origin)
          == "地址不是 https：evil.example.com")
    check("片段全程 https → probe 能播",
          await vp.probe(_FakeSession(_FakeResp("https://cdn.example.com/a.ts", [seg.first_segment], 206)), seg, origin)
          is None)


async def serve(routes):
    app = web.Application()
    for path, h in routes.items():
        app.router.add_get(path, h)
    srv = TestServer(app)
    await srv.start_server()
    return srv


def text_handler(body, status=200, headers=None):
    async def h(request):
        return web.Response(text=body, status=status, headers=headers)
    return h


def playlist_of(n):
    return "#EXTM3U\n" + "#EXTINF:5,\nseg.ts\n" * n + "#EXT-X-ENDLIST\n"


def chunked_handler(body):
    """不带 Content-Length（分块传输），逼着走流式上限。"""
    async def h(request):
        resp = web.StreamResponse()
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        data = body.encode()
        for i in range(0, len(data), 8192):
            await resp.write(data[i:i + 8192])
        await resp.write_eof()
        return resp
    return h


def redirect_handler(to):
    async def h(request):
        raise web.HTTPFound(to)
    return h


async def test_fetch_playlist():
    srv = await serve({
        "/m/index.m3u8": text_handler(load("vod_master.m3u8")),
        "/m/1000k/hls/mixed.m3u8": text_handler(load("vod_variant_ads.m3u8")),
        "/forbidden": text_handler("<html>403</html>", 403),
        "/html": text_handler("<html>hi</html>"),
        "/big": chunked_handler(playlist_of(vp.MAX_PLAYLIST_BYTES // 14 + 1000)),
        "/medium": chunked_handler(playlist_of(30000)),
        "/redir/index.m3u8": redirect_handler("/m/index.m3u8"),
        "/bad/index.m3u8": text_handler(load("vod_master.m3u8")),
    })
    base = f"http://127.0.0.1:{srv.port}"
    async with aiohttp.ClientSession() as s:
        pl = await vp.fetch_playlist(s, f"{base}/m/index.m3u8", allow_http=True)
        check("master → 变体，时长", abs(pl.duration - 3486.48) < 0.5)
        check("地址指向测试服务器", all(u.startswith(f"{base}/m/1000k/hls/") for u in uris(pl.text)))
        check("大小测试用的列表确实合法且超限", len(playlist_of(vp.MAX_PLAYLIST_BYTES // 14 + 1000)) > vp.MAX_PLAYLIST_BYTES
              and len(uris(vp.rewrite_media(playlist_of(100), base + "/x.m3u8", True).text)) == 100)
        med = await vp.fetch_playlist(s, f"{base}/medium", allow_http=True)
        check("几百 KB 的列表完整返回", len(uris(med.text)) == 30000 and abs(med.duration - 150000) < 0.01)
        redir = await vp.fetch_playlist(s, f"{base}/redir/index.m3u8", allow_http=True)
        check("按跳转后的地址解析相对路径", all(u.startswith(f"{base}/m/1000k/hls/") for u in uris(redir.text)))
        for name, path in [("403 页面", "/forbidden"), ("非 m3u8", "/html"), ("太大", "/big"), ("变体 404", "/bad/index.m3u8")]:
            try:
                await vp.fetch_playlist(s, base + path, allow_http=True)
                check(name + " 报错", False)
            except PlaylistError as e:
                check(name + " 报错", bool(e.message))
        try:
            await vp.fetch_playlist(s, f"{base}/m/index.m3u8")
            check("默认拒绝 http 地址", False)
        except PlaylistError:
            check("默认拒绝 http 地址", True)
    await srv.close()


async def test_probe():
    seen = {}

    def seg(status, acao=None):
        async def h(request):
            seen["range"] = request.headers.get("Range")
            seen["origin"] = request.headers.get("Origin")
            return web.Response(body=b"x", status=status, headers={"Access-Control-Allow-Origin": acao} if acao else {})
        return h
    srv = await serve({"/star": seg(206, "*"), "/none": seg(206), "/403": seg(403, "*"),
                       "/origin": seg(200, "https://watch.example.com"), "/other": seg(206, "https://other.example.com")})
    base = f"http://127.0.0.1:{srv.port}"
    origin = "https://watch.example.com"
    async with aiohttp.ClientSession() as s:
        def pl(path):
            return vp.Playlist("", 1.0, base + path, 0)
        check("206 + ACAO * → 能播（None）", await vp.probe(s, pl("/star"), origin, allow_http=True) is None)
        check("发了 Range 和 Origin", seen["range"] == "bytes=0-1023" and seen["origin"] == origin)
        r = await vp.probe(s, pl("/none"), origin, allow_http=True)
        check("无 ACAO → 原因说明跨域", r == f"片段不允许跨域（CORS 无，127.0.0.1:{srv.port}）")
        r = await vp.probe(s, pl("/403"), origin, allow_http=True)
        check("403 → 原因带状态码和域名", r == f"片段 HTTP 403（127.0.0.1:{srv.port}）")
        check("ACAO = origin → 能播", await vp.probe(s, pl("/origin"), origin, allow_http=True) is None)
        r = await vp.probe(s, pl("/other"), origin, allow_http=True)
        check("ACAO 是别人 → 原因带对方的 ACAO", r and r.startswith("片段不允许跨域（CORS https://other.example.com"))
        check("没有片段 → 原因", await vp.probe(s, vp.Playlist("", 0.0, None, 0), origin, allow_http=True) == "m3u8 里没有片段")
        dead = vp.Playlist("", 1.0, "http://127.0.0.1:1/x.ts", 0)
        r = await vp.probe(s, dead, origin, allow_http=True)
        check("连不上 → 原因、不抛", r and r.startswith("片段取不到：") and "127.0.0.1:1" in r)
        evil = vp.Playlist("", 1.0, "http://evil.example.com/a.ts", 0)
        check("非 https 片段 → 原因", await vp.probe(s, evil, origin) == "地址不是 https：evil.example.com")
    await srv.close()


async def test_find_playable():
    good = media([([10.0] * 3, "")])
    srv = await serve({
        "/l0/index.m3u8": text_handler(good),
        "/l0/b0s0.ts": text_handler("x", 403),
        "/l1/index.m3u8": text_handler("nope", 404),
        "/l2/index.m3u8": text_handler(good),
        "/l2/b0s0.ts": text_handler("x", 206, {"Access-Control-Allow-Origin": "*"}),
    })
    base = f"http://127.0.0.1:{srv.port}"
    origin = "https://watch.example.com"

    def line(name, d, n):
        return Line(name, [Episode(str(i + 1), f"{base}/{d}/index.m3u8") for i in range(n)])
    show = Show("k", "片", 2020, "剧", [line("甲", "l0", 2), line("乙", "l1", 3), line("丙", "l2", 3)])
    async with aiohttp.ClientSession() as s:
        idx, pl = await vp.find_playable(s, show, 2, 0, origin, allow_http=True)
        check("甲线没有第 3 集，乙线 404，丙线可播", idx == 2 and pl.first_segment.endswith("/l2/b0s0.ts"))
        idx, _ = await vp.find_playable(s, show, 0, 2, origin, allow_http=True)
        check("先试 start_line", idx == 2)
        idx, _ = await vp.find_playable(s, show, 0, 1, origin, allow_http=True)
        check("回绕一圈（乙 404 → 丙）", idx == 2)
        show.lines[2].episodes = []
        try:
            await vp.find_playable(s, show, 0, 0, origin, allow_http=True)
            check("全不可播报错", False)
        except PlaylistError as e:
            check("全不可播报错", e.message == "这一集暂时没有能播的片源")
            check("原因按线路列出，线路只写序号", e.detail == f"来源1：片段 HTTP 403（127.0.0.1:{srv.port}）；"
                  f"来源2：m3u8 HTTP 404（127.0.0.1:{srv.port}）；来源3：没有第 1 集"
                  and "甲" not in e.detail)
            check("text = message + 括号里的原因", e.text == f"这一集暂时没有能播的片源（{e.detail}）")
        check("没有 detail 时 text 就是 message", PlaylistError("x").text == "x")
        long = vp.join_reasons(["甲：" + "很长" * 300])
        check("原因过长会截断", len(long) == vp.DETAIL_MAX_CHARS and long.endswith("…"))
        check("没有线路", vp.join_reasons([]) == "没有片源线路")
    await srv.close()


if __name__ == "__main__":
    test_pick_variant()
    test_rewrite_ads()
    test_rewrite_keeps_unsure()
    test_discontinuity_sequence()
    test_rewrite_uris()
    test_rewrite_tag_between_extinf_and_uri()
    test_rewrite_zero_duration()
    asyncio.run(test_redirect_checks())
    asyncio.run(test_fetch_playlist())
    asyncio.run(test_probe())
    asyncio.run(test_find_playable())
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
