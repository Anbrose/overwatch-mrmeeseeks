"""赛事推送调度测试：plan() 用固定时间；EsportsFeed 用假的抓取和发送函数。

悉尼时区：2026-10-03 是 AEST（UTC+10），2026-10-04 02:00 起夏令时 AEDT（UTC+11）。
运行：python tests/test_esports_feed.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import esports_feed as ef  # noqa: E402

results = []
UTC = timezone.utc


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def match(mid, start, finished=False, score=None, t1="T1", t2="ZETA DIVISION"):
    return {"id": mid, "start": int(start.timestamp()), "tournament": "Overwatch Champions Series/2026/x",
            "label": "OWCS Korea Stage 3 - Regular Season - Week 1", "team1": t1, "team2": t2,
            "best_of": 5, "finished": finished, "score": score,
            "url": "https://liquipedia.net/overwatch/Overwatch_Champions_Series/2026/x"}


def ready_state(**kw):
    s = ef.new_state()
    s.update(initialized=True, news_initialized=True, **kw)
    return s


def texts(posts):
    return [p.text for p in posts if p.text is not None]


def run_plan(now, matches, news, state):
    posts = ef.plan(now, matches, news, state)
    for p in posts:
        ef.apply_marks(state, p.marks)
    return posts


# 悉尼 2026-10-03 10:00 AEST = 2026-10-03 00:00 UTC
SYD_10AM = at(2026, 10, 3, 0)


def test_digest():
    m1 = match("a", SYD_10AM + timedelta(hours=5))
    m2 = match("b", SYD_10AM + timedelta(hours=2), t1="Crazy Raccoon", t2="O2 Blast")
    far = match("c", SYD_10AM + timedelta(hours=30))
    state = ready_state()
    check("10:00 之前不发预告", texts(run_plan(SYD_10AM - timedelta(minutes=1), [m1, m2, far], [], state)) == [])
    posts = texts(run_plan(SYD_10AM, [m1, m2, far], [], state))
    check("10:00 发一条预告", len(posts) == 1 and posts[0].startswith("📅"))
    check("预告按开始时间排序，只含 24 小时内", posts[0].index("Crazy Raccoon") < posts[0].index("T1 vs")
          and "<t:" + str(far["start"]) not in posts[0])
    check("预告用 Discord 时间戳并注明来源", f"<t:{m2['start']}:t>" in posts[0] and f"<t:{m2['start']}:R>" in posts[0]
          and ef.ATTRIBUTION in posts[0])
    check("记下悉尼日期", state["digest_date"] == "2026-10-03")
    check("当天不重复发", texts(run_plan(SYD_10AM + timedelta(hours=3), [m1, m2], [], state)) == [])
    check("bot 10:00 没在跑，当天晚些时候补发", len(texts(run_plan(SYD_10AM + timedelta(hours=4), [m1], [],
                                                         ready_state()))) == 1)
    empty = ready_state()
    check("没有比赛不发预告", texts(run_plan(SYD_10AM, [far], [], empty)) == [])
    check("没有比赛也记下日期，当天不再检查", empty["digest_date"] == "2026-10-03")
    check("已结束的比赛不进预告", texts(run_plan(SYD_10AM, [match("d", SYD_10AM + timedelta(hours=1),
                                                                True, [3, 0])], [], ready_state(
        resulted={"d": 0}))) == [])


def test_digest_dst():
    # 2026-10-04 起 AEDT：悉尼 10:00 = 2026-10-03 23:00 UTC
    state = ready_state(digest_date="2026-10-03")
    m = match("x", at(2026, 10, 4, 2))
    check("夏令时第一天：22:59 UTC（悉尼 09:59）不发", texts(run_plan(at(2026, 10, 3, 22, 59), [m], [], state)) == [])
    check("夏令时第一天：23:00 UTC（悉尼 10:00）发", len(texts(run_plan(at(2026, 10, 3, 23), [m], [], state))) == 1
          and state["digest_date"] == "2026-10-04")


def test_digest_split():
    many = [match(f"m{i}", SYD_10AM + timedelta(hours=1, minutes=i), t1="X" * 60, t2="Y" * 60) for i in range(40)]
    posts = ef.plan(SYD_10AM, many, [], ready_state())
    check("预告超长时拆成多条、每条不超过 1990 字符", len(posts) > 1 and all(len(p.text) <= 1990 for p in posts))
    check("只有最后一条带 digest_date 记账", [bool(p.marks) for p in posts] == [False] * (len(posts) - 1) + [True])


def test_reminders():
    start = at(2026, 10, 3, 6)
    m = match("r", start)
    state = ready_state(digest_date="2026-10-03")
    check("开赛前 16 分钟不提醒", texts(run_plan(start - timedelta(minutes=16), [m], [], state)) == [])
    posts = texts(run_plan(start - timedelta(minutes=15), [m], [], state))
    check("开赛前 15 分钟提醒，带直播和赛事链接", len(posts) == 1 and posts[0].startswith("🔴")
          and ef.WATCH_URL in posts[0] and m["url"] in posts[0] and f"<t:{m['start']}:R>" in posts[0])
    check("同一场不重复提醒", texts(run_plan(start - timedelta(minutes=5), [m], [], state)) == [])
    late = ready_state(digest_date="2026-10-03")
    check("开赛 5 分钟内仍补发", len(texts(run_plan(start + timedelta(minutes=5), [m], [], late))) == 1)
    later = ready_state(digest_date="2026-10-03")
    check("开赛超过 5 分钟不补发", texts(run_plan(start + timedelta(minutes=6), [m], [], later)) == [])


def test_results():
    start = at(2026, 10, 3, 6)
    done = match("f", start, finished=True, score=[3, 1])
    state = ready_state(digest_date="2026-10-03")
    posts = texts(run_plan(start + timedelta(hours=2), [done], [], state))
    check("赛果：胜者和比分在剧透遮罩里", len(posts) == 1 and "||T1 3 : 1 ZETA DIVISION||" in posts[0]
          and posts[0].startswith("✅") and ef.ATTRIBUTION in posts[0])
    check("队名和赛事不遮挡", "T1 vs ZETA DIVISION · OWCS Korea" in posts[0])
    check("赛果只发一次", texts(run_plan(start + timedelta(hours=3), [done], [], state)) == [])
    old = ready_state(digest_date="2026-10-03")
    check("开始超过 24 小时的赛果不发", texts(run_plan(start + timedelta(hours=25), [done], [], old)) == [])


def test_first_run_and_news():
    start = at(2026, 10, 3, 6)
    done = match("old", start, finished=True, score=[3, 0])
    news = [{"url": "https://esports.overwatch.com/en-us/news/a", "title": "A", "date": "9/9/2026"}]
    state = ef.new_state()
    check("首次启用：比赛和新闻都只记账、不发", texts(run_plan(start + timedelta(hours=1), [done], news, state)) == []
          and state["initialized"] and state["news_initialized"] and "old" in state["resulted"])
    fresh = ef.new_state()
    run_plan(start + timedelta(hours=1), [done], None, fresh)
    check("新闻抓取失败不阻塞比赛初始化", fresh["initialized"] and not fresh["news_initialized"])
    more = news + [{"url": "https://esports.overwatch.com/en-us/news/b", "title": None, "date": None}]
    posts = texts(run_plan(start + timedelta(hours=1, minutes=1), [done], more, state))
    check("只推新出现的新闻；没标题时用默认标题", posts == ["📰 Overwatch esports news https://esports.overwatch.com/en-us/news/b"])
    check("还没抓到比赛时（None）比赛部分什么都不做", ef.plan(SYD_10AM, None, [], ready_state()) == [])


def test_prune():
    now = at(2026, 10, 20, 0)
    state = ready_state(reminded={"old": int((now - timedelta(days=15)).timestamp()),
                                  "new": int((now - timedelta(days=1)).timestamp())})
    ef.prune(state, now)
    check("状态只保留最近 14 天", list(state["reminded"]) == ["new"])


def test_feed_tick():
    start = datetime.now(UTC) + timedelta(minutes=10)
    upcoming = match("live", start)
    calls = {"matches": 0, "news": 0}

    async def fetch_matches():
        calls["matches"] += 1
        return [upcoming]

    async def fetch_news():
        calls["news"] += 1
        raise RuntimeError("homepage down")

    sent = []

    async def send(channel_id, text):
        sent.append((channel_id, text))

    async def failing_send(channel_id, text):
        raise RuntimeError("missing permissions")

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "esports_state.json")
        feed = ef.EsportsFeed(path, fetch_matches, fetch_news)
        now = datetime.now(UTC)
        asyncio.run(feed.tick(now, send, mono=0))
        check("首次 tick：抓取比赛、初始化、没有频道不发送", calls["matches"] == 1 and sent == []
              and feed.state["initialized"])
        check("状态写入文件", json.load(open(path))["initialized"] is True)
        asyncio.run(feed.tick(now, send, mono=60))
        check("10 分钟内不重复抓比赛；新闻失败不影响", calls["matches"] == 1 and calls["news"] == 1)
        check("没有频道时照常记账（提醒已记下，之后设频道不补发）", "live" in feed.state["reminded"] and sent == [])

        feed2 = ef.EsportsFeed(path, fetch_matches, fetch_news)
        feed2.load()
        feed2.state["reminded"] = {}
        feed2.set_channel(42)
        asyncio.run(feed2.tick(now, failing_send, mono=0))
        check("发送失败不记账，下一轮重试", "live" not in feed2.state["reminded"])
        asyncio.run(feed2.tick(now, send, mono=60))
        check("重试成功后发到设置的频道并记账", sent and sent[0][0] == 42 and sent[0][1].startswith("🔴")
              and "live" in feed2.state["reminded"])
        check("upcoming 列出未开始的比赛", [m["id"] for m in feed2.upcoming()] == ["live"])
        feed3 = ef.EsportsFeed(path)
        feed3.load()
        check("重启后从文件恢复频道和记账", feed3.channel_id == 42 and "live" in feed3.state["reminded"])

    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, "esports_state.json")
        with open(bad, "w") as f:
            f.write("{broken")
        feed = ef.EsportsFeed(bad)
        feed.load()
        check("状态文件损坏时用默认状态", feed.state == ef.new_state())
        blocker = os.path.join(d, "blocker")
        open(blocker, "w").close()
        unwritable = ef.EsportsFeed(os.path.join(blocker, "esports_state.json"))
        unwritable.set_channel(1)
        check("状态写不进去时不抛异常、内存里照常生效", unwritable.channel_id == 1)


def test_feed_tick_send_failure_stops_tick():
    """Send failure should stop tick, not retry in same tick."""
    # 3 matches all in reminder window
    now = datetime.now(UTC)
    m1 = match("m1", now + timedelta(minutes=5))
    m2 = match("m2", now + timedelta(minutes=6))
    m3 = match("m3", now + timedelta(minutes=7))

    # Track send attempts
    send_attempts = []

    async def send_fail_on_second(channel_id, text):
        send_attempts.append((channel_id, text))
        if len(send_attempts) == 2:
            raise RuntimeError("broken channel")

    async def fetch_matches():
        return [m1, m2, m3]

    async def fetch_news():
        return []

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "esports_state.json")
        feed = ef.EsportsFeed(path, fetch_matches, fetch_news)

        # Initialize state
        state = ef.new_state()
        state["initialized"] = True
        state["news_initialized"] = True
        state["digest_date"] = now.astimezone(ef.TZ).date().isoformat()
        state["channel_id"] = 42
        feed.state = state
        feed.save()

        # Tick 1: second send fails, should not attempt third
        asyncio.run(feed.tick(now, send_fail_on_second, mono=0))
        check("第一次发送失败后停止该 tick", len(send_attempts) == 2)
        check("只有第一个比赛被记账", list(feed.state["reminded"].keys()) == ["m1"])

        # Tick 2: all sends succeed, should send m2 and m3, not re-send m1
        send_attempts.clear()

        async def send_ok(channel_id, text):
            send_attempts.append((channel_id, text))

        asyncio.run(feed.tick(now, send_ok, mono=60))
        check("重试时按顺序发送剩余的消息（m2, m3）", len(send_attempts) == 2)
        check("第一次失败的消息不重新发送", all("m1" not in text for _, text in send_attempts))
        check("三个比赛都被最终记账", set(feed.state["reminded"].keys()) == {"m1", "m2", "m3"})


def test_feed_tick_split_digest_send_failure():
    """Split digest: first chunk fails, others not attempted."""
    now = datetime.now(UTC)
    # Create many matches to force split
    many = [match(f"m{i}", now + timedelta(hours=1, minutes=i), t1="X" * 60, t2="Y" * 60)
            for i in range(40)]

    send_attempts = []

    async def send_fail_first(channel_id, text):
        send_attempts.append((channel_id, text))
        if len(send_attempts) == 1:
            raise RuntimeError("broken channel")

    async def fetch_matches():
        return many

    async def fetch_news():
        return []

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "esports_state.json")
        feed = ef.EsportsFeed(path, fetch_matches, fetch_news)

        # Initialize state without digest sent today
        state = ef.new_state()
        state["initialized"] = True
        state["news_initialized"] = True
        state["channel_id"] = 42
        feed.state = state
        feed.save()

        # Set time to 10:00 Sydney time to trigger digest
        sydney = now.astimezone(ef.TZ)
        utc_10am_syd = sydney.replace(hour=10, minute=0, second=0, microsecond=0).astimezone(UTC)

        # Tick: first send fails
        asyncio.run(feed.tick(utc_10am_syd, send_fail_first, mono=0))
        check("分割预告首行失败后停止", len(send_attempts) == 1)
        check("digest_date 未被记录（只在最后一行发出时记）", feed.state["digest_date"] is None)


if __name__ == "__main__":
    test_digest()
    test_digest_dst()
    test_digest_split()
    test_reminders()
    test_results()
    test_first_run_and_news()
    test_prune()
    test_feed_tick()
    test_feed_tick_send_failure_stops_tick()
    test_feed_tick_split_digest_send_failure()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
