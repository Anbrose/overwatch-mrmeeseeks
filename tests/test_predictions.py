"""竞猜存储与计分测试（内存 SQLite）。

运行：python tests/test_predictions.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import predictions as pr  # noqa: E402

results = []
START = 1_791_000_000


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def match(mid="m1", start=START, t1="T1", t2="ZETA DIVISION"):
    return {"id": mid, "start": start, "team1": t1, "team2": t2, "label": "OWCS Korea Stage 3"}


def store_with(picks, mid="m1"):
    s = pr.PredictionStore(":memory:")
    key = s.register(match(mid), 10, 99)
    for uid, side in picks:
        s.pick(key, uid, f"u{uid}", side, START - 100)
    return s, key


def test_key_and_register():
    key = pr.key_for("Overwatch Champions Series/2026/x|1791000000|T1|ZETA DIVISION")
    check("key 是 12 位十六进制", len(key) == 12 and all(c in "0123456789abcdef" for c in key))
    long_id = "Overwatch Champions Series/2026/Asia/Stage 3/Korea/Regular Season#Week 1|1791012600|" + "X" * 80
    check("长比赛 ID 也能放进 custom_id（pred:<key>:<side> ≤ 100）", len(f"pred:{pr.key_for(long_id)}:1") <= 100)
    s = pr.PredictionStore(":memory:")
    k1 = s.register(match(), 10, 99)
    k2 = s.register(match(), 11, 100)
    m = s.get(k1)
    check("重复登记返回同一个 key，不覆盖频道和消息", k1 == k2 and m["channel_id"] == 10 and m["message_id"] == 99)
    check("新登记的比赛是 open", m["status"] == pr.OPEN and s.by_match_id("m1")["key"] == k1)


def test_find_and_url():
    s = pr.PredictionStore(":memory:")
    tour = "Overwatch Champions Series/2026/North America/Stage 3/Regular Season#Week 2"
    first = {"id": f"{tour}|{START}|Team Liquid|The Kafe", "start": START, "team1": "Team Liquid",
             "team2": "The Kafe", "label": "OWCS NA"}
    key = s.register(first, 10, 99, "https://discord.com/channels/1/10/99")
    check("登记时保存消息链接", s.get(key)["url"] == "https://discord.com/channels/1/10/99")
    moved = dict(first, id=f"{tour}|{START + 600}|Team Liquid|The Kafe", start=START + 600)
    check("改期 10 分钟、旧 ID 已不在列表：找到原来的竞猜", s.find(moved, {moved["id"]})["key"] == key)
    check("旧 ID 还在列表上（重赛）：不算同一场", s.find(moved, {moved["id"], first["id"]}) is None)
    far = dict(first, id=f"{tour}|{START + 7 * 3600}|Team Liquid|The Kafe", start=START + 7 * 3600)
    check("相隔 6 小时以上：不算同一场", s.find(far, {far["id"]}) is None)
    check("ID 完全相同直接找到", s.find(first)["key"] == key)


def test_pick():
    s = pr.PredictionStore(":memory:")
    key = s.register(match(), 10, 99)
    r = s.pick(key, 1, "Alice", 1, START - 60)
    check("押注成功", r.ok and not r.changed and s.counts(key) == (1, 0))
    r = s.pick(key, 1, "Alice", 2, START - 30)
    check("改选：票数跟着变，标记 changed", r.ok and r.changed and s.counts(key) == (0, 1))
    r = s.pick(key, 1, "Alice", 2, START - 20)
    check("押同一边不算改选", r.ok and not r.changed)
    r = s.pick(key, 2, "Bob", 1, START)
    check("开赛时刻起拒绝", not r.ok and "closed" in r.reason and s.counts(key) == (0, 1))
    check("不存在的比赛", not s.pick("deadbeef0000", 3, "C", 1, START - 60).ok)
    check("非法选项", not s.pick(key, 4, "D", 3, START - 60).ok)
    s.pick(key, 5, "Eve", 1, START - 10)
    check("名单按押注时间排序", s.names(key) == {1: ["Eve"], 2: ["Alice"]})


def test_lock():
    s, key = store_with([(1, 1), (2, 2), (3, 1)])
    names = s.lock(key)
    check("截止返回双方名单", names == {1: ["u1", "u3"], 2: ["u2"]} and s.get(key)["status"] == pr.LOCKED)
    check("重复截止返回 None", s.lock(key) is None)
    check("截止后不能再押", not s.pick(key, 4, "u4", 1, START - 50).ok)
    check("active 包含 locked 的比赛", [m["key"] for m in s.active()] == [key])


def test_scoring():
    check("少于 3 人：猜中只得 1 分", pr.points_for(0.5, 2) == 1.0 and pr.points_for(0.0001, 2) == 1.0)
    check("10 人 3 人押中：1.8 分", pr.points_for(0.3, 10) == 1.8)
    check("过半押中没有加成", pr.points_for(0.6, 10) == 1.0 and pr.points_for(0.5, 10) == 1.0)
    check("上限 3 分", pr.points_for(0.05, 20) == 2.8 and pr.points_for(0.0, 20) == 3.0)

    picks = [(i, 1) for i in range(1, 8)] + [(i, 2) for i in range(8, 11)]   # 7 押队1，3 押队2
    s, key = store_with(picks)
    preview = s.score_preview(key, 2)
    check("预览：押中冷门 3 人各 1.8 分，猜错 0 分",
          sorted(p.points for p in preview if p.correct) == [1.8, 1.8, 1.8]
          and all(p.points == 0 for p in preview if not p.correct) and s.get(key)["status"] == pr.OPEN)
    lines = s.resolve(key, 2, START + 7200)
    check("结算结果与预览一致并写库", [(l.user_id, l.points) for l in lines] == [(p.user_id, p.points) for p in preview]
          and s.get(key)["status"] == pr.RESOLVED and s.get(key)["winner"] == 2)
    check("重复结算不重复计分", s.resolve(key, 2, START + 9000) == []
          and s.leaderboard(0, START * 2)[0]["points"] == 1.8)
    empty_store, empty_key = store_with([])
    check("没人押：结算返回空列表", empty_store.resolve(empty_key, 1, START + 1) == []
          and empty_store.get(empty_key)["status"] == pr.RESOLVED)


def test_void():
    s, key = store_with([(1, 1)])
    check("作废", s.void(key) and s.get(key)["status"] == pr.VOID)
    check("作废后不能结算、也不在 active 里", s.resolve(key, 1, START + 1) == [] and s.active() == [])
    check("已作废再作废返回 False", not s.void(key))


def test_leaderboard_and_titles():
    s = pr.PredictionStore(":memory:")
    week = START
    # 第 1 场：u1、u2、u3 押队1，u4 押队2，队2 赢（u4 冷门 1/4 → 2.0）
    k1 = s.register(match("a"), 10, 1)
    for uid, side in [(1, 1), (2, 1), (3, 1), (4, 2)]:
        s.pick(k1, uid, f"u{uid}", side, START - 100)
    s.resolve(k1, 2, week + 10)
    # 第 2、3 场：大家都押队1，队1 赢
    for mid in ("b", "c"):
        k = s.register(match(mid), 10, 1)
        for uid in (1, 2, 3, 4):
            s.pick(k, uid, f"u{uid}", 1, START - 100)
        s.resolve(k, 1, week + 20)
    # 范围外的一场
    k4 = s.register(match("d"), 10, 1)
    s.pick(k4, 1, "u1", 1, START - 100)
    s.resolve(k4, 1, week + 10_000)

    board = s.leaderboard(week, week + 1000)
    check("周榜只统计时间范围内", sum(r["played"] for r in board) == 12)
    check("按总分排序：u4 4.0 第一", board[0]["name"] == "u4" and board[0]["points"] == 4.0
          and board[0]["correct"] == 3 and board[0]["played"] == 3)
    check("同分按猜中场数再按名字", [r["name"] for r in board[1:]] == ["u1", "u2", "u3"])
    t = s.titles(week, week + 1000)
    check("称号：预言家 u4、冷门猎手 u4 (+2.0)、最惨预言家 u1（2/3）",
          t["prophet"]["name"] == "u4" and t["underdog"]["user_name"] == "u4" and t["underdog"]["points"] == 2.0
          and t["worst"]["name"] == "u1")
    check("称号文字", pr.format_titles(t) ==
          "🔮 Prophet of the week: u4 · 🎯 Underdog hunter: u4 (+2) · 🙈 Worst prophet: u1 (2/3)")
    check("没有数据时称号为空", pr.format_titles(s.titles(0, 1)) is None)
    perfect = pr.PredictionStore(":memory:")
    for mid in ("p", "q", "r"):
        k = perfect.register(match(mid), 10, 1)
        perfect.pick(k, 1, "u1", 1, START - 100)
        perfect.resolve(k, 1, week + 10)
    check("全部猜中的人不会被评为最惨预言家", perfect.titles(week, week + 1000)["worst"] is None)
    lines = pr.format_board("🏆 Week", board)
    check("榜单文字", lines[1] == "1. u4 — 4 pts (3/3)" and len(lines) == 5)
    check("空榜单提示", pr.format_board("x", []) == ["x", "No resolved predictions yet."])


def test_seasons_and_file():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "predictions.db")
        s = pr.PredictionStore(path)
        check("默认赛季从 0 开始", s.season_start() == 0)
        s.new_season(START)
        check("开新赛季", s.season_start() == START)
        key = s.register(match(), 10, 99)
        s.pick(key, 1, "Alice", 1, START - 60)
        s.close()
        again = pr.PredictionStore(path)
        check("重启后数据还在", again.counts(key) == (1, 0) and again.season_start() == START)
        again.close()


if __name__ == "__main__":
    test_key_and_register()
    test_find_and_url()
    test_pick()
    test_lock()
    test_scoring()
    test_void()
    test_leaderboard_and_titles()
    test_seasons_and_file()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
