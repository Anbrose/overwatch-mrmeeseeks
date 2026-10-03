"""功能许愿频道测试：每日限额、存档、导出、表单提交、入口按钮、入口消息、方案起草与修改、bot 路由。

运行：python tests/test_wishes.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import discord  # noqa: E402

import bot  # noqa: E402
import wishes  # noqa: E402
import wish_plan  # noqa: E402
from wish_plan import MAX_REVISIONS, WishPlanner, WishPlans, plan_message, server_layout  # noqa: E402
from wishes import (DEFAULT_GAMES, ENTRY_TEXT, OTHER, PICK_TEXT, WISH_TZ,  # noqa: E402
                    GameNameModal, GamePickView, WishDesk, WishEntryView, WishLog, WishModal, WishQuota,
                    ensure_entry, export_csv, parse_export_args, parse_games, select_wishes, vote_count)

results = []
NS = types.SimpleNamespace
TMP = Path(tempfile.mkdtemp())


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def sydney(y, m, d, hh=12):
    return datetime(y, m, d, hh, tzinfo=WISH_TZ)


# ---------------- 限额 ----------------
def test_quota():
    path = TMP / "q1.json"
    q = WishQuota(path)
    day = sydney(2026, 10, 3)
    check("quota: fresh user has 2", q.remaining(1, day) == 2)
    check("quota: first take ok", q.take(1, day))
    check("quota: second take ok", q.take(1, day))
    check("quota: third take refused", not q.take(1, day))
    check("quota: other user unaffected", q.remaining(2, day) == 2)

    q.refund(1, day)
    check("quota: refund gives one back", q.remaining(1, day) == 1)

    reloaded = WishQuota(path)
    check("quota: persisted across restart", reloaded.remaining(1, day) == 1)

    # 悉尼 23:30 和次日 00:10 属于不同的一天
    q2 = WishQuota(TMP / "q2.json")
    q2.take(1, sydney(2026, 10, 3, 23))
    q2.take(1, sydney(2026, 10, 3, 23))
    check("quota: exhausted late at night", q2.remaining(1, sydney(2026, 10, 3, 23)) == 0)
    check("quota: resets at Sydney midnight", q2.remaining(1, sydney(2026, 10, 4, 0)) == 2)

    bad = TMP / "bad.json"
    bad.write_text("{not json")
    check("quota: corrupt file starts empty", WishQuota(bad).remaining(1) == 2)

    q3 = WishQuota(TMP / "q3.json")
    q3.entry_message_id = 42
    check("quota: entry message id persisted", WishQuota(TMP / "q3.json").entry_message_id == 42)
    check("quota: entry id survives day reset", json.loads((TMP / "q3.json").read_text())["entry_message_id"] == 42)


# ---------------- 假 Discord 对象 ----------------
class FakeResponse:
    def __init__(self):
        self.sent, self.modals, self.deferred = [], [], False

    def is_done(self):
        return self.deferred or bool(self.sent) or bool(self.modals)

    async def send_message(self, content, ephemeral=False, view=None):
        self.sent.append((content, ephemeral))
        self.view = view

    async def edit_message(self, content=None, view=None):
        self.edited = (content, view)

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def defer(self, ephemeral=False, thinking=False):
        self.deferred = True


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content, ephemeral=False):
        self.sent.append((content, ephemeral))


class FakeMessage:
    def __init__(self, id_=100, content=""):
        self.id, self.content = id_, content
        self.jump_url = f"https://discord.com/channels/1/2/{id_}"
        self.reactions, self.threads, self.pinned, self.edits = [], [], False, []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def create_thread(self, name, auto_archive_duration=None):
        self.threads.append(name)
        return FakeThread(self.id, parent_id=555)

    async def pin(self):
        self.pinned = True

    async def edit(self, content=None, view=None):
        self.edits.append(content)
        self.content = content


class FakeChannel:
    def __init__(self, fail_send=False, existing=None):
        self.posts, self.fail_send, self.existing = [], fail_send, existing or {}

    async def send(self, content=None, embed=None, view=None):
        if self.fail_send:
            raise discord.HTTPException(NS(status=500, reason="boom"), "boom")
        msg = FakeMessage(200 + len(self.posts), content or "")
        self.posts.append((content, embed, view, msg))
        return msg

    async def fetch_message(self, mid):
        if mid not in self.existing:
            raise discord.NotFound(NS(status=404, reason="nf"), "nf")
        return self.existing[mid]


class FakeThread:
    def __init__(self, id_, parent_id=555, guild=None):
        self.id, self.parent_id, self.guild = id_, parent_id, guild or fake_guild()
        self.sent = []

    async def send(self, content, allowed_mentions=None):
        self.sent.append((content, allowed_mentions))

    def typing(self):
        return Typing()


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def fake_guild():
    return NS(by_category=lambda: [
        (NS(name="Overwatch"), [NS(name="ow-chat", topic="Talk OW"), NS(name="ow-lfg", topic=None)]),
        (None, [NS(name="general", topic="")])])


def fake_user(uid=7):
    return NS(id=uid, display_name="Ana", display_avatar=NS(url="https://cdn/avatar.png"))


def fake_interaction(channel, uid=7):
    return NS(user=fake_user(uid), channel=channel, response=FakeResponse(), followup=FakeFollowup())


def fill(modal, title="Ult tracker", problem="I can't see enemy ults", how="", notes="", game="Overwatch"):
    modal.game._values = [game]
    modal.wish_title._value, modal.problem._value = title, problem
    modal.how._value, modal.notes._value = how, notes


def make_desk(name, games=DEFAULT_GAMES, on_posted=None):
    return WishDesk(WishQuota(TMP / f"{name}.json"), WishLog(TMP / f"{name}-log"), games, on_posted)


def wish_record(id_, game="Overwatch", created_at="2026-10-01T00:00:00+00:00", plans=None):
    return {"id": id_, "created_at": created_at, "user_id": 7, "user_name": "ana", "game": game,
            "title": f"Wish {id_}", "problem": "p", "how": "", "notes": "", "url": f"u/{id_}",
            "plans": plans or []}


def test_games():
    check("games: default when empty", parse_games("") == DEFAULT_GAMES)
    check("games: default has Overwatch first, Other last, ≤25, all bilingual",
          DEFAULT_GAMES[0] == "Overwatch ｜ 守望先锋" and DEFAULT_GAMES[-1] == OTHER and len(DEFAULT_GAMES) <= 25
          and all(" ｜ " in g for g in DEFAULT_GAMES))
    check("games: comma list trimmed, deduped, Other moved to the end",
          parse_games(" Overwatch, other, Marvel Rivals ,,Overwatch") == ("Overwatch", "Marvel Rivals", OTHER))
    check("games: capped at 25 incl. Other", parse_games(",".join(f"G{i}" for i in range(30)))[-2:] == ("G23", OTHER))


# ---------------- 存档与导出 ----------------
def test_log_and_export():
    wl = WishLog(TMP / "log")
    wl.save(wish_record(2, created_at="2026-10-02T00:00:00+00:00"))
    wl.save(wish_record(1, game="Valorant"))
    check("log: get by id", wl.get(2)["title"] == "Wish 2" and wl.get(99) is None)
    check("log: all sorted by time", [r["id"] for r in wl.all()] == [1, 2])
    wl.add_plan(2, "", "plan A")
    wl.add_plan(2, "use #general", "plan B")
    plans = wl.get(2)["plans"]
    check("log: plans appended with request", [(p["request"], p["text"]) for p in plans]
          == [("", "plan A"), ("use #general", "plan B")])
    (TMP / "log" / "junk.json").write_text("{oops")
    check("log: unreadable file skipped", len(wl.all()) == 2)

    check("export args: empty", parse_export_args("") == (None, None))
    check("export args: game + days", parse_export_args("marvel rivals 7d") == ("marvel rivals", 7))

    now = datetime.fromisoformat("2026-10-03T00:00:00+00:00")
    records = wl.all()
    check("select: by game, case-insensitive", [r["id"] for r in select_wishes(records, "valorant", None, now)] == [1])
    check("select: partial name matches bilingual label",
          [r["id"] for r in select_wishes([wish_record(9, game="Marvel Rivals ｜ 漫威争锋")], "漫威", None, now)] == [9])
    check("select: by days", [r["id"] for r in select_wishes(records, None, 1, now)] == [2])

    data = export_csv(records + [wish_record(3)], {1: 4, 2: 9, 3: None}).decode("utf-8-sig")
    lines = data.strip().splitlines()
    check("export: header", lines[0].startswith("created_at,game,title,votes"))
    check("export: most votes first, deleted last", [l.split(",")[2] for l in lines[1:]] == ["Wish 2", "Wish 1", "Wish 3"])
    check("export: latest plan + revision count", "plan B" in lines[1] and ",1,plan B," in lines[1])
    check("export: BOM for Excel", export_csv(records, {}).startswith("\ufeff".encode("utf-8")))

    msg = NS(reactions=[NS(emoji="🔥", count=3, me=False), NS(emoji="👍", count=5, me=True)])
    check("votes: bot's own 👍 not counted", vote_count(msg) == 4)
    check("votes: no reaction is 0", vote_count(NS(reactions=[])) == 0)


# ---------------- 表单与按钮 ----------------
async def test_modal():
    layout = WishModal(make_desk("layout", ("Overwatch", "Marvel Rivals")))
    labels = [c.text for c in layout.children]
    check("modal: 5 fields, game first", len(labels) == 5 and labels[0] == "Which game?")
    check("modal: game options from config",
          [o.label for o in layout.game.options] == ["Overwatch", "Marvel Rivals"] and layout.game.required)

    posted = []

    async def on_posted(record, thread):
        posted.append((record, thread))

    desk = make_desk("m", on_posted=on_posted)
    ch = FakeChannel()
    modal = WishModal(desk)
    fill(modal, how="Show % next to names", game="Marvel Rivals")
    it = fake_interaction(ch)
    await modal.on_submit(it)
    content, embed, _, msg = ch.posts[0]
    check("modal: posts an embed", embed is not None and embed.title == "💡 Ult tracker")
    check("modal: embed has game + problem + how, skips empty notes",
          [f.name for f in embed.fields] == ["Game", "What problem does it solve?", "How should it work?"])
    check("modal: chosen game shown", embed.fields[0].value == "Marvel Rivals")
    check("modal: author shown", embed.author.name == "Ana")
    check("modal: vote reaction added", msg.reactions == ["👍"])
    check("modal: discussion thread created", msg.threads == ["[Marvel Rivals] Ult tracker"])
    check("modal: deferred before posting", it.response.deferred)
    check("modal: ephemeral confirmation with remaining",
          it.followup.sent and it.followup.sent[0][1] and "1 wish(es) left" in it.followup.sent[0][0])
    saved = desk.log.get(msg.id)
    check("modal: wish archived", saved and saved["game"] == "Marvel Rivals" and saved["how"] == "Show % next to names"
          and saved["user_id"] == 7 and saved["url"] == msg.jump_url)
    check("modal: plan hook gets record + thread", len(posted) == 1 and posted[0][0]["id"] == msg.id
          and posted[0][1].id == msg.id)

    modal2 = WishModal(desk)
    fill(modal2)
    await modal2.on_submit(fake_interaction(ch))
    modal3 = WishModal(desk)
    fill(modal3)
    it3 = fake_interaction(ch)
    await modal3.on_submit(it3)
    check("modal: third wish refused", len(ch.posts) == 2 and "used all 2" in it3.response.sent[0][0])
    check("modal: refusal is ephemeral", it3.response.sent[0][1])

    desk2 = make_desk("m2")
    modal4 = WishModal(desk2)
    fill(modal4)
    it4 = fake_interaction(FakeChannel(fail_send=True))
    await modal4.on_submit(it4)
    check("modal: failed post refunds quota", desk2.quota.remaining(7) == 2)
    check("modal: failed post tells user", "wasn't counted" in it4.followup.sent[0][0])
    check("modal: failed post not archived", desk2.log.all() == [])


async def test_button():
    desk = make_desk("b", ("Overwatch", "Valorant"))
    view = WishEntryView(desk)
    check("button: persistent view", view.timeout is None and view.is_persistent())
    it = fake_interaction(FakeChannel())
    await view.submit.callback(it)
    check("button: opens the form", len(it.response.modals) == 1 and isinstance(it.response.modals[0], WishModal)
          and [o.label for o in it.response.modals[0].game.options] == ["Overwatch", "Valorant"])
    desk.quota.take(7)
    desk.quota.take(7)
    it2 = fake_interaction(FakeChannel())
    await view.submit.callback(it2)
    check("button: exhausted user gets no form", not it2.response.modals and "used all 2" in it2.response.sent[0][0])


async def test_entry():
    desk = make_desk("e")
    q, view = desk.quota, WishEntryView(desk)
    ch = FakeChannel()
    await ensure_entry(ch, q, view)
    msg = ch.posts[0][3]
    check("entry: posted with button and pinned", ch.posts[0][2] is view and msg.pinned)
    check("entry: id remembered", q.entry_message_id == msg.id)

    ch2 = FakeChannel(existing={msg.id: FakeMessage(msg.id, ENTRY_TEXT)})
    await ensure_entry(ch2, q, view)
    check("entry: reused when it still exists", not ch2.posts)

    old = FakeMessage(msg.id, "old text")
    ch3 = FakeChannel(existing={msg.id: old})
    await ensure_entry(ch3, q, view)
    check("entry: outdated text is updated in place", not ch3.posts and old.content == ENTRY_TEXT)

    ch4 = FakeChannel()
    await ensure_entry(ch4, q, view)
    check("entry: reposted when deleted", len(ch4.posts) == 1 and q.entry_message_id == ch4.posts[0][3].id)


# ---------------- 选了 Other ----------------
async def test_other_game():
    posted = []

    async def on_posted(record, thread):
        posted.append(record)

    desk = make_desk("o", on_posted=on_posted)
    ch = FakeChannel()
    modal = WishModal(desk)
    fill(modal, game="Valorant ｜ 无畏契约")
    await modal.on_submit(fake_interaction(ch))
    check("popular game: posted directly", len(ch.posts) == 1 and ch.posts[0][1].fields[0].value == "Valorant ｜ 无畏契约")

    modal = WishModal(desk)
    fill(modal, game=OTHER)
    it = fake_interaction(ch)
    await modal.on_submit(it)
    picker = it.response.view
    check("other: asks for the name privately, nothing posted yet",
          len(ch.posts) == 1 and it.response.sent[0] == (PICK_TEXT, True) and isinstance(picker, GamePickView))
    check("other: quota already taken", desk.quota.remaining(7) == 0)

    it2 = fake_interaction(ch)
    await picker.type_name.callback(it2)
    name_modal = it2.response.modals[0]
    check("other: button opens a name form", isinstance(name_modal, GameNameModal))
    name_modal.name._value = "  Deadlock "
    it3 = fake_interaction(ch)
    await name_modal.on_submit(it3)
    embed, msg = ch.posts[1][1], ch.posts[1][3]
    check("other: typed game used everywhere", embed.fields[0].value == "Deadlock"
          and msg.threads == ["[Deadlock] Ult tracker"] and posted[-1]["game"] == "Deadlock")
    check("other: button message replaced", it3.response.edited[1] is None and "Deadlock" in it3.response.edited[0])
    check("other: confirmation sent", "Wish posted" in it3.followup.sent[0][0])
    it4 = fake_interaction(ch)
    await picker.type_name.callback(it4)
    check("other: button after posting does nothing", not it4.response.modals and "already posted" in it4.response.sent[0][0])
    await name_modal.on_submit(fake_interaction(ch))
    check("other: second submit doesn't double-post", len(ch.posts) == 2)

    desk2 = make_desk("o2")
    ch2 = FakeChannel()
    modal = WishModal(desk2)
    fill(modal, game=OTHER)
    it = fake_interaction(ch2)
    await modal.on_submit(it)
    await it.response.view.on_timeout()
    check("other: timeout posts as Other", ch2.posts[0][1].fields[0].value == OTHER
          and desk2.log.get(ch2.posts[0][3].id)["game"] == OTHER)
    await it.response.view.on_timeout()
    check("other: timeout only posts once", len(ch2.posts) == 1)


# ---------------- 方案 ----------------
class FakeClaude:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = self

    async def create(self, **kw):
        self.calls.append(kw)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return NS(stop_reason="end_turn", content=[NS(type="thinking", thinking="…"), NS(type="text", text=reply)])


def member(uid, manage=False):
    return NS(id=uid, bot=False, guild_permissions=NS(manage_guild=manage))


def thread_message(thread, author, text="<@1> x"):
    replies = []

    async def reply(content, **kw):
        replies.append(content)

    return NS(channel=thread, author=author, content=text, mentions=[NS(id=1)], reply=reply), replies


def test_plan_text():
    layout = server_layout(fake_guild())
    check("layout: categories and channels listed",
          "Category: Overwatch" in layout and "#ow-chat [text] (Talk OW)" in layout and "No category:" in layout)

    msg = plan_message(7, "PLAN", 0, 42)
    check("plan msg: pings wisher, names reviewer, explains how to steer",
          msg.startswith("<@7>") and "<@42>" in msg and "`@mrmeeseeks <what to change>`" in msg)
    check("plan msg: no reviewer configured", "Ask a server admin" in plan_message(7, "PLAN", 0, None))
    check("plan msg: revision label", f"revision 2/{MAX_REVISIONS}" in plan_message(7, "PLAN", 2, None))
    long = plan_message(7, "x" * 5000, 0, 42)
    check("plan msg: fits Discord limit and keeps the instructions", len(long) <= 2000 and long.endswith("final review."))


async def test_plans():
    wl = WishLog(TMP / "plans")
    wl.save(wish_record(300, game="Marvel Rivals"))
    claude = FakeClaude(["PLAN v1", "PLAN v2", "PLAN v3"])
    plans = WishPlans(WishPlanner(claude, "m", "low"), wl, "FEATURES", reviewer_id=42)
    thread = FakeThread(300)

    await plans.draft(wl.get(300), thread)
    first = claude.calls[0]
    check("draft: wish, layout and features in prompt",
          "Marvel Rivals" in first["messages"][0]["content"] and "#ow-chat" in first["messages"][0]["content"]
          and "FEATURES" in first["messages"][0]["content"])
    check("draft: effort passed", first["output_config"] == {"effort": "low"})
    content, mentions = thread.sent[0]
    check("draft: posted in thread, pings only the wisher",
          "PLAN v1" in content and [u.id for u in mentions.users] == [7] and "<@42>" in content)
    check("draft: archived", [p["text"] for p in wl.get(300)["plans"]] == ["PLAN v1"])

    msg, replies = thread_message(thread, member(7))
    await plans.handle(msg, "put it in #general")
    convo = claude.calls[1]["messages"]
    check("revise: conversation is wish -> plan -> change",
          [m["role"] for m in convo] == ["user", "assistant", "user"]
          and convo[1]["content"] == "PLAN v1" and "put it in #general" in convo[2]["content"])
    check("revise: posted as revision 1", "revision 1/" in thread.sent[1][0] and "PLAN v2" in thread.sent[1][0])

    msg, _ = thread_message(thread, member(42))
    await plans.handle(msg, "split into two channels")
    convo = claude.calls[2]["messages"]
    check("revise: earlier changes replayed in order",
          [m["content"] for m in convo if m["role"] == "assistant"] == ["PLAN v1", "PLAN v2"]
          and "put it in #general" in convo[2]["content"] and "split into two" in convo[4]["content"])

    msg, replies = thread_message(thread, member(99))
    await plans.handle(msg, "make it purple")
    check("revise: random member refused", "Only the person" in replies[0] and len(claude.calls) == 3)
    msg, replies = thread_message(thread, member(99, manage=True))
    await plans.handle(msg, "")
    check("revise: admin allowed, but empty request asks what to change", "Tell me what to change" in replies[0])

    record = wl.get(300)
    record["plans"] = [{"request": "", "text": f"P{i}"} for i in range(MAX_REVISIONS + 1)]
    wl.save(record)
    msg, replies = thread_message(thread, member(7))
    await plans.handle(msg, "again")
    check("revise: capped", "already been revised" in replies[0] and len(claude.calls) == 3)

    msg, replies = thread_message(FakeThread(404), member(7))
    await plans.handle(msg, "x")
    check("revise: unknown thread", "can't find the wish" in replies[0])

    wl.save(wish_record(301))
    failing = WishPlans(WishPlanner(FakeClaude([RuntimeError("api down")]), "m"), wl, "F")
    t2 = FakeThread(301)
    await failing.draft(wl.get(301), t2)
    check("draft: failure explained, nothing archived",
          "couldn't draft" in t2.sent[0][0] and wl.get(301)["plans"] == [])
    check("draft: busy flag cleared after failure", 301 not in failing.busy)

    retry = WishPlans(WishPlanner(FakeClaude(["PLAN ok"]), "m"), wl, "F")
    msg, _ = thread_message(t2, member(7))
    await retry.handle(msg, "")
    check("retry: @ with no plan yet drafts one", "PLAN ok" in t2.sent[-1][0] and "first plan" in t2.sent[-1][0])


# ---------------- bot 路由 ----------------
def make_bot(**attrs):
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    b.wish_channel_id, b.wish_desk, b.wish_plans = 555, None, None
    b._connection = NS(user=NS(id=1))
    b.__dict__.update(attrs)
    return b


def channel_message(channel, text="<@1> help", author=None):
    replies = []

    async def reply(content, **kw):
        replies.append(content)

    return NS(author=author or member(7), mentions=[NS(id=1)], content=text, channel=channel, reply=reply), replies


async def test_bot_routing():
    b = make_bot()
    msg, replies = channel_message(NS(id=555))
    await b.on_message(msg)
    check("bot: @ in wish channel ignored", not replies)
    msg, replies = channel_message(NS(id=9))
    await b.on_message(msg)
    check("bot: @ elsewhere still works", replies and "commands" in replies[0])

    handled = []

    class Plans:
        reviewer_id = 42

        async def handle(self, message, request):
            handled.append(request)

    b = make_bot(wish_plans=Plans())
    msg, replies = channel_message(FakeThread(300), "<@1> move it to #lfg")
    await b.on_message(msg)
    msg, _ = channel_message(FakeThread(300), "<@1>  Plan ")
    await b.on_message(msg)
    check("bot: @ in wish thread goes to plans, `plan` means redraft", handled == ["move it to #lfg", ""])
    msg, _ = channel_message(FakeThread(300), "thanks")     # 回复 bot 但没写 @
    await b.on_message(msg)
    check("bot: reply-ping in wish thread ignored", len(handled) == 2)
    msg, replies = channel_message(FakeThread(300, parent_id=777), "<@1> help")
    await b.on_message(msg)
    check("bot: other threads unaffected", replies and "commands" in replies[0])

    b = make_bot()
    msg, replies = channel_message(FakeThread(300), "<@1> change it")
    await b.on_message(msg)
    check("bot: no API key explained in thread", "ANTHROPIC_API_KEY" in replies[0])


async def test_export_command():
    desk = make_desk("x", ("Overwatch", "Valorant"))
    desk.log.save(wish_record(500))
    desk.log.save(wish_record(501, game="Valorant"))
    wish_channel = FakeChannel(existing={500: NS(reactions=[NS(emoji="👍", count=3, me=True)])})
    b = make_bot(wish_desk=desk)

    async def _channel(cid):
        return wish_channel
    b._channel = _channel

    msg, replies = channel_message(NS(id=9, typing=lambda: Typing()), "<@1> wishes", member(7))
    await b.on_message(msg)
    check("export: non-admin refused", "Only admins" in replies[0])

    dms = []

    async def send(content, file=None):
        dms.append((content, file))

    admin = member(8, manage=True)
    admin.send = send
    msg, replies = channel_message(NS(id=9, typing=lambda: Typing()), "<@1> wishes", admin)
    await b.on_message(msg)
    csv_text = dms[0][1].fp.read().decode("utf-8-sig")
    check("export: CSV sent by DM", dms and dms[0][1].filename.endswith(".csv") and "📬" in replies[0])
    check("export: live votes, deleted card blank",
          csv_text.splitlines()[1].split(",")[2:4] == ["Wish 500", "2"] and ",Wish 501,," in csv_text)

    msg, replies = channel_message(NS(id=9, typing=lambda: Typing()), "<@1> wishes halo", admin)
    await b.on_message(msg)
    check("export: no match explained", "No wishes match" in replies[0])

    async def closed_dm(content, file=None):
        raise discord.Forbidden(NS(status=403, reason="no"), "no")
    admin.send = closed_dm
    msg, replies = channel_message(NS(id=9, typing=lambda: Typing()), "<@1> wishes valorant", admin)
    await b.on_message(msg)
    check("export: closed DMs explained", "can't DM you" in replies[0])


async def run_async():
    await test_modal()
    await test_button()
    await test_entry()
    await test_other_game()
    await test_plans()
    await test_bot_routing()
    await test_export_command()


if __name__ == "__main__":
    test_quota()
    test_games()
    test_log_and_export()
    test_plan_text()
    asyncio.run(run_async())
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
