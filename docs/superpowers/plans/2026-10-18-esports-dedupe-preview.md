# 赛事推送：改期去重与 preview 测试入口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复"Liquipedia 改开赛时间导致同一场比赛重复提醒"这个线上 bug；新增 `@mrmeeseeks esports preview` 指令，让测试人员在部署后、或者新频道绑定赛区后，能马上看到这个频道会收到的推送样例。

**Architecture:**
- **去重**：`esports_feed.already()` 用 `match_base`（比赛 ID 去掉开始时间）加 6 小时容差来判断"是不是已经发过"，比赛 ID 格式和状态文件都不变。
- **样例**：纯函数 `preview_posts()` 生成"会路由到当前频道"的比赛样例。`bot._esports` 新增 `preview` 分支，样例直接发到频道，不记账。

**Tech Stack:** Python 3.12（生产镜像）、discord.py。测试沿用仓库的风格：`python tests/test_xxx.py`，自带 `check()`。本机的 Python 3.13 装不上 `rapidocr-onnxruntime`，所以**所有测试都在生产镜像里跑**。

**Spec:** `docs/superpowers/specs/2026-10-18-esports-dedupe-preview-design.md`

**分支：** `esports-dedupe-preview`（从 `main` 46c6a25 拉出）。

## Global Constraints

- **比赛 ID 格式**保持 `赛事路径|开始时间|队1|队2` 不变，现有的 `esports_state.json` 可以原样沿用；改期后的新 ID 不另外记账。
- **同一场**的判断：同一个 `赛事路径（含 #轮次）|队1|队2`，并且开始时间相差 **小于 6 小时**（`SAME_MATCH_WINDOW = timedelta(hours=6)`）。不是这种格式的 ID 原样比较。
- **`esports preview`**：
  - 只包含 `EsportsFeed.channel_for(m) == 当前频道` 的比赛；
  - 依次是未来 24 小时的预告、下一场比赛的提醒、最近一场比赛的赛果，每条以 `🧪 Preview` 开头；
  - 不记账，可以重复触发；
  - 需要"管理频道"权限；
  - 发送时用 `AllowedMentions.none()`。
- **没有比赛时**的回复以 "No matches routed to this channel right now:" 开头，并说明是"数据还没抓到"，还是"没有比赛路由到这里"。
- **代码风格**：注释用中文，和现有的 `server/*.py` 保持一致。

## Review Focus

1. **同一赛段里同样两队在 6 小时内重赛**：第二场会被吞掉。这是已知限制，已经写进 spec 和 ARCHITECTURE，测试只覆盖了"6 小时以上"和"不同赛段"。→ Task 1「同样两队相隔 6 小时以上算另一场」「不同赛段的同样两队不受影响」。
2. **TBD 队伍确定后**，队名变化会被当成一场新比赛。这和改期是两回事，本计划不处理；提醒里本来就不会出现双方都是 TBD 的比赛。
3. **preview 的路由要和真实推送完全一致**：都用 `channel_for`，否则测试人员看到的样例会误导他们。→ Task 2「比赛都分流到别的赛区频道」「在赛区频道里发出三条样例」。
4. **preview 不能改动推送状态**，否则会吞掉真实的提醒或赛果。→ Task 2「preview 不改状态（不记账）」「preview 不改推送状态」。
5. **有人用 preview 刷屏**：通过"管理频道"权限限制。→ Task 2「preview 需要管理频道权限」。

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `server/esports_feed.py` | 修改 | Task 1：`SAME_MATCH_WINDOW`、`match_base`、`already`，`plan()` 改用 `already`。Task 2：`preview_posts`、`EsportsFeed.preview` |
| `server/bot.py` | 修改 | Task 2：`_esports` 的 `preview` 分支、用法提示、`HELP_TEXT` |
| `tests/test_esports_feed.py` | 修改 | Task 1：`real()`、`test_reschedule_dedupe`。Task 2：`test_preview` |
| `tests/test_esports_bot.py` | 修改 | Task 2：`test_preview_command` |
| `README.md`、`docs/ARCHITECTURE.md` | 修改 | Task 2：文档 |

---

### Task 1: 改期去重

**Files:**
- Modify: `server/esports_feed.py`
- Modify: `tests/test_esports_feed.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `SAME_MATCH_WINDOW = timedelta(hours=6)`
  - `match_base(match_id: str) -> str`
  - `already(done: dict[str, int], m: dict) -> bool`
  - `plan()` 判断提醒和赛果是否已经发过时，改用 `already`。

- [ ] **Step 1: 写失败的测试**

把下面的补丁原样保存到计划工作区中的文件（例如 `t1-feedtest.diff`），在仓库根目录执行 `git apply t1-feedtest.diff`。补丁应用失败时停下来报告 git apply 的报错，不要绕开它手改：

```diff
--- a/tests/test_esports_feed.py
+++ b/tests/test_esports_feed.py
@@ -390,6 +390,36 @@
               [c for c, _ in sent] == [11] and set(feed.state["reminded"]) == {"kr", "na"})
 
 
+def real(start, t1="Team Liquid", t2="The Kafe", finished=False, score=None,
+         tour="Overwatch Champions Series/2026/North America/Stage 3/Regular Season#Week 2"):
+    """真实格式的比赛 ID：赛事路径|开始时间|队1|队2。"""
+    ts = int(start.timestamp())
+    m = match(f"{tour}|{ts}|{t1}|{t2}", start, finished, score, t1=t1, t2=t2)
+    m["tournament"] = tour
+    return m
+
+
+def test_reschedule_dedupe():
+    """线上 2026-10-17：Liquipedia 把开赛推迟 10 分钟，比赛 ID 变了，同一场收到两条提醒。"""
+    start = at(2026, 10, 17, 21, 30)
+    first, moved = real(start), real(start + timedelta(minutes=10))
+    check("改期前后 base 相同", ef.match_base(first["id"]) == ef.match_base(moved["id"]))
+    state = ready_state(digest_date="2026-10-18")
+    check("改期前 15 分钟提醒一次", len(texts(run_plan(start - timedelta(minutes=15), [first], [], state))) == 1)
+    check("推迟 10 分钟后不再重复提醒", texts(run_plan(start - timedelta(minutes=5), [moved], [], state)) == [])
+    check("改期后的 ID 不需要另外记账（每次都按容差识别）", moved["id"] not in state["reminded"])
+    done_first = real(start, finished=True, score=[3, 1])
+    done_moved = real(start + timedelta(minutes=10), finished=True, score=[3, 1])
+    run_plan(start + timedelta(hours=2), [done_first], [], state)
+    check("赛果也不因改期重复发", texts(run_plan(start + timedelta(hours=2, minutes=1), [done_moved], [], state)) == [])
+    far = real(start + timedelta(hours=7))
+    check("同样两队相隔 6 小时以上算另一场", len(texts(run_plan(start + timedelta(hours=6, minutes=50),
+                                                         [far], [], state))) == 1)
+    other_stage = real(start + timedelta(minutes=20), tour="Overwatch Champions Series/2026/North America/Stage 3/Playoffs")
+    check("不同赛段的同样两队不受影响", len(texts(run_plan(start + timedelta(minutes=8), [other_stage], [], state))) == 1)
+    check("旧格式（非 赛事|时间|队1|队2）的 ID 原样比较", ef.match_base("a") == "a")
+
+
 if __name__ == "__main__":
     test_digest()
     test_digest_dst()
@@ -402,6 +432,7 @@
     test_feed_tick_send_failure_stops_tick()
     test_feed_tick_split_digest_send_failure()
     test_region_routing()
+    test_reschedule_dedupe()
     passed = sum(r for _, r in results)
     print(f"\n{passed}/{len(results)} passed")
     sys.exit(0 if passed == len(results) else 1)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `docker build -q -t mrmeeseeks:test . >/dev/null && docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_esports_feed.py`
Expected: 前面已有的用例通过，然后报 `AttributeError: module 'esports_feed' has no attribute 'match_base'`。

- [ ] **Step 3: 实现**

把下面的补丁原样保存到计划工作区中的文件（例如 `t1-feed.diff`），在仓库根目录执行 `git apply t1-feed.diff`。补丁应用失败时停下来报告 git apply 的报错，不要绕开它手改：

```diff
--- a/server/esports_feed.py
+++ b/server/esports_feed.py
@@ -33,6 +33,7 @@
 REMIND_LATE = timedelta(minutes=5)      # 开赛超过 5 分钟就不再补发提醒
 RESULT_MAX_AGE = timedelta(hours=24)    # 只发最近 24 小时内开始的比赛的赛果
 STATE_KEEP = timedelta(days=14)
+SAME_MATCH_WINDOW = timedelta(hours=6)  # 同一赛事、同样两队、开始时间相差不到 6 小时视为同一场（Liquipedia 改期）
 MATCH_REFRESH = 600              # 比赛每 10 分钟抓一次
 NEWS_REFRESH = 3 * 3600          # 新闻每 3 小时抓一次
 TICK_SECONDS = 60
@@ -65,6 +66,21 @@
     for flag in ("initialized", "news_initialized"):
         if marks.get(flag):
             state[flag] = True
+
+
+def match_base(match_id: str) -> str:
+    """比赛 ID（赛事路径|开始时间|队1|队2）去掉开始时间：同一场比赛改期后 base 不变。"""
+    parts = match_id.split("|")
+    return "|".join([parts[0], *parts[2:]]) if len(parts) >= 4 else match_id
+
+
+def already(done: dict[str, int], m: dict[str, Any]) -> bool:
+    """这场比赛是否已经处理过：ID 相同，或者同一赛事同样两队、开始时间相差不到 6 小时（改期）。"""
+    if m["id"] in done:
+        return True
+    base = match_base(m["id"])
+    window = SAME_MATCH_WINDOW.total_seconds()
+    return any(match_base(k) == base and abs(v - m["start"]) < window for k, v in done.items())
 
 
 def prune(state: dict[str, Any], now: datetime) -> None:
@@ -154,11 +170,11 @@
             digest[-1].marks = {"digest_date": today}
             posts += digest
         for m in sorted(matches, key=lambda m: m["start"]):
-            if (not m["finished"] and m["id"] not in state["reminded"]
+            if (not m["finished"] and not already(state["reminded"], m)
                     and not (m["team1"] == "TBD" and m["team2"] == "TBD")   # 对阵都没定，不提醒
                     and m["start"] - REMIND_BEFORE.total_seconds() <= ts <= m["start"] + REMIND_LATE.total_seconds()):
                 posts.append(Post(format_reminder(m), {"reminded": {m["id"]: m["start"]}}, target(m)))
-            if (m["finished"] and m["id"] not in state["resulted"]
+            if (m["finished"] and not already(state["resulted"], m)
                     and ts - RESULT_MAX_AGE.total_seconds() <= m["start"] <= ts):
                 posts.append(Post(format_result(m), {"resulted": {m["id"]: m["start"]}}, target(m)))
     if news is not None and not state["news_initialized"]:
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_esports_feed.py`
Expected: `66/66 passed`。日志里会出现 "homepage down"、"missing permissions"、"broken channel" 和 "File exists: …/blocker" 的 traceback，这些来自已有的失败路径测试，是预期内的。

- [ ] **Step 5: Commit**

```bash
git add server/esports_feed.py tests/test_esports_feed.py
git commit -m "Treat rescheduled matches as the same match for reminders and results"
```

---

### Task 2: `esports preview` 测试入口

**Files:**
- Modify: `server/esports_feed.py`
- Modify: `server/bot.py`
- Modify: `tests/test_esports_feed.py`
- Modify: `tests/test_esports_bot.py`
- Modify: `README.md`、`docs/ARCHITECTURE.md`

**Interfaces:**
- Consumes: 已有的 `EsportsFeed.channel_for`、`format_digest`、`format_reminder`、`format_result`、`DIGEST_WINDOW`；`MeeseeksBot._can_manage`
- Produces:
  - `preview_posts(now: datetime, matches: list | None, routed_here: Callable[[dict], bool]) -> list[str]`
  - `EsportsFeed.preview(channel_id: int, now: datetime | None = None) -> list[str]`
  - `@mrmeeseeks esports preview`

- [ ] **Step 1: 写推送部分的失败测试**

把下面的补丁原样保存到计划工作区中的文件（例如 `t2-feedtest.diff`），在仓库根目录执行 `git apply t2-feedtest.diff`。补丁应用失败时停下来报告 git apply 的报错，不要绕开它手改：

```diff
--- a/tests/test_esports_feed.py
+++ b/tests/test_esports_feed.py
@@ -420,6 +420,36 @@
     check("旧格式（非 赛事|时间|队1|队2）的 ID 原样比较", ef.match_base("a") == "a")
 
 
+def test_preview():
+    now = at(2026, 10, 17, 20, 0)
+    kr = real(now + timedelta(hours=2), "T1", "ZETA DIVISION",
+              tour="Overwatch Champions Series/2026/Asia/Stage 3/Korea/Regular Season#Week 2")
+    na_next = real(now + timedelta(hours=1))
+    na_done = real(now - timedelta(hours=3), "NTMR", "Disguised", finished=True, score=[3, 2])
+    na_later = real(now + timedelta(hours=30), "Shopify Rebellion", "Team Peps")
+    matches = [kr, na_next, na_done, na_later]
+    route = {kr["id"]: 11}
+    posts = ef.preview_posts(now, matches, lambda m: route.get(m["id"], 99) == 99)
+    check("只包含路由到当前频道的比赛", all("T1 vs" not in p for p in posts))
+    check("依次是预告、下一场提醒、最近一场赛果", [p.split("\n")[1][:1] for p in posts] == ["📅", "🔴", "✅"])
+    check("预告只含 24 小时内", "Shopify Rebellion" not in posts[0] and "Team Liquid" in posts[0])
+    check("提醒是下一场", "Team Liquid vs The Kafe" in posts[1])
+    check("赛果照常剧透遮罩", "||NTMR 3 : 2 Disguised||" in posts[2])
+    check("每条都标明是 Preview", all(p.startswith("🧪 Preview") for p in posts))
+    check("没有路由到这里的比赛：空列表", ef.preview_posts(now, matches, lambda m: False) == [])
+    check("还没抓到数据：空列表", ef.preview_posts(now, None, lambda m: True) == [])
+
+    with tempfile.TemporaryDirectory() as d:
+        feed = ef.EsportsFeed(os.path.join(d, "esports_state.json"))
+        feed.matches = matches
+        feed.set_channel(99)
+        before = json.dumps(feed.state, sort_keys=True)
+        out = feed.preview(99, now)
+        check("EsportsFeed.preview 用默认频道路由", len(out) == 3)
+        check("preview 不改状态（不记账）", json.dumps(feed.state, sort_keys=True) == before)
+        check("可以重复触发", feed.preview(99, now) == out)
+
+
 if __name__ == "__main__":
     test_digest()
     test_digest_dst()
@@ -433,6 +463,7 @@
     test_feed_tick_split_digest_send_failure()
     test_region_routing()
     test_reschedule_dedupe()
+    test_preview()
     passed = sum(r for _, r in results)
     print(f"\n{passed}/{len(results)} passed")
     sys.exit(0 if passed == len(results) else 1)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_esports_feed.py`
Expected: 报 `AttributeError: module 'esports_feed' has no attribute 'preview_posts'`。

- [ ] **Step 3: 实现 `preview_posts` 和 `EsportsFeed.preview`**

把下面的补丁原样保存到计划工作区中的文件（例如 `t2-feed.diff`），在仓库根目录执行 `git apply t2-feed.diff`。补丁应用失败时停下来报告 git apply 的报错，不要绕开它手改：

```diff
--- a/server/esports_feed.py
+++ b/server/esports_feed.py
@@ -184,6 +184,27 @@
             if n["url"] not in state["seen_news"]:
                 posts.append(Post(format_news(n), {"seen_news": [n["url"]]}))
     return posts
+
+
+def preview_posts(now: datetime, matches: list[dict[str, Any]] | None,
+                  routed_here: Callable[[dict[str, Any]], bool]) -> list[str]:
+    """`esports preview` 的样例：路由到当前频道的比赛里，未来 24 小时预告、下一场的提醒、最近一场的赛果。
+
+    纯函数，不读写状态；用真实数据，所以部署到新频道、绑定赛区后可以马上验证分流。
+    """
+    ts = now.timestamp()
+    mine = [m for m in matches or [] if routed_here(m)]
+    upcoming = sorted((m for m in mine if not m["finished"] and m["start"] >= ts), key=lambda m: m["start"])
+    finished = sorted((m for m in mine if m["finished"]), key=lambda m: m["start"])
+    posts: list[str] = []
+    soon = [m for m in upcoming if m["start"] <= ts + DIGEST_WINDOW.total_seconds()]
+    if soon:
+        posts += format_digest(soon)
+    if upcoming:
+        posts.append(format_reminder(upcoming[0]))
+    if finished:
+        posts.append(format_result(finished[-1]))
+    return [f"🧪 Preview\n{p}" for p in posts]
 
 
 # ---------- 运行 ----------
@@ -242,6 +263,11 @@
         """这场比赛会发到哪个频道（赛区频道，否则默认频道）。"""
         return resolve(self.channels, region_candidates(m.get("tournament") or "")) or self.channel_id
 
+    def preview(self, channel_id: int, now: datetime | None = None) -> list[str]:
+        """当前频道会收到哪些推送的样例（不记账，可重复触发）。"""
+        return preview_posts(now or datetime.now(timezone.utc), self.matches,
+                             lambda m: self.channel_for(m) == channel_id)
+
     async def refresh(self, mono: float) -> None:
         """到点就重新抓；抓取失败保留上一次的数据。"""
         if mono - self._matches_at >= MATCH_REFRESH:
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_esports_feed.py`
Expected: `77/77 passed`

- [ ] **Step 5: 写指令部分的失败测试**

把下面的补丁原样保存到计划工作区中的文件（例如 `t2-bottest.diff`），在仓库根目录执行 `git apply t2-bottest.diff`。补丁应用失败时停下来报告 git apply 的报错，不要绕开它手改：

```diff
--- a/tests/test_esports_bot.py
+++ b/tests/test_esports_bot.py
@@ -264,13 +264,57 @@
     s = lw.LiveStore(path)
     s.load()
     return "bilibili:7734200" in s.sources
+
+
+def test_preview_command():
+    import json
+    from datetime import datetime, timedelta, timezone
+    now = datetime.now(timezone.utc)
+    tour = "Overwatch Champions Series/2026/North America/Stage 3/Regular Season#Week 2"
+
+    def m(hours, t1, t2, finished=False, score=None, tournament=tour):
+        start = int((now + timedelta(hours=hours)).timestamp())
+        return {"id": f"{tournament}|{start}|{t1}|{t2}", "start": start, "tournament": tournament,
+                "label": "OWCS NA Stage 3", "team1": t1, "team2": t2, "best_of": 5, "finished": finished,
+                "score": score, "url": "https://liquipedia.net/x"}
+
+    with tempfile.TemporaryDirectory() as d:
+        regions = RegionStore(None)
+        feed = EsportsFeed(os.path.join(d, "esports_state.json"), regions=regions)
+        b = make_bot(feed)
+        b._can_manage = bot.MeeseeksBot._can_manage.__get__(b)
+
+        msg = FakeMessage(manage=False)
+        asyncio.run(b._esports(msg, "preview"))
+        check("preview 需要管理频道权限", "Manage Channels" in msg.last and msg.channel.sent == [])
 
+        msg = FakeMessage(manage=True)
+        asyncio.run(b._esports(msg, "preview"))
+        check("还没抓到数据时说明原因", "hasn't been fetched" in msg.last)
 
+        feed.matches = [m(1, "Team Liquid", "The Kafe"), m(-3, "NTMR", "Disguised", True, [3, 2])]
+        regions.set("na", 777)
+        msg = FakeMessage(manage=True, channel_id=555)
+        asyncio.run(b._esports(msg, "preview"))
+        check("比赛都分流到别的赛区频道：说明没有比赛路由到这里", "No matches routed to this channel" in msg.last
+              and msg.channel.sent == [])
+
+        before = json.dumps(feed.state, sort_keys=True)
+        msg = FakeMessage(manage=True, channel_id=777)
+        asyncio.run(b._esports(msg, "preview"))
+        sent = [t for t, _, _ in msg.channel.sent]
+        check("在赛区频道里发出预告、提醒、赛果三条样例", len(sent) == 3 and all(t.startswith("🧪 Preview") for t in sent))
+        check("样例不 ping 任何人", all(a is not None and a.everyone is False for _, a, _ in msg.channel.sent))
+        check("preview 不改推送状态", json.dumps(feed.state, sort_keys=True) == before)
+    check("帮助里有 esports preview", "esports preview" in bot.HELP_TEXT)
+
+
 if __name__ == "__main__":
     test_routing()
     test_commands()
     test_post_esports()
     test_region_and_live()
+    test_preview_command()
     passed = sum(r for _, r in results)
     print(f"\n{passed}/{len(results)} passed")
     sys.exit(0 if passed == len(results) else 1)
```

- [ ] **Step 6: 运行测试，确认失败**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_esports_bot.py`
Expected: 出现 `FAIL preview 需要管理频道权限`、`FAIL 还没抓到数据时说明原因`、`FAIL 比赛都分流到别的赛区频道：说明没有比赛路由到这里`、`FAIL 在赛区频道里发出预告、提醒、赛果三条样例` 等失败项。

- [ ] **Step 7: 实现指令**

把下面的补丁原样保存到计划工作区中的文件（例如 `t2-bot.diff`），在仓库根目录执行 `git apply t2-bot.diff`。补丁应用失败时停下来报告 git apply 的报错，不要绕开它手改：

```diff
--- a/server/bot.py
+++ b/server/bot.py
@@ -73,7 +73,8 @@
     "`@mrmeeseeks label` Label portraits I couldn't recognize\n"
     "`@mrmeeseeks disconnect` Unpair the client bound to this channel\n"
     "`@mrmeeseeks esports here` / `esports off` Post OWCS schedule, reminders, results and news in this channel "
-    "(needs Manage Channels); `esports status` shows the current setup\n"
+    "(needs Manage Channels); `esports status` shows the current setup; "
+    "`esports preview` posts sample posts for this channel right now (for testing)\n"
     "`@mrmeeseeks region cn` Make this channel the home of one region (cn, asia, korea, japan, pacific, emea, na, world): "
     "its matches and live alerts go here instead of the esports channel; `region cn off` unbinds, `region` lists\n"
     "`@mrmeeseeks live add <region> <Bilibili room url> [label]` Alert that region's channel when the room goes live; "
@@ -321,8 +322,22 @@
                 lines.append(f"• <t:{m['start']}:f> {m['team1']} vs {m['team2']} · {m['label']}"
                              + (f" → <#{dest}>" if dest else ""))
             await message.reply("\n".join(lines)[:1990])
+        elif action == "preview":
+            # 测试入口：立刻在当前频道发一组用真实数据生成的样例，不记账、不影响自动推送的去重
+            if not self._can_manage(message):
+                await message.reply("You need Manage Channels permission to preview esports posts.")
+                return
+            posts = feed.preview(message.channel.id)
+            if not posts:
+                reason = ("match data hasn't been fetched yet — try again in a minute" if feed.matches is None
+                          else "no upcoming or recent official matches are routed here "
+                               "(check `@mrmeeseeks esports status` and `region`)")
+                await message.reply(f"No matches routed to this channel right now: {reason}.")
+                return
+            for text in posts:
+                await message.channel.send(text, allowed_mentions=discord.AllowedMentions.none())
         else:
-            await message.reply("Usage: `@mrmeeseeks esports here`, `esports off` or `esports status`.")
+            await message.reply("Usage: `@mrmeeseeks esports here`, `esports off`, `esports status` or `esports preview`.")
 
     # ---------- 赛区频道 ----------
     def _can_manage(self, message: discord.Message, perm: str = "manage_channels") -> bool:
```

- [ ] **Step 8: 运行测试，确认通过**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test python tests/test_esports_bot.py`
Expected: `55/55 passed`

- [ ] **Step 9: 文档**

把下面的补丁原样保存到计划工作区中的文件（例如 `t2-readme.diff`），在仓库根目录执行 `git apply t2-readme.diff`。补丁应用失败时停下来报告 git apply 的报错，不要绕开它手改：

```diff
--- a/README.md
+++ b/README.md
@@ -369,7 +369,7 @@
 | `@mrmeeseeks help` | 显示帮助 |
 | `@mrmeeseeks wishes [游戏] [7d]` | 管理员（「管理服务器」权限或 `WISH_REVIEWER_ID`）导出许愿 CSV，按 👍 数排序，私信发送。可按游戏（包含匹配，`漫威` 也行）、最近 N 天筛选 |
 | `@mrmeeseeks <问题>` | 英雄问答：数值（血量、子弹体积）、最近的补丁、N 米处几枪击杀。例如 `@mrmeeseeks 卡西迪最近被削了吗`（先查[官方补丁说明](https://overwatch.blizzard.com/en-us/news/patch-notes/)，补丁当天就能答对，再查 Wiki 的历史记录）、`@mrmeeseeks 卡西迪 30 米爆头几枪杀毛加`、`@mrmeeseeks 查莉娅怕谁`。克制关系来自 [counterwatch.gg](https://www.counterwatch.gg) 的对位评分（对决和团战结果，全段位，不是整局胜率）。需要 `ANTHROPIC_API_KEY` |
-| `@mrmeeseeks esports here` / `esports off` / `esports status` | 赛事推送的默认频道：推送 OWCS 和世界杯的每日赛程预告（悉尼时间 10:00）、开赛前 15 分钟提醒、赛果（比分用剧透遮罩）和官方新闻。绑定了赛区频道的比赛会发到赛区频道（见下方「按赛区分频道」），其余和新闻发到这里。`here`/`off` 需要"管理频道"权限；`status` 显示默认频道、赛区频道和接下来几场比赛各发到哪。比赛数据来自 [Liquipedia](https://liquipedia.net/overwatch)（CC-BY-SA） |
+| `@mrmeeseeks esports here` / `esports off` / `esports status` / `esports preview` | 赛事推送的默认频道：推送 OWCS 和世界杯的每日赛程预告（悉尼时间 10:00）、开赛前 15 分钟提醒、赛果（比分用剧透遮罩）和官方新闻。绑定了赛区频道的比赛会发到赛区频道（见下方「按赛区分频道」），其余和新闻发到这里。`here`/`off` 需要"管理频道"权限；`status` 显示默认频道、赛区频道和接下来几场比赛各发到哪；`preview` 立刻在当前频道发一组用真实数据生成的样例（预告、下一场提醒、最近一场赛果，标着 🧪 Preview），只含会发到这个频道的比赛，不影响自动推送，部署后或绑定赛区后用来快速验证（需要"管理频道"权限）。比赛数据来自 [Liquipedia](https://liquipedia.net/overwatch)（CC-BY-SA） |
 | `@mrmeeseeks region <赛区>` / `region <赛区> off` / `region` | 把当前频道绑定成某个赛区的频道（需要"管理频道"权限）。该赛区的比赛预告、提醒、赛果和开播通知都发到这里。赛区名：`cn`、`asia`、`korea`、`japan`、`pacific`、`emea`、`na`、`world`（总决赛、季中赛）、`owwc`（世界杯）。不带参数列出所有绑定 |
 | `@mrmeeseeks live add <赛区> <B 站直播间链接或房间号> [名字]` / `live remove <链接或房间号>` / `live list` | 开播通知：手动登记 B 站直播间，开播时在该赛区的频道里发一条带标题和封面的消息（需要"管理消息"权限）。短号自动解析；不写名字用主播昵称 |
 | `@mrmeeseeks patches here` / `patches off` / `patches status` | 补丁推送频道：每 3 小时检查一次[官方补丁说明](https://overwatch.blizzard.com/en-us/news/patch-notes/)，有新补丁就发一篇要点式摘要并附原文链接：每个改动的英雄名前有头像，下面固定分 **Basic Info / Abilities / Perks** 三项（没有改动写 None）；改动内容用英文，英雄、武器、技能、威能名写成「英文/简体中文/韩文」（例如 `Roadhog/路霸/로드호그`、`Chain Hook/链钩/사슬 갈고리`），技能名后面标出 PC 默认键位（如 `Shift`），描述里顺带提到的技能也标（来源：补丁原文 > 官方技能名 > Wiki；都没有的写进 `server/keybinds.json` 手动标注，启动日志会列出缺键位的技能），中文取自国服官网、韩文取自韩服官网的同一篇补丁，找不到的语言留空。`here` 设置后会立刻发一次最新的补丁。`here`/`off` 需要"管理频道"权限；摘要需要 `ANTHROPIC_API_KEY`，没有时只列出改动的英雄 |
```

把下面的补丁原样保存到计划工作区中的文件（例如 `t2-arch.diff`），在仓库根目录执行 `git apply t2-arch.diff`。补丁应用失败时停下来报告 git apply 的报错，不要绕开它手改：

```diff
--- a/docs/ARCHITECTURE.md
+++ b/docs/ARCHITECTURE.md
@@ -103,8 +103,11 @@
 3. **发送**：发送成功后才记账，失败的下一轮重试。没有设置频道时照常记账、不发送。状态保存在英雄数据缓存同一目录的 `esports_state.json`。
 4. **分赛区**：`regions.region_candidates` 把赛事路径里的段按 `REGION_ALIASES` 映射成赛区列表，具体的在前（`…/Asia/Stage 3/Korea/…` → `['korea', 'asia']`；没有赛区的总决赛、季中赛 → `['world']`，世界杯 → `['owwc', 'world']`）。`plan()` 收到赛区 -> 频道的绑定后，每场比赛取列表里第一个绑定了频道的赛区作为 `Post.channel`，都没绑就是 `None`（默认频道）。预告按目标频道分组，每个频道一份只含自己比赛的预告，只有最后一条记 `digest_date`。新闻只发默认频道。绑定用 `@mrmeeseeks region <赛区>` 写进 `STATE_DIR/regions.json`。
 
-已知限制：Liquipedia 汇总页只有最近约 50 场即将进行和 50 场已结束的比赛；官方首页只放 1–2 条精选新闻。
+5. **改期去重**：比赛 ID 是 `赛事路径|开始时间|队1|队2`，Liquipedia 改开赛时间会生成新 ID。`already()` 在 `reminded` / `resulted` 里找同一赛事、同样两队（`match_base`）且开始时间相差不到 `SAME_MATCH_WINDOW`（6 小时）的记录，找到就视为同一场，不再重复提醒或发赛果。
+6. **测试入口**：`@mrmeeseeks esports preview` 调用纯函数 `preview_posts`，只取会路由到当前频道的比赛，立刻发预告、下一场提醒、最近一场赛果的样例；不记账，不影响自动推送的去重。
 
+已知限制：Liquipedia 汇总页只有最近约 50 场即将进行和 50 场已结束的比赛；官方首页只放 1–2 条精选新闻；同一赛段里同样两队 6 小时内重赛，第二场会被改期去重吞掉。
+
 ## 开播通知
 
 `live_watch.LiveWatcher` 每 `LIVE_POLL_INTERVAL` 秒（默认 30，最低 15）跑一次 `poll_once`：把所有登记的直播间分批（每批 20 个）发给 B 站公开的 `getRoomBaseInfo` 接口（不需要登录，标准库 urllib 跑在线程里），再用纯函数 `detect_transitions` 和上一轮比较，产出 `LiveEvent("live" | "offline")`，交给 `MeeseeksBot._live_notify`：按直播源的赛区在 `RegionStore` 里找频道，发一条带标题、封面的嵌入消息（不 ping 任何人）。
```

- [ ] **Step 10: 运行完整测试**

Run: `docker run --rm -u root -v "$PWD":/repo -w /repo mrmeeseeks:test sh -c 'pip install -q -r client/requirements.txt >/dev/null 2>&1; for t in tests/test_*.py; do printf "%s: " $t; python $t 2>/dev/null | tail -1; done'`

Expected（17 个文件全部通过）：

```
tests/test_counter_advice.py: 28/28 passed
tests/test_damage.py: 37/37 passed
tests/test_e2e.py: 38/38 passed
tests/test_esports.py: 19/19 passed
tests/test_esports_bot.py: 55/55 passed
tests/test_esports_feed.py: 77/77 passed
tests/test_hero_qa.py: 69/69 passed
tests/test_herodata.py: 33/33 passed
tests/test_heroparse.py: 60/60 passed
tests/test_live.py: 39/39 passed
tests/test_matchups.py: 37/37 passed
tests/test_overfast.py: 28/28 passed
tests/test_patchnotes.py: 138/138 passed
tests/test_regions.py: 26/26 passed
tests/test_units.py: 26/26 passed
tests/test_vision.py: 67/67 passed
tests/test_wishes.py: 116/116 passed
```

- [ ] **Step 11: Commit**

```bash
git add server/esports_feed.py server/bot.py tests/test_esports_feed.py tests/test_esports_bot.py README.md docs/ARCHITECTURE.md
git commit -m "Add esports preview command for testers"
```

- [ ] **Step 12: 部署后验证（需要用户批准部署；合并到 main 之后才部署）**

在已经设置好推送的频道里发 `@mrmeeseeks esports preview`，应该立刻收到最多三条 `🧪 Preview` 样例；在绑定了赛区的频道里只出现该赛区的比赛。观察下一次比赛改期：同一场比赛只会收到一条开赛提醒。
