"""英雄头像表情：把官方补丁页上的英雄图标上传成 bot 的应用表情（application emoji），补丁摘要里放在英雄名旁边。

应用表情归 bot 所有，在任何服务器都能用，最多 2000 个；名字形如 ow_roadhog。第一次遇到某个英雄时上传，
之后复用（启动后第一次用时先拉一遍已有的表情）。下载或上传失败就不显示头像，不影响发帖。
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import cv2
import numpy as np

from herodata import _norm

log = logging.getLogger("mrmeeseeks.emojis")

PREFIX = "ow_"
SIZE = 128                    # Discord 表情最终显示 ~22px，128 足够清晰，也远小于 256KB 上限
MAX_FAILURES = 3              # 连续上传失败这么多次就不再试（多半是额度满了或没权限），只用已有的表情
USER_AGENT = "mrmeeseeks-discord-bot/1.0 (https://github.com/Anbrose/overwatch-mrmeeseeks)"


def emoji_name(hero: str) -> str:
    """'Lúcio' -> 'ow_lucio'，'Soldier: 76' -> 'ow_soldier76'（表情名只能是字母数字下划线，2–32 个字符）。"""
    return (PREFIX + _norm(hero))[:32]


def to_png(data: bytes, size: int = SIZE) -> bytes:
    """任意图片 -> 正方形 PNG（保留透明通道）。"""
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError("not an image")
    image = cv2.resize(image, (size, size), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("could not encode PNG")
    return buf.tobytes()


async def _download(url: str) -> bytes:
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT},
                                     timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


class HeroEmojis:
    def __init__(self, client, download=_download):
        self.client = client          # discord.Client：fetch_application_emojis / create_application_emoji
        self.download = download
        self.known: dict[str, str] | None = None    # 表情名 -> "<:ow_roadhog:123>"

    async def _load(self) -> None:
        await self.client.wait_until_ready()
        self.known = {e.name: str(e) for e in await self.client.fetch_application_emojis()}
        log.info("Loaded %d application emojis", len(self.known))

    async def for_patch(self, patch: dict[str, Any]) -> dict[str, str]:
        """补丁里每个英雄 -> 表情标记；没有图标或上传失败的英雄不在结果里。"""
        try:
            if self.known is None:
                await self._load()
        except Exception:
            log.exception("Could not list application emojis; posting without hero icons")
            return {}
        icons: dict[str, str] = {}
        tried: set[str] = set()       # 同一英雄可能出现在几个分区里，只试一次
        failures = 0
        for block in patch.get("blocks", []):
            hero, url = block["hero"], block.get("icon")
            if hero in tried:
                continue
            tried.add(hero)
            name = emoji_name(hero)
            if name not in self.known and url and failures < MAX_FAILURES:
                try:
                    emoji = await self.client.create_application_emoji(name=name, image=to_png(await self.download(url)))
                    self.known[name] = str(emoji)
                    failures = 0
                    log.info("Created emoji %s for %s", name, hero)
                except Exception as e:
                    failures += 1
                    log.warning("Could not create an emoji for %s from %s: %r", hero, url, e)
                    if failures == MAX_FAILURES:
                        log.warning("Giving up on new hero emojis for %s after %d failures in a row",
                                    patch.get("id"), failures)
            if name in self.known:
                icons[hero] = self.known[name]
        return icons
