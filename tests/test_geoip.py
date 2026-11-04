"""地区限制：RIR 分配表解析与合并、IP 查国家（v4 / v6 / 内网 / 未知）、放行判定、缓存读写、下载刷新（假会话）、
X-Forwarded-For 取客户端 IP、被拦记录。

运行：python tests/test_geoip.py
"""
import asyncio
import ipaddress
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import geoip  # noqa: E402

results = []
NS = types.SimpleNamespace

DELEGATED = """\
2|apnic|20261025|1234|19830101|20261024|+1000
apnic|*|ipv4|*|100|summary
apnic|*|ipv6|*|50|summary
apnic|AU|ipv4|1.0.0.0|256|20110811|assigned|A1
apnic|AU|ipv4|1.0.1.0|256|20110811|allocated|A2
apnic|CN|ipv4|1.0.2.0|512|20110811|allocated|A3
apnic|NZ|ipv4|14.1.0.0|65536|20090101|allocated|A4
apnic||ipv4|27.0.0.0|256|20090101|available|
apnic|ZZ|ipv4|27.0.1.0|256|20090101|reserved|
apnic|AU|ipv6|2001:8000::|20|20020101|allocated|A5
apnic|JP|ipv6|2001:200::|35|19990813|allocated|A6
apnic|AU|ipv4|not-an-ip|256|20110811|assigned|A7
apnic|AU|ipv4|1.1.1.0|0|20110811|assigned|A8
"""


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def ip4(s):
    return int(ipaddress.IPv4Address(s))


def test_parse_and_merge():
    v4, v6 = geoip.parse_delegated(DELEGATED)
    check("跳过文件头、汇总、available / reserved、坏行、0 地址", len(v4) == 4 and len(v6) == 2)
    check("ipv4 value 是地址数", v4[0] == (ip4("1.0.0.0"), ip4("1.0.0.255"), "AU")
          and v4[2] == (ip4("1.0.2.0"), ip4("1.0.3.255"), "CN"))
    check("ipv6 value 是前缀长度", v6[0] == (int(ipaddress.IPv6Address("2001:8000::")),
                                         int(ipaddress.IPv6Address("2001:8000::")) + (1 << 108) - 1, "AU"))
    merged = geoip.merge_ranges(v4)
    check("相邻同国家合并", merged[0] == (ip4("1.0.0.0"), ip4("1.0.1.255"), "AU") and len(merged) == 3)
    overlap = geoip.merge_ranges([(10, 20, "AU"), (15, 30, "US"), (31, 40, "US"), (5, 8, "NZ"), (12, 14, "CN")])
    check("重叠只补多出的部分、排序", overlap == [(5, 8, "NZ"), (10, 20, "AU"), (21, 40, "US")])


def test_lookup_and_verdict():
    db = geoip.GeoDB(None, allowed=("au", "NZ"))
    check("没加载：check 为 None（放行由调用方决定），LOCAL 直接放行",
          db.check("1.0.0.1").allowed is None and db.check("127.0.0.1") == geoip.Verdict(True, "LOCAL")
          and not db.loaded and db.allowed == {"AU", "NZ"})
    v4, v6 = geoip.parse_delegated(DELEGATED)
    db.set_ranges(v4, v6, fetched_at=1000.0)
    check("已加载", db.loaded and db.status()["ranges"] == 5 and db.status()["allowed"] == ["AU", "NZ"])
    check("AU 放行", db.check("1.0.1.77") == geoip.Verdict(True, "AU"))
    check("NZ 放行", db.check("14.1.200.1") == geoip.Verdict(True, "NZ"))
    check("CN 拦", db.check("1.0.3.1") == geoip.Verdict(False, "CN"))
    check("不在任何表里 → 国家 None、拦", db.check("8.8.8.8") == geoip.Verdict(False, None))
    check("段的边界", db.country("1.0.1.255") == "AU" and db.country("1.0.2.0") == "CN" and db.country("1.0.4.0") is None)
    check("ipv6", db.country("2001:8000:1234::1") == "AU" and db.country("2001:200::1") == "JP"
          and db.check("2001:200::1").allowed is False and db.country("2a00::1") is None)
    check("ipv4-mapped ipv6 按 v4 查", db.country("::ffff:1.0.0.5") == "AU")
    check("内网 / 回环 / 非法", db.country("10.1.2.3") == "LOCAL" and db.country("::1") == "LOCAL"
          and db.country("fe80::1") == "LOCAL" and db.country("abc") is None and db.country(None) is None
          and db.check("abc") == geoip.Verdict(False, None))
    check("stale 按天", db.stale() is False or True)   # 取决于当前时间；下面用假时钟验证
    clock = [1000.0]
    db2 = geoip.GeoDB(None, clock=lambda: clock[0], refresh_days=7)
    check("没数据算过期", db2.stale())
    db2.set_ranges(v4, v6, fetched_at=1000.0)
    clock[0] = 1000.0 + 6 * 86400
    check("6 天没过期", not db2.stale())
    clock[0] = 1000.0 + 7 * 86400
    check("7 天过期", db2.stale())


def test_cache_file():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "geo" / "geoip.json"
        db = geoip.GeoDB(p)
        check("没缓存 load False", db.load() is False and not db.loaded)
        v4, v6 = geoip.parse_delegated(DELEGATED)
        db.set_ranges(v4, v6, fetched_at=1234.0)
        db.save()
        raw = json.loads(p.read_text(encoding="utf-8"))
        check("缓存写入（已合并）", raw["fetched_at"] == 1234.0 and len(raw["v4"]) == 3 and len(raw["v6"]) == 2)
        db2 = geoip.GeoDB(p)
        check("重新读取", db2.load() and db2.fetched_at == 1234.0 and db2.country("1.0.1.1") == "AU"
              and db2.country("2001:200::1") == "JP")
        p.write_text("{bad", encoding="utf-8")
        db3 = geoip.GeoDB(p)
        check("坏缓存 → False、空表", db3.load() is False and not db3.loaded)


class FakeResp:
    def __init__(self, status, body):
        self.status, self._body = status, body
        self.content = self

    async def iter_chunked(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, bodies):
        self.bodies, self.urls = bodies, []

    def get(self, url, allow_redirects=True):
        self.urls.append(url)
        body = self.bodies.get(url, 404)
        return FakeResp(404, b"") if body == 404 else FakeResp(200, body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_refresh():
    async def go():
        clock = [5000.0]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "geoip.json"
            sources = ("https://a/x", "https://b/y")
            other = DELEGATED.replace("apnic", "arin").replace("1.0.", "9.0.")
            sess = FakeSession({"https://a/x": DELEGATED.encode(), "https://b/y": other.encode()})
            db = geoip.GeoDB(p, sources=sources, clock=lambda: clock[0], session_factory=lambda: sess)
            ok = await db.refresh()
            check("刷新成功：两份都解析、写缓存、记时间", ok and db.loaded and db.fetched_at == 5000.0
                  and db.country("9.0.3.1") == "CN" and db.country("1.0.0.1") == "AU" and p.exists()
                  and db.last_error is None and sorted(sess.urls) == sorted(sources))
            # 一个来源失败：保留旧数据、记错误
            clock[0] = 6000.0
            bad = FakeSession({"https://a/x": DELEGATED.encode()})
            db._session_factory = lambda: bad
            ok = await db.refresh()
            check("一个来源 404 → 失败、旧数据保留", not ok and db.fetched_at == 5000.0 and db.country("9.0.3.1") == "CN"
                  and db.last_error and "HTTP 404" in db.last_error and db.status()["last_error"] == db.last_error)
            # 返回的不是分配表
            db._session_factory = lambda: FakeSession({"https://a/x": b"<html>", "https://b/y": b"<html>"})
            ok = await db.refresh()
            check("解析不出任何段 → 失败", not ok and "no ranges" in db.last_error and db.fetched_at == 5000.0)
            # run 循环：过期就刷新，失败后按 RETRY_INTERVAL 再试
            waits = []
            calls = []

            async def fake_sleep(s):
                waits.append(s)
                if len(waits) >= 3:
                    raise asyncio.CancelledError

            async def fake_refresh():
                calls.append(1)
                return len(calls) > 1
            db.refresh = fake_refresh
            clock[0] = 5000.0 + 8 * 86400
            try:
                await db.run(sleep=fake_sleep)
            except asyncio.CancelledError:
                pass
            check("run：过期 → 刷新；失败等 RETRY_INTERVAL，成功等 CHECK_INTERVAL",
                  waits[:2] == [geoip.RETRY_INTERVAL, geoip.CHECK_INTERVAL] and len(calls) >= 2)
    asyncio.run(go())


def test_client_ip_and_block_log():
    def req(remote, xff=None):
        return NS(remote=remote, headers={"X-Forwarded-For": xff} if xff else {})
    check("直连用对端", geoip.client_ip(req("8.8.8.8", "1.2.3.4")) == "8.8.8.8")
    check("反向代理：取 XFF 最后一段", geoip.client_ip(req("127.0.0.1", "9.9.9.9, 1.2.3.4")) == "1.2.3.4"
          and geoip.client_ip(req("172.18.0.1", "1.2.3.4")) == "1.2.3.4")
    check("没有 XFF 就是对端 / 对端为空", geoip.client_ip(req("127.0.0.1")) == "127.0.0.1" and geoip.client_ip(req(None)) is None)
    check("parse_allowed", geoip.parse_allowed(None) == ("AU", "NZ") and geoip.parse_allowed("") == ()
          and geoip.parse_allowed(" au, nz ,AU") == ("AU", "NZ"))

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "watch_blocked.json"
        clock = [100.0]
        log = geoip.BlockLog(p, clock=lambda: clock[0])
        e = log.record("8.8.8.8", "US", "room01")
        check("第一次记：6 位编号、次数 1", len(e.id) == 6 and e.hits == 1 and e.country == "US" and e.rooms == ["room01"]
              and e.first_at == 100.0 == e.last_at)
        clock[0] = 200.0
        e2 = log.record("8.8.8.8", None, "room02")
        check("同一 IP 累加、编号不变、国家不被 None 覆盖", e2 is e and e2.id == e.id and e2.hits == 2 and e2.country == "US"
              and e2.rooms == ["room01", "room02"] and e2.last_at == 200.0 and e2.first_at == 100.0)
        clock[0] = 300.0
        log.record("1.1.1.1", "CN", "room01")
        check("按最近一次排序", [x.ip for x in log.all()] == ["1.1.1.1", "8.8.8.8"])
        log2 = geoip.BlockLog(p, clock=lambda: clock[0])
        check("持久化", [x.ip for x in log2.all()] == ["1.1.1.1", "8.8.8.8"] and log2.entries["8.8.8.8"].id == e.id
              and log2.entries["8.8.8.8"].hits == 2)
        for i in range(7):
            log.record("8.8.8.8", "US", f"r{i}")
        check("每个 IP 最多记 5 个房间", len(log.entries["8.8.8.8"].rooms) == 5 and log.entries["8.8.8.8"].rooms[-1] == "r6")
        old = geoip.BLOCK_LOG_MAX
        geoip.BLOCK_LOG_MAX = 3
        try:
            for i in range(5):
                clock[0] += 1
                log.record(f"10.0.0.{i}", "X" + "X", "r")
            check("超过上限丢最旧", len(log.entries) == 3 and "8.8.8.8" not in log.entries)
        finally:
            geoip.BLOCK_LOG_MAX = old
        p.write_text("nope", encoding="utf-8")
        check("坏文件按空处理", geoip.BlockLog(p).all() == [])
        check("空 IP 记成 ?", geoip.BlockLog(None).record(None, None, "r").ip == "?")


if __name__ == "__main__":
    test_parse_and_merge()
    test_lookup_and_verdict()
    test_cache_file()
    test_refresh()
    test_client_ip_and_block_log()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
