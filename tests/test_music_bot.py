"""音乐分享的 Discord 部分：share music / music here 指令和权限、连接按钮、换歌消息、授权失效私信，
以及 WebSocket 端口上的 HTTP 回调路由（假的 Discord 对象，不联网）。

运行：python tests/test_music_bot.py
"""
import asyncio
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import discord  # noqa: E402

import bot  # noqa: E402
import music_share as ms  # noqa: E402
from ws_server import WSServer  # noqa: E402

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
        if member is BOT_MEMBER:
            return NS(manage_channels=False, send_messages=self.bot_can_send)
        return NS(manage_channels=self.manage, send_messages=True)

    async def send(self, text=None, allowed_mentions=None, embed=None, view=None):
        self.sent.append(NS(text=text, allowed_mentions=allowed_mentions, embed=embed, view=view))


class FakeMessage:
    def __init__(self, manage=True, guild=True, channel_id=555, user_id=1):
        self.author = NS(id=user_id, __str__=lambda s: "alice#1")
        self.guild = NS(id=9, me=BOT_MEMBER) if guild else None
        self.channel = FakeChannel(channel_id, manage)
        self.replies = []

    async def reply(self, content, view=None, allowed_mentions=None):
        self.replies.append(NS(text=content, view=view))
        return NS(id=1)

    @property
    def last(self):
        return self.replies[-1].text


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content=None, file=None, view=None, ephemeral=False):
        self.sent.append(NS(content=content, file=file, view=view, ephemeral=ephemeral))


def fake_interaction(user_id):
    return NS(user=NS(id=user_id, __str__=lambda s: f"user{user_id}"), response=FakeResponse())


class FakeSpotify:
    def __init__(self):
        self.now = {}

    async def __call__(self, method, url, headers=None, data=None):
        if url == ms.TOKEN_URL:
            return 200, {}, {"access_token": "tok", "refresh_token": "ref", "expires_in": 3600}
        if url == ms.ME_URL:
            return 200, {}, {"id": "u", "display_name": "Alice"}
        return 204, {}, None


def make_bot(share):
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    b.music = share
    return b


def run(method, msg, arg):
    asyncio.run(method(msg, arg))
    return msg.last


def test_commands():
    check("share 指令路由", bot.parse_command("share music") == ("share", "music")
          and bot.parse_command("Share Music OFF") == ("share", "Music OFF"))
    check("music 指令路由", bot.parse_command("music here") == ("music", "here"))
    check("帮助里列出 share music 和 music here", "share music" in bot.HELP_TEXT and "music here" in bot.HELP_TEXT)

    off = make_bot(None)
    check("未启用时提示", "not enabled" in run(off._share, FakeMessage(), "music")
          and "not enabled" in run(off._music, FakeMessage(), "here"))

    with tempfile.TemporaryDirectory() as d:
        store = ms.MusicStore(os.path.join(d, "music.json"))
        share = ms.MusicShare(store, ms.SpotifyClient(ms.SpotifyConfig("abcdefghij", "https://x.example/cb"), fetch=FakeSpotify()))
        b = make_bot(share)

        # ---- music here/off/status ----
        check("没有管理频道权限不能设置", "Manage Channels" in run(b._music, FakeMessage(manage=False), "here")
              and store.channel_id is None)
        check("私信里不能设置", "Manage Channels" in run(b._music, FakeMessage(guild=False), "here"))
        check("设置音乐频道并持久化", run(b._music, FakeMessage(channel_id=777), "here").startswith("✅")
              and store.channel_id == 777 and _reload(store.path).channel_id == 777)
        st = run(b._music, FakeMessage(manage=False), "status")
        check("status 显示频道、app 和人数", "<#777>" in st and "abcdefgh…" in st and "0 linked" in st)
        check("未知参数显示用法", run(b._music, FakeMessage(), "bogus").startswith("Usage"))

        # ---- share music ----
        check("share 后面不是 music 显示用法", run(b._share, FakeMessage(), "video").startswith("Usage")
              and run(b._share, FakeMessage(), "").startswith("Usage"))
        check("没连接时 status", "isn't linked" in run(b._share, FakeMessage(), "music status"))
        check("没连接时 off", "isn't linked" in run(b._share, FakeMessage(), "music off"))
        m = FakeMessage()
        run(b._share, m, "music")
        view = m.replies[-1].view
        check("share music -> 带按钮的回复，提到音乐频道", isinstance(view, bot.SpotifyConnectView) and view.owner_id == 1
              and "<#777>" in m.last and view.message is not None)

        # 别人点按钮：拒绝；本人点：只给本人看的二维码 + 登录链接
        other = fake_interaction(99)
        asyncio.run(view.connect.callback(other))
        check("别人点按钮被拒绝（ephemeral）", other.response.sent[0].ephemeral and "yourself" in other.response.sent[0].content
              and share.pending == {})
        me = fake_interaction(1)
        asyncio.run(view.connect.callback(me))
        sent = me.response.sent[0]
        link = [c for c in sent.view.children if isinstance(c, discord.ui.Button)][0]
        check("本人点按钮：ephemeral、二维码 PNG、登录链接按钮、生成了待授权记录",
              sent.ephemeral and sent.file.filename == "spotify-login.png"
              and sent.file.fp.read(8) == b"\x89PNG\r\n\x1a\n"
              and link.url.startswith(ms.AUTH_URL) and len(share.pending) == 1
              and next(iter(share.pending.values())).discord_id == 1)

        # 走完回调后 status / off
        state = next(iter(share.pending))
        status, _ = asyncio.run(share.callback(f"state={state}&code=ok"))
        check("回调成功后记下监听者", status == 200 and store.listeners[1].spotify_name == "Alice")
        check("已连接时 status", "Alice" in run(b._share, FakeMessage(), "music status"))
        m = FakeMessage()
        run(b._share, m, "music")
        check("已连接时再 share music：提示已连接，仍给按钮", "already linked" in m.last and m.replies[-1].view is not None)
        st = run(b._music, FakeMessage(manage=False), "status")
        check("music status 列出已连接的人", "<@1>" in st and "Alice" in st)
        check("share music off 解除", "Unlinked" in run(b._share, FakeMessage(), "music off") and store.listeners == {})

        # ---- 换歌消息 ----
        channel = FakeChannel(777, True)
        waited = []

        async def wait_until_ready():
            waited.append(True)

        async def _channel(channel_id):
            channel.asked = channel_id
            return channel

        b.wait_until_ready, b._channel = wait_until_ready, _channel
        lst = ms.Listener(1, "alice#1", "u", "Alice", "ref")
        track = ms.Track("t1", "Hello", ["Adele"], album="25", image="https://i.scdn.co/x.jpg",
                         url="https://open.spotify.com/track/t1", duration_ms=295000, is_playing=True)
        asyncio.run(b._music_notify(ms.NowPlaying(lst, track)))
        post = channel.sent[0]
        button = post.view.children[0]
        check("换歌：发到音乐频道，提到人和歌，嵌入封面，播放按钮指向 Spotify，不 ping",
              channel.asked == 777 and "<@1> is listening to **Hello** by Adele" in post.text
              and post.embed.title == "Hello" and post.embed.thumbnail.url == track.image
              and post.embed.footer.text == "4:55" and button.url == track.url and button.label == "Play on Spotify"
              and post.allowed_mentions.users is False and waited == [True])
        store.set_channel(None)
        asyncio.run(b._music_notify(ms.NowPlaying(lst, track)))
        check("没设频道时不发", len(channel.sent) == 1)

        # ---- 授权失效私信 ----
        dms = []

        class User:
            async def send(self, text):
                dms.append(text)

        b.get_user = lambda uid: User()
        asyncio.run(b._music_disconnected(lst, "revoked"))
        check("授权失效后私信本人", len(dms) == 1 and "revoked" in dms[0] and "share music" in dms[0])


def test_http_route():
    seen = []

    async def handler(query):
        seen.append(query)
        return 200, "<b>ok</b>"

    server = WSServer(registry=None, events=None, host="127.0.0.1", port=0, http_routes={"/spotify/callback": handler})
    conn = NS(respond=lambda status, text: NS(status_code=int(status), body=text.encode()))
    ws_headers = {"Upgrade": "websocket"}

    def req(path, headers=None):
        h = headers or {}
        return NS(path=path, headers=NS(get=lambda k, d="": h.get(k, d)))

    r = asyncio.run(server.process_request(conn, req("/spotify/callback?code=x&state=y")))
    check("注册的路径交给处理函数，query 原样传入，返回 HTML",
          seen == ["code=x&state=y"] and r.status_code == 200 and r.body == b"<b>ok</b>"
          and r.headers["Content-Type"].startswith("text/html"))
    r = asyncio.run(server.process_request(conn, req("/other")))
    check("其它普通 HTTP 请求 -> 404", r.status_code == 404)
    check("WebSocket 握手不受影响（返回 None）", asyncio.run(server.process_request(conn, req("/", ws_headers))) is None)

    async def broken(query):
        raise RuntimeError("x")

    server.http_routes["/bad"] = broken
    r = asyncio.run(server.process_request(conn, req("/bad")))
    check("处理函数抛错 -> 500", r.status_code == 500)


def _reload(path):
    s = ms.MusicStore(path)
    s.load()
    return s


if __name__ == "__main__":
    import logging
    logging.disable(logging.ERROR)
    test_commands()
    test_http_route()
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
