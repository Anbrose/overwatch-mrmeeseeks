"""队徽缓存：从 Liquipedia 下载队徽图片，存到磁盘（herodata 卷的 logos/），之后一直用本地文件。

图片不是 API 请求，但同样要客气：两次下载之间至少隔 FETCH_INTERVAL 秒，User-Agent 只带项目地址；
下载失败的地址 RETRY_AFTER 秒内不再重试。只接受 Liquipedia 上的地址（不跟随跳转），
存盘前用 Pillow 完整解码一次：截断的、不是图片的、像素过多的都不保存。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import tempfile
import time
from typing import Awaitable, Callable, Iterable

from PIL import Image

import esports

log = logging.getLogger("mrmeeseeks.logos")

FETCH_INTERVAL = 2.0
RETRY_AFTER = 6 * 3600
MAX_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 2_000_000           # 队徽缩略图 300px 左右，远小于这个数
ALLOWED_PREFIX = esports.LIQUIPEDIA + "/"
FORMATS = ("PNG", "JPEG", "GIF", "WEBP")


def check_image(data: bytes) -> None:
    """完整解码一次；截断、不是图片、像素过多都抛 ValueError。"""
    try:
        im = Image.open(io.BytesIO(data))
        if im.format not in FORMATS:
            raise ValueError(f"unsupported format {im.format}")
        if im.width * im.height > MAX_PIXELS:
            raise ValueError(f"too many pixels: {im.width}x{im.height}")
        im.load()
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"not a readable image: {e!r}") from e


async def _download(url: str) -> bytes:
    """读完整个响应（分块读，超过 MAX_BYTES 就放弃）；不跟随跳转。"""
    async with esports._session() as session, session.get(url, allow_redirects=False) as resp:
        if resp.status != 200:
            raise ValueError(f"HTTP {resp.status} for {url}")
        if resp.content_length is not None and resp.content_length > MAX_BYTES:
            raise ValueError(f"logo too large: {url}")
        data = bytearray()
        async for chunk in resp.content.iter_chunked(64 * 1024):
            data += chunk
            if len(data) > MAX_BYTES:
                raise ValueError(f"logo too large: {url}")
    return bytes(data)


class LogoCache:
    def __init__(self, folder: str,
                 fetch: Callable[[str], Awaitable[bytes]] = _download,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.folder = folder
        self.fetch, self.clock, self.sleep = fetch, clock, sleep
        self._failed: dict[str, float] = {}
        self._last_fetch = float("-inf")

    def _file(self, url: str) -> str:
        return os.path.join(self.folder, hashlib.sha1(url.encode()).hexdigest()[:16] + ".png")

    def path(self, url: str | None) -> str | None:
        """已经下载好的队徽文件路径，没有就是 None。"""
        if not url:
            return None
        f = self._file(url)
        return f if os.path.exists(f) else None

    async def ensure(self, urls: Iterable[str | None]) -> None:
        for url in dict.fromkeys(u for u in urls if u):
            if not url.startswith(ALLOWED_PREFIX) or self.path(url):
                continue
            if self.clock() - self._failed.get(url, float("-inf")) < RETRY_AFTER:
                continue
            wait = FETCH_INTERVAL - (self.clock() - self._last_fetch)
            if wait > 0:
                await self.sleep(wait)
            self._last_fetch = self.clock()
            try:
                data = await self.fetch(url)
                check_image(data)
                self._save(url, data)
            except Exception:
                log.exception("Could not download team logo %s", url)
                self._failed[url] = self.clock()

    def _save(self, url: str, data: bytes) -> None:
        os.makedirs(self.folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.folder, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, self._file(url))
        except BaseException:
            os.unlink(tmp)
            raise
