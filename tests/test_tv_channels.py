"""一起看电视直播 频道表测试：m3u 解析、名称归一化、别名、匹配、持久化和刷新。

运行：python tests/test_tv_channels.py
不联网：刷新用 127.0.0.1 上的本地 aiohttp 服务器提供 tests/fixtures/tv/ 里的三个列表。
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import tv_channels as tc  # noqa: E402
from tv_channels import Candidate, Channel, ChannelTable, Entry, Match  # noqa: E402
import tv_net  # noqa: E402

FIX = Path(ROOT) / "tests" / "fixtures" / "tv"
NAMES = ["guovin.m3u", "iptvorg_cn.m3u", "myiptv.m3u"]

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def fixture_entries():
    out = []
    for i, n in enumerate(NAMES):
        out += tc.parse_m3u((FIX / n).read_text(encoding="utf-8"), i)
    return out


def cands(*specs):
    return [Candidate(f"http://h/{i}", None, idx, k4, ok) for i, (ok, idx, k4) in enumerate(specs)]


def test_normalize():
    n = tc.normalize_name
    for raw, want in [("CCTV-1 (1080p)", "CCTV1"), ("CCTV1综合", "CCTV1"), ("CCTV5+体育赛事", "CCTV5+"),
                      ("CCTV-4K (1080p)", "CCTV4K"), ("东方卫视4K", "东方卫视"), ("湖南卫视 高清", "湖南卫视"),
                      ("Hunan TV (2160p)", "HUNANTV"), ("CGTN英语", "CGTN英语"), ("CCTV-8K", "CCTV8K")]:
        check(f"normalize {raw!r}", n(raw) == want)
    check("canonical Hunan TV", tc.canonical_key("Hunan TV (2160p)") == "湖南卫视")
    check("canonical Dragon TV International", tc.canonical_key("Dragon TV International") == "东方卫视")
    check("canonical 央视十三套", tc.canonical_key("央视十三套") == "CCTV13")
    check("canonical 中央十七套", tc.canonical_key("中央十七套") == "CCTV17")
    check("canonical 芒果台", tc.canonical_key("芒果台") == "湖南卫视")
    check("parse_sources_env", tc.parse_sources_env(None) == tc.DEFAULT_TV_LISTS.split(",")
          and tc.parse_sources_env("") == [] and tc.parse_sources_env(" a, b ,") == ["a", "b"])


def test_parse():
    ents = tc.parse_m3u((FIX / "myiptv.m3u").read_text(encoding="utf-8"), 2)
    check("fixture parsed", len(ents) > 10)
    check("no rtmp", not any(e.url.lower().startswith("rtmp") for e in ents))
    check("no ipv6 host", not any("[" in e.url for e in ents))
    check("no # urls", not any(e.url.startswith("#") for e in ents))
    check("chinese path kept", any(e.url == "http://example.com/直播/湖南.m3u8" for e in ents))
    text = ("﻿#EXTINF:-1 http-user-agent=\"AptvPlayer-UA\",东方卫视\r\n#EXTVLCOPT:http-referrer=x\r\n"
            "http://a/b.m3u8\r\n#EXTINF:-1,无地址\r\n#EXTINF:-1,湖南卫视\r\nhttp://c/d.m3u8\r\n")
    got = tc.parse_m3u(text, 0)
    check("messy inline: 2 entries", len(got) == 2)
    check("messy inline: first", got[0].name == "东方卫视" and got[0].ua == "AptvPlayer-UA"
          and got[0].url == "http://a/b.m3u8")
    check("messy inline: second", got[1].name == "湖南卫视" and got[1].ua is None
          and got[1].url == "http://c/d.m3u8")
    clean = tc.parse_m3u("#EXTM3U\n#EXTINF:-1,A卫视\n#http://x/commented\nhttp://x/a\n", 0)
    check("commented url line skipped", len(clean) == 1 and clean[0].url == "http://x/a")


def test_build():
    chs = tc.build_channels(fixture_entries())
    check("has 湖南卫视/东方卫视/CCTV1", all(k in chs for k in ("湖南卫视", "东方卫视", "CCTV1")))
    check("东方卫视 has 4K candidate", any(c.is_4k for c in chs["东方卫视"].candidates))
    urls = [c.url for ch in chs.values() for c in ch.candidates]
    check("no duplicate urls", len(urls) == len(set(urls)))
    check("display name of 湖南卫视", chs["湖南卫视"].name == "湖南卫视")
    inline = [Entry("X台", None, None, None, "http://dup/1", 2), Entry("X台", None, None, None, "http://dup/1", 0),
              Entry("X台", None, None, None, "http://dup/2", 1)]
    c2 = tc.build_channels(inline)["X台"]
    check("dup url keeps lowest list_index", [(c.url, c.list_index) for c in c2.candidates]
          == [("http://dup/1", 0), ("http://dup/2", 1)])
    # 真实夹具里同一个地址出现在 guovin(0) 和 myiptv(2) 时取 0
    g = {e.url for e in tc.parse_m3u((FIX / NAMES[0]).read_text(encoding="utf-8"), 0)}
    m = {e.url for e in tc.parse_m3u((FIX / NAMES[2]).read_text(encoding="utf-8"), 2)}
    shared = g & m
    if shared:
        byurl = {c.url: c for ch in chs.values() for c in ch.candidates}
        check("fixture shared url has list_index 0", all(byurl[u].list_index == 0 for u in shared))
    prev = {"http://dup/2": Candidate("http://dup/2", None, 1, False, True, 5.0)}
    c3 = tc.build_channels(inline, prev)["X台"]
    check("previous probe result carried", [c.ok for c in c3.candidates] == [None, True])
    k = tc.build_channels([Entry("CCTV-4K (1080p)", None, None, None, "http://k/1", 0)])
    check("CCTV4K not flagged 4k", k["CCTV4K"].candidates[0].is_4k is False)


def test_ordered():
    ch = Channel("X", "X", cands((False, 0, False), (None, 0, False), (True, 0, True), (True, 2, False)))
    got = [(c.ok, c.is_4k) for c in ch.ordered()]
    check("ordered", got == [(True, False), (True, True), (None, False), (False, False)])
    check("all_failed", Channel("X", "X", cands((False, 0, False))).all_failed
          and not ch.all_failed and not Channel("X", "X", []).all_failed)


def make_table(channels):
    t = ChannelTable(None, [], None)
    t.channels = channels
    return t


def test_match():
    t = make_table(tc.build_channels(fixture_entries()))
    for q in ("湖南卫视", "湖南", "湖南台", "芒果台", "hunan tv"):
        m = t.match(q)
        check(f"exact {q!r}", m.exact is not None and m.exact.key == "湖南卫视")
    m = t.match("东方")
    check("东方 exact 东方卫视", m.exact is not None and m.exact.key == "东方卫视")
    if "东方影视" in t.channels:
        m = t.match("东方影")
        check("东方影 partial 东方影视", m.exact is None and [c.key for c in m.partial] == ["东方影视"])
    else:
        t2 = make_table({k: Channel(k, k, cands((None, 0, False))) for k in ("东方卫视", "东方影视")})
        m = t2.match("东方影")
        check("东方影 partial 东方影视 (inline)", m.exact is None and [c.key for c in m.partial] == ["东方影视"])
    m = t.match("卫视")
    check("卫视 partial", m.exact is None and 0 < len(m.partial) <= 5 and all(c.must_have for c in m.partial))
    check("long query no reverse match", t.match("湖南卫视直播") == Match(None, []))
    check("short query", t.match("湖") == Match(None, []))
    t3 = make_table({"湖南卫视": Channel("湖南卫视", "湖南卫视", cands((False, 0, False))),
                     "湖南经视": Channel("湖南经视", "湖南经视", cands((False, 0, False))),
                     "湖南都市": Channel("湖南都市", "湖南都市", cands((None, 0, False)))})
    m = t3.match("湖南卫视")
    check("all_failed still exact", m.exact is not None and m.exact.key == "湖南卫视")
    m = t3.match("湖南经")
    check("all_failed excluded from partial", m.exact is None and m.partial == [])
    m = t3.match("湖南都")
    check("healthy partial", [c.key for c in m.partial] == ["湖南都市"])
    t4 = make_table({f"频道{i}": Channel(f"频道{i}", f"频道{i}", cands((None, 0, False))) for i in range(9)})
    check("partial capped at 5", len(t4.match("频道").partial) == 5)


async def fixture_server(state):
    async def handler(request):
        i = int(request.match_info["i"])
        if i == 0 and state.get("fail0"):
            return web.Response(status=500)
        return web.Response(body=(FIX / NAMES[i]).read_bytes())
    app = web.Application()
    app.router.add_get("/{i}.m3u", handler)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_table():
    allow = lambda ip: ip == "127.0.0.1"  # noqa: E731
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "tv_channels.json"
        for bad in ("", "{not json", '{"channels": 5}'):
            path.write_text(bad, encoding="utf-8")
            check(f"bad state file {bad!r} -> empty", ChannelTable(path, [], None).channels == {})
        check("missing state file -> empty", ChannelTable(Path(d) / "nope.json", [], None).channels == {})

        state = {}
        server = await fixture_server(state)
        base = f"http://127.0.0.1:{server.port}"
        probed = []

        async def prober(c):
            probed.append(c.url)
            if "boom" in c.url:
                raise RuntimeError("x")
            return "63.141" in c.url

        t = ChannelTable(path, [f"{base}/{i}.m3u" for i in range(3)], prober,
                         session_factory=lambda: tv_net.make_session(ip_allowed=allow), ip_allowed=allow)
        try:
            await t.refresh()
            allc = [c for ch in t.channels.values() for c in ch.candidates]
            check("refresh built channels", "湖南卫视" in t.channels and len(allc) > 20)
            check("every candidate has ok set", all(c.ok is not None and c.checked_at is not None for c in allc))
            check("fake prober result used", all(c.ok == ("63.141" in c.url) for c in allc))
            check("file reloads equal", ChannelTable(path, [], None).channels == t.channels)
            n0 = len(t.channels["湖南卫视"].candidates)
            first_urls = {c.url for ch in t.channels.values() for c in ch.candidates if c.list_index == 0}
            check("list 0 contributed", len(first_urls) > 0)

            state["fail0"] = True
            probed.clear()
            await t.refresh()
            after = {c.url for ch in t.channels.values() for c in ch.candidates if c.list_index == 0}
            check("failed list reuses previous entries", after == first_urls)
            check("probe results kept by url", all(c.ok == ("63.141" in c.url)
                                                    for ch in t.channels.values() for c in ch.candidates))

            # 第一次就失败的列表直接跳过
            t2 = ChannelTable(None, [f"{base}/0.m3u", f"{base}/1.m3u"], prober,
                              session_factory=lambda: tv_net.make_session(ip_allowed=allow), ip_allowed=allow)
            await t2.refresh()
            check("first-run failed list skipped", t2.channels and all(
                c.list_index == 1 for ch in t2.channels.values() for c in ch.candidates))

            # 默认的 is_public_ip 拦住本地列表，不抛异常
            t3 = ChannelTable(None, [f"{base}/1.m3u"], prober)
            await t3.refresh()
            check("blocked list does not raise", t3.channels == {})
        finally:
            await server.close()

    async def boom(c):
        raise RuntimeError("x")
    t4 = ChannelTable(None, ["http://127.0.0.1:9/x.m3u"], boom)   # 被默认规则拦截，沿用上次的条目
    t4._last_entries[0] = [Entry("甲台", None, None, None, "http://z/1", 0)]
    await t4.refresh()
    check("prober exception counts as False", t4.channels["甲台"].candidates[0].ok is False)


if __name__ == "__main__":
    test_normalize()
    test_parse()
    test_build()
    test_ordered()
    test_match()
    asyncio.run(test_table())
    bad = [n for n, ok in results if not ok]
    print(f"{len(results) - len(bad)}/{len(results)} passed")
    sys.exit(1 if bad else 0)
