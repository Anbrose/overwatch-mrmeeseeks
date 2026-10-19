"""多语言公告测试：分段、embed 打包、翻译请求与解析、存档、表单 -> 预览 -> 发布 -> 语言按钮、bot 路由。

运行：python tests/test_announce.py
"""
import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import discord  # noqa: E402

import announce as an  # noqa: E402
import bot  # noqa: E402

results = []
NS = types.SimpleNamespace
TMP = Path(tempfile.mkdtemp())


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


# ---------------- 纯函数 ----------------
def test_split_body():
    check("split: short text unchanged", an.split_body("hello", 10) == ["hello"])
    text = "aaaa\n\nbbbb\n\ncccc"
    check("split: prefers paragraph breaks", an.split_body(text, 11) == ["aaaa\n\nbbbb", "cccc"])
    check("split: falls back to line breaks", an.split_body("aaaa\nbbbb\ncccc", 9) == ["aaaa\nbbbb", "cccc"])
    chunks = an.split_body("x" * 25, 10)
    check("split: hard cut when no breaks", chunks == ["x" * 10, "x" * 10, "x" * 5])
    long = "\n\n".join(f"paragraph {i} " + "y" * 300 for i in range(40))
    chunks = an.split_body(long, an.EMBED_DESC_LIMIT)
    check("split: every chunk fits an embed", all(len(c) <= an.EMBED_DESC_LIMIT for c in chunks) and len(chunks) > 1)
    check("split: nothing lost", "".join(chunks).replace("\n", "") == long.replace("\n", ""))


def test_pack_embeds():
    msgs = an.pack_embeds("Title", "short body")
    check("pack: one message, one embed with title", len(msgs) == 1 and len(msgs[0]) == 1
          and msgs[0][0].title == "Title" and msgs[0][0].description == "short body")
    body = "\n\n".join("z" * 1000 for _ in range(12))         # ~12k 字符，必须拆成多条消息
    msgs = an.pack_embeds("T", body)
    sizes = [sum(len(e.title or "") + len(e.description or "") + len(e.footer.text or "") for e in m) for m in msgs]
    check("pack: each message under Discord's 6000-char embed total", all(s <= 6000 for s in sizes) and len(msgs) > 1)
    check("pack: at most 10 embeds per message", all(len(m) <= 10 for m in msgs))
    check("pack: title only on the first embed", msgs[0][0].title == "T"
          and all(not e.title for m in msgs for e in m[1:]) and all(not m[0].title for m in msgs[1:]))
    msgs = an.pack_embeds("", "body", footer="🌐 hi")
    check("pack: no title -> no title; footer on last embed", not msgs[0][0].title and msgs[-1][-1].footer.text == "🌐 hi")


def test_parse_translation():
    check("parse: title and body", an.parse_translation("<title>Hi</title>\n<body>\nLine 1\nLine 2\n</body>")
          == ("Hi", "Line 1\nLine 2"))
    check("parse: body only", an.parse_translation("<body>Only</body>") == ("", "Only"))
    try:
        an.parse_translation("no tags here")
        check("parse: missing body raises", False)
    except ValueError:
        check("parse: missing body raises", True)


# ---------------- 翻译 ----------------
class FakeClaude:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, tuple):          # (stop_reason, text)
            return NS(stop_reason=reply[0], content=[NS(type="text", text=reply[1])])
        return NS(stop_reason="end_turn", content=[NS(type="thinking", thinking="…"), NS(type="text", text=reply)])


async def test_translator():
    claude = FakeClaude(["<title>お知らせ</title><body>本文</body>"])
    tr = an.Translator(claude, "claude-test", effort="low")
    out = await tr.translate("公告", "正文", "zh-Hans", "ja")
    call = claude.calls[0]
    check("translator: parsed result", out == ("お知らせ", "本文"))
    check("translator: names both languages in the prompt", "Simplified Chinese" in call["system"]
          and "Japanese" in call["system"])
    check("translator: sends title and body", "公告" in call["messages"][0]["content"]
          and "正文" in call["messages"][0]["content"])
    check("translator: passes model and effort", call["model"] == "claude-test"
          and call["output_config"] == {"effort": "low"})
    out = await an.Translator(FakeClaude(["<body>Body</body>"]), "m").translate("", "正文", "zh-Hans", "en")
    check("translator: no title -> prompt says so, result has none", out == ("", "Body"))
    try:
        await an.Translator(FakeClaude([("max_tokens", "<body>cut")]), "m").translate("", "x", "en", "ko")
        check("translator: truncated reply raises", False)
    except RuntimeError:
        check("translator: truncated reply raises", True)


# ---------------- 存档 ----------------
def test_store():
    store = an.AnnouncementStore(TMP / "ann")
    rec = {"key": "0123456789ab", "source": "en", "texts": {"en": {"title": "T", "body": "B"}}}
    store.save(rec)
    check("store: roundtrip", store.load("0123456789ab") == rec)
    check("store: unknown key -> None", store.load("ffffffffffff") is None)
    check("store: invalid key -> None (no path tricks)", store.load("../../etc") is None)


# ---------------- 交互 ----------------
class FakeResponse:
    def __init__(self):
        self.sent, self.modals, self.deferred, self.edited = [], [], False, None

    def is_done(self):
        return self.deferred or bool(self.sent) or bool(self.modals)

    async def send_message(self, content=None, ephemeral=False, view=None, embeds=None):
        self.sent.append(NS(content=content, ephemeral=ephemeral, view=view, embeds=embeds))

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def defer(self, ephemeral=False, thinking=False):
        self.deferred = True

    async def edit_message(self, content=None, view=None):
        self.edited = NS(content=content, view=view)


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, ephemeral=False, view=None, embeds=None, wait=False):
        self.sent.append(NS(content=content, ephemeral=ephemeral, view=view, embeds=embeds))
        return NS(edit=self._edit)

    async def _edit(self, content=None, view=None):
        self.sent.append(NS(content=content, ephemeral=True, view=view, embeds=None, edit=True))


class FakeChannel:
    def __init__(self, fail=False):
        self.id, self.sent, self.fail = 42, [], fail

    async def send(self, content=None, embeds=None, view=None, allowed_mentions=None):
        if self.fail:
            raise discord.Forbidden(NS(status=403, reason="Forbidden"), "Missing Access")
        self.sent.append(NS(content=content, embeds=embeds, view=view, allowed_mentions=allowed_mentions))
        return NS(id=999, jump_url="https://discord.com/channels/1/42/999")


def interaction(user_id=7, channel=None):
    inter = NS(user=NS(id=user_id, mention=f"<@{user_id}>"), response=FakeResponse(), followup=FakeFollowup(),
               channel=channel or FakeChannel(), original_edits=[])

    async def edit_original_response(content=None, view=None):
        inter.original_edits.append(NS(content=content, view=view))
    inter.edit_original_response = edit_original_response
    return inter


def translations(*langs, fail=()):
    """按 desk 的翻译顺序（目标语言按 LANGS 顺序）生成假回复。"""
    return [RuntimeError("api down") if lang in fail else f"<title>T-{lang}</title><body>B-{lang}</body>"
            for lang in langs]


def make_desk(replies):
    return an.AnnounceDesk(an.Translator(FakeClaude(replies), "m"), an.AnnouncementStore(TMP / "flow"))


def fill(modal, source, title, body):
    modal.language._values = [source]
    modal.ann_title._value = title
    modal.body._value = body


async def test_flow():
    targets = [lang for lang in an.LANGS if lang != "zh-Hans"]
    desk = make_desk(translations(*targets))
    an.LangButton.handler = desk

    # 入口按钮：只有发指令的人能点
    start = an.StartView(desk, owner_id=7)
    inter = interaction(user_id=8)
    await start.write.callback(inter)
    check("start: others can't open the form", inter.response.sent and inter.response.sent[0].ephemeral
          and not inter.response.modals)
    inter = interaction(user_id=7)
    await start.write.callback(inter)
    check("start: owner gets the form", len(inter.response.modals) == 1)
    modal = inter.response.modals[0]
    check("modal: language picker lists all 5 languages",
          [o.value for o in modal.language.options] == list(an.LANGS))

    # 提交表单 -> 每种语言一条私密预览 + 一条带按钮的控制消息
    fill(modal, "zh-Hans", "公告", "正文")
    inter = interaction(user_id=7)
    await modal.on_submit(inter)
    previews = [m for m in inter.followup.sent if m.embeds]
    controls = [m for m in inter.followup.sent if m.view is not None]
    check("submit: deferred while translating", inter.response.deferred)
    check("submit: one preview per language, all ephemeral", len(previews) == 5 and all(m.ephemeral for m in previews))
    check("submit: preview shows the translation", any(e.description == "B-ja" for m in previews for e in m.embeds))
    check("submit: preview keeps the original", any(e.description == "正文" for m in previews for e in m.embeds))
    view = controls[-1].view
    check("submit: publish enabled when every language succeeded", not view.publish.disabled)

    # 发布
    channel = FakeChannel()
    inter = interaction(user_id=7, channel=channel)
    await view.publish.callback(inter)
    posted = channel.sent[0]
    ids = [c.custom_id for c in posted.view.children]
    key = view.draft.key
    check("publish: posts the original in the channel", posted.embeds[0].title == "公告"
          and posted.embeds[0].description == "正文")
    check("publish: five language buttons", ids == [f"ann:{key}:{lang}" for lang in an.LANGS])
    check("publish: no pings", posted.allowed_mentions.everyone is False)
    rec = desk.store.load(key)
    check("publish: record saved with all texts", rec and set(rec["texts"]) == set(an.LANGS))
    check("publish: control message updated, buttons gone", inter.response.edited and inter.response.edited.view is None)

    # 成员点语言按钮
    btn = an.LangButton(key, "ko")
    inter = interaction(user_id=50)
    await btn.callback(inter)
    sent = inter.response.sent[0]
    check("button: ephemeral reply in that language", sent.ephemeral and sent.embeds[0].description == "B-ko"
          and sent.embeds[0].title == "T-ko")
    inter = interaction(user_id=50)
    await an.LangButton("ffffffffffff", "ko").callback(inter)
    check("button: unknown announcement -> polite message", inter.response.sent[0].ephemeral
          and "no longer" in inter.response.sent[0].content)
    built = an.LangButton(key, "ja")
    check("button: survives restart via custom_id template",
          an.LangButton.__discord_ui_compiled_template__.fullmatch(built.item.custom_id) is not None)


async def test_retry_and_failures():
    targets = [lang for lang in an.LANGS if lang != "en"]
    desk = make_desk(translations(*targets, fail=("ja",)) + translations("ja"))
    modal = an.AnnounceModal(desk)
    fill(modal, "en", "", "Hello")
    inter = interaction(user_id=7)
    await modal.on_submit(inter)
    view = [m for m in inter.followup.sent if m.view is not None][-1].view
    check("failure: publish disabled while a language failed", view.publish.disabled)
    check("failure: control message names the failed language", "日本語" in
          [m for m in inter.followup.sent if m.view is not None][-1].content)
    check("failure: retry offered", not view.retry.disabled)

    inter = interaction(user_id=7)
    await view.retry.callback(inter)
    check("retry: only the failed language is translated again", len(desk.translator.client.calls) == 5)
    check("retry: publish enabled after the retry", not view.publish.disabled)
    check("retry: control message shows the new status", inter.original_edits
          and "Publish" in inter.original_edits[-1].content)
    check("retry: sends the new preview", any(e.description == "B-ja" for m in inter.followup.sent if m.embeds
                                              for e in m.embeds))

    others = interaction(user_id=8)
    await view.publish.callback(others)
    check("preview: only the author can publish", others.response.sent and others.response.sent[0].ephemeral)

    inter = interaction(user_id=7, channel=FakeChannel(fail=True))
    await view.publish.callback(inter)
    check("publish: missing permission -> ephemeral error", inter.response.sent
          and "couldn't" in inter.response.sent[0].content)

    inter = interaction(user_id=7)
    await view.cancel.callback(inter)
    check("cancel: closes the preview", inter.response.edited and inter.response.edited.view is None)


# ---------------- bot 路由 ----------------
def make_bot(**attrs):
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    b.wish_channel_id, b.wish_desk, b.wish_plans, b.announce_desk = None, None, None, None
    b._connection = NS(user=NS(id=1))
    b.__dict__.update(attrs)
    return b


def message(manage):
    replies = []

    async def reply(content=None, **kw):
        replies.append(NS(content=content, view=kw.get("view")))

    perms = NS(manage_guild=manage)
    channel = NS(id=9, permissions_for=lambda _: perms)
    return NS(author=NS(id=7, bot=False), mentions=[NS(id=1)], content="<@1> announce", channel=channel,
              guild=NS(), reply=reply), replies


async def test_bot():
    check("bot: announce is a command", bot.parse_command("announce")[0] == "announce")
    check("bot: help lists announce", "announce" in bot.HELP_TEXT)
    msg, replies = message(manage=True)
    await make_bot().on_message(msg)
    check("bot: without API key explains why", "ANTHROPIC_API_KEY" in replies[0].content)
    desk = make_desk([])
    msg, replies = message(manage=False)
    await make_bot(announce_desk=desk).on_message(msg)
    check("bot: needs Manage Server", "Manage Server" in replies[0].content and replies[0].view is None)
    msg, replies = message(manage=True)
    await make_bot(announce_desk=desk).on_message(msg)
    check("bot: admin gets the start button", isinstance(replies[0].view, an.StartView)
          and replies[0].view.owner_id == 7)
    added = []
    b = make_bot(announce_desk=desk, wish_view=None, prediction_ui=None)
    b.add_dynamic_items = lambda *items: added.extend(items)
    await b.setup_hook()
    check("bot: language buttons registered at startup", an.LangButton in added and an.LangButton.handler is desk)


async def run_async():
    await test_translator()
    await test_flow()
    await test_retry_and_failures()
    await test_bot()


if __name__ == "__main__":
    test_split_body()
    test_pack_embeds()
    test_parse_translation()
    test_store()
    asyncio.run(run_async())
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
