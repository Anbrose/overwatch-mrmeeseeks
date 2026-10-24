"""一起看按频道记录的观看进度（只记按片名搜索的 vod 房间），用于下次续看。

存储形状：{"<channel_id>": {"<show key>": {"title", "episode", "position", "updated_at"}}}
本模块不依赖 watch_ui（watch_ui 会反过来用它），所以自带时间格式化。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from watch_rooms import Room, RoomManager

log = logging.getLogger(__name__)

MIN_POSITION = 30.0       # 少于 30 秒不算看过
RECORD_INTERVAL = 30.0    # 播放中每隔这么久记一次
_TZ = ZoneInfo("Australia/Sydney")


@dataclass
class Progress:
    title: str
    episode: int       # 从 0 开始
    position: float
    updated_at: float


class ProgressStore:
    """path=None 时只在内存里。"""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path is not None else None
        self.data: dict[str, dict[str, Progress]] = self._load()

    def _load(self) -> dict[str, dict[str, Progress]]:
        if self.path is None:
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return {str(int(ch)): {str(k): Progress(str(v["title"]), int(v["episode"]),
                                                    float(v["position"]), float(v["updated_at"]))
                                   for k, v in shows.items()}
                    for ch, shows in raw.items()}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            log.warning("Could not read %s; starting with no watch progress", self.path)
            return {}

    def _save(self) -> None:
        if self.path is None:
            return
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({ch: {k: {"title": p.title, "episode": p.episode, "position": p.position,
                                    "updated_at": p.updated_at} for k, p in shows.items()}
                           for ch, shows in self.data.items()}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write watch progress to %s", self.path)

    def get(self, channel_id: int, key: str) -> Progress | None:
        return self.data.get(str(int(channel_id)), {}).get(key)

    def record(self, channel_id: int, key: str, p: Progress) -> bool:
        if not p.position >= MIN_POSITION:
            return False
        self.data.setdefault(str(int(channel_id)), {})[key] = p
        self._save()
        return True


class ProgressRecorder:
    """只在房间里有人时记录：没人后服务器上的播放时钟还在走（空房保留 10 分钟），
    那段时间没人在看，不能算进度。最后一个人的断线宽限期结束（rooms 发 "members"、房间变空）时记最后一次。"""

    def __init__(self, rooms: RoomManager, store: ProgressStore, interval: float = RECORD_INTERVAL):
        self.rooms = rooms
        self.store = store
        self.interval = interval
        self._last: dict[str, float] = {}   # room_id -> 上次记录时间
        self._empty: set[str] = set()       # 已经因为变空记过一次的房间
        rooms.listeners.append(self.on_event)

    def _record(self, room: Room) -> None:
        vod = room.video.vod
        if vod is None:
            return
        now = self.rooms.clock()
        self._last[room.room_id] = now
        pos = room.playback.position_at(now, room.video.duration)
        self.store.record(room.channel_id, vod.show.key, Progress(vod.show.title, vod.episode, pos, now))

    def on_event(self, room: Room, kind: str) -> None:
        if kind == "playback":
            self._record(room)
        elif kind == "members":
            if room.present():
                self._empty.discard(room.room_id)
            elif room.room_id not in self._empty:
                self._empty.add(room.room_id)
                self._record(room)      # 最多晚 GRACE_SECONDS 秒
        elif kind == "closed":
            if room.present():
                self._record(room)
            self._last.pop(room.room_id, None)
            self._empty.discard(room.room_id)

    def tick(self) -> None:
        now = self.rooms.clock()
        for room in self.rooms.active():
            if room.video.vod is None or not room.playback.playing or not room.present():
                continue
            last = self._last.get(room.room_id)
            if last is None or now - last >= self.interval:
                self._record(room)


def _fmt_time(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def resume_line(p: Progress, show_episode_count: int) -> str:
    d = datetime.fromtimestamp(p.updated_at, _TZ)
    ep = "" if show_episode_count == 1 else f"第 {p.episode + 1} 集 "
    return f"上次一起看到 {ep}{_fmt_time(p.position)}（{d.month}月{d.day}日）"
