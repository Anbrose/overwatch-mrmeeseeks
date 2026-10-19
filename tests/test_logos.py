"""队徽缓存：下载到磁盘、请求间隔、失败后暂不重试、只接受图片。

运行：python tests/test_logos.py
"""
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import logos  # noqa: E402

results = []
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
A = "https://liquipedia.net/commons/images/thumb/e/e4/T1_2019_allmode.png/300px-T1_2019_allmode.png"
B = "https://liquipedia.net/commons/images/thumb/9/95/ZETA_DIVISION_darkmode.png/300px-ZETA_DIVISION_darkmode.png"
C = "https://liquipedia.net/commons/images/thumb/x/xx/Broken.png/300px-Broken.png"


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


class Clock:
    def __init__(self):
        self.t, self.slept = 1_000_000.0, []

    def now(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_cache():
    clock, calls = Clock(), []

    async def fetch(url):
        calls.append(url)
        if url == C:
            raise RuntimeError("404")
        return PNG

    with tempfile.TemporaryDirectory() as d:
        cache = logos.LogoCache(d, fetch, clock.now, clock.sleep)
        check("没下载过时没有路径", cache.path(A) is None and cache.path(None) is None)
        asyncio.run(cache.ensure([A, B, None, A]))
        check("去重、跳过 None，按顺序下载", calls == [A, B])
        check("两次下载之间至少隔 2 秒", clock.slept == [logos.FETCH_INTERVAL])
        pa = cache.path(A)
        check("存成磁盘上的 png 文件", pa and pa.startswith(d) and pa.endswith(".png") and open(pa, "rb").read() == PNG)
        check("不同地址不同文件", cache.path(B) and cache.path(B) != pa)
        asyncio.run(cache.ensure([A, B]))
        check("已经下载过的不再下载", calls == [A, B])
        again = logos.LogoCache(d, fetch, clock.now, clock.sleep)
        check("新进程直接读到磁盘上的队徽", again.path(A) == pa)

        asyncio.run(cache.ensure([C]))
        check("下载失败：没有路径", cache.path(C) is None and calls[-1] == C)
        asyncio.run(cache.ensure([C]))
        check("失败后一段时间内不再重试", calls.count(C) == 1)
        clock.t += logos.RETRY_AFTER + 1
        asyncio.run(cache.ensure([C]))
        check("过了重试间隔再试一次", calls.count(C) == 2)


def test_rejects_non_images():
    async def fetch(url):
        return b"<html>not an image</html>"

    with tempfile.TemporaryDirectory() as d:
        cache = logos.LogoCache(d, fetch, Clock().now, Clock().sleep)
        asyncio.run(cache.ensure([A]))
        check("不是图片的内容不保存", cache.path(A) is None and os.listdir(d) == [])


def test_only_liquipedia():
    calls = []

    async def fetch(url):
        calls.append(url)
        return PNG

    with tempfile.TemporaryDirectory() as d:
        cache = logos.LogoCache(d, fetch, Clock().now, Clock().sleep)
        asyncio.run(cache.ensure(["https://example.com/logo.png", "file:///etc/passwd"]))
        check("只下载 Liquipedia 的图片", calls == [])


if __name__ == "__main__":
    test_cache()
    test_rejects_non_images()
    test_only_liquipedia()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
