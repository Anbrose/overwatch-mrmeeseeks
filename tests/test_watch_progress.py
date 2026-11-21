"""一起看观看进度测试：存取、损坏文件、事件记录、定时记录、续看文案。

运行：python tests/test_watch_progress.py
不联网，不需要 Discord。
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

from watch_progress import (MIN_POSITION, Progress, ProgressRecorder, ProgressStore, resume_line,  # noqa: E402
                            resume_target, seen_text)
from watch_rooms import RoomManager, Video, VodInfo, vod_video  # noqa: E402
from vod_search import Episode, Line, Show  # noqa: E402

results = []
now = [1000.0]


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def clock():
    return now[0]


def mkshow(n=3, title="绝命毒师 第一季"):
    lines = [Line("线路1", [Episode(f"第{i + 1}集", f"https://v1.example/{i}/index.m3u8") for i in range(n)])]
    return Show("k1", title, 2008, "美剧", lines)


def mkvod(episode=0, duration=3000.0):
    return vod_video(VodInfo(mkshow(), episode, 0), duration)


def test_store_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "sub" / "watch_progress.json"
        s = ProgressStore(path)
        p1 = Progress("绝命毒师 第一季", 2, 2052.0, 111.0)
        p2 = Progress("别的", 0, 99.0, 222.0)
        check("record 返回 True", s.record(1, "k1", p1) and s.record(2, "k1", p2))
        s2 = ProgressStore(path)
        check("重新加载得到相同进度", s2.get(1, "k1") == p1)
        check("频道互相独立", s2.get(2, "k1") == p2 and s2.get(3, "k1") is None and s2.get(1, "zz") is None)
        p3 = Progress("绝命毒师 第一季", 3, 500.0, 333.0)
        s2.record(1, "k1", p3)
        check("同一个 key 覆盖旧记录", ProgressStore(path).get(1, "k1") == p3)


def test_store_min_position():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "p.json"
        s = ProgressStore(path)
        check("不足 30 秒不记", MIN_POSITION == 30.0 and s.record(1, "k", Progress("t", 0, 29.9, 1.0)) is False)
        check("get 为空且没写文件", s.get(1, "k") is None and not path.exists())
        check("刚好 30 秒记", s.record(1, "k", Progress("t", 0, 30.0, 1.0)) and s.get(1, "k") is not None)


def test_store_corrupt():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "p.json"
        check("文件不存在 → 空", ProgressStore(path).get(1, "k") is None)
        for i, content in enumerate(['', '"', '{', '[]', '{"1": "x"}', '{"1": {"k": "x"}}',
                                     '{"1": {"k": {"title": "t"}}}', 'null']):
            path.write_text(content, encoding="utf-8")
            try:
                s = ProgressStore(path)
                ok = s.get(1, "k") is None
                p = Progress("t", 1, 60.0, 5.0)
                ok = ok and s.record(1, "k", p) and ProgressStore(path).get(1, "k") == p
            except Exception as e:  # noqa: BLE001
                print("exception:", repr(e))
                ok = False
            check(f"损坏文件 {content!r} → 空，之后能写", ok)


def mkrec():
    now[0] = 1000.0
    m = RoomManager(clock)
    s = ProgressStore(None)
    rec = ProgressRecorder(m, s)
    return m, s, rec


def test_recorder_events():
    m, s, rec = mkrec()
    check("注册为 listener", rec.on_event in m.listeners)
    r = m.create(7, 9, "小明", mkvod(1))
    h = m.connect(r.room_id, None, None)
    m.control(r.room_id, h.viewer_id, "play", 0.0)
    now[0] += 100
    m.control(r.room_id, h.viewer_id, "pause", None)
    p = s.get(7, "k1")
    check("暂停时记录当前位置", p is not None and p.position == 100.0 and p.episode == 1
          and p.title == "绝命毒师 第一季" and p.updated_at == 1100.0)
    m.control(r.room_id, h.viewer_id, "play", None)
    now[0] += 50
    m.close(r.room_id)
    p = s.get(7, "k1")
    check("关闭时记录最终位置", p.position == 150.0 and p.updated_at == 1150.0)
    check("关闭后忘记房间", r.room_id not in rec._last)

    m, s, rec = mkrec()
    r = m.create(7, 9, "小明", Video("youtube", "x", 1, "t", 5000.0, "https://youtu.be/x"))
    h = m.connect(r.room_id, None, None)
    m.control(r.room_id, h.viewer_id, "play", 0.0)
    now[0] += 100
    m.control(r.room_id, h.viewer_id, "pause", None)
    rec.tick()
    m.close(r.room_id)
    check("YouTube 房间不记录", s.get(7, "x") is None and not s.data)


def test_recorder_tick_interval():
    m, s, rec = mkrec()
    r = m.create(7, 9, "小明", mkvod())
    h = m.connect(r.room_id, None, None)
    m.control(r.room_id, h.viewer_id, "play", 0.0)
    now[0] = 1000.0 + 5
    rec.tick()
    check("不足 30 秒位置不记", s.get(7, "k1") is None)
    now[0] = 1000.0 + 31
    rec.tick()
    check("+31s 记录", s.get(7, "k1").position == 31.0)
    now[0] = 1000.0 + 45
    rec.tick()
    check("+45s 不记", s.get(7, "k1").position == 31.0)
    now[0] = 1000.0 + 62
    rec.tick()
    check("+62s 再记", s.get(7, "k1").position == 62.0)
    m.control(r.room_id, h.viewer_id, "pause", None)
    now[0] = 1000.0 + 200
    rec.tick()
    check("暂停中不 tick 记录", s.get(7, "k1").position == 62.0)


def test_recorder_empty_room():
    from watch_rooms import EMPTY_TTL_SECONDS, GRACE_SECONDS
    m, s, rec = mkrec()
    r = m.create(7, 9, "小明", mkvod())
    h = m.connect(r.room_id, None, None)
    m.control(r.room_id, h.viewer_id, "play", 0.0)
    now[0] += 100
    m.disconnect(r.room_id, h.viewer_id)       # 没暂停就走了
    while not r.closed and now[0] < 1000.0 + 100 + GRACE_SECONDS + EMPTY_TTL_SECONDS + 60:
        now[0] += 5
        m.tick()
        rec.tick()
    p = s.get(7, "k1")
    check("空房最终关闭", r.closed)
    check("空房不继续记进度：位置≈离开时（+≤30 秒宽限）而不是 +10 分钟",
          p is not None and 100.0 <= p.position <= 100.0 + GRACE_SECONDS + 5)
    check("变空那一刻记一次", p is not None and p.updated_at <= 1000.0 + 100 + GRACE_SECONDS + 5)
    check("关闭后忘记房间（空房）", r.room_id not in rec._last and r.room_id not in rec._empty)

    # 宽限期内回来：不算变空，之后照常记
    m, s, rec = mkrec()
    r = m.create(7, 9, "小明", mkvod())
    h = m.connect(r.room_id, None, None)
    m.control(r.room_id, h.viewer_id, "play", 0.0)
    now[0] += 50
    m.disconnect(r.room_id, h.viewer_id)
    now[0] += 10
    m.tick()
    h2 = m.connect(r.room_id, h.viewer_id, None)
    check("宽限期内回来不算变空", r.room_id not in rec._empty and h2.viewer_id == h.viewer_id)
    now[0] += 40
    rec.tick()
    check("回来后 tick 照常记", s.get(7, "k1").position == 100.0)

    # 变空后又有人进来、再走：再记一次
    m.disconnect(r.room_id, h.viewer_id)
    now[0] += GRACE_SECONDS
    m.tick()
    first = s.get(7, "k1").position
    check("变空时记一次", first == 100.0 + GRACE_SECONDS and r.room_id in rec._empty)
    now[0] += 200
    m.tick()
    rec.tick()
    check("空房 tick 不记", s.get(7, "k1").position == first)
    h3 = m.connect(r.room_id, None, None)
    check("有人进来清掉变空标记", r.room_id not in rec._empty)
    m.control(r.room_id, h3.viewer_id, "seek", 500.0)
    m.disconnect(r.room_id, h3.viewer_id)
    now[0] += GRACE_SECONDS
    m.tick()
    check("第二次变空再记", s.get(7, "k1").position == 500.0 + GRACE_SECONDS)


def test_resume_line():
    ts = datetime(2026, 10, 23, 20, 0, tzinfo=ZoneInfo("Australia/Sydney")).timestamp()
    p = Progress("绝命毒师 第一季", 2, 2052, ts)
    check("多集", resume_line(p, 7) == "上次一起看到 第 3 集 34:12（10月23日）")
    check("超过一小时", resume_line(Progress("t", 2, 3725, ts), 7) == "上次一起看到 第 3 集 1:02:05（10月23日）")
    check("单集不带第 n 集", resume_line(p, 1) == "上次一起看到 34:12（10月23日）")
    named = Progress("后会友期", 5, 2052, ts, "第3期上")
    check("带名字的分段写副标题", resume_line(named, 27) == "上次一起看到 第3期上 34:12（10月23日）"
          and seen_text(named) == "上次看到 第3期上" and seen_text(p) == "上次看到第 3 集")


def test_titled_progress():
    def line(name, labels):
        return Line(name, [Episode(lb, f"https://{name}.example/{i}/index.m3u8") for i, lb in enumerate(labels)])
    show = Show("v", "后会友期", 2026, "综艺", [line("a", ["第20261029期上", "第20261029期下"]),
                                              line("b", ["20261030加更版", "20261029下", "20261029上"])])
    now = [1000.0]
    m = RoomManager(clock=lambda: now[0])
    s = ProgressStore(None)
    ProgressRecorder(m, s)
    r = m.create(7, 42, "甲", vod_video(VodInfo(show, 1, 1), 3000.0))
    h = m.connect(r.room_id, None, None)
    m.control(r.room_id, h.viewer_id, "seek", 100.0)
    p = s.get(7, "v")
    check("分段的进度带副标题", p is not None and p.episode == 1 and p.label == "20261029下")
    check("续看按副标题找：当前线路优先", resume_target(show, 1, p) == (1, 1) and resume_target(show, 0, p) == (0, 1))
    check("当前线路没有就去别的线路找", resume_target(show, 0, Progress("t", 0, 60.0, 1.0, "20261030加更")) == (1, 0))
    check("哪条线路都没有 → None", resume_target(show, 0, Progress("t", 0, 60.0, 1.0, "第9期")) is None)
    check("旧记录没有副标题 → 按序号落在当前线路", resume_target(show, 0, Progress("t", 1, 60.0, 1.0)) == (0, 1)
          and resume_target(show, 0, Progress("t", 2, 60.0, 1.0)) is None)
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "p.json"
        st = ProgressStore(path)
        st.record(1, "v", p)
        st.record(1, "k", Progress("剧", 2, 60.0, 1.0))
        back = ProgressStore(path)
        check("副标题存盘后读得回来，没有的不写", back.get(1, "v").label == "20261029下" and back.get(1, "k").label == ""
              and "label" not in json.loads(path.read_text(encoding="utf-8"))["1"]["k"])


if __name__ == "__main__":
    test_store_roundtrip()
    test_store_min_position()
    test_store_corrupt()
    test_recorder_events()
    test_recorder_tick_interval()
    test_recorder_empty_room()
    test_resume_line()
    test_titled_progress()
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
