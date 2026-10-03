"""赛事数据解析测试：tests/fixtures/esports/ 下是 2026-10-03 保存的 Liquipedia:Matches（API parse 结果）
和 esports.overwatch.com 首页。

运行：python tests/test_esports.py
"""
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import esports  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "esports")
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def page():
    with open(os.path.join(FIXTURES, "liquipedia_matches.json"), encoding="utf-8") as f:
        return json.load(f)["parse"]["text"]


def find(matches, team1, team2):
    return next(m for m in matches if m["team1"] == team1 and m["team2"] == team2)


def test_parse_matches():
    all_matches = esports.parse_matches(page())
    check("解析出全部 100 场（50 场即将进行 + 50 场已结束）", len(all_matches) == 100)
    check("id 唯一", len({m["id"] for m in all_matches}) == 100)
    m = find(all_matches, "T1", "ZETA DIVISION")
    check("即将进行的比赛字段", m == {
        "id": "Overwatch Champions Series/2026/Asia/Stage 3/Korea/Regular Season#Week 1|1791012600|T1|ZETA DIVISION",
        "start": 1791012600,
        "tournament": "Overwatch Champions Series/2026/Asia/Stage 3/Korea/Regular Season#Week 1",
        "label": "OWCS Korea Stage 3 - Regular Season - Week 1",
        "team1": "T1", "team2": "ZETA DIVISION", "best_of": 5,
        "finished": False, "score": None,
        "url": "https://liquipedia.net/overwatch/Overwatch_Champions_Series/2026/Asia/Stage_3/Korea/Regular_Season#Week_1"})
    done = find(all_matches, "Team Falcons", "ZETA DIVISION")
    check("已结束的比赛带比分", done["finished"] is True and done["score"] == [0, 3])
    vty = find(all_matches, "Vivacity", "WAY2THANUS")
    check("队伍页不存在时去掉 '(page does not exist)'", vty["score"] == [2, 3])
    check("TBD 队伍保留", any(x["team1"] == "TBD" and x["team2"] == "TBD" for x in all_matches))
    check("坏块被跳过、不抛异常", esports.parse_matches('<div class="match-info"><span>broken</span>') == [])
    check("空页面返回空列表", esports.parse_matches("") == [])


def test_official_filter():
    off = esports.official(esports.parse_matches(page()))
    check("只保留 OWCS / 世界杯（26 场）", len(off) == 26)
    check("全部是 OWCS 赛事", all(m["tournament"].startswith("Overwatch Champions Series/") for m in off))
    check("FACEIT League 被过滤", not any("FACEIT" in m["tournament"] for m in off))
    check("世界杯前缀也算官方", esports.official([{"tournament": "Overwatch World Cup/2026/Finals"}]) != [])


def test_parse_news():
    with open(os.path.join(FIXTURES, "owesports_home.html"), encoding="utf-8") as f:
        news = esports.parse_news(f.read())
    check("首页新闻：链接、标题、日期", news[0] == {
        "url": "https://esports.overwatch.com/en-us/news/owwc-finals-at-blizzcon-viewers-guide",
        "title": "OWWC FINALS AT BLIZZCON VIEWERS GUIDE", "date": "9/9/2026"})
    check("取不到标题时为 None，链接仍保留", news[1] == {
        "url": "https://esports.overwatch.com/en-us/news/owcs-2025", "title": None, "date": None})
    check("同一链接去重", len(esports.parse_news(
        '<a href="https://esports.overwatch.com/en-us/news/a">x</a><a href="https://esports.overwatch.com/en-us/news/a">y</a>'
    )) == 1)


def test_fetch_page_changed():
    class FakeResp:
        def __init__(self, payload):
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        async def json(self, content_type=None):
            return self.payload

    class FakeSession:
        def __init__(self, payload):
            self.payload, self.calls = payload, []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url, params=None):
            self.calls.append((url, params))
            return FakeResp(self.payload)

    orig = esports._session
    try:
        changed = FakeSession({"parse": {"text": "<div>redesigned page without match blocks</div>"}})
        esports._session = lambda: changed
        try:
            asyncio.run(esports.fetch_matches())
            raised = False
        except esports.EsportsUnavailable:
            raised = True
        check("页面有内容却解析不出比赛：抛 EsportsUnavailable", raised)
        ok = FakeSession({"parse": {"text": page()}})
        esports._session = lambda: ok
        got = asyncio.run(esports.fetch_matches())
        check("fetch_matches 只返回官方赛事", len(got) == 26)
        check("请求 Liquipedia:Matches 的 parse 接口", ok.calls[0][1]["page"] == "Liquipedia:Matches"
              and ok.calls[0][1]["action"] == "parse")
    finally:
        esports._session = orig
    check("User-Agent 带项目地址、不含个人信息", "github.com/Anbrose/overwatch-mrmeeseeks" in esports.USER_AGENT
          and "@" not in esports.USER_AGENT)


if __name__ == "__main__":
    test_parse_matches()
    test_official_filter()
    test_parse_news()
    test_fetch_page_changed()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
