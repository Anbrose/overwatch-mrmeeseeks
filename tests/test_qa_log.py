"""英雄问答记录（qa_log.jsonl）和私信汇报。"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
import bot  # noqa: E402
import qa_log  # noqa: E402
from hero_qa import Cooldown  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def test_record():
    with tempfile.TemporaryDirectory() as d:
        log = qa_log.QaLog(Path(d) / "sub" / "qa_log.jsonl")
        now = datetime(2026, 10, 30, 1, 2, 3, tzinfo=timezone.utc)
        log.record(user_id=7, user_name="小红", guild_id=None, channel_id=99, message_id=5, question="卡西迪血量",
                   outcome=qa_log.ANSWERED, answer="225", now=now)
        log.record(user_id=8, user_name="Ana", guild_id=1, channel_id=2, message_id=6, question="q",
                   outcome=qa_log.ERROR, answer="Sorry", now=now)
        lines = [json.loads(l) for l in log.path.read_text(encoding="utf-8").splitlines()]
        check("每次一行 JSON，中文原样", len(lines) == 2 and lines[0]["question"] == "卡西迪血量"
              and "卡西迪" in log.path.read_text(encoding="utf-8"))
        check("私信记 where=dm、频道记 guild", lines[0]["where"] == "dm" and lines[0]["guild_id"] is None
              and lines[1]["where"] == "guild" and lines[1]["guild_id"] == 1)
        check("字段齐全", lines[0] == {"time": "2026-10-30T01:02:03+00:00", "where": "dm", "guild_id": None,
                                     "channel_id": 99, "message_id": 5, "user_id": 7, "user_name": "小红",
                                     "question": "卡西迪血量", "outcome": "answered", "answer": "225"})
        old = qa_log.MAX_BYTES
        qa_log.MAX_BYTES = 10
        try:
            log.record(user_id=9, user_name="x", guild_id=None, channel_id=1, message_id=1, question="new",
                       outcome=qa_log.ANSWERED, answer="a")
        finally:
            qa_log.MAX_BYTES = old
        rotated = log.path.with_name("qa_log.jsonl.1")
        check("超过上限改名成 .1，新文件从头写", rotated.exists() and len(rotated.read_text().splitlines()) == 2
              and len(log.path.read_text().splitlines()) == 1)
        bad = qa_log.QaLog(Path(d) / "sub")      # 是目录，写不进去
        bad.record(user_id=1, user_name="x", guild_id=None, channel_id=1, message_id=1, question="q",
                   outcome=qa_log.ANSWERED, answer="a")
        check("写不进去不抛异常", True)


def test_report_text():
    t = qa_log.qa_report(7, "小红", "卡西迪\n血量", qa_log.ANSWERED, "225")
    check("问答汇报：谁、问题引用、回答", t == "❓ **小红** (<@7>) asked in DM:\n> 卡西迪\n> 血量\n**Answer:**\n225")
    t = qa_log.qa_report(7, "小红", "q", qa_log.COOLDOWN, "Please wait")
    check("没答的写原因", "**Rate-limited:**\nPlease wait" in t)
    t = qa_log.qa_report(7, "小红", "q" * 1500, qa_log.ANSWERED, "a" * 1990)
    check("超长截断到 2000 以内并注明", len(t) <= 2000 and t.endswith("full text in qa_log.jsonl)")
          and "**Answer:**" in t)
    t = qa_log.dm_report(7, "小红", "一起看   湖南卫视\n")
    check("其他指令一行", t == "💬 **小红** (<@7>) in DM: `一起看 湖南卫视`")
    check("反引号不破坏格式", "`" not in qa_log.dm_report(7, "x", "a`b")[len("💬 **x** (<@7>) in DM: `"):-1])


class FakeChannel:
    def __init__(self, cid=99, fail=False):
        self.id, self.sent, self.fail = cid, [], fail

    def typing(self):
        class _Ctx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *a):
                return False
        return _Ctx()

    async def send(self, content, allowed_mentions=None):
        if self.fail:
            raise RuntimeError("no perms")
        self.sent.append((content, allowed_mentions))


class FakeQA:
    async def answer(self, q):
        return "225 HP"


def make_bot(d, report=True, fail=False):
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    b.wish_channel_id, b.wish_desk, b.wish_plans, b.announce_desk = None, None, None, None
    b._connection = NS(user=NS(id=1))
    b.hero_qa, b.store, b.ask_cooldown = FakeQA(), NS(ready=True), Cooldown(5)
    b.qa_log = qa_log.QaLog(Path(d) / "qa_log.jsonl")
    b.report_channel_id = 500 if report else None
    b.report = FakeChannel(500, fail)

    async def channel(cid):
        assert cid == 500
        return b.report
    b._channel = channel

    async def player(message, arg):
        pass
    b._player = player
    return b


def message(content, dm=True, mention=False):
    replies = []

    async def reply(content=None, **kw):
        replies.append(content)

    return NS(id=55, author=NS(id=7, bot=False, display_name="小红"), mentions=[NS(id=1)] if mention else [],
              content=content, channel=FakeChannel(), guild=None if dm else NS(id=3), reply=reply), replies


def entries(b):
    p = b.qa_log.path
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_bot():
    with tempfile.TemporaryDirectory() as d:
        b = make_bot(d)
        msg, replies = message("卡西迪血量")
        asyncio.run(b.on_message(msg))
        e = entries(b)
        check("私信问答：照常回复", replies == ["225 HP"])
        check("私信问答：记进日志", len(e) == 1 and e[0]["where"] == "dm" and e[0]["question"] == "卡西迪血量"
              and e[0]["answer"] == "225 HP" and e[0]["message_id"] == 55 and e[0]["user_name"] == "小红")
        content, mentions = b.report.sent[0]
        check("私信问答：汇报到频道且不 @ 人", len(b.report.sent) == 1 and "卡西迪血量" in content
              and "225 HP" in content and mentions.users is False)

    with tempfile.TemporaryDirectory() as d:
        b = make_bot(d)
        msg, replies = message("<@1> 卡西迪血量", dm=False, mention=True)
        asyncio.run(b.on_message(msg))
        e = entries(b)
        check("频道问答：只记录不汇报", replies == ["225 HP"] and len(e) == 1 and e[0]["where"] == "guild"
              and e[0]["guild_id"] == 3 and b.report.sent == [])

    with tempfile.TemporaryDirectory() as d:
        b = make_bot(d)
        msg, _ = message("卡西迪血量")
        asyncio.run(b.on_message(msg))
        msg, replies = message("又问")
        asyncio.run(b.on_message(msg))
        check("冷却也记录、也汇报", entries(b)[1]["outcome"] == "cooldown" and "Rate-limited" in b.report.sent[1][0])

    with tempfile.TemporaryDirectory() as d:
        b = make_bot(d)
        for text in ("player Name#1234", "esports here", ""):
            msg, _ = message(text)
            asyncio.run(b.on_message(msg))
        sent = [c for c, _ in b.report.sent]
        check("私信里其他指令汇报一行（含被拒的、空的）", sent == [qa_log.dm_report(7, "小红", "player Name#1234"),
                                                    qa_log.dm_report(7, "小红", "esports here"),
                                                    qa_log.dm_report(7, "小红", "")])
        check("其他指令不进问答日志", entries(b) == [])

    with tempfile.TemporaryDirectory() as d:
        b = make_bot(d, report=False)
        msg, replies = message("卡西迪血量")
        asyncio.run(b.on_message(msg))
        check("没设置汇报频道：照常回复、仍记录", replies == ["225 HP"] and len(entries(b)) == 1
              and b.report.sent == [])

    with tempfile.TemporaryDirectory() as d:
        b = make_bot(d, fail=True)
        msg, replies = message("卡西迪血量")
        asyncio.run(b.on_message(msg))
        check("汇报发不出去不影响回复", replies == ["225 HP"] and len(entries(b)) == 1)


def test_help():
    check("帮助说明管理员能看私信", "admins can read DMs" in bot.HELP_TEXT and len(bot.HELP_TEXT) < 2000)


if __name__ == "__main__":
    test_record()
    test_report_text()
    test_bot()
    test_help()
    passed = sum(ok for _, ok in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
