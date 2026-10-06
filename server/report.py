"""/report 问题反馈：弹出表单（选 bot + 描述），提交后发到报告频道。

配置（server/.env）：
  REPORT_CHANNEL_ID  报告发到哪个频道
  REPORT_BOTS        下拉框里的 bot 名单，逗号分隔；末尾自动加 "Other"
两项都填了才注册 /report。
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands

log = logging.getLogger("mrmeeseeks.report")

OTHER = "Other"
MAX_BOTS = 24          # 下拉框最多 25 项，给 Other 留一个
MAX_TEXT = 1000
THANKS_TEXT = "Thanks, your report was sent."
FAILED_TEXT = "Sorry, I couldn't deliver your report. Please tell a server admin directly."


def parse_bot_list(raw: str) -> list[str]:
    """逗号分隔的名单 -> 去空白、忽略大小写去重、去掉 Other，最多 MAX_BOTS 个。"""
    names: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        name = part.strip()[:100]
        key = name.lower()
        if not name or key == OTHER.lower() or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names[:MAX_BOTS]


def parse_channel_id(raw: str) -> int | None:
    raw = raw.strip()
    return int(raw) if raw.isdigit() else None


def build_report_embed(bot_name: str, what: str, when: str, steps: str,
                       reporter: str, source: str) -> discord.Embed:
    embed = discord.Embed(title=f"🐞 Issue report: {bot_name}", description=what,
                          color=discord.Color.orange())
    if when.strip():
        embed.add_field(name="When", value=when.strip(), inline=False)
    if steps.strip():
        embed.add_field(name="Steps / links", value=steps.strip(), inline=False)
    embed.add_field(name="Reported by", value=reporter)
    embed.add_field(name="From", value=source)
    return embed


class ReportModal(discord.ui.Modal, title="Report a bot issue"):
    def __init__(self, bots: list[str], channel_id: int):
        super().__init__()
        self.channel_id = channel_id
        self.bot = discord.ui.Select(
            placeholder="Choose the bot that has a problem",
            options=[discord.SelectOption(label=name, value=name) for name in [*bots, OTHER]])
        self.what = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, max_length=MAX_TEXT,
            placeholder="What went wrong? What did you expect instead?")
        self.when = discord.ui.TextInput(required=False, max_length=100, placeholder="e.g. just now, around 9pm")
        self.steps = discord.ui.TextInput(
            style=discord.TextStyle.paragraph, required=False, max_length=MAX_TEXT,
            placeholder="How to reproduce it, or links to screenshots")
        self.add_item(discord.ui.Label(text="Which bot", component=self.bot))
        self.add_item(discord.ui.Label(text="What happened", component=self.what))
        self.add_item(discord.ui.Label(text="When did it happen", component=self.when))
        self.add_item(discord.ui.Label(text="Steps / links", component=self.steps))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        source = f"<#{interaction.channel_id}>" if interaction.guild else "a DM"
        embed = build_report_embed(self.bot.values[0], self.what.value, self.when.value, self.steps.value,
                                   interaction.user.mention, source)
        try:
            channel = interaction.client.get_channel(self.channel_id) \
                or await interaction.client.fetch_channel(self.channel_id)
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.DiscordException:
            log.exception("Could not deliver report to channel %s", self.channel_id)
            await interaction.response.send_message(FAILED_TEXT, ephemeral=True)
            return
        await interaction.response.send_message(THANKS_TEXT, ephemeral=True)


def register(tree: app_commands.CommandTree, bots: list[str], channel_id: int) -> None:
    @tree.command(name="report", description="Report a problem with one of the bots")
    async def report(interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ReportModal(bots, channel_id))
