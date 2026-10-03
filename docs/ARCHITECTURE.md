# 架构与协议

给想修改或扩展 mrmeeseeks 的开发者看。只想跑起来的话，看根目录的 README 就够了。

## 模块

| 文件 | 职责 |
|---|---|
| `server/bot.py` | 入口。Discord bot（指令、下拉菜单、发消息）和 WebSocket 服务跑在同一个 asyncio 事件循环里 |
| `server/ws_server.py` | WebSocket 服务：握手、配对码校验、截图接收、限流，把事件交给 bot |
| `server/registry.py` | 在线客户端表和配对状态机，不依赖网络和 Discord，便于单独测试 |
| `server/analyzer.py` | 调用 Claude：先识别画面事实，再基于事实给建议 |
| `server/heroparse.py` | 把 Overwatch Wiki 英雄页面的 wikitext 解析成规范化 dict（纯函数） |
| `server/herodata.py` | 英雄数据：从 Wiki 抓取、缓存到 JSON、定时刷新、按中英文名查英雄 |
| `server/damage.py` | 伤害衰减、护甲/护盾、爆头倍率、几枪击杀（纯函数） |
| `server/hero_qa.py` | 英雄问答：Claude tool use 循环，数字只来自上面两个模块和 `matchups.py` |
| `server/matchups.py` | 英雄对位（克制）数据：从 counterwatch.gg 抓取、缓存、每日刷新 |
| `server/esports.py` | 赛事数据：解析 Liquipedia:Matches（比赛）和 esports.overwatch.com 首页（新闻） |
| `server/esports_feed.py` | 赛事推送：`plan()` 纯函数决定发什么，`EsportsFeed` 负责抓取、状态持久化和发送 |
| `client/client.py` | 本地客户端：连接/重连、配对码输入、Tab 监听、两段截图、上传 |

## 客户端状态

```
启动 ──hello──▶ 已握手（拿到 MEE-XXXX，出现在 Discord 下拉菜单里）
                  │  Discord 用户选择此标识 → 服务器下发 pair_request
                  ▼
              等待配对码 ──错 5 次 / 超时 5 分钟──▶ 回到"已握手"
                  │  配对码正确
                  ▼
              已绑定频道（开始接受截图）
                  │  @mrmeeseeks disconnect → 回到"已握手"
                  │  连接断开 → 从表中移除，重连后拿到新标识
```

## WebSocket 协议

所有消息都是 JSON 文本帧。客户端连上后 10 秒内必须先发 `hello`，否则连接会被关闭。

**客户端 → 服务器**

| type | 字段 | 说明 |
|---|---|---|
| `hello` | `hostname`, `version` | 握手 |
| `pair_code` | `code` | 8 位数字配对码 |
| `snapshot` | `scoreboard`, `hud` | 两张 JPEG 的 base64；只有已绑定的客户端才会被处理 |

**服务器 → 客户端**

| type | 字段 | 说明 |
|---|---|---|
| `welcome` | `client_id` | 分配的标识，如 `MEE-7K3Q` |
| `pair_request` | `user`, `channel`, `expires_in` | 有人在 Discord 里选择了本客户端 |
| `pair_result` | `ok`, `reason`, `attempts_left` | 配对码校验结果 |
| `unpaired` | `reason` | 在 Discord 里被断开 |
| `info` / `error` | `message` | 提示信息 |
| `analysis` | `summary` | 分析结论的第一行 |

## 截图限流

同一个客户端：
- 上一组还在分析时，新截图直接跳过
- 两次截图间隔小于 `MIN_SNAPSHOT_INTERVAL`（服务器）或 `--cooldown`（客户端）时跳过
- 单张图片超过 8 MB 拒绝

## 分析的两步设计

目标是让建议只基于"看到的事实"，而不是模型的猜测。

1. **识别**（`Analyzer.extract`）：输入两张图，输出固定格式的 JSON：地图、模式、攻防、阶段、双方阵容，每项带置信度，看不清就填 `null`。
2. **建议**（`Analyzer.advise`）：**不给模型看图**，只给第 1 步的 JSON 和 `context` 里的补充数据。系统提示词要求每条建议注明依据，置信度低于 0.6 视为未知，数据不足直接说。

### 克制关系

建议里的"谁克谁"只来自 counterwatch.gg 的对位评分，不用模型常识。

- `matchups.MatchupStore` 每天抓取 counterwatch 的 53 个英雄页面，解析页面内嵌的 `counterScoreData`。评分是对决和团战结果，全段位，去掉了英雄本身强弱，不是整局胜率；正数表示前一个英雄占优，约等于百分点。
- `analyzer.build_matchup_data` 用当前识别出的阵容算出每个我方英雄对敌方各英雄的评分和合计，以及同职责的换人候选（按合计排序，取前 3），作为 `matchup_data` 交给模型。计算由代码完成，模型只负责组织语言。
- 系统提示要求克制说法必须引用 `matchup_data` 里的数字；没有数据的对位不做克制断言。
- 对局中按 Tab（`snapshot_received`）和 `@mrmeeseeks analyze` 都走 `Analyzer.advise`，都会带上对位数据。

## 英雄问答

`@mrmeeseeks` 后面跟的文本如果不是 connect/status/disconnect/help，就交给 `HeroQA.answer`。

1. **数据**：`herodata.fetch_all` 通过 MediaWiki API 抓取 `Category:Heroes` 下每个英雄的页面，`heroparse.parse_hero` 解析 infobox（血量/护甲/护盾/副职业）、`Ability_details` 中的武器、`ChangelogsTabber` 的 `owpvp` 补丁。快照写到 `HERO_DATA_PATH`；新快照明显变少（Wiki 改版）时不替换旧的。
2. **工具**：`get_hero_stats`、`get_patch_history`、`shots_to_kill`、`get_matchups`（counterwatch 对位评分）。英雄名在工具内解析，不唯一时返回候选，由模型反问。
3. **计算**：`damage.shots_to_kill` 逐发模拟 护盾 → 护甲 → 生命值，规则常量和 Wiki 出处写在 `damage.py` 顶部。
4. **约束**：系统提示要求所有数字来自工具结果、引用补丁原文和日期、写出假设（弹丸全中、Role Queue 等）。

本期不支持：TTK、技能伤害、perk 加成。

## 赛事推送

用 `@mrmeeseeks esports here` 指定一个频道后，`EsportsFeed.run` 每分钟执行一次：

1. **抓取**：比赛每 10 分钟通过 Liquipedia MediaWiki API 解析一次 `Liquipedia:Matches`（条款要求 gzip、带联系方式的 User-Agent、parse 每 30 秒最多 1 次，并注明 CC-BY-SA 来源），只保留赛事路径以 `Overwatch Champions Series/`、`Overwatch World Cup/` 开头的比赛。新闻每 3 小时抓一次官方首页。抓取失败时保留上一次的数据。
2. **计划**：`esports_feed.plan(now, matches, news, state)` 返回要发的消息，每条附带发出后要记的账：悉尼时间 10:00 发未来 24 小时的预告，开赛前 15 分钟发提醒（开赛超过 5 分钟不补发），比赛开始后 24 小时内发赛果（剧透遮罩），以及新出现的新闻。首次启用时只记账、不发送。
3. **发送**：发送成功后才记账，失败的下一轮重试。没有设置频道时照常记账、不发送。状态保存在英雄数据缓存同一目录的 `esports_state.json`。

已知限制：Liquipedia 汇总页只有最近约 50 场即将进行和 50 场已结束的比赛；官方首页只放 1–2 条精选新闻。

## 扩展点

`Analyzer.analyze` 里的 `context` 字典就是接入更多事实数据的地方，目前三项都是"暂未接入"：

- `队友生涯数据`：按计分板上的名字调用 OverFast API（https://overfast-api.tekrop.fr/）搜索玩家，取各英雄游戏时长和胜率；重名或生涯私密时标注"无法确认"
- `地图分段对照`：每张地图每一段的关键位置名称，建议做成一个手动维护的 JSON 文件
- `版本数据`：`herodata.HeroStore` 已有当前版本的英雄数值和补丁，可以直接作为 `patch_data` 接入

新增 Discord 指令：在 `MeeseeksBot.on_message` 里按 `cmd` 分发。
