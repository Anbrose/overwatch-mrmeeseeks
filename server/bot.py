"""mrmeeseeks 服务器入口：Discord bot + WebSocket 配对服务，跑在同一个进程里。

频道里的指令：
  @mrmeeseeks            连接一个本地客户端（选择标识 -> 私发 8 位配对码），也可写 connect
  @mrmeeseeks status     查看所有在线客户端
  @mrmeeseeks player <BattleTag>  用 OverFast 查玩家段位和常用英雄
  @mrmeeseeks analyze    用本频道最近一次识别结果重新给建议
  @mrmeeseeks label      拿出待标注的未知头像
  @mrmeeseeks disconnect 解绑当前频道里的客户端
  @mrmeeseeks help       显示帮助
  @mrmeeseeks <问题>     英雄问答（数值、补丁、几枪击杀），见 hero_qa.py
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
from collections import defaultdict
from pathlib import Path

import discord
import numpy as np
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

import overfast
from analyzer import Analyzer, format_facts
from hero_qa import Cooldown, HeroQA
from herodata import HeroStore, load_aliases, refresh_loop
from label_ui import Roster, build_roster, send_prompts
from labeling import LabelStore
from registry import CODE_TTL_SECONDS, ClientConn, PairingError, Registry
from situation import Situation
from vision.heroes import HeroMatcher
from vision.recognize import Recognizer, to_facts
from vision.text import load_maps
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
    "`@mrmeeseeks help` Show this help\n"
    "`@mrmeeseeks <question>` Ask about heroes, e.g. `was Cassidy nerfed recently?`, "
    "`Tracer HP`, `how many Cassidy headshots kill Mauga at 30m?`"
)
COMMANDS = ("connect", "status", "disconnect", "help", "analyze", "label")
ARG_COMMANDS = ("player",)     # 带参数的指令；参数保留大小写（BattleTag 大小写敏感）
ASK_COOLDOWN_SECONDS = 5


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
    def __init__(self, registry: Registry, analyzer: Analyzer | None, recognizer: Recognizer,
                 labels: LabelStore, roster: Roster,
                 store: HeroStore | None = None, hero_qa: HeroQA | None = None):
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

    def hero_name(self, key: str) -> str:
        return self.names.get(key) or overfast.hero_name(key)

    def _owners(self, channel_id: int) -> set[int]:
        return {c.user_id for c in self.registry.in_channel(channel_id) if c.user_id}

    async def on_ready(self) -> None:
        log.info("Logged in to Discord as %s", self.user)

    async def _channel(self, channel_id: int):
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        return channel

    # ---------- 指令 ----------
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.user is None or self.user not in message.mentions:
            return
        text = message.content
        tokens = (f"<@{self.user.id}>", f"<@!{self.user.id}>")
        explicit = any(t in text for t in tokens)
        for token in tokens:
            text = text.replace(token, "")
        cmd, arg = parse_command(text, explicit)

        if cmd == "connect":
            await self._connect_flow(message)
        elif cmd == "status":
            await self._status(message)
        elif cmd == "disconnect":
            await self._disconnect(message)
        elif cmd == "player":
            await self._player(message, arg)
        elif cmd == "analyze":
            await self._reanalyze(message)
        elif cmd == "label":
            await self._label(message)
        elif cmd == "help":
            await message.reply(HELP_TEXT)
        else:
            await self._ask(message, arg)

    async def _ask(self, message: discord.Message, question: str) -> None:
        if self.hero_qa is None:
            await message.reply("Hero Q&A needs ANTHROPIC_API_KEY to be set.")
            return
        if self.store is None or not self.store.ready:
            await message.reply("Hero data is not ready yet, try again in a minute.")
            return
        if not self.ask_cooldown.allow(message.author.id):
            await message.reply(f"Please wait {ASK_COOLDOWN_SECONDS} seconds between questions.")
            return
        try:
            async with message.channel.typing():
                answer = await self.hero_qa.answer(question)
        except Exception:
            log.exception("Hero Q&A failed for %r", question)
            await message.reply("Sorry, I couldn't answer that right now. Please try again later.")
            return
        await message.reply(answer[:1990] or "I couldn't come up with an answer.")

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
    analyzer = Analyzer(api_key, model, effort) if api_key else None
    if analyzer is None:
        log.warning("ANTHROPIC_API_KEY not set; screenshots will be recognized but no advice given")
    else:
        log.info("Advice model: %s (effort %s)", model, effort or "default")

    state_dir = Path(os.environ.get("STATE_DIR", DATA_DIR.parent / "state"))
    matcher = HeroMatcher([DATA_DIR / "templates", state_dir / "templates"])
    labels = LabelStore(state_dir, matcher)
    recognizer = Recognizer(matcher, load_maps(DATA_DIR / "maps.json"))
    await asyncio.to_thread(recognizer.ocr, np.zeros((32, 32, 3), np.uint8))   # 预加载 OCR 模型
    log.info("Loaded %d hero templates (%d labels); %d portraits waiting to be labeled",
             len(matcher.templates), len(matcher.labels), len(labels.pending()))
    try:
        heroes = await overfast.OverFast().heroes()
    except (overfast.OverFastUnavailable, overfast.PlayerNotFound) as e:
        log.warning("Could not fetch hero list from OverFast (%s); using bundled list", e)
        heroes = json.loads((DATA_DIR / "roster.json").read_text(encoding="utf-8"))
    roster = build_roster(heroes)

    server_dir = os.path.dirname(os.path.abspath(__file__))
    store = HeroStore(os.environ.get("HERO_DATA_PATH", "").strip() or os.path.join(server_dir, "data", "heroes.json"),
                      load_aliases())
    store.load()
    hero_qa = HeroQA(AsyncAnthropic(api_key=api_key), model, store) if api_key else None
    refresh_hours = float(os.environ.get("HERO_REFRESH_HOURS", "24"))

    registry = Registry()
    bot = MeeseeksBot(registry, analyzer, recognizer, labels, roster, store, hero_qa)
    ws = WSServer(
        registry, bot,
        host=os.environ.get("WS_HOST", "0.0.0.0"),
        port=int(os.environ.get("WS_PORT", "8765")),
        min_snapshot_interval=float(os.environ.get("MIN_SNAPSHOT_INTERVAL", "5")),
    )

    async with bot:
        await ws.start()
        refresher = asyncio.create_task(refresh_loop(store, refresh_hours))
        try:
            await bot.start(token)
        except discord.LoginFailure:
            raise SystemExit("Discord login failed: DISCORD_TOKEN is invalid. Regenerate it in the Developer Portal.")
        except discord.PrivilegedIntentsRequired:
            raise SystemExit("Discord refused the connection: enable Message Content Intent on the Bot page of the Developer Portal.")
        finally:
            refresher.cancel()
            await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
