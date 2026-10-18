"""音乐分享：成员把 Spotify 账号授权给 bot，之后每换一首歌，bot 在音乐频道发「xx 正在听 xx」和播放按钮。

流程：
  1. `@mrmeeseeks share music` -> bot 回一个只有本人能点的按钮 -> 点了弹出只给本人看的登录二维码 / 链接
  2. 成员在 Spotify 授权（Authorization Code + PKCE），Spotify 把浏览器跳回 SPOTIFY_REDIRECT_URI
  3. 回调由 ws_server 在 WebSocket 同一个端口上接（见 WSServer.http_routes），`MusicShare.complete` 换 token、存起来
  4. `MusicShare.run` 每 MUSIC_POLL_INTERVAL 秒问一次每个人的「正在播放」，换歌了就交给 notify

这个模块不依赖 discord.py；HTTP 用 aiohttp（discord.py 的依赖），请求函数可注入，tests/test_music.py 离线跑。
Discord 指令、按钮和发消息在 bot.py 里。

状态存在 STATE_DIR/music.json（含 refresh token，文件权限 0600）：
{"channel_id": 123, "listeners": {"<discord 用户 id>": {...Listener 字段...}}}

Spotify 的限制（2026）：开发模式的应用最多 5 个白名单用户（开发者后台 -> Settings -> User Management 按邮箱加），
应用所有者要有 Premium；回调地址必须是 HTTPS（本机回环地址除外）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import tempfile
import time
import urllib.parse
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Awaitable, Callable

log = logging.getLogger("mrmeeseeks.music")

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"
NOW_PLAYING_URL = API_BASE + "/me/player/currently-playing"
ME_URL = API_BASE + "/me"
SCOPES = "user-read-currently-playing"
DEFAULT_POLL_INTERVAL = 20.0   # 秒；每个人每轮一个请求，5 个人也远在限额之内
MIN_POLL_INTERVAL = 10.0
PENDING_TTL = 10 * 60          # 二维码 / 登录链接的有效期（秒）
TOKEN_MARGIN = 60              # access token 还剩不到这么多秒就提前刷新
MAX_FAILURES = 3               # 连续多少轮 token 刷不出来才断开

Fetch = Callable[..., Awaitable[tuple[int, dict[str, str], Any]]]


class MusicError(Exception):
    """参数、授权或网络问题，消息可直接展示给用户。"""


class SpotifyAuthError(MusicError):
    """token 失效 / 被撤销 / 用户不在白名单：这个人要重新授权。"""


# ---------------- 数据结构 ----------------
@dataclass
class Listener:
    discord_id: int
    discord_name: str
    spotify_id: str
    spotify_name: str
    refresh_token: str
    access_token: str = ""
    expires_at: float = 0.0
    connected_at: float = field(default_factory=time.time)
    last_track: str = ""       # 上次分享过的曲目 id，换歌才分享
    failures: int = 0


@dataclass
class Track:
    id: str
    name: str
    artists: list[str]
    album: str = ""
    image: str = ""            # 封面
    url: str = ""              # open.spotify.com 链接，播放按钮用
    duration_ms: int = 0
    progress_ms: int = 0
    is_playing: bool = False

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)


@dataclass
class NowPlaying:
    listener: Listener
    track: Track


@dataclass
class Pending:
    discord_id: int
    discord_name: str
    verifier: str
    created_at: float = field(default_factory=time.time)


@dataclass
class SpotifyConfig:
    client_id: str
    redirect_uri: str
    client_secret: str = ""    # PKCE 不需要；填了就一并用 Basic 认证

    @property
    def callback_path(self) -> str:
        return urllib.parse.urlsplit(self.redirect_uri).path or "/"


# ---------------- 持久化 ----------------
class MusicStore:
    """path 为 None 时只存在内存里（测试用）。写文件失败只记日志，内存里照常生效。"""

    def __init__(self, path: str | os.PathLike | None):
        self.path = str(path) if path is not None else None
        self.channel_id: int | None = None
        self.listeners: dict[int, Listener] = {}

    def load(self) -> None:
        if self.path is None:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            known = {f.name for f in fields(Listener)}
            listeners = {}
            for item in (raw.get("listeners") or {}).values():
                lst = Listener(**{k: v for k, v in item.items() if k in known})
                listeners[lst.discord_id] = lst
            self.listeners = listeners
            self.channel_id = int(raw["channel_id"]) if raw.get("channel_id") else None
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError) as e:
            log.warning("No usable music state at %s (%r)", self.path, e)

    def save(self) -> None:
        if self.path is None:
            return
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"channel_id": self.channel_id,
                           "listeners": {str(k): asdict(v) for k, v in self.listeners.items()}},
                          f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)      # 里面有 refresh token
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write music state to %s", self.path)

    def set_channel(self, channel_id: int | None) -> None:
        self.channel_id = channel_id
        self.save()

    def add(self, listener: Listener) -> None:
        self.listeners[listener.discord_id] = listener
        self.save()

    def remove(self, discord_id: int) -> Listener | None:
        lst = self.listeners.pop(discord_id, None)
        if lst is not None:
            self.save()
        return lst


# ---------------- OAuth 工具 ----------------
def make_pkce() -> tuple[str, str]:
    """-> (code_verifier, code_challenge)。"""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def auth_url(cfg: SpotifyConfig, state: str, challenge: str) -> str:
    params = {"client_id": cfg.client_id, "response_type": "code", "redirect_uri": cfg.redirect_uri,
              "scope": SCOPES, "state": state, "code_challenge_method": "S256", "code_challenge": challenge}
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def parse_track(payload: dict | None) -> Track | None:
    """「正在播放」接口的返回 -> Track；没在放、放的是广告 / 播客、或者字段缺失 -> None。"""
    if not payload or payload.get("currently_playing_type", "track") != "track":
        return None
    item = payload.get("item") or {}
    if not item.get("id"):
        return None
    album = item.get("album") or {}
    images = album.get("images") or []
    return Track(id=str(item["id"]), name=str(item.get("name") or "?"),
                 artists=[str(a.get("name") or "?") for a in (item.get("artists") or [])],
                 album=str(album.get("name") or ""), image=str(images[0].get("url") or "") if images else "",
                 url=str((item.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/track/{item['id']}"),
                 duration_ms=int(item.get("duration_ms") or 0), progress_ms=int(payload.get("progress_ms") or 0),
                 is_playing=bool(payload.get("is_playing")))


# ---------------- Spotify 接口 ----------------
async def _aiohttp_fetch(method: str, url: str, headers: dict[str, str] | None = None,
                         data: dict[str, str] | None = None) -> tuple[int, dict[str, str], Any]:
    """默认的请求函数：-> (状态码, 响应头, JSON 或 None)。"""
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, headers=headers, data=data) as resp:
            body = None
            if resp.status != 204:
                try:
                    body = await resp.json(content_type=None)
                except (ValueError, aiohttp.ClientError):
                    body = None
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body


class SpotifyClient:
    def __init__(self, cfg: SpotifyConfig, fetch: Fetch | None = None):
        self.cfg = cfg
        self._fetch = fetch or _aiohttp_fetch

    async def _token_request(self, form: dict[str, str]) -> dict:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        form = dict(form, client_id=self.cfg.client_id)
        if self.cfg.client_secret:
            basic = base64.b64encode(f"{self.cfg.client_id}:{self.cfg.client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {basic}"
        try:
            status, _, body = await self._fetch("POST", TOKEN_URL, headers=headers, data=form)
        except Exception as e:   # aiohttp.ClientError、超时等
            raise MusicError(f"Could not reach Spotify ({e.__class__.__name__}).") from e
        if status != 200 or not isinstance(body, dict) or "access_token" not in body:
            err = (body or {}).get("error") if isinstance(body, dict) else None
            desc = (body or {}).get("error_description") if isinstance(body, dict) else None
            if err in ("invalid_grant", "invalid_client") or status in (400, 401):
                raise SpotifyAuthError(f"Spotify rejected the authorization ({desc or err or status}).")
            raise MusicError(f"Spotify token request failed (HTTP {status}: {desc or err or 'unknown'}).")
        return body

    async def exchange(self, code: str, verifier: str) -> dict:
        return await self._token_request({"grant_type": "authorization_code", "code": code,
                                          "redirect_uri": self.cfg.redirect_uri, "code_verifier": verifier})

    async def refresh(self, refresh_token: str) -> dict:
        return await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def _get(self, url: str, access_token: str) -> tuple[int, dict[str, str], Any]:
        try:
            return await self._fetch("GET", url, headers={"Authorization": f"Bearer {access_token}"})
        except Exception as e:
            raise MusicError(f"Could not reach Spotify ({e.__class__.__name__}).") from e

    async def me(self, access_token: str) -> dict:
        status, _, body = await self._get(ME_URL, access_token)
        if status in (401, 403):
            raise SpotifyAuthError("Spotify refused the account lookup "
                                   "(is this Spotify account on the app's user allowlist?).")
        if status != 200 or not isinstance(body, dict):
            raise MusicError(f"Spotify profile request failed (HTTP {status}).")
        return body

    async def now_playing(self, access_token: str) -> Track | None:
        """-> 正在放的曲目；没在放 / 不是歌 -> None。401/403 抛 SpotifyAuthError，429 抛 MusicError（带等待秒数）。"""
        status, headers, body = await self._get(NOW_PLAYING_URL, access_token)
        if status == 204:
            return None
        if status == 401:
            raise SpotifyAuthError("access token expired")
        if status == 403:
            raise SpotifyAuthError("Spotify refused the request (account removed from the allowlist, "
                                   "or the app lost access).")
        if status == 429:
            raise RateLimited(float(headers.get("retry-after") or 5))
        if status != 200:
            raise MusicError(f"Spotify now-playing request failed (HTTP {status}).")
        return parse_track(body if isinstance(body, dict) else None)


class RateLimited(MusicError):
    def __init__(self, retry_after: float):
        super().__init__(f"Spotify rate limit; retry after {retry_after:.0f}s")
        self.retry_after = retry_after


# ---------------- 编排 ----------------
class MusicShare:
    """授权流程 + 轮询。notify(NowPlaying) 换歌时调用；on_disconnect(Listener, reason) 授权失效被移除时调用。"""

    def __init__(self, store: MusicStore, client: SpotifyClient,
                 notify: Callable[[NowPlaying], Awaitable[None]] | None = None,
                 on_disconnect: Callable[[Listener, str], Awaitable[None]] | None = None,
                 interval: float = DEFAULT_POLL_INTERVAL):
        self.store = store
        self.client = client
        self.notify = notify
        self.on_disconnect = on_disconnect
        self.interval = max(float(interval), MIN_POLL_INTERVAL)
        self.pending: dict[str, Pending] = {}    # state -> 等待回调的授权
        self.last_error: str | None = None
        self.last_tracks: dict[int, Track] = {}  # discord_id -> 最近一次看到在放的歌（给 status 用）

    @property
    def cfg(self) -> SpotifyConfig:
        return self.client.cfg

    # ----- 授权 -----
    def begin(self, discord_id: int, discord_name: str) -> str:
        """生成登录链接；同一个人再点一次会作废上一个链接。"""
        self._prune_pending()
        for state, p in list(self.pending.items()):
            if p.discord_id == discord_id:
                del self.pending[state]
        verifier, challenge = make_pkce()
        state = secrets.token_urlsafe(24)
        self.pending[state] = Pending(discord_id, discord_name, verifier)
        return auth_url(self.cfg, state, challenge)

    def _prune_pending(self) -> None:
        cutoff = time.time() - PENDING_TTL
        for state in [s for s, p in self.pending.items() if p.created_at < cutoff]:
            del self.pending[state]

    async def complete(self, state: str, code: str | None, error: str | None = None) -> Listener:
        """Spotify 跳回来后：校验 state、换 token、查账号、保存。失败抛 MusicError。"""
        self._prune_pending()
        pending = self.pending.pop(state or "", None)
        if pending is None:
            raise MusicError("This login link has expired or was already used. "
                             "Run `@mrmeeseeks share music` again to get a new one.")
        if error or not code:
            raise MusicError("Spotify login was cancelled." if error == "access_denied"
                             else f"Spotify returned an error: {error or 'no code'}.")
        tokens = await self.client.exchange(code, pending.verifier)
        access = tokens["access_token"]
        profile = await self.client.me(access)
        listener = Listener(discord_id=pending.discord_id, discord_name=pending.discord_name,
                            spotify_id=str(profile.get("id") or ""),
                            spotify_name=str(profile.get("display_name") or profile.get("id") or "Spotify user"),
                            refresh_token=tokens.get("refresh_token") or "", access_token=access,
                            expires_at=time.time() + float(tokens.get("expires_in") or 3600))
        # 正在放的歌作为基线：授权完不补发，下一首才分享
        try:
            track = await self.client.now_playing(access)
        except MusicError:
            track = None
        if track is not None:
            self.last_tracks[listener.discord_id] = track
            if track.is_playing:
                listener.last_track = track.id
        self.store.add(listener)
        log.info("Spotify connected: %s -> %s", listener.discord_name, listener.spotify_name)
        return listener

    def disconnect(self, discord_id: int) -> Listener | None:
        self.last_tracks.pop(discord_id, None)
        return self.store.remove(discord_id)

    # ----- 轮询 -----
    async def _access_token(self, lst: Listener) -> str:
        if lst.access_token and time.time() < lst.expires_at - TOKEN_MARGIN:
            return lst.access_token
        tokens = await self.client.refresh(lst.refresh_token)
        lst.access_token = tokens["access_token"]
        lst.expires_at = time.time() + float(tokens.get("expires_in") or 3600)
        if tokens.get("refresh_token"):      # PKCE 每次刷新会换新的 refresh token
            lst.refresh_token = tokens["refresh_token"]
        self.store.save()
        return lst.access_token

    async def _check(self, lst: Listener) -> NowPlaying | None:
        try:
            track = await self.client.now_playing(await self._access_token(lst))
        except SpotifyAuthError as e:
            if "expired" in str(e) and lst.access_token:   # 过期了就刷新重试一次
                lst.access_token = ""
                track = await self.client.now_playing(await self._access_token(lst))
            else:
                raise
        if track is None:
            return None
        self.last_tracks[lst.discord_id] = track
        if not track.is_playing or track.id == lst.last_track:
            return None
        lst.last_track = track.id
        return NowPlaying(lst, track)

    async def poll_once(self) -> list[NowPlaying]:
        events: list[NowPlaying] = []
        dirty = False
        for lst in list(self.store.listeners.values()):
            try:
                ev = await self._check(lst)
            except RateLimited as e:
                self.last_error = str(e)
                log.warning("%s; pausing this round", e)
                await asyncio.sleep(min(e.retry_after, 120))
                break
            except SpotifyAuthError as e:
                lst.failures += 1
                dirty = True
                log.warning("Spotify auth failed for %s (%d/%d): %s", lst.discord_name, lst.failures, MAX_FAILURES, e)
                if lst.failures >= MAX_FAILURES:
                    self.disconnect(lst.discord_id)
                    if self.on_disconnect:
                        try:
                            await self.on_disconnect(lst, str(e))
                        except Exception:
                            log.exception("on_disconnect failed")
                continue
            except MusicError as e:
                self.last_error = str(e)
                log.warning("Now-playing check failed for %s: %s", lst.discord_name, e)
                continue
            if lst.failures:
                lst.failures = 0
                dirty = True
            if ev is not None:
                events.append(ev)
                dirty = True
            self.last_error = None
        if dirty:
            self.store.save()
        return events

    async def run(self) -> None:
        log.info("Music share started: %d listener(s), every %.0fs", len(self.store.listeners), self.interval)
        while True:
            try:
                for ev in await self.poll_once():
                    if self.notify:
                        try:
                            await self.notify(ev)
                        except Exception:
                            log.exception("Could not share %s for %s", ev.track.name, ev.listener.discord_name)
            except Exception:
                log.exception("Music poll failed")
            await asyncio.sleep(self.interval)

    # ----- 回调页 -----
    async def callback(self, query: str) -> tuple[int, str]:
        """浏览器从 Spotify 跳回来：query 是 `?` 后面的部分。-> (HTTP 状态码, HTML)。"""
        params = urllib.parse.parse_qs(query)
        state = (params.get("state") or [""])[0]
        code = (params.get("code") or [None])[0]
        error = (params.get("error") or [None])[0]
        try:
            lst = await self.complete(state, code, error)
        except MusicError as e:
            return 400, _page("Spotify login failed", str(e))
        return 200, _page("Spotify connected",
                          f"<b>{_esc(lst.discord_name)}</b> is now linked to Spotify account "
                          f"<b>{_esc(lst.spotify_name)}</b>. You can close this tab; "
                          "the next song you play will be shared in Discord.")


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _page(title: str, body: str) -> str:
    return ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            f"<title>{_esc(title)}</title><style>body{{font-family:system-ui,sans-serif;max-width:32em;margin:4em auto;"
            "padding:0 1em;line-height:1.5}h1{font-size:1.4em}</style></head>"
            f"<body><h1>{_esc(title)}</h1><p>{body}</p></body></html>")
