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
| `server/esports_feed.py` | 赛事推送：`plan()` 纯函数决定发什么（含按赛区分频道），`EsportsFeed` 负责抓取、状态持久化和发送 |
| `server/predictions.py` | 赛事竞猜存储（`STATE_DIR/predictions.db`，SQLite）：押注、截止、结算、作废、计分、周榜和赛季榜 |
| `server/rosters.py` | 赛事报名阵容：解析 Liquipedia 赛事页面的参赛卡片（选手、教练、队徽地址），缓存到 `rosters.json`，限速抓取 |
| `server/logos.py` | 队徽缓存：从 Liquipedia 下载队徽到 herodata 卷的 `logos/`，请求间隔 2 秒，失败 6 小时内不重试 |
| `server/roster_card.py` | 对阵图：两队队徽、队名左右对称，中间按职责（坦克 / 输出 / 全能 / 支援 / 教练）列出双方选手，输出 PNG；字体是 `server/assets/fonts` 里的 Noto Sans（OFL） |
| `server/prediction_feed.py` | 竞猜接入推送：竞猜 / 截止 / 作废 / 周榜消息，给提醒和赛果补充竞猜信息（不依赖 discord） |
| `server/prediction_ui.py` | 竞猜按钮（`DynamicItem`，`pred:<key>:<side>`）：点击押注、只回复本人、节流更新票数 |
| `server/regions.py` | 赛区 -> 频道的绑定（`regions.json`），以及 Liquipedia 赛事路径 -> 赛区的识别；赛事推送和开播通知共用 |
| `server/live_watch.py` | 开播通知：手动登记的 B 站直播间（`live_sources.json`）、状态查询、开播/下播判定。不依赖 discord.py |
| `server/patchnotes.py` | 官方补丁说明：抓取 overwatch.blizzard.com 的当月和上月补丁页，解析成整篇文本 + 按英雄分组的改动 |
| `server/patch_feed.py` | 补丁推送：`PatchFeed` 定时检查、用 `PatchSummarizer`（Claude）写摘要并发到频道；也给英雄问答提供最近的官方补丁 |
| `server/watch_rooms.py` | 一起看房间：状态（播放/暂停/位置）、观众、控制权限、房主交接、过期；不依赖网络和 Discord |
| `server/watch_sources.py` | 一起看视频源：解析 B 站 / YouTube 链接，取 B 站 DASH 流地址（buvid3、WBI 签名）和 YouTube 信息 |
| `server/vod_search.py` | 一起看按片名搜索：解析 `WATCH_VOD_SOURCES`，并发查询采集接口（`?ac=detail&wd=`），只留 https 的 `.m3u8` 片源，多个接口按「片名+年份」合并、按贴近程度排序，最多 10 条 |
| `server/vod_playlist.py` | 一起看 vod 播放列表：取 m3u8（master 跟进一层最高码率）、改写成全绝对 https 地址（密钥地址改成本服务器的 `key{n}`，原地址记在 `Playlist.keys`，`fetch_key` 取）、去掉片中插入的广告、试播探测（带 Origin 的 Range 请求，m3u8 和片段的跳转链也只认 https）、按线路找第一条能播的；`WatchServer.prepare_vod` 给整个过程加 45 秒总时限（`VOD_PREPARE_TIMEOUT`），超时按「这一集暂时没有能播的片源」处理 |
| `server/watch_progress.py` | 一起看按频道的观看进度（`STATE_DIR/watch_progress.json`）：只记 vod 房间，少于 30 秒不记，房间里没人时不记（最后一个人宽限期结束时记最后一次），坏文件按空处理；生成「上次一起看到…」文案 |
| `server/watch_server.py` | 一起看 HTTP / WebSocket 服务（aiohttp）：房间页、静态文件、同步协议、B 站转流和 MPD |
| `server/watch_ui.py` | 一起看的 Discord 部分：房间卡片（只有公开链接）、房主链接文案（bot 私信或 ephemeral 发给发起人）、搜索选单（`DynamicItem`）、人数更新 |
| `server/watch_web/` | 一起看网页：`index.html`、`player.js`（播放器、同步、权限面板）、`style.css`。vod 房间只要浏览器有 MSE 就用 hls.js，不信 `canPlayType` 的原生 HLS（Android Chrome 等常以「格式不支持」失败）；原生只给没有 MSE 的 iPhone Safari 或 hls.js 脚本加载不了时用。房主的播放器在 vod 线路上失败时自动发 `next_line`（每集每条线路一次，`autoSwitch`），因为服务器的探测代表不了房主所在网络 |
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

## 一起看 WebSocket 协议

一起看的网页服务（`WATCH_PORT`，默认 8080）和上面的客户端协议是两套，互不相通。所有消息都是 JSON 文本帧。

**HTTP 路由**

| 路由 | 说明 |
|---|---|
| `GET /r/{room_id}` | 房间页（`index.html`）；房主链接带 `?k=<密钥>`，页面连上后会把它从地址栏去掉。房间不存在返回「房间不存在或已经结束」 |
| `GET /static/{name}` | `player.js`、`style.css` 等静态文件 |
| `GET /ws/{room_id}` | WebSocket，协议见下 |
| `GET /bili/{room_id}/{version}/manifest.mpd` | B 站视频的 DASH 清单，`BaseURL` 指向下面的转流地址 |
| `GET /bili/{room_id}/{version}/{video\|audio}` | B 站视频流、音频流转发（支持 Range）。`version` 随换视频递增，旧地址失效 |
| `GET /vod/{room_id}/{version}/index.m3u8` | 按片名搜索的剧集播放列表：本服务器只取当前集、当前片源线路的 m3u8，改写后返回（去广告，所有 URI 变成绝对 https；`#EXT-X-KEY` 的 URI 改成下面的 `key{n}`）。片段由浏览器直接从资源站 CDN 取。`version` 随换视频递增，旧地址失效 |
| `GET /vod/{room_id}/{version}/key{n}` | 播放列表里第 n 把 AES 密钥（16 字节）：本服务器向播放列表里记下的原地址取一次后缓存，`application/octet-stream`。中转的原因是有些资源站的密钥域名在部分地区的 CDN 边缘节点直接拒绝连接（实测上海电信/联通），浏览器拿不到密钥整集就播不了；片段域名通常不受影响。取不到 → 502，正文是原因 |

页面用 `localStorage["watch_viewer_id:<room_id>"]`（每个房间一个）保存 `viewer_id`，断线 2 秒后自动重连并带上同一个 `viewer_id`。`viewer_id` 是重连凭据，服务器只在 `welcome` 里发给本人；别人看到的是每个成员的公开 `id`（`members` 列表、`grant.target` 用它）。

**客户端 → 服务器**

| type | 字段 | 说明 |
|---|---|---|
| `hello` | `viewer_id`（或 null）, `key`（房主密钥或 null） | 握手 |
| `ping` | `t`（`performance.now()`） | 连上后每 10 秒一次，用来估算时钟偏差 |
| `control` | `action`（`play` / `pause` / `seek`）, `position` | 播放控制，只有房主和被授权的人有效；被拒绝时服务器只给发送者回一条 `state`，让它撤回乐观更新 |
| `duration` | `value` | YouTube 播放器加载后上报一次视频时长；只接受房主和有控制权的人，且只在时长未知时 |
| `grant` | `target`（成员的公开 `id`）, `allowed` | 房主开关某人的控制权限 |
| `change_video` | `url` | 房主换视频 |
| `episode` | `index`（从 0 开始） | 房主选集（vod 房间）：停在开头、暂停，优先用当前片源线路。当前视频不是 vod 时忽略 |
| `next_line` | 无 | 房主换一个片源（vod 房间）：同一集、下一条线路（绕圈），保持当前位置和播放/暂停状态 |
| `resume` | 无 | 房主或有控制权的人（vod 房间）：跳到建房时的进度快照（需要时先换集），暂停 |
| `report` | `detail`, `url`, `env` | 网页端播放失败的回报（stage 上显示的那句原因，如「视频加载失败：片段 HTTP 403 · cdn 域名 [fragLoadError]」；`env` 是播放器模式加浏览器 UA，如 `hls.js 1.5.20; Mozilla/5.0 …`）。服务器只记一条 WARNING 日志（房间号、成员公开 id、当前视频地址、env、连接的 User-Agent），不改房间状态；每个连接最多 5 条 |

**服务器 → 客户端**

| type | 字段 | 说明 |
|---|---|---|
| `welcome` | `viewer_id`, `name`, `is_host`, `can_control` | 握手回复 |
| `pong` | `t`, `server_time` | 回复 `ping` |
| `state` | `video`（`source`, `id`, `title`, `duration`, `version`, `mpd`, `hls`, `episode`, `episode_count`, `line`, `line_count`, `show_title`）, `playing`, `position`, `server_time`, `resume` | 房间状态；`mpd` 只对 B 站有值。`hls` 只对 vod 房间有值（播放列表地址），`episode` 从 0 开始，`line` 是当前片源线路的序号（从 0 开始；网页只显示「来源N」，不给线路名），`line_count` 是线路数，`show_title` 是剧名。`resume` 是建房时的进度快照（`episode`, `position`, `text`），没有进度或不是 vod 时为 null，`text` 就是「上次一起看到…」文案 |
| `you` | `is_host`, `can_control` | 自己的身份或权限变了 |
| `members` | `members`（`id`（公开 id，不是 `viewer_id`）, `name`, `can_control`） | 只发给房主，不含房主自己 |
| `error` | `message` | 提示信息，如 `正在换视频，请稍候`（上一次 `change_video` 还没处理完时） |
| `ended` | 无 | 房间已结束 |

同步：客户端取最近 5 次 ping 里往返最小的一次，`offset = server_time + rtt/2 − 本地时间`；期望位置 = `position + (playing ? 现在的服务器时间 − server_time : 0)`。每秒比较一次，差距超过 2 秒直接跳转，0.3–2 秒微调播放速度。只有用户亲手操作才发 `control`，服务器下发状态引起的播放器事件不回传。

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
4. **分赛区**：`regions.region_candidates` 把赛事路径里的段按 `REGION_ALIASES` 映射成赛区列表，具体的在前（`…/Asia/Stage 3/Korea/…` → `['korea', 'asia']`；没有赛区的总决赛、季中赛 → `['world']`，世界杯 → `['owwc', 'world']`）。`plan()` 收到赛区 -> 频道的绑定后，每场比赛取列表里第一个绑定了频道的赛区作为 `Post.channel`，都没绑就是 `None`（默认频道）。预告按目标频道分组，每个频道一份只含自己比赛的预告，只有最后一条记 `digest_date`。新闻只发默认频道。绑定用 `@mrmeeseeks region <赛区>` 写进 `STATE_DIR/regions.json`。

5. **改期去重**：比赛 ID 是 `赛事路径|开始时间|队1|队2`，Liquipedia 改开赛时间会生成新 ID。`already()` 在 `reminded` / `resulted` 里找同一赛事、同样两队（`match_base`）且开始时间相差不到 `SAME_MATCH_WINDOW`（6 小时）的记录，找到就视为同一场，不再重复提醒或发赛果。旧记录的 ID 如果仍在当前比赛列表里，说明那是另一场（比如季后赛几小时后的重赛），不算改期。
6. **测试入口**：`@mrmeeseeks esports preview` 调用纯函数 `preview_posts`，只取会路由到当前频道的比赛，立刻发预告、下一场提醒、最近一场赛果的样例；不记账，不影响自动推送的去重。

已知限制：Liquipedia 汇总页只有最近约 50 场即将进行和 50 场已结束的比赛；官方首页只放 1–2 条精选新闻；同一赛段里同样两队 6 小时内重赛、且前一场已经不在 Liquipedia 汇总页上时，第二场会被改期去重吞掉。

## 赛事竞猜

开了竞猜时，`EsportsFeed.tick` 在 `plan()` 之后调用 `PredictionFeed.augment`：

1. **快照**：`snapshot()` 从 `PredictionStore` 读出每场比赛已登记的竞猜（`find`：ID 相同，或者同一赛事同样两队、开始时间相差不到 6 小时并且旧 ID 已经不在汇总页上——和改期去重同一规则）、票数、赛果揭晓的预览（`score_preview`，只读）、阵容缓存和上周榜。
2. **计划**：纯函数 `plan_predictions` 产出 `kind` 为 `prediction`（悉尼 10:00 之后、未来 24 小时、离开赛至少 15 分钟、不是 TBD 对 TBD、还没登记）、`lock`（到开赛时间）、`void`（开赛 48 小时后还没结算）的消息，以及周一 10:00 的周榜；`decorate` 给提醒加上票数和竞猜链接，给赛果加上剧透揭晓并标记"发出后结算"（平局或没有胜负就作废）。
3. **执行**：`execute` 发带按钮的竞猜消息（发送函数返回消息 ID 和链接后 `register`）；截止时先公布名单、再 `lock`、再把原消息按钮改成禁用；作废时编辑原消息。发送失败照常停在这一轮，下一轮重试；编辑失败只记日志。`after_sent` 在赛果发出后 `resolve`。
4. **按钮**：`prediction_ui.PredictButton` 是 `DynamicItem`，`setup_hook` 里 `add_dynamic_items` 一次，重启前发的竞猜消息也能点；点击后 `pick`、ephemeral 回复、每 2 秒最多编辑一次票数。
5. **阵容**：`RosterCache.ensure` 在每次抓到比赛后放到后台跑（Liquipedia `action=parse` 每 30 秒最多 1 次，由 `esports.parse_page` 和赛程抓取共用同一个限速；同一页面 24 小时内不重抓）；阵容抓完后再下载双方队徽（`LogoCache.ensure`）。发竞猜消息时只读缓存，用 `roster_card.render_card` 画对阵图，消息文字只有标题和开赛时间；拿不到阵容的一方在图里写 "Roster unavailable"，没有队徽就画首字母圆标；画图失败时退回文字版阵容。

计分：猜中 `1 + max(0, (0.5 − 押中一方占比) × 4)`，上限 3，参与少于 3 人没有加成。

## 开播通知

`live_watch.LiveWatcher` 每 `LIVE_POLL_INTERVAL` 秒（默认 30，最低 15）跑一次 `poll_once`：把所有登记的直播间分批（每批 20 个）发给 B 站公开的 `getRoomBaseInfo` 接口（不需要登录，标准库 urllib 跑在线程里），再用纯函数 `detect_transitions` 和上一轮比较，产出 `LiveEvent("live" | "offline")`，交给 `MeeseeksBot._live_notify`：按直播源的赛区在 `RegionStore` 里找频道，发一条带标题、封面的嵌入消息（不 ping 任何人）。

- 第一次看到某个源只记基线，不发事件；`live add` 时也把当前状态写进基线，所以新加一个正在直播的源不会立刻刷屏
- 查询失败（412 限流、断网）只记日志并保留上一轮状态，`live list` 会显示最近一次错误
- 赛区没绑频道时事件只记日志；`live add` 回复里会提醒先 `region <赛区>`
- 直播源存在 `STATE_DIR/live_sources.json`，按 `platform:room_id` 去重；短号在添加时用 `room_init` 接口解析成真实房间号

接其他平台（Twitch、YouTube、CHZZK）：`Source.platform` 已预留；需要一个类似 `BilibiliClient.statuses` 的查询器，返回 `dict[room_id, RoomStatus]`，再在 `LiveWatcher.poll_once` 里按 platform 分发。Twitch 的 EventSub 可以做成推送而不是轮询。

## 补丁推送

英雄问答原来只从 Wiki 拿补丁历史，但 Wiki 的英雄页常常要过几天才收录新补丁。现在直接读官方补丁说明：

1. **抓取**：`PatchFeed.run` 每 3 小时抓一次 `overwatch.blizzard.com/en-us/news/patch-notes/`（只显示当月）和上个月的归档页 `/live/YYYY/MM/`，合并后保留最近 8 个补丁。每个补丁按页面上的锚点 `patch-YYYY-MM-DD` 去重。当月页有内容却解析不出补丁时抛 `PatchNotesUnavailable`（多半是官网改版），失败时保留上一次的数据，10 分钟后重试。
2. **三语名称**：`fetch_translations` 抓韩服（`/ko-kr/.../live/YYYY/MM/`）和国服（`ow.blizzard.cn/news/patch-notes/live/YYYY/MM/`，按北京时间可能晚一天，没有锚点，从标题取日期）里的同一篇补丁。解析时每个英雄条目记下图标和其中的技能/威能名：韩服和美服图标相同，按图标对齐；国服图标换了地址，只在英雄条目数一致时按顺序对齐，单个条目里技能数对不上就跳过。威能名去掉「– Minor Perk / ——次级威能 / - 보조 특전」后缀再对照。对不上的语言留空，国服缺失时英雄中文名退回 `aliases.json`。
3. **发送**：新补丁用 `PatchSummarizer` 写成要点式摘要：内容英文，英雄/武器/技能/威能名按对照表写成「英文/中文/韩文」，模型只写 `[[Hero]]`/`[[Hero|Ability]]` 标签，由 `fill_names` 填入官方名称。每个英雄下固定 Basic Info / Abilities / Perks 三项：解析时按页面结构记下每个条目是技能（带图标的技能块）还是威能（英雄通用更新里的小标题，官方有时不写 "– Minor Perk"），`normalize_sections` 补齐缺的项（None）、统一顺序，并把模型放错大类的条目挪回去。英雄名前的头像是 bot 的应用表情（`hero_emojis.py`，第一次遇到该英雄时把补丁页上的官方图标上传成 `ow_<英雄>`，之后复用；连续失败 3 次就不再上传）。技能名后面标出 PC 默认键位（`keybinds.py`）：依次查 `keybinds.json` 手动标注、补丁原文写明的新技能键位（"New ability assigned to Secondary Fire by default"）、官方技能名里的 "– Primary/Secondary Fire"、Wiki 英雄数据的键位（没写时武器按 LMB、终极技能 Q、被动 Passive），都没有就不标；威能不标。描述里提到的技能也要打标签：给模型的名单里附上英雄的其余技能（Wiki），描述里的技能有译名才写三语，否则只写英文加键位。模型原文和名称对照表一起缓存，发送时才填名称、键位和头像。超过 2000 字时按行拆成多条。摘要生成一次就缓存（键带格式版本 `SUMMARY_FORMAT`，改格式后旧缓存自动作废）；生成失败时只列出改动的英雄。发送成功后才记账，失败的下次重试。首次启动只记下已有的补丁，不补发；没有设置频道时照常记账、不发送。`patches here` 设置频道时立刻发一次最新的补丁。状态（频道、已发补丁、最近的补丁全文、摘要缓存）保存在英雄数据缓存同一目录的 `patch_state.json`。
4. **问答**：`get_official_patch_notes` 工具按英雄返回最近几次官方补丁里该英雄的改动（官方写法如 `Lúcio` 通过 `HeroStore.resolve` 和 Wiki 名对齐）；不带英雄时返回最新补丁全文。提示词要求比 Wiki 新的改动以官方为准。

## 扩展点

`Analyzer.analyze` 里的 `context` 字典就是接入更多事实数据的地方，目前三项都是"暂未接入"：

- `队友生涯数据`：按计分板上的名字调用 OverFast API（https://overfast-api.tekrop.fr/）搜索玩家，取各英雄游戏时长和胜率；重名或生涯私密时标注"无法确认"
- `地图分段对照`：每张地图每一段的关键位置名称，建议做成一个手动维护的 JSON 文件
- `版本数据`：`herodata.HeroStore` 已有当前版本的英雄数值和补丁，可以直接作为 `patch_data` 接入

新增 Discord 指令：在 `MeeseeksBot.on_message` 里按 `cmd` 分发。
