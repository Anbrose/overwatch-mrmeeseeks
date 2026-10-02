"""Discord 里标注未知头像：图片 + 两级下拉菜单（职责 → 英雄）。"""
from __future__ import annotations

import io
import logging
from typing import Iterable

import cv2
import discord

from labeling import SPECIAL_LABELS, LabelStore, Pending

log = logging.getLogger("mrmeeseeks.label")

ROLES = (("tank", "Tank"), ("damage", "Damage"), ("support", "Support"))
DISCARD = "_discard"
VIEW_TIMEOUT = 24 * 3600     # bot 重启后菜单失效，用 @mrmeeseeks label 重新拿


Roster = dict[str, list[tuple[str, str]]]    # role -> [(key, name)]


def build_roster(heroes: Iterable[dict]) -> Roster:
    roster: Roster = {role: [] for role, _ in ROLES}
    for h in heroes:
        if h.get("role") in roster:
            roster[h["role"]].append((h["key"], h["name"]))
    for role in roster:
        roster[role].sort(key=lambda kv: kv[1])
    return roster


def prompt_text(item: Pending) -> str:
    m = item.meta
    team = "Ally" if m.get("team") == "ally" else "Enemy"
    who = f" ({m['player']})" if m.get("player") else ""
    seen = f" · seen {m['seen']}×" if m.get("seen", 1) > 1 else ""
    return f"❓ Unknown portrait — {team} row {m.get('row', '?')}{who}{seen}. Which hero is this?"


def prompt_file(item: Pending) -> discord.File:
    big = cv2.resize(item.image, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    ok, buf = cv2.imencode(".png", big)
    return discord.File(io.BytesIO(buf.tobytes()), f"unknown-{item.id}.png")


class LabelView(discord.ui.View):
    def __init__(self, store: LabelStore, roster: Roster, hero_name, pid: str, owners: set[int]):
        super().__init__(timeout=VIEW_TIMEOUT)
        self.store, self.roster, self.hero_name = store, roster, hero_name
        self.pid, self.owners = pid, owners
        options = [discord.SelectOption(label=name, value=role) for role, name in ROLES]
        options += [discord.SelectOption(label=text, value=label) for label, text in SPECIAL_LABELS.items()]
        options.append(discord.SelectOption(label="Not a portrait (discard)", value=DISCARD))
        self._add_select("Role…", options, self._on_role)

    def _add_select(self, placeholder: str, options: list[discord.SelectOption], handler) -> None:
        select = discord.ui.Select(placeholder=placeholder, options=options[:25])
        select.callback = lambda interaction, s=select: handler(interaction, s.values[0])
        self.add_item(select)

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if self.owners and interaction.user.id not in self.owners:
            await interaction.response.send_message(
                "Only the player who owns this client can label it.", ephemeral=True)
            return False
        return True

    async def _on_role(self, interaction: discord.Interaction, value: str) -> None:
        if not await self._allowed(interaction):
            return
        if value in SPECIAL_LABELS or value == DISCARD:
            await self._finish(interaction, value)
            return
        self.clear_items()
        heroes = self.roster.get(value, [])
        for i in range(0, len(heroes), 25):
            chunk = heroes[i:i + 25]
            self._add_select(f"{value.title()} hero…",
                             [discord.SelectOption(label=name, value=key) for key, name in chunk], self._on_hero)
        await interaction.response.edit_message(view=self)

    async def _on_hero(self, interaction: discord.Interaction, key: str) -> None:
        if await self._allowed(interaction):
            await self._finish(interaction, key)

    async def _finish(self, interaction: discord.Interaction, label: str) -> None:
        if label == DISCARD:
            self.store.discard(self.pid)
            text = f"🗑️ Discarded by {interaction.user.mention}."
        elif self.store.label(self.pid, label):
            name = SPECIAL_LABELS.get(label) or self.hero_name(label)
            text = f"✅ Labeled as **{name}** by {interaction.user.mention}. I'll recognize it from now on."
            log.info("Labeled %s as %s by %s", self.pid, label, interaction.user)
        else:
            text = "This portrait was already labeled or discarded."
        self.stop()
        await interaction.response.edit_message(content=text, view=None)


async def send_prompts(channel, store: LabelStore, roster: Roster, hero_name, items: list[Pending],
                       owners: set[int]) -> None:
    for item in items:
        await channel.send(prompt_text(item), file=prompt_file(item),
                           view=LabelView(store, roster, hero_name, item.id, owners))
        store.mark_prompted(item.id)
