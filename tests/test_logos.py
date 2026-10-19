"""队徽缓存：下载到磁盘、请求间隔、失败后暂不重试、只接受图片。

运行：python tests/test_logos.py
"""
import asyncio
import io
import os
import random
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import logos  # noqa: E402
from aiohttp import web  # noqa: E402
from PIL import Image  # noqa: E402

results = []


def real_png(size=(300, 300), noisy=False):
    im = Image.new("RGBA", size, (230, 20, 40, 255))
    if noisy:                                       # 噪点让 PNG 压不小（约 150 KB），模拟真实大小的队徽
        rnd = random.Random(1)
        im.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256), 255) for _ in range(size[0] * size[1])])
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


PNG = real_png((8, 8))
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


def test_rejects_broken_images():
    big = real_png(noisy=True)

    async def truncated(url):
        return big[:8192]

    async def bomb(url):
        return real_png((3000, 3000))               # 压得很小，但解码后像素太多
    with tempfile.TemporaryDirectory() as d:
        cache = logos.LogoCache(d, truncated, Clock().now, Clock().sleep)
        asyncio.run(cache.ensure([A]))
        check("截断的图片（开头是 PNG）不保存", cache.path(A) is None and os.listdir(d) == [])
        cache = logos.LogoCache(d, bomb, Clock().now, Clock().sleep)
        asyncio.run(cache.ensure([B]))
        check("像素过多的图片不保存", cache.path(B) is None)


def test_download_real_http():
    """真的起一个 HTTP 服务，分块发送，确认 _download 读完整个响应；不跟随跳转；超过大小上限放弃。"""
    big = real_png(noisy=True)

    async def chunked(request):
        resp = web.StreamResponse()
        resp.content_type = "image/png"
        await resp.prepare(request)
        for i in range(0, len(big), 8192):
            await resp.write(big[i:i + 8192])
            await asyncio.sleep(0)
        await resp.write_eof()
        return resp

    async def redirect(request):
        raise web.HTTPFound("/logo.png")

    async def huge(request):
        return web.Response(body=b"\x89PNG\r\n\x1a\n" + b"\x00" * (logos.MAX_BYTES + 10), content_type="image/png")

    async def run():
        app = web.Application()
        app.router.add_get("/logo.png", chunked)
        app.router.add_get("/moved.png", redirect)
        app.router.add_get("/huge.png", huge)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        out = {}
        try:
            out["data"] = await logos._download(base + "/logo.png")
            for name in ("moved", "huge"):
                try:
                    await logos._download(f"{base}/{name}.png")
                    out[name] = "ok"
                except Exception as e:
                    out[name] = repr(e)
        finally:
            await runner.cleanup()
        return out

    out = asyncio.run(run())
    check(f"分块发送的 {len(big) // 1024} KB 队徽完整读到", out["data"] == big)
    check("不跟随跳转", out["moved"] != "ok")
    check("超过大小上限放弃", out["huge"] != "ok")


if __name__ == "__main__":
    test_cache()
    test_rejects_broken_images()
    test_download_real_http()
    test_rejects_non_images()
    test_only_liquipedia()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
