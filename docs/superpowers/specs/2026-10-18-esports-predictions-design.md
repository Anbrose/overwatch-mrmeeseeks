# 赛事竞猜（含双方阵容）：设计

日期：2026-10-18

## 目标

让频道里**人和人之间的互动**变多。数据显示，10/12–10/17 期间：
- 主动 @ bot 的功能几乎没人用：对局识别 0 次，问答和建议一共 1 次。
- 赛事推送一直在跑：发了 99 条开赛提醒、58 条赛果。

所以这次在已有的赛事推送上加"比赛竞猜"。点一下按钮就能参与，开赛和赛果出来时公布谁押了谁、谁猜中，每周发排行榜，用来制造朋友之间的话题和调侃。

成功标准：
- 每场官方比赛都有一条竞猜消息，点一下按钮即可参与。
- 竞猜期间只显示票数。开赛时公布押注名单，赛果揭晓谁猜中（放在剧透遮罩里）。
- 积分、周榜和赛季榜准确，每场比赛只结算一次。
- bot 重启后，旧消息上的按钮仍然可以点，竞猜不丢失。

## 用户确认的决策

- **用户画像**：熟人经常一起开黑，也会看比赛，但频道很安静、多数人潜水。目标是人和人之间的互动。
- **先做 A（比赛竞猜），之后再做 B（开黑战报）。**
- **竞猜入口**：悉尼时间每天 10:00 的预告之后，每场比赛单独发一条竞猜消息，开赛时截止。
- **计分**：猜对得 1 分，押冷门猜中有额外加成。
- **可见性**：竞猜期间只显示票数，开赛时公布名单，赛果出来揭晓谁猜中。
- **排行榜**：每周一发周榜，另有赛季总榜，可以随时查询。赛季由管理员重置。
- **竞猜消息里显示双方的报名阵容。**
- **方案 A**：数据存 SQLite，按钮用持久按钮，竞猜流程接进现有的 `plan()`。
- **分赛区推送**：已由 PR #19 实现（`regions.py`，指令 `@mrmeeseeks region <赛区>`）。竞猜跟随比赛所在的频道，不另做分区。

## 现有代码（main 46c6a25）

- **`esports_feed.plan(now, matches, news, state, channels)`**：返回 `Post(text, marks, channel)`；`channel` 由 `regions.resolve` 按赛区选出，`None` 表示默认频道。
- **`EsportsFeed.tick(now, send, mono)`**：逐条发送，发送成功后才执行 `apply_marks`，遇到第一次失败就停止这一轮。`send(channel_id, text)` 没有返回值，补丁推送（`post_patches = post_esports`）也在用同一个函数。
- **持久按钮**：`wishes.WishEntryView` 用固定 `custom_id` 加 `bot.add_view`（`bot.setup_hook`）。discord.py ≥ 2.6，支持 `DynamicItem`。
- **存储位置**：`STATE_DIR`（Docker 里是 `/state`）存放 `regions.json`、`wishes.json` 等用户产生的数据。`esports_state.json` 在 `herodata` 卷里。

## 数据源事实（2026-10-18 核对）

- **比赛**：沿用 `Liquipedia:Matches`（见赛事推送的 spec）。比赛的 `tournament` 字段形如 `Overwatch Champions Series/2026/Asia/Stage 3/Korea/Regular Season#Week 1`。
- **阵容**：在赛事页面（MediaWiki API `action=parse`，每 30 秒最多 1 次）。
  - 例如 `Overwatch_Champions_Series/2026/Asia/Stage_3/Korea` 返回约 570KB 的 HTML（`html.unescape` 之后）。
  - 每支参赛队伍是一个 `<div class="general-collapsible collapsed team-participant-card">`：
    - 队名在 `team-participant-card__opponent-full` 中 `<span class="name"><a title="T1">`。
    - `toggle-area-content-active` 是 "Main" 名单，每名队员是一个 `team-participant-card__member`，其中：
      - `team-participant-card__member-role-left` 里的 `<img alt="Tank|DPS|Support">` 是位置；
      - `team-participant-card__member-name` 里 `.flag img[alt]` 是国籍，`<span class="name"><a title=…>ID</a>` 是游戏 ID。
    - `toggle-area-content-inactive` 是 "Staff"（教练组），**不使用**。
  - 例子：T1 是 Proud、ZEST（DPS），DONGHAK、Jasm1ne（Tank），skewed、Bliss（Support）。
  - 子页面（如 `…/Korea/Regular Season`）上可能没有参赛卡片，要往上一级找（如 `…/Korea`）。
  - 这是本赛事的**报名阵容**，不是这一场的实际首发。
- **Discord**：按钮的 `custom_id` 最长 100 个字符，而比赛 ID（赛事|时间|队1|队2）可能超过这个长度，所以要用短编号。

## 0. 前置：提醒去重修复与测试入口

> **已在 PR #21 上线（2026-10-19）。** 本期只需要在 `esports preview` 里加一条竞猜样例（见 0.2 最后一条），其余内容保留作背景。

### 0.1 同一场比赛重复提醒（线上 bug，2026-10-17 观察到）

- **现象**：Liquipedia 把开赛时间往后推了 10–15 分钟。比赛 ID（`赛事|开始时间|队1|队2`）随之变化，同一场比赛收到两条开赛提醒。线上记录：
  - Team Liquid vs The Kafe：21:15 和 21:40 各一条；
  - NTMR vs Disguised：22:45 和 23:10 各一条。
- **修复**：比赛 ID 格式**不变**，现有的 `esports_state.json` 可以原样沿用。去重时增加一条容差规则：
  - `reminded` / `resulted` 中如果已经有同一个 `赛事路径（含 #轮次）|队1|队2`，并且记录的开始时间与本场相差 **< 6 小时**，就视为同一场，不再发提醒或赛果；
  - 改期后的新 ID 不另外记账，每次都按容差规则识别。
- **同一天的重赛**：
  - 不同赛段（小组赛和季后赛）的赛事路径不同，所以不受影响；
  - 同一赛段 6 小时内重赛的情况会被吞掉一条，这一点接受，记为已知限制。
- **竞猜**：同样用这条规则把改期后的比赛对应到已登记的竞猜。`by_match_id` 找不到时，按 `赛事路径|队1|队2` 加 6 小时容差再找一次，所以改期不会再导致竞猜作废。

### 0.2 `@mrmeeseeks esports preview`（测试入口）

- **作用**：立刻在当前频道发一组样例，全部使用真实数据：
  - 未来 24 小时的预告；
  - 下一场比赛的开赛提醒；
  - 最近一场比赛的赛果（剧透遮罩照常）；
  - 实现竞猜之后，再加一条下一场比赛的竞猜消息（含阵容），按钮禁用，并标明 "Preview"。
- **范围**：只包含路由到**当前频道**的比赛，也就是这个频道绑定的赛区；如果当前是默认频道，就包含没绑定赛区的比赛。这样部署到新频道、执行 `region <赛区>` 之后，可以马上验证分流是否正确。没有可用的比赛时，回复 "No matches routed to this channel right now" 并说明原因（数据还没抓到，或者这个赛区近期没有比赛）。
- **不影响自动推送**：不记账（不写 `esports_state.json`，也不写 `predictions.db`），可以重复触发，也不影响自动推送的去重。
- **权限**：需要"管理频道"权限，避免被刷屏。
- **测试**：
  - preview 只产生当前频道的比赛；
  - 不修改 `state`；
  - 没有数据时的提示；
  - 权限检查。

## 1. 竞猜数据与计分 `server/predictions.py`

### 存储

`STATE_DIR/predictions.db`（SQLite，标准库 `sqlite3`，WAL 模式），四张表：

| 表 | 字段 |
|---|---|
| `matches` | `key` TEXT PK（比赛 ID 的 sha1 前 12 位）、`match_id` TEXT UNIQUE、`team1`、`team2`、`label`、`start` INT、`channel_id` INT、`message_id` INT NULL、`status` TEXT（`open`/`locked`/`resolved`/`void`）、`winner` INT NULL（1/2） |
| `picks` | `key`、`user_id` INT、`user_name` TEXT、`side` INT（1/2）、`picked_at` INT；PK(`key`,`user_id`) |
| `scores` | `key`、`user_id`、`user_name`、`correct` INT、`points` REAL、`resolved_at` INT；PK(`key`,`user_id`) |
| `seasons` | `id` INT PK、`started_at` INT |

### 接口：`PredictionStore(path)`

- **`register(match, channel_id, message_id) -> key`**：登记一场比赛（已登记就直接返回 key）。
- **`pick(key, user_id, user_name, side, now) -> PickResult`**：押注或改选。`status != open` 或 `now >= start` 时拒绝，并返回原因。
- **`counts(key) -> (n1, n2)`**：双方当前票数。
- **`lock(key) -> {1: [名字…], 2: [名字…]}`**：状态改为 `locked`，返回双方押注名单，同一场只生效一次。
- **`score_preview(key, winner) -> list[ScoreLine]`**：只读，按计分规则算出每人的得分，不写库。赛果消息里的揭晓行就用它生成。
- **`resolve(key, winner, now) -> list[ScoreLine]`**：状态改为 `resolved`，把得分写进 `scores`（与 `score_preview` 的结果一致）。同一场只结算一次；重复调用返回空列表。
- **`void(key)`**：状态改为 `void`，不计分。
- **`due(now)`**：
  - 返回已经到开赛时间、还没截止的比赛（用来截止）；
  - 返回开赛超过 48 小时、仍未结算的比赛（用来作废）。
- **`leaderboard(since, until) -> list[Row]`**：按 `resolved_at` 落在 `[since, until)` 内的得分汇总；每行有名字、总分、猜中场数和参与场数。按总分降序，相同时按猜中场数降序。
- **`season_start()`、`new_season(now)`**：查询当前赛季开始时间；开启新赛季。

### 计分

- 猜错：0 分。
- 猜中：`1 + bonus`。`bonus = max(0, (0.5 − share) × 4)`，其中 `share` 是押中那一方的人数占这场总人数的比例。
- 总分上限 3，保留 1 位小数。
- 这场总人数少于 3 时，`bonus = 0`。
- 例：10 人中 3 人押了 O2，O2 赢，每人得 `1 + 0.2 × 4 = 1.8` 分。

### 称号（周榜附带）

- **本周预言家**：周榜第一。
- **冷门猎手**：本周单场得分最高的人，并列时取最早的。
- **最惨预言家**：本周参与至少 3 场，命中率最低。

## 2. 双方阵容（`server/esports.py` 扩展 + 缓存）

- **`parse_rosters(page_html) -> {队名: [{"id", "role", "flag"}]}`**：只取 Main 名单；`role` 规范为 `tank`/`dps`/`support`/`None`。
- **`roster_pages(tournament) -> [页面…]`**：去掉 `#` 后面的部分，得到 `path`、`path` 的上一级、再上一级，最多 3 个候选。
- **`RosterCache(path)`**：
  - 存在 `herodata` 卷的 `rosters.json`，格式为 `{页面: {"fetched_at", "teams"}}`。
  - 同一页面 24 小时内不重复抓取；两次 `action=parse` 之间至少间隔 30 秒。
  - 抓取失败时继续用旧缓存。没有缓存的页面，这一轮就不显示阵容。
- **`roster_for(match) -> (team1_roster | None, team2_roster | None)`**：按候选页面依次找，第一个包含该队的页面就用它。
- 阵容拿不到时，竞猜消息照发，只是阵容显示 `Roster unavailable`。

## 3. 推送流程（接进 `esports_feed.plan` / `tick`）

### `Post` 扩展

`Post` 新增可选字段 `kind`（默认 `"text"`）和 `data`：

| kind | 何时 | tick 的副作用 |
|---|---|---|
| `prediction` | 每日预告之后，每场符合条件的比赛一条 | 发带按钮的消息，`register()` 记下消息 ID |
| `lock` | 已经到开赛时间、状态为 `open` | `lock()`；编辑竞猜消息，禁用按钮并显示最终票数；在同一个频道发押注名单 |
| `void` | 开赛满 48 小时仍未结算 | `void()`；编辑竞猜消息，注明已作废 |
| `leaderboard` | 每周一悉尼 10:00 之后、本周还没发过 | 发周榜，发到默认频道 |

- **提醒和赛果**：仍是 `text` 类型。
  - **提醒**：附上竞猜消息的链接和当前票数。
  - **赛果**：在原文后面追加一行揭晓 `||Correct: A +1.0, D +1.8 · Missed: B, C||`（没人押就省略），揭晓内容由 `score_preview()` 在发送前算出；发送成功后才执行 `resolve()` 落库，发送失败则不结算，下一轮重试。
- **数据传递**：`plan()` 会额外收到一份竞猜快照，它仍然是纯函数，不直接读写数据库。快照内容是：每场比赛的 `status`/`counts`/`message link`，以及本周的周榜数据。

### 条件

- **发竞猜消息**：
  - 比赛未结束；
  - 不是双方都是 TBD；
  - 开始时间在每日预告覆盖的 24 小时以内；
  - 数据库里还没有这场比赛。
- **补发**：bot 不在线错过了 10:00，或者某场比赛是预告之后才出现在赛程里的，那么只要开赛前还有至少 15 分钟，就在下一轮补发它的竞猜消息。
- **周榜**：
  - 统计范围：上周一 00:00 到本周一 00:00（悉尼时间）结算的得分；
  - 发送后在 `esports_state.json` 里记下 `leaderboard_week`；
  - 上周没有任何得分就不发。

### 发送函数

- `send(channel_id, text, view=None) -> int | None`：返回消息 ID。补丁推送不传 `view`，行为不变。
- `edit(channel_id, message_id, text=None, view=None)`：用来更新票数、截止、作废。
- 编辑失败（消息被删、没有权限）只记日志，不阻塞本轮的其他消息，**也不重试**。数据库里的状态以截止和结算的结果为准。

## 4. Discord 交互 `server/prediction_ui.py`

### 按钮

- `PredictButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pred:(?P<key>[0-9a-f]{12}):(?P<side>[12])")`，在 `setup_hook` 里用 `bot.add_dynamic_items(PredictButton)` 注册。
- 按钮文字：`<队名> · <票数>`。
- **点击后**：
  1. `pick()`；
  2. 用 ephemeral 消息回复点击的人："You picked T1 — you can change until the match starts." 或者拒绝的原因（"Picks are closed."）；
  3. 编辑竞猜消息，更新两个按钮上的票数。
- **并发**：同一场比赛的票数编辑最多每 2 秒执行一次，用每场比赛一把锁加上节流，避免撞上 Discord 的速率限制。

### 消息格式（英文，和现有推送一致）

```
🎯 **Who wins?** T1 vs ZETA DIVISION · OWCS Korea Stage 3 - Regular Season - Week 1 · Bo5
Starts <t:TS:t> (<t:TS:R>) — picks close at start

**T1**  🛡️ DONGHAK, Jasm1ne · ⚔️ Proud, ZEST · 💉 skewed, Bliss
**ZETA DIVISION**  🛡️ … · ⚔️ … · 💉 …
-# Registered event roster — the starting lineup may differ. Data: Liquipedia (CC-BY-SA)
[ T1 · 0 ]  [ ZETA DIVISION · 0 ]
```

- **截止**：
  - 按钮禁用，文字显示最终票数；
  - 另发一条：`🔒 Picks closed: T1 vs ZETA DIVISION — T1: A, B, C · ZETA DIVISION: D, E`，名字用纯文本，不 @ 任何人。
- **周榜**：
  ```
  🏆 **Prediction leaderboard — week of <date>**
  1. A — 6.4 pts (5/7) …（前 10）
  🔮 Prophet of the week: A · 🎯 Underdog hunter: D (+2.6) · 🙈 Worst prophet: B (1/5)
  ```

### 指令（`bot.py`）

- **`predictions`**（`ARG_COMMANDS`）：
  - **`@mrmeeseeks predictions`**：本周榜和赛季榜前 10 名，以及发指令的人自己的排名。
  - **`@mrmeeseeks predictions reset`**：开新赛季，需要 `manage_guild` 权限。
- **`HELP_TEXT`**：加上这两条。

## 不做（本期）

- 竞猜比分（只猜胜负）。
- 开黑战报（B）：下一期再做。
- 押注奖励、身份组奖励。
- 每场比赛的实际首发阵容（数据源没有）。
- 一个服务器以外的多服务器排行榜。

## 已知限制

- 比赛改期在 6 小时以内的，通过容差规则对应到原来那场（见 0.1）。TBD 确定后队名变化，会被当成新比赛：旧的竞猜在 48 小时后作废，新的竞猜在开赛前至少 15 分钟补发。
- 同一赛段 6 小时内两队重赛，第二场的提醒和赛果会被去重规则吞掉。
- 阵容是报名阵容，可能和实际出场不同。
- 弃权局（W/FF）没有比分，48 小时后作废、不计分。

## 测试

全部离线，在 Python 3.12 生产镜像里跑。

- **fixture**：新增 2026-10-18 保存的 Korea Stage 3 赛事页面（API parse 结果）。
- **`tests/test_predictions.py`**（临时数据库）：
  - 押注、改选、开赛后拒绝；
  - 截止名单，以及重复截止无效；
  - 结算计分：冷门加成、少于 3 人不加成、3 分上限、猜错 0 分，以及重复结算不重复计分；
  - 作废；
  - 周榜和赛季榜的时间范围、排序和称号；
  - 新赛季。
- **`tests/test_rosters.py`**：
  - 解析出全部参赛队，T1 的 6 名队员和位置都正确，教练不混入；
  - 往上一级查找的候选页面；
  - 缓存在 24 小时内不重复抓取、两次抓取间隔至少 30 秒、失败时用旧缓存。
- **`tests/test_esports_feed.py`** 新增：
  - 竞猜消息的产生条件（TBD、24 小时窗口、补发、已登记）；
  - 截止、作废、周榜（周一 10:00、上周没有得分不发、不重复发）；
  - 提醒附上链接和票数，赛果附上剧透揭晓；
  - 与赛区路由的组合：竞猜消息发到比赛所属赛区的频道。
- **`tests/test_prediction_ui.py`**（假的 Discord 交互对象）：
  - 押注、改选、截止后点击；
  - ephemeral 回复；
  - 票数编辑的节流；
  - `custom_id` 模板解析。
- **指令测试**：`predictions` 和 `predictions reset` 的路由与权限。

## 实现说明（2026-10-19 写计划时确定）

以下内容是在原型中验证过的实现细节。和上文冲突时，以本节为准。

- **模块划分**：
  - 竞猜的计划、格式和副作用都放在新模块 `server/prediction_feed.py`（`PredictionFeed`），`esports_feed.py` 只做最小改动：
    - `Post` 新增 `kind`（默认 `"text"`）和 `data`；
    - 提醒和赛果的 `Post` 带上 `data={"match": m}`；
    - 状态新增 `leaderboard_week`；
    - `EsportsFeed(..., predictions=None)` 在 `tick` 里依次调用 `augment` / `execute` / `after_sent`，抓到比赛后调用 `prepare`（后台准备阵容）；
    - 新增 `preview_prediction`；
    - `run(send, edit=None)`。
  - `esports_feed` 不 import 竞猜模块。
- **竞猜存储**：
  - `matches` 表新增 `url`（竞猜消息的链接，提醒里引用）。
  - 新增 `find(match, listed)`：先按 ID 精确匹配，否则按"同一赛事、同样两队、开始时间相差不到 6 小时、并且旧 ID 已不在当前汇总页"匹配，规则和 `esports_feed.already` 一致。
  - `predictions.py` 从 `esports_feed` import `match_base` 和 `SAME_MATCH_WINDOW`。
- **发送函数**：`send(channel_id, text, view=None)` 返回 `(消息 ID, 链接)`；`edit(channel_id, message_id, view)`。按钮在推送模块里只用描述字典 `view_spec` 表示，由 `prediction_ui.build_view` 生成 discord 组件。
- **截止**：先用 `names()` 公布名单（发送失败就下一轮重试），发送成功后再 `lock()`，最后编辑原消息。截止和作废都按**当前**汇总页上的开赛时间判断；比赛已经从汇总页消失的，用登记时的时间。
- **结算**：赛果没有胜负（比分相同或缺失）时作废，不计分。
- **周榜**：周一悉尼 10:00 之后发到默认频道；上周没有得分时不发，但仍记下 `leaderboard_week`。
- **阵容**：位置还有 `Flex`（全能位，图标 🔀）。
- **测试文件**：
  - `test_predictions.py`；
  - `test_rosters.py`；
  - `test_prediction_feed.py`：完整流程、改期、作废、平局、无频道、发送失败、周榜、preview；
  - `test_prediction_ui.py`；
  - `test_prediction_bot.py`；
  - 另外，`test_esports_bot.py` 的假 `send` 改为返回消息对象（和 discord.py 一致）。
