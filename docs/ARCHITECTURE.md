# 架构与协议

给想修改或扩展 mrmeeseeks 的开发者看。只想跑起来的话，看根目录的 README 就够了。

## 模块

| 文件 | 职责 |
|---|---|
| `server/bot.py` | 入口。Discord bot（指令、下拉菜单、发消息）和 WebSocket 服务跑在同一个 asyncio 事件循环里 |
| `server/ws_server.py` | WebSocket 服务：握手、配对码校验、截图接收、限流，把事件交给 bot |
| `server/registry.py` | 在线客户端表和配对状态机，不依赖网络和 Discord，便于单独测试 |
| `server/analyzer.py` | 调用 Claude：先识别画面事实，再基于事实给建议 |
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
                  │  @mrmeeseeks 断开 → 回到"已握手"
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

## 扩展点

`Analyzer.analyze` 里的 `context` 字典就是接入更多事实数据的地方，目前三项都是"暂未接入"：

- `队友生涯数据`：按计分板上的名字调用 OverFast API（https://overfast-api.tekrop.fr/）搜索玩家，取各英雄游戏时长和胜率；重名或生涯私密时标注"无法确认"
- `地图分段对照`：每张地图每一段的关键位置名称，建议做成一个手动维护的 JSON 文件
- `版本数据`：OverFast 的英雄资料和使用率

新增 Discord 指令：在 `MeeseeksBot.on_message` 里按 `cmd` 分发。
