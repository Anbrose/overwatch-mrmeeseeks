"""开播通知：手动登记的直播源（目前是 B 站直播间）按赛区发到对应频道，轮询直播间状态，开播时通知。

赛区 -> 频道的映射在 regions.py（和赛事推送共用，用 `@mrmeeseeks region cn` 绑定）。
这个模块不依赖 discord.py，也不依赖第三方 HTTP 库（标准库 urllib 跑在线程里），
所以 tests/test_live.py 可以离线、不装 discord 跑。Discord 指令和发消息在 bot.py 里。

直播源存在 STATE_DIR/live_sources.json：
{"sources": {"bilibili:7734200": {...Source 字段...}}}   按 platform:room_id 去重
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Iterable

log = logging.getLogger("mrmeeseeks.live")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ROOM_INFO_URL = "https://api.live.bilibili.com/xlive/web-room/v1/index/getRoomBaseInfo"
ROOM_INIT_URL = "https://api.live.bilibili.com/room/v1/Room/room_init"
BATCH_SIZE = 20                 # 一次请求查多少个房间
DEFAULT_POLL_INTERVAL = 30.0    # 秒；B 站对匿名请求比较敏感，不要低于 15
MIN_POLL_INTERVAL = 15.0


class LiveError(Exception):
    """参数或网络问题，消息可直接展示给用户。"""


# ---------------- 数据结构 ----------------
@dataclass
class Source:
    platform: str          # 目前只有 "bilibili"
    room_id: int           # 真实房间号（短号已解析）
    region: str            # 赛区，小写，如 cn / asia / emea / na
    label: str             # 显示名：用户给的，或主播昵称
    url: str
    added_by: str = ""
    added_at: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.room_id}"


@dataclass
class RoomStatus:
    room_id: int
    live: bool
    title: str = ""
    uname: str = ""
    cover: str = ""
    url: str = ""
    area: str = ""
    live_time: str = ""


@dataclass
class LiveEvent:
    kind: str              # "live" 或 "offline"
    source: Source
    status: RoomStatus


# ---------------- 持久化 ----------------
class LiveStore:
    """path 为 None 时只存在内存里（测试用）。写文件失败只记日志，内存里照常生效。"""

    def __init__(self, path: str | os.PathLike | None):
        self.path = str(path) if path is not None else None
        self.sources: dict[str, Source] = {}

    def load(self) -> None:
        if self.path is None:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            sources = {}
            for item in (raw.get("sources") or {}).values():
                src = Source(**item)
                sources[src.key] = src
            self.sources = sources
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as e:
            log.warning("No usable live sources at %s (%r)", self.path, e)

    def save(self) -> None:
        if self.path is None:
            return
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"sources": {k: asdict(s) for k, s in self.sources.items()}}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write live sources to %s", self.path)

    def add(self, source: Source) -> None:
        self.sources[source.key] = source
        self.save()

    def remove(self, key: str) -> Source | None:
        src = self.sources.pop(key, None)
        if src is not None:
            self.save()
        return src

    def by_region(self) -> dict[str, list[Source]]:
        out: dict[str, list[Source]] = {}
        for s in sorted(self.sources.values(), key=lambda s: (s.region, s.added_at)):
            out.setdefault(s.region, []).append(s)
        return out


# ---------------- B 站接口 ----------------
_BILI_URL_RE = re.compile(r"(?:https?://)?(?:live\.bilibili\.com|live\.b23\.tv)/(?:h5/)?(\d+)", re.I)


def parse_bilibili_target(text: str) -> int | None:
    """从 `https://live.bilibili.com/7734200?x=y`、`live.bilibili.com/6` 或纯数字里取房间号（可能是短号）。"""
    text = text.strip().strip("<>")
    if text.isdigit():
        return int(text)
    m = _BILI_URL_RE.search(text)
    return int(m.group(1)) if m else None


def parse_room_info(payload: dict[str, Any]) -> dict[int, RoomStatus]:
    """解析 getRoomBaseInfo 的返回。live_status: 0 未开播, 1 直播中, 2 轮播（算未开播）。"""
    if payload.get("code") != 0:
        raise LiveError(f"Bilibili API error {payload.get('code')}: {payload.get('message') or payload.get('msg')}")
    rooms = (payload.get("data") or {}).get("by_room_ids") or {}
    out: dict[int, RoomStatus] = {}
    for item in rooms.values():
        rid = int(item["room_id"])
        out[rid] = RoomStatus(
            room_id=rid,
            live=item.get("live_status") == 1,
            title=item.get("title") or "",
            uname=item.get("uname") or "",
            cover=item.get("cover") or "",
            url=item.get("live_url") or f"https://live.bilibili.com/{rid}",
            area=" / ".join(x for x in (item.get("parent_area_name"), item.get("area_name")) if x),
            live_time=item.get("live_time") or "",
        )
    return out


def _http_get_json(url: str, params: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    full = url + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT, "Referer": "https://live.bilibili.com/"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LiveError(f"Bilibili returned HTTP {e.code} (rate limited?)") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise LiveError(f"Could not reach Bilibili: {e}") from e


class BilibiliClient:
    """两个接口都不需要登录。fetch 可注入，便于测试。"""

    def __init__(self, fetch: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None):
        self._fetch = fetch or _http_get_json

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._fetch, url, params)

    async def resolve_room(self, room_or_short_id: int) -> int:
        """短号 -> 真实房间号；房间不存在时抛 LiveError。"""
        payload = await self._get(ROOM_INIT_URL, {"id": room_or_short_id})
        if payload.get("code") != 0:
            raise LiveError(f"Bilibili room {room_or_short_id} not found ({payload.get('message') or payload.get('msg')}).")
        return int(payload["data"]["room_id"])

    async def statuses(self, room_ids: Iterable[int]) -> dict[int, RoomStatus]:
        ids = sorted(set(int(r) for r in room_ids))
        out: dict[int, RoomStatus] = {}
        for i in range(0, len(ids), BATCH_SIZE):
            chunk = ids[i:i + BATCH_SIZE]
            payload = await self._get(ROOM_INFO_URL, {"room_ids": chunk, "req_biz": "web_room_componet"})
            out.update(parse_room_info(payload))
        return out


# ---------------- 轮询与状态变化 ----------------
def detect_transitions(previous: dict[str, bool], sources: dict[str, Source],
                       statuses: dict[int, RoomStatus]) -> tuple[dict[str, bool], list[LiveEvent]]:
    """纯函数：根据上一轮的 live 状态和这一轮的查询结果，算出新状态和事件。

    第一次看到某个源时只记录基线，不发事件（否则 bot 一重启就把正在直播的全报一遍）。
    查询结果里没有的房间保持上一轮状态。
    """
    new_state = dict(previous)
    events: list[LiveEvent] = []
    for key, src in sources.items():
        st = statuses.get(src.room_id)
        if st is None:
            continue
        before = previous.get(key)
        new_state[key] = st.live
        if before is None:
            continue
        if not before and st.live:
            events.append(LiveEvent("live", src, st))
        elif before and not st.live:
            events.append(LiveEvent("offline", src, st))
    for key in list(new_state):
        if key not in sources:
            del new_state[key]
    return new_state, events


class LiveWatcher:
    """每 interval 秒查一遍所有直播源，状态变化时调用 notify(event)。notify 由 bot 提供（按赛区找频道发消息）。"""

    def __init__(self, store: LiveStore, client: BilibiliClient,
                 notify: Callable[[LiveEvent], Awaitable[None]] | None = None,
                 interval: float = DEFAULT_POLL_INTERVAL):
        self.store = store
        self.client = client
        self.notify = notify
        self.interval = max(MIN_POLL_INTERVAL, interval)
        self.state: dict[str, bool] = {}
        self.last_statuses: dict[int, RoomStatus] = {}
        self.last_error: str | None = None

    def forget(self, key: str) -> None:
        self.state.pop(key, None)

    async def poll_once(self) -> list[LiveEvent]:
        sources = dict(self.store.sources)
        if not sources:
            self.state = {}
            return []
        statuses = await self.client.statuses(s.room_id for s in sources.values())
        self.last_statuses.update(statuses)
        self.state, events = detect_transitions(self.state, sources, statuses)
        return events

    async def run(self) -> None:
        log.info("Live watcher started: %d source(s), every %.0fs", len(self.store.sources), self.interval)
        while True:
            try:
                events = await self.poll_once()
                self.last_error = None
            except LiveError as e:
                self.last_error = str(e)
                log.warning("Live poll failed: %s", e)
                events = []
            except Exception:
                log.exception("Live poll crashed")
                events = []
            for ev in events:
                if self.notify is None:
                    continue
                try:
                    await self.notify(ev)
                except Exception:
                    log.exception("Failed to notify for %s", ev.source.key)
            await asyncio.sleep(self.interval)
