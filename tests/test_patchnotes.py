"""官方补丁说明测试：页面解析（真实页面快照）、跨月合并、改版检测、推送频道（首次不补发、发送失败重试、
摘要缓存与降级）、英雄问答的 get_official_patch_notes、patches 指令。

运行：python tests/test_patchnotes.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import bot  # noqa: E402
import patchnotes  # noqa: E402
from hero_qa import SYSTEM, TOOLS, HeroQA  # noqa: E402
from herodata import HeroStore, load_aliases  # noqa: E402
from patch_feed import (PatchFeed, PatchSummarizer, chinese_names, render, split_message)  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "patchnotes")
TMP = tempfile.mkdtemp()
results = []
NS = types.SimpleNamespace


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


LIVE = patchnotes.parse_page(fixture("live.html"))
SEPT = patchnotes.parse_page(fixture("2026-09.html"))


# ---------------- 解析 ----------------
def test_parse():
    check("live: one patch with id, date, title", [(p["id"], p["date"]) for p in LIVE] == [("patch-2026-10-06", "2026-10-06")]
          and LIVE[0]["title"] == "Overwatch Retail Patch Notes – October 6, 2026")
    p = LIVE[0]
    check("live: url points at the patch anchor", p["url"].endswith("/news/patch-notes/#patch-2026-10-06"))
    check("live: sections as headings", "## Reign of Talon - Season 5: A Grim Doctrine" in p["text"]
          and "## Hero Updates" in p["text"] and "## Hero Hit Volumes" in p["text"])
    check("live: 40 heroes incl. accented and new ones",
          len(p["heroes"]) == 40 and {"Lúcio", "Torbjörn", "D.Va", "Jetpack Cat", "Domina"} <= set(p["heroes"]))
    cass = p["heroes"]["Cassidy"]
    check("hero: section tag, dev comment, ability, nested change",
          cass[0] == "[Damage]" and cass[1].startswith("Developer Comments:") and "Deadeye:" in cass
          and "  - Now prevents attaching to Jetpack Cat's Lifeline." in cass)
    check("hero: appears in a second section (hit volumes) under the same hero",
          "[Hero Hit Volumes]" in cass and "- Torso hit volume reduced from 37 to 33." in cass)
    hog = p["heroes"]["Roadhog"]
    check("hero: rework details with numbers", hog[0] == "[Hero Reworks]"
          and "  - Ammo increased from 6 to 12." in hog and "Trash Compactor:" in hog)
    check("text: general updates are not attributed to a hero",
          not any("Mystery Mayhem" in line for lines in p["heroes"].values() for line in lines)
          and "### New Event - Mystery Mayhem: Graveyard Games" in p["text"])
    check("text: entities decoded", "Junkenstein’s Lab" in p["text"] and "&#" not in p["text"])

    check("archive: four September patches, newest first",
          [x["date"] for x in SEPT] == ["2026-09-22", "2026-09-17", "2026-09-10", "2026-09-08"])
    check("archive: small patches without hero changes parse", SEPT[0]["heroes"] == {} and len(SEPT[0]["text"]) > 50)
    check("garbage page parses to nothing", patchnotes.parse_page("<html><body>maintenance</body></html>") == [])


def test_merge_and_months():
    merged = patchnotes.merge(LIVE, SEPT, SEPT)
    check("merge: deduped, newest first", [p["date"] for p in merged] ==
          ["2026-10-06", "2026-09-22", "2026-09-17", "2026-09-10", "2026-09-08"])
    check("merge: capped", len(patchnotes.merge(LIVE, SEPT, keep=2)) == 2)
    check("previous month: normal", patchnotes.previous_month(date(2026, 10, 8)) == (2026, 9))
    check("previous month: January wraps", patchnotes.previous_month(date(2027, 1, 3)) == (2026, 12))


async def test_fetch():
    pages = {}

    async def fake_page(session, url):
        return pages[url]

    real_page = patchnotes._page
    patchnotes._page = fake_page
    try:
        pages[patchnotes.BASE_URL] = fixture("live.html")
        pages[patchnotes.BASE_URL + "live/2026/09/"] = fixture("2026-09.html")
        got = await patchnotes.fetch_recent(date(2026, 10, 8))
        check("fetch: current + previous month", [p["date"] for p in got][:2] == ["2026-10-06", "2026-09-22"]
              and len(got) == 5)
        pages[patchnotes.BASE_URL] = "<html>new design</html>"
        try:
            await patchnotes.fetch_recent(date(2026, 10, 8))
            check("fetch: redesign detected", False)
        except patchnotes.PatchNotesUnavailable:
            check("fetch: redesign detected", True)
    finally:
        patchnotes._page = real_page


# ---------------- 推送频道 ----------------
class FakeSummarizer:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def summarize(self, patch):
        self.calls.append(patch["id"])
        if self.fail:
            raise RuntimeError("api down")
        return f"SUMMARY of {patch['date']}"


def make_feed(name, patches_list, summarizer=None):
    box = {"patches": patches_list, "fail": False}

    async def fetch():
        if box["fail"]:
            raise RuntimeError("network")
        return box["patches"]

    return PatchFeed(os.path.join(TMP, f"{name}.json"), fetch=fetch, summarizer=summarizer), box


class Sender:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def __call__(self, channel_id, text):
        if self.fail:
            raise RuntimeError("discord down")
        self.sent.append((channel_id, text))


async def test_feed():
    old = SEPT[:]
    summ = FakeSummarizer()
    feed, box = make_feed("f1", old, summ)
    send = Sender()
    feed.set_channel(77)
    ok = await feed.check(send)
    check("feed: first run records existing patches without posting", ok and send.sent == []
          and set(feed.state["seen"]) == {p["id"] for p in old} and feed.state["initialized"])

    box["patches"] = patchnotes.merge(LIVE, old)
    await feed.check(send)
    check("feed: new patch posted once to the channel", len(send.sent) == 1 and send.sent[0][0] == 77
          and "SUMMARY of 2026-10-06" in send.sent[0][1] and "patch-2026-10-06" in send.sent[0][1])
    check("feed: post has title and link", "🛠️ **Overwatch Retail Patch Notes – October 6, 2026**" in send.sent[0][1])
    await feed.check(send)
    check("feed: not posted again", len(send.sent) == 1 and summ.calls == ["patch-2026-10-06"])

    reloaded = PatchFeed(feed.path)
    reloaded.load()
    check("feed: state persisted (channel, seen, patches, summary cache)", reloaded.channel_id == 77
          and "patch-2026-10-06" in reloaded.state["seen"] and reloaded.patches[0]["id"] == "patch-2026-10-06"
          and reloaded.state["summaries"]["patch-2026-10-06"] == "SUMMARY of 2026-10-06")

    # 发送失败：不记账，下次重试
    feed2, box2 = make_feed("f2", old, FakeSummarizer())
    await feed2.check(Sender())
    feed2.set_channel(5)
    box2["patches"] = patchnotes.merge(LIVE, old)
    bad = Sender(fail=True)
    ok = await feed2.check(bad)
    check("feed: send failure asks for a retry and doesn't mark seen",
          not ok and "patch-2026-10-06" not in feed2.state["seen"])
    good = Sender()
    await feed2.check(good)
    check("feed: retried successfully", len(good.sent) == 1 and feed2.summarizer.calls == ["patch-2026-10-06"])

    # 没设置频道：记账不发
    feed3, box3 = make_feed("f3", old, FakeSummarizer())
    await feed3.check(Sender())
    box3["patches"] = patchnotes.merge(LIVE, old)
    s3 = Sender()
    await feed3.check(s3)
    check("feed: no channel -> marked seen silently, no summary spent",
          s3.sent == [] and "patch-2026-10-06" in feed3.state["seen"] and feed3.summarizer.calls == [])

    # 抓取失败：保留旧数据
    box3["fail"] = True
    ok = await feed3.check(Sender())
    check("feed: fetch failure keeps previous patches", not ok and feed3.patches[0]["id"] == "patch-2026-10-06")

    # 摘要失败：发简短版本
    feed4, box4 = make_feed("f4", old, FakeSummarizer(fail=True))
    await feed4.check(Sender())
    feed4.set_channel(9)
    box4["patches"] = patchnotes.merge(LIVE, old)
    s4 = Sender()
    await feed4.check(s4)
    check("feed: summary failure posts the hero list instead",
          len(s4.sent) == 1 and "Heroes changed:" in s4.sent[0][1] and "Roadhog" in s4.sent[0][1]
          and "patch-2026-10-06" not in feed4.state["summaries"])

    # 设置频道后立刻发最新的
    feed5, _ = make_feed("f5", patchnotes.merge(LIVE, old), FakeSummarizer())
    s5 = Sender()
    latest = await feed5.post_latest(42, s5)
    check("post_latest: fetches first, posts the newest patch", latest["id"] == "patch-2026-10-06"
          and len(s5.sent) == 1 and s5.sent[0][0] == 42)
    check("post_latest: marks it seen so the schedule doesn't repost", "patch-2026-10-06" in feed5.state["seen"])
    s6 = Sender()
    await feed5.check(s6)
    check("post_latest then check: no duplicate", s6.sent == [])

    feed6, box6 = make_feed("f6", [], None)
    box6["fail"] = True
    check("post_latest: nothing loaded and fetch fails -> None", await feed6.post_latest(1, Sender()) is None)


def test_text_helpers():
    long_text = "\n".join(f"line {i} " + "x" * 80 for i in range(60))
    chunks = split_message(long_text)
    check("split: chunks within limit, nothing lost",
          all(len(c) <= 1990 for c in chunks) and "\n".join(chunks) == long_text and len(chunks) > 1)
    check("split: one giant line is hard-cut", all(len(c) <= 1990 for c in split_message("y" * 5000)))
    check("render: link not embedded", "<https://" in render(LIVE[0], "S"))
    names = chinese_names(load_aliases())
    check("names: first Chinese alias", names.get("Cassidy") == "卡西迪" and names.get("Doctrine") == "血律"
          and "Domina" not in names)
    check("names: D.Va keeps her official name, not her real name", names["D.Va"] == "D.Va")


async def test_summarizer():
    calls = []

    async def create(**kw):
        calls.append(kw)
        return NS(stop_reason="end_turn", content=[NS(type="text", text="  总结  ")])

    client = NS(messages=NS(create=create))
    summ = PatchSummarizer(client, "m", "low", "Simplified Chinese", {"Cassidy": "卡西迪", "Mercy": "天使", "Pharah": "法老之鹰"})
    text = await summ.summarize(LIVE[0])
    system = calls[0]["system"]
    check("summary: returns stripped text", text == "总结")
    check("summary: language and only relevant hero names in prompt",
          "in Simplified Chinese" in system and "Cassidy = 卡西迪" in system and "Mercy = 天使" in system
          and "Pharah" not in system)
    check("summary: rules keep numbers honest, no invented names",
          "only use facts and numbers from the notes" in system and "never invent a translation" in system)
    check("summary: full notes sent", "Trash Compactor" in calls[0]["messages"][0]["content"])

    async def truncated(**kw):
        return NS(stop_reason="max_tokens", content=[])
    try:
        await PatchSummarizer(NS(messages=NS(create=truncated)), "m").summarize(LIVE[0])
        check("summary: truncated output raises", False)
    except RuntimeError:
        check("summary: truncated output raises", True)


# ---------------- 英雄问答 ----------------
def make_store():
    store = HeroStore("unused.json", load_aliases())
    for name in ("Cassidy", "Lúcio", "Tracer", "Roadhog"):
        store.heroes[name] = {"name": name, "role": "x", "patches": [], "source_url": f"wiki/{name}"}
    store.fetched_at = "2026-10-07T13:00:42Z"
    return store


def test_qa_tool():
    check("qa: tool registered", any(t["name"] == "get_official_patch_notes" for t in TOOLS))
    check("qa: prompt prefers official notes over a lagging Wiki",
          "get_official_patch_notes" in SYSTEM and "lag" in SYSTEM and "Blizzard patch notes" in SYSTEM)
    store = make_store()
    feed = PatchFeed(os.path.join(TMP, "qa.json"))
    feed.state["patches"] = patchnotes.merge(LIVE, SEPT)
    feed.state["checked_at"] = "2026-10-08T01:00:00+00:00"
    qa = HeroQA(None, "m", store, patches=feed)

    r = qa.run_tool("get_official_patch_notes", {"hero": "卡西迪"})
    check("qa: Chinese name resolved, Cassidy's change found",
          r["hero"] == "Cassidy" and r["patches"][0]["date"] == "2026-10-06"
          and any("Lifeline" in line for line in r["patches"][0]["changes"]))
    check("qa: reports newest official patch", r["newest_official_patch"] == "2026-10-06")
    r = qa.run_tool("get_official_patch_notes", {"hero": "lucio"})
    check("qa: accent-insensitive match (Lúcio)", r["hero"] == "Lúcio" and r["patches"]
          and any("Sound Barrier" in line for line in r["patches"][0]["changes"]))
    r = qa.run_tool("get_official_patch_notes", {"hero": "Tracer", "limit": 1})
    check("qa: limit respected", r["patches_searched"] == ["2026-10-06"])
    r = qa.run_tool("get_official_patch_notes", {})
    check("qa: no hero -> latest patch notes and heroes changed",
          r["date"] == "2026-10-06" and "Roadhog" in r["heroes_changed"] and "Trash Compactor" in r["notes"])
    r = qa.run_tool("get_official_patch_notes", {"hero": "zzzz"})
    check("qa: unknown hero passes through resolve error", r.get("error") == "unknown_hero")
    feed.state["patches"] = [SEPT[0]]
    r = qa.run_tool("get_official_patch_notes", {"hero": "Cassidy"})
    check("qa: hero not in recent patches says so", r["patches"] == [] and "not mentioned" in r["note"])
    empty = HeroQA(None, "m", store, patches=PatchFeed(os.path.join(TMP, "empty.json")))
    check("qa: nothing loaded -> explicit error", empty.run_tool("get_official_patch_notes", {})["error"]
          == "no_official_patch_data")
    check("qa: no feed configured -> explicit error",
          HeroQA(None, "m", store).run_tool("get_official_patch_notes", {"hero": "Cassidy"})["error"]
          == "no_official_patch_data")


# ---------------- 指令 ----------------
BOT_MEMBER = NS(id=2)


class FakeChannel:
    def __init__(self, channel_id, manage=True, bot_can_send=True):
        self.id, self.manage, self.bot_can_send = channel_id, manage, bot_can_send

    def permissions_for(self, member):
        if member is BOT_MEMBER:
            return NS(send_messages=self.bot_can_send)
        return NS(manage_channels=self.manage)

    def typing(self):
        return Typing()


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeMessage:
    def __init__(self, channel):
        self.author, self.guild, self.channel, self.replies = NS(id=1), NS(id=9, me=BOT_MEMBER), channel, []

    async def reply(self, content):
        self.replies.append(content)


def make_bot(feed):
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    b.patch_feed = feed
    b.sent = []

    async def post(channel_id, text):
        b.sent.append((channel_id, text))
    b.post_patches = post
    return b


async def test_command():
    check("cmd: patches is an argument command", bot.parse_command("patches here") == ("patches", "here"))
    check("cmd: help mentions patches", "patches here" in bot.HELP_TEXT)

    feed, _ = make_feed("cmd", patchnotes.merge(LIVE, SEPT), FakeSummarizer())
    b = make_bot(feed)
    msg = FakeMessage(FakeChannel(300, manage=False))
    await b._patches(msg, "here")
    check("cmd: here needs Manage Channels", "Manage Channels" in msg.replies[0] and feed.channel_id is None)

    msg = FakeMessage(FakeChannel(300, bot_can_send=False))
    await b._patches(msg, "here")
    check("cmd: here checks the bot can send", "can't send" in msg.replies[0] and feed.channel_id is None)

    msg = FakeMessage(FakeChannel(300))
    await b._patches(msg, "here")
    check("cmd: here sets channel and posts the latest patch right away",
          feed.channel_id == 300 and len(b.sent) == 1 and b.sent[0][0] == 300
          and "October 6, 2026" in b.sent[0][1] and "latest one" in msg.replies[0])

    msg = FakeMessage(FakeChannel(300))
    await b._patches(msg, "here")
    check("cmd: here again in the same channel doesn't repost", len(b.sent) == 1 and "already" in msg.replies[0])

    msg = FakeMessage(FakeChannel(300))
    await b._patches(msg, "status")
    check("cmd: status shows channel and latest patch",
          "<#300>" in msg.replies[0] and "October 6, 2026" in msg.replies[0])

    msg = FakeMessage(FakeChannel(300))
    await b._patches(msg, "off")
    check("cmd: off clears the channel", feed.channel_id is None and "turned off" in msg.replies[0])

    msg = FakeMessage(FakeChannel(300))
    await b._patches(msg, "banana")
    check("cmd: unknown action shows usage", "Usage" in msg.replies[0])

    b2 = make_bot(None)
    msg = FakeMessage(FakeChannel(300))
    await b2._patches(msg, "here")
    check("cmd: feed disabled explained", "not enabled" in msg.replies[0])

    broken, box = make_feed("cmd2", [], None)
    box["fail"] = True
    b3 = make_bot(broken)
    msg = FakeMessage(FakeChannel(301))
    await b3._patches(msg, "here")
    check("cmd: here still sets the channel when notes can't load", broken.channel_id == 301
          and "couldn't load" in msg.replies[-1])


async def run_async():
    await test_fetch()
    await test_feed()
    await test_summarizer()
    await test_command()


if __name__ == "__main__":
    test_parse()
    test_merge_and_months()
    test_text_helpers()
    test_qa_tool()
    asyncio.run(run_async())
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
