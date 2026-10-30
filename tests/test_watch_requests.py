"""一起看「想看但没有」记录：每日限额、同一天同一部去重、持久化、坏文件、上限、合并统计。

运行：python tests/test_watch_requests.py
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

import watch_requests as wr  # noqa: E402

results = []
SYD = ZoneInfo("Australia/Sydney")


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def ts(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=SYD).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def test_quota_and_dedupe():
    clock = Clock(ts(2026, 10, 25))
    s = wr.RequestStore(None, limit=3, clock=clock)
    check("初始剩余 = 上限", s.remaining(1) == 3)
    r = s.add("绝命毒师", wr.KIND_NOT_LISTED, 1, "小明", 555)
    check("第一次 ok 并扣一次", r.status == "ok" and r.remaining == 2 and s.remaining(1) == 2)
    r = s.add("绝命  毒师 ", wr.KIND_NO_RESULTS, 1, "小明", 555)
    check("同一天同一部（规范化后相同）算重复、不扣", r.status == "duplicate" and s.remaining(1) == 2
          and len(s.all()) == 1)
    r = s.add("绝命毒师", wr.KIND_NOT_LISTED, 2, "小红", 555)
    check("别人提同一部照常记", r.status == "ok" and len(s.all()) == 2 and s.remaining(2) == 2)
    s.add("权力的游戏", wr.KIND_NOT_LISTED, 1, "小明", 555)
    s.add("老友记", wr.KIND_NO_RESULTS, 1, "小明", 556)
    r = s.add("生活大爆炸", wr.KIND_NOT_LISTED, 1, "小明", 555)
    check("用完 3 次 → limit", r.status == "limit" and r.remaining == 0 and len(s.all()) == 4)
    r = s.add("绝命毒师", wr.KIND_NOT_LISTED, 1, "小明", 555)
    check("用完后重复的仍提示重复", r.status == "duplicate")
    clock.t = ts(2026, 10, 26, 0, 1)
    check("悉尼 0 点后次数重置", s.remaining(1) == 3)
    r = s.add("绝命毒师", wr.KIND_NOT_LISTED, 1, "小明", 555)
    check("第二天同一部可以再提", r.status == "ok" and len(s.all()) == 5)
    try:
        s.add("x", "bogus", 1, "a", 1)
        check("未知 kind 抛 ValueError", False)
    except ValueError:
        check("未知 kind 抛 ValueError", True)
    try:
        s.add("   ", wr.KIND_NOT_LISTED, 1, "a", 1)
        check("空片名抛 ValueError", False)
    except ValueError:
        check("空片名抛 ValueError", True)
    check("记录字段", s.all()[0].query == "绝命毒师" and s.all()[0].user_name == "小明"
          and s.all()[0].channel_id == 555 and s.all()[0].at == ts(2026, 10, 25)
          and s.all()[0].key == "绝命毒师")


def test_persistence():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sub" / "watch_requests.json"
        clock = Clock(ts(2026, 10, 25))
        s = wr.RequestStore(p, limit=5, clock=clock)
        s.add("绝命毒师", wr.KIND_NOT_LISTED, 1, "小明", 555)
        s.add("老友记", wr.KIND_NO_RESULTS, 1, "小明", 555)
        raw = json.loads(p.read_text(encoding="utf-8"))
        check("写到文件（建目录）", raw["date"] == "2026-10-25" and raw["counts"] == {"1": 2}
              and [r["query"] for r in raw["requests"]] == ["绝命毒师", "老友记"])
        s2 = wr.RequestStore(p, limit=5, clock=clock)
        check("重新读取：次数和记录都在", s2.remaining(1) == 3 and len(s2.all()) == 2
              and s2.all()[1].kind == wr.KIND_NO_RESULTS)
        p.write_text("{not json", encoding="utf-8")
        s3 = wr.RequestStore(p, limit=5, clock=clock)
        check("坏文件按空处理", s3.all() == [] and s3.remaining(1) == 5)
        p.write_text(json.dumps({"date": "2026-10-25", "counts": {"1": 1},
                                 "requests": [{"query": "a", "kind": "weird", "user_id": 1, "user_name": "x",
                                               "channel_id": 1, "at": 1.0},
                                              {"query": "b", "kind": "no_results", "user_id": 1, "user_name": "x",
                                               "channel_id": 1, "at": 2.0}]}), encoding="utf-8")
        s4 = wr.RequestStore(p, clock=clock)
        check("未知 kind 的记录跳过", [r.query for r in s4.all()] == ["b"])
        check("文件不存在 → 空", wr.RequestStore(Path(d) / "none.json").all() == [])


def test_cap_and_since():
    clock = Clock(ts(2026, 10, 25))
    s = wr.RequestStore(None, limit=10 ** 6, clock=clock)
    old_max = wr.MAX_ENTRIES
    wr.MAX_ENTRIES = 5
    try:
        for i in range(8):
            clock.t += 1
            s.add(f"片{i}", wr.KIND_NOT_LISTED, 1, "a", 1)
        check("超过上限只留最新", [r.query for r in s.all()] == [f"片{i}" for i in range(3, 8)])
    finally:
        wr.MAX_ENTRIES = old_max
    check("since 按时间过滤", [r.query for r in s.since(2.5)] == ["片5", "片6", "片7"])


def test_group_and_text():
    reqs = [wr.Request("绝命毒师", wr.KIND_NOT_LISTED, 1, "小明", 5, 10.0),
            wr.Request("绝命  毒师", wr.KIND_NO_RESULTS, 2, "小红", 5, 30.0),
            wr.Request("老友记", wr.KIND_NO_RESULTS, 1, "小明", 6, 20.0),
            wr.Request("绝命毒师", wr.KIND_NOT_LISTED, 3, "小刚", 7, 25.0)]
    groups = wr.group_requests(reqs)
    check("按规范化片名合并、最近的在前", [g.title for g in groups] == ["绝命毒师", "老友记"]
          and groups[0].count == 3 and groups[0].by_kind == {wr.KIND_NOT_LISTED: 2, wr.KIND_NO_RESULTS: 1}
          and groups[0].users == ["小明", "小红", "小刚"] and groups[0].first_at == 10.0 and groups[0].last_at == 30.0)
    check("clean_query 压空白截 50 字", wr.clean_query("  a \n b\t c ") == "a b c" and len(wr.clean_query("x" * 80)) == 50)
    check("文案", wr.recorded_text("绝命毒师", 4, 5) == "已记下「绝命毒师」，管理员会看到。你今天还能提 4 次（每天最多 5 次）。"
          and wr.duplicate_text("老友记") == "你今天已经提过「老友记」了。"
          and wr.limit_text(5) == "你今天已经提了 5 次，明天再来吧。")
    check("默认每天 5 次", wr.DAILY_LIMIT == 5 and wr.KIND_LABEL[wr.KIND_NO_RESULTS] == "没搜到")


if __name__ == "__main__":
    test_quota_and_dedupe()
    test_persistence()
    test_cap_and_since()
    test_group_and_text()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
