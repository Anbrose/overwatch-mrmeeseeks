# 克制关系以统计数据为准：设计

日期：2026-10-03

## 目标

换英雄建议里的克制判断目前来自模型的常识（`ADVISE_SYSTEM` 第 2 条），有时与统计结果相反。改为只以 counterwatch.gg 的对位评分为准，并让 `@mrmeeseeks` 问答也能查询克制关系。

成功标准：
- 建议和问答中出现的"X 克制 Y"都带 counterwatch 的数字。
- 没有数据的对位不做克制断言。
- 对局中（connect 后按 Tab）的建议和 `@mrmeeseeks analyze` 都自动使用这份数据。

## 用户确认的决策

- **数据源**：counterwatch.gg 对位评分。它的含义是对决和团战结果，按全段位统计，去掉英雄本身强弱，**不是整局胜率**。
  - 示例：查莉娅对温斯顿 +7.1，查莉娅占优，样本 552 人。
  - 用户原先以为"查莉娅被温斯顿克制"，这份数据结论相反。数据源由用户选定。
- **克制规则**：只认数据。没有数据的对位不说谁克谁；地图、站位等其他推理仍可用常识。
- **范围**：换英雄建议和英雄问答都接入。
- **方案 A**：每日抓取并缓存对位矩阵，代码预先算好候选，再交给模型。

## 数据源事实（2026-10-03 核对）

- **抓取许可**：`robots.txt` 对所有 User-Agent 都是 `Allow: /`，只禁止 `/auth/`。
- **英雄列表**：`https://www.counterwatch.gg/sitemap.xml` 中的 `/stats/overwatch/heroes/<slug>`，共 53 个（比 Wiki 少 Doctrine）。slug 例：`soldier76`、`junker-queen`、`wrecking-ball`、`dva`、`torbjorn`。
- **页面结构**：英雄页面是服务端渲染的 Next.js 页面，内嵌 `"counterScoreData":{"threats":[...],"targets":[...],"usedAllFallback":false}`，在 flight 数据中引号被转义为 `\"`。
  - 每个列表各 52 项，每项包含 `heroRawName`（如 `WINSTON`、`SOLDIER76`、`JUNKERQUEEN`）、`roleName`、`counterScore`、`resid1v1`、`resid1vn`、`counterType`、`distinctUsers`。
  - `targets` 表示本页英雄对对手的评分（正数 = 本页英雄占优）。
  - `threats` 是对手对本页英雄的评分，与对方页面上的 `targets` 取反一致。已核对：Zarya→Winston +7.1149，Winston→Zarya −7.1149。
- **展示门槛**：至少 50 名玩家才展示。查莉娅页面最少 288 人、最多 1541 人。
- **段位**：对位数据只有全段位一种，URL 参数（`?rank=`、`?tier=`）不生效。
- **更新**：页面写明 "Data refreshes daily … last updated <date>"。

## 1. 数据层 `server/matchups.py`

- **抓取**：读 sitemap 得到英雄页面列表，逐个抓取（串行，每次间隔约 1 秒，User-Agent 与 `herodata` 相同）。用 `json.JSONDecoder().raw_decode` 从 `"counterScoreData":` 之后解析出对象，先把 `\"` 还原为 `"`。
- **名字**：所有键用 `herodata._norm` 归一化（`SOLDIER76`→`soldier76`，`Soldier: 76`→`soldier76`，`Torbjörn`→`torbjorn`）。
- **快照**：`{"fetched_at", "source_updated", "scores": {a: {b: {"score", "type", "users"}}}}`。只存 `targets`，反向查询时取对方页面的 `targets`。
- **`source_updated`**：取页面里 "last updated <date>" 的日期文本，取不到就为 `null`。
- **缓存**：`herodata` 卷下的 `matchups.json`，用临时文件加 `os.replace` 写入；写失败时记日志，继续使用内存里的数据。
- **刷新**：并入现有刷新循环，每 `HERO_REFRESH_HOURS` 小时一次，英雄数据刷新完接着刷新对位数据。
- **快照校验**：新快照满足以下条件才替换：英雄数 ≥ 旧快照的 90%，且每个英雄的平均对位数 ≥ 旧值的 90%。无旧快照时非空即可。单个页面失败只跳过并记日志。
- **接口**：
  - `MatchupStore(path)`，`.ready`、`.load()`、`.save()`、`.accept(new)`、`async .refresh(fetch)`
  - `.score(a, b) -> dict | None`
  - `.profile(hero, top=5) -> {"strong_against": [...], "weak_against": [...]}`：`weak_against` 用对手页面上的 `targets`，即谁对它的评分最高。
  - `async fetch_all() -> dict`

## 2. 换英雄建议 `server/analyzer.py`

- **纯函数 `build_matchup_data(facts, matchups, roles) -> dict | None`**：
  - **英雄来源**：每个玩家取 `hero`（`remembered` 也算）；没有 `hero` 但有 `last_seen_hero` 时，使用它并标 `"likely": true`。
  - **`current`**：每个我方英雄对每个已知敌方英雄的 `score`，以及有数据部分的合计 `vs_enemies_total` 和对位数 `pairs_with_data`。
  - **`swap_candidates`**：对每个我方位置，取 `roles` 中与该英雄同职责的所有英雄，按对已知敌方英雄的评分合计排序，取前 3。只计入有数据的对位，并给出 `pairs_with_data`。当前英雄不进入候选列表。
  - **`enemies_without_data`**：未识别或在 counterwatch 上没有数据的敌方英雄。
  - **说明字段**：`source` 写明来源和更新日期，`unit` 写明 "positive = first hero favored, ≈ percentage points"。
  - 敌方没有任何已知英雄、或者没有对位数据时，返回 `None`。
- **`roles`**：从现有 `roster.json`（`[{key, name, role}]`）构建，键为归一化后的名字。
- **`Analyzer(..., matchups=None, roles=None)`**：`advise` 在 `build_matchup_data` 有结果时，把它放进 payload 的 `matchup_data`。
- **`ADVISE_SYSTEM` 第 2 条改为**：
  - 克制关系只能来自 `matchup_data`，引用时带数字，例如 `(basis: Zarya vs Winston +7.1, counterwatch)`。
  - 没有数据的对位不说谁克谁；地图、站位、进度等推理仍可使用常识。
  - 推荐换人时优先参考 `swap_candidates`。合计差距在 3 以内时说明差别不大。
  - 标了 `likely` 的英雄要说明"可能"。
- 第 3 条"数据不全也要给建议"保持不变。

## 3. 英雄问答 `server/hero_qa.py`

- **新工具 `get_matchups(hero, opponent?)`**：
  - 只给 `hero`：返回 `profile(hero, 5)`。
  - 给出 `opponent`：返回双向 `score`。
  - 都附上 `source`、`source_updated` 和含义说明。
  - 英雄名用 `HeroStore.resolve` 解析，不唯一时返回候选；解析后再归一化去查对位。
  - 没有数据时返回 `{"error": "no_matchup_data", ...}`。
- **系统提示**：问克制关系时必须调用 `get_matchups` 并引用数字，说明它衡量的是对决和团战表现，不是整局胜率。
- **构造参数**：`HeroQA(..., matchups=None)`；为 `None` 时 `get_matchups` 返回数据未就绪。

## 4. 接入 `server/bot.py`

- 启动时创建 `MatchupStore`，放在 `HERO_DATA_PATH` 所在目录下，文件名 `matchups.json`，然后 `.load()`。
- `refresh_loop` 改为依次刷新英雄数据和对位数据。
- 同一个 `MatchupStore` 实例传给 `Analyzer` 和 `HeroQA`；`roles` 用 `roster.json` 构建。
- `snapshot_received`（对局中按 Tab）和 `_reanalyze` 都调用 `Analyzer.advise`，因此都会带上对位数据，这两处代码不需要改。"阵容没变就不出新建议"的行为保持不变。

## 不做（本期）

- **按段位的对位数据**：数据源不提供。
- **整局胜率口径的对位数据**：没有可用的数据源。
- **协同（synergy）数据**：页面上有 `friendlyScores`，本期不接。
- **换地图建议、英雄强度榜。**

## 测试

全部离线运行，在 Python 3.12 生产镜像里跑。

- **fixture**：
  - counterwatch 的 Zarya、Winston 页面（2026-10-03 抓取）。
  - 一段精简的 sitemap。
- **`tests/test_matchups.py`**：
  - 数据提取（Zarya→Winston 7.1149，样本 552）。
  - 正反向一致性。
  - 名字归一化（`SOLDIER76`、`JUNKERQUEEN`）。
  - 页面缺少 `counterScoreData` 时跳过。
  - 快照校验、原子写入、缓存不可写。
  - `profile` 的排序。
- **`build_matchup_data`**：
  - 合计与排序。
  - 职责筛选。
  - 跳过未知或无数据的英雄。
  - `likely` 标记。
  - 无数据时返回 `None`。
- **`advise`**：用假的 Claude 客户端，检查 payload 带有 `matchup_data`、系统提示包含新规则；无对位数据时不带 `matchup_data`。
- **`get_matchups`**：
  - 单英雄。
  - 双英雄。
  - 中文名。
  - 未知英雄。
  - 无数据。
  - 数据未就绪。
