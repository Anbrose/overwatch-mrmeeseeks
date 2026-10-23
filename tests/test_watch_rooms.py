"""一起看房间逻辑测试：权限、房主交接、断线宽限、空房关闭、B 站上限、播放进度。

运行：python tests/test_watch_rooms.py
不联网，不需要 Discord（watch_rooms 模块不依赖 discord.py）。
"""
import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import watch_rooms as wr  # noqa: E402
from watch_rooms import RoomManager, RoomsFull, Video  # noqa: E402

results = []
now = [1000.0]
events = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def clock():
    return now[0]


def yt(i):
    return Video("youtube", i, 1, "t", None, f"https://youtu.be/{i}")


def bili(i):
    return Video("bilibili", i, 1, "t", None, f"https://www.bilibili.com/video/{i}")


def mk(video=None):
    now[0] = 1000.0
    events.clear()
    m = RoomManager(clock)
    m.listeners.append(lambda room, kind: events.append((kind, room.room_id)))
    return m, m.create(1, 9, "小明", video or yt("x1"))


def raises(exc, fn):
    try:
        fn()
    except exc:
        return True
    return False


def test_defaults_and_permissions():
    m, r = mk()
    check("新房间暂停在 0", r.playback.playing is False and r.playback.position == 0)
    check("房间号 6 位", len(r.room_id) == 6 and all(c in wr._ROOM_ALPHABET for c in r.room_id))
    a = m.connect(r.room_id, None, None)
    check("第一个进来的人暂时当房主", r.host_id == a.viewer_id)
    check("没用房主链接时房主显示发起人以外的随机名", r.host_name() == a.name)
    b = m.connect(r.room_id, None, None)
    check("默认没有控制权", b.can_control is False and not m.control(r.room_id, b.viewer_id, "play", None))
    check("普通观众不能给权限", not m.set_permission(r.room_id, b.viewer_id, b.public_id, True))
    check("公开 id 和 viewer_id 不同", b.public_id and b.public_id != b.viewer_id and a.public_id != b.public_id)
    check("用 viewer_id 当目标无效", not m.set_permission(r.room_id, a.viewer_id, b.viewer_id, True) and not b.can_control)
    check("房主给权限", m.set_permission(r.room_id, a.viewer_id, b.public_id, True) and b.can_control)
    check("有权限能播放", m.control(r.room_id, b.viewer_id, "play", None) and r.playback.playing)
    check("有权限的人不能换视频", not m.change_video(r.room_id, b.viewer_id, yt("x2")))
    check("房主能换视频", m.change_video(r.room_id, a.viewer_id, yt("x2")))


def test_owner_key_claim_once():
    m, r = mk()
    a = m.connect(r.room_id, None, None)
    o = m.connect(r.room_id, None, r.owner_key)
    check("房主链接拿回房主", r.host_id == o.viewer_id and r.key_used)
    check("拿回房主后显示发起人名字", r.host_name() == "小明")
    m.disconnect(r.room_id, o.viewer_id)
    now[0] += 31
    m.tick()
    check("房主离开后交给最早进来的人", r.host_id == a.viewer_id)
    check("交接后显示随机名字", r.host_name() == a.name)
    o2 = m.connect(r.room_id, None, r.owner_key)
    check("用过的房主链接作废", r.host_id == a.viewer_id and o2.viewer_id != a.viewer_id)
    check("host 事件已通知", any(k == "host" for k, _ in events))


def test_untrusted_key():
    m, r = mk()
    a = m.connect(r.room_id, None, None)
    ok = True
    for bad in ("é", "\ud800", 123, b"x", "wrong-key"):
        try:
            x = m.connect(r.room_id, a.viewer_id, bad)
        except Exception:
            ok = False
            break
        ok = ok and x is a
    check("非法/错误房主链接不抛异常", ok)
    check("非法/错误房主链接不拿房主、链接未作废", r.host_id == a.viewer_id and not r.key_used and r.owner_viewer_id is None)
    check("连接数正确", a.connections == 6)
    check("bool 位置被拒绝", not m.control(r.room_id, a.viewer_id, "seek", True))


def test_grace_and_tabs():
    m, r = mk()
    a = m.connect(r.room_id, None, None)
    m.connect(r.room_id, a.viewer_id, None)
    m.disconnect(r.room_id, a.viewer_id)
    check("关一个标签页仍在、仍是房主", r.viewer_count() == 1 and r.host_id == a.viewer_id)
    m.disconnect(r.room_id, a.viewer_id)
    now[0] += 29
    m.tick()
    check("宽限期内还算在房间", r.viewer_count() == 1)
    again = m.connect(r.room_id, a.viewer_id, None)
    check("宽限期内回来保留名字和身份", again.name == a.name and r.host_id == a.viewer_id)


def test_empty_room_closes():
    m, r = mk()
    a = m.connect(r.room_id, None, None)
    m.disconnect(r.room_id, a.viewer_id)
    now[0] += 30 + 599
    m.tick()
    check("不到 10 分钟还在", m.get(r.room_id) is not None)
    now[0] += 2
    m.tick()
    check("空房 10 分钟后关闭", m.get(r.room_id) is None and r.closed)
    check("关闭时通知 listener", ("closed", r.room_id) in events)
    check("关闭的房间不在 active 里", r not in m.active())
    check("关闭的房间从 rooms 里删掉", r.room_id not in m.rooms)


def test_close():
    m, r = mk()
    m.connect(r.room_id, None, None)
    events.clear()
    m.close(r.room_id)
    check("close 标记关闭并删掉", r.closed and r.room_id not in m.rooms and m.get(r.room_id) is None)
    check("close 通知 closed", events == [("closed", r.room_id)])
    m.close(r.room_id)
    m.close("nope")
    check("重复 close / 不存在的房间不再通知", events == [("closed", r.room_id)])
    m2 = RoomManager(clock, max_bili_rooms=1)
    b = m2.create(1, 9, "小明", bili("BV1"))
    m2.close(b.room_id)
    check("close 后释放 B 站名额", m2.create(1, 9, "小明", bili("BV2")) is not None)


def test_never_joined_room_closes():
    m, r = mk()
    now[0] += 599
    m.tick()
    check("没人进过的房间 599 秒还在", m.get(r.room_id) is not None)
    now[0] += 2
    m.tick()
    check("没人进过的房间 600 秒后关闭", m.get(r.room_id) is None)


def test_bili_limit():
    m = RoomManager(clock, max_bili_rooms=1)
    m.create(1, 9, "小明", bili("BV1"))
    check("B 站满了拒绝新房间", raises(RoomsFull, lambda: m.create(1, 9, "小明", bili("BV2"))))
    r = m.create(1, 9, "小明", yt("a"))
    h = m.connect(r.room_id, None, r.owner_key)
    check("满了不能换成 B 站且原视频不变",
          raises(RoomsFull, lambda: m.change_video(r.room_id, h.viewer_id, bili("BV3"))) and r.video.id == "a")


def test_bili_limit_counts_current_video():
    m = RoomManager(clock, max_bili_rooms=1)
    r1 = m.create(1, 9, "小明", bili("BV1"))
    h = m.connect(r1.room_id, None, None)
    check("B 站房间换成 YouTube", m.change_video(r1.room_id, h.viewer_id, yt("a")))
    r2 = m.create(1, 9, "小明", bili("BV2"))
    check("换走后名额释放", r2 is not None)
    check("房间自己换 B 站不算占两个", m.change_video(r2.room_id, m.connect(r2.room_id, None, None).viewer_id, bili("BV9")))


def test_playback_math_and_seek_clamp():
    m, r = mk()
    r.video.duration = 100.0
    h = m.connect(r.room_id, None, r.owner_key)
    m.control(r.room_id, h.viewer_id, "play", None)
    now[0] += 10
    check("播放 10 秒后进度 10", r.playback.position_at(now[0], r.video.duration) == 10)
    check("seek 负数夹到 0", m.control(r.room_id, h.viewer_id, "seek", -5) and r.playback.position == 0)
    check("seek 超过时长夹到时长", m.control(r.room_id, h.viewer_id, "seek", 500) and r.playback.position == 100)
    check("seek NaN 拒绝", not m.control(r.room_id, h.viewer_id, "seek", math.nan))
    check("seek inf 拒绝", not m.control(r.room_id, h.viewer_id, "seek", math.inf))
    check("seek 不带位置拒绝", not m.control(r.room_id, h.viewer_id, "seek", None))
    check("未知动作拒绝", not m.control(r.room_id, h.viewer_id, "dance", None))
    m.control(r.room_id, h.viewer_id, "seek", 20)
    m.control(r.room_id, h.viewer_id, "pause", None)
    now[0] += 50
    check("暂停后进度不动", r.playback.position_at(now[0], r.video.duration) == 20)
    m.control(r.room_id, h.viewer_id, "play", None)
    now[0] += 500
    check("播放推进夹到时长", r.playback.position_at(now[0], r.video.duration) == 100)
    # 换视频
    b = m.connect(r.room_id, None, None)
    m.set_permission(r.room_id, h.viewer_id, b.public_id, True)
    v = r.version
    m.change_video(r.room_id, h.viewer_id, yt("x9"))
    check("换视频后暂停在 0、version + 1", not r.playback.playing and r.playback.position == 0 and r.version == v + 1)
    check("换视频后控制权保留", b.can_control)


def test_set_duration():
    m, r = mk()
    check("非正数拒绝", not m.set_duration(r.room_id, 0) and not m.set_duration(r.room_id, -3))
    check("NaN/inf 拒绝", not m.set_duration(r.room_id, math.nan) and not m.set_duration(r.room_id, math.inf))
    check("字符串拒绝", not m.set_duration(r.room_id, "90"))
    events.clear()
    check("没有时长时接受", m.set_duration(r.room_id, 90.5) and r.video.duration == 90.5)
    check("通知 video 事件", ("video", r.room_id) in events)
    check("已有时长不再接受", not m.set_duration(r.room_id, 10) and r.video.duration == 90.5)
    check("不存在的房间", not m.set_duration("nope", 10))


def test_listener_errors_and_random_name():
    m, r = mk()
    def boom(room, kind):
        raise RuntimeError("x")
    m.listeners.insert(0, boom)
    m.connect(r.room_id, None, None)
    check("listener 抛异常不影响其他 listener", ("members", r.room_id) in events)
    taken = {a + b for a in wr._ADJECTIVES for b in wr._ANIMALS}
    n = wr.random_name(taken, random.Random(1))
    check("组合用完追加数字后缀", n not in taken and n[-1].isdigit())
    check("名字不重复", wr.random_name({"快乐的企鹅"}, random.Random(1)) != "快乐的企鹅")


if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    test_defaults_and_permissions()
    test_owner_key_claim_once()
    test_untrusted_key()
    test_grace_and_tabs()
    test_empty_room_closes()
    test_never_joined_room_closes()
    test_close()
    test_bili_limit()
    test_bili_limit_counts_current_video()
    test_playback_math_and_seek_clamp()
    test_set_duration()
    test_listener_errors_and_random_name()
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
