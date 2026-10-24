"""一起看的 Discord 接入：指令路由、房间卡片文字、「我是房主」按钮、卡片编辑节流、重启后的遗留卡片。
用假的 Discord 对象，不连 Discord、不联网。

运行：python tests/test_watch_bot.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import discord  # noqa: E402

import bot  # noqa: E402
import watch_ui as wui  # noqa: E402
from watch_rooms import RoomManager, Video  # noqa: E402
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


def make_bot(ui=None, sources=None):
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    b.watch_ui, b.watch_sources = ui, sources
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
    check("帮助里有一起看", "`@mrmeeseeks 一起看 <Bilibili or YouTube link>` (or `watch`)" in bot.HELP_TEXT)


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
        check("无链接给用法", msg.replies[0][0].startswith("用法：") and "一起看 <B站或YouTube链接>" in msg.replies[0][0]
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
    asyncio.run(b.setup_hook())
    check("启动时注册房主按钮并设置 handler", added == [wui.HostButton] and wui.HostButton.handler is ui)
    wui.HostButton.handler = None


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
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
