"""一起看：链接解析与视频来源测试。

运行：python tests/test_watch_sources.py
不联网：HTTP 用假的 session。
"""
import asyncio
import hashlib
import json
import os
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import watch_sources as ws  # noqa: E402
from watch_sources import Link, VideoUnavailable  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def load(name):
    with open(os.path.join(ROOT, "tests", "fixtures", "watch", name), encoding="utf-8") as f:
        return json.load(f)


def raises_msg(exc, msg, fn):
    try:
        fn()
    except exc as e:
        return getattr(e, "message", str(e)) == msg
    return False


def test_find_link():
    check("BV", ws.find_link("一起看 https://www.bilibili.com/video/BV1xx411c7mD") == Link("bilibili", "BV1xx411c7mD"))
    check("分P", ws.find_link("bilibili.com/video/BV1xx411c7mD?p=3&t=10").page == 3)
    check("短链", ws.find_link("这个超好笑 https://b23.tv/abc123 哈哈") == Link("bilibili", "abc123", short=True))
    check("youtu.be", ws.find_link("https://youtu.be/dQw4w9WgXcQ?si=x") == Link("youtube", "dQw4w9WgXcQ"))
    check("watch", ws.find_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30").id == "dQw4w9WgXcQ")
    check("shorts", ws.find_link("https://youtube.com/shorts/dQw4w9WgXcQ").id == "dQw4w9WgXcQ")
    check("其他网站拒绝", ws.find_link("https://evil.example.com/video/BV1xx411c7mD") is None)
    check("假域名拒绝", ws.find_link("https://evilbilibili.com/video/BV1xx411c7mD") is None)
    check("参数里的域名拒绝", ws.find_link("https://evil.com/?u=bilibili.com/video/BV1xx411c7mD") is None)
    check("没有链接", ws.find_link("一起看") is None)
    check("取第一个", ws.find_link("https://youtu.be/dQw4w9WgXcQ https://b23.tv/x1").source == "youtube")
    check("中文紧贴链接", ws.find_link("看这个https://b23.tv/abc123").id == "abc123")


def test_wbi_sign():
    tab = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39,
           12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63,
           57, 62, 11, 36, 20, 34, 44, 52]
    img, sub = "7cd084941338484aae1ad9425b84077c", "4932caff0ff746eab6f01bf08b70ac45"
    raw = img + sub
    mixin = "".join(raw[i] for i in tab)[:32]
    check("mixin key", ws.mixin_key(img, sub) == mixin)
    now = 1700000000
    params = {"foo": "114", "bar": "514", "zab": 1919810, "x": "a!b'c(d)e*f"}
    q = ws.wbi_sign(params, mixin, now)
    base = "bar=514&foo=114&wts=1700000000&x=abcdef&zab=1919810"
    expect = base + "&w_rid=" + hashlib.md5((base + mixin).encode()).hexdigest()
    check("wbi 签名", q == expect)
    check("不改动入参", "wts" not in params and params["x"] == "a!b'c(d)e*f")
    q2 = ws.wbi_sign({"t": "中 文"}, mixin, now)
    check("中文被编码", "%E4%B8%AD" in q2 and q2.startswith("t="))


def test_parse_fixtures():
    title, cid, dur = ws.parse_view(load("bili_view.json"), 1)
    check("view", title == "折1万次，会坏吗？iPhone Duo深度评测" and cid == 42926145926 and dur == 1492)
    check("分P不存在", raises_msg(VideoUnavailable, "这个视频没有第 5 P", lambda: ws.parse_view(load("bili_view.json"), 5)))
    s = ws.parse_playurl(load("bili_playurl_dash.json"), 80, now=0)
    check("1080p AVC", s.height == 1080 and s.video_codecs.startswith("avc1"))
    check("max_qn=80 跳过 116 档", s.video_bandwidth > 0 and "-30080.m4s" in s.video_url)
    check("画质上限 720", ws.parse_playurl(load("bili_playurl_dash.json"), 64, 0).height <= 720)
    check("画质上限 480", ws.parse_playurl(load("bili_playurl_dash.json"), 32, 0).height <= 480)
    check("有 SegmentBase", s.video_init == "0-931" and s.video_index == "932-4595" and s.audio_init and s.audio_index)
    check("音频取最高码率", s.audio_bandwidth == 188461 and s.audio_codecs == "mp4a.40.2")
    check("备用地址", s.video_backup and s.audio_backup)
    check("deadline", s.expires_at == 1792800000)
    check("无 deadline 用 now+1800", ws.parse_playurl(_strip_deadline(load("bili_playurl_dash.json")), 80, 100).expires_at == 1900)
    check("时长", s.duration == 1492)
    check("view 不存在", raises_msg(VideoUnavailable, "这个视频在海外服务器上无法播放",
                                    lambda: ws.parse_view(load("bili_view_missing.json"), 1)))
    check("playurl 错误码", raises_msg(VideoUnavailable, "这个视频在海外服务器上无法播放",
                                       lambda: ws.parse_playurl({"code": -10403, "message": "x"}, 80, 0)))


def _strip_deadline(resp):
    return json.loads(json.dumps(resp).replace("?deadline=1792800000&x=1", ""))


# ---------------- 假 session ----------------
class FakeResp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status, self._payload, self.headers = status, payload, headers or {}

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        for key, val in self.routes:
            if key in url:
                if callable(val):
                    val = val(url)
                return val
        return FakeResp(404, {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


NAV = {"code": -101, "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
                                           "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"}}}
SPI = {"code": 0, "data": {"b_3": "B3-xxx", "b_4": "B4-yyy"}}


def bili_routes(**extra):
    r = [("finger/spi", FakeResp(200, SPI)), ("web-interface/nav", FakeResp(200, NAV)),
         ("wbi/view", FakeResp(200, load("bili_view.json"))),
         ("wbi/playurl", FakeResp(200, load("bili_playurl_dash.json")))]
    return list(extra.items()) + r if extra else r


def test_resolve():
    async def run():
        fs = FakeSession([("b23.tv/abc123", FakeResp(302, headers={"Location": "https://www.bilibili.com/video/BV1xrnz6gEVD?p=1&share=x"}))]
                         + bili_routes())
        src = ws.Sources("SECRET-SESS", session_factory=lambda: fs)
        v = await src.resolve("看 https://b23.tv/abc123")
        check("短链展开", v.source == "bilibili" and v.id == "BV1xrnz6gEVD" and v.page == 1)
        check("标题时长", v.title.startswith("折1万次") and v.duration == 1492)
        check("url", v.url == "https://www.bilibili.com/video/BV1xrnz6gEVD?p=1")
        view_calls = [c for c in fs.calls if "wbi/view" in c[0]]
        check("view 带签名", "w_rid=" in view_calls[0][0] and "wts=" in view_calls[0][0])
        h = view_calls[0][1]["headers"]
        check("头和 cookie", h["Referer"] == "https://www.bilibili.com/" and h["Origin"] == "https://www.bilibili.com"
              and "buvid3=B3-xxx" in h["Cookie"] and "SESSDATA=SECRET-SESS" in h["Cookie"])
        spi_calls = [c for c in fs.calls if "finger/spi" in c[0]]
        s = await src.bili_streams(v)
        s2 = await src.bili_streams(v)
        check("streams", s.height == 1080 and s2.height == 1080)
        pu = [c for c in fs.calls if "wbi/playurl" in c[0]]
        q = urllib.parse.parse_qs(urllib.parse.urlparse(pu[0][0]).query)
        check("playurl 参数", q["cid"] == ["42926145926"] and q["qn"] == ["80"] and q["fnval"] == ["16"] and q["fourk"] == ["0"])
        check("每次重新请求播放地址", len(pu) == 2)
        check("buvid/nav 只取一次", len([c for c in fs.calls if "finger/spi" in c[0]]) == len(spi_calls) == 1
              and len([c for c in fs.calls if "web-interface/nav" in c[0]]) == 1)

        fs2 = FakeSession(bili_routes())
        v2 = await ws.Sources(None, session_factory=lambda: fs2).resolve("https://www.bilibili.com/video/BV1xrnz6gEVD?p=1")
        check("无 SESSDATA 不带", "SESSDATA" not in [c for c in fs2.calls if "wbi/view" in c[0]][0][1]["headers"]["Cookie"])

        fs3 = FakeSession([("youtube.com/oembed", FakeResp(200, {"title": "Never Gonna"}))])
        v3 = await ws.Sources(None, session_factory=lambda: fs3).resolve("https://youtu.be/dQw4w9WgXcQ")
        check("youtube", v3.source == "youtube" and v3.title == "Never Gonna" and v3.duration is None
              and v3.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

        fs4 = FakeSession([("youtube.com/oembed", FakeResp(401, {}))])
        try:
            await ws.Sources(None, session_factory=lambda: fs4).resolve("https://youtu.be/dQw4w9WgXcQ")
            check("youtube 禁嵌入", False)
        except VideoUnavailable as e:
            check("youtube 禁嵌入", e.message == "这个 YouTube 视频不允许嵌入播放")

        try:
            await ws.Sources(None, session_factory=lambda: fs4).resolve("hello")
            check("非法链接", False)
        except VideoUnavailable:
            check("非法链接", True)

        # -352 触发刷新 key 并重试一次
        n = {"c": 0}

        def view(url):
            n["c"] += 1
            return FakeResp(200, {"code": -352, "message": "x"} if n["c"] == 1 else load("bili_view.json"))
        fs5 = FakeSession([("wbi/view", view)] + bili_routes())
        v5 = await ws.Sources(None, session_factory=lambda: fs5).resolve("https://www.bilibili.com/video/BV1xrnz6gEVD")
        check("风控重试", v5.title.startswith("折1万次") and n["c"] == 2
              and len([c for c in fs5.calls if "web-interface/nav" in c[0]]) == 2)

    asyncio.run(run())


if __name__ == "__main__":
    test_find_link()
    test_wbi_sign()
    test_parse_fixtures()
    test_resolve()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
