"""一起看电视直播 出站请求层测试：地址过滤、URL 转义、手动跟随重定向、限长读取。

运行：python tests/test_tv_net.py
不联网：用 127.0.0.1 上的本地 aiohttp 假服务器；放行 127.0.0.1 只在测试里传 ip_allowed。
"""
import asyncio
import os
import sys

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import tv_net  # noqa: E402
from tv_net import Blocked, FilteringResolver, TooLarge  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


class FakeInner:
    def __init__(self, hosts):
        self.hosts = hosts

    async def resolve(self, host, port=0, family=0):
        return [{"hostname": host, "host": h, "port": port, "family": 2,
                 "proto": 6, "flags": 0} for h in self.hosts]

    async def close(self):
        pass


def test_pure():
    for ip in ("8.8.8.8",):
        check(f"public {ip}", tv_net.is_public_ip(ip))
    for ip in ("127.0.0.1", "10.1.2.3", "172.18.0.5", "192.168.1.1", "169.254.169.254",
               "::1", "fe80::1", "224.0.0.1", "0.0.0.0", "::ffff:127.0.0.1", "not-an-ip"):
        check(f"not public {ip}", not tv_net.is_public_ip(ip))
    check("quote non-ascii", tv_net.quote_url("http://example.com/直播/湖南.m3u8?a=1&b=中")
          == "http://example.com/%E7%9B%B4%E6%92%AD/%E6%B9%96%E5%8D%97.m3u8?a=1&b=%E4%B8%AD")
    check("quote keeps %xx", tv_net.quote_url("http://h/a%20b.m3u8") == "http://h/a%20b.m3u8")
    check("quote space", tv_net.quote_url("http://h/a b.m3u8") == "http://h/a%20b.m3u8")


async def test_resolver():
    r = FilteringResolver(tv_net.is_public_ip, FakeInner(["127.0.0.1", "93.184.216.34"]))
    out = await r.resolve("x.test", 80)
    check("resolver drops private", [a["host"] for a in out] == ["93.184.216.34"])
    r = FilteringResolver(tv_net.is_public_ip, FakeInner(["127.0.0.1"]))
    try:
        await r.resolve("x.test", 80)
        check("resolver all blocked raises", False)
    except OSError:
        check("resolver all blocked raises", True)


async def test_open():
    async def ok(request):
        return web.Response(body=b"x" * 5000)

    async def r(request):
        raise web.HTTPFound("/ok")

    async def loop(request):
        raise web.HTTPFound("/loop")

    async def evil(request):
        raise web.HTTPFound("http://10.0.0.1/x")

    async def ua(request):
        return web.Response(text=request.headers.get("User-Agent", ""))

    app = web.Application()
    app.router.add_get("/ok", ok)
    app.router.add_get("/r", r)
    app.router.add_get("/loop", loop)
    app.router.add_get("/evil", evil)
    app.router.add_get("/ua", ua)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    base = f"http://127.0.0.1:{server.port}"
    allow = lambda ip: ip == "127.0.0.1"  # noqa: E731
    session = tv_net.make_session(ip_allowed=allow)
    try:
        resp = await tv_net.open_url(session, base + "/r", None, ip_allowed=allow)
        check("redirect followed", resp.status == 200 and str(resp.url).endswith("/ok"))
        resp.release()

        for path, name in (("/loop", "loop blocked"), ("/evil", "private redirect blocked")):
            try:
                await tv_net.open_url(session, base + path, None, ip_allowed=allow)
                check(name, False)
            except Blocked:
                check(name, True)
        try:
            await tv_net.open_url(session, "rtmp://x/y", None, ip_allowed=allow)
            check("bad scheme blocked", False)
        except Blocked:
            check("bad scheme blocked", True)
        try:
            await tv_net.open_url(session, base + "/ok", None)
            check("default blocks literal loopback", False)
        except Blocked:
            check("default blocks literal loopback", True)

        resp = await tv_net.open_url(session, base + "/ua", "AptvPlayer-UA", ip_allowed=allow)
        check("custom UA", await resp.text() == "AptvPlayer-UA")
        resp = await tv_net.open_url(session, base + "/ua", None, ip_allowed=allow)
        check("default UA", await resp.text() == tv_net.USER_AGENT)

        resp = await tv_net.open_url(session, base + "/ok", None, ip_allowed=allow)
        try:
            await tv_net.read_capped(resp, 1000)
            check("read_capped too large", False)
        except TooLarge:
            check("read_capped too large", True)
        resp.release()
        resp = await tv_net.open_url(session, base + "/ok", None, ip_allowed=allow)
        check("read_capped ok", len(await tv_net.read_capped(resp, 10000)) == 5000)
        resp.release()
    finally:
        await session.close()
        await server.close()


async def test_unicode_hosts():
    """Unicode / 非常规写法的 IP 主机：默认 ip_allowed 下一律拦住，本地服务器零命中。"""
    hits = {"target": 0, "src": 0}

    async def target(request):
        hits["target"] += 1
        return web.Response(text="secret")

    tapp = web.Application()
    tapp.router.add_get("/x", target)
    tserver = TestServer(tapp, host="127.0.0.1")
    await tserver.start_server()
    port = tserver.port

    fullwidth = f"http://１２７.０.０.１:{port}/x"
    ideographic = f"http://127。0。0。1:{port}/x"

    async def redir(request):
        hits["src"] += 1
        which = request.match_info["which"]
        loc = fullwidth if which == "fw" else ideographic
        return web.Response(status=302, headers={"Location": loc})

    sapp = web.Application()
    sapp.router.add_get("/r/{which}", redir)
    sserver = TestServer(sapp, host="127.0.0.1")
    await sserver.start_server()

    session = tv_net.make_session()
    try:
        cases = [("fullwidth digits", fullwidth), ("ideographic full stop", ideographic),
                 ("hex int", f"http://0x7f000001:{port}/x"),
                 ("short dotted", f"http://127.1:{port}/x"),
                 ("v4-mapped v6", f"http://[::ffff:127.0.0.1]:{port}/x")]
        for name, url in cases:
            before = hits["target"]
            try:
                resp = await tv_net.open_url(session, url, None)
                resp.release()
                ok = False
            except Blocked:
                ok = True
            except (aiohttp.ClientError, OSError) as e:
                # 非 Unicode 写法交给解析器过滤，报连接错误也算拦住
                ok = name in ("hex int", "short dotted", "v4-mapped v6")
                print(f"  {name}: {type(e).__name__}", flush=True)
            if name in ("fullwidth digits", "ideographic full stop", "v4-mapped v6"):
                ok = ok and _raised_blocked(url)
            check(f"unicode/odd host blocked: {name}", ok and hits["target"] == before)
    finally:
        await session.close()

    # 重定向：源站通过“解析器映射的主机名”放行，Location 指向 Unicode 写法的 127.0.0.1
    src_resolver = FilteringResolver(lambda ip: ip == "127.0.0.1", FakeInner(["127.0.0.1"]))
    session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=src_resolver))
    try:
        for which, name in (("fw", "fullwidth"), ("id", "ideographic")):
            src_before = hits["src"]
            try:
                resp = await tv_net.open_url(session, f"http://src.test:{sserver.port}/r/{which}",
                                             None)
                resp.release()
                ok = False
            except Blocked:
                ok = True
            check(f"redirect to {name} host blocked", ok and hits["src"] == src_before + 1)
        check("unicode target never hit", hits["target"] == 0)
    finally:
        await session.close()
        await sserver.close()
        await tserver.close()


def _raised_blocked(url):
    """_check_url 本身就该拒绝（不依赖连接失败）。"""
    try:
        tv_net._check_url(url, tv_net.is_public_ip)
        return False
    except Blocked:
        return True


if __name__ == "__main__":
    test_pure()
    asyncio.run(test_resolver())
    asyncio.run(test_open())
    asyncio.run(test_unicode_hosts())
    bad = [n for n, ok in results if not ok]
    print(f"{len(results) - len(bad)}/{len(results)} passed")
    sys.exit(1 if bad else 0)
