"""一起看房间存盘：进程重启（部署）后把房间原样接回来，正在看的人不用重新建房。

存什么：房间本身（视频、播放位置、版本号、房主密钥、成员的身份和权限）。
不存什么：连接、B 站地址 / m3u8 / 密钥 / 直播流这些缓存——它们本来就是按需再取的。

- RoomStore.flush()：把所有房间写进一个 JSON 文件。内容没变就不写，但有房间时至少每
  HEARTBEAT_SECONDS 写一次，好让 saved_at 表示「进程最后活着的时间」。网络层每个 tick 调一次，退出前再调一次。
- RoomStore.restore()：启动时读回来。停机超过 MAX_DOWNTIME_SECONDS 的存盘整个不要
  （那么久没人能看，房间早该按空房关掉了）；单个房间读不出来只跳过那一个。

播放位置不用特别处理：Playback 记的是「某一时刻在某个位置」，重启后按现在的时间照样算得出来。
网页端在停机期间继续放（片段直接来自资源站 CDN），连回来时位置对得上，不会被拉回去。

成员：重启前在线的人给 RESTORE_GRACE_SECONDS 的宽限，这段时间内连回来还是原来的身份（名字、控制权、房主）；
文件里有 viewer_id 和房主密钥（都是凭据），所以只放在 STATE_DIR 里，不进日志。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from tv_channels import Candidate, Channel
from vod_search import Episode, Line, Show
from watch_progress import Progress
from watch_rooms import (EMPTY_TTL_SECONDS, GRACE_SECONDS, Member, Playback, Room, RoomManager, TvInfo, Video,
                         VodInfo)

log = logging.getLogger("mrmeeseeks.watch")

FORMAT = 1
HEARTBEAT_SECONDS = 60
MAX_DOWNTIME_SECONDS = EMPTY_TTL_SECONDS + HEARTBEAT_SECONDS
RESTORE_GRACE_SECONDS = 120


# ---------------- 房间 → dict ----------------
def _dump_show(show: Show) -> dict:
    return {"key": show.key, "title": show.title, "year": show.year, "type_name": show.type_name, "pic": show.pic,
            "lines": [{"name": l.name, "episodes": [[e.label, e.url] for e in l.episodes]} for l in show.lines]}


def _dump_candidate(c: Candidate) -> dict:
    return {"url": c.url, "ua": c.ua, "list_index": c.list_index, "is_4k": c.is_4k, "ok": c.ok,
            "checked_at": c.checked_at}


def _dump_video(v: Video) -> dict:
    d = {"source": v.source, "id": v.id, "page": v.page, "title": v.title, "duration": v.duration, "url": v.url}
    if v.vod is not None:
        p = v.vod.resume
        d["vod"] = {"show": _dump_show(v.vod.show), "episode": v.vod.episode, "line": v.vod.line,
                    "resume": None if p is None else {"title": p.title, "episode": p.episode, "position": p.position,
                                                      "updated_at": p.updated_at, "label": p.label}}
    if v.tv is not None:
        d["tv"] = {"key": v.tv.channel.key, "name": v.tv.channel.name,
                   "candidates": [_dump_candidate(c) for c in v.tv.channel.candidates],
                   "order": [_dump_candidate(c) for c in v.tv.order],
                   "index": v.tv.index, "bad": sorted(v.tv.bad)}
    return d


def dump_room(room: Room) -> dict:
    return {
        "room_id": room.room_id, "channel_id": room.channel_id, "creator_id": room.creator_id,
        "creator_name": room.creator_name, "owner_key": room.owner_key, "key_used": room.key_used,
        "owner_viewer_id": room.owner_viewer_id, "host_id": room.host_id, "message_id": room.message_id,
        "version": room.version, "finished_version": room.finished_version, "created_at": room.created_at,
        "empty_since": room.empty_since,
        "playback": {"playing": room.playback.playing, "position": room.playback.position, "at": room.playback.at},
        "video": _dump_video(room.video),
        "members": [{"viewer_id": m.viewer_id, "name": m.name, "joined_at": m.joined_at,
                     "can_control": m.can_control, "public_id": m.public_id, "online": m.connections > 0,
                     "gone_since": m.gone_since} for m in room.members.values()],
    }


# ---------------- dict → 房间 ----------------
def _opt_float(v) -> float | None:
    return None if v is None else float(v)


def _load_show(d: dict) -> Show:
    lines = [Line(str(l["name"]), [Episode(str(label), str(url)) for label, url in l["episodes"]])
             for l in d["lines"]]
    return Show(key=str(d["key"]), title=str(d["title"]), year=None if d.get("year") is None else int(d["year"]),
                type_name=str(d.get("type_name") or ""), lines=lines, pic=d.get("pic") or None)


def _load_candidate(d: dict) -> Candidate:
    return Candidate(url=str(d["url"]), ua=d.get("ua") or None, list_index=int(d["list_index"]),
                     is_4k=bool(d["is_4k"]), ok=d.get("ok"), checked_at=_opt_float(d.get("checked_at")))


def _load_video(d: dict) -> Video:
    vod = tv = None
    if d.get("vod") is not None:
        v = d["vod"]
        show = _load_show(v["show"])
        episode, line = int(v["episode"]), int(v["line"])
        if not (0 <= line < len(show.lines) and 0 <= episode < len(show.lines[line].episodes)):
            raise ValueError("vod episode out of range")
        r = v.get("resume")
        resume = None if r is None else Progress(str(r["title"]), int(r["episode"]), float(r["position"]),
                                                 float(r["updated_at"]), str(r.get("label") or ""))
        vod = VodInfo(show, episode, line, resume)
    if d.get("tv") is not None:
        t = d["tv"]
        order = [_load_candidate(c) for c in t["order"]]
        index = int(t["index"])
        if not 0 <= index < len(order):
            raise ValueError("tv source out of range")
        channel = Channel(str(t["key"]), str(t["name"]), [_load_candidate(c) for c in t["candidates"]])
        tv = TvInfo(channel, order, index, {int(i) for i in t.get("bad", [])})
    source = str(d["source"])
    if (source == "vod") != (vod is not None) or (source == "tv") != (tv is not None):
        raise ValueError("video source does not match its details")
    return Video(source=source, id=str(d["id"]), page=int(d["page"]), title=str(d["title"]),
                 duration=_opt_float(d.get("duration")), url=str(d["url"]), vod=vod, tv=tv)


def load_room(d: dict, clock, now: float) -> Room:
    """now 是恢复的时刻：重启前在线的人从这时起有 RESTORE_GRACE_SECONDS 可以连回来。"""
    pb = d["playback"]
    room = Room(
        room_id=str(d["room_id"]), channel_id=int(d["channel_id"]), creator_id=int(d["creator_id"]),
        creator_name=str(d["creator_name"]), owner_key=str(d["owner_key"]), video=_load_video(d["video"]),
        playback=Playback(bool(pb["playing"]), float(pb["position"]), float(pb["at"])),
        key_used=bool(d.get("key_used")), message_id=None if d.get("message_id") is None else int(d["message_id"]),
        version=int(d.get("version", 0)),
        finished_version=None if d.get("finished_version") is None else int(d["finished_version"]),
        owner_viewer_id=d.get("owner_viewer_id") or None, created_at=float(d.get("created_at") or now), now=clock)
    if not room.room_id or not room.owner_key:
        raise ValueError("room without id or owner key")
    for m in d.get("members", []):
        if m.get("online"):
            # present() 按 gone_since + GRACE_SECONDS 算，所以往后挪，凑成总共 RESTORE_GRACE_SECONDS
            gone = now + RESTORE_GRACE_SECONDS - GRACE_SECONDS
        else:
            gone = _opt_float(m.get("gone_since"))
            gone = now - GRACE_SECONDS if gone is None else gone
        member = Member(viewer_id=str(m["viewer_id"]), name=str(m["name"]), joined_at=float(m["joined_at"]),
                        can_control=bool(m.get("can_control")), connections=0, gone_since=gone)
        if m.get("public_id"):
            member.public_id = str(m["public_id"])
        room.members[member.viewer_id] = member
    host = d.get("host_id")
    room.host_id = host if host in room.members else None
    if room.present():
        room.empty_since = None
    else:
        saved = _opt_float(d.get("empty_since"))
        room.empty_since = now if saved is None else saved
    return room


# ---------------- 文件 ----------------
class RoomStore:
    """path=None 时什么都不存（测试、没配 STATE_DIR）。"""

    def __init__(self, path: Path | None, rooms: RoomManager):
        self.path = Path(path) if path is not None else None
        self.rooms = rooms
        self._written: str | None = None      # 上次写进文件的房间列表（不含 saved_at）
        self._written_at = float("-inf")

    def restore(self) -> int:
        """把存盘的房间放回 RoomManager，返回接回来的个数。任何读不出来的情况都只是少恢复，不抛异常。"""
        if self.path is None:
            return 0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            saved_at, items = float(data["saved_at"]), list(data["rooms"])
            if data.get("format") != FORMAT:
                raise ValueError(f"unknown format {data.get('format')!r}")
        except FileNotFoundError:
            return 0
        except (OSError, ValueError, TypeError, KeyError):
            log.warning("Could not read %s; starting with no watch rooms", self.path)
            return 0
        now = self.rooms.clock()
        down = now - saved_at
        if items and not 0 <= down <= MAX_DOWNTIME_SECONDS:
            log.info("watch rooms: %d saved room(s) dropped, the server was down for %.0f s (limit %d s)",
                     len(items), down, MAX_DOWNTIME_SECONDS)
            return 0
        restored = 0
        for item in items:
            try:
                room = load_room(item, self.rooms.clock, now)
                if not self.rooms.adopt(room):
                    continue
            except Exception:  # noqa: BLE001  一个房间坏了不影响别的
                log.exception("watch rooms: could not restore a saved room")
                continue
            restored += 1
        if items:
            log.info("watch rooms: restored %d of %d saved room(s) after %.0f s down", restored, len(items), down)
        return restored

    def flush(self, force: bool = False) -> None:
        """有变化就写；有房间时至少每 HEARTBEAT_SECONDS 写一次。写不了只记日志。"""
        if self.path is None:
            return
        rooms = json.dumps([dump_room(r) for r in self.rooms.active()], ensure_ascii=False)
        now = self.rooms.clock()
        changed = rooms != self._written
        beat = rooms != "[]" and now - self._written_at >= HEARTBEAT_SECONDS
        if not (changed or beat or (force and rooms != "[]")):
            return
        if self._written is None and rooms == "[]" and not self.path.exists():
            self._written = rooms      # 从来没有过房间：不用为了写一个空列表建文件
            return
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f'{{"format": {FORMAT}, "saved_at": {now!r}, "rooms": {rooms}}}')
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write watch rooms to %s", self.path)
            return
        self._written, self._written_at = rooms, now
