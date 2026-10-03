"""赛事推送的 Discord 指令：路由、权限检查、here/off/status，以及推送用的发送函数（假的 Discord 对象）。

运行：python tests/test_esports_bot.py
"""
import asyncio
import os
import sys
import tempfile
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import bot  # noqa: E402
from esports_feed import EsportsFeed  # noqa: E402

results = []
NS = types.SimpleNamespace


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


class FakeChannel:
    def __init__(self, channel_id, manage):
        self.id, self.manage, self.sent = channel_id, manage, []

    def permissions_for(self, member):
        return NS(manage_channels=self.manage)

    async def send(self, text, allowed_mentions=None):
        self.sent.append((text, allowed_mentions))


class FakeMessage:
    def __init__(self, manage=True, guild=True, channel_id=555):
        self.author = NS(id=1)
        self.guild = NS(id=9) if guild else None
        self.channel = FakeChannel(channel_id, manage)
        self.replies = []

    async def reply(self, content):
        self.replies.append(content)


def make_bot(feed):
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)   # 不连 Discord，只测指令
    b.esports_feed = feed
    return b


def test_routing():
    check("esports here 路由到 esports，参数 here", bot.parse_command("esports here") == ("esports", "here"))
    check("大小写不敏感", bot.parse_command("Esports STATUS") == ("esports", "STATUS"))
    check("只回复 bot（没有显式 @）时指令仍可用", bot.parse_command("esports off", explicit=False) == ("esports", "off"))
    check("帮助里列出 esports 指令", "esports here" in bot.HELP_TEXT)


def test_commands():
    with tempfile.TemporaryDirectory() as d:
        feed = EsportsFeed(os.path.join(d, "esports_state.json"))
        b = make_bot(feed)

        m = FakeMessage(manage=False)
        asyncio.run(b._esports(m, "here"))
        check("没有管理频道权限不能设置", "Manage Channels" in m.replies[0] and feed.channel_id is None)
        m = FakeMessage(guild=False)
        asyncio.run(b._esports(m, "here"))
        check("私信里不能设置", "Manage Channels" in m.replies[0] and feed.channel_id is None)

        m = FakeMessage(manage=True, channel_id=555)
        asyncio.run(b._esports(m, "here"))
        check("有权限：设为当前频道并持久化", feed.channel_id == 555 and _reloaded(feed.path) == 555)
        check("回复确认", m.replies[0].startswith("✅"))

        feed.matches = [{"id": "x", "start": int(time.time()) + 3600, "finished": False, "team1": "T1",
                         "team2": "ZETA DIVISION", "label": "OWCS Korea Stage 3"}]
        m = FakeMessage(manage=False)
        asyncio.run(b._esports(m, "status"))
        check("status 不需要权限，显示频道和接下来的比赛", "<#555>" in m.replies[0] and "T1 vs ZETA DIVISION" in m.replies[0])

        m = FakeMessage(manage=True)
        asyncio.run(b._esports(m, "off"))
        check("off 关闭推送", feed.channel_id is None and "turned off" in m.replies[0])
        m = FakeMessage()
        asyncio.run(b._esports(m, "status"))
        check("未设置时 status 提示怎么设置", "not set" in m.replies[0])
        m = FakeMessage()
        asyncio.run(b._esports(m, "whatever"))
        check("未知参数显示用法", m.replies[0].startswith("Usage"))

    m = FakeMessage()
    asyncio.run(make_bot(None)._esports(m, "here"))
    check("没有启用推送时提示", "not enabled" in m.replies[0])


def _reloaded(path):
    f = EsportsFeed(path)
    f.load()
    return f.channel_id


def test_post_esports():
    channel = FakeChannel(777, True)
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    waited = []

    async def wait_until_ready():
        waited.append(True)

    async def _channel(channel_id):
        return channel

    b.wait_until_ready, b._channel = wait_until_ready, _channel
    asyncio.run(b.post_esports(777, "📅 hello"))
    check("发送前等 bot 就绪", waited == [True])
    check("发送内容，并且不 ping 任何人", channel.sent[0][0] == "📅 hello"
          and channel.sent[0][1].everyone is False and channel.sent[0][1].users is False)


if __name__ == "__main__":
    test_routing()
    test_commands()
    test_post_esports()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
