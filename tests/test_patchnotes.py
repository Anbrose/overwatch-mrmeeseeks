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
import hero_emojis  # noqa: E402
from patch_feed import (PatchFeed, PatchSummarizer, chinese_names, entry_kinds, fill_names,  # noqa: E402
                        name_list, normalize_sections, render, split_message)

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
KO = patchnotes.parse_page(fixture("live-ko.html"))
CN = patchnotes.parse_page(fixture("cn-2026-10.html"))


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


def test_locales():
    check("ko: same anchor id as English", [p["id"] for p in KO] == ["patch-2026-10-06"])
    check("cn: no anchor, date taken from the Chinese title (Beijing time, a day later)",
          [(p["id"], p["date"]) for p in CN][:1] == [("patch-2026-10-07", "2026-10-07")])
    en, ko, cn = LIVE[0], KO[0], CN[0]
    check("blocks: same hero-entry count in all three", len(en["blocks"]) == len(ko["blocks"]) == len(cn["blocks"]) == 46)
    hog = en["blocks"][0]
    check("blocks: hero icon and perks + abilities in page order",
          hog["hero"] == "Roadhog" and hog["icon"] and [e["name"] for e in hog["entries"]][:2] ==
          ["Scrap Hook – Minor Perk", "Shrapnel Launcher – Minor Perk"]
          and hog["entries"][5] == {"name": "Scrap Gun – Primary Fire", "icon": hog["entries"][5]["icon"], "kind": "ability"}
          and hog["entries"][0]["kind"] == "perk"
          and hog["entries"][5]["icon"] and hog["entries"][0]["icon"] is None)
    check("blocks: Korean icons identical to English", [b["icon"] for b in en["blocks"]] == [b["icon"] for b in ko["blocks"]])
    check("perk split: three languages", [patchnotes.split_perk(n) for n in
          ("Scrap Hook – Minor Perk", "废料链钩——次级威能", "고철 갈고리 - 보조 특전", "Chain Hook", "Pulled Pork – Major Perk")]
          == [("Scrap Hook", "Minor Perk"), ("废料链钩", "次级威能"), ("고철 갈고리", "보조 특전"), ("Chain Hook", None),
              ("Pulled Pork", "Major Perk")])
    check("title dates", [patchnotes._title_date(t) for t in
          ("Overwatch Retail Patch Notes – October 6, 2026", "《守望先锋》补丁说明——2026年10月7日", "패치 노트 - 2026년 10월 6일", "x")]
          == ["2026-10-06", "2026-10-07", "2026-10-06", None])

    check("match: Korean by id", patchnotes.find_match(KO, en) is KO[0])
    check("match: Chinese by date +1 day", patchnotes.find_match(CN, en) is CN[0])
    check("match: nothing within a day", patchnotes.find_match(CN, SEPT[0]) is None)

    t = patchnotes.translations(en, ko, cn)
    check("table: hero names", t["heroes"]["Roadhog"] == {"zh": "路霸", "ko": "로드호그"}
          and t["heroes"]["Domina"] == {"zh": "金驭", "ko": "도미나"} and t["heroes"]["Jetpack Cat"]["zh"] == "飞天猫")
    check("table: ability names", t["names"]["Roadhog"]["Chain Hook"] == {"zh": "链钩", "ko": "사슬 갈고리"}
          and t["names"]["Roadhog"]["Scrap Gun – Primary Fire"] == {"zh": "爆裂枪——主要攻击模式", "ko": "고철총 - 기본 발사"}
          and t["names"]["Cassidy"]["Deadeye"] == {"zh": "神射手", "ko": "황야의 무법자"})
    check("table: perk names without suffix", t["names"]["Roadhog"]["Scrap Hook"] == {"zh": "废料链钩", "ko": "고철 갈고리"})
    check("table: complete for this patch",
          all(v["zh"] and v["ko"] for d in t["names"].values() for v in d.values())
          and all(v["zh"] and v["ko"] for v in t["heroes"].values()))

    only_en = patchnotes.translations(en)
    check("table: no other pages -> every slot empty but names listed",
          only_en["heroes"]["Roadhog"] == {"zh": "", "ko": ""} and "Chain Hook" in only_en["names"]["Roadhog"])
    short_cn = {**cn, "blocks": cn["blocks"][1:]}
    t2 = patchnotes.translations(en, ko, short_cn)
    check("table: Chinese hero count differs -> Chinese left empty, Korean kept",
          t2["heroes"]["Roadhog"] == {"zh": "", "ko": "로드호그"} and t2["names"]["Roadhog"]["Chain Hook"]["zh"] == "")
    cn_blocks = [dict(b) for b in cn["blocks"]]
    cn_blocks[0] = {**cn_blocks[0], "entries": cn_blocks[0]["entries"][1:]}
    t3 = patchnotes.translations(en, None, {**cn, "blocks": cn_blocks})
    check("table: entry count differs in one hero -> that hero's abilities empty, hero name kept",
          t3["heroes"]["Roadhog"]["zh"] == "路霸" and t3["names"]["Roadhog"]["Chain Hook"]["zh"] == ""
          and t3["names"]["Sombra"]["Hotfix"]["zh"] == "在线修复")
    swapped = [dict(b) for b in cn["blocks"]]
    e = list(swapped[0]["entries"])
    e[4], e[5] = e[5], e[4]      # 一个威能和一个技能换了位置
    swapped[0] = {**swapped[0], "entries": e}
    t4 = patchnotes.translations(en, None, {**cn, "blocks": swapped})
    check("table: perk/ability misalignment not trusted",
          t4["names"]["Roadhog"]["Toxic Exhaust"]["zh"] == "" and t4["names"]["Roadhog"]["Scrap Gun – Primary Fire"]["zh"] == "")


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
        pages[patchnotes.KO_URL + "live/2026/10/"] = fixture("live-ko.html")
        pages[patchnotes.CN_URL + "live/2026/10/"] = fixture("cn-2026-10.html")
        t = await patchnotes.fetch_translations(LIVE[0])
        check("fetch translations: Korean and Chinese month pages", t["heroes"]["Roadhog"] == {"zh": "路霸", "ko": "로드호그"})

        async def flaky(session, url):
            if "ow.blizzard.cn" in url:
                raise patchnotes.aiohttp.ClientError("down")
            return pages[url]
        patchnotes._page = flaky
        t = await patchnotes.fetch_translations(LIVE[0])
        check("fetch translations: China site down -> Chinese empty, Korean kept",
              t["heroes"]["Roadhog"] == {"zh": "", "ko": "로드호그"})
        patchnotes._page = fake_page

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
        self.calls, self.tables, self.icons, self.fail = [], [], [], fail

    async def summarize(self, patch):
        self.calls.append(patch["id"])
        if self.fail:
            raise RuntimeError("api down")
        return f"SUMMARY of {patch['date']}"

    def finish(self, patch, raw, table, icons=None):
        self.tables.append(table)
        self.icons.append(icons)
        return raw


def make_feed(name, patches_list, summarizer=None):
    box = {"patches": patches_list, "fail": False, "translate_fail": False}

    async def fetch():
        if box["fail"]:
            raise RuntimeError("network")
        return box["patches"]

    async def translate(patch):
        if box["translate_fail"]:
            raise RuntimeError("names down")
        return patchnotes.translations(patch, KO[0], CN[0]) if patch["id"] == "patch-2026-10-06" else \
            patchnotes.translations(patch)

    return PatchFeed(os.path.join(TMP, f"{name}.json"), fetch=fetch, summarizer=summarizer,
                     translate=translate), box


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
    check("feed: summarizer got the trilingual name table", summ.tables[0]["heroes"]["Roadhog"]["ko"] == "로드호그")

    reloaded = PatchFeed(feed.path)
    reloaded.load()
    check("feed: state persisted (channel, seen, patches, summary cache)", reloaded.channel_id == 77
          and "patch-2026-10-06" in reloaded.state["seen"] and reloaded.patches[0]["id"] == "patch-2026-10-06"
          and reloaded.state["summaries"]["v3:patch-2026-10-06"]["raw"] == "SUMMARY of 2026-10-06"
          and reloaded.state["summaries"]["v3:patch-2026-10-06"]["table"]["heroes"]["Roadhog"]["zh"] == "路霸")

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
          and "v3:patch-2026-10-06" not in feed4.state["summaries"])

    # 名称对照抓取失败：照样写摘要，名称留空
    feed7, box7 = make_feed("f7", patchnotes.merge(LIVE, old), FakeSummarizer())
    box7["translate_fail"] = True
    s7 = Sender()
    await feed7.post_latest(3, s7)
    check("feed: name lookup failure -> summary still written with empty slots",
          "SUMMARY" in s7.sent[0][1] and feed7.summarizer.tables[0]["heroes"]["Roadhog"] == {"zh": "", "ko": ""})

    # 旧格式（中文摘要）的缓存不再使用
    feed8, _ = make_feed("f8", patchnotes.merge(LIVE, old), FakeSummarizer())
    feed8.state["summaries"] = {"patch-2026-10-06": "旧的中文摘要", "v2:patch-2026-10-06": "旧的 v2 摘要"}
    s8 = Sender()
    await feed8.post_latest(3, s8)
    check("feed: old-format cached summary ignored and dropped",
          "旧的" not in s8.sent[0][1] and feed8.summarizer.calls == ["patch-2026-10-06"])
    await feed8.refresh()
    check("feed: refresh prunes old-format keys", list(feed8.state["summaries"]) == ["v3:patch-2026-10-06"])

    # 头像：每次发送时取，缓存的摘要也能用上后来才有的头像
    icon_calls = []

    async def icons(patch):
        icon_calls.append(patch["id"])
        return {"Roadhog": "<:ow_roadhog:1>"}
    feed9, _ = make_feed("f9", patchnotes.merge(LIVE, old), FakeSummarizer())
    feed9.icons = icons
    await feed9.post_latest(3, Sender())
    await feed9.post_latest(4, Sender())
    check("feed: icons looked up on every post, summary generated once",
          icon_calls == ["patch-2026-10-06"] * 2 and feed9.summarizer.calls == ["patch-2026-10-06"]
          and feed9.summarizer.icons[-1] == {"Roadhog": "<:ow_roadhog:1>"})

    async def broken_icons(patch):
        raise RuntimeError("emoji api down")
    feed10, _ = make_feed("f10", patchnotes.merge(LIVE, old), FakeSummarizer())
    feed10.icons = broken_icons
    s10 = Sender()
    await feed10.post_latest(3, s10)
    check("feed: icon failure still posts, without icons", "SUMMARY" in s10.sent[0][1] and feed10.summarizer.icons == [{}])

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
    heroes = "\n\n".join(f"- **Hero {i}**\n" + "\n".join(f"  - change {j} " + "z" * 60 for j in range(8))
                         for i in range(12))
    parts = split_message(heroes)
    check("split: breaks between heroes, never inside one",
          len(parts) > 1 and all(p.startswith("- **Hero") for p in parts) and "\n\n".join(parts) == heroes)
    check("render: link not embedded", "<https://" in render(LIVE[0], "S"))
    names = chinese_names(load_aliases())
    check("names: first Chinese alias", names.get("Cassidy") == "卡西迪" and names.get("Doctrine") == "血律"
          and "Domina" not in names)
    check("names: D.Va keeps her official name, not her real name", names["D.Va"] == "D.Va")


async def test_summarizer():
    calls = []
    reply = ("- Season 5 patch.\n\n**Hero changes**\n- 🔄 **[[Roadhog]]**\n  - **Basic Info**: None\n"
             "  - **Abilities**\n    - [[Roadhog|Chain Hook]]: cooldown 7s → 6s\n"
             "  - **Perks**\n    - [[Roadhog|Scrap Hook]] (Minor): 2 → 4\n\n- ⬆️ **[[Domina]]**\n"
             "  - **abilities**\n    - [[Crystal Charge]] fallback\n    - [[Domina|Made Up Ability]]: x")

    async def create(**kw):
        calls.append(kw)
        return NS(stop_reason="end_turn", content=[NS(type="text", text=f"  {reply}  ")])

    client = NS(messages=NS(create=create))
    summ = PatchSummarizer(client, "m", "low", {"Roadhog": "路霸(aliases)", "Pharah": "法老之鹰"})
    table = patchnotes.translations(LIVE[0], KO[0], CN[0])
    raw = await summ.summarize(LIVE[0])
    system = calls[0]["system"]
    check("summary: returns the model's raw text with tags", raw == reply)
    check("summary: bullets, English, three sections per hero",
          "bullet points only" in system and "in English" in system and "**Basic Info**" in system
          and "**Abilities**" in system and "**Perks**: None" in system)
    check("summary: model sees which names are abilities and which are perks (English only)",
          "Roadhog — Abilities: Scrap Gun – Primary Fire | Scrap Gun – Secondary Fire | Trash Compactor" in system
          and "Perks: Scrap Hook (Minor) | Shrapnel Launcher (Minor)" in system and "路霸" not in system
          and "로드호그" not in system)
    check("summary: only heroes in this patch", "Pharah" not in system)
    check("summary: full notes sent", "Trash Compactor" in calls[0]["messages"][0]["content"])

    text = summ.finish(LIVE[0], raw, table, {"Roadhog": "<:ow_roadhog:1>"})
    check("finish: icon before the trilingual hero name",
          "**<:ow_roadhog:1> Roadhog/路霸/로드호그**" in text and "**Domina/金驭/도미나**" in text)
    check("finish: tags replaced with official trilingual names",
          "Chain Hook/链钩/사슬 갈고리: cooldown" in text and "Scrap Hook/废料链钩/고철 갈고리 (Minor)" in text)
    check("finish: official Chinese name preferred over aliases", "路霸(aliases)" not in text)
    check("finish: [[Name]] without hero resolved when unique", "Crystal Charge/爆能水晶/수정 발사 fallback" in text)
    check("finish: unknown name kept in English with empty slots", "Made Up Ability//: x" in text)
    check("finish: no tags left", "[[" not in text and "]]" not in text)
    dom = text.split("**Domina/金驭/도미나**")[1].split("\n")
    check("finish: missing sections filled with None, case and order fixed",
          dom[1:5] == ["  - **Basic Info**: None", "  - **Abilities**",
                       "    - Crystal Charge/爆能水晶/수정 발사 fallback", "    - Made Up Ability//: x"]
          and dom[5] == "  - **Perks**: None")

    no_cn = patchnotes.translations(LIVE[0], KO[0], None)
    filled, unknown = fill_names("[[Roadhog]] [[Roadhog|Chain Hook]] [[Domina]] [[Pharah]]", no_cn, {"Roadhog": "路霸"})
    check("fill: China page missing -> hero falls back to aliases, abilities keep an empty Chinese slot",
          filled.startswith("Roadhog/路霸/로드호그 Chain Hook//사슬 갈고리 Domina//도미나"))
    check("fill: hero not in the patch reported, still formatted", "Pharah//" in filled and unknown == ["[[Pharah]]"])
    check("name list: hit-volume-only hero listed bare", "\nMercy\n" in "\n" + name_list(LIVE[0]) + "\n")

    async def truncated(**kw):
        return NS(stop_reason="max_tokens", content=[])
    try:
        await PatchSummarizer(NS(messages=NS(create=truncated)), "m").summarize(LIVE[0])
        check("summary: truncated output raises", False)
    except RuntimeError:
        check("summary: truncated output raises", True)


def test_sections_by_kind():
    kinds = entry_kinds(LIVE[0])
    check("kinds: perks and abilities from the official structure",
          kinds["Ana"].get("localanesthetic") == "Perks" and kinds["Roadhog"]["chainhook"] == "Abilities"
          and kinds["Domina"]["reconstruction"] == "Abilities")
    raw = ("**Hero changes**\n- 🔄 **[[Ana]]**\n  - **Basic Info**: None\n"
           "  - **Abilities**: [[Ana|Local Anesthetic]] (Minor, new): Sleep Dart explodes\n"
           "  - **Perks**: [[Ana|Groggy]] (Minor, removed)\n\n- ⬆️ **[[D.Va]]**\n"
           "  - **Abilities**: [[D.Va|Micro Missiles]]: fire rate 11 → 14\n  - **Perks**: none.")
    out = normalize_sections(raw, kinds)
    ana = out.split("**[[Ana]]**\n")[1].split("\n\n")[0].split("\n")
    check("sections: misplaced perk moved to Perks, inline items moved down a level",
          ana == ["  - **Basic Info**: None", "  - **Abilities**: None", "  - **Perks**",
                  "    - [[Ana|Local Anesthetic]] (Minor, new): Sleep Dart explodes",
                  "    - [[Ana|Groggy]] (Minor, removed)"])
    dva = out.split("**[[D.Va]]**\n")[1].split("\n")
    moved = normalize_sections("**Hero changes**\n- 🔄 **[[Ana]]**\n  - **Abilities**\n"
                               "    - [[Ana|Local Anesthetic]]: new\n      - detail line\n    - [[Ana|Sleep Dart]]: x", kinds)
    check("sections: deeper detail follows its moved parent",
          moved.split("\n")[2:] == ["  - **Basic Info**: None", "  - **Abilities**", "    - [[Ana|Sleep Dart]]: x",
                                     "  - **Perks**", "    - [[Ana|Local Anesthetic]]: new", "      - detail line"])
    check("sections: single change nested, 'none.' treated as None",
          dva == ["  - **Basic Info**: None", "  - **Abilities**", "    - [[D.Va|Micro Missiles]]: fire rate 11 → 14",
                  "  - **Perks**: None"])
    table = patchnotes.translations(LIVE[0], KO[0], CN[0])
    filled, unknown = fill_names("[[Domina|Reconstruction]] [[Domina|reconstruction (passive)]]", table)
    check("fill: name matched without its '(Passive)' suffix",
          filled == "Reconstruction/护盾重构（被动）/재조립(지속 능력) reconstruction (passive)/护盾重构（被动）/재조립(지속 능력)"
          and unknown == [])


def test_sections():
    raw = ("- intro\n\n**New content**\n- **[[Doctrine]]** joins a mode\n\n**Hero changes**\n"
           "- ⬇️ **[[Doctrine]]**\n  - **Perks**\n    - [[Doctrine|Transfusion]] (Major): -50%\n"
           "  - **Basic Info**\n    - Ultimate cost +8%\n\n- 🔄 **[[Cassidy]]**\n    - stray line\n\n"
           "- **Hit volumes**: adjusted\n\n**Other**\n- **[[Ana]]** something")
    out = normalize_sections(raw)
    doc = out.split("**[[Doctrine]]**\n")[1].split("\n\n")[0].split("\n")
    check("sections: reordered Basic Info / Abilities / Perks with None filled",
          doc == ["  - **Basic Info**", "    - Ultimate cost +8%", "  - **Abilities**: None", "  - **Perks**",
                  "    - [[Doctrine|Transfusion]] (Major): -50%"])
    cas = out.split("**[[Cassidy]]**\n")[1].split("\n\n")[0].split("\n")
    check("sections: untagged stray line goes under Basic Info, all three present",
          cas == ["  - **Basic Info**", "    - stray line", "  - **Abilities**: None", "  - **Perks**: None"])
    check("sections: only inside Hero changes, only for hero tags",
          "- **[[Doctrine]]** joins a mode\n\n**Hero changes**" in out and "- **Hit volumes**: adjusted\n\n**Other**" in out
          and out.endswith("- **[[Ana]]** something"))


# ---------------- 头像表情 ----------------
class FakeEmojiClient:
    def __init__(self, existing=(), fail_create=False):
        self.existing = [NS(name=n, markup=f"<:{n}:9{i}>") for i, n in enumerate(existing)]
        self.created, self.fail_create, self.listed = [], fail_create, 0

    async def wait_until_ready(self):
        pass

    async def fetch_application_emojis(self):
        self.listed += 1
        return [EmojiObj(e.name, e.markup) for e in self.existing]

    async def create_application_emoji(self, name, image):
        if self.fail_create:
            raise RuntimeError("no slots")
        self.created.append((name, image[:8]))
        return EmojiObj(name, f"<:{name}:{100 + len(self.created)}>")


class EmojiObj:
    def __init__(self, name, markup):
        self.name, self.markup = name, markup

    def __str__(self):
        return self.markup


def png_bytes(size=256):
    import cv2
    import numpy as np
    ok, buf = cv2.imencode(".png", np.full((size, size, 4), 200, np.uint8))
    return buf.tobytes()


async def test_emojis():
    check("emoji name: normalized, prefixed", [hero_emojis.emoji_name(h) for h in ("Lúcio", "Soldier: 76", "D.Va", "Jetpack Cat")]
          == ["ow_lucio", "ow_soldier76", "ow_dva", "ow_jetpackcat"])
    small = hero_emojis.to_png(png_bytes())
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(small, np.uint8), cv2.IMREAD_UNCHANGED)
    check("to_png: square 128px with alpha, PNG", img.shape == (128, 128, 4) and small[:4] == b"\x89PNG")

    downloads = []

    async def download(url):
        downloads.append(url)
        return png_bytes()

    client = FakeEmojiClient(existing=["ow_roadhog"])
    emojis = hero_emojis.HeroEmojis(client, download=download)
    icons = await emojis.for_patch(LIVE[0])
    check("emojis: existing reused, missing created from the official icon",
          icons["Roadhog"] == "<:ow_roadhog:90>" and icons["Cassidy"].startswith("<:ow_cassidy:")
          and len(icons) == 40 and len(client.created) == 39 and len(downloads) == 39
          and all(u.startswith("https://") for u in downloads))
    again = await emojis.for_patch(LIVE[0])
    check("emojis: second patch reuses everything, lists emojis once",
          again == icons and len(client.created) == 39 and client.listed == 1)

    failing_client = FakeEmojiClient(fail_create=True)
    attempts = []
    real_create = failing_client.create_application_emoji

    async def counting_create(name, image):
        attempts.append(name)
        return await real_create(name=name, image=image)
    failing_client.create_application_emoji = counting_create
    failing = hero_emojis.HeroEmojis(failing_client, download=download)
    check("emojis: upload failure -> no icon, no crash", await failing.for_patch(LIVE[0]) == {})
    check("emojis: gives up after 3 failures in a row", len(attempts) == 3)

    async def bad_download(url):
        return b"not an image"
    broken = hero_emojis.HeroEmojis(FakeEmojiClient(existing=["ow_roadhog"]), download=bad_download)
    check("emojis: bad image -> only already-existing icons", await broken.for_patch(LIVE[0]) == {"Roadhog": "<:ow_roadhog:90>"})


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
    await test_emojis()
    await test_command()


if __name__ == "__main__":
    test_parse()
    test_locales()
    test_merge_and_months()
    test_text_helpers()
    test_sections()
    test_sections_by_kind()
    test_qa_tool()
    asyncio.run(run_async())
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
