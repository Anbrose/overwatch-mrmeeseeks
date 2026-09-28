"""mrmeeseeks 服务器入口：Discord bot + WebSocket 配对服务，跑在同一个进程里。

频道里的指令：
  @mrmeeseeks            连接一个本地客户端（选择标识 -> 私发 8 位配对码）
  @mrmeeseeks 状态       查看所有在线客户端
  @mrmeeseeks 断开       解绑当前频道里的客户端
  @mrmeeseeks 帮助       显示帮助
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time

import discord
from dotenv import load_dotenv

from analyzer import Analyzer, format_facts
from registry import CODE_TTL_SECONDS, ClientConn, PairingError, Registry
from ws_server import WSServer

log = logging.getLogger("mrmeeseeks")

HELP_TEXT = (
    "**mrmeeseeks 指令**\n"
    "`@mrmeeseeks` 连接本地客户端（选择客户端标识后，我会私下给你一个 8 位配对码）\n"
    "`@mrmeeseeks 状态` 查看在线客户端\n"
    "`@mrmeeseeks 断开` 解绑本频道的客户端\n"
    "`@mrmeeseeks 帮助` 显示这条帮助"
)


def _age(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes} 分钟" if minutes else f"{int(seconds)} 秒"


# ---------------- 选择客户端的下拉菜单 ----------------
class ClientSelect(discord.ui.Select):
    def __init__(self, bot: "MeeseeksBot", clients: list[ClientConn]):
        now = time.time()
        options = [
            discord.SelectOption(
                label=c.client_id, value=c.client_id,
                description=f"{c.hostname} · 已在线 {_age(now - c.connected_at)}"[:100])
            for c in clients[:25]
        ]
        super().__init__(placeholder="选择要连接的客户端标识", options=options)
        self.bot = bot

    async def callback(self, interaction: discord.Interaction) -> None:
        view: ClientSelectView = self.view  # type: ignore[assignment]
        if interaction.user.id != view.owner_id:
            await interaction.response.send_message("只有发起连接的人可以选择。", ephemeral=True)
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
            await interaction.response.send_message(f"客户端 {client_id} 已离线。", ephemeral=True)
            return

        self.disabled = True
        view.stop()
        await interaction.response.edit_message(
            content=f"已选择 **{client_id}**，配对码已私下发给 {interaction.user.mention}。",
            view=view)
        await interaction.followup.send(
            f"你的配对码：**{pending.code}**\n"
            f"请在客户端 **{client_id}** 的窗口里输入，{CODE_TTL_SECONDS // 60} 分钟内有效。",
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
                await self.message.edit(content="选择已超时，请重新 @mrmeeseeks。", view=self)
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

    async def on_ready(self) -> None:
        log.info("Discord 已登录：%s", self.user)

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
        cmd = text.strip().lower()

        if cmd in ("", "连接", "connect"):
            await self._connect_flow(message)
        elif cmd in ("状态", "status"):
            await self._status(message)
        elif cmd in ("断开", "disconnect"):
            await self._disconnect(message)
        else:
            await message.reply(HELP_TEXT)

    async def _connect_flow(self, message: discord.Message) -> None:
        clients = self.registry.available()
        if not clients:
            await message.reply("当前没有可用的客户端。请先在你的电脑上运行客户端脚本。")
            return
        view = ClientSelectView(self, message.author.id, clients)
        view.message = await message.reply(
            f"有 {len(clients)} 个客户端在线，请选择要连接的标识：", view=view)

    async def _status(self, message: discord.Message) -> None:
        if not self.registry.clients:
            await message.reply("当前没有在线的客户端。")
            return
        now = time.time()
        lines = []
        for c in sorted(self.registry.clients.values(), key=lambda c: c.connected_at):
            where = f"已绑定 <#{c.channel_id}>（{c.user_name}）" if c.paired else "未绑定"
            lines.append(f"`{c.client_id}` {c.hostname} · 在线 {_age(now - c.connected_at)} · {where}")
        await message.reply("\n".join(lines))

    async def _disconnect(self, message: discord.Message) -> None:
        clients = self.registry.in_channel(message.channel.id)
        if not clients:
            await message.reply("本频道没有绑定的客户端。")
            return
        for c in clients:
            self.registry.unpair(c.client_id)
            try:
                await c.send(type="unpaired", reason=f"{message.author} 在 Discord 里断开了连接")
            except Exception:
                pass
        await message.reply("已断开：" + "、".join(f"`{c.client_id}`" for c in clients))

    # ---------- WebSocket 事件（ws_server.Events） ----------
    async def client_paired(self, client: ClientConn) -> None:
        channel = await self._channel(client.channel_id)
        await channel.send(f"✅ 客户端 **{client.client_id}**（{client.hostname}）已连接。"
                           f"在游戏里按住 Tab 再松开，我就会开始分析。")

    async def pair_failed(self, client: ClientConn, channel_id: int, reason: str) -> None:
        channel = await self._channel(channel_id)
        await channel.send(f"❌ 客户端 **{client.client_id}** 配对失败：{reason}")

    async def client_disconnected(self, client: ClientConn) -> None:
        if client.paired:
            channel = await self._channel(client.channel_id)
            await channel.send(f"⚠️ 客户端 **{client.client_id}** 已断开。")

    async def snapshot_received(self, client: ClientConn, scoreboard: bytes, hud: bytes) -> str:
        channel = await self._channel(client.channel_id)
        files = [discord.File(io.BytesIO(scoreboard), "scoreboard.jpg"),
                 discord.File(io.BytesIO(hud), "hud.jpg")]
        msg = await channel.send(f"🔍 收到 **{client.client_id}** 的截图，分析中…", files=files)

        if self.analyzer is None:
            await msg.reply("（未配置 ANTHROPIC_API_KEY，只转发截图，不做分析）")
            return "未配置分析"

        facts, advice = await self.analyzer.analyze(scoreboard, hud)
        text = f"**识别**：{format_facts(facts)}\n\n{advice}"
        await msg.reply(text[:1990])
        return advice.splitlines()[0] if advice else ""


async def main() -> None:
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise SystemExit("缺少 DISCORD_TOKEN：请把 server/.env.example 复制为 server/.env 并填写。")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    model = os.environ.get("CLAUDE_MODEL", "").strip() or "claude-sonnet-5"
    analyzer = Analyzer(api_key, model) if api_key else None
    if analyzer is None:
        log.warning("未设置 ANTHROPIC_API_KEY，截图只会转发到频道，不做分析")
    else:
        log.info("分析模型：%s", model)

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
            raise SystemExit("Discord 登录失败：DISCORD_TOKEN 不正确，请到开发者后台重新生成。")
        except discord.PrivilegedIntentsRequired:
            raise SystemExit("Discord 拒绝连接：请在开发者后台 Bot 页面打开 Message Content Intent。")
        finally:
            await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
