"""音乐分享测试：PKCE / 登录链接、接口返回解析、授权回调、token 刷新、换歌判定、状态文件、轮询循环。

运行：python tests/test_music.py
不联网，不需要 Discord（music_share 模块不依赖 discord.py；请求函数用假的）。
"""
import asyncio
import base64
import hashlib
import json
import os
import stat
import sys
import tempfile
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import music_share as ms  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


CFG = ms.SpotifyConfig("abc123", "https://example.com/spotify/callback")


def playing(track_id, name="Song", playing=True, kind="track", artists=("A", "B")):
    return {"currently_playing_type": kind, "is_playing": playing, "progress_ms": 1234,
            "item": {"id": track_id, "name": name, "duration_ms": 200000,
                     "artists": [{"name": a} for a in artists],
                     "album": {"name": "Album", "images": [{"url": "https://i.scdn.co/big.jpg"}, {"url": "small"}]},
                     "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"}}}


class FakeSpotify:
    """假的 Spotify：记录请求，按脚本返回。"""

    def __init__(self):
        self.calls = []
        self.now = {}            # access token -> 「正在播放」返回（dict）、None（204）或 int 状态码
        self.tokens = {"tok1": "user1"}
        self.refresh_ok = True
        self.me_status = 200

    async def __call__(self, method, url, headers=None, data=None):
        self.calls.append((method, url, headers or {}, data or {}))
        if url == ms.TOKEN_URL:
            if data["grant_type"] == "authorization_code":
                if data["code"] != "good":
                    return 400, {}, {"error": "invalid_grant", "error_description": "Invalid authorization code"}
                return 200, {}, {"access_token": "tok1", "refresh_token": "ref1", "expires_in": 3600}
            if not self.refresh_ok:
                return 400, {}, {"error": "invalid_grant", "error_description": "Refresh token revoked"}
            n = len([c for c in self.calls if c[3].get("grant_type") == "refresh_token"])
            return 200, {}, {"access_token": f"tok{n + 1}", "refresh_token": f"ref{n + 1}", "expires_in": 3600}
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        if url == ms.ME_URL:
            if self.me_status != 200:
                return self.me_status, {}, {"error": {"status": self.me_status}}
            return 200, {}, {"id": "user1", "display_name": "Alice"}
        if url == ms.NOW_PLAYING_URL:
            if token not in self.now:
                return 401, {}, {"error": {"status": 401, "message": "The access token expired"}}
            v = self.now[token]
            if v is None:
                return 204, {}, None
            if isinstance(v, int):
                return v, {"retry-after": "3"} if v == 429 else {}, {"error": {"status": v}}
            return 200, {}, v
        raise AssertionError(url)


# ---------------- OAuth 工具 ----------------
def test_oauth_helpers():
    verifier, challenge = ms.make_pkce()
    expect = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    check("PKCE challenge 是 verifier 的 S256", challenge == expect and len(verifier) >= 43)
    url = ms.auth_url(CFG, "st4te", challenge)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    check("登录链接指向 accounts.spotify.com 且带齐参数",
          url.startswith(ms.AUTH_URL + "?") and q["client_id"] == ["abc123"] and q["response_type"] == ["code"]
          and q["redirect_uri"] == [CFG.redirect_uri] and q["scope"] == [ms.SCOPES] and q["state"] == ["st4te"]
          and q["code_challenge_method"] == ["S256"] and q["code_challenge"] == [challenge])
    check("回调路径取自 redirect_uri", CFG.callback_path == "/spotify/callback"
          and ms.SpotifyConfig("x", "https://h.example").callback_path == "/")

    t = ms.parse_track(playing("t1", "Hello"))
    check("解析曲目：名字、歌手、专辑、大图、链接、时长、播放中",
          t.id == "t1" and t.name == "Hello" and t.artist == "A, B" and t.album == "Album"
          and t.image == "https://i.scdn.co/big.jpg" and t.url == "https://open.spotify.com/track/t1"
          and t.duration_ms == 200000 and t.progress_ms == 1234 and t.is_playing)
    check("暂停时 is_playing=False", not ms.parse_track(playing("t1", playing=False)).is_playing)
    check("广告 / 播客 / 空返回 -> None", ms.parse_track(playing("t1", kind="ad")) is None
          and ms.parse_track(playing("t1", kind="episode")) is None and ms.parse_track(None) is None
          and ms.parse_track({"currently_playing_type": "track", "item": None}) is None)
    check("HTML 转义", "&lt;b&gt;" in ms._page("t", ms._esc("<b>")) and "<title>t</title>" in ms._page("t", "x"))


# ---------------- 存储 ----------------
def test_store():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "music.json")
        store = ms.MusicStore(path)
        store.load()
        check("文件不存在 -> 空", store.channel_id is None and store.listeners == {})
        store.set_channel(42)
        store.add(ms.Listener(1, "alice#1", "user1", "Alice", "ref1", "tok1", time.time() + 100, last_track="t1"))
        raw = json.load(open(path, encoding="utf-8"))
        check("落盘：频道和监听者，键是 discord id 字符串", raw["channel_id"] == 42 and raw["listeners"]["1"]["spotify_name"] == "Alice")
        if os.name != "nt":
            check("文件权限 0600", stat.S_IMODE(os.stat(path).st_mode) == 0o600)
        again = ms.MusicStore(path)
        again.load()
        check("重新加载一致", again.channel_id == 42 and again.listeners[1].refresh_token == "ref1"
              and again.listeners[1].last_track == "t1")
        check("删除", again.remove(1).spotify_name == "Alice" and again.remove(1) is None
              and json.load(open(path, encoding="utf-8"))["listeners"] == {})
        with open(path, "w") as f:
            f.write("{broken")
        broken = ms.MusicStore(path)
        broken.load()
        check("文件损坏时用空配置、不抛错", broken.listeners == {} and broken.channel_id is None)
        with open(path, "w") as f:
            json.dump({"channel_id": None, "listeners": {"1": {"discord_id": 1, "discord_name": "a", "spotify_id": "u",
                                                                "spotify_name": "A", "refresh_token": "r", "bogus": 1}}}, f)
        extra = ms.MusicStore(path)
        extra.load()
        check("多余字段忽略（向后兼容）", extra.listeners[1].spotify_name == "A")
    mem = ms.MusicStore(None)
    mem.set_channel(1)
    check("path=None 只存内存", mem.channel_id == 1)


# ---------------- 授权 + 轮询 ----------------
def test_flow():
    sp = FakeSpotify()
    store = ms.MusicStore(None)
    share = ms.MusicShare(store, ms.SpotifyClient(CFG, fetch=sp), interval=0.01)
    check("轮询间隔不低于下限", share.interval == ms.MIN_POLL_INTERVAL)
    share.interval = 0.01

    async def go():
        url1 = share.begin(1, "alice#1")
        url2 = share.begin(1, "alice#1")
        state2 = urllib.parse.parse_qs(urllib.parse.urlsplit(url2).query)["state"][0]
        state1 = urllib.parse.parse_qs(urllib.parse.urlsplit(url1).query)["state"][0]
        check("同一个人再发起一次：旧链接作废，只剩新的", list(share.pending) == [state2] and state1 != state2)

        status, html = await share.callback(f"state={state1}&code=good")
        check("用作废的 state 回调 -> 400 + 过期提示", status == 400 and "expired" in html)
        status, html = await share.callback(f"state={state2}&error=access_denied")
        check("用户取消授权 -> 400 + cancelled，state 用掉", status == 400 and "cancelled" in html and share.pending == {})

        state = urllib.parse.parse_qs(urllib.parse.urlsplit(share.begin(1, "alice#1")).query)["state"][0]
        status, html = await share.callback(f"state={state}&code=bad")
        check("换 token 失败 -> 400 + Spotify 的错误说明", status == 400 and "Invalid authorization code" in html)

        sp.now["tok1"] = playing("t0", "Baseline")
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(share.begin(1, "alice#1")).query)["state"][0]
        status, html = await share.callback(f"state={state}&code=good")
        lst = store.listeners.get(1)
        check("授权成功 -> 200，记下 Spotify 账号和 token", status == 200 and "Alice" in html and lst is not None
              and lst.spotify_name == "Alice" and lst.refresh_token == "ref1" and lst.access_token == "tok1")
        exchange = [c for c in sp.calls if c[3].get("grant_type") == "authorization_code"][-1]
        check("换 token 带 PKCE verifier 和 redirect_uri、没有 secret 时不带 Basic 认证",
              exchange[3]["code_verifier"] and exchange[3]["redirect_uri"] == CFG.redirect_uri
              and exchange[3]["client_id"] == "abc123" and "Authorization" not in exchange[2])
        check("授权时正在放的歌作为基线", lst.last_track == "t0" and share.last_tracks[1].name == "Baseline")

        notified = []

        async def notify(ev):
            notified.append((ev.listener.discord_id, ev.track.id))

        share.notify = notify
        check("同一首歌不重复分享", await share.poll_once() == [])
        sp.now["tok1"] = playing("t1", "New")
        ev = await share.poll_once()
        check("换歌 -> 事件", [(e.listener.discord_id, e.track.id) for e in ev] == [(1, "t1")] and lst.last_track == "t1")
        sp.now["tok1"] = playing("t1", "New", playing=False)
        check("暂停不算", await share.poll_once() == [])
        sp.now["tok1"] = playing("t1", "New")
        check("继续放同一首不重复", await share.poll_once() == [])
        sp.now["tok1"] = None
        check("没在放（204）不算，基线保留", await share.poll_once() == [] and lst.last_track == "t1")
        sp.now["tok1"] = playing("t2", kind="episode")
        check("播客不分享", await share.poll_once() == [])
        sp.now["tok1"] = playing("t1", "New")
        check("放完别的再放回同一首也不算（基线没变）", await share.poll_once() == [])

        # access token 过期：401 -> 刷新 -> 重试，refresh token 轮换
        del sp.now["tok1"]
        sp.now["tok2"] = playing("t3")
        ev = await share.poll_once()
        check("401 后刷新 token 重试成功", [e.track.id for e in ev] == ["t3"] and lst.access_token == "tok2"
              and lst.refresh_token == "ref2")
        # 到期前提前刷新
        lst.expires_at = time.time() + 10
        sp.now["tok3"] = playing("t3")
        await share.poll_once()
        check("快到期时主动刷新", lst.access_token == "tok3")

        # 限流：这一轮跳过，不崩
        sp.now["tok3"] = 429
        t0 = time.time()
        check("429 -> 等 Retry-After 后跳过本轮", await share.poll_once() == [] and "rate limit" in share.last_error
              and time.time() - t0 >= 2.5)
        sp.now["tok3"] = 500
        check("其它错误记 last_error、继续", await share.poll_once() == [] and "HTTP 500" in share.last_error)
        sp.now["tok3"] = playing("t4")
        check("恢复后 last_error 清空", [e.track.id for e in await share.poll_once()] == ["t4"] and share.last_error is None)

        # 403（移出白名单）：连续 MAX_FAILURES 轮后移除并回调
        gone = []

        async def on_disconnect(lst, reason):
            gone.append((lst.discord_id, reason))

        share.on_disconnect = on_disconnect
        sp.now["tok3"] = 403
        for _ in range(ms.MAX_FAILURES - 1):
            await share.poll_once()
        check("失败次数不够不移除", 1 in store.listeners and gone == [] and lst.failures == ms.MAX_FAILURES - 1)
        await share.poll_once()
        check("连续失败到上限 -> 移除 + on_disconnect", 1 not in store.listeners and len(gone) == 1
              and "allowlist" in gone[0][1])

        # refresh token 被撤销
        sp.now["tok1"] = playing("t0")
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(share.begin(2, "bob#2")).query)["state"][0]
        await share.callback(f"state={state}&code=good")
        store.listeners[2].access_token = ""
        sp.refresh_ok = False
        for _ in range(ms.MAX_FAILURES):
            await share.poll_once()
        check("refresh token 失效 -> 移除", 2 not in store.listeners and gone[-1][0] == 2 and "revoked" in gone[-1][1])

        # run() 循环
        sp.refresh_ok = True
        sp.now["tok1"] = playing("t0")
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(share.begin(3, "c#3")).query)["state"][0]
        await share.callback(f"state={state}&code=good")
        task = asyncio.create_task(share.run())
        sp.now["tok1"] = playing("t9")
        await asyncio.sleep(0.1)
        check("run 循环调用 notify", (3, "t9") in notified and not task.done())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        check("disconnect 清掉监听者和最近曲目", share.disconnect(3) is not None and 3 not in share.last_tracks
              and share.disconnect(3) is None)

        # client secret：带 Basic 认证
        secret_client = ms.SpotifyClient(ms.SpotifyConfig("id", CFG.redirect_uri, "sec"), fetch=sp)
        await secret_client.refresh("ref1")
        check("配了 client secret 时 token 请求带 Basic 认证",
              sp.calls[-1][2]["Authorization"] == "Basic " + base64.b64encode(b"id:sec").decode())

        # 网络错误 -> MusicError
        async def boom(*a, **k):
            raise ConnectionError("no network")
        try:
            await ms.SpotifyClient(CFG, fetch=boom).now_playing("x")
            check("网络错误 -> MusicError", False)
        except ms.MusicError as e:
            check("网络错误 -> MusicError", "ConnectionError" in str(e))

    asyncio.run(go())


if __name__ == "__main__":
    import logging
    logging.disable(logging.WARNING)
    test_oauth_helpers()
    test_store()
    test_flow()
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
