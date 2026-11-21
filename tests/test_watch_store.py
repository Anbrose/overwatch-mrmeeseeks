"""一起看房间存盘测试：存取往返、重启后成员的宽限、停机太久不恢复、坏文件、卡片保留。

运行：python tests/test_watch_store.py
不联网，不需要 Discord。
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

from tv_channels import Candidate, Channel  # noqa: E402
from vod_search import Episode, Line, Show  # noqa: E402
from watch_progress import Progress  # noqa: E402
from watch_rooms import (EMPTY_TTL_SECONDS, GRACE_SECONDS, RoomManager, TvInfo, Video, VodInfo,  # noqa: E402
                         tv_video, vod_video)
from watch_store import (HEARTBEAT_SECONDS, MAX_DOWNTIME_SECONDS, RESTORE_GRACE_SECONDS, RoomStore,  # noqa: E402
                         dump_room, load_room)
from watch_ui import CardStore, WatchUI  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def mkshow():
    def line(name, labels):
        return Line(name, [Episode(lb, f"https://{name}.example/{i}/index.m3u8") for i, lb in enumerate(labels)])
    return Show("后会友期|2026", "后会友期", 2026, "大陆综艺",
                [line("a", ["第20261029期上", "第20261029期下"]), line("b", ["20261029下", "20261029上"])],
                pic="https://img.example/p.jpg")


def mkvod():
    return vod_video(VodInfo(mkshow(), 1, 0, Progress("后会友期", 0, 99.0, 5.0, "第20261029期上")), 3000.0)


def mktv():
    cands = [Candidate("https://tv.example/a.m3u8", "ua", 0, False, True, 10.0),
             Candidate("https://tv.example/b.m3u8", None, 1, True)]
    return tv_video(TvInfo(Channel("CCTV1", "CCTV-1 综合", cands), [cands[1], cands[0]], 1, {0}))


def setup(path, now):
    m = RoomManager(clock=lambda: now[0])
    return m, RoomStore(path, m)


def test_roundtrip():
    now = [1000.0]
    m, _ = setup(None, now)
    r = m.create(7, 42, "小明", mkvod())
    r.message_id = 555
    host = m.connect(r.room_id, None, r.owner_key)
    guest = m.connect(r.room_id, None, None)
    gone = m.connect(r.room_id, None, None)
    m.set_permission(r.room_id, host.viewer_id, guest.public_id, True)
    m.control(r.room_id, host.viewer_id, "play", 120.0)
    now[0] += 10
    m.disconnect(r.room_id, gone.viewer_id)
    now[0] += 5
    saved = json.loads(json.dumps(dump_room(r)))      # 必须过得了 JSON
    back = load_room(saved, lambda: now[0], now[0])
    check("房间号、频道、发起人、卡片、密钥原样", (back.room_id, back.channel_id, back.creator_id, back.creator_name,
                                         back.message_id, back.owner_key, back.key_used, back.owner_viewer_id)
          == (r.room_id, 7, 42, "小明", 555, r.owner_key, True, host.viewer_id))
    check("版本号和建房时间原样", back.version == r.version and back.created_at == r.created_at)
    check("播放位置按现在的时间算得出来", back.playback.playing
          and abs(back.playback.position_at(now[0] + 20, back.video.duration) - 155.0) < 1e-6)
    v = back.video
    check("vod：片子、第几段、线路、续看快照原样", v.source == "vod" and v.title == r.video.title and v.url == r.video.url
          and v.duration == 3000.0 and v.vod.episode == 1 and v.vod.line == 0 and v.vod.show == r.video.vod.show
          and v.vod.resume == r.video.vod.resume)
    bh, bg, bx = back.members[host.viewer_id], back.members[guest.viewer_id], back.members[gone.viewer_id]
    check("成员的名字、公开 id、控制权原样", (bh.name, bh.public_id, bg.name, bg.public_id, bg.can_control)
          == (host.name, host.public_id, guest.name, guest.public_id, True) and bh.connections == 0)
    check("房主还是原来的房主", back.host_id == host.viewer_id and back.host_name() == "小明")
    check("重启前在线的人算在场，没连接", [x.viewer_id for x in back.present()] == [host.viewer_id, guest.viewer_id, gone.viewer_id][:3]
          or {x.viewer_id for x in back.present()} >= {host.viewer_id, guest.viewer_id})
    check("有人要回来的房间不算空房", back.empty_since is None)

    tv = m.create(8, 43, "小红", mktv())
    tb = load_room(json.loads(json.dumps(dump_room(tv))), lambda: now[0], now[0])
    t = tb.video.tv
    check("直播：频道、换源顺序、当前源、坏源原样", tb.video.source == "tv" and tb.video.duration is None
          and t.channel.key == "CCTV1" and len(t.channel.candidates) == 2 and t.index == 1 and t.bad == {0}
          and [c.url for c in t.order] == ["https://tv.example/b.m3u8", "https://tv.example/a.m3u8"]
          and t.order[1].ok is True and tb.video.url == tv.video.url)
    check("没人的房间保留空房计时", tb.empty_since == tv.empty_since)

    yt = m.create(9, 44, "小刚", Video("youtube", "abc", 1, "视频", None, "https://youtu.be/abc"))
    yb = load_room(json.loads(json.dumps(dump_room(yt))), lambda: now[0], now[0])
    check("普通视频（没有 vod / tv）", yb.video == yt.video)


def test_restore_grace():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "rooms.json"
        now = [1000.0]
        m, s = setup(path, now)
        r = m.create(7, 42, "小明", mkvod())
        host = m.connect(r.room_id, None, r.owner_key)
        guest = m.connect(r.room_id, None, None)
        s.flush()
        now[0] += 25                                  # 停机 25 秒
        m2, s2 = setup(path, now)
        check("恢复一个房间", s2.restore() == 1 and m2.get(r.room_id) is not None)
        r2 = m2.get(r.room_id)
        now[0] += 40                                  # 普通断线宽限（30 秒）已过
        m2.tick()
        check("重启后的宽限比普通断线长", GRACE_SECONDS < 40 < RESTORE_GRACE_SECONDS
              and set(r2.members) == {host.viewer_id, guest.viewer_id} and r2.host_id == host.viewer_id)
        again = m2.connect(r.room_id, host.viewer_id, None)
        check("连回来还是原来的身份和房主", again.viewer_id == host.viewer_id and again.name == host.name
              and r2.host_id == host.viewer_id and again.connections == 1 and again.gone_since is None)
        now[0] += RESTORE_GRACE_SECONDS
        m2.tick()
        check("没回来的人过了宽限就走了", set(r2.members) == {host.viewer_id})
        m2.disconnect(r.room_id, host.viewer_id)
        now[0] += GRACE_SECONDS + EMPTY_TTL_SECONDS + 1
        m2.tick()
        m2.tick()
        check("之后照常按空房关闭", m2.get(r.room_id) is None)
        check("房间号已在用就不接", m.adopt(r) is False)


def test_downtime_and_bad_files():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "sub" / "rooms.json"
        now = [1000.0]
        m, s = setup(path, now)
        s.flush()
        check("从来没有房间：不建文件", not path.exists())
        r = m.create(7, 42, "小明", mkvod())
        m.connect(r.room_id, None, None)
        s.flush()
        first = path.read_text(encoding="utf-8")
        check("有房间就写文件", json.loads(first)["rooms"][0]["room_id"] == r.room_id)
        now[0] += HEARTBEAT_SECONDS - 1
        s.flush()
        check("没变化、没到心跳时间不重写", path.read_text(encoding="utf-8") == first)
        now[0] += 2
        s.flush()
        check("到了心跳时间更新 saved_at", json.loads(path.read_text(encoding="utf-8"))["saved_at"] == now[0])

        now[0] += MAX_DOWNTIME_SECONDS + 1
        m2, s2 = setup(path, now)
        check("停机太久的存盘不恢复", s2.restore() == 0 and not m2.active())
        now[0] -= 2
        m3, s3 = setup(path, now)
        check("刚好在期限内就恢复", s3.restore() == 1)

        data = json.loads(path.read_text(encoding="utf-8"))
        good = data["rooms"][0]
        bad = json.loads(json.dumps(good))
        bad["room_id"] = "zzzzzz"
        bad["video"]["vod"]["episode"] = 99
        data["rooms"] = [bad, {"room_id": "x"}, good]
        path.write_text(json.dumps(data), encoding="utf-8")
        m4, s4 = setup(path, now)
        check("坏的房间跳过，好的照常恢复", s4.restore() == 1 and [x.room_id for x in m4.active()] == [r.room_id])

        for text in ("{not json", "[]", json.dumps({"format": 99, "saved_at": now[0], "rooms": []})):
            path.write_text(text, encoding="utf-8")
            m5, s5 = setup(path, now)
            check(f"读不懂的文件当作没有房间（{text[:12]}）", s5.restore() == 0 and not m5.active())
        m6, s6 = setup(Path(d) / "missing.json", now)
        check("没有文件", s6.restore() == 0)
        check("path=None 什么都不做", RoomStore(None, m6).restore() == 0 and RoomStore(None, m6).flush() is None)

        m.close(r.room_id, "test")
        s.flush()
        check("房间都关了：文件里是空列表", json.loads(path.read_text(encoding="utf-8"))["rooms"] == [])


def test_cards_kept():
    async def run():
        now = [1000.0]
        m = RoomManager(clock=lambda: now[0])
        ui = WatchUI(m, "https://watch.example", CardStore(None))
        edits = []

        async def edit(channel_id, message_id, text, view):
            edits.append((channel_id, message_id, text))
        ui.edit = edit
        r = m.create(7, 42, "小明", mkvod())
        r.message_id = 111
        ui.cards.add(7, 111)      # 接回来的房间
        ui.cards.add(7, 222)      # 重启前就没了的房间
        await ui.end_stale()
        check("接回来的房间卡片留着，别的改成已结束", [e[:2] for e in edits] == [(7, 222)] and "已结束" in edits[0][2]
              and ui.cards.all() == [(7, 111)])
    asyncio.run(run())


if __name__ == "__main__":
    test_roundtrip()
    test_restore_grace()
    test_downtime_and_bad_files()
    test_cards_kept()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)
