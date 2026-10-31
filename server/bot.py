"""mrmeeseeks 服务器入口：Discord bot + WebSocket 配对服务，跑在同一个进程里。

频道里的指令：
  @mrmeeseeks            连接一个本地客户端（选择标识 -> 私发 8 位配对码），也可写 connect
  @mrmeeseeks status     查看所有在线客户端
  @mrmeeseeks player <BattleTag>  用 OverFast 查玩家段位和常用英雄
  @mrmeeseeks analyze    用本频道最近一次识别结果重新给建议
  @mrmeeseeks label      拿出待标注的未知头像
  @mrmeeseeks disconnect 解绑当前频道里的客户端
  @mrmeeseeks help       显示帮助
  @mrmeeseeks wishes [游戏] [7d]  管理员导出许愿 CSV（私信发送）
  @mrmeeseeks announce   管理员发多语言公告（表单 -> 翻译预览 -> 发布），见 announce.py
  @mrmeeseeks patches here|off|status  官方补丁说明推送频道，见 patch_feed.py
  @mrmeeseeks esports here|off|status  赛事推送的默认频道，见 esports_feed.py
  @mrmeeseeks region <赛区> [off]       把当前频道绑定成某赛区的频道（赛事推送和开播通知共用），见 regions.py
  @mrmeeseeks live add|remove|list     按赛区登记 B 站直播间，开播时通知，见 live_watch.py
  @mrmeeseeks 一起看 <链接、片名或台名>  开一起看房间（B 站 / YouTube 链接，按片名搜影视，或按台名看电视直播；也可写 watch），见 watch_ui.py
  @mrmeeseeks <问题>     英雄问答（数值、补丁、几枪击杀），见 hero_qa.py

功能许愿频道（WISH_CHANNEL_ID）只接受表单提交，见 wishes.py；许愿子区里的 @ 用来改方案，见 wish_plan.py。
"""
from __future__ import annotations

import asyncio
import functools
import io
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Sequence

import discord
import numpy as np
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

import overfast
from announce import AnnounceDesk, AnnouncementStore, LangButton, StartView, Translator
import matchups
import predictions as pr
from esports_feed import EsportsFeed
from prediction_feed import PredictionFeed, week_start
from prediction_ui import PredictButton, PredictionUI, build_view
from logos import LogoCache
from rosters import RosterCache
from live_watch import (DEFAULT_POLL_INTERVAL, BilibiliClient, LiveError, LiveEvent, LiveStore, LiveWatcher, Source,
                        parse_bilibili_target)
from regions import KNOWN_REGIONS, RegionError, RegionStore, normalize_region
from hero_emojis import HeroEmojis
from keybinds import KeyBinds, load_manual as load_manual_keys
from patch_feed import PatchFeed, PatchSummarizer, chinese_names
from analyzer import Analyzer, format_facts, roles_from_roster
from hero_qa import Cooldown, HeroQA
from herodata import HeroStore, load_aliases, refresh_loop
from label_ui import Roster, build_roster, merge_heroes, send_prompts
from labeling import LabelStore
from registry import CODE_TTL_SECONDS, ClientConn, PairingError, Registry
from situation import Situation
from vision.heroes import HeroMatcher
from vision.recognize import Recognizer, to_facts
from vision.text import load_maps
from wish_plan import WishPlanner, WishPlans
from wishes import (WISH_DAILY_LIMIT, WishDesk, WishEntryView, WishLog, WishQuota, ensure_entry, export_csv, parse_export_args,
                    parse_games, select_wishes, vote_count)
import tv_net
import tv_stream
from tv_channels import Channel, ChannelTable, parse_sources_env as parse_tv_lists
import qa_log
from qa_log import QaLog
from vod_playlist import PlaylistError
from vod_search import SearchUnavailable, VodSearch, parse_sources_env
from geoip import BlockLog, GeoDB, parse_allowed
from sysstats import SystemStats
from watch_progress import ProgressRecorder, ProgressStore
from watch_requests import DAILY_LIMIT as REQUEST_DAILY_LIMIT, RequestStore
from watch_rooms import RoomManager, RoomsFull, TvRoomsFull
from watch_server import TvUnavailable, WatchServer, load_admin_key
from watch_sources import QUALITY_QN, Sources, VideoUnavailable, find_link
from watch_ui import (HOST_LINK_FAILED, PICK_PREPARING, CardStore, PendingSearch, PickSelect, RequestButton, WatchUI,
                      build_pick_embeds, build_pick_view, build_want_view, host_link_text, search_header,
                      card_text, build_view as build_watch_view)
from ws_server import WSServer

DATA_DIR = Path(__file__).resolve().parent / "data"
AUTO_PROMPTS = 3     # 每次截图最多当场弹几个未知头像
LABEL_BATCH = 5      # @mrmeeseeks label 一次拿几个

log = logging.getLogger("mrmeeseeks")

HELP_TEXT = (
    "**mrmeeseeks commands**\n"
    "`@mrmeeseeks` or `@mrmeeseeks connect` Connect a local client (pick its ID and I'll DM you an 8-digit pairing code)\n"
    "`@mrmeeseeks status` List online clients\n"
    "`@mrmeeseeks player Name#1234` Look up a player's ranks and most played heroes (case-sensitive BattleTag)\n"
    "`@mrmeeseeks analyze` Re-run advice on the latest recognized situation in this channel\n"
    "`@mrmeeseeks label` Label portraits I couldn't recognize\n"
    "`@mrmeeseeks disconnect` Unpair the client bound to this channel\n"
    "`@mrmeeseeks esports here` / `esports off` Post OWCS schedule, reminders, results and news in this channel "
    "(needs Manage Channels); `esports status` shows the current setup; "
    "`esports preview` posts samples here (for testing)\n"
    "`@mrmeeseeks predictions` Prediction leaderboard (this week, season, your rank); "
    "`predictions reset` starts a new season (needs Manage Server)\n"
    "`@mrmeeseeks region cn` Make this channel the home of one region (cn, asia, korea, japan, pacific, emea, na, world): "
    "its matches and live alerts go here instead of the esports channel; `region cn off` unbinds, `region` lists\n"
    "`@mrmeeseeks live add <region> <Bilibili room url> [label]` Alert that region's channel when the room goes live; "
    "`live remove <url>`, `live list`\n"
    "`@mrmeeseeks patches here` / `patches off` Post a summary of each new official Overwatch patch in this channel "
    "(needs Manage Channels); `patches status` shows the latest patch\n"
    "`@mrmeeseeks 一起看 <Bilibili/YouTube link, show name or TV channel>` (or `watch`) Open a watch-together room with synced playback\n"
    "`@mrmeeseeks help` Show this help\n"
    "In DMs, no `@mrmeeseeks` needed (admins can read DMs)\n"
    "`@mrmeeseeks wishes [game] [7d]` (admins) Get feature wishes with their votes as a CSV by DM\n"
    "`@mrmeeseeks announce` (admins) Post an announcement with buttons to read it in "
    "简体中文 / 繁體中文 / English / 日本語 / 한국어\n"
    "`@mrmeeseeks <question>` Ask about heroes, e.g. `was Cassidy nerfed recently?`, "
    "`Tracer HP`, `how many Cassidy headshots kill Mauga at 30m?`"
)
COMMANDS = ("connect", "status", "disconnect", "help", "analyze", "label", "announce")
ARG_COMMANDS = ("player", "esports", "patches", "wishes", "region", "live", "predictions", "一起看", "watch")     # 带参数的指令；参数保留大小写（BattleTag 大小写敏感）
LIVE_USAGE = ("Usage: `@mrmeeseeks live add <region> <Bilibili room url or id> [label]`, "
              "`live remove <room url or id>` or `live list`. Bind the region's channel first with `@mrmeeseeks region <region>`.")
ASK_COOLDOWN_SECONDS = 5
# 私信里不能用的指令：都是给服务器频道做设置的（predictions 只有 reset 不行，见 on_message）
GUILD_ONLY = ("esports", "patches", "region", "live", "announce")
DM_GUILD_ONLY = "This command only works in a server channel, not in DMs."
WATCH_USAGE = "用法：@mrmeeseeks 一起看 <B站或YouTube链接、片名或台名>"
_WATCH_PREFIX = re.compile(r"watch(?=\s|http)", re.IGNORECASE)     # \s 含全角空格


def parse_command(text: str, explicit: bool = True) -> tuple[str, str]:
    """去掉 @ 之后的文本 -> (指令, 参数)。

    空文本是 connect；无参数指令要整句完全匹配，参数是原文；`player Name#1234` 的参数是后半句；
    其余都当作英雄问答，参数是原文。explicit=False 表示消息里没有写 @mrmeeseeks（只是回复了
    bot 的消息）：这种情况下普通文本（如 "thanks"）显示帮助，不调用付费的问答。
    """
    stripped = text.strip()
    if not stripped:
        return "connect", ""
    if stripped.lower() in COMMANDS:
        return stripped.lower(), stripped
    # 一起看：链接可以紧贴指令（`一起看https://…`），也可以用全角空格隔开
    if stripped.startswith("一起看"):
        return "一起看", stripped[len("一起看"):].strip()
    watch = _WATCH_PREFIX.match(stripped)
    if watch:
        return "watch", stripped[len("watch"):].strip()
    head, _, rest = stripped.partition(" ")
    if head.lower() in ARG_COMMANDS:
        return head.lower(), rest.strip()
    return ("ask" if explicit else "help"), stripped


def _age(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes} min" if minutes else f"{int(seconds)} s"


# ---------------- 选择客户端的下拉菜单 ----------------
class ClientSelect(discord.ui.Select):
    def __init__(self, bot: "MeeseeksBot", clients: list[ClientConn]):
        now = time.time()
        options = [
            discord.SelectOption(
                label=c.client_id, value=c.client_id,
                description=f"{c.hostname} · online for {_age(now - c.connected_at)}"[:100])
            for c in clients[:25]
        ]
        super().__init__(placeholder="Choose a client ID to connect", options=options)
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        view: ClientSelectView = self.view  # type: ignore[assignment]
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message("Only the person who started the connection can choose.", ephemeral=True)
            return

        client_id = self.values[0]
        try:
            pending = self.bot.registry.start_pairing(
                client_id, interaction.user.id, str(interaction.user), interaction.channel_id)
        except PairingError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        client = self.bot.registry.get(client_id)
        try:
            await client.send(type="pair_request", user=str(interaction.user),
                              channel=getattr(interaction.channel, "name", ""),
                              expires_in=CODE_TTL_SECONDS)
        except Exception:
            self.bot.registry.cancel_pairing(client_id)
            await interaction.response.send_message(f"Client {client_id} is offline.", ephemeral=True)
            return

        self.disabled = True
        view.stop()
        await interaction.response.edit_message(
            content=f"Selected **{client_id}**. The pairing code was sent privately to {interaction.user.mention}.",
            view=view)
        await interaction.followup.send(
            f"Your pairing code: **{pending.code}**\n"
            f"Enter it in the **{client_id}** client window. Valid for {CODE_TTL_SECONDS // 60} minutes.",
            ephemeral=True)


class ClientSelectView(discord.ui.View):
    def __init__(self, bot: "MeeseeksBot", owner_id: int, clients: list[ClientConn]):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.message: discord.Message | None = None
        self.add_item(ClientSelect(bot, clients))

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(content="Selection timed out. Please @mrmeeseeks again.", view=self)
            except discord.HTTPException:
                pass


# ---------------- Bot 本体 ----------------
class MeeseeksBot(discord.Client):
    watch_ui: WatchUI | None = None          # 一起看：没设置 WATCH_PUBLIC_URL 时为 None
    watch_sources: Sources | None = None
    vod_search: VodSearch | None = None
    watch_server: WatchServer | None = None
    qa_log: QaLog | None = None              # 英雄问答记录（STATE_DIR/qa_log.jsonl）
    report_channel_id: int | None = None     # 私信汇报频道（QA_REPORT_CHANNEL_ID）
    progress: ProgressStore | None = None
    tv_table: ChannelTable | None = None     # 电视直播的频道表：WATCH_TV_LISTS 为空时为 None
    _watch_ready = False

    def __init__(self, registry: Registry, analyzer: Analyzer | None, recognizer: Recognizer,
                 labels: LabelStore, roster: Roster,
                 store: HeroStore | None = None, hero_qa: HeroQA | None = None,
                 esports_feed: EsportsFeed | None = None,
                 wish_channel_id: int | None = None, wish_desk: WishDesk | None = None,
                 wish_plans: WishPlans | None = None, patch_feed: PatchFeed | None = None,
                 regions: RegionStore | None = None, live_watcher: LiveWatcher | None = None,
                 live_notify_offline: bool = False, predictions: pr.PredictionStore | None = None,
                 prediction_ui: PredictionUI | None = None, announce_desk: AnnounceDesk | None = None,
                 watch_ui: WatchUI | None = None, watch_sources: Sources | None = None,
                 vod_search: VodSearch | None = None, watch_server: WatchServer | None = None,
                 progress: ProgressStore | None = None, tv_table: ChannelTable | None = None,
                 qa_log: QaLog | None = None, report_channel_id: int | None = None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
        self.registry = registry
        self.analyzer = analyzer
        self.recognizer = recognizer
        self.labels = labels
        self.roster = roster
        self.names = {key: name for heroes in roster.values() for key, name in heroes}
        self.situations: dict[int, Situation] = defaultdict(Situation)
        self.overfast = overfast.OverFast()
        self.store = store
        self.hero_qa = hero_qa
        self.ask_cooldown = Cooldown(ASK_COOLDOWN_SECONDS)
        self.esports_feed = esports_feed
        self.patch_feed = patch_feed
        self.regions = regions if regions is not None else RegionStore(None)
        self.live_watcher = live_watcher
        self.live_notify_offline = live_notify_offline
        if live_watcher is not None:
            live_watcher.notify = self._live_notify
        self.wish_channel_id = wish_channel_id
        self.wish_desk = wish_desk
        self.wish_plans = wish_plans
        self.wish_view = WishEntryView(wish_desk) if wish_desk else None
        self._wish_entry_ready = False
        self.predictions = predictions
        self.prediction_ui = prediction_ui
        self.announce_desk = announce_desk
        self.watch_ui = watch_ui
        self.watch_sources = watch_sources
        self.vod_search, self.watch_server, self.progress = vod_search, watch_server, progress
        self.tv_table = tv_table
        self.qa_log, self.report_channel_id = qa_log, report_channel_id
        if watch_ui is not None:
            watch_ui.edit = self.edit_watch
            watch_ui.create_vod_room = self._create_vod_room
            watch_ui.create_tv_room = self._create_tv_room

    def hero_name(self, key: str) -> str:
        return self.names.get(key) or overfast.hero_name(key)

    def _owners(self, channel_id: int) -> set[int]:
        return {c.user_id for c in self.registry.in_channel(channel_id) if c.user_id}

    async def setup_hook(self) -> None:
        if self.prediction_ui:
            PredictButton.handler = self.prediction_ui
            self.add_dynamic_items(PredictButton)   # 竞猜按钮：重启前发的竞猜消息照样能点
        if self.wish_view:
            self.add_view(self.wish_view)     # 持久化按钮：重启前发的入口消息照样能点
        if self.announce_desk:
            LangButton.handler = self.announce_desk
            self.add_dynamic_items(LangButton)      # 公告的语言按钮：重启前发的公告照样能点
        if self.watch_ui:
            PickSelect.handler = self.watch_ui
            RequestButton.handler = self.watch_ui
            self.watch_ui.create_vod_room = self._create_vod_room
            self.watch_ui.create_tv_room = self._create_tv_room
            # 一起看的搜索选单、「没有我想看的」/「我正在寻找这一部影视资源」按钮
            self.add_dynamic_items(PickSelect, RequestButton)

    async def on_ready(self) -> None:
        log.info("Logged in to Discord as %s", self.user)
        # on_ready 断线重连后会再次触发，入口消息只检查一次
        if self.wish_view and self.wish_channel_id and not self._wish_entry_ready:
            self._wish_entry_ready = True
            try:
                await ensure_entry(await self._channel(self.wish_channel_id), self.wish_desk.quota, self.wish_view)
            except discord.HTTPException:
                log.exception("Could not set up the wish channel %s", self.wish_channel_id)
        # 房间只在内存里：重启前发的一起看卡片都改成"已结束"，只做一次
        if self.watch_ui and not self._watch_ready:
            self._watch_ready = True
            await self.watch_ui.end_stale()

    async def _channel(self, channel_id: int):
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        return channel

    # ---------- 指令 ----------
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.user is None:
            return
        # 许愿子区里 @ 审核人（没 @ bot）= 请求最终审核，把开发者备注私信给审核人
        if (self.wish_plans and self._in_wish_thread(message.channel) and self.user not in message.mentions
                and self.wish_plans.requests_review(message)):
            await self.wish_plans.review_requested(message)
            return
        dm = message.guild is None      # 私信：每条消息都是指令，不用 @
        if self.user not in message.mentions and not dm:
            return
        if message.channel.id == self.wish_channel_id:
            return      # 许愿频道只走表单，不响应 @ 指令
        text = message.content
        tokens = (f"<@{self.user.id}>", f"<@!{self.user.id}>")
        explicit = dm or any(t in text for t in tokens)
        for token in tokens:
            text = text.replace(token, "")
        if self._in_wish_thread(message.channel):
            if explicit:
                await self._wish_thread(message, text)
            return
        cmd, arg = parse_command(text, explicit)
        if dm and not text.strip():
            cmd = "help"        # 私信里只发了图片、贴纸等：显示帮助，不进入配对
        if dm and cmd != "ask":
            await self._report(qa_log.dm_report(message.author.id, message.author.display_name, text))
        if dm and (cmd in GUILD_ONLY or (cmd == "predictions" and arg.strip().lower() == "reset")):
            await message.reply(DM_GUILD_ONLY)
            return

        if cmd == "connect":
            await self._connect_flow(message)
        elif cmd == "status":
            await self._status(message)
        elif cmd == "disconnect":
            await self._disconnect(message)
        elif cmd == "player":
            await self._player(message, arg)
        elif cmd == "esports":
            await self._esports(message, arg)
        elif cmd == "predictions":
            await self._predictions(message, arg)
        elif cmd == "patches":
            await self._patches(message, arg)
        elif cmd == "region":
            await self._region(message, arg)
        elif cmd == "live":
            await self._live(message, arg)
        elif cmd == "analyze":
            await self._reanalyze(message)
        elif cmd == "label":
            await self._label(message)
        elif cmd == "wishes":
            await self._export_wishes(message, arg)
        elif cmd == "announce":
            await self._announce(message)
        elif cmd in ("一起看", "watch"):
            await self._watch(message, arg)
        elif cmd == "help":
            await message.reply(HELP_TEXT)
        else:
            await self._ask(message, arg)

    async def _esports(self, message: discord.Message, arg: str) -> None:
        feed = self.esports_feed
        if feed is None:
            await message.reply("The esports feed is not enabled on this server.")
            return
        action = arg.strip().lower()
        if action in ("here", "off"):
            perms = message.channel.permissions_for(message.author) if message.guild else None
            if perms is None or not perms.manage_channels:
                await message.reply("You need Manage Channels permission to change the esports channel.")
                return
            if action == "here" and not message.channel.permissions_for(message.guild.me).send_messages:
                await message.reply("I can't send messages in this channel — give me Send Messages permission first.")
                return
            feed.set_channel(message.channel.id if action == "here" else None)
            await message.reply("✅ OWCS schedule, reminders, results and news will be posted in this channel."
                                if action == "here" else "Esports posts are turned off.")
        elif action == "status":
            where = f"<#{feed.channel_id}>" if feed.channel_id else "not set (use `@mrmeeseeks esports here`)"
            lines = [f"Esports channel: {where}"]
            if feed.channels:
                lines.append("Region channels: " + ", ".join(f"{r} → <#{c}>" for r, c in sorted(feed.channels.items())))
            for m in feed.upcoming():
                dest = feed.channel_for(m)
                lines.append(f"• <t:{m['start']}:f> {m['team1']} vs {m['team2']} · {m['label']}"
                             + (f" → <#{dest}>" if dest else ""))
            await message.reply("\n".join(lines)[:1990])
        elif action == "preview":
            # 测试入口：立刻在当前频道发一组用真实数据生成的样例，不记账、不影响自动推送的去重
            if not self._can_manage(message):
                await message.reply("You need Manage Channels permission to preview esports posts.")
                return
            posts = feed.preview(message.channel.id)
            if not posts:
                reason = ("match data hasn't been fetched yet — try again in a minute" if feed.matches is None
                          else "no upcoming or recent official matches are routed here "
                               "(check `@mrmeeseeks esports status` and `region`)")
                await message.reply(f"No matches routed to this channel right now: {reason}.")
                return
            for text in posts:
                await message.channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            # 开了竞猜时，再发一条按钮禁用的竞猜样例（画对阵图放到线程里，不卡事件循环）
            sample = await asyncio.to_thread(feed.preview_prediction, message.channel.id)
            if sample:
                text, spec, card = sample
                kwargs = {"file": discord.File(io.BytesIO(card), filename="matchup.png")} if card else {}
                await message.channel.send(text, view=build_view(spec), allowed_mentions=discord.AllowedMentions.none(),
                                           **kwargs)
        else:
            await message.reply("Usage: `@mrmeeseeks esports here`, `esports off`, `esports status` or `esports preview`.")

    # ---------- 赛区频道 ----------
    def _can_manage(self, message: discord.Message, perm: str = "manage_channels") -> bool:
        perms = message.channel.permissions_for(message.author) if message.guild else None
        return perms is not None and getattr(perms, perm, False)

    async def _region(self, message: discord.Message, arg: str) -> None:
        words = arg.split()
        if not words or words[0].lower() in ("list", "status"):
            if not self.regions.channels:
                await message.reply("No region channels yet. In the channel for a region, run `@mrmeeseeks region cn` "
                                    f"(known regions: {', '.join(KNOWN_REGIONS)}).")
                return
            lines = [f"**{r}** → <#{c}>" for r, c in sorted(self.regions.channels.items())]
            lines.append("Matches whose region has no channel, and news, go to the `esports here` channel.")
            await message.reply("\n".join(lines))
            return
        try:
            region = normalize_region(words[0])
        except RegionError as e:
            await message.reply(f"❌ {e}")
            return
        if not self._can_manage(message):
            await message.reply("You need Manage Channels permission to bind a region to a channel.")
            return
        if len(words) > 1 and words[1].lower() in ("off", "none", "remove"):
            await message.reply(f"Region **{region}** is no longer bound to a channel." if self.regions.remove(region)
                                else f"Region **{region}** had no channel.")
            return
        if not message.channel.permissions_for(message.guild.me).send_messages:
            await message.reply("I can't send messages in this channel — give me Send Messages permission first.")
            return
        self.regions.set(region, message.channel.id)
        n = sum(1 for s in self.live_watcher.store.sources.values() if s.region == region) if self.live_watcher else 0
        note = "" if region in KNOWN_REGIONS else (
            f"\n⚠️ `{region}` is not a region Liquipedia uses ({', '.join(KNOWN_REGIONS)}), "
            "so OWCS matches won't be routed here automatically; live rooms added with this region will.")
        await message.reply(f"✅ **{region}** matches and live alerts will be posted in <#{message.channel.id}>"
                            f" ({n} live room(s) registered).{note}")

    # ---------- 开播通知 ----------
    async def _live(self, message: discord.Message, arg: str) -> None:
        watcher = self.live_watcher
        if watcher is None:
            await message.reply("Live alerts are not enabled on this server.")
            return
        words = arg.split()
        sub = words[0].lower() if words else "list"
        rest = words[1:]
        try:
            if sub == "add" and len(rest) >= 2:
                if not self._can_manage(message, "manage_messages"):
                    await message.reply("You need Manage Messages permission to add live rooms.")
                    return
                region = normalize_region(rest[0])
                target = parse_bilibili_target(rest[1])
                if target is None:
                    raise LiveError(f"`{rest[1]}` is not a Bilibili live room URL or room id.")
                room_id = await watcher.client.resolve_room(target)
                status = (await watcher.client.statuses([room_id])).get(room_id)
                label = " ".join(rest[2:]).strip() or (status.uname if status else "") or f"room {room_id}"
                src = Source(platform="bilibili", room_id=room_id, region=region, label=label,
                             url=f"https://live.bilibili.com/{room_id}", added_by=str(message.author))
                replaced = src.key in watcher.store.sources
                watcher.store.add(src)
                watcher.forget(src.key)
                if status is not None:   # 当前状态作为基线，刚添加时不发通知
                    watcher.state[src.key] = status.live
                    watcher.last_statuses[room_id] = status
                where = self.regions.get(region)
                lines = [f"{'🔁 Updated' if replaced else '✅ Watching'} **{label}** (`{src.key}`) for region **{region}**."]
                if status is not None:
                    lines.append(f"Currently: {'🔴 LIVE — ' + status.title if status.live else '⚫ offline'}")
                lines.append(f"Alerts go to <#{where}>." if where else
                             f"⚠️ Region `{region}` has no channel yet: run `@mrmeeseeks region {region}` in the channel "
                             "that should receive alerts.")
                await message.reply("\n".join(lines))
            elif sub == "remove" and rest:
                if not self._can_manage(message, "manage_messages"):
                    await message.reply("You need Manage Messages permission to remove live rooms.")
                    return
                target = parse_bilibili_target(rest[0])
                if target is None:
                    raise LiveError(f"`{rest[0]}` is not a Bilibili live room URL or room id.")
                key = f"bilibili:{target}"
                src = watcher.store.remove(key)
                if src is None:   # 可能给的是短号
                    try:
                        key = f"bilibili:{await watcher.client.resolve_room(target)}"
                        src = watcher.store.remove(key)
                    except LiveError:
                        src = None
                if src is None:
                    await message.reply(f"Not watching `{rest[0]}`. See `@mrmeeseeks live list`.")
                    return
                watcher.forget(key)
                await message.reply(f"🗑️ Stopped watching **{src.label}** (`{key}`, region {src.region}).")
            elif sub == "list":
                groups = watcher.store.by_region()
                if not groups:
                    await message.reply("No live rooms registered yet.\n" + LIVE_USAGE)
                    return
                lines = []
                for region, sources in sorted(groups.items()):
                    ch = self.regions.get(region)
                    where = f"<#{ch}>" if ch else f"⚠️ no channel (`@mrmeeseeks region {region}`)"
                    lines.append(f"**{region}** → {where}")
                    for src in sources:
                        st = watcher.last_statuses.get(src.room_id)
                        lines.append(f"  {'🔴' if st and st.live else '⚫'} {src.label} · <{src.url}>")
                if watcher.last_error:
                    lines.append(f"⚠️ Last poll failed: {watcher.last_error}")
                await message.reply("\n".join(lines)[:1990])
            else:
                await message.reply(LIVE_USAGE)
        except (LiveError, RegionError) as e:
            await message.reply(f"❌ {e}")

    async def _live_notify(self, ev: LiveEvent) -> None:
        """LiveWatcher 的回调：按赛区找频道发开播消息。没绑频道的赛区只记日志。"""
        channel_id = self.regions.get(ev.source.region)
        if channel_id is None:
            log.info("Region %s has no channel; skipping %s event for %s", ev.source.region, ev.kind, ev.source.key)
            return
        await self.wait_until_ready()
        channel = await self._channel(channel_id)
        src, st = ev.source, ev.status
        if ev.kind == "live":
            embed = discord.Embed(title=f"🔴 {src.label} is live", url=st.url, description=st.title or None,
                                  color=0xFB7299, timestamp=datetime.now(timezone.utc))
            embed.set_author(name=f"{src.region.upper()} · Bilibili")
            if st.cover:
                embed.set_image(url=st.cover)
            if st.area:
                embed.set_footer(text=st.area)
            await channel.send(f"**{src.label}** just went live: {st.url}", embed=embed,
                               allowed_mentions=discord.AllowedMentions.none())
        elif self.live_notify_offline:
            await channel.send(f"⚫ **{src.label}** ({src.region.upper()}) ended the stream.",
                               allowed_mentions=discord.AllowedMentions.none())

    async def post_esports(self, channel_id: int, text: str, view: dict | None = None,
                           card: bytes | None = None) -> tuple[int, str]:
        """推送任务用的发送函数：等 bot 登录完成再发，不 ping 任何人。view 是竞猜按钮的描述（prediction_feed.view_spec），
        card 是对阵图 PNG。返回 (消息 ID, 消息链接)，竞猜消息据此登记。"""
        await self.wait_until_ready()
        channel = await self._channel(channel_id)
        kwargs: dict = {"view": build_view(view)} if view else {}
        if card:
            kwargs["file"] = discord.File(io.BytesIO(card), filename="matchup.png")
        msg = await channel.send(text, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        return msg.id, msg.jump_url

    async def edit_esports(self, channel_id: int, message_id: int, view: dict) -> None:
        """竞猜截止 / 作废时更新原消息的按钮。"""
        await self.wait_until_ready()
        channel = await self._channel(channel_id)
        await channel.get_partial_message(message_id).edit(view=build_view(view))

    async def edit_watch(self, channel_id: int, message_id: int, text: str, view: discord.ui.View | None) -> None:
        """一起看卡片的编辑函数（WatchUI.edit）；view=None 去掉按钮。"""
        await self.wait_until_ready()
        channel = await self._channel(channel_id)
        await channel.get_partial_message(message_id).edit(content=text, view=view,
                                                           allowed_mentions=discord.AllowedMentions.none())

    async def _watch(self, message: discord.Message, arg: str) -> None:
        ui, sources = self.watch_ui, self.watch_sources
        if ui is None or sources is None:
            await message.reply("一起看功能没有启用。")
            return
        if find_link(arg) is None:
            channels: list[Channel] = []
            if self.tv_table is not None and self.watch_server is not None and len(arg.strip()) >= 2:
                m = self.tv_table.match(arg)
                if m.exact is not None:          # 台名精确匹配：直接开电视房间
                    await self._open_tv(message, m.exact)
                    return
                channels = m.partial
            await self._watch_search(message, arg, channels)
            return
        try:
            video = await sources.resolve(arg)
            room = ui.rooms.create(message.channel.id, message.author.id, message.author.display_name, video)
        except VideoUnavailable as e:
            await message.reply(str(e))
            return
        except RoomsFull as e:
            await message.reply(f"B 站房间已满（最多 {e.limit} 个），请稍后再试或换 YouTube 视频")
            return
        delivered = await self._send_host_link(room, message.author)
        try:
            card = await message.reply(card_text(room, ui.rooms.clock()), view=build_watch_view(room, ui.public_url),
                                       allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Could not post the watch card in channel %s", message.channel.id)
            ui.rooms.close(room.room_id)   # 没有卡片就没人进得来，直接关掉，别占着 B 站名额
            return
        room.message_id = card.id
        ui.cards.add(message.channel.id, card.id)
        if not delivered:
            try:
                await message.reply(HOST_LINK_FAILED, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                log.exception("Could not post the host-link notice in channel %s", message.channel.id)

    async def _send_host_link(self, room, user: discord.abc.User, interaction: discord.Interaction | None = None) -> bool:
        """把房主链接单独发给发起人：有交互就 ephemeral 回复，否则（或失败时）私信。发不出去返回 False。"""
        text = host_link_text(self.watch_ui.public_url, room)
        if interaction is not None:
            try:
                await interaction.followup.send(text, ephemeral=True)
                return True
            except discord.HTTPException:
                log.info("Ephemeral host link failed for room %s, trying a DM", room.room_id)
        try:
            await user.send(text)
            return True
        except discord.HTTPException:      # 私信关了（Forbidden）等
            log.info("Could not DM the host link for room %s to user %s", room.room_id, user.id)
            return False

    async def _watch_search(self, message: discord.Message, arg: str, channels: Sequence[Channel] = ()) -> None:
        """按片名搜影视；channels 是部分匹配的电视台，和影视结果放在同一个选单里（台在前）。"""
        ui, search = self.watch_ui, self.vod_search
        channels = list(channels)
        if search is None or self.watch_server is None:
            search = None
            if not channels:
                await message.reply("按片名搜索没有启用，请贴 B 站或 YouTube 链接")
                return
        if not 1 <= len(arg) <= 50:
            await message.reply(WATCH_USAGE)
            return
        shows = []
        if search is not None:
            try:
                shows = await search.search(arg)
            except SearchUnavailable as e:
                if not channels:
                    await message.reply(e.message)
                    return
        if not shows and not channels:
            await message.reply(f"没找到「{arg}」", view=build_want_view(arg),
                                allowed_mentions=discord.AllowedMentions.none())
            return
        p = PendingSearch(secrets.token_urlsafe(12), message.author.id, arg, shows, message.channel.id, None,
                          message, ui.clock(), channels=channels)
        try:
            sent = await message.reply(search_header(arg, message.author.mention),
                                       view=build_pick_view(p, self.progress),
                                       embeds=build_pick_embeds(shows),
                                       allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Could not post the search results in channel %s", message.channel.id)
            return
        p.message_id = sent.id
        ui.add_search(p)

    async def _create_vod_room(self, p: PendingSearch, index: int) -> None:
        ui, server = self.watch_ui, self.watch_server
        show = p.shows[index]
        resume = self.progress.get(p.channel_id, show.key) if self.progress is not None else None
        try:
            video, playlist = await server.prepare_vod(show, 0, resume=resume)
            room = ui.rooms.create(p.channel_id, p.requester_id, p.origin_message.author.display_name, video)
        except PlaylistError as e:
            await self._edit_search(p, e.text)      # 带上每条线路不能播的原因
            return
        except Exception:
            log.exception("Could not prepare a vod room for %r", show.title)
            await self._edit_search(p, "出错了，请稍后再试")
            return
        server.attach_playlist(room, playlist)
        delivered = await self._send_host_link(room, p.origin_message.author, p.interaction)
        try:
            card = await p.origin_message.reply(card_text(room, ui.rooms.clock()),
                                                view=build_watch_view(room, ui.public_url),
                                                allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Could not post the watch card in channel %s", p.channel_id)
            ui.rooms.close(room.room_id)
            await self._edit_search(p, "出错了，请稍后再试")
            return
        room.message_id = card.id
        ui.cards.add(p.channel_id, card.id)
        await self._edit_search(p, f"已选择：{show.title}" + ("" if delivered else f"\n{HOST_LINK_FAILED}"))

    async def _open_tv(self, message: discord.Message, channel: Channel) -> None:
        """台名精确匹配：先回一条「正在准备…」，探到能播的源再建房发卡片，最后把那条改成结果。"""
        try:
            self.watch_ui.rooms.check_tv()       # 名额已满就别让人等探测
        except TvRoomsFull as e:
            await message.reply(str(e), allowed_mentions=discord.AllowedMentions.none())
            return
        try:
            status = await message.reply(PICK_PREPARING, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Could not reply in channel %s", message.channel.id)
            return

        async def set_status(text: str) -> None:
            try:
                await self.watch_ui.edit(message.channel.id, status.id, text, None)
            except Exception:
                log.exception("Could not edit message %s", status.id)
        await self._start_tv(message, channel, set_status)

    async def _create_tv_room(self, p: PendingSearch, channel: Channel) -> None:
        """搜索选单里选了台：和 _open_tv 一样，只是结果写回搜索消息。"""
        async def set_status(text: str) -> None:
            await self._edit_search(p, text)
        await self._start_tv(p.origin_message, channel, set_status, p.interaction)

    async def _start_tv(self, origin: discord.Message, channel: Channel,
                        set_status: Callable[[str], Awaitable[None]],
                        interaction: discord.Interaction | None = None) -> None:
        ui, server = self.watch_ui, self.watch_server
        try:
            ui.rooms.check_tv()                  # 探测前先查名额；create 里还会再查一次（防并发）
            video = await server.prepare_tv(channel)
            room = ui.rooms.create(origin.channel.id, origin.author.id, origin.author.display_name, video)
        except TvUnavailable as e:
            await set_status(e.message)
            return
        except TvRoomsFull as e:
            await set_status(str(e))
            return
        except Exception:
            log.exception("Could not prepare a tv room for %r", channel.name)
            await set_status("出错了，请稍后再试")
            return
        delivered = await self._send_host_link(room, origin.author, interaction)
        try:
            card = await origin.reply(card_text(room, ui.rooms.clock()), view=build_watch_view(room, ui.public_url),
                                      allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Could not post the watch card in channel %s", origin.channel.id)
            ui.rooms.close(room.room_id)   # 没有卡片就没人进得来，直接关掉，别占着直播名额
            await set_status("出错了，请稍后再试")
            return
        room.message_id = card.id
        ui.cards.add(origin.channel.id, card.id)
        await set_status(f"已选择：📺 {channel.name}" + ("" if delivered else f"\n{HOST_LINK_FAILED}"))

    async def _edit_search(self, p: PendingSearch, text: str) -> None:
        """改搜索结果消息（去掉选单），尽力而为：失败只记日志。"""
        try:
            await self.watch_ui.edit(p.channel_id, p.message_id, text, None)
        except Exception:
            log.exception("Could not edit search message %s", p.message_id)

    async def _predictions(self, message: discord.Message, arg: str) -> None:
        store = self.predictions
        if store is None:
            await message.reply("Predictions are not enabled on this server.")
            return
        now = time.time()
        if arg.strip().lower() == "reset":
            perms = message.channel.permissions_for(message.author) if message.guild else None
            if perms is None or not perms.manage_guild:
                await message.reply("You need Manage Server permission to start a new prediction season.")
                return
            store.new_season(now)
            await message.reply("🏁 A new prediction season has started. The weekly leaderboard is unaffected.")
            return
        week = week_start(datetime.now(timezone.utc)).timestamp()
        weekly = store.leaderboard(week, now + 1)
        season = store.leaderboard(store.season_start(), now + 1)
        lines = pr.format_board("🏆 **This week**", weekly) + [""] + pr.format_board("🏅 **Season**", season)
        mine = next((i for i, r in enumerate(season, 1) if r["user_id"] == message.author.id), None)
        if mine:
            r = season[mine - 1]
            lines.append(f"You: #{mine} — {r['points']:g} pts ({r['correct']}/{r['played']})")
        await message.reply("\n".join(lines)[:1990], allowed_mentions=discord.AllowedMentions.none())

    post_patches = post_esports      # 补丁推送用同一个发送函数

    async def _patches(self, message: discord.Message, arg: str) -> None:
        feed = self.patch_feed
        if feed is None:
            await message.reply("The patch notes feed is not enabled on this server.")
            return
        action = arg.strip().lower()
        if action in ("here", "off"):
            perms = message.channel.permissions_for(message.author) if message.guild else None
            if perms is None or not perms.manage_channels:
                await message.reply("You need Manage Channels permission to change the patch notes channel.")
                return
            if action == "off":
                feed.set_channel(None)
                await message.reply("Patch notes posts are turned off.")
                return
            if not message.channel.permissions_for(message.guild.me).send_messages:
                await message.reply("I can't send messages in this channel — give me Send Messages permission first.")
                return
            moved = feed.channel_id != message.channel.id
            feed.set_channel(message.channel.id)
            if not moved:
                await message.reply("✅ Patch notes are already posted in this channel.")
                return
            await message.reply("✅ New official Overwatch patch notes will be summarized in this channel. "
                                "Here's the latest one:")
            try:
                async with message.channel.typing():
                    latest = await feed.post_latest(message.channel.id, self.post_patches)
            except Exception:
                log.exception("Could not post the latest patch to %s", message.channel.id)
                latest = None
            if latest is None:
                await message.reply("I couldn't load the official patch notes right now; "
                                    "new patches will still be posted here.")
        elif action == "status":
            where = f"<#{feed.channel_id}>" if feed.channel_id else "not set (use `@mrmeeseeks patches here`)"
            lines = [f"Patch notes channel: {where}"]
            if feed.patches:
                latest = feed.patches[0]
                lines.append(f"Latest official patch: [{latest['title']}](<{latest['url']}>)")
            if feed.state.get("checked_at"):
                lines.append(f"Last checked: {feed.state['checked_at'].replace('T', ' ')} UTC")
            await message.reply("\n".join(lines))
        else:
            await message.reply("Usage: `@mrmeeseeks patches here`, `patches off` or `patches status`.")

    # ---------- 功能许愿 ----------
    def _in_wish_thread(self, channel) -> bool:
        # 只有子区有 parent_id
        return self.wish_channel_id is not None and getattr(channel, "parent_id", None) == self.wish_channel_id

    async def _wish_thread(self, message: discord.Message, text: str) -> None:
        if self.wish_plans is None:
            await message.reply("Drafting plans needs ANTHROPIC_API_KEY to be set.")
            return
        request = text.strip()
        await self.wish_plans.handle(message, "" if request.lower() == "plan" else request)

    async def _export_wishes(self, message: discord.Message, arg: str) -> None:
        if self.wish_desk is None:
            await message.reply("The wish channel isn't set up (WISH_CHANNEL_ID).")
            return
        perms = getattr(message.author, "guild_permissions", None)
        reviewer = self.wish_plans.reviewer_id if self.wish_plans else None
        if not (perms and perms.manage_guild) and message.author.id != reviewer:
            await message.reply("Only admins (Manage Server) can export wishes.")
            return
        game, days = parse_export_args(arg)
        records = select_wishes(self.wish_desk.log.all(), game, days)
        if not records:
            await message.reply("No wishes match.")
            return
        votes: dict[int, int | None] = {}
        async with message.channel.typing():
            channel = await self._channel(self.wish_channel_id)
            for r in records:
                try:
                    votes[r["id"]] = vote_count(await channel.fetch_message(r["id"]))
                except discord.NotFound:
                    votes[r["id"]] = None     # 卡片被删了，存档还在
        name = f"wishes-{time.strftime('%Y%m%d')}.csv"
        try:
            await message.author.send(f"{len(records)} wish(es), most votes first.",
                                      file=discord.File(io.BytesIO(export_csv(records, votes)), name))
        except discord.Forbidden:
            await message.reply("I can't DM you. Allow DMs from server members and try again.")
            return
        await message.reply(f"📬 Sent you {len(records)} wish(es) as a CSV by DM.")

    async def _announce(self, message: discord.Message) -> None:
        if self.announce_desk is None:
            await message.reply("Announcements need ANTHROPIC_API_KEY to be set (for the translations).")
            return
        if not self._can_manage(message, "manage_guild"):
            await message.reply("Only members with **Manage Server** can post announcements.")
            return
        await message.reply("Press the button to write your announcement. I'll translate it and show you a "
                            "preview before anything is posted in this channel.",
                            view=StartView(self.announce_desk, message.author.id))

    async def _ask(self, message: discord.Message, question: str) -> None:
        outcome, reply = await self._answer(message, question)
        await message.reply(reply)
        if self.qa_log is not None:
            self.qa_log.record(user_id=message.author.id, user_name=message.author.display_name,
                               guild_id=message.guild.id if message.guild else None, channel_id=message.channel.id,
                               message_id=message.id, question=question, outcome=outcome, answer=reply)
        if message.guild is None:       # 频道里的问答只记录，不汇报
            await self._report(qa_log.qa_report(message.author.id, message.author.display_name,
                                                question, outcome, reply))

    async def _answer(self, message: discord.Message, question: str) -> tuple[str, str]:
        """(结果, 回复文本)；结果是 qa_log 里的 ANSWERED / COOLDOWN 等。"""
        if self.hero_qa is None:
            return qa_log.NO_API_KEY, "Hero Q&A needs ANTHROPIC_API_KEY to be set."
        if self.store is None or not self.store.ready:
            return qa_log.NOT_READY, "Hero data is not ready yet, try again in a minute."
        if not self.ask_cooldown.allow(message.author.id):
            return qa_log.COOLDOWN, f"Please wait {ASK_COOLDOWN_SECONDS} seconds between questions."
        try:
            async with message.channel.typing():
                answer = await self.hero_qa.answer(question)
        except Exception:
            log.exception("Hero Q&A failed for %r", question)
            return qa_log.ERROR, "Sorry, I couldn't answer that right now. Please try again later."
        return qa_log.ANSWERED, answer[:1990] or "I couldn't come up with an answer."

    async def _report(self, text: str) -> None:
        """发到私信汇报频道，尽力而为：没设置或发不出去只记日志。"""
        if self.report_channel_id is None:
            return
        try:
            channel = await self._channel(self.report_channel_id)
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            log.exception("Could not post a DM report to channel %s", self.report_channel_id)

    async def _player(self, message: discord.Message, arg: str) -> None:
        tag = overfast.normalize_battletag(arg)
        if tag is None:
            await message.reply("Usage: `@mrmeeseeks player Name#1234` (BattleTags are case-sensitive).")
            return
        async with message.channel.typing():
            try:
                summary, stats = await self.overfast.player(tag)
            except overfast.PlayerNotFound:
                log.info("OverFast: player not found: %s", tag)
                # 暴雪对私密生涯也返回 404，OverFast 无法区分"不存在"和"私密"
                await message.reply(
                    f"Player `{tag.replace('-', '#')}` not found. Either:\n"
                    "- the BattleTag is misspelled (it's case-sensitive, e.g. `Name#1234`), or\n"
                    "- the career profile is private: in Overwatch go to Options → Social → "
                    "Career Profile Visibility → Public, then try again in ~10 minutes.")
                return
            except overfast.OverFastUnavailable as e:
                log.warning("OverFast lookup failed for %s: %s", tag, e)
                await message.reply("OverFast is unavailable right now (rate-limited or down). Try again in a minute.")
                return
        await message.reply(overfast.format_player(tag, summary, stats))

    async def _reanalyze(self, message: discord.Message) -> None:
        state = self.situations.get(message.channel.id)
        if state is None or state.last_facts is None:
            await message.reply("Nothing to analyze yet. Hold Tab in game first.")
            return
        if self.analyzer is None:
            await message.reply("ANTHROPIC_API_KEY is not set, so I can't give advice.")
            return
        async with message.channel.typing():
            advice = await self.analyzer.advise(state.last_facts)
        reply = await message.reply(f"{format_facts(state.last_facts)}\n\n{advice}"[:1990])
        state.remember_advice(state.last_facts, reply.jump_url)

    async def _label(self, message: discord.Message) -> None:
        items = self.labels.pending(LABEL_BATCH)
        if not items:
            await message.reply("Nothing to label. 🎉")
            return
        total = len(self.labels.pending())
        await message.reply(f"{total} portrait(s) waiting to be labeled; here are {len(items)}.")
        owners = self._owners(message.channel.id) | {message.author.id}
        await send_prompts(message.channel, self.labels, self.roster, self.hero_name, items, owners)

    async def _connect_flow(self, message: discord.Message) -> None:
        clients = self.registry.available()
        if not clients:
            await message.reply("No clients available. Run the client script on your PC first.")
            return
        view = ClientSelectView(self, message.author.id, clients)
        view.message = await message.reply(
            f"{len(clients)} client(s) online. Choose the one to connect:", view=view)

    async def _status(self, message: discord.Message) -> None:
        if not self.registry.clients:
            await message.reply("No clients online.")
            return
        now = time.time()
        lines = []
        for c in sorted(self.registry.clients.values(), key=lambda c: c.connected_at):
            where = f"paired to <#{c.channel_id}> ({c.user_name})" if c.paired else "not paired"
            lines.append(f"`{c.client_id}` {c.hostname} · online for {_age(now - c.connected_at)} · {where}")
        await message.reply("\n".join(lines))

    async def _disconnect(self, message: discord.Message) -> None:
        clients = self.registry.in_channel(message.channel.id)
        if not clients:
            await message.reply("No client is paired to this channel.")
            return
        for c in clients:
            self.registry.unpair(c.client_id)
            try:
                await c.send(type="unpaired", reason=f"{message.author} disconnected it from Discord")
            except Exception:
                pass
        await message.reply("Disconnected: " + ", ".join(f"`{c.client_id}`" for c in clients))

    # ---------- WebSocket 事件（ws_server.Events） ----------
    async def client_paired(self, client: ClientConn) -> None:
        channel = await self._channel(client.channel_id)
        await channel.send(f"✅ Client **{client.client_id}** ({client.hostname}) connected. "
                           f"Hold Tab in game and release it, and I'll start analyzing.")

    async def pair_failed(self, client: ClientConn, channel_id: int, reason: str) -> None:
        channel = await self._channel(channel_id)
        await channel.send(f"❌ Pairing failed for client **{client.client_id}**: {reason}")

    async def client_disconnected(self, client: ClientConn) -> None:
        if client.paired:
            channel = await self._channel(client.channel_id)
            await channel.send(f"⚠️ Client **{client.client_id}** disconnected.")

    async def snapshot_received(self, client: ClientConn, scoreboard: bytes, hud: bytes) -> str:
        channel = await self._channel(client.channel_id)
        rec = await asyncio.to_thread(self.recognizer.recognize, scoreboard, hud)
        log.info("Recognized %s in %.2fs: table=%s map=%s side=%s stage=%s unknown=%d", client.client_id,
                 rec.elapsed, rec.table_found, rec.map and rec.map.en, rec.side, rec.stage, len(rec.unknowns))
        if not rec.table_found:
            await channel.send("👀 Couldn't see the scoreboard in that screenshot. "
                               "Hold Tab a little longer (about a second) and try again.")
            return "Couldn't see the scoreboard; hold Tab a bit longer"

        state = self.situations[client.channel_id]
        facts = state.update(to_facts(rec, self.hero_name))
        to_prompt = []
        for slot in rec.unknowns:
            pid, prompt = self.labels.add(slot.crop, {"team": slot.team, "row": slot.row, "player": slot.player,
                                                      "score": round(slot.score, 2), "at": time.time()})
            if prompt:
                to_prompt.append(pid)

        if state.should_skip(facts):
            await channel.send(f"⏸️ No change ({format_facts(facts).splitlines()[0]}, same heroes) — "
                               f"previous advice still applies: {state.advice_url}")
            return "No change; previous advice still applies"

        files = [discord.File(io.BytesIO(scoreboard), "scoreboard.jpg"),
                 discord.File(io.BytesIO(hud), "hud.jpg")]
        msg = await channel.send(f"🔍 {format_facts(facts)}", files=files)
        summary = "Recognized (advice disabled: ANTHROPIC_API_KEY not set)"
        if self.analyzer is not None:
            advice = await self.analyzer.advise(facts)
            reply = await msg.reply(advice[:1990])
            state.remember_advice(facts, reply.jump_url)
            summary = advice.splitlines()[0] if advice else ""

        prompts = [p for p in (self.labels.get(pid) for pid in to_prompt[:AUTO_PROMPTS]) if p]
        if prompts:
            await send_prompts(channel, self.labels, self.roster, self.hero_name, prompts,
                               self._owners(client.channel_id))
        return summary


async def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_TOKEN is missing: copy server/.env.example to server/.env and fill it in.")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    model = os.environ.get("CLAUDE_MODEL", "").strip() or "claude-sonnet-5"
    effort = os.environ.get("ADVISE_EFFORT", "low").strip() or None

    state_dir = Path(os.environ.get("STATE_DIR", DATA_DIR.parent / "state"))
    matcher = HeroMatcher([DATA_DIR / "templates", state_dir / "templates"])
    labels = LabelStore(state_dir, matcher)
    recognizer = Recognizer(matcher, load_maps(DATA_DIR / "maps.json"))
    await asyncio.to_thread(recognizer.ocr, np.zeros((32, 32, 3), np.uint8))   # 预加载 OCR 模型
    log.info("Loaded %d hero templates (%d labels); %d portraits waiting to be labeled",
             len(matcher.templates), len(matcher.labels), len(labels.pending()))
    bundled = json.loads((DATA_DIR / "roster.json").read_text(encoding="utf-8"))
    try:
        heroes = merge_heroes(await overfast.OverFast().heroes(), bundled)
    except (overfast.OverFastUnavailable, overfast.PlayerNotFound) as e:
        log.warning("Could not fetch hero list from OverFast (%s); using bundled list", e)
        heroes = bundled
    roster = build_roster(heroes)

    server_dir = os.path.dirname(os.path.abspath(__file__))
    store = HeroStore(os.environ.get("HERO_DATA_PATH", "").strip() or os.path.join(server_dir, "herodata", "heroes.json"),
                      load_aliases())
    store.load()
    # 对位数据和英雄数据放在同一个目录（Docker 里是 herodata 卷）
    matchup_store = matchups.MatchupStore(os.path.join(os.path.dirname(os.path.abspath(store.path)), "matchups.json"))
    matchup_store.load()
    # 补丁推送状态也放在 herodata 目录（Docker 里是 herodata 卷）
    keys = KeyBinds(store, load_manual_keys())
    if keys.missing():
        log.warning("No default key known for %d abilities (add them to server/keybinds.json): %s",
                    len(keys.missing()), ", ".join(f"{h} / {a}" for h, a in keys.missing()))
    summarizer = (PatchSummarizer(AsyncAnthropic(api_key=api_key), model, effort, chinese_names(load_aliases()),
                                  keys)
                  if api_key else None)
    patch_feed = PatchFeed(os.path.join(os.path.dirname(os.path.abspath(store.path)), "patch_state.json"),
                           summarizer=summarizer)
    patch_feed.load()
    hero_qa = (HeroQA(AsyncAnthropic(api_key=api_key), model, store, effort=effort, matchups=matchup_store,
                      patches=patch_feed)
               if api_key else None)
    analyzer = (Analyzer(api_key, model, effort, matchups=matchup_store, roles=roles_from_roster(roster))
                if api_key else None)
    if analyzer is None:
        log.warning("ANTHROPIC_API_KEY not set; screenshots will be recognized but no advice given")
    else:
        log.info("Advice model: %s (effort %s)", model, effort or "default")
    refresh_hours = float(os.environ.get("HERO_REFRESH_HOURS", "24"))

    # 赛区 -> 频道的绑定和手动登记的直播源放在 STATE_DIR（Docker 里是挂载的 /state）
    regions = RegionStore(state_dir / "regions.json")
    regions.load()
    live_store = LiveStore(state_dir / "live_sources.json")
    live_store.load()
    live_watcher = LiveWatcher(live_store, BilibiliClient(),
                               interval=float(os.environ.get("LIVE_POLL_INTERVAL", "").strip() or DEFAULT_POLL_INTERVAL))
    live_notify_offline = os.environ.get("LIVE_NOTIFY_OFFLINE", "").strip().lower() in ("1", "true", "yes")
    log.info("Regions: %s; %d live room(s) registered", ", ".join(sorted(regions.channels)) or "none",
             len(live_store.sources))

    # 赛事推送状态和英雄数据放在同一个目录（Docker 里是 herodata 卷）
    # 竞猜：押注和积分存 STATE_DIR（用户产生的数据）；赛事报名阵容和队徽缓存和英雄数据放一起（herodata 卷）
    prediction_store = pr.PredictionStore(state_dir / "predictions.db")
    herodata_dir = os.path.dirname(os.path.abspath(store.path))
    roster_cache = RosterCache(os.path.join(herodata_dir, "rosters.json"))
    roster_cache.load()
    logo_cache = LogoCache(os.path.join(herodata_dir, "logos"))
    esports_feed = EsportsFeed(os.path.join(herodata_dir, "esports_state.json"),
                               regions=regions, predictions=PredictionFeed(prediction_store, roster_cache, logo_cache))
    esports_feed.load()

    wish_channel = os.environ.get("WISH_CHANNEL_ID", "").strip()
    wish_desk = wish_plans = None
    if wish_channel:
        wish_desk = WishDesk(WishQuota(state_dir / "wishes.json",
                                       int(os.environ.get("WISH_DAILY_LIMIT", "").strip() or WISH_DAILY_LIMIT)), WishLog(state_dir / "wishes"),
                             parse_games(os.environ.get("WISH_GAMES", "")))
        reviewer = os.environ.get("WISH_REVIEWER_ID", "").strip()
        if api_key:
            features = (HELP_TEXT + "\nAll of the above is for Overwatch only; mrmeeseeks has no features for "
                        "other games yet.")
            wish_plans = WishPlans(WishPlanner(AsyncAnthropic(api_key=api_key), model, effort), wish_desk.log,
                                   features, int(reviewer) if reviewer else None)
            wish_desk.on_posted = wish_plans.draft
        else:
            log.warning("ANTHROPIC_API_KEY not set; wishes will be posted without a plan")

    # 公告的各语言版本存 STATE_DIR（用户产生的数据），语言按钮靠它在重启后继续工作
    announce_desk = (AnnounceDesk(Translator(AsyncAnthropic(api_key=api_key), model, effort),
                                  AnnouncementStore(state_dir / "announcements"))
                     if api_key else None)

    # 一起看：设置了 WATCH_PUBLIC_URL（网页的公网地址）才启用
    watch_url = os.environ.get("WATCH_PUBLIC_URL", "").strip().rstrip("/")
    watch_ui = watch_sources = watch_server = vod_search = progress = tv_table = tv_session = None
    if watch_url:
        sessdata = os.environ.get("BILIBILI_SESSDATA", "").strip()
        if not sessdata:
            log.warning("BILIBILI_SESSDATA not set; B 站将使用未登录画质")
        quality = os.environ.get("WATCH_MAX_QUALITY", "").strip() or "1080"
        if quality not in QUALITY_QN:
            log.warning("Unknown WATCH_MAX_QUALITY %r (use 1080, 720 or 480); using 1080", quality)
            quality = "1080"
        watch_rooms = RoomManager(max_bili_rooms=int(os.environ.get("WATCH_MAX_BILI_ROOMS", "").strip() or "2"),
                                  max_tv_rooms=int(os.environ.get("WATCH_MAX_TV_ROOMS", "").strip() or "2"))
        watch_sources = Sources(sessdata or None, QUALITY_QN[quality])
        vod_sources = parse_sources_env(os.environ.get("WATCH_VOD_SOURCES"))
        vod_search = VodSearch(vod_sources) if vod_sources else None
        progress = ProgressStore(state_dir / "watch_progress.json")
        recorder = ProgressRecorder(watch_rooms, progress)
        # 「没有我想看的」记录 + 管理后台（/admin 输入密钥进入；密钥没配就生成一个存在 STATE_DIR/watch_admin_key）
        watch_requests = RequestStore(state_dir / "watch_requests.json",
                                      int(os.environ.get("WATCH_REQUEST_DAILY_LIMIT", "").strip() or REQUEST_DAILY_LIMIT))
        admin_key = load_admin_key(state_dir / "watch_admin_key", os.environ.get("WATCH_ADMIN_KEY"))
        # 地区限制：默认只放行 AU、NZ；WATCH_ALLOWED_COUNTRIES 设成空字符串就不限制。IP 分配表缓存在 herodata 卷
        allowed = parse_allowed(os.environ.get("WATCH_ALLOWED_COUNTRIES"))
        geo = blocks = None
        if allowed:
            geo = GeoDB(Path(herodata_dir) / "geoip.json", allowed)
            if geo.load():
                log.info("Watch geo restriction: allow %s (%d ranges loaded)", ", ".join(allowed),
                         geo.status()["ranges"])
            else:
                log.info("Watch geo restriction: allow %s; IP data will be downloaded first "
                         "(nobody is blocked until then)", ", ".join(allowed))
            blocks = BlockLog(state_dir / "watch_blocked.json")
        else:
            log.info("Watch geo restriction disabled (WATCH_ALLOWED_COUNTRIES is empty)")
        watch_server = WatchServer(watch_rooms, watch_sources, host=os.environ.get("WATCH_HOST", "").strip() or "0.0.0.0",
                                   port=int(os.environ.get("WATCH_PORT", "").strip() or "8080"),
                                   origin=watch_url, recorder=recorder, admin_key=admin_key, requests=watch_requests,
                                   stats=SystemStats(disk_path=state_dir), geo=geo, blocks=blocks,
                                   admin_password=os.environ.get("WATCH_ADMIN_PASSWORD", "").strip() or None)
        watch_ui = WatchUI(watch_rooms, watch_url, CardStore(state_dir / "watch_cards.json"), requests=watch_requests)
        # 电视直播：WATCH_TV_LISTS 不设用默认列表，设成空字符串关闭
        tv_lists = parse_tv_lists(os.environ.get("WATCH_TV_LISTS"))
        if tv_lists:
            tv_session = tv_net.make_session()
            tv_table = ChannelTable(state_dir / "tv_channels.json", tv_lists,
                                    prober=functools.partial(tv_stream.probe, tv_session))
            log.info("Live TV enabled with %d channel lists", len(tv_lists))
        log.info("Watch together enabled at %s (max quality %s); admin dashboard at %s/admin (WATCH_ADMIN_KEY %s, "
                 "WATCH_ADMIN_PASSWORD %s)", watch_url, quality, watch_url,
                 "set" if os.environ.get("WATCH_ADMIN_KEY", "").strip() else f"unset: generated key in {state_dir / 'watch_admin_key'}",
                 "set" if watch_server.admin_password else "unset: key alone logs in")

    # 私信汇报频道：私信里的问答和指令发到这里；频道里的问答只记进 qa_log.jsonl
    report = os.environ.get("QA_REPORT_CHANNEL_ID", "").strip()
    registry = Registry()
    bot = MeeseeksBot(registry, analyzer, recognizer, labels, roster, store, hero_qa, esports_feed,
                      int(wish_channel) if wish_channel else None, wish_desk, wish_plans, patch_feed,
                      regions=regions, live_watcher=live_watcher, live_notify_offline=live_notify_offline,
                      predictions=prediction_store, prediction_ui=PredictionUI(prediction_store),
                      announce_desk=announce_desk, watch_ui=watch_ui, watch_sources=watch_sources,
                      vod_search=vod_search, watch_server=watch_server, progress=progress, tv_table=tv_table,
                      qa_log=QaLog(state_dir / "qa_log.jsonl"), report_channel_id=int(report) if report else None)
    patch_feed.icons = HeroEmojis(bot).for_patch     # 补丁摘要里英雄名旁边的头像
    if watch_server is not None:
        watch_server.channel_name = lambda cid: getattr(bot.get_channel(cid), "name", None)   # 管理后台显示频道名
    ws = WSServer(
        registry, bot,
        host=os.environ.get("WS_HOST", "0.0.0.0"),
        port=int(os.environ.get("WS_PORT", "8765")),
        min_snapshot_interval=float(os.environ.get("MIN_SNAPSHOT_INTERVAL", "5")),
    )

    async with bot:
        await ws.start()
        refreshers = [asyncio.create_task(refresh_loop(store, refresh_hours)),
                      asyncio.create_task(refresh_loop(matchup_store, refresh_hours, fetch=matchups.fetch_all)),
                      asyncio.create_task(esports_feed.run(bot.post_esports, bot.edit_esports)),
                      asyncio.create_task(patch_feed.run(bot.post_patches)),
                      asyncio.create_task(live_watcher.run())]
        if watch_server is not None:
            await watch_server.start()
            refreshers.append(asyncio.create_task(watch_server.run()))
            if watch_server.geo is not None:
                refreshers.append(asyncio.create_task(watch_server.geo.run()))   # IP 分配表：没有就下载，每周刷新
            if tv_table is not None:
                refreshers.append(asyncio.create_task(tv_table.run()))
        try:
            await bot.start(token)
        except discord.LoginFailure:
            raise SystemExit("Discord login failed: DISCORD_TOKEN is invalid. Regenerate it in the Developer Portal.")
        except discord.PrivilegedIntentsRequired:
            raise SystemExit("Discord refused the connection: enable Message Content Intent on the Bot page of the Developer Portal.")
        finally:
            for task in refreshers:
                task.cancel()
            if watch_server is not None:
                await watch_server.close()
            if tv_session is not None:
                await tv_session.close()
            await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
