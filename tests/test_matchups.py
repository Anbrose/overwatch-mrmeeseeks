"""对位数据测试：用 tests/fixtures/counterwatch/ 下保存的真实页面（2026-10-03 抓取，数据日期 Oct 2, 2026）。

运行：python tests/test_matchups.py
"""
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import matchups  # noqa: E402
from matchups import MatchupStore  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "counterwatch")
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return f.read()


def fixture_scores():
    out = {}
    for hero in ("zarya", "winston"):
        targets, _ = matchups.parse_page(read(hero + ".html"))
        out[hero] = matchups.page_scores(targets)
    return out


def test_sitemap():
    urls = matchups.hero_urls(read("sitemap.xml"))
    check("只取 overwatch 英雄页面，去重保序", urls == [
        "https://www.counterwatch.gg/stats/overwatch/heroes/winston",
        "https://www.counterwatch.gg/stats/overwatch/heroes/zarya",
        "https://www.counterwatch.gg/stats/overwatch/heroes/soldier76"])


def test_parse_page():
    targets, updated = matchups.parse_page(read("zarya.html"))
    check("Zarya 页面有 52 个对手", len(targets) == 52)
    check("页面更新日期", updated == "Oct 2, 2026")
    s = matchups.page_scores(targets)
    check("Zarya 对 Winston +7.11，样本 552", s["winston"] == {"score": 7.11, "type": "pressure", "users": 552})
    check("名字归一化：SOLDIER76 / JUNKERQUEEN / WRECKINGBALL",
          {"soldier76", "junkerqueen", "wreckingball", "dva", "torbjorn"} <= set(s))
    check("没有 counterScoreData 的页面返回 None", matchups.parse_page("<html>nothing</html>") is None)
    check("JSON 损坏返回 None", matchups.parse_page('"counterScoreData":{"targets":[{') is None)
    check("targets 不是列表返回 None", matchups.parse_page('"counterScoreData":{"targets":5}') is None)


def test_store_queries():
    store = MatchupStore("unused.json")
    store.scores = fixture_scores()
    check("正向：Zarya 对 Winston +7.11", store.score("Zarya", "Winston")["score"] == 7.11)
    check("反向一致：Winston 对 Zarya -7.11", store.score("Winston", "Zarya")["score"] == -7.11)
    check("显示名也能查：Soldier: 76", store.score("Zarya", "Soldier: 76") is not None)
    check("没有数据返回 None", store.score("Zarya", "Doctrine") is None and store.score("Doctrine", "Zarya") is None)
    p = store.profile("Zarya", top=3)
    strong = [e["score"] for e in p["strong_against"]]
    check("profile：最克制的按评分降序，且都为正", len(strong) == 3 and strong == sorted(strong, reverse=True)
          and strong[0] > 0)
    check("profile：最怕谁来自对手页面（只有 Winston 页面时就是 Winston，但它不克 Zarya）",
          p["weak_against"] == [])
    check("profile：没有该英雄返回 None", store.profile("Doctrine") is None)
    store.scores = {"genji": {"zarya": {"score": -7.6}}, "symmetra": {"zarya": {"score": 14.6}},
                    "zarya": {"genji": {"score": 7.6}, "symmetra": {"score": -14.6}}}
    check("profile：最怕谁 = 对手对它评分为正的，按评分降序",
          [e["hero"] for e in store.profile("Zarya")["weak_against"]] == ["symmetra"])


def test_accept_and_refresh():
    def snap(n, pairs=52):
        return {"scores": {f"h{i}": {f"o{j}": {"score": 1.0} for j in range(pairs)} for i in range(n)},
                "source_updated": "Oct 2, 2026"}
    store = MatchupStore("unused.json")
    check("空快照不接受", not store.accept({"scores": {}}))
    check("无旧快照时非空即接受", store.accept(snap(1)))
    store.scores = snap(53)["scores"]
    check("英雄数降到 90% 以下不接受", not store.accept(snap(47)))
    check("英雄数 90% 以上接受", store.accept(snap(48)))
    check("平均对位数明显下降不接受", not store.accept(snap(53, pairs=40)))

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "matchups.json")
        store = MatchupStore(path)
        check("没有缓存时 load 返回 False", store.load() is False and not store.ready)

        async def good():
            return {"scores": fixture_scores(), "source_updated": "Oct 2, 2026"}

        async def broken():
            raise RuntimeError("counterwatch down")

        check("刷新成功并写缓存", asyncio.run(store.refresh(good)) and os.path.exists(path) and store.fetched_at)
        check("目录里没有残留临时文件", os.listdir(os.path.dirname(path)) == ["matchups.json"])
        check("抓取失败保留旧数据", asyncio.run(store.refresh(broken)) is False and store.ready)
        fresh = MatchupStore(path)
        check("新进程从缓存加载", fresh.load() and fresh.score("Zarya", "Winston")["score"] == 7.11
              and fresh.source_updated == "Oct 2, 2026")
        blocker = os.path.join(d, "blocker")
        open(blocker, "w").close()
        unwritable = MatchupStore(os.path.join(blocker, "matchups.json"))
        check("缓存写不进去时仍更新内存数据", asyncio.run(unwritable.refresh(good)) and unwritable.ready)
        with open(path, "w") as f:
            f.write("{broken")
        check("缓存损坏时 load 返回 False", MatchupStore(path).load() is False)


def test_partial_refresh_and_derived():
    def row(v):
        return {"score": v, "type": None, "users": 500}
    full = {f"h{i}": {f"o{j}": row(1.0) for j in range(10)} for i in range(10)}
    store = MatchupStore("unused.json")
    store.scores = {k: dict(v) for k, v in full.items()}
    partial = {k: {"o0": row(9.0)} if k == "h0" else dict(v) for k, v in full.items() if k != "h9"}
    partial["h0"] = {f"o{j}": row(9.0) for j in range(10)}

    async def fetch():
        return {"scores": partial, "source_updated": "Oct 3, 2026"}
    with tempfile.TemporaryDirectory() as d:
        store.path = os.path.join(d, "m.json")
        check("部分抓取（缺 1/10 英雄）被接受", asyncio.run(store.refresh(fetch)))
    check("缺失英雄沿用旧行", "h9" in store.scores and store.scores["h9"]["o0"]["score"] == 1.0)
    check("新行优先于旧行", store.scores["h0"]["o0"]["score"] == 9.0)

    s = MatchupStore("unused.json")
    s.scores = {"zarya": {"winston": {"score": 7.114, "type": "pressure", "users": 552}}}
    d = s.score("winston", "zarya")
    check("缺失方向由反向取负得到并标记 derived", d == {"score": -7.11, "type": None, "users": 552, "derived": True})
    check("有直接数据时不标 derived", "derived" not in s.score("zarya", "winston"))
    check("两个方向都没有时仍是 None", s.score("winston", "tracer") is None)
    check("has: 顶层键", s.has("Zarya"))
    check("has: 只出现在别人的行里", s.has("Winston"))
    check("has: 完全没有", not s.has("Tracer"))


if __name__ == "__main__":
    test_sitemap()
    test_parse_page()
    test_store_queries()
    test_accept_and_refresh()
    test_partial_refresh_and_derived()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
