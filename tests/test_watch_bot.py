"""一起看的 Discord 接入：指令路由、房间卡片文字、「我是房主」按钮、卡片编辑节流、重启后的遗留卡片。
用假的 Discord 对象，不连 Discord、不联网。

运行：python tests/test_watch_bot.py
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import types
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import discord  # noqa: E402

import bot  # noqa: E402
import watch_ui as wui  # noqa: E402
from vod_playlist import PlaylistError  # noqa: E402
from vod_search import Episode, Line, SearchUnavailable, Show  # noqa: E402
from watch_progress import Progress, ProgressStore  # noqa: E402
from watch_rooms import RoomManager, Video, VodInfo, vod_video  # noqa: E402
from watch_sources import VideoUnavailable  # noqa: E402

results = []
NS = types.SimpleNamespace
PUBLIC = "https://watch.example.com"


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def yt(title="Never Gonna", duration=195.0):
    return Video("youtube", "dQw4w9WgXcQ", 1, title, duration, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")


def bili(title="B站视频", duration=100.0):
    return Video("bilibili", "BV1xx411c7mD", 1, title, duration, "https://www.bilibili.com/video/BV1xx411c7mD?p=1")


class Clock:
    def __init__(self, t=1000.0):
        self.t = t
        self.sleepers = []      # 假 sleep：测试调用 release() 才"过去"这段时间

    def release(self):
        while self.sleepers:
            s, fut = self.sleepers.pop(0)
            self.t += s
            if not fut.done():
                fut.set_result(None)

    def __call__(self):
        return self.t


def make_ui(clock=None, cards_path=None, max_bili=2):
    clock = clock or Clock()
    rooms = RoomManager(clock=clock, max_bili_rooms=max_bili)

    async def fake_sleep(s):
        fut = asyncio.get_running_loop().create_future()
        clock.sleepers.append((s, fut))
        await fut
    ui = wui.WatchUI(rooms, PUBLIC, wui.CardStore(Path(cards_path) if cards_path else None), sleep=fake_sleep)
    edits = []

    async def edit(channel_id, message_id, text, view):
        edits.append((channel_id, message_id, text, view))
    ui.edit = edit
    return ui, rooms, clock, edits


async def drain(ui):
    for _ in range(20):
        await asyncio.sleep(0)
        ui.rooms.clock.release()
        await asyncio.sleep(0)
        tasks = [t for t in ui._tasks if not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


class FakeSources:
    def __init__(self, video=None, error=None):
        self.video, self.error, self.calls = video, error, []

    async def resolve(self, text):
        self.calls.append(text)
        if self.error:
            raise self.error
        return self.video


class FakeMessage:
    def __init__(self, uid=1, name="小明", channel_id=555):
        self.author = NS(id=uid, display_name=name, bot=False)
        self.channel = NS(id=channel_id)
        self.replies = []

    async def reply(self, content, view=None, allowed_mentions=None):
        self.replies.append((content, view, allowed_mentions))
        return NS(id=7000 + len(self.replies))


def make_bot(ui=None, sources=None, vod_search=None, watch_server=None, progress=None):
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    b.watch_ui, b.watch_sources = ui, sources
    b.vod_search, b.watch_server, b.progress = vod_search, watch_server, progress
    return b


def make_interaction(uid):
    sent = NS(content=None, ephemeral=None)

    async def send_message(content, ephemeral=False):
        sent.content, sent.ephemeral = content, ephemeral
    return NS(user=NS(id=uid, display_name="x"), response=NS(send_message=send_message)), sent


# ---------------------------------------------------------------- tests
def test_parse_and_route():
    check("中文指令", bot.parse_command("一起看 https://youtu.be/dQw4w9WgXcQ") == ("一起看", "https://youtu.be/dQw4w9WgXcQ"))
    check("watch 指令", bot.parse_command("watch https://youtu.be/x")[0] == "watch")
    check("watch 不带参数也是指令", bot.parse_command("watch") == ("watch", ""))
    url = "https://youtu.be/dQw4w9WgXcQ"
    check("一起看紧贴链接", bot.parse_command("一起看" + url) == ("一起看", url))
    check("一起看后全角空格", bot.parse_command("一起看\u3000" + url) == ("一起看", url))
    check("一起看不带参数", bot.parse_command("一起看") == ("一起看", ""))
    check("一起看 explicit=False 也是指令", bot.parse_command("一起看" + url, explicit=False) == ("一起看", url))
    check("watch 紧贴链接", bot.parse_command("watch" + url) == ("watch", url))
    check("watch 后全角空格 / 大小写", bot.parse_command("Watch\u3000" + url) == ("watch", url))
    check("watch 开头的普通问题不是指令", bot.parse_command("watchpoint genji?") == ("ask", "watchpoint genji?"))
    check("帮助里有一起看", "`@mrmeeseeks 一起看 <Bilibili/YouTube link or show name>` (or `watch`)" in bot.HELP_TEXT)
    check("按片名一起看", bot.parse_command("一起看 绝命毒师") == ("一起看", "绝命毒师")
          and bot.parse_command("watch Breaking Bad") == ("watch", "Breaking Bad"))


def test_card_text():
    ui, rooms, clock, _ = make_ui()
    r = rooms.create(555, 1, "小明", yt())
    r.playback.playing, r.playback.position, r.playback.at = True, 0.0, clock.t
    for i in range(2):
        rooms.connect(r.room_id, None, None)
    text = wui.card_text(r, clock.t + 10)
    check("标题行", text.startswith("🎬 **一起看**\n正在播放：**Never Gonna**（YouTube）"))
    check("播放中", "▶ 播放中 0:10 / 3:15 · 👥 2 人在看" in text)
    check("房主行", text.endswith(f"房主：{r.host_name()}"))
    r.playback.playing = False
    check("暂停", "⏸ 已暂停 0:00 / 3:15" in wui.card_text(r, clock.t + 10))
    r2 = rooms.create(555, 1, "小明", bili(duration=None))
    t2 = wui.card_text(r2, clock.t)
    check("无时长不显示总长", " / " not in t2 and "（B 站）" in t2 and "👥 0 人在看" in t2)
    r2.video.duration = 4000.0
    r2.playback.position = 3725.0
    check("超过一小时用 h:mm:ss", "1:02:05 / 1:06:40" in wui.card_text(r2, clock.t))
    r.playback.playing, r.playback.position, r.playback.at = True, 0.0, clock.t
    check("播到结尾显示已播完", "⏹ 已播完 3:15 / 3:15" in wui.card_text(r, clock.t + 500))
    r.playback.playing, r.playback.position = False, 195.0
    check("暂停在结尾也是已播完", "⏹ 已播完" in wui.card_text(r, clock.t))
    r.playback.position = 194.0
    check("没到结尾仍是已暂停", "⏸ 已暂停" in wui.card_text(r, clock.t))
    ended = wui.card_text(r, clock.t, ended=True, reason="服务器重启")
    check("结束文案", ended == "🎬 **一起看** · 已结束\n最后播放：**Never Gonna**\n（服务器重启）")
    check("结束无原因不加行", wui.card_text(r, clock.t, ended=True) == "🎬 **一起看** · 已结束\n最后播放：**Never Gonna**")


def test_build_view():
    ui, rooms, _, _ = make_ui()
    r = rooms.create(555, 1, "小明", yt())
    view = wui.build_view(r, PUBLIC)
    items = view.children
    check("两个按钮", len(items) == 2)
    check("进入房间是链接按钮", items[0].style == discord.ButtonStyle.link and items[0].url == f"{PUBLIC}/r/{r.room_id}"
          and items[0].label == "进入房间")
    host = items[1]
    check("我是房主是持久按钮", isinstance(host, wui.HostButton) and host.item.custom_id == f"watch:host:{r.room_id}"
          and host.item.label == "我是房主" and view.timeout is None)


def test_watch_command():
    msg = FakeMessage()
    asyncio.run(make_bot()._watch(msg, "https://youtu.be/dQw4w9WgXcQ"))
    check("没启用", msg.replies[0][0] == "一起看功能没有启用。")

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "watch_cards.json")
        ui, rooms, _, _ = make_ui(cards_path=path)
        src = FakeSources(yt())
        b = make_bot(ui, src)
        msg = FakeMessage()
        asyncio.run(b._watch(msg, "看这个"))
        check("无链接且没启用搜索", msg.replies[0][0] == "按片名搜索没有启用，请贴 B 站或 YouTube 链接"
              and src.calls == [])

        msg = FakeMessage(uid=42, name="小红")
        asyncio.run(b._watch(msg, "来看 https://youtu.be/dQw4w9WgXcQ"))
        room = rooms.active()[0]
        text, view, mentions = msg.replies[0]
        check("建房发卡片并记录", text.startswith("🎬 **一起看**") and view is not None and ui.cards.all() == [(555, 7001)]
              and room.message_id == 7001 and room.creator_id == 42 and room.creator_name == "小红"
              and room.channel_id == 555)
        check("卡片不 ping 人", mentions is not None and mentions.everyone is False and mentions.users is False)
        check("卡片记录写到文件", json.loads(Path(path).read_text(encoding="utf-8")) == {"cards": [[555, 7001]]})
        check("重新读取卡片记录", wui.CardStore(Path(path)).all() == [(555, 7001)])

        msg = FakeMessage()
        asyncio.run(make_bot(ui, FakeSources(error=VideoUnavailable("这个视频在海外服务器上无法播放")))._watch(
            msg, "https://www.bilibili.com/video/BV1xx411c7mD"))
        check("放不了提示", "这个视频在海外服务器上无法播放" in msg.replies[0][0])

        ui2, rooms2, _, _ = make_ui(max_bili=1)
        rooms2.create(1, 1, "a", bili())
        msg = FakeMessage()
        asyncio.run(make_bot(ui2, FakeSources(bili()))._watch(msg, "https://www.bilibili.com/video/BV1xx411c7mD"))
        check("B 站房间已满提示", msg.replies[0][0] == "B 站房间已满（最多 1 个），请稍后再试或换 YouTube 视频")

        # 卡片发不出去：房间直接关掉并从 rooms 删掉，WatchUI 收到 closed 也不去编辑（没有 message_id）
        ui3, rooms3, _, edits3 = make_ui(max_bili=1)
        msg = FakeMessage()

        async def fail_reply(content, view=None, allowed_mentions=None):
            raise discord.HTTPException(NS(status=403, reason="Forbidden"), "no perms")
        msg.reply = fail_reply
        closed = []
        rooms3.listeners.append(lambda r, k: closed.append(k) if k == "closed" else None)

        async def post_fail():
            await make_bot(ui3, FakeSources(bili()))._watch(msg, "https://www.bilibili.com/video/BV1xx411c7mD")
            await drain(ui3)
        asyncio.run(post_fail())
        check("卡片发送失败就关房并删掉", rooms3.rooms == {} and closed == ["closed"] and edits3 == []
              and ui3.cards.all() == [])
        check("关房后 B 站名额释放", rooms3.create(1, 1, "a", bili()) is not None)


def test_card_store_corrupt():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "watch_cards.json"
        check("文件不存在 → 空", wui.CardStore(p).all() == [])
        p.write_text("{not json", encoding="utf-8")
        check("文件损坏 → 空", wui.CardStore(p).all() == [])
        s = wui.CardStore(p)
        s.add(1, 2)
        s.add(1, 2)
        s.add(3, 4)
        s.remove(1, 2)
        check("增删、不重复", s.all() == [(3, 4)] and wui.CardStore(p).all() == [(3, 4)])


def test_host_button():
    ui, rooms, _, _ = make_ui()
    r = rooms.create(555, 42, "小红", yt())
    rid = r.room_id
    it, sent = make_interaction(42)
    asyncio.run(ui.on_host(it, rid))
    check("发起人拿到 ephemeral 房主链接", sent.ephemeral and f"{PUBLIC}/r/{rid}?k={r.owner_key}" in sent.content)
    it, sent = make_interaction(7)
    asyncio.run(ui.on_host(it, rid))
    check("别人点提示不是房主", sent.content == "你不是这个房间的房主。" and sent.ephemeral)
    m = rooms.connect(rid, None, r.owner_key)
    rooms.connect(rid, None, None)
    rooms.disconnect(rid, m.viewer_id)
    it, sent = make_interaction(42)
    asyncio.run(ui.on_host(it, rid))
    check("链接用过后提示", sent.content == f"房主链接已经用过了，房主身份现在在房间里的「{r.host_name()}」手上。"
          and sent.ephemeral)
    it, sent = make_interaction(42)
    asyncio.run(ui.on_host(it, "zzzzzz"))
    check("房间不存在", sent.content == "这个房间已经结束了。" and sent.ephemeral)

    # 按钮回调走 handler；没有 handler 时提示没启用
    btn = wui.HostButton(rid)
    it, sent = make_interaction(42)
    wui.HostButton.handler = None
    asyncio.run(btn.callback(it))
    check("没 handler 提示没启用", sent.content == "一起看功能没有启用。" and sent.ephemeral)
    wui.HostButton.handler = ui
    it, sent = make_interaction(42)
    asyncio.run(btn.callback(it))
    check("按钮回调交给 handler", "房主链接已经用过了" in sent.content)
    wui.HostButton.handler = None


def test_throttle_and_close():
    with tempfile.TemporaryDirectory() as d:
        ui, rooms, clock, edits = make_ui(cards_path=os.path.join(d, "c.json"))

        async def scenario():
            r = rooms.create(555, 1, "小明", yt())
            rooms.connect(r.room_id, None, None)        # 还没发卡片：不编辑
            await drain(ui)
            no_card = len(edits)
            r.message_id = 9001
            ui.cards.add(555, 9001)
            start = clock.t
            for _ in range(3):
                rooms.connect(r.room_id, None, None)
            for _ in range(5):
                await asyncio.sleep(0)
            first = len(edits)
            await drain(ui)
            second = len(edits)
            delay = clock.t - start
            last = edits[-1]
            r.closed = True
            rooms._notify(r, "closed")
            await drain(ui)
            return r, no_card, first, second, delay, last

        r, no_card, first, second, delay, last = asyncio.run(scenario())
        check("没有卡片时不编辑", no_card == 0)
        check("连续变化只立即编辑 1 次", first == 1)
        check("15 秒后合并编辑 1 次", second == 2 and abs(delay - wui.MIN_EDIT_INTERVAL) < 1e-6)
        check("延后编辑用最新状态", "👥 4 人在看" in last[2] and last[:2] == (555, 9001)
              and last[3] is not None)
        check("closed 立即编辑、去掉按钮", len(edits) == 3 and "已结束" in edits[-1][2] and edits[-1][3] is None)
        check("closed 从 CardStore 删除", ui.cards.all() == [])


def test_edit_failure_logged():
    ui, rooms, _, _ = make_ui()

    async def boom(*a):
        raise discord.HTTPException(NS(status=404, reason="Not Found"), "gone")
    ui.edit = boom

    async def scenario():
        r = rooms.create(555, 1, "小明", yt())
        r.message_id = 1
        rooms.connect(r.room_id, None, None)
        await drain(ui)
        return [t for t in ui._tasks if not t.done()]

    left = asyncio.run(scenario())
    check("编辑失败只记日志、不抛出", left == [])


def test_stale_cards_on_ready():
    with tempfile.TemporaryDirectory() as d:
        ui, _, _, edits = make_ui(cards_path=os.path.join(d, "c.json"))
        ui.cards.add(1, 11)
        ui.cards.add(2, 22)
        b = make_bot(ui, FakeSources(yt()))
        b.wish_view = None
        b._connection = NS(user=None)
        asyncio.run(b.on_ready())
        check("遗留卡片都编辑成已结束（服务器重启）", [(c, m) for c, m, _, _ in edits] == [(1, 11), (2, 22)]
              and all("已结束" in t and "（服务器重启）" in t and v is None for _, _, t, v in edits))
        check("CardStore 清空", ui.cards.all() == [] and wui.CardStore(Path(d) / "c.json").all() == [])
        ui.cards.add(3, 33)
        asyncio.run(b.on_ready())
        check("第二次 on_ready 不再编辑", len(edits) == 2)


def test_bot_edit_and_setup_hook():
    edited = []

    class Channel:
        def get_partial_message(self, mid):
            class Partial:
                async def edit(self, content=None, view=None, allowed_mentions=None):
                    edited.append((mid, content, view, allowed_mentions))
            return Partial()

    b = make_bot()

    async def ready():
        return None

    async def get_channel(cid):
        return Channel()
    b.wait_until_ready, b._channel = ready, get_channel
    asyncio.run(b.edit_watch(5, 6, "hi", None))
    check("编辑卡片：文字、去掉按钮、不 ping", edited and edited[0][:3] == (6, "hi", None)
          and edited[0][3] is not None and edited[0][3].users is False)

    ui, _, _, _ = make_ui()
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    added = []
    b.add_dynamic_items = lambda *items: added.extend(items)
    b.wish_view = b.announce_desk = b.prediction_ui = None
    b.watch_ui = ui
    b.vod_search = b.watch_server = b.progress = None
    asyncio.run(b.setup_hook())
    check("启动时注册房主按钮和选单并设置 handler", added == [wui.HostButton, wui.PickSelect]
          and wui.HostButton.handler is ui and wui.PickSelect.handler is ui and ui.create_vod_room == b._create_vod_room)
    wui.HostButton.handler = wui.PickSelect.handler = None


# ---------------------------------------------------------------- 按片名搜索
def show(title="绝命毒师 第一季", key="k1", year=2008, type_name="欧美剧", eps=7):
    lines = [Line("量子", [Episode(f"第{i + 1}集", f"https://a.example.com/{i}.m3u8") for i in range(eps)]),
             Line("1080资源", [Episode(f"第{i + 1}集", f"https://b.example.com/{i}.m3u8") for i in range(eps)])]
    return Show(key, title, year, type_name, lines)


class FakeVodSearch:
    def __init__(self, shows=None, error=None):
        self.shows, self.error, self.calls = shows or [], error, []

    async def search(self, query):
        self.calls.append(query)
        if self.error:
            raise self.error
        return self.shows


class FakeWatchServer:
    def __init__(self, error=None):
        self.error, self.prepared, self.attached = error, [], []

    async def prepare_vod(self, show, episode, start_line=0, resume=None):
        self.prepared.append((show, episode, resume))
        if self.error:
            raise self.error
        info = VodInfo(show, episode, 0, resume)
        return vod_video(info, 2400.0), NS(duration=2400.0)

    def attach_playlist(self, room, playlist):
        self.attached.append((room.room_id, playlist))


def make_pick_interaction(uid, index=0):
    it, sent = make_interaction(uid)
    deferred = []

    async def defer():
        deferred.append(True)
    it.response.defer = defer
    it.data = {"values": [str(index)]}
    sent.deferred = deferred
    return it, sent


def setup_search(shows=None, progress=None, ws=None, uid=42):
    ui, rooms, clock, edits = make_ui()
    vs = FakeVodSearch([show()] if shows is None else shows)
    ws = ws or FakeWatchServer()
    b = make_bot(ui, FakeSources(yt()), vs, ws, progress)
    ui.create_vod_room = b._create_vod_room
    msg = FakeMessage(uid=uid, name="小红")
    msg.author.mention = f"<@{uid}>"
    asyncio.run(b._watch(msg, "绝命毒师"))
    return b, ui, rooms, clock, edits, ws, msg


def token_of(msg):
    return msg.replies[0][1].children[0].item.custom_id.split(":")[-1]


def test_watch_search_errors():
    ui, rooms, _, _ = make_ui()
    msg = FakeMessage()
    asyncio.run(make_bot(ui, FakeSources(yt()), None, FakeWatchServer())._watch(msg, "绝命毒师"))
    check("搜索没启用", msg.replies[0][0] == "按片名搜索没有启用，请贴 B 站或 YouTube 链接")
    vs = FakeVodSearch()
    b = make_bot(ui, FakeSources(yt()), vs, FakeWatchServer())
    msg = FakeMessage()
    asyncio.run(b._watch(msg, "x" * 51))
    check("51 个字给用法", msg.replies[0][0] == bot.WATCH_USAGE == "用法：@mrmeeseeks 一起看 <B站或YouTube链接 或 片名>"
          and vs.calls == [])
    msg = FakeMessage()
    asyncio.run(b._watch(msg, ""))
    check("空参数给用法", msg.replies[0][0] == bot.WATCH_USAGE)
    msg = FakeMessage()
    asyncio.run(b._watch(msg, "x" * 50))
    check("50 个字可以搜", vs.calls == ["x" * 50])
    b = make_bot(ui, FakeSources(yt()), FakeVodSearch(error=SearchUnavailable()), FakeWatchServer())
    msg = FakeMessage()
    asyncio.run(b._watch(msg, "绝命毒师"))
    check("资源站不可用", msg.replies[0][0] == "资源站暂时无法访问，请稍后再试")
    b = make_bot(ui, FakeSources(yt()), FakeVodSearch([]), FakeWatchServer())
    msg = FakeMessage()
    asyncio.run(b._watch(msg, "不存在的剧"))
    check("没找到", msg.replies[0][0] == "没找到「不存在的剧」")


def test_watch_search_posts_menu():
    b, ui, rooms, clock, edits, ws, msg = setup_search()
    text, view, mentions = msg.replies[0]
    sel = view.children[0].item
    check("搜索结果文字", text == wui.search_header("绝命毒师", "<@42>")
          and text == "🔎 「绝命毒师」的搜索结果（只有 <@42> 能选，2 分钟内有效）")
    check("选单 custom_id 匹配模板", len(view.children) == 1 and re.fullmatch(wui.PICK_TEMPLATE, sel.custom_id))
    check("选项标签", len(sel.options) <= 10 and sel.options[0].label == "绝命毒师 第一季 · 2008 · 欧美剧 · 7 集"
          and sel.options[0].value == "0" and not sel.options[0].description)
    check("结果消息不 ping", mentions is not None and mentions.users is False)
    check("登记了待选", token_of(msg) in ui.pending and ui.pending[token_of(msg)].message_id == 7001)
    check("缺 year / type_name 就省掉",
          wui.option_label(show(year=None, type_name="")) == "绝命毒师 第一季 · 7 集"
          and wui.option_label(show(title="x" * 200)).__len__() == 100)
    prog = ProgressStore(None)
    prog.record(555, "k1", Progress("绝命毒师 第一季", 2, 300.0, 1.0))
    b, ui, rooms, clock, edits, ws, msg = setup_search(progress=prog)
    check("有进度就写上次看到第几集",
          msg.replies[0][1].children[0].item.options[0].description == "上次看到第 3 集")


def test_pick_flow():
    b, ui, rooms, clock, edits, ws, msg = setup_search()
    tok = token_of(msg)

    async def go(uid):
        it, sent = make_pick_interaction(uid)
        await ui.on_pick(it, tok)
        return sent
    sent = asyncio.run(go(7))
    check("别人选 → 只有发起人能选", sent.content == "只有发起人能选" and sent.ephemeral and rooms.active() == []
          and not tok_done(ui, tok))
    sent = asyncio.run(go(42))
    room = rooms.active()[0]
    check("选中后建 vod 房间", len(rooms.active()) == 1 and room.video.source == "vod" and sent.deferred == [True]
          and room.creator_id == 42 and room.video.vod.episode == 0 and len(ws.attached) == 1)
    check("选中后先改成正在准备…并去掉选单", edits[0] == (555, 7001, "正在准备…", None) and wui.PICK_PREPARING == "正在准备…")
    check("搜索消息改成已选择且去掉选单", edits[1:] == [(555, 7001, "已选择：绝命毒师 第一季", None)])
    text, view, mentions = msg.replies[1]
    check("卡片回复在原消息下并记录", text.startswith("🎬 **一起看**") and "片源：量子" in text and view is not None
          and room.message_id == 7002 and ui.cards.all() == [(555, 7002)]
          and mentions is not None and mentions.users is False)
    check("没有进度时 resume 为空", room.video.vod.resume is None)


def tok_done(ui, tok):
    return ui.pending[tok].done


def test_pick_resume():
    prog = ProgressStore(None)
    pr = Progress("绝命毒师 第一季", 2, 300.0, 1.0)
    prog.record(555, "k1", pr)
    b, ui, rooms, clock, edits, ws, msg = setup_search(progress=prog)

    async def go():
        it, _ = make_pick_interaction(42)
        await ui.on_pick(it, token_of(msg))
    asyncio.run(go())
    check("房间带上进度快照", rooms.active()[0].video.vod.resume == pr and ws.prepared[0][2] == pr)


def test_pick_twice_unknown_unplayable():
    b, ui, rooms, clock, edits, ws, msg = setup_search()
    tok = token_of(msg)

    async def go():
        a, sa = make_pick_interaction(42)
        c, sc = make_pick_interaction(42)
        await asyncio.gather(ui.on_pick(a, tok), ui.on_pick(c, tok))
        return sa, sc
    sa, sc = asyncio.run(go())
    check("连点两次只建一个房间", len(rooms.active()) == 1 and len(ws.prepared) == 1)
    second = [s for s in (sa, sc) if s.content is not None]
    check("第二次提示已过期", len(second) == 1 and second[0].content == "已过期，请重新搜索" and second[0].ephemeral)

    it, sent = make_pick_interaction(42)
    asyncio.run(ui.on_pick(it, "nosuchtoken123"))
    check("未知 token 已过期", sent.content == "已过期，请重新搜索" and sent.ephemeral)

    b, ui, rooms, clock, edits, ws, msg = setup_search(ws=FakeWatchServer(PlaylistError("这一集暂时没有能播的片源")))

    async def go2():
        it, _ = make_pick_interaction(42)
        await ui.on_pick(it, token_of(msg))
    asyncio.run(go2())
    check("没有能播的片源", rooms.active() == [] and edits == [(555, 7001, "正在准备…", None),
                                                          (555, 7001, "这一集暂时没有能播的片源", None)]
          and len(msg.replies) == 1)


def test_pick_preparing_shown_before_prepare():
    """“正在准备…”在开始找片源之前就已经显示（找片源可能要几十秒）。"""
    seen = []

    class SlowServer(FakeWatchServer):
        async def prepare_vod(self, show, episode, start_line=0, resume=None):
            seen.append(list(edits))
            raise PlaylistError("这一集暂时没有能播的片源")
    b, ui, rooms, clock, edits, ws, msg = setup_search(ws=SlowServer())

    async def go():
        it, _ = make_pick_interaction(42)
        await ui.on_pick(it, token_of(msg))
    asyncio.run(go())
    check("找片源时搜索消息已经是正在准备…", seen == [[(555, 7001, "正在准备…", None)]])
    check("超时/没有片源 → 改成没有能播的片源", edits[-1] == (555, 7001, "这一集暂时没有能播的片源", None))


def test_pick_failures():
    # 搜索消息编辑失败：卡片照发、房间保持开着
    b, ui, rooms, clock, edits, ws, msg = setup_search()

    async def boom(*a):
        raise discord.HTTPException(NS(status=404, reason="Not Found"), "gone")
    ui.edit = boom

    async def go(uid=42):
        it, sent = make_pick_interaction(uid)
        await ui.on_pick(it, token_of(msg))
        return sent
    asyncio.run(go())
    room = rooms.active()[0] if rooms.active() else None
    check("搜索消息编辑失败仍发卡片并保持房间", room is not None and len(msg.replies) == 2
          and room.message_id == 7002 and ui.cards.all() == [(555, 7002)])
    sent = asyncio.run(go(7))
    check("选过之后别人点仍提示只有发起人能选", sent.content == "只有发起人能选")
    sent = asyncio.run(go(42))
    check("选过之后发起人再点提示已过期", sent.content == "已过期，请重新搜索")

    # 卡片发不出去：关房
    b, ui, rooms, clock, edits, ws, msg = setup_search()

    async def fail_reply(content, view=None, allowed_mentions=None):
        raise discord.HTTPException(NS(status=403, reason="Forbidden"), "no perms")
    msg.reply = fail_reply

    async def go2():
        it, _ = make_pick_interaction(42)
        await ui.on_pick(it, token_of_saved)
    token_of_saved = list(ui.pending)[0]
    asyncio.run(go2())
    check("卡片发送失败就关房", rooms.active() == [] and ui.cards.all() == [])

    # 意外错误：搜索消息改成通用提示
    b, ui, rooms, clock, edits, ws, msg = setup_search(ws=FakeWatchServer(RuntimeError("boom")))
    tok = token_of(msg)

    async def go3():
        it, _ = make_pick_interaction(42)
        await ui.on_pick(it, tok)
    asyncio.run(go3())
    check("意外错误给通用提示", rooms.active() == [] and edits == [(555, 7001, "正在准备…", None),
                                                          (555, 7001, "出错了，请稍后再试", None)])

    # 回复的 allowed_mentions
    ui, rooms, _, _ = make_ui()
    b = make_bot(ui, FakeSources(yt()), FakeVodSearch([]), FakeWatchServer())
    msg = FakeMessage()
    asyncio.run(b._watch(msg, "@everyone"))
    check("没找到不 ping", msg.replies[0][2] is not None and msg.replies[0][2].everyone is False)
    b = make_bot(ui, FakeSources(yt()), FakeVodSearch([show()]), FakeWatchServer())
    msg = FakeMessage()
    msg.author.mention = "<@1>"

    async def fail_menu(content, view=None, allowed_mentions=None):
        raise discord.HTTPException(NS(status=403, reason="Forbidden"), "no perms")
    msg.reply = fail_menu
    asyncio.run(b._watch(msg, "绝命毒师"))
    check("选单发不出去就不登记", ui.pending == {})


def test_pick_timeout():
    ui, rooms, clock, edits = make_ui()
    b = make_bot(ui, FakeSources(yt()), FakeVodSearch([show()]), FakeWatchServer())
    ui.create_vod_room = b._create_vod_room
    msg = FakeMessage(uid=42)
    msg.author.mention = "<@42>"

    async def go():
        await b._watch(msg, "绝命毒师")
        tok = token_of(msg)
        before = tok in ui.pending
        await drain(ui)
        return tok, before
    tok, before = asyncio.run(go())
    check("2 分钟后选单过期", before and edits == [(555, 7001, "选择已过期", None)] and tok not in ui.pending)
    check("过期 120 秒", wui.PICK_TIMEOUT == 120.0 and clock.t == 1000.0 + 120.0)


def test_pick_select_item():
    sel = wui.PickSelect("abcdefgh1234", [discord.SelectOption(label="a", value="0")])
    check("PickSelect 模板", sel.item.custom_id == "watch:pick:abcdefgh1234"
          and re.fullmatch(wui.PICK_TEMPLATE, sel.item.custom_id))

    async def go():
        item = await wui.PickSelect.from_custom_id(None, sel.item, re.fullmatch(wui.PICK_TEMPLATE, sel.item.custom_id))
        return item
    item = asyncio.run(go())
    check("from_custom_id 还原 token", item.token == "abcdefgh1234")
    it, sent = make_pick_interaction(1)
    wui.PickSelect.handler = None
    asyncio.run(item.callback(it))
    check("没 handler 提示没启用", sent.content == "一起看功能没有启用。")
    ui, *_ = make_ui()
    ui.pending = {}
    wui.PickSelect.handler = ui
    it, sent = make_pick_interaction(1)
    asyncio.run(item.callback(it))
    check("回调交给 handler", sent.content == "已过期，请重新搜索")
    wui.PickSelect.handler = None


def test_watch_link_unchanged():
    ui, rooms, _, _ = make_ui()
    src = FakeSources(yt())
    b = make_bot(ui, src, FakeVodSearch([show()]), FakeWatchServer())
    msg = FakeMessage()
    asyncio.run(b._watch(msg, "https://youtu.be/dQw4w9WgXcQ"))
    check("链接仍走 sources.resolve", src.calls == ["https://youtu.be/dQw4w9WgXcQ"] and len(rooms.active()) == 1
          and b.vod_search.calls == [])


if __name__ == "__main__":
    test_parse_and_route()
    test_card_text()
    test_build_view()
    test_watch_command()
    test_card_store_corrupt()
    test_host_button()
    test_throttle_and_close()
    test_edit_failure_logged()
    test_stale_cards_on_ready()
    test_bot_edit_and_setup_hook()
    test_watch_search_errors()
    test_watch_search_posts_menu()
    test_pick_flow()
    test_pick_preparing_shown_before_prepare()
    test_pick_resume()
    test_pick_twice_unknown_unplayable()
    test_pick_failures()
    test_pick_timeout()
    test_pick_select_item()
    test_watch_link_unchanged()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
