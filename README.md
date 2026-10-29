# mrmeeseeks

守望先锋换英雄参谋。游戏里按住 Tab 再松开，mrmeeseeks 会截下计分板和顶部进度条，用 Claude 识别双方阵容和战局位置，然后在 Discord 频道里给出换英雄建议。

这份 README 就是完整的**启动指南**：从零开始，按顺序做完下面的步骤，就能把它跑起来。

- [它是怎么工作的](#它是怎么工作的)
- [准备工作](#准备工作)
- [第 1 步：获取代码](#第-1-步获取代码)
- [第 2 步：创建 Discord bot](#第-2-步创建-discord-bot)
- [第 3 步：获取 Claude API Key](#第-3-步获取-claude-api-key)
- [第 4 步：启动服务器端](#第-4-步启动服务器端)
- [第 5 步：启动本地客户端](#第-5-步启动本地客户端)
- [第 6 步：分三个阶段验证](#第-6-步分三个阶段验证)
- [可选：部署到云服务器](#可选部署到云服务器)
- [配置参考](#配置参考)
- [运行测试](#运行测试)
- [常见问题](#常见问题)
- [已知限制与路线图](#已知限制与路线图)

---

## 它是怎么工作的

项目分成两部分：

```
你的游戏电脑（Windows）                服务器（可以是同一台电脑，也可以在云上）
┌──────────────────┐   WebSocket   ┌─────────────────────────────┐
│ client/client.py │ ────────────▶ │ server/bot.py               │
│ · 监听 Tab 键    │  握手 / 配对  │ · WebSocket 端口（默认 8765）│ ──▶ Discord 频道
│ · 两段截图       │  上传截图     │ · Discord bot               │
└──────────────────┘ ◀──────────── │ · 调用 Claude 分析           │
                       分析结论    └─────────────────────────────┘
```

- **客户端**只能跑在打游戏的电脑上，因为它要监听键盘、截屏。它不保存任何密钥。
- **服务器端**持有 Discord Token 和 Claude API Key，可以跑在任何地方。

**配对流程**：客户端启动后连上服务器，拿到一个标识（如 `MEE-7K3Q`）。在 Discord 频道里 `@mrmeeseeks`，从下拉菜单选择这个标识，bot 会**私下**发给你一个 8 位配对码。在客户端窗口输入配对码，客户端就绑定到这个频道了。之后每次按住 Tab 再松开，分析结果都会发到这个频道。

**分析流程**：服务器先在本地识别截图，不调用大模型：英雄头像用模板匹配，昵称、地图、攻防用 OCR，推车进度看进度条像素，大约 1–2 秒。认不出的头像标为未知（绝不猜），并在频道里弹出菜单让你标注，标完立刻生效。然后把识别结果（纯文本）交给 Claude 生成建议，每条建议都要注明依据。如果地图、阶段和双方英雄都和上次一样，就不再调用 Claude，直接提示「局面没变」。设计见 [docs/superpowers/specs/2026-10-02-cv-recognition-design.md](docs/superpowers/specs/2026-10-02-cv-recognition-design.md)。

仓库结构：

```
mrmeeseeks/
├── README.md              本指南
├── server/                服务器端
│   ├── bot.py             入口：Discord bot + WebSocket 服务
│   ├── ws_server.py       WebSocket 握手、配对、收截图
│   ├── registry.py        在线客户端与配对状态
│   ├── analyzer.py        调用 Claude 识别和给建议
│   ├── requirements.txt
│   └── .env.example       配置模板
├── client/                本地客户端
│   ├── client.py
│   └── requirements.txt
├── tests/                 测试（不需要 Discord 和 API Key）
├── deploy/                云部署示例（systemd、Caddy）
└── docs/ARCHITECTURE.md   架构与通信协议（给开发者）
```

---

## 准备工作

| 需要 | 说明 |
|---|---|
| Python 3.10 或更高 | 服务器和客户端都需要。Windows 安装时勾选 "Add python.exe to PATH" |
| Git | 用来下载代码（也可以直接下载 zip） |
| 一个 Discord 服务器 | 你需要有"管理服务器"权限，才能把 bot 拉进去 |
| Claude API Key | 第一阶段调试可以先不填，见[第 3 步](#第-3-步获取-claude-api-key) |
| 运行守望先锋的 Windows 电脑 | 客户端跑在这里 |

检查 Python 版本：

```bash
python --version
```

---

## 第 1 步：获取代码

```bash
git clone https://github.com/<你的用户名>/mrmeeseeks.git
cd mrmeeseeks
```

没有 Git 的话，在 GitHub 页面点 **Code → Download ZIP**，解压后进入目录。

> 服务器和客户端都在同一台电脑上时，只需要下载一份。分开部署时，服务器和游戏电脑各放一份。

---

## 第 2 步：创建 Discord bot

1. 打开 [Discord 开发者后台](https://discord.com/developers/applications)，点 **New Application**，名字填 `mrmeeseeks`。
2. 左侧进入 **Bot** 页面：
   - 点 **Reset Token**，复制得到的 Token，下一步要用。**Token 相当于密码，不要发给任何人，也不要提交到 Git。**
   - 往下找到 **Privileged Gateway Intents**，打开 **Message Content Intent**，保存。
3. 左侧进入 **OAuth2 → URL Generator**：
   - **Scopes** 勾选 `bot`
   - **Bot Permissions** 勾选：`View Channels`、`Send Messages`、`Attach Files`、`Embed Links`、`Read Message History`
4. 复制页面底部生成的链接，在浏览器里打开，选择你的 Discord 服务器，授权。

完成后 bot 会出现在服务器成员列表里，状态是离线，等第 4 步启动后才会上线。

---

## 第 3 步：获取 Claude API Key

1. 登录 [Claude Console](https://platform.claude.com)，创建一个 API Key。详细步骤见[官方文档](https://platform.claude.com/docs/en/get-api-key)。
2. 复制保存好这个 Key。

这一步可以先跳过。不填 API Key 时，bot 只识别截图、不给建议，正好用来调试截图时机（见[阶段 B](#阶段-b测试截图时机不需要-api-key)）。

---

## 第 4 步：启动服务器端

在项目根目录执行：

**Windows（PowerShell）**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r server\requirements.txt
copy server\.env.example server\.env
notepad server\.env
```

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r server/requirements.txt
cp server/.env.example server/.env
nano server/.env
```

在 `server/.env` 里填入：

```ini
DISCORD_TOKEN=第 2 步拿到的 Token
ANTHROPIC_API_KEY=第 3 步拿到的 Key（可以先留空）
```

启动：

```bash
cd server
python bot.py
```

看到这两行就说明启动成功：

```
... INFO mrmeeseeks.ws: WebSocket server listening on 0.0.0.0:8765
... INFO mrmeeseeks: Logged in to Discord as mrmeeseeks#1234
```

这时去 Discord 看，bot 应该已经上线。**这个窗口保持开着**，关掉 bot 就下线了。

---

## 第 5 步：启动本地客户端

在打游戏的 Windows 电脑上，**另开一个** PowerShell 窗口，进入项目根目录：

```powershell
# 如果服务器也在这台电脑上，可以复用第 4 步的 .venv，直接激活即可
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r client\requirements.txt
```

启动客户端：

```powershell
# 服务器在同一台电脑上
python client\client.py

# 服务器在别的机器上（把 IP 换成服务器的地址）
python client\client.py --server ws://192.168.1.10:8765
```

看到类似下面的输出就说明连接成功，记下这个标识（Client ID）：

```
[20:15:02] ============================================
[20:15:02] Connected to server. Client ID: MEE-7K3Q
[20:15:02] @mrmeeseeks in a Discord channel and pick this ID.
[20:15:02] ============================================
```

---

## 第 6 步：分三个阶段验证

建议按顺序来，每一阶段只多一个变量，出问题容易定位。

### 阶段 A：只测配对

客户端加 `--no-capture` 启动，不监听键盘：

```powershell
python client\client.py --no-capture
```

1. 在 Discord 任意文字频道发送 `@mrmeeseeks`
2. bot 回复一个下拉菜单，选择你的标识 `MEE-XXXX`
3. 你会收到一条**只有你能看到**的消息，里面是 8 位配对码
4. 客户端窗口出现 "Enter the 8-digit pairing code"，输入它
5. 客户端显示 "✅ Paired!"，频道里出现 "✅ Client MEE-XXXX (...) connected."

也可以顺便试试 `@mrmeeseeks status` 和 `@mrmeeseeks disconnect`。

### 阶段 B：测试截图时机（不需要 API Key）

1. 确认 `server/.env` 里 `ANTHROPIC_API_KEY` 为空，重启服务器
2. 客户端**去掉** `--no-capture` 重新启动，再按阶段 A 的方法配对一次
3. 守望先锋的显示模式设为**无边框窗口**（独占全屏下可能截到黑屏）
4. 进一局游戏，按住 Tab 一秒左右再松开
5. 频道里会收到两张图：
   - `scoreboard.jpg`：应该是完整显示的计分板
   - `hud.jpg`：应该是屏幕顶部，计分板已经消失，能看到目标进度条

如果截图时机不对，用这些参数调整（见[配置参考](#客户端参数)）：

| 现象 | 调整 |
|---|---|
| 计分板没完全弹出来 | 调大 `--score-delay`，如 `0.3` |
| 进度条图里还残留计分板 | 调大 `--hud-delay`，如 `0.6` |
| 进度条被截掉一部分 | 调大 `--hud-ratio`，如 `0.25` |
| 有多个显示器，截错了屏幕 | 改 `--monitor 2` |

### 阶段 C：完整分析

1. 在 `server/.env` 里填上 `ANTHROPIC_API_KEY`，重启服务器
2. 客户端重新配对（服务器重启后所有客户端都要重新配对）
3. 游戏里按住 Tab 再松开

频道里会先出现截图，几秒后 bot 回复：

```
🔍 Neon Junction · Hybrid · Attack · checkpoint 1 (~26%)
Allies: D.Va, Vendetta, Venture, Illari, Kiriko
Enemies: Mauga, Pharah, Reaper†, Kiriko, ?

One-sentence conclusion…
- Reason 1 (basis: …)
- Reason 2 (basis: …)
```

前三行是识别结果，用来核对有没有认错：`†` 表示阵亡（英雄按昵称从之前的截图记住），`?` 表示没认出来。没认出来的头像会另外弹出标注菜单。局面没变时只回一行 `⏸️ No change … previous advice still applies`。客户端窗口里也会显示结论。

---

## 可选：部署到云服务器

调通之后，可以把服务器端搬到云上，游戏电脑上只留客户端。有两种方式。

### 方式一：Docker（推荐）

服务器装好 Docker，并且能用 SSH 密钥登录，在本机项目根目录运行：

```bash
deploy/deploy.sh deploy@你的服务器IP        # 默认密钥 ~/.ssh/jobseeker_deploy，可用 DEPLOY_KEY 覆盖
```

脚本会把 `server/`、`Dockerfile`、`compose.yml` 同步到服务器的 `/opt/mrmeeseeks`，然后 `docker compose up -d --build`。第一次运行时会生成 `/opt/mrmeeseeks/.env` 并停下，在服务器上填好 `DISCORD_TOKEN` 等再运行一次即可。之后改了代码，重新运行这个脚本就是更新。

容器把 8765 端口直接暴露到公网（明文 `ws://`），记得在防火墙放行 `8765/tcp`。常用命令（在服务器 `/opt/mrmeeseeks` 目录下）：

```bash
docker compose logs -f --tail 50           # 看日志
docker compose up -d --force-recreate      # 改了 .env 后重启
```

### 方式二：systemd

下面以 Ubuntu 为例。

**1. 放代码并安装依赖**

```bash
sudo useradd -r -m -d /opt/mrmeeseeks meeseeks
sudo -u meeseeks git clone https://github.com/<你的用户名>/mrmeeseeks.git /opt/mrmeeseeks
cd /opt/mrmeeseeks
sudo -u meeseeks python3 -m venv .venv
sudo -u meeseeks .venv/bin/pip install -r server/requirements.txt
sudo -u meeseeks cp server/.env.example server/.env
sudo -u meeseeks nano server/.env
```

**2. 设为系统服务（开机自启、崩溃重启）**

```bash
sudo cp deploy/mrmeeseeks.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mrmeeseeks
journalctl -u mrmeeseeks -f      # 查看日志
```

**3. 让客户端连得上**，两种方式选一种：

- **简单方式（明文）**：在云服务器的防火墙 / 安全组里放行 TCP 8765。客户端用 `--server ws://服务器公网IP:8765`。注意这种方式下截图和配对码都是明文传输。
- **推荐方式（加密）**：准备一个域名解析到服务器，安装 [Caddy](https://caddyserver.com/docs/install)，参考 `deploy/Caddyfile` 配置反向代理，Caddy 会自动申请 HTTPS 证书。然后把 `server/.env` 里的 `WS_HOST` 改成 `127.0.0.1`，放行 80 和 443 端口（不要放行 8765）。客户端用 `--server wss://你的域名`。

---

## 配置参考

### 服务器 `server/.env`

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `DISCORD_TOKEN` | 是 | – | Discord bot Token |
| `ANTHROPIC_API_KEY` | 否 | 空 | 留空则只识别截图，不给建议 |
| `CLAUDE_MODEL` | 否 | `claude-sonnet-5` | 给建议用的模型；想更快可换成 `claude-haiku-4-5-20251001`。可用模型见[模型列表](https://platform.claude.com/docs/en/models/overview) |
| `ADVISE_EFFORT` | 否 | `low` | 给建议时的思考强度（`low`/`medium`/`high`），越高越慢 |
| `STATE_DIR` | 否 | `state/` | 标注的模板、待标注队列、许愿次数（`wishes.json`）、赛区频道绑定（`regions.json`）、直播源（`live_sources.json`）、赛事竞猜（`predictions.db`）和公告的各语言版本（`announcements/`）存放位置；Docker 部署时是挂载的 `/state` |
| `WS_HOST` | 否 | `0.0.0.0` | WebSocket 监听地址；用反向代理时改为 `127.0.0.1` |
| `WS_PORT` | 否 | `8765` | WebSocket 端口 |
| `MIN_SNAPSHOT_INTERVAL` | 否 | `5` | 同一客户端两次分析的最短间隔（秒） |
| `LIVE_POLL_INTERVAL` | 否 | `30` | 开播通知：轮询 B 站直播间状态的间隔（秒），最低 15 |
| `LIVE_NOTIFY_OFFLINE` | 否 | 空 | 设为 `1` 时下播也发一条消息，默认只报开播 |
| `WATCH_PUBLIC_URL` | 否 | 空 | 一起看网页的公网地址，如 `https://watch.pigdogie.com.au`；留空不启用一起看。见下方「一起看」 |
| `WATCH_HOST` | 否 | `0.0.0.0` | 一起看网页的监听地址；用反向代理时改为 `127.0.0.1`（Docker 部署已在 `compose.yml` 里设好） |
| `WATCH_PORT` | 否 | `8080` | 一起看网页的端口 |
| `BILIBILI_SESSDATA` | 否 | 空 | B 站登录 cookie 的 `SESSDATA`，登录后才能拿到高画质。浏览器登录 bilibili.com → F12 → Application → Cookies → `https://www.bilibili.com` → 复制 `SESSDATA` 的值。它等同于账号登录凭证，务必保密，不要发给别人或提交到仓库 |
| `WATCH_MAX_QUALITY` | 否 | `1080` | B 站视频的画质上限，可选 `1080`、`720`、`480` |
| `WATCH_MAX_BILI_ROOMS` | 否 | `2` | 同时存在的 B 站房间上限（服务器转流，占出口带宽）；YouTube 房间不限 |
| `WATCH_VOD_SOURCES` | 否 | 见说明 | 按片名搜索用的资源站（采集接口），格式 `名字=接口地址`，逗号分隔，靠前的优先。不设置时用默认的 `创享影视`、`1080资源`、`量子` 三个；设成空字符串则关闭按片名搜索（仍可贴 B 站 / YouTube 链接）。见下方「按片名搜索」 |
| `WATCH_TV_LISTS` | 否 | 见说明 | 电视直播用的公开 IPTV 列表（m3u 地址），逗号分隔，靠前的优先。不设置时用默认的三个：`https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/ipv4/result.m3u`、`https://raw.githubusercontent.com/iptv-org/iptv/master/streams/cn.m3u`、`https://raw.githubusercontent.com/suxuang/myIPTV/main/ipv4.m3u`；设成空字符串则关闭电视直播（`一起看 <名字>` 只搜影视）。见下方「电视直播」 |
| `WATCH_MAX_TV_ROOMS` | 否 | `2` | 同时存在的电视直播房间上限（服务器转发，占出口带宽） |
| `HERO_DATA_PATH` | 否 | `server/herodata/heroes.json` | 英雄数据缓存文件。Docker 部署时位于 `herodata` 卷 |
| `HERO_REFRESH_HOURS` | 否 | `24` | 每隔多少小时刷新英雄数据（Overwatch Wiki）和对位数据（counterwatch.gg）；对位数据缓存在同一目录的 `matchups.json` |
| `WISH_CHANNEL_ID` | 否 | 空 | 功能许愿频道的 ID，留空不启用。见下方「功能许愿频道」 |
| `WISH_GAMES` | 否 | 守望先锋 + 22 个热门游戏 | 许愿表单里「哪个游戏」的下拉选项，逗号分隔，最多 24 个；末尾自动加 `Other ｜ 其他` |
| `WISH_DAILY_LIMIT` | 否 | `2` | 每人每天能许几个愿，悉尼时间 0 点重置。改了之后重启，置顶入口消息里的说明会自动更新 |
| `WISH_REVIEWER_ID` | 否 | 空 | 许愿方案的最终审核人（Discord 用户 ID）。方案里会请许愿人 @ 这个人；留空则写「找管理员」 |

### 客户端参数

```powershell
python client\client.py --help
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--server` | `ws://127.0.0.1:8765` | 服务器地址，支持 `ws://` 和 `wss://` |
| `--name` | 电脑名 | 在 Discord 下拉菜单里显示的设备名 |
| `--monitor` | `1` | 截哪个显示器，1 为主显示器 |
| `--score-delay` | `0.15` | 按下 Tab 后等多久截计分板（秒） |
| `--hud-delay` | `0.4` | 松开 Tab 后等多久截进度条（秒） |
| `--hud-ratio` | `0.18` | 进度条截图取屏幕顶部的比例 |
| `--min-hold` | `0.2` | 按住少于这个时长视为误触（秒） |
| `--cooldown` | `5` | 两次发送截图的最短间隔（秒） |
| `--quality` | `85` | JPEG 质量 |
| `--retry` | `5` | 断线后重连间隔（秒） |
| `--no-capture` | – | 只测试连接和配对，不监听 Tab |

### Discord 指令

下表的指令也可以私信 bot 使用，私信里不用写 `@mrmeeseeks`，直接发问题或指令（如 `卡西迪最近被削了吗`、`一起看 湖南卫视`），回复只有自己看得到。只发图片或贴纸时 bot 回帮助；配对要明说 `connect`。给服务器频道做设置的指令（`esports`、`patches`、`region`、`live`、`announce`、`predictions reset`）在私信里会被拒绝。私信里开的一起看房间，卡片发在私信里，把公开链接转给朋友即可；看到第几集按私信单独记。只有和 bot 在同一个服务器的人才能私信它。

| 指令 | 作用 |
|---|---|
| `@mrmeeseeks` 或 `@mrmeeseeks connect` | 连接客户端：选择标识，私下收到 8 位配对码 |
| `@mrmeeseeks status` | 查看所有在线客户端和绑定情况 |
| `@mrmeeseeks analyze` | 用本频道最近一次识别结果重新给一次建议，不用再按 Tab |
| `@mrmeeseeks label` | 拿出待标注的未知头像（一次 5 个），用菜单选「职责 → 英雄」，标完立刻生效 |
| `@mrmeeseeks player Name#1234` | 用 [OverFast](https://overfast-api.tekrop.fr/) 查玩家各职责段位、总体数据和最常玩的 5 个英雄。BattleTag 区分大小写；生涯设为私密时只能看到段位 |
| `@mrmeeseeks disconnect` | 解绑本频道的客户端 |
| `@mrmeeseeks help` | 显示帮助 |
| `@mrmeeseeks wishes [游戏] [7d]` | 管理员（「管理服务器」权限或 `WISH_REVIEWER_ID`）导出许愿 CSV，按 👍 数排序，私信发送。可按游戏（包含匹配，`漫威` 也行）、最近 N 天筛选 |
| `@mrmeeseeks announce` | 管理员（「管理服务器」权限）发多语言公告：用一种语言写，bot 翻译成简体中文、繁體中文、English、日本語、한국어，先私下预览再发布。频道里只有一条原文公告，成员点语言按钮私下看到对应版本。见下方「多语言公告」。需要 `ANTHROPIC_API_KEY` |
| `@mrmeeseeks <问题>` | 英雄问答：数值（血量、子弹体积）、最近的补丁、N 米处几枪击杀。例如 `@mrmeeseeks 卡西迪最近被削了吗`（先查[官方补丁说明](https://overwatch.blizzard.com/en-us/news/patch-notes/)，补丁当天就能答对，再查 Wiki 的历史记录）、`@mrmeeseeks 卡西迪 30 米爆头几枪杀毛加`、`@mrmeeseeks 查莉娅怕谁`。克制关系来自 [counterwatch.gg](https://www.counterwatch.gg) 的对位评分（对决和团战结果，全段位，不是整局胜率）。需要 `ANTHROPIC_API_KEY` |
| `@mrmeeseeks esports here` / `esports off` / `esports status` / `esports preview` | 赛事推送的默认频道：推送 OWCS 和世界杯的每日赛程预告（悉尼时间 10:00）、开赛前 15 分钟提醒、赛果（比分用剧透遮罩）和官方新闻。绑定了赛区频道的比赛会发到赛区频道（见下方「按赛区分频道」），其余和新闻发到这里。`here`/`off` 需要"管理频道"权限；`status` 显示默认频道、赛区频道和接下来几场比赛各发到哪；`preview` 立刻在当前频道发一组用真实数据生成的样例（预告、下一场提醒、最近一场赛果，以及一条按钮禁用的竞猜消息，标着 🧪 Preview），只含会发到这个频道的比赛，不影响自动推送，部署后或绑定赛区后用来快速验证（需要"管理频道"权限）。比赛数据来自 [Liquipedia](https://liquipedia.net/overwatch)（CC-BY-SA） |
| `@mrmeeseeks predictions` / `predictions reset` | 赛事竞猜：每天悉尼时间 10:00 预告之后，每场官方比赛在它的赛区频道（或默认频道）发一条竞猜消息，附一张对阵图（双方队徽和按职责排列的报名阵容、教练），点按钮押胜者，开赛前可以改。开赛时截止并公布谁押了谁，赛果出来后在剧透遮罩里揭晓谁猜中；猜中得 1 分，押冷门猜中最多 3 分。每周一 10:00 在默认频道发上周榜和称号。`predictions` 查看本周榜、赛季榜和自己的排名；`predictions reset` 开新赛季（需要"管理服务器"权限） |
| `@mrmeeseeks region <赛区>` / `region <赛区> off` / `region` | 把当前频道绑定成某个赛区的频道（需要"管理频道"权限）。该赛区的比赛预告、提醒、赛果和开播通知都发到这里。赛区名：`cn`、`asia`、`korea`、`japan`、`pacific`、`emea`、`na`、`world`（总决赛、季中赛）、`owwc`（世界杯）。不带参数列出所有绑定 |
| `@mrmeeseeks live add <赛区> <B 站直播间链接或房间号> [名字]` / `live remove <链接或房间号>` / `live list` | 开播通知：手动登记 B 站直播间，开播时在该赛区的频道里发一条带标题和封面的消息（需要"管理消息"权限）。短号自动解析；不写名字用主播昵称 |
| `@mrmeeseeks 一起看 <B 站或 YouTube 链接、片名 或 台名>`（或 `watch`） | 一起看：开一个同步观看房间，频道里发房间卡片和公开链接，带密钥的房主链接只私下发给发起人。片名会去资源站搜索，见下方「按片名搜索」；台名（如 `东方卫视`、`CCTV1`）开电视直播，见下方「电视直播」。见下方「一起看」。需要设置 `WATCH_PUBLIC_URL` |
| `@mrmeeseeks patches here` / `patches off` / `patches status` | 补丁推送频道：每 3 小时检查一次[官方补丁说明](https://overwatch.blizzard.com/en-us/news/patch-notes/)，有新补丁就发一篇要点式摘要并附原文链接：每个改动的英雄名前有头像，下面固定分 **Basic Info / Abilities / Perks** 三项（没有改动写 None）；改动内容用英文，英雄、武器、技能、威能名写成「英文/简体中文/韩文」（例如 `Roadhog/路霸/로드호그`、`Chain Hook/链钩/사슬 갈고리`），技能名后面标出 PC 默认键位（如 `Shift`），描述里顺带提到的技能也标（来源：补丁原文 > 官方技能名 > Wiki；都没有的写进 `server/keybinds.json` 手动标注，启动日志会列出缺键位的技能），中文取自国服官网、韩文取自韩服官网的同一篇补丁，找不到的语言留空。`here` 设置后会立刻发一次最新的补丁。`here`/`off` 需要"管理频道"权限；摘要需要 `ANTHROPIC_API_KEY`，没有时只列出改动的英雄 |

### 一起看

在频道里发 `@mrmeeseeks 一起看 <B 站或 YouTube 链接>`（也可以写 `watch`），bot 会开一个房间并发一张房间卡片，点链接进网页，大家同步看同一个视频。频道里只有公开链接；带密钥的房主链接（只能用一次）bot 会单独发给发起人：贴链接建房是私信，按片名搜索是只有你能看到的回复。用它进房才是房主；没收到（比如关了私信，bot 会在频道里说明）就用公开链接，最先进房的人是房主。房主可以给别人开放播放、暂停、拖动的权限，也可以换视频；房主离开后控制权会交给其他人。所有人离开 10 分钟后房间结束，卡片显示「已结束」。

- **B 站**：服务器自己取流再转发给大家（浏览器直连 B 站会被防盗链拦住），所以占服务器出口带宽：1080p 每个观众约 3 Mbps，默认最多同时 2 个 B 站房间（`WATCH_MAX_BILI_ROOMS`）。部分视频只限大陆观看，在海外服务器上会提示 `这个视频在海外服务器上无法播放`，换 YouTube 链接或别的视频。
- **YouTube**：观众的浏览器直接播放，不走服务器流量。
- **按片名搜索**（见下方）：视频片段由观众的浏览器直接从资源站的 CDN 加载，服务器只取播放列表（m3u8）并改写，不转发视频流。
- **电视直播**（见下方）：和按片名搜索不同，直播的播放列表和视频分片**全部经服务器转发**，观众的浏览器只连我们的服务器，所以占服务器出口带宽，默认最多同时 2 个直播房间（`WATCH_MAX_TV_ROOMS`）。

#### 按片名搜索

发 `@mrmeeseeks 一起看 绝命毒师`，bot 会按 `WATCH_VOD_SOURCES` 里的资源站并发搜索，把结果做成一个下拉菜单，每项显示「片名 · 年份 · 类型 · 集数」，看过的显示「上次看到第 n 集」。

- 选单只有发起人能用，2 分钟内有效；超时后消息改成「选择已过期」。bot 重启后再点旧选单会提示「已过期，请重新搜索」。
- 选中后消息先改成「正在准备…」，找到能播的片源后开房间（最多等 45 秒，找不到就提示「这一集暂时没有能播的片源」），卡片和房间标题是「片名 · 第 n 集」（只有一集的条目就是片名）。
- 房主打开网页后，🔑 面板里有集数列表（点哪集就从哪集开始，停在开头）和「换一个片源」。有些 CDN 会挡部分地区，某个片源在你这边打不开时，换一个线路即可（同一集，从当前位置接着播，原来在播就继续播）。只有一个片源时这个按钮不可用。
- 一集播完会自动接下一集并开始播放；期间如果有人手动换过集，自动下一集作废。
- 加密（AES-128）片源的密钥由服务器中转，浏览器不直接连资源站的密钥域名：有些资源站的密钥域名在部分地区连不上（实测上海的电信、联通节点直接拒绝连接），而片段域名没事。密钥取不到时画面上会写「密钥 HTTP …」或「密钥取不到…」，服务器日志里是 `watch vod key unavailable`。
- 同一个频道看过的剧会记下进度（存在 `STATE_DIR/watch_progress.json`，不到 30 秒的不记；房间里没人以后不再往前记）。下次在这个频道搜到同一部剧，网页上会显示「上次一起看到 第 n 集 m:ss（M月D日）」，有控制权的人点它就能跳回上次的位置。
- 资源站由第三方提供，内容和可用性都不由 bot 控制；接口出错或返回的不是有效数据时，bot 只提示「资源站暂时无法访问，请稍后再试」。
- **出问题时怎么看原因**：找不到能播的片源时，提示后面会按线路列出原因，如「这一集暂时没有能播的片源（来源1：m3u8 超时（域名）；来源2：片段 HTTP 403（域名））」（用户看到的只有「来源N」，线路名只在服务器日志里）。网页上视频加载失败时，画面上会写明是哪一步、什么错，如「视频加载失败：片段 HTTP 403 · cdn 域名 [fragLoadError]」（`[]` 里是 hls.js 的错误码），这句话同时会记进服务器日志（`watch client playback failed`，带房间号、视频地址、播放器模式和浏览器 UA）；服务器端每条线路跳过的原因也在日志里（`watch vod … skipped`）。

#### 电视直播

发 `@mrmeeseeks 一起看 <台名>`，比如 `一起看 东方卫视`、`一起看 CCTV1`、`一起看 湖南`。台名能完全对上频道表时直接开直播房间（卡片显示「正在播放：**📺 东方卫视**（直播）」）；只模糊对上几个台时（比如 `东方`），最多 5 个台以「📺 东方卫视 · 直播」的样子排在影视搜索结果前面，放进同一个下拉菜单；一个台也对不上就照旧搜影视。查询至少 2 个字。

- **来源**：频道表由 `WATCH_TV_LISTS` 里的公开 IPTV 列表合并而成（同一个台的不同写法会归并，如 `CCTV-1 (1080p)` 和 `CCTV1综合`）。启动时和之后每 6 小时重新下载列表并探测每条地址能不能播，结果存在 `STATE_DIR/tv_channels.json`，重启后先用这份文件。H.265 的地址浏览器多半解不了，探测时就排除。
- **经服务器转发**：这些地址大多是 http（我们的网页是 https，浏览器会拦），而且常常跳转到只认请求方 IP 的地址，所以服务器替大家拉直播的播放列表和分片，几个人要同一个分片只从源头拉一次。服务器只请求列表里的地址和这些地址的播放列表里出现的地址，并拒绝一切内网地址。
- **网页上**：标题旁有红色的「直播」标记，没有进度条。有控制权的人可以暂停，大家一起停；恢复播放时所有人回到最新画面，不从暂停处接着放。直播不记「看到哪了」。
- **换源**：一条地址断流或卡住约 10 秒，服务器自动换下一条能播的，房间里提示「已切换片源」（同一房间 15 秒内最多因卡住换一次）。有些地址播的是「仅限境内观看」之类的占位画面，服务器分辨不出来，这时房主在 🔑 面板里点「换一个源」。只有一条地址时这个按钮不可用。所有地址都不能播时提示「这个台暂时没有能播的源」；直播房间满了提示「直播房间已满（最多 2 个），请稍后再试」。
- 列表和地址都来自第三方，能看哪些台、清晰度如何都不由 bot 控制。

**部署步骤**（先把 `compose.yml` 里的端口和下面的 Caddy 配置备好）：

1. 在域名后台加一条 A 记录，如 `watch.pigdogie.com.au` 指向服务器 IP。
2. 服务器 `.env` 里写 `WATCH_PUBLIC_URL=https://watch.pigdogie.com.au`；要高画质再写 `BILIBILI_SESSDATA`（取法见上表）。
3. 把 `deploy/Caddyfile` 里 `watch.pigdogie.com.au` 那一段加到实际占用 80/443 的 Caddy 里（Caddy 自动申请证书），重载 Caddy。`flush_interval -1` 让视频流边收边发，不要删。
4. 运行 `deploy/deploy.sh`，日志里应该能看到 watch server 的监听信息。

**部署备注**（2026-10-23 在生产服务器上实测）：

- 服务器在新加坡。B 站接口需要 buvid3 和 WBI 签名，代码里已经处理。
- 当前生产机的 80/443 端口被另一个项目的 Caddy 容器占着，所以 `watch.pigdogie.com.au` 的站点块要加到实际服务 80/443 的那个 Caddy 里。如果那个 Caddy 在容器里，它必须能访问到 bot 的 8080 端口，例如加入共用的 docker 网络，或者用宿主机网关地址（此时 `reverse_proxy` 的目标和 `compose.yml` 的端口映射要对应调整）。

### 功能许愿频道

用户在这个频道里只能点按钮、填表单来许愿（先在下拉菜单里选游戏），每人每天 2 次（`WISH_DAILY_LIMIT` 可调），悉尼时间 0 点重置。bot 会把表单发成卡片，加 👍 用于投票，并开一个子区用于讨论。

下拉菜单里是守望先锋和 22 个热门游戏（「英文名 ｜ 中文名」，列表在 `server/wishes.py` 的 `DEFAULT_GAMES`）。小众游戏选最后的 `Other ｜ 其他`，提交后点 bot 私下回复里的按钮填游戏名；10 分钟内没填就按 `Other` 发出。

每条许愿发出后，bot 会参考服务器现有的分类、频道和自己现有的功能，在子区里起草一份方案（需要 `ANTHROPIC_API_KEY`）：要不要为新游戏开分类、建哪些频道、功能放在哪个频道、实现步骤。然后 @ 许愿人 review：

- 想改：在子区里 `@mrmeeseeks <要改什么>`，bot 重写整份方案（每条许愿最多改 5 次；许愿人、审核人和管理员可以要求修改）
- 满意：在子区里 @ 审核人（`WISH_REVIEWER_ID`）做最终审核。bot 只出方案，不会自己建频道
- 方案没生成出来：在子区里 `@mrmeeseeks plan` 重试

每条许愿（表单内容 + 历次方案）存档在 `state/wishes/<消息ID>.json`，频道里的卡片删了也还在。用 `@mrmeeseeks wishes` 导出。

设置步骤：

1. 新建文字频道，例如 `💡｜功能许愿池`
2. 频道权限：
   - `@everyone`：开 **查看频道**、**阅读消息历史**、**添加反应**、**在子区中发送消息**；关 **发送消息**、**创建公开子区**、**创建私密子区**
   - mrmeeseeks：开 **发送消息**、**在子区中发送消息**、**嵌入链接**、**附加文件**、**阅读消息历史**、**创建公开子区**、**添加反应**、**管理消息**（置顶入口消息要用）
3. 打开开发者模式，右键频道 → 复制频道 ID，填进 `.env` 的 `WISH_CHANNEL_ID`，然后重启 bot

bot 启动时会在频道里发一条带「Make a wish」按钮的置顶消息；已经有了就直接复用，被删了会重新发。在这个频道里 @mrmeeseeks 不会触发指令。

### 按赛区分频道

赛事推送默认把所有赛区的比赛发到一个频道。如果想让 OWCS 各赛区各有一个频道，给每个赛区建一个频道，在里面绑定赛区：

```
#owcs-cn   里：@mrmeeseeks region cn
#owcs-asia 里：@mrmeeseeks region asia      # 韩国、日本、太平洋赛区都归这里；想单独分出韩国就再 region korea
#owcs-emea 里：@mrmeeseeks region emea
#owcs-na   里：@mrmeeseeks region na
```

之后每个频道只收本赛区的预告、提醒和赛果（每个频道一份只含自己比赛的预告）。总决赛、季中赛等没有赛区的比赛，以及官方新闻，仍发到 `esports here` 设的默认频道；想单独放就 `region world`。赛区从 Liquipedia 的赛事路径自动识别（如 `Overwatch Champions Series/2026/Asia/Stage 3/Korea/…` → `korea`，退而求其次 `asia`）。

**开播通知**：同一套赛区频道也用来发直播开播提醒。B 站上转播某赛区比赛的直播间可以手动登记：

```
@mrmeeseeks live add cn https://live.bilibili.com/7734200 守望先锋赛事
@mrmeeseeks live add emea 12345 OWCS EMEA 转播
@mrmeeseeks live list
@mrmeeseeks live remove 7734200
```

bot 每 30 秒查一次 B 站公开接口（不需要登录），直播间从未开播变成直播中时，在该赛区的频道里发一条带标题和封面的消息。bot 启动或新加直播间时只记录当前状态，不补发。目前只支持 B 站；OWCS 亚洲、EMEA、北美的官方流在 Twitch / YouTube，暂时只能登记 B 站上的转播间。绑定和直播源存在 `STATE_DIR`（`regions.json`、`live_sources.json`），重启不丢。

### 多语言公告

一份公告只写一次，大家各看各的语言：

1. 在要发公告的频道里 `@mrmeeseeks announce`，点 bot 回复里的 **Write announcement**（只有发指令的人能点）
2. 表单里选原文语言，填标题（选填）和正文（最多 4000 字，Discord 格式如粗体、列表、emoji 会保留）
3. bot 把原文翻译成另外 4 种语言，每种语言私下发你一份预览。英雄、技能和地图名用官方译名（简体用国服、繁体用台服），`@mrmeeseeks help` 这类指令原样保留
4. 确认没问题点 **Publish**；某种语言翻译失败时 **Publish** 不可用，点 **Retry** 只重翻失败的语言。预览 15 分钟内不发布就作废

发布后频道里是一条原文公告，下面有 `简体中文` `繁體中文` `English` `日本語` `한국어` 五个按钮，点了只有自己能看到对应版本。翻译在发布前一次做完并存在 `STATE_DIR/announcements/`，点按钮不调用 API，bot 重启后按钮照样能用。公告不会 @ 任何人；要改内容就删掉重发。

### 配对规则

- 配对码 5 分钟内有效，最多输错 5 次，之后自动作废，需要重新 `@mrmeeseeks`
- 只有发起 `@mrmeeseeks` 的人能在下拉菜单里做选择
- 已绑定的客户端不会再出现在下拉菜单里
- 客户端断线重连后会拿到新标识，需要重新配对；服务器重启后所有客户端都要重新配对

---

## 运行测试

测试不需要 Discord、API Key 或显示器，改代码后建议跑一遍：

```bash
pip install -r server/requirements.txt
python tests/test_units.py     # 分析器两步流程、Tab 截图时序
python tests/test_e2e.py       # 真实服务器 + 真实客户端：握手、配对、错码、过期、截图转发、限流
python tests/test_heroparse.py # Wiki 英雄页面解析
python tests/test_damage.py    # 伤害衰减、护甲、几枪击杀
python tests/test_herodata.py  # 英雄数据缓存、刷新、名字解析
python tests/test_hero_qa.py   # 英雄问答工具与对话循环、bot 路由
python tests/test_matchups.py  # counterwatch 对位数据解析、缓存、查询
python tests/test_counter_advice.py # 换英雄建议里的对位评分和换人候选
python tests/test_esports.py   # Liquipedia 赛程、官方新闻解析
python tests/test_esports_feed.py # 赛事推送调度：预告、提醒、赛果、新闻、夏令时
python tests/test_esports_bot.py  # esports / region / live 指令和权限、开播通知发送
python tests/test_predictions.py   # 竞猜：押注、截止、结算、计分、排行榜
python tests/test_rosters.py       # 赛事报名阵容：解析（含教练、队徽）、候选页面、缓存与限速
python tests/test_logos.py         # 队徽缓存：下载、间隔、失败重试、只接受图片
python tests/test_roster_card.py   # 对阵图：尺寸、队徽大小一致、长队名、没有队徽 / 阵容
python tests/test_prediction_feed.py # 竞猜接入推送：完整流程、改期、作废、周榜
python tests/test_prediction_ui.py   # 竞猜按钮：点击、改选、节流
python tests/test_prediction_bot.py  # predictions 指令、preview 样例、带按钮的发送与编辑
python tests/test_regions.py      # 赛区映射：Liquipedia 赛事路径 -> 赛区、赛区 -> 频道
python tests/test_live.py         # 开播通知：B 站链接和接口解析、开播/下播判定、直播源文件、轮询循环
python tests/test_patchnotes.py   # 官方补丁说明解析、推送频道、问答工具、patches 指令
python tests/test_wishes.py     # 功能许愿：每日限额、存档导出、表单提交、入口消息、方案起草与修改
python tests/test_announce.py   # 多语言公告：分段、翻译、预览、发布、语言按钮
python tests/test_watch_rooms.py   # 一起看：房间状态、权限、房主交接、过期
python tests/test_watch_sources.py # 一起看：链接解析、B 站取流、YouTube 信息
python tests/test_watch_server.py  # 一起看：HTTP 路由、WebSocket 协议、B 站转流
python tests/test_watch_bot.py     # 一起看：指令、房间卡片、房主按钮
python tests/test_tv_net.py        # 电视直播：出站请求只连公网地址、跳转每一跳都检查
python tests/test_tv_channels.py   # 电视直播：m3u 解析、频道归并和别名、匹配、频道表落盘和刷新
python tests/test_tv_stream.py     # 电视直播：选清晰度、识别 H.265、探测
python tests/test_tv_relay.py      # 电视直播：播放列表改写、分片转发和缓存、断流判定
```

每个脚本最后一行显示 `N/N passed`，全部通过时退出码为 0。

---

## 常见问题

**启动服务器提示 "DISCORD_TOKEN is missing"**
没有创建 `server/.env`，或者 Token 没填。按第 4 步复制 `.env.example` 并填写。

**提示 "Discord login failed"**
Token 填错了，或者在开发者后台重新生成过。重新复制一次。

**提示 "enable Message Content Intent"**
按第 2 步第 2 点，在 Bot 页面打开 Message Content Intent 并保存。

**@mrmeeseeks 没有任何反应**
- 确认服务器窗口还开着，bot 在线
- 确认 @ 的是 bot 本人，而不是同名的身份组
- 确认 bot 在这个频道有"查看频道"和"发送消息"权限

**回复 "No clients available"，但客户端明明开着**
- 看客户端窗口有没有显示"本机标识"。没有的话说明没连上，检查 `--server` 地址和防火墙
- 这个客户端可能已经绑定到别的频道了，用 `@mrmeeseeks status` 查看，用 `@mrmeeseeks disconnect` 解绑

**PowerShell 提示"无法加载 Activate.ps1，因为在此系统上禁止运行脚本"**
先执行 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`，再激活虚拟环境。

**客户端连不上服务器（一直在重连）**
- 服务器在同一台电脑：确认服务器已启动，端口没被占用
- 服务器在别的机器：确认 IP 正确，服务器防火墙 / 云安全组放行了 8765；用了 Caddy 的话，地址要写 `wss://域名`，不要带端口

**截图是黑的**
把守望先锋改成"无边框窗口"模式。

**游戏里按 Tab 没反应（客户端没有 "Screenshots sent" 日志）**
- 确认客户端已配对成功，没有加 `--no-capture`
- 两次截图之间要隔 `--cooldown` 秒，按住时间要超过 `--min-hold`
- 如果游戏是以管理员身份运行的，客户端所在的 PowerShell 也要以管理员身份运行，否则收不到游戏窗口里的按键

**英雄显示成 `?`**
没见过的英雄或皮肤。频道里会弹出标注菜单，选「职责 → 英雄」后立刻生效；也可以之后用 `@mrmeeseeks label` 补标。标注的模板存在服务器的 `state/` 目录，用 `deploy/pull-templates.sh` 可以拉回仓库。

**提示 "Couldn't see the scoreboard"**
截到计分板之前就松开了 Tab，或者计分板还没完全弹出。按住 Tab 一秒左右再松开；还不行就调大 `--score-delay`。

---

## 已知限制与路线图

目前的限制：

- 只做了画面识别。建议依据的是识别出的地图、阶段和双方阵容，**还没有接入队友的生涯数据**
- 闪点模式暂不支持
- 国服（网易）账号的生涯数据无法通过 OverFast 查询

接下来计划做的（扩展点见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)）：

- [ ] 通过 [OverFast API](https://overfast-api.tekrop.fr/) 查询队友擅长的英雄；支持登记固定队友的 BattleTag
- [ ] 地图分段对照表，把"第 2 段 40%"翻译成具体交战位置
- [ ] 接入当前版本的英雄数据，替代模型自带的克制知识
- [ ] 同一局内的上下文记忆（比如识别"敌方刚换了英雄"）
- [ ] 在语音频道里用 TTS 念出建议

---

## 声明

本项目与暴雪娱乐无关。客户端只读取屏幕画面，不读取游戏内存，也不自动操作游戏；暴雪没有专门就这类工具表态，是否使用请自行判断。Overwatch 是暴雪娱乐的商标。
