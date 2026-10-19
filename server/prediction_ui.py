"""竞猜按钮：discord.ui.DynamicItem，custom_id 模板 `pred:<key>:<1|2>`。

bot 启动时 add_dynamic_items(PredictButton) 一次，之后所有竞猜消息上的按钮（包括重启前发的）都能点。
点击：记录这一票 → 只回复点的人（ephemeral）→ 更新这条消息按钮上的票数。
同一场比赛的票数编辑每 MIN_EDIT_INTERVAL 秒最多一次，期间的点击合并成一次延后的编辑，避免撞 Discord 速率限制。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable

import discord

import predictions as pr

log = logging.getLogger("mrmeeseeks.prediction_ui")

TEMPLATE = r"pred:(?P<key>[0-9a-f]{12}):(?P<side>[12])"
MIN_EDIT_INTERVAL = 2.0


def custom_id(key: str, side: int) -> str:
    return f"pred:{key}:{side}"


def label(team: str, count: int) -> str:
    return f"{team} · {count}"[:80]          # Discord 按钮文字最长 80


class PredictButton(discord.ui.DynamicItem[discord.ui.Button], template=TEMPLATE):
    handler: "PredictionUI | None" = None     # bot 启动时设置

    def __init__(self, key: str, side: int, text: str = "…", disabled: bool = False):
        style = discord.ButtonStyle.primary if side == 1 else discord.ButtonStyle.danger
        super().__init__(discord.ui.Button(label=text, custom_id=custom_id(key, side), style=style, disabled=disabled))
        self.key, self.side = key, side

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str]) -> "PredictButton":
        return cls(match["key"], int(match["side"]), item.label or "…", item.disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        if PredictButton.handler is None:
            await interaction.response.send_message("Predictions are not enabled right now.", ephemeral=True)
            return
        await PredictButton.handler.on_pick(interaction, self.key, self.side)


def build_view(spec: dict[str, Any]) -> discord.ui.View:
    """prediction_feed.view_spec -> 持久化 View（timeout=None）。"""
    view = discord.ui.View(timeout=None)
    for side, (team, count) in enumerate(zip(spec["labels"], spec["counts"]), start=1):
        view.add_item(PredictButton(spec["key"], side, label(team, count), spec["disabled"]))
    return view


class PredictionUI:
    def __init__(self, store: pr.PredictionStore, clock: Callable[[], float] = time.time,
                 min_edit_interval: float = MIN_EDIT_INTERVAL):
        self.store, self.clock, self.min_edit_interval = store, clock, min_edit_interval
        self._last_edit: dict[str, float] = {}
        self._pending: dict[str, asyncio.Task] = {}

    async def on_pick(self, interaction: discord.Interaction, key: str, side: int) -> None:
        entry = self.store.get(key)
        result = self.store.pick(key, interaction.user.id, interaction.user.display_name, side, self.clock())
        if not result.ok:
            await interaction.response.send_message(result.reason, ephemeral=True)
            return
        team = entry["team1"] if side == 1 else entry["team2"]
        verb = "Changed your pick to" if result.changed else "You picked"
        await interaction.response.send_message(f"{verb} **{team}** — you can change it until the match starts.",
                                                ephemeral=True)
        await self.refresh(interaction.message, key)

    async def refresh(self, message: Any, key: str) -> None:
        """更新票数；距离上次编辑不到 min_edit_interval 秒就排一次延后的编辑（已经排了就不再排）。"""
        if message is None:
            return
        wait = self.min_edit_interval - (self.clock() - self._last_edit.get(key, float("-inf")))
        if wait <= 0:
            await self._edit(message, key)
        elif key not in self._pending or self._pending[key].done():
            self._pending[key] = asyncio.get_running_loop().create_task(self._edit_later(message, key, wait))

    async def _edit_later(self, message: Any, key: str, wait: float) -> None:
        await asyncio.sleep(wait)
        await self._edit(message, key)

    async def _edit(self, message: Any, key: str) -> None:
        entry = self.store.get(key)
        if entry is None:
            return
        self._last_edit[key] = self.clock()
        n1, n2 = self.store.counts(key)
        spec = {"key": key, "labels": [entry["team1"], entry["team2"]], "counts": [n1, n2],
                "disabled": entry["status"] != pr.OPEN}
        try:
            await message.edit(view=build_view(spec))
        except Exception:
            log.exception("Could not update prediction counts for %s", key)
