"""多语言公告：@mrmeeseeks announce -> 表单（原文语言 + 标题 + 正文）-> Claude 翻译成其余语言
-> 只给作者看的预览 -> 发布。频道里只有一条原文公告，下面挂每种语言一个按钮，点了私密收到那种语言的版本。

翻译在发布前一次做完，存到 STATE_DIR/announcements/<key>.json；按钮是 DynamicItem（custom_id
`ann:<key>:<语言>`），bot 重启后旧公告的按钮照样能点，点按钮不再调用 API。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import discord

log = logging.getLogger("mrmeeseeks.announce")

# 语言代码 -> 按钮上的名字（按这个顺序显示）
LANGS = {"zh-Hans": "简体中文", "zh-Hant": "繁體中文", "en": "English", "ja": "日本語", "ko": "한국어"}
PROMPT_NAMES = {"zh-Hans": "Simplified Chinese", "zh-Hant": "Traditional Chinese", "en": "English",
                "ja": "Japanese", "ko": "Korean"}
MAX_TITLE = 200
MAX_BODY = 4000
EMBED_DESC_LIMIT = 4096
MESSAGE_EMBED_BUDGET = 5800     # Discord 一条消息所有 embed 合计 6000 字符，给页脚留余量
MAX_EMBEDS = 10
MAX_TOKENS = 16000              # adaptive thinking 也计入 max_tokens，见 hero_qa.py
PREVIEW_TTL = 900
COLOR = discord.Color.blurple()
KEY_RE = re.compile(r"[0-9a-f]{12}")
TEMPLATE = r"ann:(?P<key>[0-9a-f]{12}):(?P<lang>" + "|".join(re.escape(lang) for lang in LANGS) + ")"

READ_FOOTER = "🌐 Tap a language below to read this in your language"
FAILED_TEXT = "Sorry, I couldn't post in this channel. Check that I can send messages and embed links here."
GONE_TEXT = "This announcement is no longer available."

SYSTEM = """You translate announcements for an Overwatch community Discord server from {src} into {dst}.
Rules:
- Keep the Discord formatting exactly: **bold**, *italics*, __underline__, ||spoilers||, `code`, code blocks, \
bullet characters, line breaks, emoji, links and mentions like <@123>, <#123>, <:name:123>.
- Bot commands stay unchanged, e.g. `@mrmeeseeks help`, `@mrmeeseeks player Name#1234`. Free-text example \
questions after @mrmeeseeks may be translated.
- Use the official {dst} names Blizzard uses for Overwatch heroes, abilities, maps and game terms \
(for Simplified Chinese the China server names, for Traditional Chinese the Taiwan server names).
- Keep the tone. Do not add, drop or explain anything.
Reply with only the translation in this format:
<title>translated title</title>
<body>
translated body
</body>
If the input has no <title>, reply with only the <body> part."""


# ---------------- 纯函数 ----------------
def split_body(text: str, limit: int = EMBED_DESC_LIMIT) -> list[str]:
    """把正文切成每段不超过 limit 的块：优先在空行处切，其次换行，实在没有就硬切。"""
    chunks: list[str] = []
    rest = text.strip("\n")
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit + 1)
        if cut <= 0:
            cut = rest.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip("\n"))
        rest = rest[cut:].lstrip("\n")
    chunks.append(rest)
    return chunks


def pack_embeds(title: str, body: str, footer: str | None = None) -> list[list[discord.Embed]]:
    """标题 + 正文 -> 若干条消息，每条是一组 embed（合计不超过 Discord 的字符和数量上限）。"""
    messages: list[list[discord.Embed]] = [[]]
    size = 0
    for i, chunk in enumerate(split_body(body)):
        embed = discord.Embed(description=chunk, color=COLOR)
        n = len(chunk)
        if i == 0 and title:
            embed.title = title[:256]
            n += len(embed.title)
        if messages[-1] and (size + n > MESSAGE_EMBED_BUDGET or len(messages[-1]) >= MAX_EMBEDS):
            messages.append([])
            size = 0
        messages[-1].append(embed)
        size += n
    if footer:
        messages[-1][-1].set_footer(text=footer)
    return messages


def parse_translation(text: str) -> tuple[str, str]:
    body = re.search(r"<body>(.*?)(?:</body>|\Z)", text, re.S)
    if not body or not body.group(1).strip():
        raise ValueError("translation has no <body>")
    title = re.search(r"<title>(.*?)</title>", text, re.S)
    return (title.group(1).strip() if title else ""), body.group(1).strip("\n").strip()


# ---------------- 翻译 ----------------
class Translator:
    def __init__(self, client, model: str, effort: str | None = None):
        self.client = client
        self.model = model
        self.effort = effort

    async def translate(self, title: str, body: str, src: str, dst: str) -> tuple[str, str]:
        content = (f"<title>{title}</title>\n" if title else "") + f"<body>\n{body}\n</body>"
        resp = await self.client.messages.create(
            model=self.model, max_tokens=MAX_TOKENS,
            system=SYSTEM.format(src=PROMPT_NAMES[src], dst=PROMPT_NAMES[dst]),
            messages=[{"role": "user", "content": content}],
            **({"output_config": {"effort": self.effort}} if self.effort else {}))
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if resp.stop_reason == "max_tokens" or not text:
            raise RuntimeError(f"no translation (stop_reason={resp.stop_reason})")
        out_title, out_body = parse_translation(text)
        return (out_title if title else ""), out_body


# ---------------- 存档 ----------------
class AnnouncementStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def save(self, record: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / f"{record['key']}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def load(self, key: str) -> dict[str, Any] | None:
        if not KEY_RE.fullmatch(key):
            return None
        try:
            return json.loads((self.dir / f"{key}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


# ---------------- 草稿与翻译 ----------------
@dataclass
class Draft:
    author_id: int
    source: str
    title: str
    body: str
    key: str = field(default_factory=lambda: secrets.token_hex(6))
    texts: dict[str, dict[str, str]] = field(default_factory=dict)
    failed: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.texts.setdefault(self.source, {"title": self.title, "body": self.body})


class AnnounceDesk:
    def __init__(self, translator: Translator, store: AnnouncementStore):
        self.translator = translator
        self.store = store

    async def translate(self, draft: Draft) -> list[str]:
        """翻译还缺的语言，返回这次新翻好的语言。失败的记在 draft.failed。"""
        todo = [lang for lang in LANGS if lang not in draft.texts]
        outs = await asyncio.gather(*(self.translator.translate(draft.title, draft.body, draft.source, lang)
                                      for lang in todo), return_exceptions=True)
        done, draft.failed = [], []
        for lang, out in zip(todo, outs):
            if isinstance(out, BaseException):
                log.warning("Translation to %s failed: %r", lang, out)
                draft.failed.append(lang)
            else:
                draft.texts[lang] = {"title": out[0], "body": out[1]}
                done.append(lang)
        return done


def status_text(draft: Draft) -> str:
    if draft.failed:
        names = ", ".join(LANGS[lang] for lang in draft.failed)
        return f"⚠️ Translation failed for: {names}. Press **Retry**, or **Cancel**."
    return (f"Previews above ({len(LANGS)} languages). **Publish** posts the {LANGS[draft.source]} original "
            f"in this channel with a button for each language.")


async def send_previews(interaction: discord.Interaction, draft: Draft, langs: list[str]) -> None:
    for lang in langs:
        text = draft.texts[lang]
        for embeds in pack_embeds(text["title"], text["body"], footer=f"Preview · {LANGS[lang]}"):
            await interaction.followup.send(embeds=embeds, ephemeral=True)


# ---------------- 界面 ----------------
class StartView(discord.ui.View):
    """表单只能由按钮或斜杠命令弹出，所以 @ 指令先回一个按钮。只有发指令的人能点。"""

    def __init__(self, desk: AnnounceDesk, owner_id: int):
        super().__init__(timeout=600)
        self.desk = desk
        self.owner_id = owner_id

    @discord.ui.button(label="Write announcement", emoji="📢", style=discord.ButtonStyle.primary)
    async def write(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who ran the command can use this.",
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(AnnounceModal(self.desk))


class AnnounceModal(discord.ui.Modal, title="Write an announcement"):
    def __init__(self, desk: AnnounceDesk):
        super().__init__()
        self.desk = desk
        self.language = discord.ui.Select(
            placeholder="The language you're writing in",
            options=[discord.SelectOption(label=name, value=lang) for lang, name in LANGS.items()])
        self.ann_title = discord.ui.TextInput(max_length=MAX_TITLE, required=False,
                                              placeholder="e.g. mrmeeseeks v1.1 is here")
        self.body = discord.ui.TextInput(style=discord.TextStyle.paragraph, max_length=MAX_BODY,
                                         placeholder="Discord formatting (**bold**, lists, emoji) is kept")
        for text, item in (("Written in", self.language), ("Title (optional)", self.ann_title),
                           ("Announcement", self.body)):
            self.add_item(discord.ui.Label(text=text, component=item))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        draft = Draft(interaction.user.id, self.language.values[0], self.ann_title.value.strip(),
                      self.body.value.strip())
        await self.desk.translate(draft)
        await send_previews(interaction, draft, list(draft.texts))
        view = PreviewView(self.desk, draft)
        view.message = await interaction.followup.send(status_text(draft), view=view, ephemeral=True, wait=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Announcement form failed", exc_info=error)
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("Something went wrong, please try again.", ephemeral=True)


class PreviewView(discord.ui.View):
    def __init__(self, desk: AnnounceDesk, draft: Draft):
        super().__init__(timeout=PREVIEW_TTL)
        self.desk = desk
        self.draft = draft
        self.message = None
        self._sync()

    def _sync(self) -> None:
        self.publish.disabled = bool(self.draft.failed)
        self.retry.disabled = not self.draft.failed

    async def _not_author(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.draft.author_id:
            return False
        await interaction.response.send_message("Only the author can do this.", ephemeral=True)
        return True

    @discord.ui.button(label="Publish", emoji="📢", style=discord.ButtonStyle.success)
    async def publish(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._not_author(interaction):
            return
        draft = self.draft
        src = draft.texts[draft.source]
        try:
            msg = await interaction.channel.send(embeds=pack_embeds(src["title"], src["body"], READ_FOOTER)[0],
                                                 view=published_view(draft.key),
                                                 allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.exception("Could not publish announcement %s", draft.key)
            await interaction.response.send_message(FAILED_TEXT, ephemeral=True)
            return
        self.desk.store.save({"key": draft.key, "source": draft.source, "author_id": draft.author_id,
                              "channel_id": interaction.channel.id, "message_id": msg.id,
                              "created_at": int(time.time()), "texts": draft.texts})
        self.stop()
        await interaction.response.edit_message(content=f"✅ Published: {msg.jump_url}", view=None)

    @discord.ui.button(label="Retry", emoji="🔁", style=discord.ButtonStyle.primary)
    async def retry(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._not_author(interaction):
            return
        self.publish.disabled = self.retry.disabled = True
        await interaction.response.edit_message(content="Translating again…", view=self)
        done = await self.desk.translate(self.draft)
        await send_previews(interaction, self.draft, done)
        self._sync()
        await interaction.edit_original_response(content=status_text(self.draft), view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if await self._not_author(interaction):
            return
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was posted.", view=None)

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(content="Preview expired. Run `@mrmeeseeks announce` again.", view=None)
            except discord.HTTPException:
                pass


class LangButton(discord.ui.DynamicItem[discord.ui.Button], template=TEMPLATE):
    handler: AnnounceDesk | None = None     # bot 启动时设置

    def __init__(self, key: str, lang: str):
        super().__init__(discord.ui.Button(label=LANGS[lang], style=discord.ButtonStyle.secondary,
                                           custom_id=f"ann:{key}:{lang}"))
        self.key, self.lang = key, lang

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str]) -> "LangButton":
        return cls(match["key"], match["lang"])

    async def callback(self, interaction: discord.Interaction) -> None:
        record = LangButton.handler.store.load(self.key) if LangButton.handler else None
        text = (record or {}).get("texts", {}).get(self.lang)
        if not text:
            await interaction.response.send_message(GONE_TEXT, ephemeral=True)
            return
        first, *rest = pack_embeds(text["title"], text["body"])
        await interaction.response.send_message(embeds=first, ephemeral=True)
        for embeds in rest:
            await interaction.followup.send(embeds=embeds, ephemeral=True)


def published_view(key: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for lang in LANGS:
        view.add_item(LangButton(key, lang))
    return view
