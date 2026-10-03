"""功能许愿频道：用户只能通过按钮弹出的表单提交，每人每天限 WISH_DAILY_LIMIT 次（可用同名环境变量覆盖）。

频道权限要关掉 @everyone 的「发送消息」「创建子区」，只留 bot 能发言；
bot 启动时在频道里发一条带按钮的置顶入口消息（已有就复用），表单提交后 bot 把内容
发成卡片，加 👍 投票，并开一个子区供讨论；随后 wish_plan.py 在子区里起草方案。

存储（state_dir）：
  wishes.json          {"entry_message_id": 123, "date": "2026-10-03", "counts": {"<user_id>": 1}}
  wishes/<id>.json     每条许愿一份存档（表单内容 + 历次方案），<id> 是卡片消息 ID，也是子区 ID
"""
from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import discord

log = logging.getLogger("mrmeeseeks.wishes")

WISH_DAILY_LIMIT = 2                       # 默认值；服务器用 .env 的 WISH_DAILY_LIMIT 调整
WISH_TZ = ZoneInfo("Australia/Sydney")    # 每天 0 点（悉尼时间）重置次数
SUBMIT_ID = "wishes:submit"                # 持久化按钮的 custom_id，bot 重启后旧按钮仍然有效
VOTE_EMOJI = "👍"
OTHER = "Other ｜ 其他"                     # 小众游戏：选它之后再让用户手填游戏名
# 表单里「哪个游戏」的选项（英文名 ｜ 中文名），可用 WISH_GAMES 覆盖；OTHER 总是排最后，合计 ≤ 25（Discord 下拉上限）
DEFAULT_GAMES = (
    "Overwatch ｜ 守望先锋",
    "Marvel Rivals ｜ 漫威争锋",
    "Valorant ｜ 无畏契约",
    "League of Legends ｜ 英雄联盟",
    "Teamfight Tactics ｜ 云顶之弈",
    "Counter-Strike 2 ｜ 反恐精英 2",
    "Apex Legends ｜ Apex 英雄",
    "PUBG: Battlegrounds ｜ 绝地求生",
    "Delta Force ｜ 三角洲行动",
    "Rainbow Six Siege ｜ 彩虹六号：围攻",
    "Fortnite ｜ 堡垒之夜",
    "Dota 2 ｜ 刀塔 2",
    "Naraka: Bladepoint ｜ 永劫无间",
    "Honor of Kings ｜ 王者荣耀",
    "Genshin Impact ｜ 原神",
    "Honkai: Star Rail ｜ 崩坏：星穹铁道",
    "Hearthstone ｜ 炉石传说",
    "World of Warcraft ｜ 魔兽世界",
    "Diablo IV ｜ 暗黑破坏神 4",
    "Minecraft ｜ 我的世界",
    "Elden Ring ｜ 艾尔登法环",
    "Monster Hunter Wilds ｜ 怪物猎人：荒野",
    "Black Myth: Wukong ｜ 黑神话：悟空",
    OTHER,
)
PICK_TIMEOUT = 600                         # 选了 OTHER 后填游戏名的按钮多久后失效（秒），超时按 OTHER 发出
PICK_TEXT = ("You picked **Other** ｜ 你选了其他游戏。\n"
             "Press the button and type the game's name ｜ 请点下面的按钮填写游戏名。\n"
             f"If you don't within {PICK_TIMEOUT // 60} minutes, your wish will be posted as **{OTHER}**.")


def entry_text(limit: int) -> str:
    """置顶入口消息的文字。改了每日次数后，bot 启动时会原地编辑已有的入口消息。"""
    return ("💡 **Feature wishes**\n"
            "Want mrmeeseeks to do something new? Press the button below and fill in the form.\n"
            f"Everyone gets **{limit} wishes per day** (resets at midnight Sydney time). "
            f"Vote with {VOTE_EMOJI} and discuss in each wish's thread.")


def used_up(limit: int) -> str:
    return f"You've used all {limit} wishes for today. Come back tomorrow!"


class WishQuota:
    """每人每天的提交次数，存在 JSON 里，重启不清零。"""

    def __init__(self, path: Path, limit: int = WISH_DAILY_LIMIT):
        self.path = Path(path)
        self.limit = limit
        self.data: dict = {"date": "", "counts": {}}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                log.warning("Could not read %s; starting with empty wish quotas", self.path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        tmp.replace(self.path)

    def _counts(self, now: datetime | None) -> dict[str, int]:
        today = (now or datetime.now(WISH_TZ)).astimezone(WISH_TZ).date().isoformat()
        if self.data.get("date") != today:
            self.data["date"], self.data["counts"] = today, {}
        return self.data["counts"]

    def remaining(self, user_id: int, now: datetime | None = None) -> int:
        return max(0, self.limit - self._counts(now).get(str(user_id), 0))

    def take(self, user_id: int, now: datetime | None = None) -> bool:
        """占用一次额度；已用完返回 False。发帖失败时用 refund 退回。"""
        counts = self._counts(now)
        used = counts.get(str(user_id), 0)
        if used >= self.limit:
            return False
        counts[str(user_id)] = used + 1
        self._save()
        return True

    def refund(self, user_id: int, now: datetime | None = None) -> None:
        counts = self._counts(now)
        key = str(user_id)
        if counts.get(key, 0) > 0:
            counts[key] -= 1
            self._save()

    @property
    def entry_message_id(self) -> int | None:
        return self.data.get("entry_message_id")

    @entry_message_id.setter
    def entry_message_id(self, message_id: int) -> None:
        self.data["entry_message_id"] = message_id
        self._save()


class WishLog:
    """许愿存档：一条许愿一个 JSON 文件。从子区创建的卡片消息和子区 ID 相同，所以按子区 ID 也能查到。"""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def save(self, record: dict[str, Any]) -> None:
        tmp = self.dir / f"{record['id']}.tmp"
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.dir / f"{record['id']}.json")

    def get(self, wish_id: int) -> dict[str, Any] | None:
        path = self.dir / f"{wish_id}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("Could not read wish %s", path)
            return None

    def all(self) -> list[dict[str, Any]]:
        records = []
        for path in self.dir.glob("*.json"):
            try:
                records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                log.warning("Skipping unreadable wish %s", path)
        return sorted(records, key=lambda r: r["created_at"])

    def add_plan(self, wish_id: int, request: str, text: str, dev: str = "") -> dict[str, Any] | None:
        """text 是发给许愿人的部分，dev 是只给审核人看的开发者备注。"""
        record = self.get(wish_id)
        if record is None:
            return None
        record.setdefault("plans", []).append(
            {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "request": request, "text": text,
             "dev": dev})
        self.save(record)
        return record


@dataclass
class WishDesk:
    """许愿频道共用的状态，按钮和表单都拿这一份。on_posted 在卡片和子区发出后调用（起草方案）。"""
    quota: WishQuota
    log: WishLog
    games: tuple[str, ...] = DEFAULT_GAMES
    on_posted: Callable[[dict[str, Any], Any], Awaitable[None]] | None = field(default=None, repr=False)


def is_other(game: str) -> bool:
    return game.strip().lower() in ("other", OTHER.lower())


def parse_games(text: str) -> tuple[str, ...]:
    """WISH_GAMES="Overwatch ｜ 守望先锋, Valorant ｜ 无畏契约" -> 去重后的选项，末尾自动补 OTHER；空则用默认。"""
    games = [g for g in dict.fromkeys(g.strip() for g in text.split(",") if g.strip()) if not is_other(g)]
    return (*games[:24], OTHER) if games else DEFAULT_GAMES


def wish_embed(user: discord.abc.User, game: str, title: str, problem: str, how: str,
               notes: str) -> discord.Embed:
    embed = discord.Embed(title=f"💡 {title}", color=discord.Color.gold(), timestamp=discord.utils.utcnow())
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    embed.add_field(name="Game", value=game, inline=False)
    embed.add_field(name="What problem does it solve?", value=problem, inline=False)
    if how:
        embed.add_field(name="How should it work?", value=how, inline=False)
    if notes:
        embed.add_field(name="Anything else", value=notes, inline=False)
    return embed


@dataclass
class Wish:
    user: Any
    game: str
    title: str
    problem: str
    how: str
    notes: str


async def publish(desk: WishDesk, channel, wish: Wish, interaction: discord.Interaction | None) -> None:
    """发卡片、加 👍、开子区、存档，再调用 on_posted（起草方案）。

    interaction 是已经 defer / 回复过的交互，用它的 followup 私下告诉许愿人结果；
    游戏选择超时后自动发出时没有交互，传 None。"""
    async def tell(text: str) -> None:
        if interaction is not None:
            await interaction.followup.send(text, ephemeral=True)

    user, quota = wish.user, desk.quota
    embed = wish_embed(user, wish.game, wish.title, wish.problem, wish.how, wish.notes)
    try:
        msg = await channel.send(embed=embed)
    except discord.HTTPException:
        log.exception("Could not post wish from %s", user)
        quota.refund(user.id)
        await tell("Sorry, I couldn't post your wish. It wasn't counted — please try again later.")
        return
    # 投票和子区失败不影响许愿本身
    thread = None
    try:
        await msg.add_reaction(VOTE_EMOJI)
        thread = await msg.create_thread(name=f"[{wish.game}] {wish.title}"[:100], auto_archive_duration=10080)
    except discord.HTTPException:
        log.warning("Could not add reaction/thread to wish %s", msg.id, exc_info=True)
    record = {"id": msg.id, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "user_id": user.id, "user_name": str(user), "game": wish.game, "title": wish.title,
              "problem": wish.problem, "how": wish.how, "notes": wish.notes, "url": msg.jump_url, "plans": []}
    desk.log.save(record)
    await tell(f"✨ Wish posted: {msg.jump_url}\nYou have {quota.remaining(user.id)} wish(es) left today.")
    if thread is not None and desk.on_posted:
        try:
            await desk.on_posted(record, thread)
        except Exception:      # 许愿已经发出，方案出错不能再报「提交失败」
            log.exception("Post-wish hook failed for wish %s", msg.id)


class GameNameModal(discord.ui.Modal, title="Which game?"):
    """选了 OTHER 时手填游戏名。"""
    name = discord.ui.TextInput(label="Game name ｜ 游戏名", max_length=60, placeholder="e.g. Deadlock")

    def __init__(self, picker: "GamePickView"):
        super().__init__()
        self.picker = picker

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.picker.choose(interaction, self.name.value.strip() or OTHER)


class GamePickView(discord.ui.View):
    """选了 OTHER 之后让用户手填游戏名。额度在表单提交时已经占用；超时没填就按 OTHER 发出。
    （bot 在这期间重启的话这条许愿会丢，额度不退。）"""

    def __init__(self, desk: WishDesk, channel, wish: Wish):
        super().__init__(timeout=PICK_TIMEOUT)
        self.desk, self.channel, self.wish = desk, channel, wish
        self.done = False

    @discord.ui.button(label="Type the game name ｜ 填写游戏名", emoji="✏️", style=discord.ButtonStyle.primary)
    async def type_name(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.done:
            await interaction.response.send_message("This wish was already posted.", ephemeral=True)
            return
        await interaction.response.send_modal(GameNameModal(self))

    async def choose(self, interaction: discord.Interaction, game: str) -> None:
        if self.done:
            await interaction.response.send_message("This wish was already posted.", ephemeral=True)
            return
        self.done = True
        self.stop()
        self.wish.game = game
        await interaction.response.edit_message(content=f"Game: **{game}** — posting your wish…", view=None)
        await publish(self.desk, self.channel, self.wish, interaction)

    async def on_timeout(self) -> None:
        if not self.done:
            self.done = True
            await publish(self.desk, self.channel, self.wish, None)


class WishModal(discord.ui.Modal, title="Make a feature wish"):
    """5 个输入框是 Discord 表单的上限：游戏（下拉）+ 名称 + 问题 + 怎么做 + 补充。"""

    def __init__(self, desk: WishDesk):
        super().__init__()
        self.desk = desk
        self.game = discord.ui.Select(placeholder="Choose a game ｜ 选择游戏",
                                      options=[discord.SelectOption(label=g, value=g) for g in desk.games])
        self.wish_title = discord.ui.TextInput(max_length=80, placeholder="e.g. Show ult charge of the enemy team")
        self.problem = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=1000,
                                            placeholder="When / why would you use it?")
        self.how = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=1000, required=False)
        self.notes = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=500, required=False)
        for text, item in (("Which game?", self.game),
                           ("Feature name", self.wish_title),
                           ("What problem does it solve?", self.problem),
                           ("How should it work? (optional)", self.how),
                           ("Anything else? (optional)", self.notes)):
            self.add_item(discord.ui.Label(text=text, component=item))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        user, quota = interaction.user, self.desk.quota
        if not quota.take(user.id):
            await interaction.response.send_message(
                used_up(quota.limit), ephemeral=True)
            return
        wish = Wish(user, self.game.values[0], self.wish_title.value.strip(), self.problem.value.strip(),
                    self.how.value.strip(), self.notes.value.strip())
        if is_other(wish.game):
            # 表单提交后不能直接再弹表单，所以先回一条只有本人能看到的按钮，点了再弹填游戏名的小表单
            view = GamePickView(self.desk, interaction.channel, wish)
            await interaction.response.send_message(PICK_TEXT, view=view, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await publish(self.desk, interaction.channel, wish, interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Wish form failed", exc_info=error)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("Something went wrong, please try again.", ephemeral=True)


class WishEntryView(discord.ui.View):
    """入口按钮。timeout=None + 固定 custom_id = 持久化视图，bot 重启后 add_view 一次即可。"""

    def __init__(self, desk: WishDesk):
        super().__init__(timeout=None)
        self.desk = desk

    @discord.ui.button(label="Make a wish", emoji="💡", style=discord.ButtonStyle.primary, custom_id=SUBMIT_ID)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.desk.quota.remaining(interaction.user.id) <= 0:
            await interaction.response.send_message(
                used_up(self.desk.quota.limit), ephemeral=True)
            return
        await interaction.response.send_modal(WishModal(self.desk))


async def ensure_entry(channel: discord.TextChannel, quota: WishQuota, view: WishEntryView) -> None:
    """确保频道里有一条带按钮的置顶入口消息；被删了就重新发一条。"""
    if quota.entry_message_id:
        try:
            msg = await channel.fetch_message(quota.entry_message_id)
            if msg.content != entry_text(quota.limit):
                await msg.edit(content=entry_text(quota.limit), view=view)
            return
        except discord.NotFound:
            log.info("Wish entry message is gone; posting a new one")
    msg = await channel.send(entry_text(quota.limit), view=view)
    quota.entry_message_id = msg.id
    try:
        await msg.pin()
    except discord.HTTPException:
        log.warning("Could not pin the wish entry message (needs Manage Messages / Pin Messages)")


# ---------------- 导出 ----------------
EXPORT_FIELDS = ("created_at", "game", "title", "votes", "user_name", "problem", "how", "notes",
                 "plan_revisions", "latest_plan", "latest_dev_notes", "url")


def parse_export_args(arg: str) -> tuple[str | None, int | None]:
    """"marvel 7d" -> ("marvel", 7)。游戏名是不区分大小写的包含匹配，"marvel" 也能匹配 "Marvel Rivals ｜ 漫威争锋"。"""
    days, words = None, []
    for token in arg.split():
        if token[:-1].isdigit() and token[-1].lower() == "d":
            days = int(token[:-1])
        else:
            words.append(token)
    return (" ".join(words) or None), days


def select_wishes(records: list[dict[str, Any]], game: str | None, days: int | None,
                  now: datetime | None = None) -> list[dict[str, Any]]:
    since = (now or datetime.now(timezone.utc)) - timedelta(days=days) if days else None
    return [r for r in records
            if (game is None or game.lower() in r["game"].lower())
            and (since is None or datetime.fromisoformat(r["created_at"]) >= since)]


def export_csv(records: list[dict[str, Any]], votes: dict[int, int | None]) -> bytes:
    """按票数从高到低；卡片已被删除的票数为空、排最后。带 BOM，Excel 打开中文不乱码。"""
    rows = sorted(records, key=lambda r: (votes.get(r["id"]) is None, -(votes.get(r["id"]) or 0), r["created_at"]))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS)
    writer.writeheader()
    for r in rows:
        plans = r.get("plans") or []
        writer.writerow({**{k: r.get(k, "") for k in EXPORT_FIELDS},
                         "votes": "" if votes.get(r["id"]) is None else votes[r["id"]],
                         "plan_revisions": max(0, len(plans) - 1),
                         "latest_plan": plans[-1]["text"] if plans else "",
                         "latest_dev_notes": plans[-1].get("dev", "") if plans else ""})
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def vote_count(message: discord.Message) -> int:
    """👍 数，不算 bot 自己加的那个。"""
    for reaction in message.reactions:
        if str(reaction.emoji) == VOTE_EMOJI:
            return reaction.count - (1 if reaction.me else 0)
    return 0
