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
from watch_rooms import RoomManager, RoomsFull, Video, VodInfo, vod_title, vod_video  # noqa: E402
from vod_search import Episode, Line, Show  # noqa: E402
from tv_channels import Candidate, Channel  # noqa: E402
from watch_rooms import TvInfo, TvRoomsFull, tv_video  # noqa: E402

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


def mkshow(n=3, title="绝命毒师 第一季"):
    lines = [Line(f"线路{k}", [Episode(f"第{i + 1}集", f"https://v{k}.example/{i}/index.m3u8") for i in range(n)])
             for k in (1, 2)]
    return Show("k1", title, 2008, "美剧", lines)


def mkvod(episode=0, duration=3000.0):
    info = VodInfo(mkshow(), episode, 0)
    return vod_video(info, duration)


def test_vod_title():
    check("多集标题", vod_title(mkshow(), 2) == "绝命毒师 第一季 · 第 3 集")
    check("单集只有片名", vod_title(mkshow(1), 0) == "绝命毒师 第一季")
    v = mkvod(1)
    check("vod_video 字段", v.source == "vod" and v.id == "k1" and v.page == 2 and v.duration == 3000.0
          and v.url == "https://v1.example/1/index.m3u8" and v.vod.episode == 1 and v.title.endswith("第 2 集"))


def test_change_video_position():
    m, r = mk()
    h = m.connect(r.room_id, None, None)
    v0 = r.version
    check("房主切到指定位置", m.change_video(r.room_id, h.viewer_id, mkvod(), position=120.0)
          and r.version == v0 + 1 and r.playback.position == 120.0 and r.playback.playing is False)
    check("video 事件", ("video", r.room_id) in events)
    check("超出时长夹住", m.change_video(r.room_id, h.viewer_id, mkvod(), position=5000.0)
          and r.playback.position == 3000.0)
    b = m.connect(r.room_id, None, None)
    v = r.version
    check("非房主不能切", not m.change_video(r.room_id, b.viewer_id, mkvod(), position=1.0) and r.version == v)
    check("非法位置拒绝", not m.change_video(r.room_id, h.viewer_id, mkvod(), position=math.nan)
          and not m.change_video(r.room_id, h.viewer_id, mkvod(), position=True) and r.version == v)


def test_resume_permission():
    m, r = mk(mkvod())
    h = m.connect(r.room_id, None, None)
    b = m.connect(r.room_id, None, None)
    check("没有控制权不能 resume", not m.resume(r.room_id, b.viewer_id, mkvod(1), 50.0))
    m.set_permission(r.room_id, h.viewer_id, b.public_id, True)
    check("有控制权可以 resume", m.resume(r.room_id, b.viewer_id, mkvod(1), 50.0)
          and r.playback.playing is False and r.playback.position == 50.0 and r.video.vod.episode == 1)
    check("房主可以 resume", m.resume(r.room_id, h.viewer_id, mkvod(2), 10.0) and r.video.vod.episode == 2)
    check("非法位置拒绝", not m.resume(r.room_id, h.viewer_id, mkvod(), math.inf))
    m2, r2 = mk()
    h2 = m2.connect(r2.room_id, None, None)
    check("当前是 YouTube 不能 resume", not m2.resume(r2.room_id, h2.viewer_id, mkvod(), 5.0))


def test_advance_version_guard():
    m, r = mk(mkvod())
    m.connect(r.room_id, None, None)
    old = r.version
    check("版本一致时切换", m.advance(r.room_id, old, mkvod(1)) and r.playback.playing is True
          and r.playback.position == 0 and r.version == old + 1 and r.video.vod.episode == 1)
    check("旧版本被丢弃", not m.advance(r.room_id, old, mkvod(2)) and r.video.vod.episode == 1
          and r.version == old + 1)
    m2, r2 = mk()
    check("非 vod 房间不能 advance", not m2.advance(r2.room_id, r2.version, mkvod()))
    m.close(r.room_id)
    check("已关闭房间不能 advance", not m.advance(r.room_id, r.version, mkvod()))


def finished_count():
    return sum(1 for k, _ in events if k == "finished")


def test_tick_finished():
    m, r = mk(mkvod())
    h = m.connect(r.room_id, None, None)
    m.control(r.room_id, h.viewer_id, "play", 2990.0)
    now[0] += 5
    m.tick()
    check("没到结尾不通知", finished_count() == 0)
    now[0] += 20
    m.tick()
    check("到结尾通知一次", finished_count() == 1 and r.finished_version == r.version)
    m.tick()
    check("不重复通知", finished_count() == 1)
    m.advance(r.room_id, r.version, mkvod(1))
    m.control(r.room_id, h.viewer_id, "seek", 2995.0)
    now[0] += 10
    m.tick()
    check("下一集结尾再次通知", finished_count() == 2)


def test_tick_finished_needs_people():
    m, r = mk(mkvod())
    h = m.connect(r.room_id, None, None)
    m.control(r.room_id, h.viewer_id, "play", 2990.0)
    m.disconnect(r.room_id, h.viewer_id)
    now[0] += wr.GRACE_SECONDS + 20
    m.tick()
    check("没人在就不通知", finished_count() == 0)


def test_tick_finished_not_for_youtube():
    m, r = mk()
    h = m.connect(r.room_id, None, None)
    m.set_duration(r.room_id, 100.0)
    m.control(r.room_id, h.viewer_id, "play", 90.0)
    now[0] += 20
    m.tick()
    check("YouTube 不通知 finished", finished_count() == 0)


def tv(key="CCTV1", n=3):
    ch = Channel(key, key, [Candidate(f"http://h{i}.example/{key}.m3u8", None, 0, False) for i in range(n)])
    return tv_video(TvInfo(ch, ch.ordered(), 0))


def test_tv_limit():
    now[0] = 1000.0
    m = RoomManager(clock, max_tv_rooms=2)
    r1 = m.create(1, 9, "a", tv("CCTV1"))
    m.create(1, 9, "a", tv("CCTV2"))
    try:
        m.create(1, 9, "a", tv("CCTV3"))
        err = None
    except TvRoomsFull as e:
        err = e
    check("第 3 个直播房间被拒绝", err is not None and err.limit == 2)
    check("提示文案", err is not None and str(err) == "直播房间已满（最多 2 个），请稍后再试")
    m2 = RoomManager(clock, max_bili_rooms=2, max_tv_rooms=2)
    m2.create(1, 9, "a", bili("b1"))
    m2.create(1, 9, "a", bili("b2"))
    m2.create(1, 9, "a", tv("CCTV1"))
    check("B 站房间不占直播名额", len(m2.active()) == 3)
    m3 = RoomManager(clock, max_bili_rooms=1, max_tv_rooms=2)
    m3.create(1, 9, "a", tv("CCTV1"))
    m3.create(1, 9, "a", tv("CCTV2"))
    m3.create(1, 9, "a", bili("b1"))
    check("直播房间不占 B 站名额", len(m3.active()) == 3)
    h = m.connect(r1.room_id, None, r1.owner_key)
    check("tv 换 tv 不算多一个", m.change_video(r1.room_id, h.viewer_id, tv("CCTV5")))
    check("换到 YouTube", m.change_video(r1.room_id, h.viewer_id, yt("y1")))
    check("换走后可以再建直播房间", m.create(1, 9, "a", tv("CCTV3")) is not None)
    r4 = m.active()[1]
    h4 = m.connect(r4.room_id, None, r4.owner_key)
    check("满员时 YouTube 换 tv 被拒绝", raises(TvRoomsFull, lambda: m.change_video(
        r1.room_id, h.viewer_id, tv("CCTV9"))))
    check("拒绝后视频没变", r1.video.source == "youtube" and h4)


def test_tv_control_and_switch():
    m, r = mk(tv())
    h = m.connect(r.room_id, None, None)
    check("tv 不能 seek", not m.control(r.room_id, h.viewer_id, "seek", 30.0))
    check("tv 暂停", m.control(r.room_id, h.viewer_id, "pause", 55.0)
          and r.playback.playing is False and r.playback.position == 0.0)
    check("tv 播放", m.control(r.room_id, h.viewer_id, "play", 77.0)
          and r.playback.playing is True and r.playback.position == 0.0)
    v0 = r.version
    v2 = tv("CCTV2")
    check("switch_tv 版本对", m.switch_tv(r.room_id, v0, v2) and r.version == v0 + 1
          and r.video is v2 and r.playback.playing is True and r.playback.position == 0.0)
    check("switch_tv 旧版本被丢弃", not m.switch_tv(r.room_id, v0, tv("CCTV3")) and r.video is v2)
    m.control(r.room_id, h.viewer_id, "pause", None)
    check("switch_tv 保持暂停", m.switch_tv(r.room_id, r.version, tv("CCTV4")) and r.playback.playing is False)
    m5, r5 = mk(yt("z"))
    check("非 tv 房间 switch_tv 无效", not m5.switch_tv(r5.room_id, r5.version, tv()))
    check("tv_video 字段", v2.source == "tv" and v2.id == "CCTV2" and v2.page == 1 and v2.duration is None
          and v2.url == v2.tv.order[0].url and v2.tv.bad == set())
    check("直播不接受时长", not m.set_duration(r.room_id, 90.0) and r.video.duration is None)


def test_tick_no_finished_for_tv():
    m, r = mk(tv())
    h = m.connect(r.room_id, None, None)
    m.control(r.room_id, h.viewer_id, "play", None)
    now[0] += 100000
    m.tick()
    check("tv 不通知 finished", finished_count() == 0)


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
    test_vod_title()
    test_change_video_position()
    test_resume_permission()
    test_advance_version_guard()
    test_tick_finished()
    test_tick_finished_needs_people()
    test_tick_finished_not_for_youtube()
    test_tv_limit()
    test_tv_control_and_switch()
    test_tick_no_finished_for_tv()
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
