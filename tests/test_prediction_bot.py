"""竞猜的 Discord 接入：predictions 指令（榜单、自己的排名、reset 权限）、preview 的竞猜样例、
带按钮的发送与编辑、启动时注册按钮。用假的 Discord 对象，不连 Discord。

运行：python tests/test_prediction_bot.py
"""
import asyncio
import os
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import bot  # noqa: E402
import prediction_ui as ui  # noqa: E402
import predictions as pr  # noqa: E402
from esports_feed import EsportsFeed  # noqa: E402
from prediction_feed import PredictionFeed  # noqa: E402
from logos import LogoCache  # noqa: E402
from rosters import RosterCache  # noqa: E402

results = []
NS = types.SimpleNamespace
BOT_MEMBER = NS(id=2)


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


class FakeChannel:
    def __init__(self, channel_id=555, manage=True):
        self.id, self.manage, self.sent, self.edited, self.files = channel_id, manage, [], [], []

    def permissions_for(self, member):
        if member is BOT_MEMBER:
            return NS(manage_channels=False, manage_guild=False, send_messages=True)
        return NS(manage_channels=self.manage, manage_guild=self.manage, send_messages=True)

    async def send(self, text, allowed_mentions=None, view=None, embed=None, file=None):
        self.sent.append((text, allowed_mentions, view))
        self.files.append(file)
        return NS(id=9000 + len(self.sent), jump_url=f"https://discord.com/channels/1/{self.id}/{9000 + len(self.sent)}")

    def get_partial_message(self, message_id):
        channel = self

        class Partial:
            async def edit(self, view=None):
                channel.edited.append((message_id, view))
        return Partial()


class FakeMessage:
    def __init__(self, manage=True, uid=1, channel=None):
        self.author = NS(id=uid)
        self.guild = NS(id=9, me=BOT_MEMBER)
        self.channel = channel or FakeChannel(manage=manage)
        self.replies = []

    async def reply(self, content, allowed_mentions=None):
        self.replies.append(content)


def make_bot(store=None, feed=None):
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    b.predictions, b.esports_feed, b.prediction_ui = store, feed, None
    b._can_manage = bot.MeeseeksBot._can_manage.__get__(b)
    return b


def resolved_store():
    s = pr.PredictionStore(":memory:")
    now = time.time()
    k = s.register({"id": "m1", "start": now - 7200, "team1": "A", "team2": "B", "label": "x"}, 1, 1)
    for uid, side in [(1, 1), (2, 2), (3, 1)]:
        s.pick(k, uid, f"user{uid}", side, now - 9000)
    s.resolve(k, 2, now - 60)
    return s


def test_routing_and_help():
    check("predictions 路由", bot.parse_command("predictions") == ("predictions", "")
          and bot.parse_command("predictions reset") == ("predictions", "reset"))
    check("帮助里有 predictions", "@mrmeeseeks predictions" in bot.HELP_TEXT and "predictions reset" in bot.HELP_TEXT)


def test_predictions_command():
    msg = FakeMessage()
    asyncio.run(make_bot(None)._predictions(msg, ""))
    check("没开竞猜时提示", "not enabled" in msg.replies[0])

    s = resolved_store()
    msg = FakeMessage(uid=2)
    asyncio.run(make_bot(s)._predictions(msg, ""))
    text = msg.replies[0]
    check("本周榜和赛季榜都有", "🏆 **This week**" in text and "🏅 **Season**" in text)
    check("冷门猜中的 user2 排第一（3 人中 1 人押中：1.7 分）", "1. user2 — 1.7 pts (1/1)" in text)
    check("显示自己的排名", "You: #1 — 1.7 pts (1/1)" in text)

    msg = FakeMessage(manage=False)
    asyncio.run(make_bot(s)._predictions(msg, "reset"))
    check("reset 需要管理服务器权限", "Manage Server" in msg.replies[0] and s.season_start() == 0)
    msg = FakeMessage(manage=True)
    asyncio.run(make_bot(s)._predictions(msg, "reset"))
    check("reset 开新赛季", s.season_start() > 0 and msg.replies[0].startswith("🏁"))
    msg = FakeMessage(uid=2)
    asyncio.run(make_bot(s)._predictions(msg, ""))
    check("新赛季总榜清零，本周榜不受影响", "1. user2" in msg.replies[0].split("🏅")[0]
          and "No resolved predictions yet." in msg.replies[0].split("🏅")[1])


def test_preview_includes_prediction():
    now = datetime.now(timezone.utc)
    tour = "Overwatch Champions Series/2026/Asia/Stage 3/Korea/Regular Season#Week 3"
    start = int((now + timedelta(hours=2)).timestamp())
    m = {"id": f"{tour}|{start}|T1|ZETA DIVISION", "start": start, "tournament": tour, "label": "OWCS Korea",
         "team1": "T1", "team2": "ZETA DIVISION", "best_of": 5, "finished": False, "score": None,
         "url": "https://liquipedia.net/x"}
    with tempfile.TemporaryDirectory() as d:
        store = pr.PredictionStore(":memory:")
        feed = EsportsFeed(os.path.join(d, "s.json"), predictions=PredictionFeed(store, RosterCache(None)))
        feed.matches = [m]
        feed.set_channel(555)
        msg = FakeMessage(manage=True)
        asyncio.run(make_bot(store, feed)._esports(msg, "preview"))
        sample = msg.channel.sent[-1]
        check("preview 最后多一条竞猜样例", sample[0].startswith("🧪 Preview\n🎯 **Who wins?** T1 vs ZETA DIVISION"))
        check("样例的按钮禁用", sample[2] is not None and all(c.item.disabled for c in sample[2].children))
        check("preview 不登记竞猜", store.active() == [])
        check("没开图片模式：样例不带图片", msg.channel.files[-1] is None)

    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as logos_dir:
        store = pr.PredictionStore(":memory:")
        feed = EsportsFeed(os.path.join(d, "s.json"),
                           predictions=PredictionFeed(store, RosterCache(None), LogoCache(logos_dir)))
        feed.matches = [m]
        feed.set_channel(555)
        msg = FakeMessage(manage=True)
        asyncio.run(make_bot(store, feed)._esports(msg, "preview"))
        f = msg.channel.files[-1]
        check("图片模式：样例附上对阵图 matchup.png", f is not None and f.filename == "matchup.png"
              and f.fp.read().startswith(b"\x89PNG"))


def test_post_and_edit():
    channel = FakeChannel(777)
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)

    async def ready():
        return None

    async def get_channel(cid):
        return channel
    b.wait_until_ready, b._channel = ready, get_channel
    spec = {"key": pr.key_for("m1"), "labels": ["T1", "ZETA"], "counts": [0, 0], "disabled": False}
    mid, url = asyncio.run(b.post_esports(777, "🎯 hi", spec))
    check("带按钮发送，返回消息 ID 和链接", (mid, url) == (9001, "https://discord.com/channels/1/777/9001")
          and channel.sent[0][2] is not None and len(channel.sent[0][2].children) == 2)
    check("不带按钮时照旧（补丁推送共用）", asyncio.run(b.post_esports(777, "plain"))[0] == 9002
          and channel.sent[1][2] is None)
    mid, _ = asyncio.run(b.post_esports(777, "🎯 card", spec, b"\x89PNG fake"))
    check("带对阵图发送：附件名 matchup.png", channel.files[-1] is not None and channel.files[-1].filename == "matchup.png"
          and channel.files[-1].fp.read() == b"\x89PNG fake" and channel.sent[-1][2] is not None)
    asyncio.run(b.edit_esports(777, 9001, dict(spec, disabled=True)))
    check("编辑原消息的按钮", channel.edited[0][0] == 9001 and all(c.item.disabled for c in channel.edited[0][1].children))


def test_setup_hook():
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    added = []
    b.add_dynamic_items = lambda *items: added.extend(items)
    b.wish_view = None
    b.prediction_ui = ui.PredictionUI(pr.PredictionStore(":memory:"))
    asyncio.run(b.setup_hook())
    check("启动时注册竞猜按钮并设置 handler", added == [ui.PredictButton] and ui.PredictButton.handler is b.prediction_ui)
    ui.PredictButton.handler = None


if __name__ == "__main__":
    test_routing_and_help()
    test_predictions_command()
    test_preview_includes_prediction()
    test_post_and_edit()
    test_setup_hook()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
