"""换英雄建议的对位数据：build_matchup_data 的计算，以及 advise 把它交给模型。

运行：python tests/test_counter_advice.py
"""
import asyncio
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import analyzer  # noqa: E402
from matchups import MatchupStore  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def store_with(pairs):
    """pairs: {(a, b): score}，自动补上反向 -score。"""
    s = MatchupStore("unused.json")
    for (a, b), v in pairs.items():
        s.scores.setdefault(a, {})[b] = {"score": v, "type": None, "users": 500}
        s.scores.setdefault(b, {})[a] = {"score": -v, "type": None, "users": 500}
    s.source_updated = "Oct 2, 2026"
    return s


STORE = store_with({
    ("zarya", "winston"): 7.1, ("zarya", "genji"): 7.6,
    ("roadhog", "winston"): 9.0, ("roadhog", "genji"): 2.0,
    ("dva", "winston"): -1.0, ("dva", "genji"): 4.0,
    ("ana", "winston"): -2.0, ("kiriko", "genji"): 3.0,
})
ROLES = analyzer.roles_from_roster({
    "tank": [("zarya", "Zarya"), ("roadhog", "Roadhog"), ("dva", "D.Va"), ("winston", "Winston")],
    "damage": [("genji", "Genji")],
    "support": [("ana", "Ana"), ("kiriko", "Kiriko")],
})


def p(hero=None, status="alive", **kw):
    return {"player": "x", "hero": hero, "status": status, **kw}


FACTS = {
    "map": {"name": "King's Row"}, "side": "Attack",
    "allies": [p("Zarya"), p("Ana")],
    "enemies": [p("Winston"), p("Genji"), p(None, "unknown"), p("Doctrine")],
}


def test_roles():
    check("roster -> 归一化名到 (职责, 显示名)", ROLES["dva"] == ("tank", "D.Va") and ROLES["zarya"] == ("tank", "Zarya"))


def test_build():
    d = analyzer.build_matchup_data(FACTS, STORE, ROLES)
    zarya = next(c for c in d["current"] if c["ally"] == "Zarya")
    check("当前对位：Zarya 对 Winston/Genji 合计 14.7", zarya["vs_enemies_total"] == 14.7 and zarya["pairs_with_data"] == 2)
    check("当前对位逐对列出", zarya["pairs"] == [{"enemy": "Winston", "score": 7.1}, {"enemy": "Genji", "score": 7.6}])
    swap = next(s for s in d["swap_candidates"] if s["ally"] == "Zarya")
    check("换人候选只看同职责、按合计降序", [c["hero"] for c in swap["best"]] == ["Roadhog", "D.Va"]
          and swap["best"][0]["vs_enemies_total"] == 11.0)
    check("当前英雄不在候选里", "Zarya" not in [c["hero"] for c in swap["best"]])
    check("候选里没有任何数据的英雄被排除（Winston 对自己无数据）", "Winston" not in [c["hero"] for c in swap["best"]])
    check("换人候选附当前英雄合计用于比较", swap["current_total"] == 14.7 and swap["role"] == "tank")
    check("没识别和没数据的敌人列出", d["enemies_without_data"] == ["unknown", "Doctrine"])
    check("来源写明 counterwatch、非整局胜率、更新日期", "counterwatch" in d["source"] and "not match win rate" in d["source"]
          and "Oct 2, 2026" in d["source"])
    ana_swap = next(s for s in d["swap_candidates"] if s["ally"] == "Ana")
    check("队友已占用的英雄不作为候选", all(c["hero"] != "Zarya" for c in ana_swap["best"]))
    check("Ana 的候选是 Kiriko", [c["hero"] for c in ana_swap["best"]] == ["Kiriko"])


def test_likely_and_missing():
    facts = {**FACTS, "allies": [p(None, "unknown", last_seen_hero="Zarya")],
             "enemies": [p("Winston"), p(None, "dead", hero_key=None)]}
    d = analyzer.build_matchup_data(facts, STORE, ROLES)
    check("认不出时用 last_seen_hero 并标 likely", d["current"][0]["ally"] == "Zarya" and d["current"][0]["likely"] is True
          and d["likely_heroes"] == ["Zarya"])
    remembered = {**FACTS, "allies": [p("Zarya", "dead", remembered=True)]}
    check("阵亡但记得的英雄算确定", "likely" not in analyzer.build_matchup_data(remembered, STORE, ROLES)["current"][0])
    check("敌方没有已知英雄时返回 None",
          analyzer.build_matchup_data({**FACTS, "enemies": [p(None, "unknown")]}, STORE, ROLES) is None)
    check("没有对位数据时返回 None", analyzer.build_matchup_data(FACTS, MatchupStore("x"), ROLES) is None
          and analyzer.build_matchup_data(FACTS, None, ROLES) is None)
    check("不认识职责的英雄仍有当前对位、只是没有候选",
          len(analyzer.build_matchup_data(FACTS, STORE, {})["current"]) == 2
          and analyzer.build_matchup_data(FACTS, STORE, {})["swap_candidates"] == [])


def test_no_data_for_current():
    roles = analyzer.roles_from_roster({
        "tank": [("zarya", "Zarya"), ("roadhog", "Roadhog"), ("dva", "D.Va"), ("winston", "Winston"), ("reinhardt", "Reinhardt")],
        "damage": [("genji", "Genji")],
        "support": [("ana", "Ana"), ("kiriko", "Kiriko")],
    })
    facts = {
        "map": {"name": "King's Row"}, "side": "Attack",
        "allies": [p("Reinhardt")],
        "enemies": [p("Winston"), p("Genji")],
    }
    d = analyzer.build_matchup_data(facts, STORE, roles)
    reinhardt_swap = next(s for s in d["swap_candidates"] if s["ally"] == "Reinhardt")
    check("当前英雄无对位数据时 current_total 为 None", reinhardt_swap["current_total"] is None)
    check("当前英雄无对位数据时 current_pairs_with_data 为 0", reinhardt_swap["current_pairs_with_data"] == 0)
    check("无对位数据的英雄仍有候选换人", len(reinhardt_swap["best"]) > 0)
    d_zarya = analyzer.build_matchup_data(FACTS, STORE, ROLES)
    zarya_swap = next((s for s in d_zarya["swap_candidates"] if s["ally"] == "Zarya"), None)
    check("既有数据的英雄有 current_pairs_with_data", zarya_swap is not None and zarya_swap["current_pairs_with_data"] == 2)


def test_advise_payload():
    calls = []

    async def create(**kw):
        calls.append(kw)
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="Swap to Roadhog")],
                                     stop_reason="end_turn")

    a = analyzer.Analyzer.__new__(analyzer.Analyzer)
    a.model, a.effort, a.matchups, a.roles = "m", "low", STORE, ROLES
    a.client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    asyncio.run(a.advise(FACTS))
    payload = json.loads(calls[0]["messages"][0]["content"].split("\n", 1)[1])
    check("advise 把 matchup_data 交给模型", payload["matchup_data"]["current"][0]["ally"] == "Zarya")
    check("系统提示：克制只能来自 matchup_data 并引用数字", 'may only come from "matchup_data"' in calls[0]["system"]
          and "Zarya vs Winston +7.1" in calls[0]["system"])
    check("系统提示：差距在 3 以内要说明差别不大", "within 3" in calls[0]["system"])
    a.matchups = MatchupStore("empty")
    asyncio.run(a.advise(FACTS))
    check("没有对位数据时不带 matchup_data", "matchup_data" not in calls[1]["messages"][0]["content"])


if __name__ == "__main__":
    test_roles()
    test_build()
    test_likely_and_missing()
    test_no_data_for_current()
    test_advise_payload()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
