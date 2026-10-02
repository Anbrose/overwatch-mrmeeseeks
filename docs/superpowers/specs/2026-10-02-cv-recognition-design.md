# 计分板识别：从 Claude 看图换成模板匹配 + OCR

## 背景
Claude 看图识别每次 36–159 秒，且约 80px 的英雄头像基本认不出（高 effort 留空，低 effort 瞎猜）。
本地验证（8 组真实截图，3440×1440，中文界面）：OpenCV 模板匹配 42/42 英雄正确，每行约 19ms；
RapidOCR 读昵称、右上角标题、目标文字均可用。

## 目标
- 按 Tab 到频道出建议 < 10 秒；局面没变时 < 2 秒且不调用 LLM。
- 认不出就是 unknown，绝不猜。
- 认不出的头像可以在 Discord 里标注，标完立刻生效。

## 架构（全部在服务器端，客户端不变）
```
截图 → vision.recognize() → Recognition → situation（按频道）→ 跳过 / analyzer.advise()
                     └ 未知头像 → labeling 队列 → Discord 标注菜单 → 新模板
```
- `server/vision/layout.py`：缩放到高 1440；行位置固定比例（±0.015h），横向滑动搜索头像，取置信行的中位偏移作为表格基准。
- `server/vision/heroes.py`：多模板匹配（TM_CCOEFF_NORMED），≥0.70 采用；`_dead`（X）、`_empty`（?）是独立类别。模板只含头像内部（四周内缩 12%），不含职责色条。
- `server/vision/ocr.py`：RapidOCR 封装。
- `server/vision/text.py`：标题解析（地图按 `data/maps.json` 匹配，模式以地图表为准）、目标文字解析（攻防、A 点阶段）、昵称分配到行。
- `server/vision/progress.py`：进度条蓝色占比，按菱形标记切分段数。
- `server/vision/recognize.py`：组合以上，输出 facts（沿用原 JSON 结构，每人加 `player`）。
- `server/situation.py`：按频道保存；昵称→英雄记忆（阵亡用记忆补全，标 †）；地图或攻防变化时清空；
  指纹 = (地图, 攻防, 段, 我方英雄集合, 敌方英雄集合)，全部已知且与上次相同时跳过。
- `server/labeling.py`：未知头像队列（相似度 ≥0.9 去重）、标注后存模板并热加载；英雄名单来自 OverFast `/heroes`，失败时用内置名单。
- `server/analyzer.py`：只保留纯文本给建议。

## Discord
- 正常：识别摘要 + 截图附件 + 建议；未知格子显示 `?`。
- 没变：一行 `No change … previous advice still applies: <链接>`，不贴图、不调 LLM。
- 没看到计分表：提示按住 Tab 久一点，不调 LLM。
- `@mrmeeseeks analyze`：用本频道最近一次识别结果重新给建议。
- `@mrmeeseeks label`：拿出最多 5 个待标注头像。每次截图自动弹最多 3 个。
- 标注菜单两级：职责 → 英雄；只有绑定该频道客户端的人（或发起 label 的人）能选。

## 存储
- 仓库自带种子模板：`server/data/templates/<hero_key>/*.png`。
- 服务器新标注：`/opt/mrmeeseeks/state`（挂载为容器 `/state`），包括 `templates/` 和 `unlabeled/`。
- `deploy/pull-templates.sh` 把服务器模板拉回仓库。

## 暂不支持（识别结果为 null）
防守方目标文字、占点图子地图与占点百分比、推进图和闪点图进度——需要补截图。

## 测试
- 合成计分板（用模板拼图 + 已知偏移）测 layout/heroes/recognize；OCR 用假实现注入。
- 文本解析、进度条（合成图）、situation、labeling 各自单元测试。
- 真实截图只在本地评估，不提交（仓库公开，截图含其他玩家昵称）。
