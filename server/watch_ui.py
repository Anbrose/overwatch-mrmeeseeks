"""一起看的 Discord 部分：房间卡片文字、[进入房间]（链接按钮）+ [我是房主]（持久按钮）、卡片编辑节流、遗留卡片记录。

- 「我是房主」是 discord.ui.DynamicItem，custom_id 模板 `watch:host:<room_id>`；bot 启动时 add_dynamic_items(HostButton)。
  发起人点 → 只回复他（ephemeral）带房主密钥的链接；别人点 → 提示不是房主。
- WatchUI.on_change 登记在 RoomManager.listeners 里（同步回调），卡片编辑交给事件循环异步执行：
  同一房间距上次编辑不到 MIN_EDIT_INTERVAL 秒就排一次延后编辑，期间的变化合并（编辑时才读房间状态）。
  closed 立即编辑成"已结束"、去掉按钮，并从 CardStore 删除。
- CardStore（STATE_DIR/watch_cards.json）记下还开着的房间卡片；房间都在内存里，重启后 bot 把这些卡片编辑成
  "已结束（服务器重启）"。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

import discord

from watch_rooms import Room, RoomManager

log = logging.getLogger("mrmeeseeks.watch_ui")

MIN_EDIT_INTERVAL = 15.0
TEMPLATE = r"watch:host:(?P<room>[23456789a-z]{6})"
SOURCE_LABEL = {"bilibili": "B 站", "youtube": "YouTube"}
TITLE_MAX = 200
NOT_ENABLED = "一起看功能没有启用。"
NOT_HOST = "你不是这个房间的房主。"
ROOM_GONE = "这个房间已经结束了。"


def fmt_time(seconds: float) -> str:
    s = max(0, int(seconds))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _title(room: Room) -> str:
    title = room.video.title or "（无标题）"
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX - 1] + "…"
    return discord.utils.escape_markdown(title)


def ended_text(title: str | None = None, reason: str = "") -> str:
    lines = ["🎬 **一起看** · 已结束"]
    if title:
        lines.append(f"最后播放：**{title}**")
    if reason:
        lines.append(f"（{reason}）")
    return "\n".join(lines)


def card_text(room: Room, now: float, ended: bool = False, reason: str = "") -> str:
    title = _title(room)
    if ended:
        return ended_text(title, reason)
    duration = room.video.duration
    pos = fmt_time(room.playback.position_at(now, duration))
    if duration is not None:
        pos += f" / {fmt_time(duration)}"
    if duration and room.playback.position_at(now, duration) >= duration:
        state = "⏹ 已播完"
    else:
        state = "▶ 播放中" if room.playback.playing else "⏸ 已暂停"
    source = SOURCE_LABEL.get(room.video.source, room.video.source)
    return (f"🎬 **一起看**\n"
            f"正在播放：**{title}**（{source}）\n"
            f"{state} {pos} · 👥 {room.viewer_count()} 人在看\n"
            f"房主：{discord.utils.escape_markdown(room.host_name())}")


def room_url(public_url: str, room_id: str) -> str:
    return f"{public_url}/r/{room_id}"


class HostButton(discord.ui.DynamicItem[discord.ui.Button], template=TEMPLATE):
    handler: "WatchUI | None" = None     # bot 启动时设置

    def __init__(self, room_id: str):
        super().__init__(discord.ui.Button(label="我是房主", custom_id=f"watch:host:{room_id}",
                                           style=discord.ButtonStyle.secondary))
        self.room_id = room_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str]) -> "HostButton":
        return cls(match["room"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if HostButton.handler is None:
            await interaction.response.send_message(NOT_ENABLED, ephemeral=True)
            return
        await HostButton.handler.on_host(interaction, self.room_id)


def build_view(room: Room, public_url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="进入房间", style=discord.ButtonStyle.link,
                                    url=room_url(public_url, room.room_id)))
    view.add_item(HostButton(room.room_id))
    return view


class CardStore:
    """还开着的房间卡片 [(channel_id, message_id)]。path=None 时只在内存里。"""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None
        self.cards: list[tuple[int, int]] = self._load()

    def _load(self) -> list[tuple[int, int]]:
        if self.path is None:
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [(int(c), int(m)) for c, m in data["cards"]]
        except FileNotFoundError:
            return []
        except (OSError, ValueError, TypeError, KeyError):
            log.warning("Could not read %s; starting with no watch cards", self.path)
            return []

    def _save(self) -> None:
        if self.path is None:
            return
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"cards": [list(c) for c in self.cards]}, f)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write watch cards to %s", self.path)

    def add(self, channel_id: int, message_id: int) -> None:
        card = (int(channel_id), int(message_id))
        if card not in self.cards:
            self.cards.append(card)
            self._save()

    def remove(self, channel_id: int, message_id: int) -> None:
        card = (int(channel_id), int(message_id))
        if card in self.cards:
            self.cards.remove(card)
            self._save()

    def all(self) -> list[tuple[int, int]]:
        return list(self.cards)


EditFn = Callable[[int, int, str, "discord.ui.View | None"], Awaitable[None]]


class WatchUI:
    def __init__(self, rooms: RoomManager, public_url: str, cards: CardStore,
                 min_edit_interval: float = MIN_EDIT_INTERVAL,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.rooms, self.public_url, self.cards = rooms, public_url.rstrip("/"), cards
        self.clock = rooms.clock
        self.min_edit_interval, self.sleep = min_edit_interval, sleep
        self.edit: EditFn | None = None          # bot 注入 (channel_id, message_id, text, view)
        self._last_edit: dict[str, float] = {}
        self._pending: dict[str, asyncio.Task] = {}
        self._tasks: set[asyncio.Task] = set()   # 保留引用，避免任务被回收
        rooms.listeners.append(self.on_change)

    # ---------------- 按钮 ----------------
    async def on_host(self, interaction: discord.Interaction, room_id: str) -> None:
        room = self.rooms.get(room_id)
        if room is None:
            await interaction.response.send_message(ROOM_GONE, ephemeral=True)
            return
        if interaction.user.id != room.creator_id:
            await interaction.response.send_message(NOT_HOST, ephemeral=True)
            return
        if room.key_used:
            await interaction.response.send_message(
                f"房主链接已经用过了，房主身份现在在房间里的「{room.host_name()}」手上。", ephemeral=True)
            return
        await interaction.response.send_message(
            f"你的房主链接（别发给别人，只能用一次）：\n{room_url(self.public_url, room.room_id)}?k={room.owner_key}",
            ephemeral=True)

    # ---------------- 卡片编辑 ----------------
    def _spawn(self, coro) -> asyncio.Task | None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            log.warning("No running event loop; skipped a watch card edit")
            return None
        task = loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def on_change(self, room: Room, kind: str) -> None:
        if room.message_id is None or kind == "permissions":   # 还没发卡片 / 卡片上不显示控制权
            return
        rid = room.room_id
        if kind == "closed":
            pending = self._pending.pop(rid, None)
            if pending is not None and not pending.done():
                pending.cancel()
            self._last_edit.pop(rid, None)
            self.cards.remove(room.channel_id, room.message_id)
            self._spawn(self._edit(room, card_text(room, self.clock(), ended=True), None))
            return
        pending = self._pending.get(rid)
        if pending is not None and not pending.done():
            return                       # 延后编辑执行时才读状态，这次变化已经包含在内
        wait = self.min_edit_interval - (self.clock() - self._last_edit.get(rid, float("-inf")))
        if wait <= 0:
            self._last_edit[rid] = self.clock()
            self._spawn(self._edit(room, card_text(room, self.clock()), build_view(room, self.public_url)))
        else:
            task = self._spawn(self._edit_later(room, wait))
            if task is not None:
                self._pending[rid] = task

    async def _edit_later(self, room: Room, wait: float) -> None:
        await self.sleep(wait)
        rid = room.room_id
        if self._pending.get(rid) is asyncio.current_task():
            del self._pending[rid]       # 从这里起再来的变化另排一次
        if room.closed:
            return
        self._last_edit[rid] = self.clock()
        await self._edit(room, card_text(room, self.clock()), build_view(room, self.public_url))

    async def _edit(self, room: Room, text: str, view: discord.ui.View | None) -> None:
        if self.edit is None or room.message_id is None:
            return
        try:
            await self.edit(room.channel_id, room.message_id, text, view)
        except Exception:
            log.exception("Could not update watch card for room %s", room.room_id)

    async def end_stale(self, reason: str = "服务器重启") -> None:
        """把 CardStore 里遗留的卡片（重启前的房间）编辑成已结束，然后清空。"""
        for channel_id, message_id in self.cards.all():
            if self.edit is not None:
                try:
                    await self.edit(channel_id, message_id, ended_text(reason=reason), None)
                except discord.NotFound:
                    pass
                except Exception:
                    log.exception("Could not end stale watch card %s/%s", channel_id, message_id)
            self.cards.remove(channel_id, message_id)
