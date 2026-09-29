"""mrmeeseeks 服务器入口：Discord bot + WebSocket 配对服务，跑在同一个进程里。

频道里的指令：
  @mrmeeseeks            连接一个本地客户端（选择标识 -> 私发 8 位配对码），也可写 connect
  @mrmeeseeks status     查看所有在线客户端
  @mrmeeseeks player <BattleTag>  用 OverFast 查玩家段位和常用英雄
  @mrmeeseeks disconnect 解绑当前频道里的客户端
  @mrmeeseeks help       显示帮助
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time

import discord
from dotenv import load_dotenv

import overfast
from analyzer import Analyzer, format_facts
from registry import CODE_TTL_SECONDS, ClientConn, PairingError, Registry
from ws_server import WSServer

log = logging.getLogger("mrmeeseeks")

HELP_TEXT = (
    "**mrmeeseeks commands**\n"
    "`@mrmeeseeks` or `@mrmeeseeks connect` Connect a local client (pick its ID and I'll DM you an 8-digit pairing code)\n"
    "`@mrmeeseeks status` List online clients\n"
    "`@mrmeeseeks player Name#1234` Look up a player's ranks and most played heroes (case-sensitive BattleTag)\n"
    "`@mrmeeseeks disconnect` Unpair the client bound to this channel\n"
    "`@mrmeeseeks help` Show this help"
)


def parse_command(text: str) -> tuple[str, str]:
    """拆成 (小写指令名, 原样参数)。参数保留大小写，因为 BattleTag 大小写敏感。"""
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0].lower(), parts[1].strip() if len(parts) > 1 else ""


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
    def __init__(self, registry: Registry, analyzer: Analyzer | None):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
        self.registry = registry
        self.analyzer = analyzer
        self.overfast = overfast.OverFast()

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
        for token in (f"<@{self.user.id}>", f"<@!{self.user.id}>"):
            text = text.replace(token, "")
        cmd, arg = parse_command(text)

        if cmd in ("", "connect"):
            await self._connect_flow(message)
        elif cmd == "status":
            await self._status(message)
        elif cmd == "disconnect":
            await self._disconnect(message)
        elif cmd == "player":
            await self._player(message, arg)
        else:
            await message.reply(HELP_TEXT)

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
                await message.reply(f"Player `{tag.replace('-', '#')}` not found. "
                                    "Check the spelling and capitalization, e.g. `Name#1234`.")
                return
            except overfast.OverFastUnavailable as e:
                log.warning("OverFast lookup failed for %s: %s", tag, e)
                await message.reply("OverFast is unavailable right now (rate-limited or down). Try again in a minute.")
                return
        await message.reply(overfast.format_player(tag, summary, stats))

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
        files = [discord.File(io.BytesIO(scoreboard), "scoreboard.jpg"),
                 discord.File(io.BytesIO(hud), "hud.jpg")]
        msg = await channel.send(f"🔍 Got screenshots from **{client.client_id}**, analyzing…", files=files)

        if self.analyzer is None:
            await msg.reply("(ANTHROPIC_API_KEY is not set, so screenshots are only forwarded, not analyzed.)")
            return "Analysis not configured"

        facts, advice = await self.analyzer.analyze(scoreboard, hud)
        text = f"**Detected**: {format_facts(facts)}\n\n{advice}"
        await msg.reply(text[:1990])
        return advice.splitlines()[0] if advice else ""


async def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("DISCORD_TOKEN is missing: copy server/.env.example to server/.env and fill it in.")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    model = os.environ.get("CLAUDE_MODEL", "").strip() or "claude-sonnet-5"
    analyzer = Analyzer(api_key, model) if api_key else None
    if analyzer is None:
        log.warning("ANTHROPIC_API_KEY not set; screenshots will be forwarded without analysis")
    else:
        log.info("Analysis model: %s", model)

    registry = Registry()
    bot = MeeseeksBot(registry, analyzer)
    ws = WSServer(
        registry, bot,
        host=os.environ.get("WS_HOST", "0.0.0.0"),
        port=int(os.environ.get("WS_PORT", "8765")),
        min_snapshot_interval=float(os.environ.get("MIN_SNAPSHOT_INTERVAL", "5")),
    )

    async with bot:
        await ws.start()
        try:
            await bot.start(token)
        except discord.LoginFailure:
            raise SystemExit("Discord login failed: DISCORD_TOKEN is invalid. Regenerate it in the Developer Portal.")
        except discord.PrivilegedIntentsRequired:
            raise SystemExit("Discord refused the connection: enable Message Content Intent on the Bot page of the Developer Portal.")
        finally:
            await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
