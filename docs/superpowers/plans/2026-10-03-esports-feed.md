# Overwatch 赛事推送频道 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `@mrmeeseeks esports here` 指定的频道里，自动推送 OWCS 和世界杯的每日赛程预告（悉尼时间 10:00）、开赛前 15 分钟提醒、赛果（剧透遮罩）和官方新闻。

**Architecture:**
- **数据层**：`server/esports.py` 解析 Liquipedia `Liquipedia:Matches`（MediaWiki API parse）和 esports.overwatch.com 首页。
- **调度**：`server/esports_feed.py` 里的纯函数 `plan()` 决定发哪些消息，每条消息附带发出后要记的账；`EsportsFeed` 负责定时抓取、把状态持久化到 `herodata` 卷、调用 `plan` 并发送（发送成功后才记账）。
- **接入**：`bot.py` 新增 `esports here/off/status` 指令，并起一个后台推送任务。

**Tech Stack:** Python 3.12（生产镜像），aiohttp，`zoneinfo`，discord.py。测试沿用仓库风格：`python tests/test_xxx.py`，自带 `check()`。本机 Python 3.13 装不上 `rapidocr-onnxruntime`，所以**所有测试都在生产镜像里跑**（命令见各步骤）。

**Spec:** `docs/superpowers/specs/2026-10-03-esports-feed-design.md`

**分支：** `esports-feed`（从合并了 #5、#6 的 `main` 拉出）。

## Global Constraints

- **赛事范围**：只推赛事路径以 `Overwatch Champions Series/` 或 `Overwatch World Cup/` 开头的比赛（常量 `OFFICIAL_PREFIXES`）。
- **Liquipedia 条款**：
  - 请求必须开启 gzip。
  - User-Agent 固定为 `mrmeeseeks-discord-bot/1.0 (https://github.com/Anbrose/overwatch-mrmeeseeks)`，**不写任何个人信息**。
  - `action=parse` 每 30 秒最多 1 次，本功能每 10 分钟才抓一次。
  - 比赛相关消息都要注明 `Data: Liquipedia (CC-BY-SA)`。
- **时间规则**：
  - 时区 `Australia/Sydney`（`zoneinfo`，自动处理夏令时）。
  - 预告每天 10:00 发一次，内容是未来 24 小时的比赛；没有比赛就不发，但仍记下当天日期。
  - 提醒在开赛前 15 分钟发，开赛超过 5 分钟就不再补发。
  - 赛果只发最近 24 小时内开始的比赛。
  - 比赛每 10 分钟抓一次，新闻每 3 小时抓一次，调度每 60 秒检查一次。
- **剧透**：胜者和比分放在 `||…||` 里，队名和赛事正常显示。
- **首次启用**：已经结束的比赛和已有新闻只记账、不发送。比赛和新闻分开初始化，互不阻塞。
- **记账**：每条消息发送成功后才记账，失败的下一轮重试。没有设置频道时照常记账、不发送。
- **状态文件**：`esports_state.json`，和英雄数据缓存放在同一目录（Docker 里是 `herodata` 卷），用临时文件加 rename 原子写入，写失败只记日志。不新增环境变量。
- **权限**：`esports here/off` 要求发指令的人在该频道有 `manage_channels` 权限，私信里不能用；`status` 谁都能用。
- **消息**：推送不 ping 任何人（`AllowedMentions.none()`），单条不超过 1990 字符，预告超长时按行拆分。
- **代码风格**：注释用中文，风格与现有 `server/*.py` 一致。

## Review Focus

1. **Liquipedia 改版或请求失败**：保留上一次的数据，绝不能推送残缺或错误的内容。页面有内容却一场比赛都解析不出来，视为改版。→ Task 1「页面有内容却解析不出比赛：抛 EsportsUnavailable」，Task 2「10 分钟内不重复抓比赛；新闻失败不影响」。
2. **bot 重启或宕机**：不能重复推送，也不能一上线就补发一堆旧消息；10:00 不在线时当天补发预告。→ Task 2「重启后从文件恢复频道和记账」「bot 10:00 没在跑，当天晚些时候补发」「首次启用：比赛和新闻都只记账、不发」。
3. **Discord 发送失败**（频道被删、没有权限）：不能把这条消息记为已发，下一轮要重试；也不能让整个任务崩掉。→ Task 2「发送失败不记账，下一轮重试」。
4. **悉尼夏令时**：2026-10-04 起 UTC+11，预告必须在当地 10:00 发，不能变成 9:00 或 11:00。→ Task 2「夏令时第一天」两个用例。
5. **随便谁都能把推送改走**：必须检查管理频道权限，私信里也不能改。→ Task 3「没有管理频道权限不能设置」「私信里不能设置」。

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `server/esports.py` | 新建 | 解析比赛和新闻、官方赛事过滤、`fetch_matches` / `fetch_news` |
| `server/esports_feed.py` | 新建 | 常量、`plan()`、消息格式、状态记账、`EsportsFeed`（抓取、持久化、发送、`run`） |
| `server/bot.py` | 修改 | `esports` 指令、`_esports`、`post_esports`、启动推送任务 |
| `tests/fixtures/esports/liquipedia_matches.json`、`owesports_home.html` | **已提交** | 2026-10-03 保存的真实页面 |
| `tests/test_esports.py`、`tests/test_esports_feed.py`、`tests/test_esports_bot.py` | 新建 | 测试 |
| `README.md`、`docs/ARCHITECTURE.md` | 修改 | 文档 |

两个数据源一直在变，没法按版本重新下载，所以 fixture 已经随计划一起提交，执行时**不要重新抓取覆盖它们**。

### 共享数据结构

```python
# esports.parse_matches / fetch_matches 返回的比赛
{"id": "<tournament>|<start>|<team1>|<team2>", "start": 1791012600,
 "tournament": "Overwatch Champions Series/2026/Asia/Stage 3/Korea/Regular Season#Week 1",
 "label": "OWCS Korea Stage 3 - Regular Season - Week 1", "team1": "T1", "team2": "ZETA DIVISION",
 "best_of": 5, "finished": False, "score": None,   # 已结束时 [3, 1]
 "url": "https://liquipedia.net/overwatch/Overwatch_Champions_Series/2026/Asia/Stage_3/Korea/Regular_Season#Week_1"}

# esports.parse_news / fetch_news 返回的新闻
{"url": "https://esports.overwatch.com/en-us/news/<slug>", "title": "..." | None, "date": "9/9/2026" | None}

# esports_feed 的状态（esports_state.json）
{"channel_id": None, "reminded": {"<id>": start}, "resulted": {"<id>": start}, "digest_date": None,
 "seen_news": ["<url>"], "initialized": False, "news_initialized": False}
```

---

### Task 1: 赛事数据层 `esports.py`

**Files:**
- Create: `server/esports.py`
- Create: `tests/test_esports.py`
- Use (already committed): `tests/fixtures/esports/liquipedia_matches.json`、`tests/fixtures/esports/owesports_home.html`

**Interfaces:**
- Consumes: 无
- Produces:
  - 常量 `LIQUIPEDIA`、`LIQUIPEDIA_API`、`OW_ESPORTS_HOME`、`USER_AGENT`、`OFFICIAL_PREFIXES`
  - `class EsportsUnavailable(Exception)`
  - `parse_matches(page_html: str) -> list[dict]`（不过滤赛事）
  - `official(matches: list[dict]) -> list[dict]`
  - `parse_news(page_html: str) -> list[dict]`
  - `_session() -> aiohttp.ClientSession`（测试会替换它）
  - `async fetch_matches() -> list[dict]`（只返回官方赛事；页面有内容却解析不出比赛时抛 `EsportsUnavailable`）
  - `async fetch_news() -> list[dict]`

- [ ] **Step 1: 写失败的测试**

`tests/test_esports.py`：

```python
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
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_esports.py`
Expected: `ModuleNotFoundError: No module named 'esports'`

- [ ] **Step 3: 实现 `server/esports.py`**

```python
"""Overwatch 赛事数据：Liquipedia 赛程汇总页（比赛）和 esports.overwatch.com 首页（新闻）。

Liquipedia API 条款（liquipedia.net/api-terms-of-use）：必须 gzip、自定义 User-Agent 带联系方式、
action=parse 每 30 秒最多 1 次、结果尽量缓存、注明来源（CC-BY-SA）。这里每 10 分钟才抓一次。

Liquipedia:Matches 的 HTML 里每场比赛是一个 <div class="match-info">：
  timer-object 的 data-timestamp（Unix 秒），已结束的带 data-finished="finished"
  两个 <span class="name">：有 <a title="全名"> 就取 title，否则是纯文本（TBD）
  scoreholder：未开始是 "vs"，已结束是两个 match-info-header-scoreholder-score；下面是 "(BoN)"
  match-info-tournament-name 里的 <a href="/overwatch/<赛事路径>#<轮次>"><span>显示名</span></a>
"""
from __future__ import annotations

import html as htmllib
import re
from typing import Any
from urllib.parse import unquote

import aiohttp

LIQUIPEDIA = "https://liquipedia.net"
LIQUIPEDIA_API = LIQUIPEDIA + "/overwatch/api.php"
OW_ESPORTS_HOME = "https://esports.overwatch.com/en-us"
USER_AGENT = "mrmeeseeks-discord-bot/1.0 (https://github.com/Anbrose/overwatch-mrmeeseeks)"
OFFICIAL_PREFIXES = ("Overwatch Champions Series/", "Overwatch World Cup/")

_BLOCK = '<div class="match-info">'
_TIMESTAMP = re.compile(r'data-timestamp="(\d+)"')
_NAME = re.compile(r'<span class="name"[^>]*>(.*?)</span>', re.S)
_TITLE = re.compile(r'<a [^>]*title="([^"]+)"')
_SCORE = re.compile(r'match-info-header-scoreholder-score[^"]*">(\d+)<')
_BEST_OF = re.compile(r'\(Bo(\d+)\)')
_TOURNAMENT = re.compile(
    r'class="match-info-tournament-name"><a href="(/overwatch/[^"]+)"[^>]*>\s*<span>(.*?)</span>', re.S)
_NEWS = re.compile(r'<a[^>]*href="(https://esports\.overwatch\.com/en-us/news/[a-z0-9-]+)"[^>]*>(.*?)</a>', re.S)


class EsportsUnavailable(Exception):
    pass


def _team(span: str) -> str:
    m = _TITLE.search(span)
    name = m.group(1) if m else re.sub(r"<[^>]+>", "", span)
    name = htmllib.unescape(name).replace(" (page does not exist)", "").strip()
    return name or "TBD"


def parse_matches(page_html: str) -> list[dict[str, Any]]:
    """Liquipedia:Matches 解析出的全部比赛（不过滤赛事）。解析不全的块直接跳过。"""
    matches = []
    for block in page_html.split(_BLOCK)[1:]:
        ts, tour = _TIMESTAMP.search(block), _TOURNAMENT.search(block)
        names = _NAME.findall(block)
        if not ts or not tour or len(names) < 2:
            continue
        href = htmllib.unescape(tour.group(1))
        path = unquote(href[len("/overwatch/"):]).replace("_", " ")
        team1, team2 = _team(names[0]), _team(names[1])
        scores = [int(s) for s in _SCORE.findall(block)]
        finished = 'data-finished="finished"' in block and len(scores) == 2
        best_of = _BEST_OF.search(block)
        start = int(ts.group(1))
        matches.append({
            "id": f"{path}|{start}|{team1}|{team2}",
            "start": start,
            "tournament": path,
            "label": htmllib.unescape(re.sub(r"<[^>]+>", "", tour.group(2))).strip(),
            "team1": team1,
            "team2": team2,
            "best_of": int(best_of.group(1)) if best_of else None,
            "finished": finished,
            "score": scores if finished else None,
            "url": LIQUIPEDIA + href,
        })
    return matches


def official(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in matches if m["tournament"].startswith(OFFICIAL_PREFIXES)]


def parse_news(page_html: str) -> list[dict[str, Any]]:
    """官方首页上的新闻链接（去重保序）；取不到标题或日期时为 None。"""
    news, seen = [], set()
    for url, body in _NEWS.findall(page_html):
        if url in seen:
            continue
        seen.add(url)
        title = re.search(r"<h2[^>]*>(.*?)</h2>", body, re.S)
        date = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", body)
        news.append({"url": url,
                     "title": htmllib.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip() if title else None,
                     "date": date.group(1) if date else None})
    return news


def _session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
                                 timeout=aiohttp.ClientTimeout(total=30))


async def fetch_matches() -> list[dict[str, Any]]:
    """官方赛事的比赛。页面有内容却一场都解析不出来时视为改版，抛 EsportsUnavailable。"""
    params = {"action": "parse", "page": "Liquipedia:Matches", "prop": "text",
              "format": "json", "formatversion": "2"}
    async with _session() as session, session.get(LIQUIPEDIA_API, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    page_html = (data.get("parse") or {}).get("text") or ""
    matches = parse_matches(page_html)
    if not matches:
        raise EsportsUnavailable(f"no matches parsed from Liquipedia:Matches ({len(page_html)} chars)")
    return official(matches)


async def fetch_news() -> list[dict[str, Any]]:
    async with _session() as session, session.get(OW_ESPORTS_HOME) as resp:
        resp.raise_for_status()
        return parse_news(await resp.text())
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_esports.py`
Expected: `19/19 passed`

- [ ] **Step 5: 联网冒烟测试（一次即可；距离上次请求 Liquipedia 至少 30 秒）**

```bash
cd server && ../.venv/bin/python -c "
import asyncio, esports
ms = asyncio.run(esports.fetch_matches()); news = asyncio.run(esports.fetch_news())
print(len(ms), sum(m['finished'] for m in ms), len(news)); print(ms[0] if ms else None)
"; cd ..
```

Expected：打印官方赛事的比赛数（当前约 20–30 场，数量随赛程变化）、其中已结束的场数和新闻条数，再打印一场比赛的完整字段，不抛异常。如果抛 `EsportsUnavailable`，说明 Liquipedia 页面结构变了，把输出写进报告，不要为了凑结果改代码。

- [ ] **Step 6: Commit**

```bash
git add server/esports.py tests/test_esports.py
git commit -m "Add esports data layer for Liquipedia matches and official news"
```

---

### Task 2: 推送调度 `esports_feed.py`

**Files:**
- Create: `server/esports_feed.py`
- Create: `tests/test_esports_feed.py`

**Interfaces:**
- Consumes: Task 1 的 `esports.fetch_matches`、`esports.fetch_news`（作为默认的抓取函数）
- Produces:
  - 常量 `TZ`、`DIGEST_HOUR`、`DIGEST_WINDOW`、`REMIND_BEFORE`、`REMIND_LATE`、`RESULT_MAX_AGE`、`STATE_KEEP`、`MATCH_REFRESH`、`NEWS_REFRESH`、`TICK_SECONDS`、`WATCH_URL`、`ATTRIBUTION`、`MAX_MESSAGE`
  - `new_state() -> dict`
  - `@dataclass Post(text: str | None, marks: dict)`
  - `apply_marks(state, marks) -> None`、`prune(state, now) -> None`
  - `format_digest(matches) -> list[str]`、`format_reminder(m) -> str`、`format_result(m) -> str`、`format_news(n) -> str`、`split_lines(lines, limit=MAX_MESSAGE) -> list[str]`
  - `plan(now: datetime, matches: list | None, news: list | None, state: dict) -> list[Post]`（`now` 必须带时区；`None` 表示这一类还没抓到）
  - `class EsportsFeed(path, fetch_matches=esports.fetch_matches, fetch_news=esports.fetch_news)`：
    - 属性：`.state`、`.matches`、`.news`、`.channel_id`
    - 方法：`.load()`、`.save()`、`.set_channel(id | None)`、`.upcoming(n=3)`、`async .refresh(mono)`、`async .tick(now, send, mono)`、`async .run(send)`
    - `send` 的签名是 `async (channel_id: int, text: str) -> None`

- [ ] **Step 1: 写失败的测试**

`tests/test_esports_feed.py`：

```python
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


if __name__ == "__main__":
    test_digest()
    test_digest_dst()
    test_digest_split()
    test_reminders()
    test_results()
    test_first_run_and_news()
    test_prune()
    test_feed_tick()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_esports_feed.py`
Expected: `ModuleNotFoundError: No module named 'esports_feed'`

- [ ] **Step 3: 实现 `server/esports_feed.py`**

```python
"""赛事推送频道：每日预告、开赛提醒、赛果（剧透遮罩）、官方新闻。

plan() 是纯函数：给定当前时间、比赛、新闻和状态，返回要发的消息，每条消息附带"发出后要记的账"。
EsportsFeed 负责抓取、持久化状态、调用 plan 并发送。没有设置频道时照常记账、不发送，
这样之后再设置频道也不会补发一堆旧消息。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import esports

log = logging.getLogger("mrmeeseeks.esports")

TZ = ZoneInfo("Australia/Sydney")
DIGEST_HOUR = 10                 # 悉尼时间每天 10:00 发未来 24 小时的预告
DIGEST_WINDOW = timedelta(hours=24)
REMIND_BEFORE = timedelta(minutes=15)
REMIND_LATE = timedelta(minutes=5)      # 开赛超过 5 分钟就不再补发提醒
RESULT_MAX_AGE = timedelta(hours=24)    # 只发最近 24 小时内开始的比赛的赛果
STATE_KEEP = timedelta(days=14)
MATCH_REFRESH = 600              # 比赛每 10 分钟抓一次
NEWS_REFRESH = 3 * 3600          # 新闻每 3 小时抓一次
TICK_SECONDS = 60
WATCH_URL = "https://www.twitch.tv/ow_esports"
ATTRIBUTION = "Data: Liquipedia (CC-BY-SA)"
MAX_MESSAGE = 1990


def new_state() -> dict[str, Any]:
    return {"channel_id": None, "reminded": {}, "resulted": {}, "digest_date": None,
            "seen_news": [], "initialized": False, "news_initialized": False}


@dataclass
class Post:
    text: str | None                      # None 表示只记账、不发消息
    marks: dict[str, Any] = field(default_factory=dict)


def apply_marks(state: dict[str, Any], marks: dict[str, Any]) -> None:
    """把一条消息的记账合并进状态（原地修改）。"""
    state["reminded"].update(marks.get("reminded", {}))
    state["resulted"].update(marks.get("resulted", {}))
    for url in marks.get("seen_news", []):
        if url not in state["seen_news"]:
            state["seen_news"].append(url)
    if "digest_date" in marks:
        state["digest_date"] = marks["digest_date"]
    for flag in ("initialized", "news_initialized"):
        if marks.get(flag):
            state[flag] = True


def prune(state: dict[str, Any], now: datetime) -> None:
    cutoff = (now - STATE_KEEP).timestamp()
    for key in ("reminded", "resulted"):
        state[key] = {k: v for k, v in state[key].items() if v >= cutoff}
    state["seen_news"] = state["seen_news"][-200:]


# ---------- 消息格式 ----------
def _vs(m: dict[str, Any]) -> str:
    return f"{m['team1']} vs {m['team2']}"


def _bo(m: dict[str, Any]) -> str:
    return f" · Bo{m['best_of']}" if m.get("best_of") else ""


def format_digest(matches: list[dict[str, Any]]) -> list[str]:
    lines = [f"• <t:{m['start']}:t> (<t:{m['start']}:R>) {_vs(m)} · {m['label']}{_bo(m)}" for m in matches]
    return split_lines(["📅 **Overwatch esports — next 24h**", *lines, ATTRIBUTION])


def format_reminder(m: dict[str, Any]) -> str:
    return f"🔴 Starting <t:{m['start']}:R>: **{_vs(m)}** · {m['label']}{_bo(m)} — watch: {WATCH_URL} · <{m['url']}>"


def format_result(m: dict[str, Any]) -> str:
    s1, s2 = m["score"]
    return (f"✅ {_vs(m)} · {m['label']} — result: ||{m['team1']} {s1} : {s2} {m['team2']}||"
            f" · <{m['url']}>\n{ATTRIBUTION}")


def format_news(n: dict[str, Any]) -> str:
    return f"📰 {n['title'] or 'Overwatch esports news'} {n['url']}"


def split_lines(lines: list[str], limit: int = MAX_MESSAGE) -> list[str]:
    """按行拼成不超过 limit 的若干条消息。"""
    out, cur = [], ""
    for line in lines:
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


# ---------- 计划 ----------
def plan(now: datetime, matches: list[dict[str, Any]] | None, news: list[dict[str, Any]] | None,
         state: dict[str, Any]) -> list[Post]:
    """now 必须带时区。matches/news 为 None 表示还没抓到（这一类本轮什么都不做）。"""
    posts: list[Post] = []
    ts = now.timestamp()
    # 首次启用：已经结束的比赛、已有新闻只记账，不刷屏。比赛和新闻各自初始化，互不阻塞
    if matches is not None and not state["initialized"]:
        posts.append(Post(None, {"initialized": True,
                                 "resulted": {m["id"]: m["start"] for m in matches if m["finished"]}}))
    elif matches is not None:
        local = now.astimezone(TZ)
        today = local.date().isoformat()
        if local.hour >= DIGEST_HOUR and state["digest_date"] != today:
            upcoming = sorted((m for m in matches if not m["finished"]
                               and ts <= m["start"] <= ts + DIGEST_WINDOW.total_seconds()),
                              key=lambda m: m["start"])
            texts = format_digest(upcoming) if upcoming else [None]
            posts += [Post(t) for t in texts[:-1]] + [Post(texts[-1], {"digest_date": today})]
        for m in sorted(matches, key=lambda m: m["start"]):
            if (not m["finished"] and m["id"] not in state["reminded"]
                    and m["start"] - REMIND_BEFORE.total_seconds() <= ts <= m["start"] + REMIND_LATE.total_seconds()):
                posts.append(Post(format_reminder(m), {"reminded": {m["id"]: m["start"]}}))
            if (m["finished"] and m["id"] not in state["resulted"]
                    and ts - RESULT_MAX_AGE.total_seconds() <= m["start"] <= ts):
                posts.append(Post(format_result(m), {"resulted": {m["id"]: m["start"]}}))
    if news is not None and not state["news_initialized"]:
        posts.append(Post(None, {"news_initialized": True, "seen_news": [n["url"] for n in news]}))
    elif news is not None:
        for n in news:
            if n["url"] not in state["seen_news"]:
                posts.append(Post(format_news(n), {"seen_news": [n["url"]]}))
    return posts


# ---------- 运行 ----------
class EsportsFeed:
    def __init__(self, path: str,
                 fetch_matches: Callable[[], Awaitable[list[dict]]] = esports.fetch_matches,
                 fetch_news: Callable[[], Awaitable[list[dict]]] = esports.fetch_news):
        self.path = path
        self.fetch_matches, self.fetch_news = fetch_matches, fetch_news
        self.state = new_state()
        self.matches: list[dict[str, Any]] | None = None
        self.news: list[dict[str, Any]] | None = None
        self._matches_at = self._news_at = float("-inf")

    @property
    def channel_id(self) -> int | None:
        return self.state["channel_id"]

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            state = new_state()
            state.update({k: data[k] for k in state if k in data})
            self.state = state
        except (OSError, ValueError, TypeError) as e:
            log.info("No usable esports state at %s (%r)", self.path, e)

    def save(self) -> None:
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write esports state to %s", self.path)

    def set_channel(self, channel_id: int | None) -> None:
        self.state["channel_id"] = channel_id
        self.save()

    def upcoming(self, n: int = 3) -> list[dict[str, Any]]:
        now = time.time()
        return sorted((m for m in self.matches or [] if not m["finished"] and m["start"] >= now),
                      key=lambda m: m["start"])[:n]

    async def refresh(self, mono: float) -> None:
        """到点就重新抓；抓取失败保留上一次的数据。"""
        if mono - self._matches_at >= MATCH_REFRESH:
            self._matches_at = mono
            try:
                self.matches = await self.fetch_matches()
            except Exception:
                log.exception("Esports match fetch failed; keeping previous data")
        if mono - self._news_at >= NEWS_REFRESH:
            self._news_at = mono
            try:
                self.news = await self.fetch_news()
            except Exception:
                log.exception("Esports news fetch failed; keeping previous data")

    async def tick(self, now: datetime, send: Callable[[int, str], Awaitable[None]], mono: float) -> None:
        await self.refresh(mono)
        state = copy.deepcopy(self.state)
        changed = False
        for post in plan(now, self.matches, self.news, state):
            if post.text is not None and state["channel_id"] is not None:
                try:
                    await send(state["channel_id"], post.text)
                except Exception:
                    log.exception("Could not post to esports channel %s; will retry", state["channel_id"])
                    continue   # 不记账，下一轮重试
            apply_marks(state, post.marks)
            changed = True
        if changed:
            prune(state, now)
            state["channel_id"] = self.state["channel_id"]   # 发送期间可能被指令改过
            self.state = state
            self.save()

    async def run(self, send: Callable[[int, str], Awaitable[None]]) -> None:
        while True:
            try:
                await self.tick(datetime.now(timezone.utc), send, time.monotonic())
            except Exception:
                log.exception("Esports feed tick failed")
            await asyncio.sleep(TICK_SECONDS)
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_esports_feed.py`
Expected: `38/38 passed`。日志里会出现 "homepage down"、"missing permissions" 和 "File exists: …/blocker" 的 traceback，分别来自"新闻抓取失败""发送失败""状态写不进去"这几个用例，都是预期内的。

- [ ] **Step 5: Commit**

```bash
git add server/esports_feed.py tests/test_esports_feed.py
git commit -m "Add esports feed scheduler: daily digest, reminders, spoiler-tagged results, news"
```

---

### Task 3: 接入 bot、指令与文档

**Files:**
- Modify: `server/bot.py`
- Create: `tests/test_esports_bot.py`
- Modify: `README.md`、`docs/ARCHITECTURE.md`

**Interfaces:**
- Consumes: Task 2 的 `EsportsFeed`（`.channel_id`、`.set_channel`、`.upcoming`、`.load`、`.run(send)`、`.matches`、`.path`）
- Produces:
  - `bot.ARG_COMMANDS` 包含 `"esports"`，所以 `parse_command("esports here") == ("esports", "here")`
  - `MeeseeksBot(..., hero_qa=None, esports_feed: EsportsFeed | None = None)`
  - `MeeseeksBot._esports(message, arg)`、`MeeseeksBot.post_esports(channel_id, text)`
  - `main()` 创建 `EsportsFeed`（英雄数据缓存目录下的 `esports_state.json`）并起后台任务 `esports_feed.run(bot.post_esports)`

- [ ] **Step 1: 写失败的测试**

`tests/test_esports_bot.py`：

```python
"""赛事推送的 Discord 指令：路由、权限检查、here/off/status，以及推送用的发送函数（假的 Discord 对象）。

运行：python tests/test_esports_bot.py
"""
import asyncio
import os
import sys
import tempfile
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import bot  # noqa: E402
from esports_feed import EsportsFeed  # noqa: E402

results = []
NS = types.SimpleNamespace


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


class FakeChannel:
    def __init__(self, channel_id, manage):
        self.id, self.manage, self.sent = channel_id, manage, []

    def permissions_for(self, member):
        return NS(manage_channels=self.manage)

    async def send(self, text, allowed_mentions=None):
        self.sent.append((text, allowed_mentions))


class FakeMessage:
    def __init__(self, manage=True, guild=True, channel_id=555):
        self.author = NS(id=1)
        self.guild = NS(id=9) if guild else None
        self.channel = FakeChannel(channel_id, manage)
        self.replies = []

    async def reply(self, content):
        self.replies.append(content)


def make_bot(feed):
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)   # 不连 Discord，只测指令
    b.esports_feed = feed
    return b


def test_routing():
    check("esports here 路由到 esports，参数 here", bot.parse_command("esports here") == ("esports", "here"))
    check("大小写不敏感", bot.parse_command("Esports STATUS") == ("esports", "STATUS"))
    check("只回复 bot（没有显式 @）时指令仍可用", bot.parse_command("esports off", explicit=False) == ("esports", "off"))
    check("帮助里列出 esports 指令", "esports here" in bot.HELP_TEXT)


def test_commands():
    with tempfile.TemporaryDirectory() as d:
        feed = EsportsFeed(os.path.join(d, "esports_state.json"))
        b = make_bot(feed)

        m = FakeMessage(manage=False)
        asyncio.run(b._esports(m, "here"))
        check("没有管理频道权限不能设置", "Manage Channels" in m.replies[0] and feed.channel_id is None)
        m = FakeMessage(guild=False)
        asyncio.run(b._esports(m, "here"))
        check("私信里不能设置", "Manage Channels" in m.replies[0] and feed.channel_id is None)

        m = FakeMessage(manage=True, channel_id=555)
        asyncio.run(b._esports(m, "here"))
        check("有权限：设为当前频道并持久化", feed.channel_id == 555 and _reloaded(feed.path) == 555)
        check("回复确认", m.replies[0].startswith("✅"))

        feed.matches = [{"id": "x", "start": int(time.time()) + 3600, "finished": False, "team1": "T1",
                         "team2": "ZETA DIVISION", "label": "OWCS Korea Stage 3"}]
        m = FakeMessage(manage=False)
        asyncio.run(b._esports(m, "status"))
        check("status 不需要权限，显示频道和接下来的比赛", "<#555>" in m.replies[0] and "T1 vs ZETA DIVISION" in m.replies[0])

        m = FakeMessage(manage=True)
        asyncio.run(b._esports(m, "off"))
        check("off 关闭推送", feed.channel_id is None and "turned off" in m.replies[0])
        m = FakeMessage()
        asyncio.run(b._esports(m, "status"))
        check("未设置时 status 提示怎么设置", "not set" in m.replies[0])
        m = FakeMessage()
        asyncio.run(b._esports(m, "whatever"))
        check("未知参数显示用法", m.replies[0].startswith("Usage"))

    m = FakeMessage()
    asyncio.run(make_bot(None)._esports(m, "here"))
    check("没有启用推送时提示", "not enabled" in m.replies[0])


def _reloaded(path):
    f = EsportsFeed(path)
    f.load()
    return f.channel_id


def test_post_esports():
    channel = FakeChannel(777, True)
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    waited = []

    async def wait_until_ready():
        waited.append(True)

    async def _channel(channel_id):
        return channel

    b.wait_until_ready, b._channel = wait_until_ready, _channel
    asyncio.run(b.post_esports(777, "📅 hello"))
    check("发送前等 bot 就绪", waited == [True])
    check("发送内容，并且不 ping 任何人", channel.sent[0][0] == "📅 hello"
          and channel.sent[0][1].everyone is False and channel.sent[0][1].users is False)


if __name__ == "__main__":
    test_routing()
    test_commands()
    test_post_esports()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_esports_bot.py`
Expected: `FAIL esports here 路由到 esports，参数 here`，之后出现 `AttributeError: 'MeeseeksBot' object has no attribute '_esports'`

- [ ] **Step 3: 修改 `server/bot.py`**

应用补丁（保存为工作区里的文件，例如 `t3-bot.diff`，然后执行 `git apply t3-bot.diff`）：

```diff
--- a/server/bot.py
+++ b/server/bot.py
@@ -28,6 +28,7 @@
 
 import overfast
 import matchups
+from esports_feed import EsportsFeed
 from analyzer import Analyzer, format_facts, roles_from_roster
 from hero_qa import Cooldown, HeroQA
 from herodata import HeroStore, load_aliases, refresh_loop
@@ -54,12 +55,14 @@
     "`@mrmeeseeks analyze` Re-run advice on the latest recognized situation in this channel\n"
     "`@mrmeeseeks label` Label portraits I couldn't recognize\n"
     "`@mrmeeseeks disconnect` Unpair the client bound to this channel\n"
+    "`@mrmeeseeks esports here` / `esports off` Post OWCS schedule, reminders, results and news in this channel "
+    "(needs Manage Channels); `esports status` shows the current setup\n"
     "`@mrmeeseeks help` Show this help\n"
     "`@mrmeeseeks <question>` Ask about heroes, e.g. `was Cassidy nerfed recently?`, "
     "`Tracer HP`, `how many Cassidy headshots kill Mauga at 30m?`"
 )
 COMMANDS = ("connect", "status", "disconnect", "help", "analyze", "label")
-ARG_COMMANDS = ("player",)     # 带参数的指令；参数保留大小写（BattleTag 大小写敏感）
+ARG_COMMANDS = ("player", "esports")     # 带参数的指令；参数保留大小写（BattleTag 大小写敏感）
 ASK_COOLDOWN_SECONDS = 5
 
 
@@ -155,7 +158,8 @@
 class MeeseeksBot(discord.Client):
     def __init__(self, registry: Registry, analyzer: Analyzer | None, recognizer: Recognizer,
                  labels: LabelStore, roster: Roster,
-                 store: HeroStore | None = None, hero_qa: HeroQA | None = None):
+                 store: HeroStore | None = None, hero_qa: HeroQA | None = None,
+                 esports_feed: EsportsFeed | None = None):
         intents = discord.Intents.default()
         intents.message_content = True
         super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
@@ -170,6 +174,7 @@
         self.store = store
         self.hero_qa = hero_qa
         self.ask_cooldown = Cooldown(ASK_COOLDOWN_SECONDS)
+        self.esports_feed = esports_feed
 
     def hero_name(self, key: str) -> str:
         return self.names.get(key) or overfast.hero_name(key)
@@ -205,6 +210,8 @@
             await self._disconnect(message)
         elif cmd == "player":
             await self._player(message, arg)
+        elif cmd == "esports":
+            await self._esports(message, arg)
         elif cmd == "analyze":
             await self._reanalyze(message)
         elif cmd == "label":
@@ -214,6 +221,35 @@
         else:
             await self._ask(message, arg)
 
+    async def _esports(self, message: discord.Message, arg: str) -> None:
+        feed = self.esports_feed
+        if feed is None:
+            await message.reply("The esports feed is not enabled on this server.")
+            return
+        action = arg.strip().lower()
+        if action in ("here", "off"):
+            perms = message.channel.permissions_for(message.author) if message.guild else None
+            if perms is None or not perms.manage_channels:
+                await message.reply("You need Manage Channels permission to change the esports channel.")
+                return
+            feed.set_channel(message.channel.id if action == "here" else None)
+            await message.reply("✅ OWCS schedule, reminders, results and news will be posted in this channel."
+                                if action == "here" else "Esports posts are turned off.")
+        elif action == "status":
+            where = f"<#{feed.channel_id}>" if feed.channel_id else "not set (use `@mrmeeseeks esports here`)"
+            lines = [f"Esports channel: {where}"]
+            for m in feed.upcoming():
+                lines.append(f"• <t:{m['start']}:f> {m['team1']} vs {m['team2']} · {m['label']}")
+            await message.reply("\n".join(lines)[:1990])
+        else:
+            await message.reply("Usage: `@mrmeeseeks esports here`, `esports off` or `esports status`.")
+
+    async def post_esports(self, channel_id: int, text: str) -> None:
+        """推送任务用的发送函数：等 bot 登录完成再发，不 ping 任何人。"""
+        await self.wait_until_ready()
+        channel = await self._channel(channel_id)
+        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
+
     async def _ask(self, message: discord.Message, question: str) -> None:
         if self.hero_qa is None:
             await message.reply("Hero Q&A needs ANTHROPIC_API_KEY to be set.")
@@ -410,8 +446,12 @@
         log.info("Advice model: %s (effort %s)", model, effort or "default")
     refresh_hours = float(os.environ.get("HERO_REFRESH_HOURS", "24"))
 
+    # 赛事推送状态和英雄数据放在同一个目录（Docker 里是 herodata 卷）
+    esports_feed = EsportsFeed(os.path.join(os.path.dirname(os.path.abspath(store.path)), "esports_state.json"))
+    esports_feed.load()
+
     registry = Registry()
-    bot = MeeseeksBot(registry, analyzer, recognizer, labels, roster, store, hero_qa)
+    bot = MeeseeksBot(registry, analyzer, recognizer, labels, roster, store, hero_qa, esports_feed)
     ws = WSServer(
         registry, bot,
         host=os.environ.get("WS_HOST", "0.0.0.0"),
@@ -422,7 +462,8 @@
     async with bot:
         await ws.start()
         refreshers = [asyncio.create_task(refresh_loop(store, refresh_hours)),
-                      asyncio.create_task(refresh_loop(matchup_store, refresh_hours, fetch=matchups.fetch_all))]
+                      asyncio.create_task(refresh_loop(matchup_store, refresh_hours, fetch=matchups.fetch_all)),
+                      asyncio.create_task(esports_feed.run(bot.post_esports))]
         try:
             await bot.start(token)
         except discord.LoginFailure:
```

改动要点：
- `esports` 加进 `ARG_COMMANDS`，`here/off/status` 作为参数。
- `post_esports` 先 `wait_until_ready()`，再用 `AllowedMentions.none()` 发送。
- 推送任务和两个刷新任务放在同一个列表里，退出时一起取消。

- [ ] **Step 4: 运行测试，确认通过**

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_esports_bot.py`
Expected: `15/15 passed`

- [ ] **Step 5: 文档**

应用两个补丁：

```diff
--- a/README.md
+++ b/README.md
@@ -362,6 +362,7 @@
 | `@mrmeeseeks disconnect` | 解绑本频道的客户端 |
 | `@mrmeeseeks help` | 显示帮助 |
 | `@mrmeeseeks <问题>` | 英雄问答：数值（血量、子弹体积）、最近的补丁、N 米处几枪击杀。例如 `@mrmeeseeks 卡西迪最近被削了吗`、`@mrmeeseeks 卡西迪 30 米爆头几枪杀毛加`、`@mrmeeseeks 查莉娅怕谁`。克制关系来自 [counterwatch.gg](https://www.counterwatch.gg) 的对位评分（对决和团战结果，全段位，不是整局胜率）。需要 `ANTHROPIC_API_KEY` |
+| `@mrmeeseeks esports here` / `esports off` / `esports status` | 赛事推送频道：在当前频道推送 OWCS 和世界杯的每日赛程预告（悉尼时间 10:00）、开赛前 15 分钟提醒、赛果（比分用剧透遮罩）和官方新闻。`here`/`off` 需要"管理频道"权限。比赛数据来自 [Liquipedia](https://liquipedia.net/overwatch)（CC-BY-SA） |
 
 ### 配对规则
 
@@ -386,6 +387,9 @@
 python tests/test_hero_qa.py   # 英雄问答工具与对话循环、bot 路由
 python tests/test_matchups.py  # counterwatch 对位数据解析、缓存、查询
 python tests/test_counter_advice.py # 换英雄建议里的对位评分和换人候选
+python tests/test_esports.py   # Liquipedia 赛程、官方新闻解析
+python tests/test_esports_feed.py # 赛事推送调度：预告、提醒、赛果、新闻、夏令时
+python tests/test_esports_bot.py  # esports 指令和权限
 ```
 
 每个脚本最后一行显示 `N/N passed`，全部通过时退出码为 0。
```

```diff
--- a/docs/ARCHITECTURE.md
+++ b/docs/ARCHITECTURE.md
@@ -15,6 +15,8 @@
 | `server/damage.py` | 伤害衰减、护甲/护盾、爆头倍率、几枪击杀（纯函数） |
 | `server/hero_qa.py` | 英雄问答：Claude tool use 循环，数字只来自上面两个模块和 `matchups.py` |
 | `server/matchups.py` | 英雄对位（克制）数据：从 counterwatch.gg 抓取、缓存、每日刷新 |
+| `server/esports.py` | 赛事数据：解析 Liquipedia:Matches（比赛）和 esports.overwatch.com 首页（新闻） |
+| `server/esports_feed.py` | 赛事推送：`plan()` 纯函数决定发什么，`EsportsFeed` 负责抓取、状态持久化和发送 |
 | `client/client.py` | 本地客户端：连接/重连、配对码输入、Tab 监听、两段截图、上传 |
 
 ## 客户端状态
@@ -88,6 +90,16 @@
 
 本期不支持：TTK、技能伤害、perk 加成。
 
+## 赛事推送
+
+用 `@mrmeeseeks esports here` 指定一个频道后，`EsportsFeed.run` 每分钟执行一次：
+
+1. **抓取**：比赛每 10 分钟通过 Liquipedia MediaWiki API 解析一次 `Liquipedia:Matches`（条款要求 gzip、带联系方式的 User-Agent、parse 每 30 秒最多 1 次，并注明 CC-BY-SA 来源），只保留赛事路径以 `Overwatch Champions Series/`、`Overwatch World Cup/` 开头的比赛。新闻每 3 小时抓一次官方首页。抓取失败时保留上一次的数据。
+2. **计划**：`esports_feed.plan(now, matches, news, state)` 返回要发的消息，每条附带发出后要记的账：悉尼时间 10:00 发未来 24 小时的预告，开赛前 15 分钟发提醒（开赛超过 5 分钟不补发），比赛结束 24 小时内发赛果（剧透遮罩），以及新出现的新闻。首次启用时只记账、不发送。
+3. **发送**：发送成功后才记账，失败的下一轮重试。没有设置频道时照常记账、不发送。状态保存在英雄数据缓存同一目录的 `esports_state.json`。
+
+已知限制：Liquipedia 汇总页只有最近约 50 场即将进行和 50 场已结束的比赛；官方首页只放 1–2 条精选新闻。
+
 ## 扩展点
 
 `Analyzer.analyze` 里的 `context` 字典就是接入更多事实数据的地方，目前三项都是"暂未接入"：
```

- [ ] **Step 6: 运行完整测试**

```bash
docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test sh -c 'pip install -q -r client/requirements.txt >/dev/null 2>&1; for t in tests/test_*.py; do printf "%s: " $t; python $t 2>/dev/null | tail -1; done'
```

Expected：13 个文件全部 `N/N passed`，依次为 `test_counter_advice` 28/28、`test_damage` 37/37、`test_e2e` 34/34、`test_esports` 19/19、`test_esports_bot` 15/15、`test_esports_feed` 38/38、`test_hero_qa` 55/55、`test_herodata` 32/32、`test_heroparse` 36/36、`test_matchups` 37/37、`test_overfast` 28/28、`test_units` 26/26、`test_vision` 67/67。

- [ ] **Step 7: Commit**

```bash
git add server/bot.py tests/test_esports_bot.py README.md docs/ARCHITECTURE.md
git commit -m "Add esports channel commands and start the esports feed"
```

- [ ] **Step 8: 部署后验证（需要用户批准部署；合并到 main 之后才部署）**

部署后在 Discord 里：
1. 在目标频道发 `@mrmeeseeks esports here`（需要管理频道权限），应该回复 ✅。
2. 发 `@mrmeeseeks esports status`，应该显示频道和接下来的 3 场比赛。
3. 等开赛前 15 分钟的提醒，以及下一个悉尼时间 10:00 的预告。

在服务器上检查日志里没有 `Esports match fetch failed` 或 `Esports feed tick failed`，以及 `/app/herodata/esports_state.json` 已经生成。

## 已知限制（不在本计划内）

- Liquipedia 汇总页只有最近约 50 场即将进行的比赛，赛程密集时，24 小时预告可能不完整。
- 官方首页只放 1–2 条精选新闻，新闻推送频率会很低。
- 比赛 ID 由赛事、开始时间和队名拼成：TBD 队伍确定后、或比赛改期后，会被当成一场新比赛。提醒窗口只有 15 分钟，到时队伍通常已经确定，所以影响很小。
