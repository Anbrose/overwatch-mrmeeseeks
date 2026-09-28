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

**分析流程**：先让 Claude 只做识别（地图、模式、攻防、第几段、双方阵容，每项带置信度，看不清就留空），再把识别结果交给第二次调用生成建议。第二次调用看不到图片，只能引用识别出的事实，每条建议都要注明依据。

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
   - **Bot Permissions** 勾选：`View Channels`、`Send Messages`、`Attach Files`、`Read Message History`
4. 复制页面底部生成的链接，在浏览器里打开，选择你的 Discord 服务器，授权。

完成后 bot 会出现在服务器成员列表里，状态是离线，等第 4 步启动后才会上线。

---

## 第 3 步：获取 Claude API Key

1. 登录 [Claude Console](https://platform.claude.com)，创建一个 API Key。详细步骤见[官方文档](https://platform.claude.com/docs/en/get-api-key)。
2. 复制保存好这个 Key。

这一步可以先跳过。不填 API Key 时，bot 只会把截图转发到频道、不做分析，正好用来调试截图时机（见[阶段 B](#阶段-b测试截图时机不需要-api-key)）。

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
Detected: Map King's Row | Escort/Attack | Checkpoint 2 ~40% | Allies D.Va, … | Enemies Winston, …

One-sentence conclusion…
- Reason 1 (basis: …)
- Reason 2 (basis: …)
```

第一行 "Detected" 是模型看到的内容，用来核对它有没有认错。客户端窗口里也会显示结论。

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
| `ANTHROPIC_API_KEY` | 否 | 空 | 留空则只转发截图，不做分析 |
| `CLAUDE_MODEL` | 否 | `claude-sonnet-5` | 分析用的模型；想更快可换成 `claude-haiku-4-5-20251001`。可用模型见[模型列表](https://platform.claude.com/docs/en/models/overview) |
| `WS_HOST` | 否 | `0.0.0.0` | WebSocket 监听地址；用反向代理时改为 `127.0.0.1` |
| `WS_PORT` | 否 | `8765` | WebSocket 端口 |
| `MIN_SNAPSHOT_INTERVAL` | 否 | `5` | 同一客户端两次分析的最短间隔（秒） |

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

| 指令 | 作用 |
|---|---|
| `@mrmeeseeks` 或 `@mrmeeseeks connect` | 连接客户端：选择标识，私下收到 8 位配对码 |
| `@mrmeeseeks status` | 查看所有在线客户端和绑定情况 |
| `@mrmeeseeks disconnect` | 解绑本频道的客户端 |
| `@mrmeeseeks help` | 显示帮助 |

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

**分析结果经常认错英雄**
频道里的 "Detected" 行能看出是哪一步出错。可以试着提高 `--quality`，或者换用更强的模型（`CLAUDE_MODEL`）。

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
