"""一起看视频的房间状态（与 Discord、网络层无关，便于单独测试）。

规则：进房默认只能看；只有房主能开关别人的控制权、换视频；控制权 = 播放、暂停、拖进度条。
网络层通过 RoomManager.listeners 订阅状态变化（kind: members/playback/video/permissions/host/closed）。
"""
from __future__ import annotations

import logging
import math
import random
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

GRACE_SECONDS = 30        # 断线宽限（按 viewer_id，同一浏览器多个标签页算一个人）
EMPTY_TTL_SECONDS = 600   # 空房保留时间
_ROOM_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
_ROOM_ID_LEN = 6

_ADJECTIVES = ["快乐的", "安静的", "勇敢的", "好奇的", "迷糊的", "优雅的", "机灵的", "淡定的", "害羞的", "活泼的",
               "慵懒的", "热情的", "神秘的", "善良的", "调皮的", "敏捷的", "温柔的", "傲娇的", "可爱的", "沉稳的"]
_ANIMALS = ["企鹅", "海豚", "熊猫", "狐狸", "兔子", "刺猬", "松鼠", "水獭", "考拉", "猫头鹰",
            "柴犬", "鲸鱼", "海豹", "浣熊", "鸭子", "羊驼", "河马", "仓鼠", "蜜蜂", "章鱼"]


class RoomsFull(Exception):
    """同时在播 B 站的房间已达上限。"""

    def __init__(self, limit: int):
        super().__init__(f"B 站房间已满（上限 {limit}）")
        self.limit = limit


@dataclass
class Video:
    source: str          # "bilibili" | "youtube"
    id: str
    page: int
    title: str
    duration: float | None
    url: str


@dataclass
class Member:
    viewer_id: str
    name: str
    joined_at: float
    can_control: bool = False
    connections: int = 0
    gone_since: float | None = None


@dataclass
class Playback:
    playing: bool
    position: float
    at: float

    def position_at(self, now: float, duration: float | None) -> float:
        """播放中按时间推进，夹到 [0, duration]。"""
        pos = self.position + (max(0.0, now - self.at) if self.playing else 0.0)
        if duration is not None:
            pos = min(pos, duration)
        return max(0.0, pos)


@dataclass
class Room:
    room_id: str
    channel_id: int
    creator_id: int
    creator_name: str
    owner_key: str
    video: Video
    playback: Playback
    members: dict[str, Member] = field(default_factory=dict)
    host_id: str | None = None
    key_used: bool = False
    message_id: int | None = None
    empty_since: float | None = None
    closed: bool = False
    version: int = 0                      # 每次换视频 +1
    owner_viewer_id: str | None = None    # 用房主链接拿回房主身份的人
    now: Callable[[], float] = field(default=time.time, repr=False)

    def present(self) -> list[Member]:
        """在房间里的人：有连接，或断线还在宽限期内；按进房顺序。"""
        now = self.now()
        out = [m for m in self.members.values()
               if m.connections > 0 or (m.gone_since is not None and now - m.gone_since < GRACE_SECONDS)]
        return sorted(out, key=lambda m: m.joined_at)

    def viewer_count(self) -> int:
        return len(self.present())

    def host_name(self) -> str:
        """房主是用过房主链接的人 / 没房主 → 发起人名字；否则是房主的随机名字。"""
        host = self.members.get(self.host_id) if self.host_id else None
        if host is None or host.viewer_id == self.owner_viewer_id:
            return self.creator_name
        return host.name


def random_name(taken: set[str], rng: random.Random | None = None) -> str:
    """随机名字，如“快乐的企鹅”；taken 里已有的不重复，组合用完就加数字后缀。"""
    rng = rng or random
    free = [a + b for a in _ADJECTIVES for b in _ANIMALS if a + b not in taken]
    if free:
        return rng.choice(free)
    base = rng.choice(_ADJECTIVES) + rng.choice(_ANIMALS)
    n = 2
    while f"{base}{n}" in taken:
        n += 1
    return f"{base}{n}"


class RoomManager:
    def __init__(self, clock: Callable[[], float] = time.time, max_bili_rooms: int = 2):
        self.clock = clock
        self.max_bili_rooms = max_bili_rooms
        self.rooms: dict[str, Room] = {}
        self.listeners: list[Callable[[Room, str], None]] = []

    # ---------------- 内部 ----------------
    def _notify(self, room: Room, kind: str) -> None:
        for fn in list(self.listeners):
            try:
                fn(room, kind)
            except Exception:
                log.exception("watch room listener failed (kind=%s)", kind)

    def _bili_count(self, exclude: Room | None = None) -> int:
        return sum(1 for r in self.rooms.values()
                   if not r.closed and r is not exclude and r.video.source == "bilibili")

    def _check_bili(self, video: Video, exclude: Room | None = None) -> None:
        if video.source == "bilibili" and self._bili_count(exclude) >= self.max_bili_rooms:
            raise RoomsFull(self.max_bili_rooms)

    def _ensure_host(self, room: Room) -> bool:
        """房主不在房间里就交给最早进来的人；房间空了就没有房主。返回房主是否变了。"""
        present = room.present()
        old = room.host_id
        if not present:
            room.host_id = None
        elif room.host_id not in {m.viewer_id for m in present}:
            room.host_id = present[0].viewer_id
        return room.host_id != old

    def _is_host(self, room: Room, viewer_id: str) -> bool:
        return room.host_id is not None and room.host_id == viewer_id

    # ---------------- 对外 ----------------
    def create(self, channel_id, creator_id, creator_name, video: Video) -> Room:
        self._check_bili(video)
        now = self.clock()
        while True:
            room_id = "".join(secrets.choice(_ROOM_ALPHABET) for _ in range(_ROOM_ID_LEN))
            if room_id not in self.rooms:
                break
        room = Room(room_id=room_id, channel_id=channel_id, creator_id=creator_id, creator_name=creator_name,
                    owner_key=secrets.token_urlsafe(16), video=video,
                    playback=Playback(False, 0.0, now), empty_since=now, now=self.clock)
        self.rooms[room_id] = room
        return room

    def get(self, room_id: str) -> Room | None:
        room = self.rooms.get(room_id)
        return room if room and not room.closed else None

    def active(self) -> list[Room]:
        return [r for r in self.rooms.values() if not r.closed]

    def connect(self, room_id, viewer_id: str | None, key: str | None) -> Member:
        room = self.get(room_id)
        if room is None:
            raise KeyError(room_id)
        now = self.clock()
        member = room.members.get(viewer_id) if viewer_id else None
        if member is None:
            vid = secrets.token_urlsafe(12)
            name = random_name({m.name for m in room.members.values()})
            member = room.members[vid] = Member(vid, name, now)
        member.connections += 1
        member.gone_since = None
        room.empty_since = None
        if (key and not room.key_used and secrets.compare_digest(key, room.owner_key)):
            room.key_used = True
            room.owner_viewer_id = member.viewer_id
            room.host_id = member.viewer_id
        self._ensure_host(room)
        self._notify(room, "members")
        self._notify(room, "host")
        return member

    def disconnect(self, room_id, viewer_id) -> None:
        room = self.get(room_id)
        member = room.members.get(viewer_id) if room else None
        if member is None or member.connections <= 0:
            return
        member.connections -= 1
        if member.connections == 0:
            member.gone_since = self.clock()
        self._notify(room, "members")

    def control(self, room_id, viewer_id, action: str, position: float | None) -> bool:
        room = self.get(room_id)
        member = room.members.get(viewer_id) if room else None
        if member is None or not (member.can_control or self._is_host(room, viewer_id)):
            return False
        if position is not None and not (isinstance(position, (int, float)) and math.isfinite(position)):
            return False
        now = self.clock()
        pb = room.playback
        if action == "seek":
            if position is None:
                return False
            playing, pos = pb.playing, position
        elif action in ("play", "pause"):
            playing = action == "play"
            pos = position if position is not None else pb.position_at(now, room.video.duration)
        else:
            return False
        if room.video.duration is not None:
            pos = min(pos, room.video.duration)
        room.playback = Playback(playing, max(0.0, float(pos)), now)
        self._notify(room, "playback")
        return True

    def set_permission(self, room_id, actor_id, target_id, allowed: bool) -> bool:
        room = self.get(room_id)
        if room is None or not self._is_host(room, actor_id):
            return False
        target = room.members.get(target_id)
        if target is None:
            return False
        target.can_control = bool(allowed)
        self._notify(room, "permissions")
        return True

    def change_video(self, room_id, actor_id, video: Video) -> bool:
        room = self.get(room_id)
        if room is None or not self._is_host(room, actor_id):
            return False
        self._check_bili(video, exclude=room)
        room.video = video
        room.version += 1
        room.playback = Playback(False, 0.0, self.clock())
        self._notify(room, "video")
        return True

    def set_duration(self, room_id: str, value: float) -> bool:
        """播放器报告时长：只在视频还没有时长时接受（有限正数）。"""
        room = self.get(room_id)
        if room is None or room.video.duration is not None:
            return False
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            return False
        room.video.duration = float(value)
        self._notify(room, "video")
        return True

    def tick(self) -> None:
        now = self.clock()
        for room in list(self.rooms.values()):
            if room.closed:
                continue
            expired = [m for m in room.members.values()
                       if m.connections <= 0 and m.gone_since is not None and now - m.gone_since >= GRACE_SECONDS]
            for m in expired:
                del room.members[m.viewer_id]
            if expired:
                self._notify(room, "members")
            if self._ensure_host(room):
                self._notify(room, "host")
            if not room.present() and room.empty_since is None:
                # 从最后一个人宽限期结束的那一刻开始算
                room.empty_since = max((m.gone_since + GRACE_SECONDS for m in expired), default=now)
            if room.empty_since is not None and now - room.empty_since >= EMPTY_TTL_SECONDS:
                room.closed = True
                self._notify(room, "closed")
