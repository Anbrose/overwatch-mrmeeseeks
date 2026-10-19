# 赛事竞猜（含双方阵容）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每场 OWCS / 世界杯比赛在它的赛区频道（或默认频道）发一条带按钮的竞猜消息，附双方报名阵容；开赛时截止并公布押注名单，赛果出来后在剧透遮罩里揭晓谁猜中并计分；每周一发周榜；`predictions` 指令查看榜单；`esports preview` 加一条竞猜样例。

**Architecture:**
- **数据层**：`predictions.py` 是 SQLite 竞猜存储；`rosters.py` 从 Liquipedia 赛事页面解析报名阵容，带缓存和限速。
- **推送流程**：`prediction_feed.py` 接进现有推送。纯函数 `plan_predictions` 和 `decorate` 决定要发什么，`PredictionFeed` 负责读快照、执行和结算。它不依赖 discord，按钮只用一个描述字典表示。
- **按钮**：`prediction_ui.py` 用 discord.py 的 `DynamicItem` 实现持久按钮，并对编辑做节流。
- **接入**：`esports_feed.py` 只加最少量的接入点；`bot.py` 负责注册按钮、发送和编辑带按钮的消息、处理指令，以及在 `main()` 里把各部分串起来。

**Tech Stack:** Python 3.12（生产镜像）、标准库 `sqlite3`、discord.py ≥ 2.6（`DynamicItem`）、aiohttp。测试沿用仓库风格：`python tests/test_xxx.py`，自带 `check()`。本机的 Python 3.13 装不上 `rapidocr-onnxruntime`，所以**所有测试都在生产镜像里跑**。

**Spec:** `docs/superpowers/specs/2026-10-18-esports-predictions-design.md`。末尾的「实现说明」是在原型中验证过的细节，和上文冲突时以它为准。

**分支：** `esports-predictions`，已变基到 `main` 7047bc6（包含 #21）。

## Global Constraints

- **计分**：猜错 0 分；猜中 `1 + max(0, (0.5 − 押中一方占比) × 4)`，上限 3，保留 1 位小数；总人数少于 3 时没有加成。
- **竞猜消息的发送条件**：
  - 悉尼时间 ≥ 10:00；
  - 比赛还没结束；
  - 在未来 24 小时内，并且离开赛至少 15 分钟；
  - 不是 TBD 对 TBD；
  - 还没登记过；
  - 发到这场比赛路由到的频道（`channel_for`）；没有频道就不发、也不登记。
- **截止和作废**：到开赛时间就截止（先公布名单，再 `lock`，再编辑原消息、禁用按钮）。开赛 48 小时后还没结算就作废。赛果没有胜负时也作废。
- **可见性**：竞猜期间只显示票数；截止时公布名单（纯文本，不 @ 任何人）；赛果揭晓放在 `||…||` 里。
- **同一场比赛**：ID 相同；或者同一赛事、同样两队、开赛时间相差不到 6 小时，并且旧 ID 已经不在当前汇总页上。这和 #21 的去重是同一条规则。
- **存储位置**：竞猜存在 `STATE_DIR/predictions.db`；阵容缓存是英雄数据缓存同目录下的 `rosters.json`。不新增环境变量。
- **Liquipedia 抓取**：`action=parse` 每 30 秒最多 1 次，同一页面 24 小时内不重复抓取；User-Agent 只写项目的 GitHub 地址；注明 `Data: Liquipedia (CC-BY-SA)`。
- **按钮**：`custom_id` 格式为 `pred:<12 位 key>:<1|2>`；按钮文字是"队名 · 票数"，最多 80 字符；同一场比赛的票数每 2 秒最多编辑一次。
- **权限**：`predictions reset` 需要"管理服务器"权限；`esports preview` 沿用"管理频道"权限。所有推送都不 ping 任何人。
- **代码风格**：注释用中文，和现有的 `server/*.py` 一致。

## Review Focus

1. **很多人同时点按钮**：SQLite 和按钮回调在同一个事件循环里执行，所以每一票都不会丢。票数编辑会被节流，最终显示的票数要正确。→ Task 4「连续 5 次点击：第一次立即编辑，其余合并」「合并后补一次编辑，显示最终票数」。
2. **bot 重启后点旧消息上的按钮**：`DynamicItem` 在 `setup_hook` 里注册之后，旧消息上的按钮仍然能用，并且读到的是数据库里的状态。→ Task 5「启动时注册竞猜按钮并设置 handler」，Task 4「from_custom_id 解析出 key 和 side」。
3. **开赛改期**：竞猜要对应回原来那场，并且按新的开赛时间截止，不能提前截止，也不能再发第二条竞猜。→ Task 3「改期 10 分钟：不再发第二条竞猜」「按新的开赛时间截止」。
4. **剧透**：赛果揭晓的名单和得分都必须放在剧透遮罩里；截止时公布的名单里不能含有赛果。→ Task 3「赛果揭晓在剧透里」「开赛截止：公布双方名单」。
5. **Discord 发送或编辑失败**：发送失败时这一轮停下来，下一轮重试，不能把没发出去的消息登记掉；编辑失败只记日志，不影响押注和结算。→ Task 3「竞猜消息发送失败：不登记」「下一轮重试成功并登记」，Task 4「编辑失败不影响押注」。

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `server/predictions.py` | 新建 | 竞猜存储与计分（Task 1） |
| `server/rosters.py` | 新建 | 赛事报名阵容的解析和缓存（Task 2） |
| `server/esports_feed.py` | 修改 | `Post.kind`/`data`、`leaderboard_week`，以及接入 `predictions` 的点（Task 3） |
| `server/prediction_feed.py` | 新建 | 竞猜推送流程（Task 3） |
| `server/prediction_ui.py` | 新建 | 竞猜按钮（Task 4） |
| `server/bot.py` | 修改 | 注册按钮、发送和编辑、`predictions` 指令、preview 竞猜样例、`main()` 接线（Task 5） |
| `tests/fixtures/esports/liquipedia_owcs_korea_stage3.json` | **已提交** | 2026-10-18 保存的 OWCS Korea Stage 3 赛事页面 |
| `tests/test_predictions.py`、`test_rosters.py`、`test_prediction_feed.py`、`test_prediction_ui.py`、`test_prediction_bot.py` | 新建 | 测试 |
| `tests/test_esports_bot.py` | 修改 | 假的 `send` 改为返回消息对象（Task 5） |
| `README.md`、`docs/ARCHITECTURE.md` | 修改 | 文档（Task 5） |

---

### Task 1: 竞猜存储与计分 `predictions.py`

**Files:**
- Create: `server/predictions.py`
- Create: `tests/test_predictions.py`

**Interfaces:**
- Consumes: `esports_feed.match_base`、`esports_feed.SAME_MATCH_WINDOW`（已在 main 上，来自 #21）
- Produces:
  - 常量 `OPEN`/`LOCKED`/`RESOLVED`/`VOID`、`BONUS_MIN_PICKS`、`MAX_POINTS`
  - `key_for(match_id) -> str`、`points_for(share, total) -> float`
  - `PickResult(ok, reason, changed)`、`ScoreLine(user_id, user_name, correct, points)`
  - `PredictionStore(path)`，方法有：
    - `register(match, channel_id, message_id, url=None) -> key`、`get(key)`、`by_match_id(id)`、`find(match, listed=frozenset())`、`active()`
    - `pick(key, user_id, user_name, side, now) -> PickResult`、`counts(key) -> (n1, n2)`、`names(key) -> {1: [...], 2: [...]}`
    - `lock(key) -> names | None`、`score_preview(key, winner)`、`resolve(key, winner, now)`、`void(key) -> bool`
    - `leaderboard(since, until) -> [{user_id, name, points, correct, played}]`、`titles(since, until)`
    - `season_start()`、`new_season(now)`、`close()`
  - `format_board(title, rows, limit=10) -> list[str]`、`format_titles(t) -> str | None`

- [ ] **Step 1: 写失败的测试**

`tests/test_predictions.py`：

```python
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
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_predictions.py`
Expected: `ModuleNotFoundError: No module named 'predictions'`

- [ ] **Step 3: 实现**

`server/predictions.py`：

```python
"""赛事竞猜：押注、截止、结算、作废、周榜和赛季榜。存 STATE_DIR/predictions.db（SQLite）。

每场比赛用 key（比赛 ID 的 sha1 前 12 位）标识：Discord 按钮的 custom_id 最长 100 字符，
比赛 ID（赛事路径|时间|队1|队2）可能更长。

计分：猜错 0 分；猜中 1 + bonus，bonus = max(0, (0.5 - 押中一方的人数占比) * 4)，总分上限 3，
保留 1 位小数；这场总人数少于 3 时没有 bonus。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3

from esports_feed import SAME_MATCH_WINDOW, match_base
from dataclasses import dataclass
from typing import Any

OPEN, LOCKED, RESOLVED, VOID = "open", "locked", "resolved", "void"
BONUS_MIN_PICKS = 3
MAX_POINTS = 3.0
VOID_AFTER = 48 * 3600          # 开赛 48 小时后还没结算就作废

_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    key TEXT PRIMARY KEY, match_id TEXT UNIQUE NOT NULL, team1 TEXT NOT NULL, team2 TEXT NOT NULL,
    label TEXT NOT NULL, start INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER, url TEXT,
    status TEXT NOT NULL DEFAULT 'open', winner INTEGER);
CREATE TABLE IF NOT EXISTS picks (
    key TEXT NOT NULL, user_id INTEGER NOT NULL, user_name TEXT NOT NULL, side INTEGER NOT NULL,
    picked_at INTEGER NOT NULL, PRIMARY KEY (key, user_id));
CREATE TABLE IF NOT EXISTS scores (
    key TEXT NOT NULL, user_id INTEGER NOT NULL, user_name TEXT NOT NULL, correct INTEGER NOT NULL,
    points REAL NOT NULL, resolved_at INTEGER NOT NULL, PRIMARY KEY (key, user_id));
CREATE TABLE IF NOT EXISTS seasons (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at INTEGER NOT NULL);
"""


def key_for(match_id: str) -> str:
    return hashlib.sha1(match_id.encode("utf-8")).hexdigest()[:12]


@dataclass
class PickResult:
    ok: bool
    reason: str | None = None       # 拒绝时的原因，可以直接展示给用户
    changed: bool = False           # 是不是改选


@dataclass
class ScoreLine:
    user_id: int
    user_name: str
    correct: bool
    points: float


def points_for(share: float, total: int) -> float:
    """押中的一方占比 share、这场总人数 total 时，猜中的人每人得几分。"""
    bonus = max(0.0, (0.5 - share) * 4) if total >= BONUS_MIN_PICKS else 0.0
    return round(min(MAX_POINTS, 1 + bonus), 1)


class PredictionStore:
    def __init__(self, path: str | os.PathLike):
        self.path = str(path)
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        if self.db.execute("SELECT COUNT(*) FROM seasons").fetchone()[0] == 0:
            self.db.execute("INSERT INTO seasons (started_at) VALUES (0)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ---------- 比赛 ----------
    def register(self, match: dict[str, Any], channel_id: int, message_id: int | None, url: str | None = None) -> str:
        key = key_for(match["id"])
        self.db.execute(
            "INSERT OR IGNORE INTO matches (key, match_id, team1, team2, label, start, channel_id, message_id, url)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, match["id"], match["team1"], match["team2"], match["label"], int(match["start"]),
             int(channel_id), message_id, url))
        self.db.commit()
        return key

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM matches WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def by_match_id(self, match_id: str) -> dict[str, Any] | None:
        return self.get(key_for(match_id))

    def find(self, match: dict[str, Any], listed: set[str] | frozenset[str] = frozenset()) -> dict[str, Any] | None:
        """这场比赛登记过的竞猜：ID 相同；或者同一赛事同样两队、开始时间相差不到 6 小时，
        并且旧 ID 已经不在当前比赛列表里（Liquipedia 改期）。规则和 esports_feed.already 一致。"""
        exact = self.by_match_id(match["id"])
        if exact:
            return exact
        base = match_base(match["id"])
        window = SAME_MATCH_WINDOW.total_seconds()
        rows = self.db.execute("SELECT * FROM matches WHERE team1 = ? AND team2 = ? AND ABS(start - ?) < ?",
                               (match["team1"], match["team2"], int(match["start"]), window))
        for r in rows:
            if r["match_id"] not in listed and match_base(r["match_id"]) == base:
                return dict(r)
        return None

    def active(self) -> list[dict[str, Any]]:
        """还没结算或作废的比赛（open / locked）。"""
        rows = self.db.execute("SELECT * FROM matches WHERE status IN (?, ?) ORDER BY start", (OPEN, LOCKED))
        return [dict(r) for r in rows]

    # ---------- 押注 ----------
    def pick(self, key: str, user_id: int, user_name: str, side: int, now: float) -> PickResult:
        m = self.get(key)
        if m is None:
            return PickResult(False, "This prediction no longer exists.")
        if m["status"] != OPEN or now >= m["start"]:
            return PickResult(False, "Picks are closed for this match.")
        if side not in (1, 2):
            return PickResult(False, "Unknown pick.")
        old = self.db.execute("SELECT side FROM picks WHERE key = ? AND user_id = ?", (key, user_id)).fetchone()
        self.db.execute(
            "INSERT INTO picks (key, user_id, user_name, side, picked_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(key, user_id) DO UPDATE SET side = excluded.side, user_name = excluded.user_name,"
            " picked_at = excluded.picked_at",
            (key, user_id, user_name, side, int(now)))
        self.db.commit()
        return PickResult(True, changed=old is not None and old["side"] != side)

    def counts(self, key: str) -> tuple[int, int]:
        rows = dict(self.db.execute("SELECT side, COUNT(*) FROM picks WHERE key = ? GROUP BY side", (key,)).fetchall())
        return rows.get(1, 0), rows.get(2, 0)

    def names(self, key: str) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {1: [], 2: []}
        for r in self.db.execute("SELECT side, user_name FROM picks WHERE key = ? ORDER BY picked_at, user_id", (key,)):
            out[r["side"]].append(r["user_name"])
        return out

    def lock(self, key: str) -> dict[int, list[str]] | None:
        """截止并返回双方押注名单；已经截止过（或不存在）返回 None。"""
        cur = self.db.execute("UPDATE matches SET status = ? WHERE key = ? AND status = ?", (LOCKED, key, OPEN))
        self.db.commit()
        return self.names(key) if cur.rowcount else None

    # ---------- 结算 ----------
    def score_preview(self, key: str, winner: int) -> list[ScoreLine]:
        picks = self.db.execute("SELECT user_id, user_name, side FROM picks WHERE key = ? ORDER BY picked_at, user_id",
                                (key,)).fetchall()
        total = len(picks)
        if not total:
            return []
        share = sum(1 for p in picks if p["side"] == winner) / total
        win_points = points_for(share, total)
        return [ScoreLine(p["user_id"], p["user_name"], p["side"] == winner,
                          win_points if p["side"] == winner else 0.0) for p in picks]

    def resolve(self, key: str, winner: int, now: float) -> list[ScoreLine]:
        """结算一次；已经结算或作废过返回空列表。"""
        cur = self.db.execute("UPDATE matches SET status = ?, winner = ? WHERE key = ? AND status IN (?, ?)",
                              (RESOLVED, winner, key, OPEN, LOCKED))
        if not cur.rowcount:
            self.db.commit()
            return []
        lines = self.score_preview(key, winner)
        self.db.executemany(
            "INSERT OR IGNORE INTO scores (key, user_id, user_name, correct, points, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(key, s.user_id, s.user_name, int(s.correct), s.points, int(now)) for s in lines])
        self.db.commit()
        return lines

    def void(self, key: str) -> bool:
        cur = self.db.execute("UPDATE matches SET status = ? WHERE key = ? AND status IN (?, ?)", (VOID, key, OPEN, LOCKED))
        self.db.commit()
        return bool(cur.rowcount)

    # ---------- 排行榜 ----------
    def leaderboard(self, since: float, until: float) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT user_id, MAX(user_name) AS name, ROUND(SUM(points), 1) AS points, SUM(correct) AS correct,"
            " COUNT(*) AS played FROM scores WHERE resolved_at >= ? AND resolved_at < ?"
            " GROUP BY user_id ORDER BY points DESC, correct DESC, name", (int(since), int(until)))
        return [dict(r) for r in rows]

    def titles(self, since: float, until: float) -> dict[str, Any]:
        """本周称号：预言家（第一）、冷门猎手（单场最高分）、最惨预言家（至少 3 场、命中率最低）。"""
        board = self.leaderboard(since, until)
        best = self.db.execute(
            "SELECT user_name, points FROM scores WHERE resolved_at >= ? AND resolved_at < ? AND correct = 1"
            " ORDER BY points DESC, resolved_at, user_id LIMIT 1", (int(since), int(until))).fetchone()
        eligible = [r for r in board if r["played"] >= 3]
        worst = min(eligible, key=lambda r: (r["correct"] / r["played"], -r["played"]), default=None)
        return {"prophet": board[0] if board else None,
                "underdog": dict(best) if best and best["points"] > 1 else None,
                "worst": worst}

    def season_start(self) -> int:
        return self.db.execute("SELECT MAX(started_at) FROM seasons").fetchone()[0]

    def new_season(self, now: float) -> None:
        self.db.execute("INSERT INTO seasons (started_at) VALUES (?)", (int(now),))
        self.db.commit()


def format_board(title: str, rows: list[dict[str, Any]], limit: int = 10) -> list[str]:
    lines = [title]
    for i, r in enumerate(rows[:limit], 1):
        lines.append(f"{i}. {r['name']} — {r['points']:g} pts ({r['correct']}/{r['played']})")
    if len(lines) == 1:
        lines.append("No resolved predictions yet.")
    return lines


def format_titles(t: dict[str, Any]) -> str | None:
    parts = []
    if t["prophet"]:
        parts.append(f"🔮 Prophet of the week: {t['prophet']['name']}")
    if t["underdog"]:
        parts.append(f"🎯 Underdog hunter: {t['underdog']['user_name']} (+{t['underdog']['points']:g})")
    if t["worst"]:
        parts.append(f"🙈 Worst prophet: {t['worst']['name']} ({t['worst']['correct']}/{t['worst']['played']})")
    return " · ".join(parts) or None
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_predictions.py`
Expected: `42/42 passed`

- [ ] **Step 5: Commit**

```bash
git add server/predictions.py tests/test_predictions.py
git commit -m "Add prediction store with scoring and leaderboards"
```

---

### Task 2: 赛事报名阵容 `rosters.py`

**Files:**
- Create: `server/rosters.py`
- Create: `tests/test_rosters.py`
- Use (already committed): `tests/fixtures/esports/liquipedia_owcs_korea_stage3.json`

**Interfaces:**
- Consumes: `esports._session`、`esports.LIQUIPEDIA_API`
- Produces:
  - 常量 `CACHE_TTL`、`PARSE_INTERVAL`、`MAX_PAGES`、`ROLE_ORDER`
  - `parse_rosters(page_html) -> {队名: [{"id", "role", "flag"}]}`，`role` 取值为 `tank` / `dps` / `support` / `flex` / `None`
  - `roster_pages(tournament) -> [页面…]`
  - `format_roster(members) -> str`，没有阵容时返回 `"Roster unavailable"`
  - `RosterCache(path, fetch=_fetch_page, clock=time.time, sleep=asyncio.sleep)`，方法有 `.load()`、`.save()`、`async .ensure(matches)`、`.lookup(m) -> (r1 | None, r2 | None)`；属性 `.pages`

- [ ] **Step 1: 写失败的测试**

`tests/test_rosters.py`：

```python
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


def page_teams():
    with open(FIXTURE, encoding="utf-8") as f:
        return rosters.parse_rosters(json.load(f)["parse"]["text"])


def match(t1="T1", t2="ZETA DIVISION", tournament=KOREA + "/Regular Season#Week 1"):
    return {"team1": t1, "team2": t2, "tournament": tournament}


def test_parse():
    teams = page_teams()
    check("解析出 9 支参赛队", len(teams) == 9 and {"T1", "ZETA DIVISION", "Crazy Raccoon"} <= set(teams))
    check("T1 的 6 名队员和位置", teams["T1"] == [
        {"id": "Proud", "role": "dps", "flag": "South Korea"}, {"id": "ZEST", "role": "dps", "flag": "South Korea"},
        {"id": "DONGHAK", "role": "tank", "flag": "South Korea"}, {"id": "Jasm1ne", "role": "tank", "flag": "South Korea"},
        {"id": "skewed", "role": "support", "flag": "South Korea"}, {"id": "Bliss", "role": "support", "flag": "South Korea"}])
    check("教练组不混进来", all(m["id"] not in ("RUSH", "Fleta", "GgulTaek") for m in teams["T1"]))
    check("全能位（Flex）识别", "flex" in [m["role"] for m in teams["Crazy Raccoon"]])
    check("没有参赛卡片的页面返回空", rosters.parse_rosters("<div>no cards</div>") == {})


def test_pages_and_format():
    check("候选页面：子页面、上一级、再上一级", rosters.roster_pages(KOREA + "/Regular Season#Week 1") == [
        KOREA + "/Regular Season", KOREA, "Overwatch Champions Series/2026/Asia/Stage 3"])
    check("候选页面最多 3 个", len(rosters.roster_pages("a/b/c/d/e/f")) == 3)
    check("阵容按位置分组", rosters.format_roster(page_teams()["T1"])
          == "🛡️ DONGHAK, Jasm1ne · ⚔️ Proud, ZEST · 💉 skewed, Bliss")
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

    async def fetch(page):
        calls.append(page)
        if page == KOREA:
            return teams
        if page.endswith("Regular Season"):
            return {}             # 子页面没有参赛卡片
        raise RuntimeError("liquipedia down")

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "rosters.json")
        cache = rosters.RosterCache(path, fetch, clock.now, clock.sleep)
        asyncio.run(cache.ensure([match()]))
        check("先找子页面，再找上一级；找到双方就停", calls == [KOREA + "/Regular Season", KOREA])
        check("两次抓取之间至少间隔 30 秒", clock.slept == [30.0])
        t1, t2 = cache.lookup(match())
        check("查到双方阵容", t1 == teams["T1"] and t2 == teams["ZETA DIVISION"])
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
    broken.pages = {"x/y": {"fetched_at": 0, "teams": {"A": [{"id": "a", "role": "tank", "flag": None}]}}}

    async def boom(page):
        raise RuntimeError("down")
    broken.fetch = boom
    asyncio.run(broken.ensure([match("A", "B", tournament="x/y/z")]))
    check("抓取失败时保留旧缓存", broken.lookup(match("A", "B", tournament="x/y/z"))[0][0]["id"] == "a")


if __name__ == "__main__":
    test_parse()
    test_pages_and_format()
    test_cache()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_rosters.py`
Expected: `ModuleNotFoundError: No module named 'rosters'`

- [ ] **Step 3: 实现**

`server/rosters.py`：

```python
"""赛事报名阵容：从 Liquipedia 赛事页面的参赛卡片解析，缓存到 herodata 卷的 rosters.json。

赛事页面（MediaWiki API action=parse，每 30 秒最多 1 次）里每支参赛队是一张
`team-participant-card`：opponent-full 里是队名，toggle-area-content-active 是 Main 名单
（位置图标 alt 是 Tank / DPS / Support / Flex），toggle-area-content-inactive 是教练组（不用）。

比赛的 tournament 形如 `…/Korea/Regular Season#Week 1`，子页面上可能没有参赛卡片，
所以依次找 `…/Korea/Regular Season`、`…/Korea`、再上一级。
这是本赛事的报名阵容，不是某一场的实际首发。
"""
from __future__ import annotations

import asyncio
import html as htmllib
import json
import logging
import os
import re
import tempfile
import time
from typing import Any, Awaitable, Callable

import esports

log = logging.getLogger("mrmeeseeks.rosters")

CACHE_TTL = 24 * 3600            # 同一赛事页面一天最多抓一次
PARSE_INTERVAL = 30.0            # Liquipedia：action=parse 每 30 秒最多 1 次
MAX_PAGES = 3
ROLES = {"tank": "tank", "dps": "dps", "support": "support", "flex": "flex"}
ROLE_ORDER = ("tank", "dps", "flex", "support")

_CARD = 'team-participant-card"><div>'
_TEAM = re.compile(r'team-participant-card__opponent-full.*?<span class="name"[^>]*><a [^>]*title="([^"]+)"', re.S)
_MEMBER = 'class="team-participant-card__member"'
_ROLE = re.compile(r'member-role-left">(?:<[^>]*>)*?<img[^>]*alt="([^"]*)"')
_FLAG = re.compile(r'class="flag">(?:<[^>]*>)*?<img[^>]*alt="([^"]*)"')
_PLAYER = re.compile(r'<span class="name"[^>]*><a [^>]*>([^<]+)</a>')


def parse_rosters(page_html: str) -> dict[str, list[dict[str, Any]]]:
    """赛事页面 -> {队名: [{"id", "role", "flag"}]}，只取 Main 名单。"""
    text = htmllib.unescape(page_html)
    teams: dict[str, list[dict[str, Any]]] = {}
    for card in text.split(_CARD)[1:]:
        team = _TEAM.search(card)
        if not team:
            continue
        main = card.split("toggle-area-content-inactive", 1)[0]
        main = main.split("toggle-area-content-active", 1)[-1]
        members = []
        for chunk in main.split(_MEMBER)[1:]:
            player = _PLAYER.search(chunk)
            if not player:
                continue
            role = _ROLE.search(chunk)
            flag = _FLAG.search(chunk)
            members.append({"id": player.group(1).strip(),
                            "role": ROLES.get(role.group(1).strip().lower()) if role else None,
                            "flag": flag.group(1).strip() if flag and flag.group(1).strip() else None})
        if members:
            teams[team.group(1).replace(" (page does not exist)", "").strip()] = members
    return teams


def roster_pages(tournament: str) -> list[str]:
    """比赛的赛事路径 -> 依次尝试的赛事页面（最具体的在前，最多 MAX_PAGES 个）。"""
    parts = [p for p in tournament.split("#", 1)[0].split("/") if p]
    pages = []
    while parts and len(pages) < MAX_PAGES:
        pages.append("/".join(parts))
        parts = parts[:-1]
    return pages


def format_roster(members: list[dict[str, Any]] | None) -> str:
    """🛡️ A, B · ⚔️ C, D · 💉 E, F；没有阵容时返回 'Roster unavailable'。"""
    if not members:
        return "Roster unavailable"
    icons = {"tank": "🛡️", "dps": "⚔️", "flex": "🔀", "support": "💉", None: "❔"}
    groups: dict[str | None, list[str]] = {}
    for m in members:
        groups.setdefault(m["role"], []).append(m["id"])
    order = [r for r in ROLE_ORDER if r in groups] + ([None] if None in groups else [])
    return " · ".join(f"{icons[r]} {', '.join(groups[r])}" for r in order)


async def _fetch_page(page: str) -> dict[str, list[dict[str, Any]]]:
    params = {"action": "parse", "page": page, "prop": "text", "format": "json", "formatversion": "2"}
    async with esports._session() as session, session.get(esports.LIQUIPEDIA_API, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
    if "error" in data:          # 页面不存在
        return {}
    return parse_rosters((data.get("parse") or {}).get("text") or "")


class RosterCache:
    """path 为 None 时只存在内存里（测试用）。fetch / clock / sleep 可替换，方便测试限速。"""

    def __init__(self, path: str | None,
                 fetch: Callable[[str], Awaitable[dict[str, list]]] = _fetch_page,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.path = path
        self.fetch, self.clock, self.sleep = fetch, clock, sleep
        self.pages: dict[str, dict[str, Any]] = {}
        self._last_fetch = float("-inf")

    def load(self) -> None:
        if self.path is None:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.pages = {k: v for k, v in data.items() if isinstance(v, dict) and "teams" in v}
        except (OSError, ValueError, TypeError) as e:
            log.info("No usable roster cache at %s (%r)", self.path, e)

    def save(self) -> None:
        if self.path is None:
            return
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.pages, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write roster cache to %s", self.path)

    def _fresh(self, page: str) -> bool:
        entry = self.pages.get(page)
        return entry is not None and self.clock() - entry["fetched_at"] < CACHE_TTL

    async def ensure(self, matches: list[dict[str, Any]]) -> None:
        """为这些比赛准备阵容：找到包含双方的页面就停；过期或没缓存的页面才抓，抓之间至少隔 30 秒。"""
        changed = False
        for m in matches:
            if self.lookup(m) != (None, None) and all(self._fresh(p) for p in self._pages_with(m)):
                continue
            for page in roster_pages(m.get("tournament") or ""):
                if not self._fresh(page):
                    wait = PARSE_INTERVAL - (self.clock() - self._last_fetch)
                    if wait > 0:
                        await self.sleep(wait)
                    self._last_fetch = self.clock()
                    try:
                        teams = await self.fetch(page)
                    except Exception:
                        log.exception("Roster fetch failed for %s; keeping cached data", page)
                        continue
                    self.pages[page] = {"fetched_at": self.clock(), "teams": teams}
                    changed = True
                teams = self.pages[page]["teams"]
                if m["team1"] in teams and m["team2"] in teams:
                    break
        if changed:
            self.save()

    def _pages_with(self, m: dict[str, Any]) -> list[str]:
        return [p for p in roster_pages(m.get("tournament") or "")
                if p in self.pages and (m["team1"] in self.pages[p]["teams"] or m["team2"] in self.pages[p]["teams"])]

    def lookup(self, m: dict[str, Any]) -> tuple[list | None, list | None]:
        """只读缓存：按候选页面依次找，第一个包含该队的页面就用它。"""
        out: list[list | None] = []
        for team in (m["team1"], m["team2"]):
            found = None
            for page in roster_pages(m.get("tournament") or ""):
                teams = (self.pages.get(page) or {}).get("teams") or {}
                if team in teams:
                    found = teams[team]
                    break
            out.append(found)
        return out[0], out[1]
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_rosters.py`
Expected: `19/19 passed`。日志里会出现 "down" 的 traceback，来自"抓取失败时保留旧缓存"这个用例，是预期内的。

- [ ] **Step 5: 联网冒烟测试（只请求一次，可选；需要 `.venv` 里有 aiohttp）**

```bash
cd server && ../.venv/bin/python -c "
import asyncio, time, esports, rosters as rs
async def main():
    ms = [m for m in await esports.fetch_matches() if not m['finished'] and 'TBD' not in (m['team1'], m['team2'])]
    if not ms: print('no upcoming match with known teams'); return
    c = rs.RosterCache(None); await c.ensure(ms[:1]); print(list(c.pages)); print(rs.format_roster(c.lookup(ms[0])[0]))
asyncio.run(main())
"; cd ..
```

Expected：打印出抓取的赛事页面，以及一支队伍按位置分组的阵容（例如 `🛡️ … · ⚔️ … · 💉 …`）。如果当前没有对阵已经确定的比赛，打印 `no upcoming match with known teams`，这也算通过。

- [ ] **Step 6: Commit**

```bash
git add server/rosters.py tests/test_rosters.py
git commit -m "Add Liquipedia event roster parsing and cache"
```

---

### Task 3: 竞猜接入推送 `prediction_feed.py`

**Files:**
- Modify: `server/esports_feed.py`
- Create: `server/prediction_feed.py`
- Create: `tests/test_prediction_feed.py`

**Interfaces:**
- Consumes: Task 1 的 `PredictionStore`（`find`/`counts`/`score_preview`/`active`/`leaderboard`/`titles`/`register`/`names`/`lock`/`void`/`resolve`）、`key_for`、`format_board`、`format_titles`；Task 2 的 `RosterCache`（`ensure`/`lookup`）、`format_roster`；`esports_feed` 已有的 `Post`、`TZ`、`DIGEST_HOUR`、`DIGEST_WINDOW`、`ATTRIBUTION`、`PREVIEW_PREFIX`、`_vs`、`_bo`
- Produces:
  - `esports_feed`：
    - `Post.kind`（默认 `"text"`）、`Post.data`；提醒和赛果的 `Post` 带 `data={"match": m}`
    - 状态新增 `leaderboard_week`
    - `EsportsFeed(..., predictions=None)`、`.preview_prediction(channel_id, now=None)`
    - `.tick(now, send, mono, edit=None)`、`.run(send, edit=None)`
  - `prediction_feed`：
    - `view_spec(key, team1, team2, counts, disabled=False) -> dict`、`format_prediction(m, r1, r2)`、`format_lock(entry, names)`、`reveal_line(lines)`、`winner_of(m)`、`week_start(now)`
    - `plan_predictions(now, matches, snap, state, channel_for) -> list[Post]`、`decorate(posts, snap) -> list[Post]`
    - `PredictionFeed(store, rosters)`，方法有 `.prepare(matches)`、`.snapshot(now, matches)`、`.augment(...)`、`async .execute(post, channel_id, send, edit)`、`.after_sent(post, now)`、`.preview(now, matches, routed_here) -> (text, spec) | None`
  - `send` 的签名是 `async (channel_id, text, view=None) -> (message_id, url)`；`edit` 的签名是 `async (channel_id, message_id, view_spec)`。

- [ ] **Step 1: 写失败的测试**

`tests/test_prediction_feed.py`：

```python
"""竞猜接入推送：一场比赛从竞猜消息、提醒、截止、赛果揭晓到结算的完整流程，以及改期、作废、周榜、
发送失败、preview。用内存 SQLite、预先填好的阵容缓存、假的 send / edit。

悉尼 2026-10-26 是周一，AEDT（UTC+11）：悉尼 10:00 = 2026-10-25 23:00 UTC。
运行：python tests/test_prediction_feed.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import esports_feed as ef  # noqa: E402
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
        tick(feed, start + timedelta(minutes=10), discord, mono=2100)
        check("到新的开赛时间截止", store.find(moved, {moved["id"]})["status"] == pr.LOCKED)

        box[0] = []                               # 比赛从汇总页消失，一直没有赛果
        tick(feed, start + timedelta(hours=47), discord, mono=3000)
        check("48 小时内不作废", store.active() != [])
        tick(feed, start + timedelta(hours=48, minutes=10), discord, mono=3700)
        check("48 小时后作废", store.active() == [] and store.find(moved)["status"] == pr.VOID)
        check("作废时编辑原消息为 Voided", discord.edits[-1][2]["labels"] == ["Voided", "Voided"]
              and discord.edits[-1][2]["disabled"])


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
    text, view = sample
    check("preview：下一场的竞猜样例，带阵容和 Preview 前缀", text.startswith(ef.PREVIEW_PREFIX + "🎯 **Who wins?** T1 vs ZETA")
          and "DONGHAK" in text)
    check("preview：按钮禁用", view["disabled"] is True and view["counts"] == [0, 0])
    check("preview：不登记", preds.store.active() == [])
    check("preview：没有路由到这里的比赛返回 None", preds.preview(SYD_10AM_MON, [m], lambda x: False) is None)


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
    test_draw_and_no_channel_and_tbd()
    test_send_failure_retry()
    test_leaderboard()
    test_preview()
    test_pure_helpers()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_prediction_feed.py`
Expected: `ModuleNotFoundError: No module named 'prediction_feed'`

- [ ] **Step 3: 修改 `server/esports_feed.py`**

把下面的补丁原样保存到计划工作区里的文件（例如 `t3-feed.diff`），在仓库根目录执行 `git apply t3-feed.diff`。补丁应用失败时，停下来报告 git apply 的报错，不要绕过它手动修改：

```diff
--- a/server/esports_feed.py
+++ b/server/esports_feed.py
@@ -45,7 +45,7 @@
 
 def new_state() -> dict[str, Any]:
     return {"channel_id": None, "reminded": {}, "resulted": {}, "digest_date": None,
-            "seen_news": [], "initialized": False, "news_initialized": False}
+            "seen_news": [], "initialized": False, "news_initialized": False, "leaderboard_week": None}
 
 
 @dataclass
@@ -53,6 +53,8 @@
     text: str | None                      # None 表示只记账、不发消息
     marks: dict[str, Any] = field(default_factory=dict)
     channel: int | None = None            # 赛区频道；None 表示默认频道（state["channel_id"]）
+    kind: str = "text"                    # text 照常发送；其他类型（竞猜、截止、作废）交给竞猜模块执行
+    data: dict[str, Any] = field(default_factory=dict)   # 提醒、赛果带上 {"match": m}，供竞猜模块补充内容
 
 
 def apply_marks(state: dict[str, Any], marks: dict[str, Any]) -> None:
@@ -62,8 +64,9 @@
     for url in marks.get("seen_news", []):
         if url not in state["seen_news"]:
             state["seen_news"].append(url)
-    if "digest_date" in marks:
-        state["digest_date"] = marks["digest_date"]
+    for key in ("digest_date", "leaderboard_week"):
+        if key in marks:
+            state[key] = marks[key]
     for flag in ("initialized", "news_initialized"):
         if marks.get(flag):
             state[flag] = True
@@ -180,10 +183,12 @@
             if (not m["finished"] and not already(state["reminded"], m, listed)
                     and not (m["team1"] == "TBD" and m["team2"] == "TBD")   # 对阵都没定，不提醒
                     and m["start"] - REMIND_BEFORE.total_seconds() <= ts <= m["start"] + REMIND_LATE.total_seconds()):
-                posts.append(Post(format_reminder(m), {"reminded": {m["id"]: m["start"]}}, target(m)))
+                posts.append(Post(format_reminder(m), {"reminded": {m["id"]: m["start"]}}, target(m),
+                                  data={"match": m}))
             if (m["finished"] and not already(state["resulted"], m, listed)
                     and ts - RESULT_MAX_AGE.total_seconds() <= m["start"] <= ts):
-                posts.append(Post(format_result(m), {"resulted": {m["id"]: m["start"]}}, target(m)))
+                posts.append(Post(format_result(m), {"resulted": {m["id"]: m["start"]}}, target(m),
+                                  data={"match": m}))
     if news is not None and not state["news_initialized"]:
         posts.append(Post(None, {"news_initialized": True, "seen_news": [n["url"] for n in news]}))
     elif news is not None:
@@ -220,8 +225,9 @@
     def __init__(self, path: str,
                  fetch_matches: Callable[[], Awaitable[list[dict]]] = esports.fetch_matches,
                  fetch_news: Callable[[], Awaitable[list[dict]]] = esports.fetch_news,
-                 regions: RegionStore | None = None):
+                 regions: RegionStore | None = None, predictions: Any = None):
         self.path = path
+        self.predictions = predictions          # prediction_feed.PredictionFeed；None 表示不开竞猜
         self.fetch_matches, self.fetch_news = fetch_matches, fetch_news
         self.regions = regions                  # 赛区 -> 频道，None 表示全部发到默认频道
         self.state = new_state()
@@ -276,6 +282,13 @@
         return preview_posts(now or datetime.now(timezone.utc), self.matches,
                              lambda m: self.channel_for(m) == channel_id)
 
+    def preview_prediction(self, channel_id: int, now: datetime | None = None) -> tuple[str, dict] | None:
+        """竞猜消息的样例（按钮禁用），没开竞猜或没有比赛时返回 None。"""
+        if self.predictions is None:
+            return None
+        return self.predictions.preview(now or datetime.now(timezone.utc), self.matches,
+                                        lambda m: self.channel_for(m) == channel_id)
+
     async def refresh(self, mono: float) -> None:
         """到点就重新抓；抓取失败保留上一次的数据。"""
         if mono - self._matches_at >= MATCH_REFRESH:
@@ -284,6 +297,8 @@
                 self.matches = await self.fetch_matches()
             except Exception:
                 log.exception("Esports match fetch failed; keeping previous data")
+            if self.predictions is not None and self.matches is not None:
+                self.predictions.prepare(self.matches)      # 后台准备阵容（限速抓取，不阻塞推送）
         if mono - self._news_at >= NEWS_REFRESH:
             self._news_at = mono
             try:
@@ -291,18 +306,27 @@
             except Exception:
                 log.exception("Esports news fetch failed; keeping previous data")
 
-    async def tick(self, now: datetime, send: Callable[[int, str], Awaitable[None]], mono: float) -> None:
+    async def tick(self, now: datetime, send: Callable[..., Awaitable[Any]], mono: float,
+                   edit: Callable[..., Awaitable[None]] | None = None) -> None:
         await self.refresh(mono)
         state = copy.deepcopy(self.state)
         changed = False
-        for post in plan(now, self.matches, self.news, state, self.channels):
+        posts = plan(now, self.matches, self.news, state, self.channels)
+        if self.predictions is not None:
+            posts = self.predictions.augment(now, self.matches, posts, state, self.channel_for)
+        for post in posts:
             channel_id = post.channel if post.channel is not None else state["channel_id"]
-            if post.text is not None and channel_id is not None:
-                try:
-                    await send(channel_id, post.text)
-                except Exception:
-                    log.exception("Could not post to esports channel %s; will retry", channel_id)
-                    break   # 停止本 tick，下一轮重试剩余消息
+            try:
+                if post.kind == "text":
+                    if post.text is not None and channel_id is not None:
+                        await send(channel_id, post.text)
+                else:
+                    await self.predictions.execute(post, channel_id, send, edit)
+            except Exception:
+                log.exception("Could not post to esports channel %s; will retry", channel_id)
+                break   # 停止本 tick，下一轮重试剩余消息
+            if self.predictions is not None:
+                self.predictions.after_sent(post, now)      # 赛果发出后才结算
             apply_marks(state, post.marks)
             changed = True
         if changed:
@@ -311,10 +335,10 @@
             self.state = state
             self.save()
 
-    async def run(self, send: Callable[[int, str], Awaitable[None]]) -> None:
+    async def run(self, send: Callable[..., Awaitable[Any]], edit: Callable[..., Awaitable[None]] | None = None) -> None:
         while True:
             try:
-                await self.tick(datetime.now(timezone.utc), send, time.monotonic())
+                await self.tick(datetime.now(timezone.utc), send, time.monotonic(), edit)
             except Exception:
                 log.exception("Esports feed tick failed")
             await asyncio.sleep(TICK_SECONDS)
```

- [ ] **Step 4: 新建 `server/prediction_feed.py`**

`server/prediction_feed.py`：

```python
"""赛事竞猜接入推送：每场比赛一条带按钮的竞猜消息，开赛截止公布名单，赛果揭晓谁猜中，每周一发周榜。

EsportsFeed.tick 先用 esports_feed.plan 算出普通推送，再交给 PredictionFeed.augment：
  - decorate：提醒附上竞猜链接和票数；赛果附上剧透揭晓，并带上"发出后结算"的标记
  - plan_predictions（纯函数）：竞猜 / 截止 / 作废 / 周榜
非文本消息由 execute 执行（发带按钮的消息、登记、截止时公布名单、编辑原消息）。
按钮只用描述字典 view_spec 表示，真正的 discord.ui 组件在 prediction_ui.build_view 里生成，
所以这里不依赖 discord，可以离线测试。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

import predictions as pr
import rosters as rs
from esports_feed import (ATTRIBUTION, DIGEST_HOUR, DIGEST_WINDOW, PREVIEW_PREFIX, TZ, Post, _bo, _vs)

log = logging.getLogger("mrmeeseeks.predictions")

PICKS_MIN_LEAD = timedelta(minutes=15)    # 离开赛不足 15 分钟就不再发竞猜消息
VOID_AFTER = timedelta(hours=48)          # 开赛 48 小时后还没结算就作废
ROSTER_NOTE = "-# Registered event roster — the starting lineup may differ. " + ATTRIBUTION


# ---------- 格式 ----------
def view_spec(key: str, team1: str, team2: str, counts: tuple[int, int], disabled: bool = False) -> dict[str, Any]:
    """按钮的描述：prediction_ui.build_view 据此生成 discord 组件。"""
    return {"key": key, "labels": [team1, team2], "counts": list(counts), "disabled": disabled}


def format_prediction(m: dict[str, Any], roster1: list | None, roster2: list | None) -> str:
    return "\n".join([
        f"🎯 **Who wins?** {_vs(m)} · {m['label']}{_bo(m)}",
        f"Starts <t:{m['start']}:t> (<t:{m['start']}:R>) — picks close at start",
        "",
        f"**{m['team1']}**  {rs.format_roster(roster1)}",
        f"**{m['team2']}**  {rs.format_roster(roster2)}",
        ROSTER_NOTE,
    ])


def format_lock(entry: dict[str, Any], names: dict[int, list[str]]) -> str:
    def side(n: int) -> str:
        return ", ".join(names[n]) if names[n] else "nobody"
    return (f"🔒 Picks closed: {entry['team1']} vs {entry['team2']} — "
            f"{entry['team1']}: {side(1)} · {entry['team2']}: {side(2)}")


def reveal_line(lines: list[pr.ScoreLine]) -> str | None:
    if not lines:
        return None
    hit = ", ".join(f"{s.user_name} +{s.points:g}" for s in lines if s.correct) or "nobody"
    miss = ", ".join(s.user_name for s in lines if not s.correct) or "nobody"
    return f"🎯 ||Correct: {hit} · Missed: {miss}||"


def winner_of(m: dict[str, Any]) -> int | None:
    s1, s2 = m["score"]
    return 1 if s1 > s2 else 2 if s2 > s1 else None


def week_start(now: datetime) -> datetime:
    """本周一 00:00（悉尼时间）。"""
    local = now.astimezone(TZ)
    return (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


# ---------- 计划（纯函数） ----------
def plan_predictions(now: datetime, matches: list[dict[str, Any]] | None, snap: dict[str, Any],
                     state: dict[str, Any], channel_for: Callable[[dict[str, Any]], int | None]) -> list[Post]:
    """snap 由 PredictionFeed.snapshot 生成：entries（比赛 ID -> 已登记的竞猜）、active、rosters、weekly、week。"""
    posts: list[Post] = []
    ts = now.timestamp()
    local = now.astimezone(TZ)
    if matches is not None and local.hour >= DIGEST_HOUR:
        candidates = sorted((m for m in matches if not m["finished"]
                             and m["start"] - ts >= PICKS_MIN_LEAD.total_seconds()
                             and m["start"] <= ts + DIGEST_WINDOW.total_seconds()
                             and not (m["team1"] == "TBD" and m["team2"] == "TBD")
                             and snap["entries"].get(m["id"]) is None), key=lambda m: m["start"])
        for m in candidates:
            channel = channel_for(m)
            if channel is None:
                continue
            r1, r2 = snap["rosters"].get(m["id"], (None, None))
            posts.append(Post(format_prediction(m, r1, r2), channel=channel, kind="prediction",
                              data={"match": m, "view": view_spec(pr.key_for(m["id"]), m["team1"], m["team2"], (0, 0))}))
    current_start = {e["key"]: m["start"] for mid, e in snap["entries"].items() if e
                     for m in (matches or []) if m["id"] == mid}
    for e in snap["active"]:
        start = current_start.get(e["key"], e["start"])
        if ts >= start + VOID_AFTER.total_seconds():
            posts.append(Post(None, channel=e["channel_id"], kind="void", data={"entry": e}))
        elif e["status"] == pr.OPEN and ts >= start:
            posts.append(Post(None, channel=e["channel_id"], kind="lock", data={"entry": e}))
    if local.weekday() == 0 and local.hour >= DIGEST_HOUR and state.get("leaderboard_week") != snap["week"]:
        posts.append(Post(snap["weekly"], {"leaderboard_week": snap["week"]}))
    return posts


def decorate(posts: list[Post], snap: dict[str, Any]) -> list[Post]:
    """提醒附上竞猜链接和票数；赛果附上剧透揭晓，并标记发出后结算（平局或没有比分就作废）。"""
    out = []
    for p in posts:
        m = p.data.get("match") if p.kind == "text" else None
        e = snap["entries"].get(m["id"]) if m else None
        if not e or p.text is None:
            out.append(p)
            continue
        if not m["finished"]:
            link = f" · {e['url']}" if e.get("url") else ""
            p = replace(p, text=f"{p.text}\n🎯 Picks: {e['team1']} · {e['counts'][0]} — "
                                f"{e['team2']} · {e['counts'][1]}{link}")
        elif e["status"] in (pr.OPEN, pr.LOCKED):
            winner = winner_of(m)
            line = reveal_line(snap["previews"].get(m["id"], []))
            text = p.text
            if line:
                body, _, last = text.rpartition("\n")
                text = f"{body}\n{line}\n{last}" if body else f"{text}\n{line}"
            p = replace(p, text=text, data={**p.data, "resolve": (e["key"], winner)})
        out.append(p)
    return out


# ---------- 运行 ----------
class PredictionFeed:
    def __init__(self, store: pr.PredictionStore, rosters: rs.RosterCache):
        self.store, self.rosters = store, rosters
        self._roster_task: asyncio.Task | None = None

    def prepare(self, matches: list[dict[str, Any]]) -> None:
        """后台为未来 24 小时的比赛准备阵容（RosterCache 自己限速），不阻塞推送。"""
        if self._roster_task is not None and not self._roster_task.done():
            return
        upcoming = [m for m in matches if not m["finished"] and not (m["team1"] == "TBD" and m["team2"] == "TBD")]
        self._roster_task = asyncio.get_running_loop().create_task(self._ensure(upcoming))

    async def _ensure(self, matches: list[dict[str, Any]]) -> None:
        try:
            await self.rosters.ensure(matches)
        except Exception:
            log.exception("Roster refresh failed")

    def snapshot(self, now: datetime, matches: list[dict[str, Any]] | None) -> dict[str, Any]:
        listed = {m["id"] for m in matches or []}
        entries: dict[str, dict | None] = {}
        previews: dict[str, list] = {}
        roster_map: dict[str, tuple] = {}
        for m in matches or []:
            e = self.store.find(m, listed)
            if e:
                e = {**e, "counts": self.store.counts(e["key"])}
                if m["finished"] and e["status"] in (pr.OPEN, pr.LOCKED) and winner_of(m):
                    previews[m["id"]] = self.store.score_preview(e["key"], winner_of(m))
            else:
                roster_map[m["id"]] = self.rosters.lookup(m)
            entries[m["id"]] = e
        week = week_start(now)
        rows = self.store.leaderboard((week - timedelta(days=7)).timestamp(), week.timestamp())
        weekly = None
        if rows:
            since = (week - timedelta(days=7)).timestamp()
            lines = pr.format_board(f"🏆 **Prediction leaderboard — week of {(week - timedelta(days=7)).date()}**", rows)
            titles = pr.format_titles(self.store.titles(since, week.timestamp()))
            weekly = "\n".join(lines + ([titles] if titles else []))
        return {"entries": entries, "active": self.store.active(), "previews": previews, "rosters": roster_map,
                "weekly": weekly, "week": week.date().isoformat()}

    def augment(self, now: datetime, matches: list[dict[str, Any]] | None, posts: list[Post],
                state: dict[str, Any], channel_for: Callable[[dict[str, Any]], int | None]) -> list[Post]:
        snap = self.snapshot(now, matches)
        return decorate(posts, snap) + plan_predictions(now, matches, snap, state, channel_for)

    async def execute(self, post: Post, channel_id: int | None, send: Callable[..., Awaitable[Any]],
                      edit: Callable[..., Awaitable[None]] | None) -> None:
        """发送失败就抛出，让 tick 停在这里、下一轮重试；编辑失败只记日志。"""
        if post.kind == "prediction":
            if channel_id is None:
                return
            m = post.data["match"]
            sent = await send(channel_id, post.text, post.data["view"])
            message_id, url = sent if sent else (None, None)
            self.store.register(m, channel_id, message_id, url)
        elif post.kind == "lock":
            e = post.data["entry"]
            names = self.store.names(e["key"])
            await send(e["channel_id"], format_lock(e, names))
            if self.store.lock(e["key"]) is not None:
                await self._edit(edit, e, disabled=True)
        elif post.kind == "void":
            e = post.data["entry"]
            if self.store.void(e["key"]):
                await self._edit(edit, e, disabled=True, labels=("Voided", "Voided"))

    async def _edit(self, edit, e: dict[str, Any], disabled: bool, labels: tuple[str, str] | None = None) -> None:
        if edit is None or not e.get("message_id"):
            return
        t1, t2 = labels or (e["team1"], e["team2"])
        try:
            await edit(e["channel_id"], e["message_id"], view_spec(e["key"], t1, t2, self.store.counts(e["key"]), disabled))
        except Exception:
            log.exception("Could not edit prediction message %s", e.get("message_id"))

    def after_sent(self, post: Post, now: datetime) -> None:
        resolve = post.data.get("resolve")
        if not resolve:
            return
        key, winner = resolve
        if winner is None:
            self.store.void(key)
        else:
            self.store.resolve(key, winner, now.timestamp())

    def preview(self, now: datetime, matches: list[dict[str, Any]] | None,
                routed_here: Callable[[dict[str, Any]], bool]) -> tuple[str, dict] | None:
        """下一场路由到当前频道的比赛的竞猜消息样例（按钮禁用，不登记）。"""
        ts = now.timestamp()
        upcoming = sorted((m for m in matches or [] if routed_here(m) and not m["finished"] and m["start"] >= ts
                           and not (m["team1"] == "TBD" and m["team2"] == "TBD")), key=lambda m: m["start"])
        if not upcoming:
            return None
        m = upcoming[0]
        r1, r2 = self.rosters.lookup(m)
        return (PREVIEW_PREFIX + format_prediction(m, r1, r2),
                view_spec(pr.key_for(m["id"]), m["team1"], m["team2"], (0, 0), disabled=True))
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_prediction_feed.py`
Expected: `39/39 passed`。日志里会出现 "discord down" 的 traceback，来自"发送失败重试"这个用例，是预期内的。

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_esports_feed.py`
Expected: `81/81 passed`（没开竞猜时，推送的行为不变）

- [ ] **Step 6: Commit**

```bash
git add server/esports_feed.py server/prediction_feed.py tests/test_prediction_feed.py
git commit -m "Plan prediction posts, lock, void and weekly leaderboard in the esports feed"
```

---

### Task 4: 竞猜按钮 `prediction_ui.py`

**Files:**
- Create: `server/prediction_ui.py`
- Create: `tests/test_prediction_ui.py`

**Interfaces:**
- Consumes: Task 1 的 `PredictionStore`（`get`/`pick`/`counts`）、`OPEN`
- Produces:
  - `TEMPLATE`、`MIN_EDIT_INTERVAL`、`custom_id(key, side)`、`label(team, count)`
  - `PredictButton(DynamicItem[Button], template=TEMPLATE)`，类属性 `handler`
  - `build_view(spec) -> discord.ui.View`（`timeout=None`）
  - `PredictionUI(store, clock=time.time, min_edit_interval=2.0)`，方法有 `async .on_pick(interaction, key, side)`、`async .refresh(message, key)`

- [ ] **Step 1: 写失败的测试**

`tests/test_prediction_ui.py`：

```python
"""竞猜按钮：custom_id 模板、View 生成、点击（押注、改选、截止后、ephemeral 回复）、票数编辑节流。
用真实的 discord.ui 组件 + 假的 Interaction / Message。

运行：python tests/test_prediction_ui.py
"""
import asyncio
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import prediction_ui as ui  # noqa: E402
import predictions as pr  # noqa: E402

results = []
NS = types.SimpleNamespace
START = 1_791_000_000


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


class Clock:
    def __init__(self, t=START - 3600):
        self.t = t

    def __call__(self):
        return self.t


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content, ephemeral=False):
        self.sent.append((content, ephemeral))


class FakeMessage:
    def __init__(self):
        self.views = []

    async def edit(self, view=None):
        self.views.append(view)


def interaction(uid, name, message):
    return NS(user=NS(id=uid, display_name=name), response=FakeResponse(), message=message)


def labels(view):
    return [c.item.label for c in view.children]      # DynamicItem 包着真正的 Button


def store_with_match():
    s = pr.PredictionStore(":memory:")
    key = s.register({"id": "m1", "start": START, "team1": "T1", "team2": "ZETA DIVISION", "label": "x"}, 42, 7)
    return s, key


def test_view_and_template():
    key = pr.key_for("m1")
    view = ui.build_view({"key": key, "labels": ["T1", "ZETA DIVISION"], "counts": [3, 1], "disabled": False})
    check("View 持久化（timeout=None）", view.timeout is None)
    check("两个按钮：队名 · 票数", labels(view) == ["T1 · 3", "ZETA DIVISION · 1"])
    check("custom_id 符合模板", [c.item.custom_id for c in view.children] == [f"pred:{key}:1", f"pred:{key}:2"]
          and all(re.fullmatch(ui.TEMPLATE, c.item.custom_id) for c in view.children))
    disabled = ui.build_view({"key": key, "labels": ["A", "B"], "counts": [0, 0], "disabled": True})
    check("禁用的按钮", all(c.item.disabled for c in disabled.children))
    check("长队名截到 80 字符", len(ui.label("X" * 100, 12)) == 80)
    item = asyncio.run(ui.PredictButton.from_custom_id(None, NS(label="T1 · 3", disabled=False),
                                                       re.fullmatch(ui.TEMPLATE, f"pred:{key}:2")))
    check("from_custom_id 解析出 key 和 side", item.key == key and item.side == 2)


def test_pick_flow():
    s, key = store_with_match()
    clock = Clock()
    handler = ui.PredictionUI(s, clock, min_edit_interval=0)
    msg = FakeMessage()
    it = interaction(1, "Alice", msg)
    asyncio.run(handler.on_pick(it, key, 1))
    check("押注成功：只回复本人", it.response.sent == [("You picked **T1** — you can change it until the match starts.", True)])
    check("更新按钮票数", labels(msg.views[-1]) == ["T1 · 1", "ZETA DIVISION · 0"])
    it = interaction(1, "Alice", msg)
    asyncio.run(handler.on_pick(it, key, 2))
    check("改选：提示 Changed", it.response.sent[0][0].startswith("Changed your pick to **ZETA DIVISION**")
          and labels(msg.views[-1]) == ["T1 · 0", "ZETA DIVISION · 1"])
    clock.t = START
    it = interaction(2, "Bob", msg)
    asyncio.run(handler.on_pick(it, key, 1))
    check("开赛后点击：拒绝并说明，不改票数", it.response.sent == [("Picks are closed for this match.", True)]
          and s.counts(key) == (0, 1))


def test_button_callback():
    s, key = store_with_match()
    ui.PredictButton.handler = None
    it = interaction(1, "Alice", FakeMessage())
    asyncio.run(ui.PredictButton(key, 1).callback(it))
    check("没有 handler 时提示未启用", "not enabled" in it.response.sent[0][0])
    ui.PredictButton.handler = ui.PredictionUI(s, Clock(), min_edit_interval=0)
    it = interaction(1, "Alice", FakeMessage())
    asyncio.run(ui.PredictButton(key, 2).callback(it))
    check("按钮回调交给 handler", s.counts(key) == (0, 1))
    ui.PredictButton.handler = None


def test_throttle():
    s, key = store_with_match()
    clock = Clock()
    handler = ui.PredictionUI(s, clock, min_edit_interval=0.05)
    msg = FakeMessage()

    async def burst():
        for uid in range(1, 6):
            await handler.on_pick(interaction(uid, f"u{uid}", msg), key, 1)
        await asyncio.sleep(0.01)
        n_immediate = len(msg.views)
        clock.t += 0.05
        await asyncio.sleep(0.1)
        return n_immediate

    immediate = asyncio.run(burst())
    check("连续 5 次点击：第一次立即编辑，其余合并", immediate == 1)
    check("合并后补一次编辑，显示最终票数", len(msg.views) == 2 and labels(msg.views[-1]) == ["T1 · 5", "ZETA DIVISION · 0"])


def test_edit_failure_and_closed_label():
    s, key = store_with_match()

    class Broken:
        async def edit(self, view=None):
            raise RuntimeError("unknown message")
    handler = ui.PredictionUI(s, Clock(), min_edit_interval=0)
    it = interaction(1, "Alice", Broken())
    asyncio.run(handler.on_pick(it, key, 1))
    check("编辑失败不影响押注", s.counts(key) == (1, 0) and it.response.sent[0][1] is True)
    s.lock(key)
    msg = FakeMessage()
    asyncio.run(handler.refresh(msg, key))
    check("截止后刷新：按钮禁用", all(c.item.disabled for c in msg.views[-1].children))


if __name__ == "__main__":
    test_view_and_template()
    test_pick_flow()
    test_button_callback()
    test_throttle()
    test_edit_failure_and_closed_label()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_prediction_ui.py`
Expected: `ModuleNotFoundError: No module named 'prediction_ui'`

- [ ] **Step 3: 实现**

`server/prediction_ui.py`：

```python
"""竞猜按钮：discord.ui.DynamicItem，custom_id 模板 `pred:<key>:<1|2>`。

bot 启动时 add_dynamic_items(PredictButton) 一次，之后所有竞猜消息上的按钮（包括重启前发的）都能点。
点击：记录这一票 → 只回复点的人（ephemeral）→ 更新这条消息按钮上的票数。
同一场比赛的票数编辑每 MIN_EDIT_INTERVAL 秒最多一次，期间的点击合并成一次延后的编辑，避免撞 Discord 速率限制。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Callable

import discord

import predictions as pr

log = logging.getLogger("mrmeeseeks.prediction_ui")

TEMPLATE = r"pred:(?P<key>[0-9a-f]{12}):(?P<side>[12])"
MIN_EDIT_INTERVAL = 2.0


def custom_id(key: str, side: int) -> str:
    return f"pred:{key}:{side}"


def label(team: str, count: int) -> str:
    return f"{team} · {count}"[:80]          # Discord 按钮文字最长 80


class PredictButton(discord.ui.DynamicItem[discord.ui.Button], template=TEMPLATE):
    handler: "PredictionUI | None" = None     # bot 启动时设置

    def __init__(self, key: str, side: int, text: str = "…", disabled: bool = False):
        style = discord.ButtonStyle.primary if side == 1 else discord.ButtonStyle.danger
        super().__init__(discord.ui.Button(label=text, custom_id=custom_id(key, side), style=style, disabled=disabled))
        self.key, self.side = key, side

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: re.Match[str]) -> "PredictButton":
        return cls(match["key"], int(match["side"]), item.label or "…", item.disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        if PredictButton.handler is None:
            await interaction.response.send_message("Predictions are not enabled right now.", ephemeral=True)
            return
        await PredictButton.handler.on_pick(interaction, self.key, self.side)


def build_view(spec: dict[str, Any]) -> discord.ui.View:
    """prediction_feed.view_spec -> 持久化 View（timeout=None）。"""
    view = discord.ui.View(timeout=None)
    for side, (team, count) in enumerate(zip(spec["labels"], spec["counts"]), start=1):
        view.add_item(PredictButton(spec["key"], side, label(team, count), spec["disabled"]))
    return view


class PredictionUI:
    def __init__(self, store: pr.PredictionStore, clock: Callable[[], float] = time.time,
                 min_edit_interval: float = MIN_EDIT_INTERVAL):
        self.store, self.clock, self.min_edit_interval = store, clock, min_edit_interval
        self._last_edit: dict[str, float] = {}
        self._pending: dict[str, asyncio.Task] = {}

    async def on_pick(self, interaction: discord.Interaction, key: str, side: int) -> None:
        entry = self.store.get(key)
        result = self.store.pick(key, interaction.user.id, interaction.user.display_name, side, self.clock())
        if not result.ok:
            await interaction.response.send_message(result.reason, ephemeral=True)
            return
        team = entry["team1"] if side == 1 else entry["team2"]
        verb = "Changed your pick to" if result.changed else "You picked"
        await interaction.response.send_message(f"{verb} **{team}** — you can change it until the match starts.",
                                                ephemeral=True)
        await self.refresh(interaction.message, key)

    async def refresh(self, message: Any, key: str) -> None:
        """更新票数；距离上次编辑不到 min_edit_interval 秒就排一次延后的编辑（已经排了就不再排）。"""
        if message is None:
            return
        wait = self.min_edit_interval - (self.clock() - self._last_edit.get(key, float("-inf")))
        if wait <= 0:
            await self._edit(message, key)
        elif key not in self._pending or self._pending[key].done():
            self._pending[key] = asyncio.get_running_loop().create_task(self._edit_later(message, key, wait))

    async def _edit_later(self, message: Any, key: str, wait: float) -> None:
        await asyncio.sleep(wait)
        await self._edit(message, key)

    async def _edit(self, message: Any, key: str) -> None:
        entry = self.store.get(key)
        if entry is None:
            return
        self._last_edit[key] = self.clock()
        n1, n2 = self.store.counts(key)
        spec = {"key": key, "labels": [entry["team1"], entry["team2"]], "counts": [n1, n2],
                "disabled": entry["status"] != pr.OPEN}
        try:
            await message.edit(view=build_view(spec))
        except Exception:
            log.exception("Could not update prediction counts for %s", key)
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_prediction_ui.py`
Expected: `16/16 passed`。日志里会出现 "unknown message" 的 traceback，来自"编辑失败不影响押注"这个用例，是预期内的。

- [ ] **Step 5: Commit**

```bash
git add server/prediction_ui.py tests/test_prediction_ui.py
git commit -m "Add persistent prediction buttons with throttled count updates"
```

---

### Task 5: 接入 bot、指令与文档

**Files:**
- Modify: `server/bot.py`
- Modify: `tests/test_esports_bot.py`
- Create: `tests/test_prediction_bot.py`
- Modify: `README.md`、`docs/ARCHITECTURE.md`

**Interfaces:**
- Consumes: Task 1–4 的全部 Produces
- Produces:
  - `ARG_COMMANDS` 包含 `"predictions"`，`HELP_TEXT` 加上说明
  - `MeeseeksBot(..., predictions=None, prediction_ui=None)`；`setup_hook` 里调用 `add_dynamic_items(PredictButton)` 并设置 `PredictButton.handler`
  - `post_esports(channel_id, text, view=None) -> (id, jump_url)`、`edit_esports(channel_id, message_id, view)`
  - `_predictions(message, arg)`；`_esports` 的 preview 分支多发一条竞猜样例
  - `main()`：创建 `PredictionStore(state_dir / "predictions.db")` 和 `RosterCache(<英雄数据缓存目录>/rosters.json)`，再创建 `EsportsFeed(..., predictions=PredictionFeed(...))`，然后启动 `esports_feed.run(bot.post_esports, bot.edit_esports)`

这一步是启动时的接线（`main()` 需要 Discord Token），没有对应的单元测试，靠完整测试和部署后的验证来覆盖。

- [ ] **Step 1: 写失败的测试**

`tests/test_prediction_bot.py`：

```python
"""竞猜的 Discord 接入：predictions 指令（榜单、自己的排名、reset 权限）、preview 的竞猜样例、
带按钮的发送与编辑、启动时注册按钮。用假的 Discord 对象，不连 Discord。

运行：python tests/test_prediction_bot.py
"""
import asyncio
import os
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import bot  # noqa: E402
import prediction_ui as ui  # noqa: E402
import predictions as pr  # noqa: E402
from esports_feed import EsportsFeed  # noqa: E402
from prediction_feed import PredictionFeed  # noqa: E402
from rosters import RosterCache  # noqa: E402

results = []
NS = types.SimpleNamespace
BOT_MEMBER = NS(id=2)


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


class FakeChannel:
    def __init__(self, channel_id=555, manage=True):
        self.id, self.manage, self.sent, self.edited = channel_id, manage, [], []

    def permissions_for(self, member):
        if member is BOT_MEMBER:
            return NS(manage_channels=False, manage_guild=False, send_messages=True)
        return NS(manage_channels=self.manage, manage_guild=self.manage, send_messages=True)

    async def send(self, text, allowed_mentions=None, view=None, embed=None):
        self.sent.append((text, allowed_mentions, view))
        return NS(id=9000 + len(self.sent), jump_url=f"https://discord.com/channels/1/{self.id}/{9000 + len(self.sent)}")

    def get_partial_message(self, message_id):
        channel = self

        class Partial:
            async def edit(self, view=None):
                channel.edited.append((message_id, view))
        return Partial()


class FakeMessage:
    def __init__(self, manage=True, uid=1, channel=None):
        self.author = NS(id=uid)
        self.guild = NS(id=9, me=BOT_MEMBER)
        self.channel = channel or FakeChannel(manage=manage)
        self.replies = []

    async def reply(self, content, allowed_mentions=None):
        self.replies.append(content)


def make_bot(store=None, feed=None):
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    b.predictions, b.esports_feed, b.prediction_ui = store, feed, None
    b._can_manage = bot.MeeseeksBot._can_manage.__get__(b)
    return b


def resolved_store():
    s = pr.PredictionStore(":memory:")
    now = time.time()
    k = s.register({"id": "m1", "start": now - 7200, "team1": "A", "team2": "B", "label": "x"}, 1, 1)
    for uid, side in [(1, 1), (2, 2), (3, 1)]:
        s.pick(k, uid, f"user{uid}", side, now - 9000)
    s.resolve(k, 2, now - 60)
    return s


def test_routing_and_help():
    check("predictions 路由", bot.parse_command("predictions") == ("predictions", "")
          and bot.parse_command("predictions reset") == ("predictions", "reset"))
    check("帮助里有 predictions", "@mrmeeseeks predictions" in bot.HELP_TEXT and "predictions reset" in bot.HELP_TEXT)


def test_predictions_command():
    msg = FakeMessage()
    asyncio.run(make_bot(None)._predictions(msg, ""))
    check("没开竞猜时提示", "not enabled" in msg.replies[0])

    s = resolved_store()
    msg = FakeMessage(uid=2)
    asyncio.run(make_bot(s)._predictions(msg, ""))
    text = msg.replies[0]
    check("本周榜和赛季榜都有", "🏆 **This week**" in text and "🏅 **Season**" in text)
    check("冷门猜中的 user2 排第一（3 人中 1 人押中：1.7 分）", "1. user2 — 1.7 pts (1/1)" in text)
    check("显示自己的排名", "You: #1 — 1.7 pts (1/1)" in text)

    msg = FakeMessage(manage=False)
    asyncio.run(make_bot(s)._predictions(msg, "reset"))
    check("reset 需要管理服务器权限", "Manage Server" in msg.replies[0] and s.season_start() == 0)
    msg = FakeMessage(manage=True)
    asyncio.run(make_bot(s)._predictions(msg, "reset"))
    check("reset 开新赛季", s.season_start() > 0 and msg.replies[0].startswith("🏁"))
    msg = FakeMessage(uid=2)
    asyncio.run(make_bot(s)._predictions(msg, ""))
    check("新赛季总榜清零，本周榜不受影响", "1. user2" in msg.replies[0].split("🏅")[0]
          and "No resolved predictions yet." in msg.replies[0].split("🏅")[1])


def test_preview_includes_prediction():
    now = datetime.now(timezone.utc)
    tour = "Overwatch Champions Series/2026/Asia/Stage 3/Korea/Regular Season#Week 3"
    start = int((now + timedelta(hours=2)).timestamp())
    m = {"id": f"{tour}|{start}|T1|ZETA DIVISION", "start": start, "tournament": tour, "label": "OWCS Korea",
         "team1": "T1", "team2": "ZETA DIVISION", "best_of": 5, "finished": False, "score": None,
         "url": "https://liquipedia.net/x"}
    with tempfile.TemporaryDirectory() as d:
        store = pr.PredictionStore(":memory:")
        feed = EsportsFeed(os.path.join(d, "s.json"), predictions=PredictionFeed(store, RosterCache(None)))
        feed.matches = [m]
        feed.set_channel(555)
        msg = FakeMessage(manage=True)
        asyncio.run(make_bot(store, feed)._esports(msg, "preview"))
        sample = msg.channel.sent[-1]
        check("preview 最后多一条竞猜样例", sample[0].startswith("🧪 Preview\n🎯 **Who wins?** T1 vs ZETA DIVISION"))
        check("样例的按钮禁用", sample[2] is not None and all(c.item.disabled for c in sample[2].children))
        check("preview 不登记竞猜", store.active() == [])


def test_post_and_edit():
    channel = FakeChannel(777)
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)

    async def ready():
        return None

    async def get_channel(cid):
        return channel
    b.wait_until_ready, b._channel = ready, get_channel
    spec = {"key": pr.key_for("m1"), "labels": ["T1", "ZETA"], "counts": [0, 0], "disabled": False}
    mid, url = asyncio.run(b.post_esports(777, "🎯 hi", spec))
    check("带按钮发送，返回消息 ID 和链接", (mid, url) == (9001, "https://discord.com/channels/1/777/9001")
          and channel.sent[0][2] is not None and len(channel.sent[0][2].children) == 2)
    check("不带按钮时照旧（补丁推送共用）", asyncio.run(b.post_esports(777, "plain"))[0] == 9002
          and channel.sent[1][2] is None)
    asyncio.run(b.edit_esports(777, 9001, dict(spec, disabled=True)))
    check("编辑原消息的按钮", channel.edited[0][0] == 9001 and all(c.item.disabled for c in channel.edited[0][1].children))


def test_setup_hook():
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    added = []
    b.add_dynamic_items = lambda *items: added.extend(items)
    b.wish_view = None
    b.prediction_ui = ui.PredictionUI(pr.PredictionStore(":memory:"))
    asyncio.run(b.setup_hook())
    check("启动时注册竞猜按钮并设置 handler", added == [ui.PredictButton] and ui.PredictButton.handler is b.prediction_ui)
    ui.PredictButton.handler = None


if __name__ == "__main__":
    test_routing_and_help()
    test_predictions_command()
    test_preview_includes_prediction()
    test_post_and_edit()
    test_setup_hook()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
```

再应用这个补丁：现有测试里假的 `send` 原来返回 `None`，现在要和 discord.py 一样返回发出的消息，因为 `post_esports` 要用到它的 ID：

把下面的补丁原样保存到计划工作区里的文件（例如 `t5-esports-bot-test.diff`），在仓库根目录执行 `git apply t5-esports-bot-test.diff`。补丁应用失败时，停下来报告 git apply 的报错，不要绕过它手动修改：

```diff
--- a/tests/test_esports_bot.py
+++ b/tests/test_esports_bot.py
@@ -42,6 +42,8 @@
 
     async def send(self, text, allowed_mentions=None, embed=None):
         self.sent.append((text, allowed_mentions, embed))
+        # 和真实的 discord.py 一样返回发出的消息（post_esports 要用它的 id 和链接）
+        return NS(id=len(self.sent), jump_url=f"https://discord.com/channels/9/{self.id}/{len(self.sent)}")
 
 
 class FakeMessage:
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_prediction_bot.py`
Expected: `先出现 `FAIL predictions 路由` 和 `FAIL 帮助里有 predictions`，然后报 `AttributeError: 'MeeseeksBot' object has no attribute '_predictions'``

- [ ] **Step 3: 修改 `server/bot.py`**

把下面的补丁原样保存到计划工作区里的文件（例如 `t5-bot.diff`），在仓库根目录执行 `git apply t5-bot.diff`。补丁应用失败时，停下来报告 git apply 的报错，不要绕过它手动修改：

```diff
--- a/server/bot.py
+++ b/server/bot.py
@@ -36,7 +36,11 @@
 
 import overfast
 import matchups
+import predictions as pr
 from esports_feed import EsportsFeed
+from prediction_feed import PredictionFeed, week_start
+from prediction_ui import PredictButton, PredictionUI, build_view
+from rosters import RosterCache
 from live_watch import (DEFAULT_POLL_INTERVAL, BilibiliClient, LiveError, LiveEvent, LiveStore, LiveWatcher, Source,
                         parse_bilibili_target)
 from regions import KNOWN_REGIONS, RegionError, RegionStore, normalize_region
@@ -75,6 +79,8 @@
     "`@mrmeeseeks esports here` / `esports off` Post OWCS schedule, reminders, results and news in this channel "
     "(needs Manage Channels); `esports status` shows the current setup; "
     "`esports preview` posts sample posts for this channel right now (for testing)\n"
+    "`@mrmeeseeks predictions` Prediction leaderboard (this week, season, your rank); "
+    "`predictions reset` starts a new season (needs Manage Server)\n"
     "`@mrmeeseeks region cn` Make this channel the home of one region (cn, asia, korea, japan, pacific, emea, na, world): "
     "its matches and live alerts go here instead of the esports channel; `region cn off` unbinds, `region` lists\n"
     "`@mrmeeseeks live add <region> <Bilibili room url> [label]` Alert that region's channel when the room goes live; "
@@ -87,7 +93,7 @@
     "`Tracer HP`, `how many Cassidy headshots kill Mauga at 30m?`"
 )
 COMMANDS = ("connect", "status", "disconnect", "help", "analyze", "label")
-ARG_COMMANDS = ("player", "esports", "patches", "wishes", "region", "live")     # 带参数的指令；参数保留大小写（BattleTag 大小写敏感）
+ARG_COMMANDS = ("player", "esports", "patches", "wishes", "region", "live", "predictions")     # 带参数的指令；参数保留大小写（BattleTag 大小写敏感）
 LIVE_USAGE = ("Usage: `@mrmeeseeks live add <region> <Bilibili room url or id> [label]`, "
               "`live remove <room url or id>` or `live list`. Bind the region's channel first with `@mrmeeseeks region <region>`.")
 ASK_COOLDOWN_SECONDS = 5
@@ -190,7 +196,8 @@
                  wish_channel_id: int | None = None, wish_desk: WishDesk | None = None,
                  wish_plans: WishPlans | None = None, patch_feed: PatchFeed | None = None,
                  regions: RegionStore | None = None, live_watcher: LiveWatcher | None = None,
-                 live_notify_offline: bool = False):
+                 live_notify_offline: bool = False, predictions: pr.PredictionStore | None = None,
+                 prediction_ui: PredictionUI | None = None):
         intents = discord.Intents.default()
         intents.message_content = True
         super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
@@ -217,6 +224,8 @@
         self.wish_plans = wish_plans
         self.wish_view = WishEntryView(wish_desk) if wish_desk else None
         self._wish_entry_ready = False
+        self.predictions = predictions
+        self.prediction_ui = prediction_ui
 
     def hero_name(self, key: str) -> str:
         return self.names.get(key) or overfast.hero_name(key)
@@ -225,6 +234,9 @@
         return {c.user_id for c in self.registry.in_channel(channel_id) if c.user_id}
 
     async def setup_hook(self) -> None:
+        if self.prediction_ui:
+            PredictButton.handler = self.prediction_ui
+            self.add_dynamic_items(PredictButton)   # 竞猜按钮：重启前发的竞猜消息照样能点
         if self.wish_view:
             self.add_view(self.wish_view)     # 持久化按钮：重启前发的入口消息照样能点
 
@@ -278,6 +290,8 @@
             await self._player(message, arg)
         elif cmd == "esports":
             await self._esports(message, arg)
+        elif cmd == "predictions":
+            await self._predictions(message, arg)
         elif cmd == "patches":
             await self._patches(message, arg)
         elif cmd == "region":
@@ -336,6 +350,10 @@
                 return
             for text in posts:
                 await message.channel.send(text, allowed_mentions=discord.AllowedMentions.none())
+            sample = feed.preview_prediction(message.channel.id)   # 开了竞猜时，再发一条按钮禁用的竞猜样例
+            if sample:
+                text, spec = sample
+                await message.channel.send(text, view=build_view(spec), allowed_mentions=discord.AllowedMentions.none())
         else:
             await message.reply("Usage: `@mrmeeseeks esports here`, `esports off`, `esports status` or `esports preview`.")
 
@@ -479,12 +497,45 @@
             await channel.send(f"⚫ **{src.label}** ({src.region.upper()}) ended the stream.",
                                allowed_mentions=discord.AllowedMentions.none())
 
-    async def post_esports(self, channel_id: int, text: str) -> None:
-        """推送任务用的发送函数：等 bot 登录完成再发，不 ping 任何人。"""
+    async def post_esports(self, channel_id: int, text: str, view: dict | None = None) -> tuple[int, str]:
+        """推送任务用的发送函数：等 bot 登录完成再发，不 ping 任何人。view 是竞猜按钮的描述（prediction_feed.view_spec）。
+        返回 (消息 ID, 消息链接)，竞猜消息据此登记。"""
         await self.wait_until_ready()
         channel = await self._channel(channel_id)
-        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
+        kwargs = {"view": build_view(view)} if view else {}
+        msg = await channel.send(text, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
+        return msg.id, msg.jump_url
+
+    async def edit_esports(self, channel_id: int, message_id: int, view: dict) -> None:
+        """竞猜截止 / 作废时更新原消息的按钮。"""
+        await self.wait_until_ready()
+        channel = await self._channel(channel_id)
+        await channel.get_partial_message(message_id).edit(view=build_view(view))
 
+    async def _predictions(self, message: discord.Message, arg: str) -> None:
+        store = self.predictions
+        if store is None:
+            await message.reply("Predictions are not enabled on this server.")
+            return
+        now = time.time()
+        if arg.strip().lower() == "reset":
+            perms = message.channel.permissions_for(message.author) if message.guild else None
+            if perms is None or not perms.manage_guild:
+                await message.reply("You need Manage Server permission to start a new prediction season.")
+                return
+            store.new_season(now)
+            await message.reply("🏁 A new prediction season has started. The weekly leaderboard is unaffected.")
+            return
+        week = week_start(datetime.now(timezone.utc)).timestamp()
+        weekly = store.leaderboard(week, now + 1)
+        season = store.leaderboard(store.season_start(), now + 1)
+        lines = pr.format_board("🏆 **This week**", weekly) + [""] + pr.format_board("🏅 **Season**", season)
+        mine = next((i for i, r in enumerate(season, 1) if r["user_id"] == message.author.id), None)
+        if mine:
+            r = season[mine - 1]
+            lines.append(f"You: #{mine} — {r['points']:g} pts ({r['correct']}/{r['played']})")
+        await message.reply("\n".join(lines)[:1990], allowed_mentions=discord.AllowedMentions.none())
+
     post_patches = post_esports      # 补丁推送用同一个发送函数
 
     async def _patches(self, message: discord.Message, arg: str) -> None:
@@ -797,8 +848,12 @@
              len(live_store.sources))
 
     # 赛事推送状态和英雄数据放在同一个目录（Docker 里是 herodata 卷）
+    # 竞猜：押注和积分存 STATE_DIR（用户产生的数据）；赛事报名阵容缓存和英雄数据放一起（herodata 卷）
+    prediction_store = pr.PredictionStore(state_dir / "predictions.db")
+    roster_cache = RosterCache(os.path.join(os.path.dirname(os.path.abspath(store.path)), "rosters.json"))
+    roster_cache.load()
     esports_feed = EsportsFeed(os.path.join(os.path.dirname(os.path.abspath(store.path)), "esports_state.json"),
-                               regions=regions)
+                               regions=regions, predictions=PredictionFeed(prediction_store, roster_cache))
     esports_feed.load()
 
     wish_channel = os.environ.get("WISH_CHANNEL_ID", "").strip()
@@ -820,7 +875,8 @@
     registry = Registry()
     bot = MeeseeksBot(registry, analyzer, recognizer, labels, roster, store, hero_qa, esports_feed,
                       int(wish_channel) if wish_channel else None, wish_desk, wish_plans, patch_feed,
-                      regions=regions, live_watcher=live_watcher, live_notify_offline=live_notify_offline)
+                      regions=regions, live_watcher=live_watcher, live_notify_offline=live_notify_offline,
+                      predictions=prediction_store, prediction_ui=PredictionUI(prediction_store))
     patch_feed.icons = HeroEmojis(bot).for_patch     # 补丁摘要里英雄名旁边的头像
     ws = WSServer(
         registry, bot,
@@ -833,7 +889,7 @@
         await ws.start()
         refreshers = [asyncio.create_task(refresh_loop(store, refresh_hours)),
                       asyncio.create_task(refresh_loop(matchup_store, refresh_hours, fetch=matchups.fetch_all)),
-                      asyncio.create_task(esports_feed.run(bot.post_esports)),
+                      asyncio.create_task(esports_feed.run(bot.post_esports, bot.edit_esports)),
                       asyncio.create_task(patch_feed.run(bot.post_patches)),
                       asyncio.create_task(live_watcher.run())]
         try:
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_prediction_bot.py`
Expected: `16/16 passed`

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_esports_bot.py`
Expected: `55/55 passed`

- [ ] **Step 5: 文档**

把下面的补丁原样保存到计划工作区里的文件（例如 `t5-readme.diff`），在仓库根目录执行 `git apply t5-readme.diff`。补丁应用失败时，停下来报告 git apply 的报错，不要绕过它手动修改：

```diff
--- a/README.md
+++ b/README.md
@@ -323,7 +323,7 @@
 | `ANTHROPIC_API_KEY` | 否 | 空 | 留空则只识别截图，不给建议 |
 | `CLAUDE_MODEL` | 否 | `claude-sonnet-5` | 给建议用的模型；想更快可换成 `claude-haiku-4-5-20251001`。可用模型见[模型列表](https://platform.claude.com/docs/en/models/overview) |
 | `ADVISE_EFFORT` | 否 | `low` | 给建议时的思考强度（`low`/`medium`/`high`），越高越慢 |
-| `STATE_DIR` | 否 | `state/` | 标注的模板、待标注队列、许愿次数（`wishes.json`）、赛区频道绑定（`regions.json`）和直播源（`live_sources.json`）存放位置；Docker 部署时是挂载的 `/state` |
+| `STATE_DIR` | 否 | `state/` | 标注的模板、待标注队列、许愿次数（`wishes.json`）、赛区频道绑定（`regions.json`）、直播源（`live_sources.json`）和赛事竞猜（`predictions.db`）存放位置；Docker 部署时是挂载的 `/state` |
 | `WS_HOST` | 否 | `0.0.0.0` | WebSocket 监听地址；用反向代理时改为 `127.0.0.1` |
 | `WS_PORT` | 否 | `8765` | WebSocket 端口 |
 | `MIN_SNAPSHOT_INTERVAL` | 否 | `5` | 同一客户端两次分析的最短间隔（秒） |
@@ -369,7 +369,8 @@
 | `@mrmeeseeks help` | 显示帮助 |
 | `@mrmeeseeks wishes [游戏] [7d]` | 管理员（「管理服务器」权限或 `WISH_REVIEWER_ID`）导出许愿 CSV，按 👍 数排序，私信发送。可按游戏（包含匹配，`漫威` 也行）、最近 N 天筛选 |
 | `@mrmeeseeks <问题>` | 英雄问答：数值（血量、子弹体积）、最近的补丁、N 米处几枪击杀。例如 `@mrmeeseeks 卡西迪最近被削了吗`（先查[官方补丁说明](https://overwatch.blizzard.com/en-us/news/patch-notes/)，补丁当天就能答对，再查 Wiki 的历史记录）、`@mrmeeseeks 卡西迪 30 米爆头几枪杀毛加`、`@mrmeeseeks 查莉娅怕谁`。克制关系来自 [counterwatch.gg](https://www.counterwatch.gg) 的对位评分（对决和团战结果，全段位，不是整局胜率）。需要 `ANTHROPIC_API_KEY` |
-| `@mrmeeseeks esports here` / `esports off` / `esports status` / `esports preview` | 赛事推送的默认频道：推送 OWCS 和世界杯的每日赛程预告（悉尼时间 10:00）、开赛前 15 分钟提醒、赛果（比分用剧透遮罩）和官方新闻。绑定了赛区频道的比赛会发到赛区频道（见下方「按赛区分频道」），其余和新闻发到这里。`here`/`off` 需要"管理频道"权限；`status` 显示默认频道、赛区频道和接下来几场比赛各发到哪；`preview` 立刻在当前频道发一组用真实数据生成的样例（预告、下一场提醒、最近一场赛果，标着 🧪 Preview），只含会发到这个频道的比赛，不影响自动推送，部署后或绑定赛区后用来快速验证（需要"管理频道"权限）。比赛数据来自 [Liquipedia](https://liquipedia.net/overwatch)（CC-BY-SA） |
+| `@mrmeeseeks esports here` / `esports off` / `esports status` / `esports preview` | 赛事推送的默认频道：推送 OWCS 和世界杯的每日赛程预告（悉尼时间 10:00）、开赛前 15 分钟提醒、赛果（比分用剧透遮罩）和官方新闻。绑定了赛区频道的比赛会发到赛区频道（见下方「按赛区分频道」），其余和新闻发到这里。`here`/`off` 需要"管理频道"权限；`status` 显示默认频道、赛区频道和接下来几场比赛各发到哪；`preview` 立刻在当前频道发一组用真实数据生成的样例（预告、下一场提醒、最近一场赛果，以及一条按钮禁用的竞猜消息，标着 🧪 Preview），只含会发到这个频道的比赛，不影响自动推送，部署后或绑定赛区后用来快速验证（需要"管理频道"权限）。比赛数据来自 [Liquipedia](https://liquipedia.net/overwatch)（CC-BY-SA） |
+| `@mrmeeseeks predictions` / `predictions reset` | 赛事竞猜：每天悉尼时间 10:00 预告之后，每场官方比赛在它的赛区频道（或默认频道）发一条竞猜消息，附双方报名阵容，点按钮押胜者，开赛前可以改。开赛时截止并公布谁押了谁，赛果出来后在剧透遮罩里揭晓谁猜中；猜中得 1 分，押冷门猜中最多 3 分。每周一 10:00 在默认频道发上周榜和称号。`predictions` 查看本周榜、赛季榜和自己的排名；`predictions reset` 开新赛季（需要"管理服务器"权限） |
 | `@mrmeeseeks region <赛区>` / `region <赛区> off` / `region` | 把当前频道绑定成某个赛区的频道（需要"管理频道"权限）。该赛区的比赛预告、提醒、赛果和开播通知都发到这里。赛区名：`cn`、`asia`、`korea`、`japan`、`pacific`、`emea`、`na`、`world`（总决赛、季中赛）、`owwc`（世界杯）。不带参数列出所有绑定 |
 | `@mrmeeseeks live add <赛区> <B 站直播间链接或房间号> [名字]` / `live remove <链接或房间号>` / `live list` | 开播通知：手动登记 B 站直播间，开播时在该赛区的频道里发一条带标题和封面的消息（需要"管理消息"权限）。短号自动解析；不写名字用主播昵称 |
 | `@mrmeeseeks patches here` / `patches off` / `patches status` | 补丁推送频道：每 3 小时检查一次[官方补丁说明](https://overwatch.blizzard.com/en-us/news/patch-notes/)，有新补丁就发一篇要点式摘要并附原文链接：每个改动的英雄名前有头像，下面固定分 **Basic Info / Abilities / Perks** 三项（没有改动写 None）；改动内容用英文，英雄、武器、技能、威能名写成「英文/简体中文/韩文」（例如 `Roadhog/路霸/로드호그`、`Chain Hook/链钩/사슬 갈고리`），技能名后面标出 PC 默认键位（如 `Shift`），描述里顺带提到的技能也标（来源：补丁原文 > 官方技能名 > Wiki；都没有的写进 `server/keybinds.json` 手动标注，启动日志会列出缺键位的技能），中文取自国服官网、韩文取自韩服官网的同一篇补丁，找不到的语言留空。`here` 设置后会立刻发一次最新的补丁。`here`/`off` 需要"管理频道"权限；摘要需要 `ANTHROPIC_API_KEY`，没有时只列出改动的英雄 |
@@ -448,6 +449,11 @@
 python tests/test_esports.py   # Liquipedia 赛程、官方新闻解析
 python tests/test_esports_feed.py # 赛事推送调度：预告、提醒、赛果、新闻、夏令时
 python tests/test_esports_bot.py  # esports / region / live 指令和权限、开播通知发送
+python tests/test_predictions.py   # 竞猜：押注、截止、结算、计分、排行榜
+python tests/test_rosters.py       # 赛事报名阵容：解析、候选页面、缓存与限速
+python tests/test_prediction_feed.py # 竞猜接入推送：完整流程、改期、作废、周榜
+python tests/test_prediction_ui.py   # 竞猜按钮：点击、改选、节流
+python tests/test_prediction_bot.py  # predictions 指令、preview 样例、带按钮的发送与编辑
 python tests/test_regions.py      # 赛区映射：Liquipedia 赛事路径 -> 赛区、赛区 -> 频道
 python tests/test_live.py         # 开播通知：B 站链接和接口解析、开播/下播判定、直播源文件、轮询循环
 python tests/test_patchnotes.py   # 官方补丁说明解析、推送频道、问答工具、patches 指令
```

把下面的补丁原样保存到计划工作区里的文件（例如 `t5-arch.diff`），在仓库根目录执行 `git apply t5-arch.diff`。补丁应用失败时，停下来报告 git apply 的报错，不要绕过它手动修改：

```diff
--- a/docs/ARCHITECTURE.md
+++ b/docs/ARCHITECTURE.md
@@ -17,6 +17,10 @@
 | `server/matchups.py` | 英雄对位（克制）数据：从 counterwatch.gg 抓取、缓存、每日刷新 |
 | `server/esports.py` | 赛事数据：解析 Liquipedia:Matches（比赛）和 esports.overwatch.com 首页（新闻） |
 | `server/esports_feed.py` | 赛事推送：`plan()` 纯函数决定发什么（含按赛区分频道），`EsportsFeed` 负责抓取、状态持久化和发送 |
+| `server/predictions.py` | 赛事竞猜存储（`STATE_DIR/predictions.db`，SQLite）：押注、截止、结算、作废、计分、周榜和赛季榜 |
+| `server/rosters.py` | 赛事报名阵容：解析 Liquipedia 赛事页面的参赛卡片，缓存到 `rosters.json`，限速抓取 |
+| `server/prediction_feed.py` | 竞猜接入推送：竞猜 / 截止 / 作废 / 周榜消息，给提醒和赛果补充竞猜信息（不依赖 discord） |
+| `server/prediction_ui.py` | 竞猜按钮（`DynamicItem`，`pred:<key>:<side>`）：点击押注、只回复本人、节流更新票数 |
 | `server/regions.py` | 赛区 -> 频道的绑定（`regions.json`），以及 Liquipedia 赛事路径 -> 赛区的识别；赛事推送和开播通知共用 |
 | `server/live_watch.py` | 开播通知：手动登记的 B 站直播间（`live_sources.json`）、状态查询、开播/下播判定。不依赖 discord.py |
 | `server/patchnotes.py` | 官方补丁说明：抓取 overwatch.blizzard.com 的当月和上月补丁页，解析成整篇文本 + 按英雄分组的改动 |
@@ -108,6 +112,18 @@
 
 已知限制：Liquipedia 汇总页只有最近约 50 场即将进行和 50 场已结束的比赛；官方首页只放 1–2 条精选新闻；同一赛段里同样两队 6 小时内重赛、且前一场已经不在 Liquipedia 汇总页上时，第二场会被改期去重吞掉。
 
+## 赛事竞猜
+
+开了竞猜时，`EsportsFeed.tick` 在 `plan()` 之后调用 `PredictionFeed.augment`：
+
+1. **快照**：`snapshot()` 从 `PredictionStore` 读出每场比赛已登记的竞猜（`find`：ID 相同，或者同一赛事同样两队、开始时间相差不到 6 小时并且旧 ID 已经不在汇总页上——和改期去重同一规则）、票数、赛果揭晓的预览（`score_preview`，只读）、阵容缓存和上周榜。
+2. **计划**：纯函数 `plan_predictions` 产出 `kind` 为 `prediction`（悉尼 10:00 之后、未来 24 小时、离开赛至少 15 分钟、不是 TBD 对 TBD、还没登记）、`lock`（到开赛时间）、`void`（开赛 48 小时后还没结算）的消息，以及周一 10:00 的周榜；`decorate` 给提醒加上票数和竞猜链接，给赛果加上剧透揭晓并标记"发出后结算"（平局或没有胜负就作废）。
+3. **执行**：`execute` 发带按钮的竞猜消息（发送函数返回消息 ID 和链接后 `register`）；截止时先公布名单、再 `lock`、再把原消息按钮改成禁用；作废时编辑原消息。发送失败照常停在这一轮，下一轮重试；编辑失败只记日志。`after_sent` 在赛果发出后 `resolve`。
+4. **按钮**：`prediction_ui.PredictButton` 是 `DynamicItem`，`setup_hook` 里 `add_dynamic_items` 一次，重启前发的竞猜消息也能点；点击后 `pick`、ephemeral 回复、每 2 秒最多编辑一次票数。
+5. **阵容**：`RosterCache.ensure` 在每次抓到比赛后放到后台跑（Liquipedia `action=parse` 每 30 秒最多 1 次，同一页面 24 小时内不重抓）；生成竞猜消息时只读缓存，拿不到就写 "Roster unavailable"。
+
+计分：猜中 `1 + max(0, (0.5 − 押中一方占比) × 4)`，上限 3，参与少于 3 人没有加成。
+
 ## 开播通知
 
 `live_watch.LiveWatcher` 每 `LIVE_POLL_INTERVAL` 秒（默认 30，最低 15）跑一次 `poll_once`：把所有登记的直播间分批（每批 20 个）发给 B 站公开的 `getRoomBaseInfo` 接口（不需要登录，标准库 urllib 跑在线程里），再用纯函数 `detect_transitions` 和上一轮比较，产出 `LiveEvent("live" | "offline")`，交给 `MeeseeksBot._live_notify`：按直播源的赛区在 `RegionStore` 里找频道，发一条带标题、封面的嵌入消息（不 ping 任何人）。
```

- [ ] **Step 6: 运行完整测试**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test sh -c 'pip install -q -r client/requirements.txt >/dev/null 2>&1; for t in tests/test_*.py; do printf "%s: " $t; python $t 2>/dev/null | tail -1; done'`

Expected（全部通过）：

```
tests/test_counter_advice.py: 28/28 passed
tests/test_damage.py: 37/37 passed
tests/test_e2e.py: 38/38 passed
tests/test_esports.py: 19/19 passed
tests/test_esports_bot.py: 55/55 passed
tests/test_esports_feed.py: 81/81 passed
tests/test_hero_qa.py: 69/69 passed
tests/test_herodata.py: 33/33 passed
tests/test_heroparse.py: 60/60 passed
tests/test_live.py: 39/39 passed
tests/test_matchups.py: 37/37 passed
tests/test_overfast.py: 28/28 passed
tests/test_patchnotes.py: 138/138 passed
tests/test_prediction_bot.py: 16/16 passed
tests/test_prediction_feed.py: 39/39 passed
tests/test_prediction_ui.py: 16/16 passed
tests/test_predictions.py: 42/42 passed
tests/test_regions.py: 26/26 passed
tests/test_rosters.py: 19/19 passed
tests/test_units.py: 26/26 passed
tests/test_vision.py: 67/67 passed
tests/test_wishes.py: 116/116 passed
```

- [ ] **Step 7: Commit**

```bash
git add server/bot.py tests/test_prediction_bot.py tests/test_esports_bot.py README.md docs/ARCHITECTURE.md
git commit -m "Wire esports predictions into the bot: buttons, predictions command, preview sample"
```

- [ ] **Step 8: 部署后验证（需要用户批准部署；合并到 main 之后才部署）**

1. 在推送频道发 `@mrmeeseeks esports preview`：最后一条应该是按钮禁用的竞猜样例，带双方阵容。
2. 下一个悉尼时间 10:00 之后，每场符合条件的比赛都应该有一条竞猜消息；点按钮后只有自己能看到确认消息，按钮上的票数随之更新。
3. 开赛时，应该看到 `🔒 Picks closed …` 的名单，原消息的按钮变成禁用。
4. 赛果消息末尾应该有剧透遮罩里的 `🎯 ||Correct: …||`。`@mrmeeseeks predictions` 能显示榜单。
5. 服务器上应该生成 `/state/predictions.db`，并且英雄数据缓存目录下有 `rosters.json`。
