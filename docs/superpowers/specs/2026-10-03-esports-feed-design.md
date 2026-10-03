# Overwatch 赛事推送频道：设计

日期：2026-10-03

## 目标

在一个专门的 Discord 频道里，自动推送 OWCS 和官方赛事的：
1. 每日赛程预告
2. 开赛提醒
3. 赛果（加剧透遮罩）
4. 官方新闻

成功标准：
- 推送不重复、不遗漏近期比赛，bot 重启后也照常运行。
- 赛果不会直接剧透。
- 频道不被次级联赛刷屏。
- 遵守数据源的使用条款。

## 用户确认的决策

- **推送内容**：赛程预告、开赛提醒、赛果、新闻/公告，四种都要。
- **赛事范围**：只推 OWCS 和官方赛事（Overwatch Champions Series、Overwatch World Cup），不推 FACEIT League 等第三方或次级赛事。
- **剧透**：胜者和比分放在 Discord 剧透遮罩 `||…||` 里。
- **时区与节奏**：每天悉尼时间（Australia/Sydney）10:00 发未来 24 小时的预告；开赛前 15 分钟提醒。
- **方案 A**：以 Liquipedia 赛程页为主数据源，官方网站提供新闻。抓取部分单独封装，以后可以换成 LiquipediaDB API（需要申请 key）。
- **默认设置（用户未提出异议）**：
  - 推送频道用指令 `@mrmeeseeks esports here` 设置。
  - User-Agent 写项目的公开 GitHub 地址，不写任何个人信息。

## 数据源事实（2026-10-03 核对）

- **Liquipedia MediaWiki API**（`https://liquipedia.net/overwatch/api.php`）
  - 条款要求：
    - 请求限速每 2 秒 1 次，`action=parse` 每 30 秒 1 次。
    - 必须开启 gzip，否则返回 406。
    - 必须使用自定义 User-Agent，并带联系方式。
    - 结果要尽量缓存。
    - 要注明来源（CC-BY-SA 3.0）。
    - 禁止自动访问非 API 的 HTML 页面。
  - `action=parse&page=Liquipedia:Matches&prop=text&format=json&formatversion=2` 返回约 370KB 的 HTML，分两块：
    - `data-toggle-area-content="1"`（即将进行）：50 场比赛，每场是一个 `<div class="match-info">`，有 `data-timestamp`（Unix 秒）、赛事链接 `title`（如 `Overwatch Champions Series/2026/Asia/Stage 3/Korea/Regular Season#Week 1`）、双方队名（`<span class="name"><a title="...">`），以及 Bo 场数。
    - `data-toggle-area-content="2"`（已结束）：50 场比赛，带 `data-finished="finished"` 和两个 `match-info-header-scoreholder-score`（如 `2 : 3`）。
    - 每场的 `match-info-tournament-name` 里有赛事链接 `href="/overwatch/<路径>#<轮次>"`，以及 Liquipedia 给的显示名（如 `OWCS Korea Stage 3 - Regular Season - Week 1`）。
    - 队伍页面不存在时，`title` 带 ` (page does not exist)` 后缀；TBD 队伍没有链接，只有纯文本。
  - 本次核对时，100 场比赛里有 26 场是 OWCS（Korea、Japan、China Stage 3），其中 6 场已结束；其余主要是 FACEIT League。
- **esports.overwatch.com**
  - `/en-us/news` 返回 500，不可用。
  - 首页 `/en-us` 有 1–2 条精选新闻，格式为 `<a href="https://esports.overwatch.com/en-us/news/<slug>">`，链接里带日期和 `<h2>` 标题。
  - 网站没有 robots.txt，也没有公开 API。
  - 官方赛程页 `/en-us/schedule` 内嵌的比赛记录（Airtable）目前全部是已结束的比赛，不适合做预告，所以不使用。

## 1. 数据层 `server/esports.py`

### 比赛

- **抓取**：`fetch_matches()` 每 10 分钟调用一次上面的 parse 接口（开启 gzip，User-Agent 为 `mrmeeseeks-discord-bot/1.0 (https://github.com/Anbrose/overwatch-mrmeeseeks)`）。
- **解析**：`parse_matches(html) -> list[Match]`，每场比赛的结构：
  ```json
  {"id": "<tournament>|<start>|<team1>|<team2>", "start": 1791000000,
   "tournament": "Overwatch Champions Series/2026/Asia/Stage 3/Korea/Regular Season#Week 1",
   "label": "OWCS Korea Stage 3 - Regular Season - Week 1",
   "team1": "T1", "team2": "ZETA DIVISION", "best_of": 5,
   "finished": false, "score": null, "url": "https://liquipedia.net/overwatch/<tournament path>"}
  ```
  - `finished` 为 true 时，`score` 是 `[队1, 队2]`。
  - 队名缺失（TBD）的比赛保留，队名记为 `"TBD"`。
- **过滤**：常量 `OFFICIAL_PREFIXES = ("Overwatch Champions Series/", "Overwatch World Cup/")`，赛事路径以这些前缀开头的比赛才保留。
- **`label`**：直接使用 Liquipedia 给的赛事显示名，不再根据路径拼接（比拼出来的更准确）。

### 新闻

- **抓取**：`fetch_news()` 每 3 小时抓一次首页。
- **解析**：`parse_news(html) -> list[{"url", "title", "date"}]`，标题或日期取不到时为 `None`，链接不能缺。

### 容错

- 抓取异常或 HTTP 错误：记日志，这一轮不更新。
- 页面有内容、但解析出 0 场比赛：记 warning（可能是改版），这一轮不更新。

## 2. 推送调度 `server/esports_feed.py`

### 状态 `esports_state.json`

- 位置：与英雄数据缓存同一目录（Docker 里是 `herodata` 卷）。
- 写入：临时文件加 `os.replace`，写失败只记日志。
- 内容：
  ```json
  {"channel_id": 123 | null, "reminded": {"<id>": start, ...}, "resulted": {"<id>": start, ...},
   "digest_date": "2026-10-03" | null, "seen_news": ["<url>", ...],
   "initialized": true, "news_initialized": true}
  ```
- `reminded` 和 `resulted` 都是 `{id: start}`，只保留最近 14 天，避免无限增长；`seen_news` 只保留最近 200 条。

### 纯函数 `plan(now, matches, news, state) -> (messages, new_state)`

- **初始化**：比赛和新闻分开初始化，互不阻塞（官方首页打不开时，比赛照常工作）。`initialized` 为 false 时，把已经结束的比赛记入 `resulted`；`news_initialized` 为 false 时，把已有新闻记入 `seen_news`。初始化时不产生任何消息。
- **返回值**：`list[Post]`，每个 `Post` 包含 `text`（可以为 None，表示只记账）和 `marks`（发出后要记的账）。
- **预告**：
  - 发送条件：悉尼本地时间 ≥ 10:00、`digest_date` 不是今天（悉尼日期），并且未来 24 小时内有比赛。
  - 内容：按开始时间排序的比赛列表。
  - 发送后把 `digest_date` 设为今天。没有比赛时也设为今天，只是不发消息。
- **提醒**：`start - 15 分钟 ≤ now ≤ start + 5 分钟`，且比赛不在 `reminded` 里。
- **赛果**：`finished`、不在 `resulted` 里、并且 `start` 在最近 24 小时内。
- **新闻**：不在 `seen_news` 里的链接。

### 后台任务 `run_feed(bot, store, ...)`

- 每 60 秒执行一次 `plan`。距离上次抓比赛超过 10 分钟就重新抓，距离上次抓新闻超过 3 小时就重新抓新闻。
- 没有设置频道时照常记账、不发送，这样之后再设置频道也不会补发一堆旧消息。
- 发送失败（频道被删除、没有权限）时记日志，不更新这条消息对应的状态，下一轮再重试。

### 消息格式（英文）

- **预告**：
  ```
  📅 Overwatch esports — next 24h
  • <t:TS:t> (<t:TS:R>) T1 vs ZETA DIVISION · OWCS 2026 Asia Stage 3 Korea — Regular Season Week 1 · Bo5
  ```
  末尾加一行 `Data: Liquipedia (CC-BY-SA)`。
- **提醒**：`🔴 Starting <t:TS:R>: T1 vs ZETA DIVISION · <label> · Bo5 — watch: https://www.twitch.tv/ow_esports · <url>`
- **赛果**：`✅ T1 vs ZETA DIVISION · <label> — result: ||T1 3 : 1 ZETA DIVISION||` 加赛事链接，再加 `Data: Liquipedia (CC-BY-SA)`。
- **新闻**：`📰 <title or url> <url>`。
- 单条消息超过 1990 字符时，预告按行拆成多条。

## 3. Discord 指令与接入 `server/bot.py`

- `esports here` / `esports off` / `esports status` 加入 `COMMANDS`（`parse_command` 原样识别）。
- **`here` / `off`**：发指令的人需要在该频道有 `manage_channels` 权限，否则回复 "You need Manage Channels permission to change the esports channel." `here` 写入 `channel_id`，`off` 清空。
- **`status`**：回复当前推送频道（或未设置），以及接下来最多 3 场比赛。
- **启动**：后台任务 `EsportsFeed.run(bot.post_esports)`，退出时取消。`post_esports` 先等 bot 登录完成（`wait_until_ready`），发送时不 ping 任何人。
- **`HELP_TEXT`**：加上这三条指令。

## 配置

- 不新增环境变量。状态文件放在英雄数据缓存目录（`HERO_DATA_PATH` 所在目录）。
- 常量都放在 `esports_feed.py` 顶部：时区 `Australia/Sydney`、预告时间 10:00、提醒提前 15 分钟、比赛每 10 分钟抓一次、新闻每 3 小时抓一次。

## 不做（本期）

- 第三方或次级赛事（FACEIT League 等）。
- 各小局地图的比分。
- 按赛区或队伍订阅。
- LiquipediaDB API。
- 每场比赛单独的直播链接（统一用官方 Twitch 频道）。
- 多个推送频道。

## 已知限制

- Liquipedia 汇总页只显示最近约 100 场比赛，赛程密集时，24 小时预告可能不完整。
- 官方首页只放 1–2 条精选新闻，新闻推送频率会很低。
- 两个数据源都是解析页面，对方改版时会失效：这时只记日志、不推送，不会发出错误内容。
- 比赛 ID 由赛事、开始时间和队名拼成：TBD 队伍确定后、或比赛改期后，会被当成一场新比赛。提醒窗口只有 15 分钟，到时队伍通常已经确定，所以影响很小。

## 测试

全部离线运行，在 Python 3.12 生产镜像里跑。

- **fixture**：2026-10-03 保存的 Liquipedia parse 结果（JSON），以及官方首页 HTML。
- **`tests/test_esports.py`**：比赛解析（时间戳、赛事、队名、Bo、比分、TBD）、官方赛事过滤、`label` 和 `id` 的生成、新闻解析，以及空页面和改版时的处理。
- **`tests/test_esports_feed.py`**，`plan` 用固定时间测试：
  - 10:00 之前不发、10:00 发、当天不再重复发。
  - 没有比赛时不发预告，但 `digest_date` 会更新。
  - 提醒的时间窗口，开赛超过 5 分钟不补发。
  - 赛果只发一次，超过 24 小时的赛果不发。
  - 首次启用时不刷旧数据。
  - 悉尼夏令时切换日（2026-10-04 开始夏令时）。
  - 剧透遮罩格式、消息拆分、状态清理。
- **`tests/test_esports_bot.py`**：`esports here/off/status` 的路由和权限检查（用假的 Discord 消息），以及 `post_esports` 的发送行为。
