"""赛事推送的 Discord 指令：路由、权限检查、here/off/status，以及推送用的发送函数（假的 Discord 对象）。
也测赛区频道绑定（region）和开播通知（live add/remove/list、发送）。

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
import live_watch as lw  # noqa: E402
from esports_feed import EsportsFeed  # noqa: E402
from regions import RegionStore  # noqa: E402

results = []
NS = types.SimpleNamespace


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


BOT_MEMBER = NS(id=2)


class FakeChannel:
    def __init__(self, channel_id, manage, bot_can_send=True):
        self.id, self.manage, self.bot_can_send, self.sent = channel_id, manage, bot_can_send, []

    def permissions_for(self, member):
        # bot 自己（guild.me）和指令发起者的权限分开
        if member is BOT_MEMBER:
            return NS(manage_channels=False, manage_messages=False, send_messages=self.bot_can_send)
        return NS(manage_channels=self.manage, manage_messages=self.manage, send_messages=True)

    async def send(self, text, allowed_mentions=None, embed=None):
        self.sent.append((text, allowed_mentions, embed))


class FakeMessage:
    def __init__(self, manage=True, guild=True, channel_id=555, bot_can_send=True):
        self.author = NS(id=1)
        self.guild = NS(id=9, me=BOT_MEMBER) if guild else None
        self.channel = FakeChannel(channel_id, manage, bot_can_send)
        self.replies = []

    async def reply(self, content):
        self.replies.append(content)

    @property
    def last(self):
        return self.replies[-1]


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

        m = FakeMessage(manage=True, channel_id=999, bot_can_send=False)
        asyncio.run(b._esports(m, "here"))
        check("bot 在该频道不能发言时拒绝，频道不变", "Send Messages" in m.replies[0] and feed.channel_id == 555)

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


KR = "Overwatch Champions Series/2026/Asia/Stage 3/Korea/Regular Season#Week 1"


def _room(room_id, live):
    return {"room_id": room_id, "live_status": live, "title": "OWCS Finals", "uname": "守望先锋赛事",
            "cover": "https://i0.hdslb.com/c.jpg", "live_url": f"https://live.bilibili.com/{room_id}",
            "parent_area_name": "赛事", "area_name": "游戏赛事"}


def test_region_and_live():
    check("region 指令路由，参数保留原样", bot.parse_command("region CN") == ("region", "CN"))
    check("live 指令路由，参数保留大小写（标签）",
          bot.parse_command("live add cn https://live.bilibili.com/6 OWCS China") == ("live", "add cn https://live.bilibili.com/6 OWCS China"))
    check("帮助里列出 region 和 live", "region cn" in bot.HELP_TEXT and "live add" in bot.HELP_TEXT)

    live = {7734200: 1}

    def fake_fetch(url, params):
        if url == lw.ROOM_INIT_URL:
            if params["id"] in (6, 7734200):
                return {"code": 0, "data": {"room_id": 7734200, "short_id": 6}}
            return {"code": 60004, "msg": "房间不存在", "data": []}
        return {"code": 0, "data": {"by_room_ids": {str(r): _room(r, live.get(r, 0)) for r in params["room_ids"]}}}

    with tempfile.TemporaryDirectory() as d:
        b = make_bot(None)
        b.regions = RegionStore(os.path.join(d, "regions.json"))
        store = lw.LiveStore(os.path.join(d, "live_sources.json"))
        b.live_watcher = lw.LiveWatcher(store, lw.BilibiliClient(fetch=fake_fetch))
        b.live_notify_offline = False
        b.esports_feed = EsportsFeed(os.path.join(d, "esports_state.json"), regions=b.regions)

        def run(method, msg, arg):
            asyncio.run(method(msg, arg))
            return msg.last

        # ---- region ----
        check("没有管理频道权限不能绑定赛区", "Manage Channels" in run(b._region, FakeMessage(manage=False), "cn")
              and b.regions.channels == {})
        check("私信里不能绑定", "Manage Channels" in run(b._region, FakeMessage(guild=False), "cn"))
        r = run(b._region, FakeMessage(channel_id=555), "CN")
        check("绑定：赛区名小写、记下当前频道并持久化", b.regions.get("cn") == 555 and r.startswith("✅")
              and RegionStore(b.regions.path).channels == {} and _reload_regions(b.regions.path) == {"cn": 555})
        run(b._region, FakeMessage(channel_id=777), "asia")
        check("bot 不能发言的频道拒绝绑定", "Send Messages" in run(b._region, FakeMessage(channel_id=1, bot_can_send=False), "na")
              and b.regions.get("na") is None)
        check("非法赛区名", run(b._region, FakeMessage(), "e!m").startswith("❌") and b.regions.get("e!m") is None)
        check("不是 Liquipedia 赛区名时提醒不会自动分流", "⚠️" in run(b._region, FakeMessage(channel_id=2), "owl"))
        listed = run(b._region, FakeMessage(), "")
        check("region 不带参数列出绑定", "<#555>" in listed and "<#777>" in listed and "esports here" in listed)
        check("region <赛区> off 解绑", "no longer bound" in run(b._region, FakeMessage(), "owl off")
              and "had no channel" in run(b._region, FakeMessage(), "owl off") and b.regions.get("owl") is None)

        # ---- esports status 显示赛区频道和每场比赛的去向 ----
        b.esports_feed.set_channel(42)
        b.esports_feed.matches = [{"id": "x", "start": int(time.time()) + 3600, "finished": False, "team1": "T1",
                                   "team2": "ZETA DIVISION", "label": "OWCS Korea Stage 3", "tournament": KR}]
        st = run(b._esports, FakeMessage(manage=False), "status")
        check("status 列出赛区频道，比赛标出目标频道", "asia → <#777>" in st and "T1 vs ZETA DIVISION" in st and "→ <#777>" in st)

        # ---- live ----
        check("没有管理消息权限不能加直播间", "Manage Messages" in run(b._live, FakeMessage(manage=False), "add cn 6"))
        r = run(b._live, FakeMessage(), "add cn https://live.bilibili.com/6?spm=1 OWCS China")
        src = store.sources.get("bilibili:7734200")
        check("添加：短号解析成真实房间号、标签保留大小写、持久化", src is not None and src.label == "OWCS China"
              and src.region == "cn" and _reload_live(store.path))
        check("回复当前在播状态和通知频道", "LIVE" in r and "<#555>" in r)
        check("添加时当前状态作为基线，不会立刻通知", b.live_watcher.state["bilibili:7734200"] is True)
        r = run(b._live, FakeMessage(), "add emea 7734200")
        check("重复添加视为更新；没写标签用主播昵称；赛区没频道时提醒", "Updated" in r and "守望先锋赛事" in r and "region emea" in r
              and store.sources["bilibili:7734200"].region == "emea")
        check("非 B 站链接", run(b._live, FakeMessage(), "add na twitch.tv/x").startswith("❌"))
        check("不存在的房间", "not found" in run(b._live, FakeMessage(), "add na 999"))
        check("非法赛区名", run(b._live, FakeMessage(), "add 'x y' 6").startswith("❌"))
        listed = run(b._live, FakeMessage(), "list")
        check("list 按赛区列出并标出没频道的赛区", "**emea**" in listed and "no channel" in listed and "🔴" in listed)
        check("live 不带参数等于 list", run(b._live, FakeMessage(), "") == listed)
        check("没有管理消息权限不能删", "Manage Messages" in run(b._live, FakeMessage(manage=False), "remove 6"))
        check("用短号删除", run(b._live, FakeMessage(), "remove 6").startswith("🗑️") and store.sources == {})
        check("删不存在的", "Not watching" in run(b._live, FakeMessage(), "remove 6"))
        check("未知子命令显示用法", run(b._live, FakeMessage(), "bogus").startswith("Usage"))
        check("没有直播源时 list 显示用法", "Usage" in run(b._live, FakeMessage(), "list"))
        nolive = make_bot(None)
        nolive.live_watcher = None
        check("未启用时提示", "not enabled" in run(nolive._live, FakeMessage(), "list"))

        # ---- 开播通知发送 ----
        channel = FakeChannel(555, True)
        waited = []

        async def wait_until_ready():
            waited.append(True)

        async def _channel(channel_id):
            channel.asked = channel_id
            return channel

        b.wait_until_ready, b._channel = wait_until_ready, _channel
        source = lw.Source("bilibili", 7734200, "cn", "OW", "https://live.bilibili.com/7734200")
        status = lw.RoomStatus(7734200, True, title="OWCS Finals", cover="https://i0.hdslb.com/c.jpg",
                               url=source.url, area="赛事")
        asyncio.run(b._live_notify(lw.LiveEvent("live", source, status)))
        check("开播：发到赛区频道，带嵌入消息，不 ping 任何人",
              channel.asked == 555 and len(channel.sent) == 1 and channel.sent[0][0].startswith("**OW** just went live")
              and channel.sent[0][2].title == "🔴 OW is live" and channel.sent[0][2].image.url == status.cover
              and channel.sent[0][1].everyone is False and waited == [True])
        asyncio.run(b._live_notify(lw.LiveEvent("offline", source, status)))
        check("下播默认不发", len(channel.sent) == 1)
        b.live_notify_offline = True
        asyncio.run(b._live_notify(lw.LiveEvent("offline", source, status)))
        check("LIVE_NOTIFY_OFFLINE 开启时下播发一条", len(channel.sent) == 2 and "ended" in channel.sent[1][0])
        unbound = lw.Source("bilibili", 1, "emea", "X", "u")
        asyncio.run(b._live_notify(lw.LiveEvent("live", unbound, status)))
        check("赛区没绑频道时不发", len(channel.sent) == 2)


def _reload_regions(path):
    r = RegionStore(path)
    r.load()
    return r.channels


def _reload_live(path):
    s = lw.LiveStore(path)
    s.load()
    return "bilibili:7734200" in s.sources


if __name__ == "__main__":
    test_routing()
    test_commands()
    test_post_esports()
    test_region_and_live()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
