# 一起看：管理后台、「没有我想看的」、地区限制：设计

日期：2026-10-25

建立在「一起看」（`2026-10-23-watch-together-design.md`）和「按片名搜索」（`2026-10-24-watch-search-design.md`）之上。

## 目标

管理员想知道一起看现在的情况：有多少房间、在播什么、每个房间几个人；想知道大家想看却没找到的片子是什么；因为视频服务跑在同一台单机上，还想看服务器资源用了多少。另外，房间暂时只对澳大利亚 / 新西兰的 IP 开放，其他地区的人进来能看到房间和自己的编号但看不了，被拦的要记下来给管理员看。

## 用户确认的决策

- **dashboard**：一个网页，显示当前房间数、每个房间在播什么、每个房间多少人。
- **「没有我想看的」按钮**：加在一起看功能上，每个用户每天最多提 5 个；dashboard 上能看到哪些剧集名称被标了。
- **没有搜索结果时**也要有一个按钮「我正在寻找这一部影视资源」。
- **服务器资源用量**：单一服务器上跑着视频服务，dashboard 要能统计。
- **地区限制**：房间暂时屏蔽澳大利亚 / 新西兰之外的 IP。不属于这两个地区的用户进来能看到 id，但无法观看。屏蔽后记录被屏蔽的 IP 和 id，并判断可能的地区，dashboard 上有一块显示。

## 我做的决定（用户可以改）

- **后台放在一起看的网页服务里**（`/admin`），不另开端口；Caddy 已经把 8080 代理出去了。用户要求现阶段只需在页面上输入密钥和密码：`/admin` 没登录时是登录页，`.env` 里 `WATCH_ADMIN_KEY`（没配就自动生成存到 `STATE_DIR/watch_admin_key`）和 `WATCH_ADMIN_PASSWORD`（没配就只要密钥）。这两个值不写进代码（仓库公开）。登录成功写 `HttpOnly` cookie，值是 sha256(密钥
密码) 的令牌，不是密钥本身；同一 IP 连续错 10 次锁 15 分钟。用户最初提过的「Discord 指令拿链接」没有做：剧名可能就叫 dashboard，会和按片名搜索冲突。后台 JSON 里不含房主密钥、`viewer_id` 和后台密钥。
- **频道名**：网页服务没有 Discord 连接，bot 注入一个 `channel_name(channel_id)` 回调（读 `get_channel` 缓存，不发请求）。
- **「没有我想看的」谁都能点**，不只发起人：别人也可能想看同一部，多一条记录就是多一份需求信号。按钮是 `DynamicItem`，片名直接放在 `custom_id` 里（`watch:req:miss:<片名>` / `watch:req:want:<片名>`，片名 ≤ 50 字符，和搜索词上限一致），所以选单过期（2 分钟）后按钮留着，bot 重启后也能点。「没找到」的回复里是 `want` 按钮，同样长期有效。
- **限额**：两种按钮共用每人每天 5 次（`WATCH_REQUEST_DAILY_LIMIT`），悉尼 0 点重置，和许愿一致。同一个人同一天重复提同一部（规范化后相同）提示「已经提过」，不扣次数。回复都只有本人可见。记录最多保留 2000 条。
- **后台里按片名合并**：规范化规则复用搜索的 `normalize_title`（去空白、NFKC、小写），显示第一次提交的写法、次数、两种情况各几次、谁提的、哪个频道、最近一次；页面可切今天 / 7 天 / 30 天 / 全部。
- **资源用量只读 `/proc` 和 cgroup**，不加依赖、不要权限（容器是 `read_only`、`cap_drop ALL`）。容器里 `/proc/stat`、`/proc/meminfo`、`/proc/loadavg` 是宿主机的，所以 CPU、内存、负载是整台服务器的；`/proc/net/dev` 是容器自己的网卡，正好是 bot 的流量（B 站转流都在里面）；cgroup 的 `memory.current/max` 对应 `compose.yml` 的 `mem_limit`；再加 bot 进程的 RSS 和 CPU、`STATE_DIR` 所在磁盘的用量。CPU 和网速是两次采样之差，服务器每 5 秒（随房间 tick）采一次，后台请求最多每 2 秒重采一次。
- **地区判断用五大 RIR 的公开分配表**（`delegated-*-extended-latest`）：免费、不用账号、不依赖第三方 API，合计约 46 MB，解析后合并相邻同国家的段存成 `geoip.json`（herodata 卷），v4 用 `array('Q')` 二分查找。每 7 天刷新，任一文件下载失败就保留旧数据。缺点是只到国家级、按「分给了哪国机构」判断，VPN 和跨国运营商会判错，用户接受。
- **数据没加载好时不拦**（fail-open）：第一次部署要先下载一两分钟，这期间拦所有人会让房间不可用；后台会显示「地区数据还没加载」。不在任何分配表里的 IP 不放行。内网 / 本机地址算 `LOCAL` 放行，否则代理没传真实 IP 时会把所有人拦掉。
- **拦在 WebSocket 和媒体路由上，不拦房间页**：用户能打开页面、看到房间号和自己的访客编号（6 位，按 IP 固定），页面显示「你所在的地区暂时不能观看（目前只对澳大利亚、新西兰开放）」，不重连；服务器只发一条 `blocked` 就关连接，不进房、不算人数、不发房间状态（所以 YouTube 的视频 id 也拿不到）。`/bili/`、`/vod/` 对不放行的 IP 返回 403。
- **真实 IP**：对端是本机 / 内网（反向代理）且带 `X-Forwarded-For` 时取最后一段（Caddy 追加的），前面的可能是客户端伪造的；直连就用对端地址。
- **被拦记录按 IP 一条**：编号、推测国家、最近去过的 5 个房间、次数、首末时间，最多 500 条，存 `STATE_DIR/watch_blocked.json`。页面自动重连会刷记录，所以 `blocked` 之后页面不重连，服务器端同一 IP 只累加次数。
- **配置**：`WATCH_ALLOWED_COUNTRIES` 默认 `AU,NZ`，设成空字符串关闭地区限制。

## 实现

`server/watch_requests.py`（记录与限额）、`server/geoip.py`（分配表、查国家、`client_ip`、`BlockLog`）、`server/sysstats.py`（资源采样）；`watch_server.py` 加 `/admin`、`/admin/api`、拦截点和 `admin_snapshot()`；`watch_ui.py` 加 `RequestButton` 和 `on_request`；`watch_web/admin.html`、`admin.js` 是后台页面；`player.js` 处理 `blocked`。测试：`tests/test_watch_requests.py`、`test_geoip.py`、`test_sysstats.py`，以及 `test_watch_server.py`、`test_watch_bot.py` 里的新用例。
