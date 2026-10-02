# 克制关系以统计数据为准 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 换英雄建议和英雄问答里的克制关系只引用 counterwatch.gg 的对位评分，不再凭模型常识断言。

**Architecture:**
- **抓取和缓存**：`server/matchups.py` 每天抓取 counterwatch 的英雄页面，解析内嵌的 `counterScoreData`，缓存到 `herodata` 卷（`MatchupStore`）。
- **换英雄建议**：`analyzer.build_matchup_data` 用代码算好当前对位和换人候选，作为 `matchup_data` 交给模型，系统提示要求克制说法必须引用其中的数字。
- **英雄问答**：新增 `get_matchups` 工具。
- **接入**：`bot.py` 负责创建、传参和起刷新任务。

**Tech Stack:** Python 3.12（生产镜像），aiohttp，anthropic SDK。测试沿用仓库风格：`python tests/test_xxx.py`，自带 `check()`。本机 Python 3.13 装不上 `rapidocr-onnxruntime`，所以**所有测试都在生产镜像里跑**（命令见各步骤）。

**Spec:** `docs/superpowers/specs/2026-10-03-counter-matchups-design.md`

**分支：** `counter-matchups`。它基于 `fix-qa-thinking`（PR #5），因为两者都改 `hero_qa.py`。PR #5 合并后，再把本分支 rebase 到 `main`。

## Global Constraints

- **评分的含义**：counterwatch 对位评分是对决和团战结果，全段位，去掉英雄本身强弱，**不是整局胜率**。只要向模型或用户展示，就要附带这句说明（`matchups.SOURCE`）。
- **方向约定**：`scores[a][b]` 是 a 对 b 的评分，正数 = a 占优，约等于百分点。只存每个页面的 `targets`；反向取对方页面的 `targets`。
- **名字归一化**：一律用 `herodata._norm`（`SOLDIER76` / `Soldier: 76` → `soldier76`，`Torbjörn` → `torbjorn`）。
- **克制规则**：克制说法只能来自数据并引用数字；没有数据的对位不做断言。地图、站位等其他推理仍可用常识。
- **抓取**：读 `https://www.counterwatch.gg/sitemap.xml` 得到英雄页面列表，串行抓取，间隔 1 秒（`herodata.REQUEST_INTERVAL`），User-Agent 用 `herodata.USER_AGENT`。
- **刷新与缓存**：每 `HERO_REFRESH_HOURS` 小时刷新一次；缓存是英雄数据缓存同目录下的 `matchups.json`。不新增环境变量。
- **快照校验**：新快照要满足英雄数 ≥ 旧的 90%，且平均对位数 ≥ 旧的 90%；无旧快照时非空即可。
- **换人候选**：同职责、不含队友已选的英雄、至少有 1 对数据，按合计降序取前 3。
- **代码风格**：注释用中文，风格与现有 `server/*.py` 一致。

## Review Focus

1. **counterwatch 改版或页面缺数据**：某些页面没有 `counterScoreData`，或 JSON 结构变了。应当跳过这些页面；整体快照明显变少时保留旧数据，绝不能用一个残缺的快照把好数据覆盖掉。→ Task 1「没有 counterScoreData 的页面返回 None」「JSON 损坏返回 None」「平均对位数明显下降不接受」。
2. **识别不全的阵容**：有人未选英雄、认不出、已阵亡，或者 counterwatch 上没有这个英雄（Doctrine）。建议仍要正常给出，只是不谈这些对位；没识别出的英雄列进 `enemies_without_data`。→ Task 2「没识别和没数据的敌人列出」「敌方没有已知英雄时返回 None」。
3. **"可能"的英雄**：只有 `last_seen_hero` 的玩家也参与计算，但必须标 `likely`，提示词要求模型说明这一点。→ Task 2「认不出时用 last_seen_hero 并标 likely」。
4. **候选合计的可比性**：候选英雄缺某一对数据时，合计少加一项（例如 Winston 对自己没有数据）。结果里必须带上 `pairs_with_data`，让模型能看出依据不完整。→ Task 2「当前对位：… pairs_with_data == 2」。目前没有对缺数据做加权修正，这是已知限制。
5. **问答里的英雄名**：对位数据的键是归一化名，回答中必须换回显示名，否则会出现 `soldier76` 这种写法。→ Task 3「单个英雄：最克制谁、最怕谁，用显示名」。

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `server/matchups.py` | 新建 | sitemap/页面解析、`MatchupStore`（缓存/刷新/查询）、`fetch_all` |
| `server/analyzer.py` | 修改 | `roles_from_roster`、`build_matchup_data`、`Analyzer(matchups, roles)`、新的提示规则 |
| `server/hero_qa.py` | 修改 | `get_matchups` 工具和提示规则 |
| `server/bot.py` | 修改 | 创建 `MatchupStore`、调整 `Analyzer` 的创建顺序、第二个刷新任务 |
| `tests/fixtures/counterwatch/{zarya,winston}.html`、`sitemap.xml` | **已提交** | 2026-10-03 抓取的真实页面（数据日期 Oct 2, 2026）和精简的 sitemap |
| `tests/test_matchups.py` | 新建 | 数据层测试 |
| `tests/test_counter_advice.py` | 新建 | `build_matchup_data` 和 `advise` 的 payload 测试 |
| `tests/test_units.py`、`tests/test_hero_qa.py` | 修改 | 更新旧断言，加 `get_matchups` 测试 |
| `README.md`、`docs/ARCHITECTURE.md` | 修改 | 文档 |

counterwatch 的页面每天都会变，没法按版本重新下载，所以 fixture 已经随计划一起提交，执行时**不要重新抓取覆盖它们**。

### 共享数据结构

```python
# MatchupStore.scores（同时也是缓存文件 matchups.json 里的 "scores"）
{"zarya": {"winston": {"score": 7.11, "type": "pressure", "users": 552}, ...}, ...}

# build_matchup_data 的返回值（作为 matchup_data 交给模型）
{
  "source": "counterwatch.gg counter ratings: ..., updated Oct 2, 2026",
  "unit": "positive = first hero favored; roughly percentage points (+7.1 ≈ +7%)",
  "current": [{"ally": "Zarya", "vs_enemies_total": 14.7, "pairs_with_data": 2,
               "pairs": [{"enemy": "Winston", "score": 7.1}, ...], "likely": True  # 仅当只有 last_seen_hero 时
             }],
  "swap_candidates": [{"ally": "Zarya", "role": "tank", "current_total": 14.7,
                       "best": [{"hero": "Roadhog", "vs_enemies_total": 11.0, "pairs_with_data": 2}]}],
  "enemies_without_data": ["unknown", "Doctrine"],
  "likely_heroes": ["Zarya"],
}
```

---

### Task 1: 对位数据层 `matchups.py`

**Files:**
- Create: `server/matchups.py`
- Create: `tests/test_matchups.py`
- Use (already committed): `tests/fixtures/counterwatch/zarya.html`, `winston.html`, `sitemap.xml`

**Interfaces:**
- Consumes: `herodata._norm`、`herodata.USER_AGENT`、`herodata.REQUEST_INTERVAL`
- Produces:
  - `SOURCE: str`、`UNIT: str`
  - `hero_urls(sitemap_xml: str) -> list[str]`
  - `parse_page(html: str) -> tuple[list[dict], str | None] | None`
  - `page_scores(targets: list[dict]) -> dict[str, dict]`
  - `class MatchupStore(path: str)`：`.scores`、`.fetched_at`、`.source_updated`、`.ready`、`.load() -> bool`、`.save()`、`.accept(new: dict) -> bool`、`async .refresh(fetch) -> bool`、`.score(a: str, b: str) -> dict | None`、`.profile(hero: str, top: int = 5) -> dict | None`（`{"strong_against": [...], "weak_against": [...]}`，每项 `{"hero": <归一化名>, "score", "type", "users"}`，只含评分为正的项）
  - `async fetch_all(interval: float = REQUEST_INTERVAL) -> {"scores": ..., "source_updated": ...}`

- [ ] **Step 1: 写失败的测试**

`tests/test_matchups.py`：

```python
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


if __name__ == "__main__":
    test_sitemap()
    test_parse_page()
    test_store_queries()
    test_accept_and_refresh()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_matchups.py`
Expected: `ModuleNotFoundError: No module named 'matchups'`

- [ ] **Step 3: 实现 `server/matchups.py`**

```python
"""英雄对位（克制）数据：从 counterwatch.gg 抓取、缓存到本地 JSON、定时刷新。

评分含义（counterwatch 页面原文）：来自社区对局的对决和团战结果，去掉了英雄本身的强弱，
不是整局胜率；全段位；至少 50 名玩家才展示；每天更新。
scores[a][b] 是 a 对 b 的评分，正数 = a 占优，约等于百分点（+7.1 ≈ +7%）。

每个英雄页面内嵌 "counterScoreData":{"threats":[...],"targets":[...]}，targets 是本页英雄对
各对手的评分；threats 是同一组数的反面（等于对手页面上 targets 的值）。只存 targets。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from typing import Any

import aiohttp

from herodata import REQUEST_INTERVAL, USER_AGENT, _norm

log = logging.getLogger("mrmeeseeks.matchups")

SITE = "https://www.counterwatch.gg"
SITEMAP_URL = SITE + "/sitemap.xml"
SOURCE = ("counterwatch.gg counter ratings: duel and teamfight outcomes from community matches, "
          "all ranks, hero strength removed; not match win rate")
UNIT = "positive = first hero favored; roughly percentage points (+7.1 ≈ +7%)"
MIN_HERO_RATIO = 0.9    # 新快照英雄数至少是旧快照的 90%
MIN_PAIR_RATIO = 0.9    # 平均每个英雄的对位数至少是旧快照的 90%
_HERO_URL = re.compile(r"<loc>(https://www\.counterwatch\.gg/stats/overwatch/heroes/[a-z0-9-]+)</loc>")
_UPDATED = re.compile(r"last updated ([A-Z][a-z]+ \d{1,2}, \d{4})")
_MARKER = '"counterScoreData":'


def hero_urls(sitemap_xml: str) -> list[str]:
    """sitemap 里的英雄页面地址，去重并保持顺序。"""
    return list(dict.fromkeys(_HERO_URL.findall(sitemap_xml)))


def parse_page(html: str) -> tuple[list[dict[str, Any]], str | None] | None:
    """英雄页面 -> (targets 列表, 页面上的更新日期)。找不到对位数据时返回 None。"""
    text = html.replace('\\"', '"')   # Next.js flight 数据里的引号是转义过的
    i = text.find(_MARKER)
    if i < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text, i + len(_MARKER))
    except ValueError:
        return None
    targets = data.get("targets") if isinstance(data, dict) else None
    if not isinstance(targets, list):
        return None
    updated = _UPDATED.search(text)
    return targets, updated.group(1) if updated else None


def page_scores(targets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {_norm(t["heroRawName"]): {"score": round(float(t["counterScore"]), 2),
                                      "type": t.get("counterType"), "users": t.get("distinctUsers")}
            for t in targets if t.get("heroRawName") and t.get("counterScore") is not None}


def _pair_count(scores: dict[str, dict]) -> float:
    return sum(len(v) for v in scores.values()) / len(scores) if scores else 0.0


class MatchupStore:
    def __init__(self, path: str):
        self.path = path
        self.scores: dict[str, dict[str, dict[str, Any]]] = {}
        self.fetched_at: str | None = None
        self.source_updated: str | None = None

    @property
    def ready(self) -> bool:
        return bool(self.scores)

    # ---------- 缓存 ----------
    def load(self) -> bool:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            scores, fetched_at, updated = data["scores"], data["fetched_at"], data.get("source_updated")
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.info("No usable matchup cache at %s (%r)", self.path, e)
            return False
        self.scores, self.fetched_at, self.source_updated = scores, fetched_at, updated
        log.info("Loaded matchups for %d heroes from cache (fetched %s)", len(scores), fetched_at)
        return True

    def save(self) -> None:
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": self.fetched_at, "source_updated": self.source_updated,
                       "scores": self.scores}, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ---------- 刷新 ----------
    def accept(self, new: dict[str, Any]) -> bool:
        scores = new.get("scores") or {}
        if not scores:
            return False
        if not self.scores:
            return True
        if len(scores) < MIN_HERO_RATIO * len(self.scores):
            return False
        return _pair_count(scores) >= MIN_PAIR_RATIO * _pair_count(self.scores)

    async def refresh(self, fetch) -> bool:
        try:
            new = await fetch()
        except Exception:
            log.exception("Matchup refresh failed; keeping the current snapshot")
            return False
        if not self.accept(new):
            log.warning("Rejected matchup snapshot: %d heroes (%.1f pairs each) vs current %d (%.1f)",
                        len(new.get("scores") or {}), _pair_count(new.get("scores") or {}),
                        len(self.scores), _pair_count(self.scores))
            return False
        self.scores, self.source_updated = new["scores"], new.get("source_updated")
        self.fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            self.save()
        except OSError:  # 缓存写不进去也继续用内存里的新数据
            log.exception("Could not write matchup cache to %s", self.path)
        log.info("Matchups refreshed: %d heroes", len(self.scores))
        return True

    # ---------- 查询 ----------
    def score(self, a: str, b: str) -> dict[str, Any] | None:
        """a 对 b 的评分（正数 = a 占优）；没有数据时返回 None。"""
        return self.scores.get(_norm(a), {}).get(_norm(b))

    def profile(self, hero: str, top: int = 5) -> dict[str, list[dict[str, Any]]] | None:
        """hero 最克制谁（它的评分最高）、最怕谁（对手对它的评分最高）。"""
        key = _norm(hero)
        if key not in self.scores:
            return None
        strong = sorted(self.scores[key].items(), key=lambda kv: -kv[1]["score"])
        weak = sorted(((opp, row[key]) for opp, row in self.scores.items() if key in row),
                      key=lambda kv: -kv[1]["score"])
        return {"strong_against": [{"hero": k, **v} for k, v in strong[:top] if v["score"] > 0],
                "weak_against": [{"hero": k, **v} for k, v in weak[:top] if v["score"] > 0]}


async def fetch_all(interval: float = REQUEST_INTERVAL) -> dict[str, Any]:
    """抓取全部英雄页面。单个页面失败或没有对位数据只记日志并跳过。"""
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=timeout) as session:
        async with session.get(SITEMAP_URL) as resp:
            resp.raise_for_status()
            urls = hero_urls(await resp.text())
        scores: dict[str, dict] = {}
        updated = None
        for url in urls:
            await asyncio.sleep(interval)
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    parsed = parse_page(await resp.text())
            except Exception:
                log.exception("Failed to fetch matchup page %s", url)
                continue
            if parsed is None:
                log.warning("No counterScoreData on %s", url)
                continue
            targets, page_updated = parsed
            scores[_norm(url.rsplit("/", 1)[1])] = page_scores(targets)
            updated = updated or page_updated
        return {"scores": scores, "source_updated": updated}
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_matchups.py`
Expected: `28/28 passed`（日志里 "counterwatch down" 和 "File exists: …/blocker" 的 traceback 是预期的，分别来自"抓取失败"和"缓存不可写"两个用例）

- [ ] **Step 5: 联网冒烟测试（一次即可，约 1 分钟）**

```bash
cd server && ../.venv/bin/python -c "
import asyncio, statistics, matchups
d = asyncio.run(matchups.fetch_all()); s = d['scores']
print(len(s), d['source_updated'], min(len(v) for v in s.values()), statistics.median(len(v) for v in s.values()))
st = matchups.MatchupStore('x'); st.scores = s
print(st.score('Zarya', 'Winston'), st.score('Winston', 'Zarya'))
"; cd ..
```

Expected：第一行类似 `53 Oct 2, 2026 52 52`（数量和日期会随网站更新变化）；第二行两个评分互为相反数。只要 `.venv` 里有 aiohttp 就能跑，不需要 rapidocr。

- [ ] **Step 6: Commit**

```bash
git add server/matchups.py tests/test_matchups.py
git commit -m "Add counterwatch matchup data store"
```

---

### Task 2: 换英雄建议使用对位数据（`analyzer.py`）

**Files:**
- Modify: `server/analyzer.py`
- Modify: `tests/test_units.py`（把"允许用通用克制知识"改成检查新规则；`_analyzer` 辅助函数设置 `matchups`/`roles`）
- Create: `tests/test_counter_advice.py`

**Interfaces:**
- Consumes: Task 1 的 `MatchupStore`（`.ready`、`.scores`、`.score`、`.source_updated`）、`matchups.SOURCE`、`matchups.UNIT`
- Produces:
  - `Roles = dict[str, tuple[str, str]]`
  - `roles_from_roster(roster: dict[str, list[tuple[str, str]]]) -> Roles`
  - `build_matchup_data(facts: dict, matchups: MatchupStore | None, roles: Roles, top: int = 3) -> dict | None`
  - `Analyzer(api_key, model, effort="low", matchups=None, roles=None)`

- [ ] **Step 1: 写失败的测试**

新建 `tests/test_counter_advice.py`：

```python
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
    test_advise_payload()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
```

再把下面的补丁应用到 `tests/test_units.py`（保存为 `/tmp/units.diff`，然后执行 `git apply /tmp/units.diff`）：

```diff
--- a/tests/test_units.py
+++ b/tests/test_units.py
@@ -48,6 +48,7 @@
 def _analyzer(create, effort="low"):
     a = analyzer.Analyzer.__new__(analyzer.Analyzer)
     a.model, a.effort = "test-model", effort
+    a.matchups, a.roles = None, {}
     a.client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
     return a
 
@@ -68,7 +69,7 @@
     sent = calls[0]["messages"][0]["content"]
     check("不再发送 not available 占位数据（模型会因此拒绝给建议）", "not available" not in sent)
     check("没有'缺信息就不给建议'的规则", "Not enough data to give advice" not in calls[0]["system"])
-    check("允许用通用克制知识", "general" in calls[0]["system"].lower())
+    check("克制关系只能来自 matchup_data，不再凭常识", 'may only come from "matchup_data"' in calls[0]["system"])
     check("effort 传给 API", calls[0]["output_config"] == {"effort": "low"})
     asyncio.run(_analyzer(create, effort=None).advise(FACTS))
     check("effort 为 None 时不传 output_config", "output_config" not in calls[1])
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_counter_advice.py`
Expected: `AttributeError: module 'analyzer' has no attribute 'roles_from_roster'`

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_units.py`
Expected: `FAIL 克制关系只能来自 matchup_data，不再凭常识`，最后一行 `25/26 passed`

- [ ] **Step 3: 修改 `server/analyzer.py`**

应用下面的补丁（保存为 `/tmp/analyzer.diff`，然后 `git apply /tmp/analyzer.diff`）：

```diff
--- a/server/analyzer.py
+++ b/server/analyzer.py
@@ -1,6 +1,7 @@
 """给换英雄建议：只看 vision 识别出的结构化 facts（纯文本），不看图。
 
 识别见 vision/；这里要求每条建议注明依据，数据不足就直说。
+克制关系只来自 counterwatch 对位数据（matchups.py）：代码先算好当前对位和换人候选，模型只负责组织语言。
 """
 from __future__ import annotations
 
@@ -9,10 +10,14 @@
 
 from anthropic import AsyncAnthropic
 
+import matchups as mu
+from herodata import _norm
+
 ADVISE_SYSTEM = """You are mrmeeseeks, an Overwatch hero-swap advisor for the player's team (the "allies").
 The data below is what was recognized from the in-game scoreboard. Rules:
 1. Use only the facts provided below for the current game state (map, mode, side, stage, heroes). Do not invent facts about this game.
-2. Use your general knowledge of Overwatch heroes, counters and map positions to reason about those facts.
+2. Counter relationships (which hero is favored against which) may only come from "matchup_data", and you must quote its number, e.g. (basis: Zarya vs Winston +7.1, counterwatch). If there is no matchup_data, or a pair is not in it, do not claim that one hero counters another. For everything else (map, positions, objective, team composition) use your general knowledge of Overwatch.
+2b. When recommending a swap, prefer heroes from "swap_candidates". If a candidate's total is within 3 of the current hero's total, say the difference is small. Heroes listed in "likely_heroes" are probable, not certain; say so if you rely on them.
 3. Always give your best recommendation with whatever is known. Some heroes may be unknown: reason around them and mention them in a few words at most; never refuse to advise just because some data is missing.
 4. A player with status "unknown" and a "last_seen_hero" was most recently seen on that hero; treat it as likely but not certain. A "dead" player with a hero listed is still playing that hero.
 5. After each reason, cite its basis in parentheses, e.g. (basis: enemy comp Winston + Tracer).
@@ -30,16 +35,90 @@
     return "".join(b.text for b in resp.content if b.type == "text")
 
 
+Roles = dict[str, tuple[str, str]]   # 归一化英雄名 -> (职责, 显示名)
+
+
+def roles_from_roster(roster: dict[str, list[tuple[str, str]]]) -> Roles:
+    """label_ui.build_roster 的结果 {role: [(key, name)]} -> {归一化名: (role, name)}。"""
+    return {_norm(name): (role, name) for role, heroes in roster.items() for _, name in heroes}
+
+
+def _team(people: list[dict[str, Any]]) -> list[tuple[str | None, bool]]:
+    """每个玩家 -> (英雄显示名, 是否只是"可能")。阵亡但记得的英雄算确定；认不出时用上次见到的英雄。"""
+    out = []
+    for p in people:
+        if p.get("hero"):
+            out.append((p["hero"], False))
+        elif p.get("last_seen_hero"):
+            out.append((p["last_seen_hero"], True))
+        else:
+            out.append((None, False))
+    return out
+
+
+def build_matchup_data(facts: dict[str, Any], matchups: mu.MatchupStore | None, roles: Roles,
+                       top: int = 3) -> dict[str, Any] | None:
+    """当前对局的对位评分和换人候选；没有可用的对位数据时返回 None（这时不给模型 matchup_data）。"""
+    if matchups is None or not matchups.ready:
+        return None
+    allies = _team(facts.get("allies") or [])
+    enemies = _team(facts.get("enemies") or [])
+    known = [h for h, _ in enemies if h and _norm(h) in matchups.scores]
+    if not known:
+        return None
+
+    def against_enemies(hero: str) -> tuple[float, list[dict[str, Any]]]:
+        pairs = [{"enemy": e, "score": s["score"]} for e in known if (s := matchups.score(hero, e))]
+        return round(sum(p["score"] for p in pairs), 2), pairs
+
+    taken = {_norm(h) for h, _ in allies if h}
+    current, swaps = [], []
+    for hero, likely in allies:
+        if not hero:
+            continue
+        total, pairs = against_enemies(hero)
+        current.append({"ally": hero, "vs_enemies_total": total, "pairs_with_data": len(pairs), "pairs": pairs,
+                        **({"likely": True} if likely else {})})
+        role = roles.get(_norm(hero), (None, None))[0]
+        if role is None:
+            continue
+        candidates = []
+        for key, (r, name) in roles.items():
+            if r != role or key in taken:
+                continue
+            c_total, c_pairs = against_enemies(name)
+            if c_pairs:
+                candidates.append({"hero": name, "vs_enemies_total": c_total, "pairs_with_data": len(c_pairs)})
+        candidates.sort(key=lambda c: -c["vs_enemies_total"])
+        swaps.append({"ally": hero, "role": role, "current_total": total, "best": candidates[:top]})
+
+    updated = f", updated {matchups.source_updated}" if matchups.source_updated else ""
+    return {
+        "source": mu.SOURCE + updated,
+        "unit": mu.UNIT,
+        "current": current,
+        "swap_candidates": swaps,
+        "enemies_without_data": [h or "unknown" for h, _ in enemies if not h or _norm(h) not in matchups.scores],
+        "likely_heroes": [h for h, likely in allies + enemies if h and likely],
+    }
+
+
 class Analyzer:
-    def __init__(self, api_key: str, model: str, effort: str | None = "low"):
+    def __init__(self, api_key: str, model: str, effort: str | None = "low",
+                 matchups: mu.MatchupStore | None = None, roles: Roles | None = None):
         self.client = AsyncAnthropic(api_key=api_key)
         self.model = model
         self.effort = effort   # 纯文本推理，low 足够且快；None 为模型默认
+        self.matchups = matchups
+        self.roles = roles or {}
 
     async def advise(self, facts: dict[str, Any]) -> str:
         # 以后接入 OverFast 队友生涯数据、地图分段对照表时加到这里；没有的数据不要放占位，
         # 否则模型会以"缺数据"为由拒绝给建议
-        payload = {"screen_facts": facts}
+        payload: dict[str, Any] = {"screen_facts": facts}
+        matchup_data = build_matchup_data(facts, self.matchups, self.roles)
+        if matchup_data:
+            payload["matchup_data"] = matchup_data
         extra = {"output_config": {"effort": self.effort}} if self.effort else {}
         resp = await self.client.messages.create(
             model=self.model,
```

改动要点：
- 第 2 条规则改为"克制关系只能来自 `matchup_data` 并引用数字"，新增 2b（优先参考 `swap_candidates`，差距 3 以内要说明差别不大；`likely` 的英雄要说"可能"）。第 3 条"数据不全也要给建议"保持不变。
- `build_matchup_data` 是纯函数；`advise` 只有在它返回非 `None` 时才在 payload 里加 `matchup_data`。

- [ ] **Step 4: 运行测试，确认通过**

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_counter_advice.py`
Expected: `20/20 passed`

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_units.py`
Expected: `26/26 passed`

- [ ] **Step 5: Commit**

```bash
git add server/analyzer.py tests/test_counter_advice.py tests/test_units.py
git commit -m "Ground hero-swap counter claims in counterwatch matchup data"
```

---

### Task 3: 英雄问答 `get_matchups` 工具（`hero_qa.py`）

**Files:**
- Modify: `server/hero_qa.py`
- Modify: `tests/test_hero_qa.py`

**Interfaces:**
- Consumes: Task 1 的 `MatchupStore`（`.ready`、`.score`、`.profile`、`.source_updated`）、`matchups.SOURCE`、`matchups.UNIT`；`HeroStore.resolve`、`HeroStore.heroes`
- Produces:
  - `HeroQA(client, model, store, max_turns=MAX_TURNS, effort="low", matchups=None)`
  - 新工具 `get_matchups`，输入 `hero`、可选 `opponent`。
    - 给出 `opponent` 时返回 `{"hero", "opponent", "hero_vs_opponent", "opponent_vs_hero", "source", "unit", "source_updated"}`。
    - 只给 `hero` 时返回 `{"hero", "strong_against", "weak_against", "source", "unit", "source_updated"}`，其中英雄名都是显示名。
    - 错误：`{"error": "unknown_hero", ...}`、`{"error": "no_matchup_data", ...}`、`{"error": "matchup data is not available yet"}`。

- [ ] **Step 1: 写失败的测试**

应用补丁（保存为 `/tmp/qa_test.diff`，然后 `git apply /tmp/qa_test.diff`）：

```diff
--- a/tests/test_hero_qa.py
+++ b/tests/test_hero_qa.py
@@ -15,6 +15,7 @@
 from hero_qa import TOO_COMPLEX, TRUNCATED, TOOLS, Cooldown, HeroQA  # noqa: E402
 from herodata import HeroStore, load_aliases  # noqa: E402
 from heroparse import parse_hero  # noqa: E402
+from matchups import MatchupStore  # noqa: E402
 
 FIXTURES = os.path.join(ROOT, "tests", "fixtures", "wiki")
 results = []
@@ -125,6 +126,37 @@
     check("工具说明要求英雄名按原话传、不要翻译", "exactly as the user wrote" in desc)
 
 
+def make_matchups():
+    m = MatchupStore("unused.json")
+    m.scores = {"zarya": {"winston": {"score": 7.11, "type": "pressure", "users": 552},
+                          "tracer": {"score": -2.0, "type": None, "users": 600}},
+                "winston": {"zarya": {"score": -7.11, "type": None, "users": 579}},
+                "reinhardt": {"zarya": {"score": 3.5, "type": "duel", "users": 400}}}
+    m.source_updated = "Oct 2, 2026"
+    return m
+
+
+def test_matchups_tool():
+    store = make_store()
+    for name in ("Zarya", "Winston"):   # 只需要能被名字解析到
+        store.heroes[name] = {**store.heroes["Tracer"], "name": name}
+    qa = HeroQA(None, "m", store, matchups=make_matchups())
+    pair = qa.run_tool("get_matchups", {"hero": "Zarya", "opponent": "Winston"})
+    check("一对英雄：双向评分", pair["hero_vs_opponent"]["score"] == 7.11 and pair["opponent_vs_hero"]["score"] == -7.11)
+    check("附来源说明（不是整局胜率）和更新日期", "not match win rate" in pair["source"]
+          and pair["source_updated"] == "Oct 2, 2026")
+    prof = qa.run_tool("get_matchups", {"hero": "Zarya"})
+    check("单个英雄：最克制谁、最怕谁，用显示名", prof["strong_against"][0]["hero"] == "Winston"
+          and [e["hero"] for e in prof["weak_against"]] == ["Reinhardt"])
+    check("中文名也能查", qa.run_tool("get_matchups", {"hero": "查莉娅", "opponent": "温斯顿"})["hero"] == "Zarya")
+    check("未知英雄返回候选", qa.run_tool("get_matchups", {"hero": "xyz"})["error"] == "unknown_hero")
+    check("这一对没有数据", qa.run_tool("get_matchups", {"hero": "Tracer", "opponent": "Moira"})["error"]
+          == "no_matchup_data")
+    check("英雄没有对位数据", qa.run_tool("get_matchups", {"hero": "Moira"})["error"] == "no_matchup_data")
+    check("对位数据未就绪", "not available" in HeroQA(None, "m", make_store()).run_tool(
+        "get_matchups", {"hero": "Zarya"})["error"])
+
+
 def test_loop():
     client = ScriptedClient([
         NS(stop_reason="tool_use", content=[
@@ -135,8 +167,8 @@
     answer = asyncio.run(HeroQA(client, "test-model", make_store()).answer("卡西迪爆头几枪杀猎空？"))
     check("循环结束后返回最终文本", answer == "2 发爆头即可击杀猎空。")
     first = client.requests[0]
-    check("请求带 system 和 3 个工具", "system" in first and [t["name"] for t in first["tools"]] ==
-          ["get_hero_stats", "get_patch_history", "shots_to_kill"])
+    check("请求带 system 和 4 个工具", "system" in first and [t["name"] for t in first["tools"]] ==
+          ["get_hero_stats", "get_patch_history", "shots_to_kill", "get_matchups"])
     second = client.requests[1]["messages"]
     check("第二轮带上 assistant 的 tool_use 和 user 的 tool_result",
           second[1]["role"] == "assistant" and second[2]["role"] == "user")
@@ -239,6 +271,7 @@
     test_default_weapon()
     test_thinking_budget()
     test_tool_schema()
+    test_matchups_tool()
     test_loop()
     test_cooldown()
     test_routing()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_hero_qa.py`
Expected: `TypeError: HeroQA.__init__() got an unexpected keyword argument 'matchups'`

- [ ] **Step 3: 修改 `server/hero_qa.py`**

应用补丁（保存为 `/tmp/qa.diff`，然后 `git apply /tmp/qa.diff`）：

```diff
--- a/server/hero_qa.py
+++ b/server/hero_qa.py
@@ -7,7 +7,8 @@
 from typing import Any
 
 import damage
-from herodata import HeroStore
+import matchups as mu
+from herodata import HeroStore, _norm
 
 log = logging.getLogger("mrmeeseeks.hero_qa")
 
@@ -26,7 +27,8 @@
 5. Pass hero and weapon names to tools exactly as the user wrote them; never translate them yourself, the tools resolve names in any language. If a tool returns candidates for an unclear hero name, ask the user which hero they meant. Do not guess. When naming a weapon in your answer, use the name from the tool result.
 6. If a tool returns "unsupported", explain why. Time-to-kill, healing numbers, ability damage (non-weapon) and perk damage bonuses are not supported yet.
 7. Answer in the same language as the question, in at most about 150 words.
-8. End with one line: "Source: Overwatch Wiki, data fetched <fetched_at date>"."""
+8. End with one line: "Source: Overwatch Wiki, data fetched <fetched_at date>"; for matchup answers use "Source: counterwatch.gg, updated <source_updated>" instead.
+9. For counter or matchup questions ("who counters X", "how does X do against Y"), call get_matchups and quote its numbers. Explain that they measure duel and teamfight outcomes, not match win rate. Never state a counter relationship that get_matchups did not return."""
 
 _HERO = {"type": "string", "description": "Hero name exactly as the user wrote it, in any language "
                                           "(e.g. Cassidy, 卡西迪, McCree). Do not translate it."}
@@ -59,6 +61,16 @@
             "variant": {"type": "integer", "description": "Index into the weapon's damage variants"},
         }, "required": ["attacker", "target"]},
     },
+    {
+        "name": "get_matchups",
+        "description": "Counter ratings from counterwatch.gg (duel and teamfight outcomes, all ranks, not match win rate). "
+                       "With only hero: who it counters most and who counters it most. With opponent: both directions "
+                       "for that pair. Positive score = first hero favored, roughly percentage points.",
+        "input_schema": {"type": "object", "properties": {
+            "hero": _HERO,
+            "opponent": {**_HERO, "description": "Optional opponent hero, " + _HERO["description"]},
+        }, "required": ["hero"]},
+    },
 ]
 
 
@@ -88,12 +100,13 @@
 
 class HeroQA:
     def __init__(self, client: Any, model: str, store: HeroStore, max_turns: int = MAX_TURNS,
-                 effort: str | None = "low"):
+                 effort: str | None = "low", matchups: mu.MatchupStore | None = None):
         self.client = client
         self.model = model
         self.store = store
         self.max_turns = max_turns
         self.effort = effort   # 问答是短对话，low 足够且快；None 为模型默认
+        self.matchups = matchups
 
     # ---------- 工具 ----------
     def _hero(self, name: str) -> dict[str, Any]:
@@ -143,8 +156,35 @@
             if "unsupported" in result:  # 让模型能换一把武器重试
                 result["weapons"] = [w["name"] for w in attacker["weapons"]]
             return {**result, "fetched_at": self.store.fetched_at}
+        if name == "get_matchups":
+            return self._matchups(args)
         return {"error": f"unknown tool {name}"}
 
+    def _matchups(self, args: dict[str, Any]) -> dict[str, Any]:
+        if self.matchups is None or not self.matchups.ready:
+            return {"error": "matchup data is not available yet"}
+        hero = self._hero(args["hero"])
+        if "error" in hero:
+            return hero
+        info = {"source": mu.SOURCE, "unit": mu.UNIT, "source_updated": self.matchups.source_updated}
+        if args.get("opponent"):
+            opp = self._hero(args["opponent"])
+            if "error" in opp:
+                return opp
+            ab, ba = self.matchups.score(hero["name"], opp["name"]), self.matchups.score(opp["name"], hero["name"])
+            if ab is None and ba is None:
+                return {"error": "no_matchup_data", "hero": hero["name"], "opponent": opp["name"]}
+            return {"hero": hero["name"], "opponent": opp["name"],
+                    "hero_vs_opponent": ab, "opponent_vs_hero": ba, **info}
+        profile = self.matchups.profile(hero["name"])
+        if profile is None:
+            return {"error": "no_matchup_data", "hero": hero["name"]}
+        names = {_norm(n): n for n in self.store.heroes}   # 对位数据的键是归一化名，回答里用显示名
+        for side in profile.values():
+            for entry in side:
+                entry["hero"] = names.get(entry["hero"], entry["hero"])
+        return {"hero": hero["name"], **profile, **info}
+
     # ---------- 对话循环 ----------
     async def answer(self, question: str) -> str:
         messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_hero_qa.py`
Expected: `54/54 passed`

- [ ] **Step 5: Commit**

```bash
git add server/hero_qa.py tests/test_hero_qa.py
git commit -m "Add get_matchups tool to hero Q&A"
```

---

### Task 4: 接入 bot、文档与线上验证

**Files:**
- Modify: `server/bot.py`
- Modify: `README.md`、`docs/ARCHITECTURE.md`

**Interfaces:**
- Consumes: `matchups.MatchupStore`、`matchups.fetch_all`（Task 1）；`analyzer.roles_from_roster`、`Analyzer(..., matchups=, roles=)`（Task 2）；`HeroQA(..., matchups=)`（Task 3）；`herodata.refresh_loop(store, hours, fetch=...)`（已有）
- Produces: 启动时创建 `matchup_store`，传给 `Analyzer` 和 `HeroQA`，并起两个刷新任务。

这一步是启动时的接线（`main()` 需要 Discord Token），没有单元测试，由完整测试套件和 Step 5 的线上验证覆盖。

- [ ] **Step 1: 修改 `server/bot.py`**

应用补丁（保存为 `/tmp/bot.diff`，然后 `git apply /tmp/bot.diff`）：

```diff
--- a/server/bot.py
+++ b/server/bot.py
@@ -27,7 +27,8 @@
 from dotenv import load_dotenv
 
 import overfast
-from analyzer import Analyzer, format_facts
+import matchups
+from analyzer import Analyzer, format_facts, roles_from_roster
 from hero_qa import Cooldown, HeroQA
 from herodata import HeroStore, load_aliases, refresh_loop
 from label_ui import Roster, build_roster, send_prompts
@@ -377,11 +378,6 @@
     api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
     model = os.environ.get("CLAUDE_MODEL", "").strip() or "claude-sonnet-5"
     effort = os.environ.get("ADVISE_EFFORT", "low").strip() or None
-    analyzer = Analyzer(api_key, model, effort) if api_key else None
-    if analyzer is None:
-        log.warning("ANTHROPIC_API_KEY not set; screenshots will be recognized but no advice given")
-    else:
-        log.info("Advice model: %s (effort %s)", model, effort or "default")
 
     state_dir = Path(os.environ.get("STATE_DIR", DATA_DIR.parent / "state"))
     matcher = HeroMatcher([DATA_DIR / "templates", state_dir / "templates"])
@@ -401,7 +397,17 @@
     store = HeroStore(os.environ.get("HERO_DATA_PATH", "").strip() or os.path.join(server_dir, "herodata", "heroes.json"),
                       load_aliases())
     store.load()
-    hero_qa = HeroQA(AsyncAnthropic(api_key=api_key), model, store, effort=effort) if api_key else None
+    # 对位数据和英雄数据放在同一个目录（Docker 里是 herodata 卷）
+    matchup_store = matchups.MatchupStore(os.path.join(os.path.dirname(os.path.abspath(store.path)), "matchups.json"))
+    matchup_store.load()
+    hero_qa = (HeroQA(AsyncAnthropic(api_key=api_key), model, store, effort=effort, matchups=matchup_store)
+               if api_key else None)
+    analyzer = (Analyzer(api_key, model, effort, matchups=matchup_store, roles=roles_from_roster(roster))
+                if api_key else None)
+    if analyzer is None:
+        log.warning("ANTHROPIC_API_KEY not set; screenshots will be recognized but no advice given")
+    else:
+        log.info("Advice model: %s (effort %s)", model, effort or "default")
     refresh_hours = float(os.environ.get("HERO_REFRESH_HOURS", "24"))
 
     registry = Registry()
@@ -415,7 +421,8 @@
 
     async with bot:
         await ws.start()
-        refresher = asyncio.create_task(refresh_loop(store, refresh_hours))
+        refreshers = [asyncio.create_task(refresh_loop(store, refresh_hours)),
+                      asyncio.create_task(refresh_loop(matchup_store, refresh_hours, fetch=matchups.fetch_all))]
         try:
             await bot.start(token)
         except discord.LoginFailure:
@@ -423,7 +430,8 @@
         except discord.PrivilegedIntentsRequired:
             raise SystemExit("Discord refused the connection: enable Message Content Intent on the Bot page of the Developer Portal.")
         finally:
-            refresher.cancel()
+            for task in refreshers:
+                task.cancel()
             await ws.close()
 
 
```

改动要点：
- `Analyzer` 的创建从 roster 构建之前移到之后，以便传入 `roles_from_roster(roster)`。
- `matchups.json` 放在英雄数据缓存同一目录（Docker 里是 `/app/herodata`，已经挂了卷）。
- 起两个 `refresh_loop` 任务，退出时都取消。

- [ ] **Step 2: 文档**

应用两个补丁（分别保存后 `git apply`）：

```diff
--- a/README.md
+++ b/README.md
@@ -328,7 +328,7 @@
 | `WS_PORT` | 否 | `8765` | WebSocket 端口 |
 | `MIN_SNAPSHOT_INTERVAL` | 否 | `5` | 同一客户端两次分析的最短间隔（秒） |
 | `HERO_DATA_PATH` | 否 | `server/herodata/heroes.json` | 英雄数据缓存文件。Docker 部署时位于 `herodata` 卷 |
-| `HERO_REFRESH_HOURS` | 否 | `24` | 每隔多少小时从 Overwatch Wiki 刷新英雄数据 |
+| `HERO_REFRESH_HOURS` | 否 | `24` | 每隔多少小时刷新英雄数据（Overwatch Wiki）和对位数据（counterwatch.gg）；对位数据缓存在同一目录的 `matchups.json` |
 
 ### 客户端参数
 
@@ -361,7 +361,7 @@
 | `@mrmeeseeks player Name#1234` | 用 [OverFast](https://overfast-api.tekrop.fr/) 查玩家各职责段位、总体数据和最常玩的 5 个英雄。BattleTag 区分大小写；生涯设为私密时只能看到段位 |
 | `@mrmeeseeks disconnect` | 解绑本频道的客户端 |
 | `@mrmeeseeks help` | 显示帮助 |
-| `@mrmeeseeks <问题>` | 英雄问答：数值（血量、子弹体积）、最近的补丁、N 米处几枪击杀。例如 `@mrmeeseeks 卡西迪最近被削了吗`、`@mrmeeseeks 卡西迪 30 米爆头几枪杀毛加`。需要 `ANTHROPIC_API_KEY` |
+| `@mrmeeseeks <问题>` | 英雄问答：数值（血量、子弹体积）、最近的补丁、N 米处几枪击杀。例如 `@mrmeeseeks 卡西迪最近被削了吗`、`@mrmeeseeks 卡西迪 30 米爆头几枪杀毛加`、`@mrmeeseeks 查莉娅怕谁`。克制关系来自 [counterwatch.gg](https://www.counterwatch.gg) 的对位评分（对决和团战结果，全段位，不是整局胜率）。需要 `ANTHROPIC_API_KEY` |
 
 ### 配对规则
 
@@ -384,6 +384,8 @@
 python tests/test_damage.py    # 伤害衰减、护甲、几枪击杀
 python tests/test_herodata.py  # 英雄数据缓存、刷新、名字解析
 python tests/test_hero_qa.py   # 英雄问答工具与对话循环、bot 路由
+python tests/test_matchups.py  # counterwatch 对位数据解析、缓存、查询
+python tests/test_counter_advice.py # 换英雄建议里的对位评分和换人候选
 ```
 
 每个脚本最后一行显示 `N/N passed`，全部通过时退出码为 0。
```

```diff
--- a/docs/ARCHITECTURE.md
+++ b/docs/ARCHITECTURE.md
@@ -13,7 +13,8 @@
 | `server/heroparse.py` | 把 Overwatch Wiki 英雄页面的 wikitext 解析成规范化 dict（纯函数） |
 | `server/herodata.py` | 英雄数据：从 Wiki 抓取、缓存到 JSON、定时刷新、按中英文名查英雄 |
 | `server/damage.py` | 伤害衰减、护甲/护盾、爆头倍率、几枪击杀（纯函数） |
-| `server/hero_qa.py` | 英雄问答：Claude tool use 循环，数字只来自上面两个模块 |
+| `server/hero_qa.py` | 英雄问答：Claude tool use 循环，数字只来自上面两个模块和 `matchups.py` |
+| `server/matchups.py` | 英雄对位（克制）数据：从 counterwatch.gg 抓取、缓存、每日刷新 |
 | `client/client.py` | 本地客户端：连接/重连、配对码输入、Tab 监听、两段截图、上传 |
 
 ## 客户端状态
@@ -67,12 +68,21 @@
 1. **识别**（`Analyzer.extract`）：输入两张图，输出固定格式的 JSON：地图、模式、攻防、阶段、双方阵容，每项带置信度，看不清就填 `null`。
 2. **建议**（`Analyzer.advise`）：**不给模型看图**，只给第 1 步的 JSON 和 `context` 里的补充数据。系统提示词要求每条建议注明依据，置信度低于 0.6 视为未知，数据不足直接说。
 
+### 克制关系
+
+建议里的"谁克谁"只来自 counterwatch.gg 的对位评分，不用模型常识。
+
+- `matchups.MatchupStore` 每天抓取 counterwatch 的 53 个英雄页面，解析页面内嵌的 `counterScoreData`。评分是对决和团战结果，全段位，去掉了英雄本身强弱，不是整局胜率；正数表示前一个英雄占优，约等于百分点。
+- `analyzer.build_matchup_data` 用当前识别出的阵容算出每个我方英雄对敌方各英雄的评分和合计，以及同职责的换人候选（按合计排序，取前 3），作为 `matchup_data` 交给模型。计算由代码完成，模型只负责组织语言。
+- 系统提示要求克制说法必须引用 `matchup_data` 里的数字；没有数据的对位不做克制断言。
+- 对局中按 Tab（`snapshot_received`）和 `@mrmeeseeks analyze` 都走 `Analyzer.advise`，都会带上对位数据。
+
 ## 英雄问答
 
 `@mrmeeseeks` 后面跟的文本如果不是 connect/status/disconnect/help，就交给 `HeroQA.answer`。
 
 1. **数据**：`herodata.fetch_all` 通过 MediaWiki API 抓取 `Category:Heroes` 下每个英雄的页面，`heroparse.parse_hero` 解析 infobox（血量/护甲/护盾/副职业）、`Ability_details` 中的武器、`ChangelogsTabber` 的 `owpvp` 补丁。快照写到 `HERO_DATA_PATH`；新快照明显变少（Wiki 改版）时不替换旧的。
-2. **工具**：`get_hero_stats`、`get_patch_history`、`shots_to_kill`。英雄名在工具内解析，不唯一时返回候选，由模型反问。
+2. **工具**：`get_hero_stats`、`get_patch_history`、`shots_to_kill`、`get_matchups`（counterwatch 对位评分）。英雄名在工具内解析，不唯一时返回候选，由模型反问。
 3. **计算**：`damage.shots_to_kill` 逐发模拟 护盾 → 护甲 → 生命值，规则常量和 Wiki 出处写在 `damage.py` 顶部。
 4. **约束**：系统提示要求所有数字来自工具结果、引用补丁原文和日期、写出假设（弹丸全中、Role Queue 等）。
 
```

- [ ] **Step 3: 运行完整测试**

Run:

```bash
docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test sh -c 'pip install -q -r client/requirements.txt >/dev/null 2>&1; for t in tests/test_*.py; do printf "%s: " $t; python $t 2>/dev/null | tail -1; done'
```

Expected：10 个文件全部 `N/N passed`，依次为 `test_counter_advice` 20/20、`test_damage` 37/37、`test_e2e` 34/34、`test_hero_qa` 54/54、`test_herodata` 32/32、`test_heroparse` 36/36、`test_matchups` 28/28、`test_overfast` 28/28、`test_units` 26/26、`test_vision` 67/67。

- [ ] **Step 4: Commit**

```bash
git add server/bot.py README.md docs/ARCHITECTURE.md
git commit -m "Load and refresh counterwatch matchups for advice and Q&A"
```

- [ ] **Step 5: 部署后验证（需要用户批准部署；分支合并到 main 之后才部署）**

部署后在服务器上检查：

```bash
ssh -i ~/.ssh/jobseeker_deploy deploy@5.223.69.203 "cd /opt/mrmeeseeks && timeout 180 docker compose logs -f --since 5m 2>&1 | grep -m2 -E 'Matchups refreshed|Rejected matchup|Matchup refresh failed'"
```

Expected：约 1 分钟内出现 `Matchups refreshed: 53 heroes`。然后请用户在 Discord 里试：
1. 对局中按 Tab：建议里的克制说法带 "(basis: X vs Y +N, counterwatch)"。
2. `@mrmeeseeks 查莉娅怕谁` / `@mrmeeseeks 温斯顿打查莉娅怎么样`：引用评分，说明不是整局胜率。

## 已知限制（不在本计划内）

- 候选英雄缺某一对数据时，合计少加一项，没有做加权修正；结果里有 `pairs_with_data` 可以看出来。
- 对位数据只有全段位；没有整局胜率口径的对位数据。
