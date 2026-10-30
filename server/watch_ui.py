"""一起看的 Discord 部分：房间卡片文字、[进入房间]（链接按钮）+ [我是房主]（持久按钮）、卡片编辑节流、遗留卡片记录。

- 「我是房主」是 discord.ui.DynamicItem，custom_id 模板 `watch:host:<room_id>`；bot 启动时 add_dynamic_items(HostButton)。
  发起人点 → 只回复他（ephemeral）带房主密钥的链接；别人点 → 提示不是房主。
- WatchUI.on_change 登记在 RoomManager.listeners 里（同步回调），卡片编辑交给事件循环异步执行：
  同一房间距上次编辑不到 MIN_EDIT_INTERVAL 秒就排一次延后编辑，期间的变化合并（编辑时才读房间状态）。
  closed 立即编辑成"已结束"、去掉按钮，并从 CardStore 删除。
- 按片名搜索：bot 发出带 PickSelect（DynamicItem，custom_id `watch:pick:<token>`）的结果消息，WatchUI.pending 记着待选项；
  只有发起人能选，选一次就作废（on_pick 在第一个 await 之前就标记 done），2 分钟没选消息改成"选择已过期"。
- 「没有我想看的」（搜索结果下面）和「我正在寻找这一部影视资源」（没找到时）是 RequestButton（DynamicItem，
  custom_id `watch:req:<miss|want>:<片名>`，片名就在 custom_id 里，所以选单过期、bot 重启后都还能点）。谁都能点，
  记到 RequestStore（每人每天 WATCH_REQUEST_DAILY_LIMIT 次），管理后台里能看到。选单过期后这个按钮保留。
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
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import discord

from vod_search import Show
from watch_progress import ProgressStore
from watch_requests import (KIND_NO_RESULTS, KIND_NOT_LISTED, RequestStore, clean_query, duplicate_text, limit_text,
                            recorded_text)
from watch_rooms import Room, RoomManager

log = logging.getLogger("mrmeeseeks.watch_ui")

MIN_EDIT_INTERVAL = 15.0
TEMPLATE = r"watch:host:(?P<room>[23456789a-z]{6})"
SOURCE_LABEL = {"bilibili": "B 站", "youtube": "YouTube"}
TITLE_MAX = 200
NOT_ENABLED = "一起看功能没有启用。"
NOT_HOST = "你不是这个房间的房主。"
ROOM_GONE = "这个房间已经结束了。"
PICK_TEMPLATE = r"watch:pick:(?P<token>[A-Za-z0-9_-]{8,32})"
PICK_TIMEOUT = 120.0
PICK_EXPIRED = "选择已过期"
PICK_GONE = "已过期，请重新搜索"
PICK_NOT_REQUESTER = "只有发起人能选"
PICK_PREPARING = "正在准备…"
OPTION_MAX = 100
REQUEST_TEMPLATE = r"watch:req:(?P<kind>miss|want):(?P<query>.{1,50})"
REQUEST_KIND = {"miss": KIND_NOT_LISTED, "want": KIND_NO_RESULTS}
REQUEST_LABEL = {"miss": "没有我想看的", "want": "我正在寻找这一部影视资源"}
REQUESTS_OFF = "这个功能没有启用。"


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
    vod = room.video.vod
    if vod is not None and 0 <= vod.line < len(vod.show.lines):
        source = f"片源：{vod.show.lines[vod.line].name}"
    else:
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


@dataclass
class PendingSearch:
    token: str
    requester_id: int
    query: str
    shows: list[Show]
    channel_id: int
    message_id: int | None
    origin_message: discord.Message
    created_at: float
    done: bool = False


def option_label(show: Show) -> str:
    parts = [show.title]
    if show.year:
        parts.append(str(show.year))
    if show.type_name:
        parts.append(show.type_name)
    parts.append(f"{show.episode_count} 集")
    return " · ".join(parts)[:OPTION_MAX]


def search_header(query: str, mention: str) -> str:
    return f"🔎 「{query}」的搜索结果（只有 {mention} 能选，2 分钟内有效）"


class PickSelect(discord.ui.DynamicItem[discord.ui.Select], template=PICK_TEMPLATE):
    handler: "WatchUI | None" = None     # bot 启动时设置

    def __init__(self, token: str, options: list[discord.SelectOption] | None = None):
        super().__init__(discord.ui.Select(custom_id=f"watch:pick:{token}", placeholder="选一个",
                                           options=options or []))
        self.token = token

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select,
                             match: re.Match[str]) -> "PickSelect":
        return cls(match["token"], item.options)

    async def callback(self, interaction: discord.Interaction) -> None:
        if PickSelect.handler is None:
            await interaction.response.send_message(NOT_ENABLED, ephemeral=True)
            return
        await PickSelect.handler.on_pick(interaction, self.token)


class RequestButton(discord.ui.DynamicItem[discord.ui.Button], template=REQUEST_TEMPLATE):
    """「没有我想看的」(miss) / 「我正在寻找这一部影视资源」(want)。片名放在 custom_id 里，按钮不依赖内存状态。"""
    handler: "WatchUI | None" = None     # bot 启动时设置

    def __init__(self, kind: str, query: str):
        query = clean_query(query)
        super().__init__(discord.ui.Button(label=REQUEST_LABEL[kind], custom_id=f"watch:req:{kind}:{query}",
                                           style=discord.ButtonStyle.secondary, emoji="🙋"))
        self.kind, self.query = kind, query

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str]) -> "RequestButton":
        return cls(match["kind"], match["query"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if RequestButton.handler is None:
            await interaction.response.send_message(NOT_ENABLED, ephemeral=True)
            return
        await RequestButton.handler.on_request(interaction, REQUEST_KIND[self.kind], self.query)


def build_miss_view(query: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(RequestButton("miss", query))
    return view


def build_want_view(query: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(RequestButton("want", query))
    return view


def build_pick_view(p: PendingSearch, progress: ProgressStore | None) -> discord.ui.View:
    options = []
    for i, show in enumerate(p.shows):
        seen = progress.get(p.channel_id, show.key) if progress is not None else None
        options.append(discord.SelectOption(label=option_label(show), value=str(i),
                                            description=f"上次看到第 {seen.episode + 1} 集" if seen else None))
    view = discord.ui.View(timeout=None)
    view.add_item(PickSelect(p.token, options))
    view.add_item(RequestButton("miss", p.query))
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
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 requests: RequestStore | None = None):
        self.rooms, self.public_url, self.cards = rooms, public_url.rstrip("/"), cards
        self.requests = requests
        self.clock = rooms.clock
        self.min_edit_interval, self.sleep = min_edit_interval, sleep
        self.edit: EditFn | None = None          # bot 注入 (channel_id, message_id, text, view)
        self.create_vod_room: Callable[[PendingSearch, int], Awaitable[None]] | None = None   # bot 注入
        self.pending: dict[str, PendingSearch] = {}
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

    async def on_request(self, interaction: discord.Interaction, kind: str, query: str) -> None:
        """「没有我想看的」/「我正在寻找这一部影视资源」：记一条，只回复本人。"""
        if self.requests is None:
            await interaction.response.send_message(REQUESTS_OFF, ephemeral=True)
            return
        user = interaction.user
        channel_id = getattr(interaction, "channel_id", None) or 0
        try:
            res = self.requests.add(query, kind, user.id, user.display_name, channel_id)
        except ValueError:
            await interaction.response.send_message(REQUESTS_OFF, ephemeral=True)
            return
        if res.status == "ok":
            text = recorded_text(clean_query(query), res.remaining, self.requests.limit)
        elif res.status == "duplicate":
            text = duplicate_text(clean_query(query))
        else:
            text = limit_text(self.requests.limit)
        await interaction.response.send_message(text, ephemeral=True)

    # ---------------- 搜索选单 ----------------
    def add_search(self, p: PendingSearch) -> None:
        self.pending[p.token] = p
        self._spawn(self._expire(p))

    async def _expire(self, p: PendingSearch) -> None:
        await self.sleep(PICK_TIMEOUT)
        self.pending.pop(p.token, None)
        if p.done or self.edit is None or p.message_id is None:
            return
        p.done = True
        try:     # 选单去掉，「没有我想看的」留着
            await self.edit(p.channel_id, p.message_id, PICK_EXPIRED, build_miss_view(p.query))
        except Exception:
            log.exception("Could not expire search message %s", p.message_id)

    async def on_pick(self, interaction: discord.Interaction, token: str) -> None:
        p = self.pending.get(token)
        if p is None:
            await interaction.response.send_message(PICK_GONE, ephemeral=True)
            return
        if interaction.user.id != p.requester_id:
            await interaction.response.send_message(PICK_NOT_REQUESTER, ephemeral=True)
            return
        if p.done:
            await interaction.response.send_message(PICK_GONE, ephemeral=True)
            return
        try:
            index = int(interaction.data["values"][0])
            if not 0 <= index < len(p.shows):
                raise ValueError(index)
        except (KeyError, IndexError, TypeError, ValueError):
            await interaction.response.send_message(PICK_GONE, ephemeral=True)
            return
        p.done = True            # 第一个 await 之前就占位：连点只有一次生效
        await interaction.response.defer()
        if self.create_vod_room is None:
            return
        if self.edit is not None and p.message_id is not None:
            try:                 # 找片源可能要几十秒：先去掉选单、告诉大家在准备
                await self.edit(p.channel_id, p.message_id, PICK_PREPARING, None)
            except Exception:
                log.exception("Could not edit search message %s", p.message_id)
        try:
            await self.create_vod_room(p, index)
        except Exception:
            log.exception("Could not create a vod room for %r", p.query)

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
