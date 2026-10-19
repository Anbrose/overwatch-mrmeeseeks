"""竞猜接入推送：一场比赛从竞猜消息、提醒、截止、赛果揭晓到结算的完整流程，以及改期、作废、周榜、
发送失败、preview。用内存 SQLite、预先填好的阵容缓存、假的 send / edit。

悉尼 2026-10-26 是周一，AEDT（UTC+11）：悉尼 10:00 = 2026-10-25 23:00 UTC。
运行：python tests/test_prediction_feed.py
"""
import asyncio
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import esports_feed as ef  # noqa: E402
from PIL import Image  # noqa: E402
import logos as lg  # noqa: E402
import roster_card as rc  # noqa: E402
import prediction_feed as pf  # noqa: E402
import predictions as pr  # noqa: E402
import rosters as rs  # noqa: E402

results = []
UTC = timezone.utc
TOUR = "Overwatch Champions Series/2026/Asia/Stage 3/Korea/Regular Season#Week 3"
SYD_10AM_MON = datetime(2026, 10, 25, 23, 0, tzinfo=UTC)
T1_ROSTER = [{"id": "DONGHAK", "role": "tank", "flag": None}, {"id": "Proud", "role": "dps", "flag": None},
             {"id": "Bliss", "role": "support", "flag": None}]


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def match(start, t1="T1", t2="ZETA DIVISION", finished=False, score=None, tour=TOUR):
    ts = int(start.timestamp())
    return {"id": f"{tour}|{ts}|{t1}|{t2}", "start": ts, "tournament": tour,
            "label": "OWCS Korea Stage 3 - Regular Season - Week 3", "team1": t1, "team2": t2, "best_of": 5,
            "finished": finished, "score": score, "url": "https://liquipedia.net/overwatch/x"}


def roster_cache():
    cache = rs.RosterCache(None)
    cache.pages["Overwatch Champions Series/2026/Asia/Stage 3/Korea"] = {"fetched_at": 0, "teams": {"T1": T1_ROSTER}}
    return cache


class Discord:
    """假的发送 / 编辑：记录调用；fail_next 让下一次发送抛异常。"""
    def __init__(self):
        self.sent, self.edits, self.fail_next, self.next_id = [], [], False, 1000

    async def send(self, channel_id, text, view=None):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("discord down")
        self.next_id += 1
        self.sent.append((channel_id, text, view))
        return self.next_id, f"https://discord.com/channels/1/{channel_id}/{self.next_id}"

    async def edit(self, channel_id, message_id, view):
        self.edits.append((channel_id, message_id, view))

    def texts(self):
        return [t for _, t, _ in self.sent]


def make_feed(d, matches_box):
    store = pr.PredictionStore(":memory:")
    preds = pf.PredictionFeed(store, roster_cache())

    async def fetch_matches():
        return list(matches_box[0])

    async def fetch_news():
        return []

    feed = ef.EsportsFeed(os.path.join(d, "esports_state.json"), fetch_matches, fetch_news, predictions=preds)
    feed.state.update(initialized=True, news_initialized=True, channel_id=42)
    return feed, store


def tick(feed, now, discord, mono):
    asyncio.run(feed.tick(now, discord.send, mono, discord.edit))


def test_lifecycle():
    start = SYD_10AM_MON + timedelta(hours=3)
    m = match(start)
    box = [[m]]
    with tempfile.TemporaryDirectory() as d:
        feed, store = make_feed(d, box)
        feed.state["leaderboard_week"] = "2026-10-26"      # 本周榜已发过，不干扰这个用例
        discord = Discord()
        tick(feed, SYD_10AM_MON - timedelta(minutes=1), discord, mono=0)
        check("10:00 之前不发竞猜", not any(v for _, _, v in discord.sent))
        tick(feed, SYD_10AM_MON, discord, mono=60)
        preds = [(c, t, v) for c, t, v in discord.sent if v]
        check("10:00 发一条竞猜消息，发到默认频道", len(preds) == 1 and preds[0][0] == 42)
        _, text, view = preds[0]
        check("竞猜消息带阵容、Bo、截止说明和来源", "🛡️ DONGHAK · ⚔️ Proud · 💉 Bliss" in text
              and "**ZETA DIVISION**  Roster unavailable" in text and "picks close at start" in text
              and "Registered event roster" in text and "· Bo5" in text)
        check("按钮描述：两队、票数 0、可点", view == {"key": pr.key_for(m["id"]), "labels": ["T1", "ZETA DIVISION"],
                                         "counts": [0, 0], "disabled": False})
        entry = store.find(m)
        mid = entry["message_id"]
        check("登记了竞猜消息的 ID 和链接（预告先发，ID 在它之后）", mid == 1002 and entry["url"].endswith("/42/1002"))
        tick(feed, SYD_10AM_MON + timedelta(minutes=1), discord, mono=120)
        check("同一场不重复发竞猜", sum(1 for _, _, v in discord.sent if v) == 1)

        store.pick(entry["key"], 7, "Alice", 1, start.timestamp() - 3600)
        store.pick(entry["key"], 8, "Bob", 2, start.timestamp() - 3600)
        store.pick(entry["key"], 9, "Cara", 1, start.timestamp() - 3600)
        tick(feed, start - timedelta(minutes=15), discord, mono=180)
        reminder = discord.texts()[-1]
        check("提醒附上票数和竞猜链接", reminder.startswith("🔴") and "🎯 Picks: T1 · 2 — ZETA DIVISION · 1" in reminder
              and entry["url"] in reminder)

        tick(feed, start, discord, mono=240)
        check("开赛截止：公布双方名单", discord.texts()[-1] == "🔒 Picks closed: T1 vs ZETA DIVISION — T1: Alice, Cara · ZETA DIVISION: Bob")
        check("截止后状态 locked", store.get(entry["key"])["status"] == pr.LOCKED)
        check("编辑原消息：按钮禁用、显示最终票数", discord.edits[-1] == (42, mid, {
            "key": entry["key"], "labels": ["T1", "ZETA DIVISION"], "counts": [2, 1], "disabled": True}))
        tick(feed, start + timedelta(minutes=1), discord, mono=300)
        check("截止只做一次", sum(1 for t in discord.texts() if t.startswith("🔒")) == 1)

        box[0] = [match(start, finished=True, score=[1, 3])]
        tick(feed, start + timedelta(hours=2), discord, mono=900)
        result = discord.texts()[-1]
        check("赛果揭晓在剧透里：Bob 押冷门（1/3）猜中", "🎯 ||Correct: Bob +1.7 · Missed: Alice, Cara||" in result)
        check("揭晓行在来源行之前", result.splitlines()[-1] == ef.ATTRIBUTION)
        check("赛果发出后结算", store.get(entry["key"])["status"] == pr.RESOLVED
              and store.leaderboard(0, 2e9)[0]["name"] == "Bob")
        tick(feed, start + timedelta(hours=3), discord, mono=1500)
        check("结算只做一次、赛果不重发", sum(1 for t in discord.texts() if t.startswith("✅")) == 1)


def test_reschedule_and_void():
    start = SYD_10AM_MON + timedelta(hours=3)
    m = match(start)
    box = [[m]]
    with tempfile.TemporaryDirectory() as d:
        feed, store = make_feed(d, box)
        feed.state["leaderboard_week"] = "2026-10-26"
        discord = Discord()
        tick(feed, SYD_10AM_MON, discord, mono=0)
        moved = match(start + timedelta(minutes=10))
        box[0] = [moved]
        tick(feed, SYD_10AM_MON + timedelta(minutes=11), discord, mono=700)
        check("改期 10 分钟：不再发第二条竞猜", sum(1 for _, _, v in discord.sent if v) == 1)
        tick(feed, start + timedelta(minutes=1), discord, mono=1400)
        check("按新的开赛时间截止：原时间不截止", store.find(moved, {moved["id"]})["status"] == pr.OPEN)
        key = store.find(moved, {moved["id"]})["key"]
        check("改期后原开赛时间已过、新时间未到：仍可押注",
              store.pick(key, 5, "Eve", 1, (start + timedelta(minutes=2)).timestamp()).ok
              and store.get(key)["start"] == moved["start"])
        tick(feed, start + timedelta(minutes=10), discord, mono=2100)
        check("到新的开赛时间截止", store.find(moved, {moved["id"]})["status"] == pr.LOCKED)

        box[0] = []                               # 比赛从汇总页消失，一直没有赛果
        tick(feed, start + timedelta(hours=47), discord, mono=3000)
        check("48 小时内不作废", store.active() != [])
        tick(feed, start + timedelta(hours=48, minutes=10), discord, mono=3700)
        check("48 小时后作废", store.active() == [] and store.find(moved)["status"] == pr.VOID)
        check("作废时编辑原消息为 Voided", discord.edits[-1][2]["labels"] == ["Voided", "Voided"]
              and discord.edits[-1][2]["disabled"])



def test_down_across_start_and_finish():
    start = SYD_10AM_MON + timedelta(hours=3)
    box = [[match(start)]]
    with tempfile.TemporaryDirectory() as d:
        feed, store = make_feed(d, box)
        feed.state["leaderboard_week"] = "2026-10-26"
        discord = Discord()
        tick(feed, SYD_10AM_MON, discord, mono=0)
        entry = store.find(box[0][0])
        store.pick(entry["key"], 7, "Alice", 2, start.timestamp() - 60)
        box[0] = [match(start, finished=True, score=[1, 3])]       # bot 停机期间开赛又打完
        tick(feed, start + timedelta(hours=2), discord, mono=7200)
        texts = discord.texts()
        lock_i = next(i for i, t in enumerate(texts) if t.startswith("🔒"))
        result_i = next(i for i, t in enumerate(texts) if t.startswith("✅"))
        check("停机跨过开赛和赛果：先公布名单再揭晓", lock_i < result_i)
        check("停机跨过开赛和赛果：原消息按钮被禁用", any(mid == entry["message_id"] and v["disabled"]
                                                    for _, mid, v in discord.edits))
        check("停机跨过开赛和赛果：照常结算", store.get(entry["key"])["status"] == pr.RESOLVED)


def test_long_name_lists():
    entry = {"team1": "T1", "team2": "ZETA"}
    names = {1: [f"player_{i:03d}_longname" for i in range(150)], 2: ["Bob"]}
    text = pf.format_lock(entry, names)
    check("名单太长时截断：整条消息不超过 2000 字", len(text) < 1900 and "and " in text and " more" in text
          and "Bob" in text)
    lines = [pr.ScoreLine(i, f"player_{i:03d}_longname", i % 2 == 0, 1.0) for i in range(200)]
    line = pf.reveal_line(lines)
    check("揭晓名单太长时截断，仍在剧透里", len(line) < 1700 and line.startswith("🎯 ||") and line.endswith("||")
          and " more" in line)


def test_draw_and_no_channel_and_tbd():
    start = SYD_10AM_MON + timedelta(hours=3)
    with tempfile.TemporaryDirectory() as d:
        feed, store = make_feed(d, [[match(start), match(start + timedelta(hours=1), "TBD", "TBD")]])
        feed.state["leaderboard_week"] = "2026-10-26"
        discord = Discord()
        tick(feed, SYD_10AM_MON, discord, mono=0)
        check("TBD 对 TBD 不开竞猜", sum(1 for _, _, v in discord.sent if v) == 1)
        key = pr.key_for(match(start)["id"])
        feed.matches = None
        feed._matches_at = float("-inf")
        feed.fetch_matches = lambda: _ret([match(start, finished=True, score=[2, 2])])
        tick(feed, start + timedelta(hours=2), discord, mono=5000)
        check("没有胜负（平局或比分缺失）：作废，不计分", store.get(key)["status"] == pr.VOID)

    with tempfile.TemporaryDirectory() as d:
        feed, store = make_feed(d, [[match(start)]])
        feed.state.update(channel_id=None, leaderboard_week="2026-10-26")
        discord = Discord()
        tick(feed, SYD_10AM_MON, discord, mono=0)
        check("没有频道：不发也不登记", discord.sent == [] and store.active() == [])


async def _ret(v):
    return v


def test_send_failure_retry():
    start = SYD_10AM_MON + timedelta(hours=3)
    with tempfile.TemporaryDirectory() as d:
        feed, store = make_feed(d, [[match(start)]])
        feed.state.update(digest_date="2026-10-26", leaderboard_week="2026-10-26")
        discord = Discord()
        discord.fail_next = True
        tick(feed, SYD_10AM_MON, discord, mono=0)
        check("竞猜消息发送失败：不登记", store.active() == [])
        tick(feed, SYD_10AM_MON + timedelta(minutes=1), discord, mono=60)
        check("下一轮重试成功并登记", len(store.active()) == 1 and discord.sent[-1][2] is not None)


def test_leaderboard():
    with tempfile.TemporaryDirectory() as d:
        feed, store = make_feed(d, [[]])
        last_week = SYD_10AM_MON - timedelta(days=3)
        for i, (winner, picks) in enumerate([(1, [(1, 1), (2, 2), (3, 1)]), (2, [(1, 1), (2, 2), (3, 2)])]):
            k = store.register(match(last_week + timedelta(hours=i)), 42, 1)
            for uid, side in picks:
                store.pick(k, uid, f"u{uid}", side, last_week.timestamp() - 3600)
            store.resolve(k, winner, last_week.timestamp() + i * 3600)
        discord = Discord()
        tick(feed, SYD_10AM_MON - timedelta(minutes=1), discord, mono=0)
        check("周一 10:00 之前不发周榜", not any(t.startswith("🏆") for t in discord.texts()))
        tick(feed, SYD_10AM_MON, discord, mono=60)
        board = [t for t in discord.texts() if t.startswith("🏆")]
        check("周一 10:00 发上周周榜到默认频道", len(board) == 1 and "week of 2026-10-19" in board[0]
              and discord.sent[-1][0] == 42)
        check("周榜带排名和称号", "1. u" in board[0] and "🔮 Prophet of the week" in board[0])
        tick(feed, SYD_10AM_MON + timedelta(hours=1), discord, mono=120)
        check("同一周不重复发", sum(1 for t in discord.texts() if t.startswith("🏆")) == 1)

    with tempfile.TemporaryDirectory() as d:
        feed, store = make_feed(d, [[]])
        discord = Discord()
        tick(feed, SYD_10AM_MON, discord, mono=0)
        check("上周没有得分：不发周榜，但记下本周", not any(t.startswith("🏆") for t in discord.texts())
              and feed.state["leaderboard_week"] == "2026-10-26")


def test_preview():
    start = SYD_10AM_MON + timedelta(hours=3)
    preds = pf.PredictionFeed(pr.PredictionStore(":memory:"), roster_cache())
    m = match(start)
    sample = preds.preview(SYD_10AM_MON, [match(start + timedelta(hours=1), "TBD", "TBD"), m], lambda x: True)
    text, view, card = sample
    check("preview：下一场的竞猜样例，带阵容和 Preview 前缀", text.startswith(ef.PREVIEW_PREFIX + "🎯 **Who wins?** T1 vs ZETA")
          and "DONGHAK" in text)
    check("preview：按钮禁用", view["disabled"] is True and view["counts"] == [0, 0])
    check("preview：不登记", preds.store.active() == [])
    check("preview：没开图片模式时没有对阵图", card is None)
    check("preview：没有路由到这里的比赛返回 None", preds.preview(SYD_10AM_MON, [m], lambda x: False) is None)


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class CardDiscord(Discord):
    """图片模式的假发送：多收一个对阵图。"""
    def __init__(self):
        super().__init__()
        self.files = []

    async def send(self, channel_id, text, view=None, file=None):
        self.files.append(file)
        return await super().send(channel_id, text, view)


def card_feed(d, box, logos_dir):
    feed, store = make_feed(d, box)
    feed.predictions = pf.PredictionFeed(store, roster_cache(), lg.LogoCache(logos_dir))
    return feed, store


def test_card_mode():
    start = SYD_10AM_MON + timedelta(hours=3)
    m = match(start)
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as logos_dir:
        feed, store = card_feed(d, [[m]], logos_dir)
        feed.state["leaderboard_week"] = "2026-10-26"
        discord = CardDiscord()
        asyncio.run(feed.tick(SYD_10AM_MON, discord.send, 0, discord.edit))
        i = next(i for i, (_, _, v) in enumerate(discord.sent) if v)
        text, png = discord.sent[i][1], discord.files[i]
        check("图片模式：竞猜消息附上对阵图（PNG）", png is not None and png.startswith(PNG_MAGIC))
        check("图片模式：文字只有标题和开赛时间，阵容在图里", text.startswith("🎯 **Who wins?** T1 vs ZETA DIVISION")
              and "picks close at start" in text and "DONGHAK" not in text and "Registered event roster" not in text)
        check("图片模式：照常登记竞猜消息", store.find(m)["message_id"] == discord.next_id)

        sample = feed.predictions.preview(SYD_10AM_MON, [m], lambda x: True)
        check("图片模式：preview 也附上对阵图", sample[2] is not None and sample[2].startswith(PNG_MAGIC)
              and sample[0].startswith(ef.PREVIEW_PREFIX + "🎯 **Who wins?**") and "DONGHAK" not in sample[0])

    original = rc.render_card

    def broken(*a, **k):
        raise RuntimeError("font missing")
    rc.render_card = broken
    try:
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as logos_dir:
            feed, store = card_feed(d, [[m]], logos_dir)
            feed.state["leaderboard_week"] = "2026-10-26"
            discord = CardDiscord()
            asyncio.run(feed.tick(SYD_10AM_MON, discord.send, 0, discord.edit))
            i = next(i for i, (_, _, v) in enumerate(discord.sent) if v)
            check("画图失败：退回文字阵容，照常发出并登记", discord.files[i] is None and "DONGHAK" in discord.sent[i][1]
                  and store.find(m) is not None)
    finally:
        rc.render_card = original


def test_prepare_downloads_logos():
    m = match(SYD_10AM_MON + timedelta(hours=3))
    url1 = "https://liquipedia.net/commons/images/thumb/a/aa/T1.png/300px-T1.png"
    url2 = "https://liquipedia.net/commons/images/thumb/b/bb/ZETA.png/300px-ZETA.png"

    async def fetch_page(page):
        return {"teams": {"T1": T1_ROSTER}, "logos": {"T1": url1, "ZETA DIVISION": url2}}
    got = []

    async def fetch_logo(url):
        got.append(url)
        out = io.BytesIO()
        Image.new("RGBA", (30, 12), (230, 20, 40, 255)).save(out, "PNG")
        return out.getvalue()

    async def no_sleep(s):
        pass
    with tempfile.TemporaryDirectory() as logos_dir:
        cache = rs.RosterCache(None, fetch_page, sleep=no_sleep)
        preds = pf.PredictionFeed(pr.PredictionStore(":memory:"), cache, lg.LogoCache(logos_dir, fetch_logo, sleep=no_sleep))
        asyncio.run(preds._ensure([m]))
        check("准备阵容之后下载双方队徽", got == [url1, url2] and preds.logos.path(url1) is not None)


def test_pure_helpers():
    check("揭晓行：没人押就不加", pf.reveal_line([]) is None)
    check("截止名单：没人押写 nobody", pf.format_lock({"team1": "A", "team2": "B"}, {1: [], 2: ["x"]})
          == "🔒 Picks closed: A vs B — A: nobody · B: x")
    check("week_start：周一 00:00 悉尼", pf.week_start(SYD_10AM_MON).isoformat() == "2026-10-26T00:00:00+11:00")
    check("winner_of", pf.winner_of({"score": [3, 1]}) == 1 and pf.winner_of({"score": [0, 3]}) == 2
          and pf.winner_of({"score": [2, 2]}) is None)


if __name__ == "__main__":
    test_lifecycle()
    test_reschedule_and_void()
    test_down_across_start_and_finish()
    test_long_name_lists()
    test_draw_and_no_channel_and_tbd()
    test_send_failure_retry()
    test_leaderboard()
    test_preview()
    test_card_mode()
    test_prepare_downloads_logos()
    test_pure_helpers()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
