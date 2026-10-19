"""赛事报名阵容：解析（tests/fixtures/esports/liquipedia_owcs_korea_stage3.json，2026-10-18 保存的
OWCS 2026 Asia Stage 3 Korea 赛事页面 API parse 结果）、候选页面、缓存与限速。

运行：python tests/test_rosters.py
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import rosters  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "esports", "liquipedia_owcs_korea_stage3.json")
KOREA = "Overwatch Champions Series/2026/Asia/Stage 3/Korea"
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def page_html():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)["parse"]["text"]


def page_teams():
    return rosters.parse_rosters(page_html())


def players(members):
    return [m for m in members if m["role"] != "coach"]


def match(t1="T1", t2="ZETA DIVISION", tournament=KOREA + "/Regular Season#Week 1"):
    return {"team1": t1, "team2": t2, "tournament": tournament}


def test_parse():
    teams = page_teams()
    check("解析出 9 支参赛队", len(teams) == 9 and {"T1", "ZETA DIVISION", "Crazy Raccoon"} <= set(teams))
    check("T1 的 6 名队员和位置", players(teams["T1"]) == [
        {"id": "Proud", "role": "dps", "flag": "South Korea"}, {"id": "ZEST", "role": "dps", "flag": "South Korea"},
        {"id": "DONGHAK", "role": "tank", "flag": "South Korea"}, {"id": "Jasm1ne", "role": "tank", "flag": "South Korea"},
        {"id": "skewed", "role": "support", "flag": "South Korea"}, {"id": "Bliss", "role": "support", "flag": "South Korea"}])
    check("教练组单独标为 coach，主教练在前", [m["id"] for m in teams["T1"] if m["role"] == "coach"]
          == ["RUSH", "Fleta", "GgulTaek"])
    check("ZETA 的教练", [m["id"] for m in teams["ZETA DIVISION"] if m["role"] == "coach"] == ["Crusty", "Twilight"])
    logos = rosters.parse_logos(page_html())
    check("队徽：只有一种模式时用它，取 300px 缩略图",
          logos["T1"] == "https://liquipedia.net/commons/images/thumb/e/e4/T1_2019_allmode.png/300px-T1_2019_allmode.png")
    check("队徽：有深色模式时用深色模式",
          logos["ZETA DIVISION"] == "https://liquipedia.net/commons/images/thumb/9/95/ZETA_DIVISION_darkmode.png/300px-ZETA_DIVISION_darkmode.png")
    check("每支参赛队都有队徽", set(logos) == set(teams))
    check("没有参赛卡片的页面没有队徽", rosters.parse_logos("<div>no cards</div>") == {})
    check("全能位（Flex）识别", "flex" in [m["role"] for m in teams["Crazy Raccoon"]])
    check("没有参赛卡片的页面返回空", rosters.parse_rosters("<div>no cards</div>") == {})


def test_pages_and_format():
    check("候选页面：子页面、上一级、再上一级", rosters.roster_pages(KOREA + "/Regular Season#Week 1") == [
        KOREA + "/Regular Season", KOREA, "Overwatch Champions Series/2026/Asia/Stage 3"])
    check("候选页面最多 3 个", len(rosters.roster_pages("a/b/c/d/e/f")) == 3)
    check("阵容按位置分组，教练在最后", rosters.format_roster(page_teams()["T1"])
          == "🛡️ DONGHAK, Jasm1ne · ⚔️ Proud, ZEST · 💉 skewed, Bliss · 📋 RUSH, Fleta, GgulTaek")
    check("全能位单独一组", "🔀" in rosters.format_roster(page_teams()["Crazy Raccoon"]))
    check("没有阵容", rosters.format_roster(None) == "Roster unavailable" and rosters.format_roster([]) == "Roster unavailable")


class Clock:
    def __init__(self):
        self.t = 1_000_000.0
        self.slept = []

    def now(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_cache():
    clock = Clock()
    calls = []
    teams = page_teams()
    logos = rosters.parse_logos(page_html())

    async def fetch(page):
        calls.append(page)
        if page == KOREA:
            return {"teams": teams, "logos": logos}
        if page.endswith("Regular Season"):
            return {"teams": {}, "logos": {}}             # 子页面没有参赛卡片
        raise RuntimeError("liquipedia down")

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "rosters.json")
        cache = rosters.RosterCache(path, fetch, clock.now, clock.sleep)
        asyncio.run(cache.ensure([match()]))
        check("先找子页面，再找上一级；找到双方就停", calls == [KOREA + "/Regular Season", KOREA])
        check("两次抓取之间至少间隔 30 秒", clock.slept == [30.0])
        t1, t2 = cache.lookup(match())
        check("查到双方阵容", t1 == teams["T1"] and t2 == teams["ZETA DIVISION"])
        check("查到双方队徽地址", cache.logos(match()) == (logos["T1"], logos["ZETA DIVISION"]))
        check("找不到队徽返回 None", cache.logos(match("T1", "Nobody")) == (logos["T1"], None))
        asyncio.run(cache.ensure([match(), match("Crazy Raccoon", "O2 Blast")]))
        check("24 小时内不重复抓取", len(calls) == 2)
        check("写入缓存文件", os.path.exists(path))
        again = rosters.RosterCache(path, fetch, clock.now, clock.sleep)
        again.load()
        check("新进程从缓存读到阵容", again.lookup(match())[0] == teams["T1"])

        clock.t += rosters.CACHE_TTL + 1
        asyncio.run(cache.ensure([match()]))
        check("过期后重新抓取", calls[-2:] == [KOREA + "/Regular Season", KOREA])

        missing = match("T1", "Unknown Team")
        check("找不到的队伍返回 None", cache.lookup(missing) == (teams["T1"], None))

    failing_clock = Clock()
    broken = rosters.RosterCache(None, fetch, failing_clock.now, failing_clock.sleep)
    broken.pages = {"x/y": {"fetched_at": 0, "teams": {"A": [{"id": "a", "role": "tank", "flag": None}]}, "logos": {}}}

    async def boom(page):
        raise RuntimeError("down")
    broken.fetch = boom
    asyncio.run(broken.ensure([match("A", "B", tournament="x/y/z")]))
    check("抓取失败时保留旧缓存", broken.lookup(match("A", "B", tournament="x/y/z"))[0][0]["id"] == "a")

    old_clock = Clock()
    old_calls = []

    async def fetch_new(page):
        old_calls.append(page)
        return {"teams": teams, "logos": logos}
    old = rosters.RosterCache(None, fetch_new, old_clock.now, old_clock.sleep)
    old.pages = {KOREA: {"fetched_at": old_clock.t, "teams": teams}}       # 旧版缓存：没有队徽字段
    asyncio.run(old.ensure([match(tournament=KOREA)]))
    check("旧版缓存（没有队徽）视为过期，重新抓取", old_calls == [KOREA] and old.logos(match(tournament=KOREA))[0] == logos["T1"])


if __name__ == "__main__":
    test_parse()
    test_pages_and_format()
    test_cache()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
