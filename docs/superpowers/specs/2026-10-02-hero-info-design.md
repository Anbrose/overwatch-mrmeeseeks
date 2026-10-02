# 英雄信息问答：设计

日期：2026-10-02

## 目标

在 Discord 里 `@mrmeeseeks <自然语言问题>`，回答三类问题：

1. 补丁历史：「X 最近被 nerf/buff 过吗？」
2. 原始数值：「X 的子弹体积 / 血量是多少？」
3. 推导计算：「X 的子弹在 N 米打多少伤害」「X 打身体/爆头几枪杀 Y」

成功标准：回答里的每个数字都来自抓取的数据或确定性计算，不来自 LLM 记忆；数据缺失或场景不支持时明确说出来，而不是给出近似值。

## 范围

做：主武器的单发伤害（距离衰减）、几枪击杀（身体/爆头）、护甲/护盾规则、Stalwart 副职业爆头减伤、Role Queue / Open Queue / 6v6 血量差异、补丁历史、英雄基础数值。

不做（本期）：TTK（击杀时间）、perk 对伤害的加成、技能伤害（非主武器）、把数据接入换英雄建议（`patch_data`）、独立的抓取定时任务。

## 架构

```
Discord @mrmeeseeks <问题>
        │
   bot.py 路由 ──(connect/status/disconnect/help/空)──▶ 现有逻辑
        │ 其他文本
        ▼
   hero_qa.py  ── Claude tool use 循环（≤5 轮）
        │ 工具调用
        ├── get_hero_stats ─────┐
        ├── get_patch_history ──┼──▶ herodata.py（内存快照 ← data/heroes.json ← Wiki）
        └── shots_to_kill ──────┴──▶ damage.py（纯函数）
```

| 文件 | 职责 | 依赖 |
|---|---|---|
| `server/herodata.py` | 抓取 Wiki、解析、规范化、缓存、定时刷新、英雄名解析 | aiohttp（discord.py 已依赖）, mwparserfromhell |
| `server/aliases.json` | 中文名与别名（手工维护） | — |
| `server/damage.py` | 伤害与击杀计算 | 无 |
| `server/hero_qa.py` | 工具定义、tool use 循环、系统提示 | anthropic, herodata, damage |
| `server/bot.py` | 路由改动、冷却、typing 提示 | hero_qa |

## 1. 数据层 `server/herodata.py`

### 来源

Overwatch fandom Wiki 的 MediaWiki API：
- 英雄列表：英雄分类页（`list=categorymembers`）。
- 每个英雄：`action=parse&page=<Hero>&prop=wikitext&format=json&formatversion=2`。
- 请求带明确的 User-Agent，串行，请求间隔约 1 秒。

一个英雄主页面包含全部所需信息：
- Infobox：`role`, `health`, `armor`, `shield`, `health6v6`, `armor6v6`, …
- 副职业：`{{Sub-Role passive|<Name>}}`
- 武器：`{{Ability_details}}` 中 `ability_type` 含 `Weapon` 的条目，字段 `damage`（如 `70 - 21`）、`damage_falloff_range`（如 `25 - 35 meters`）、`headshot`（✓/✕）、`shot_type`、`pradius`（弹体半径，如 `{{tt|0.07 meters|...}}`）等
- 补丁：`==Balance Change Log==` 下的 `{{PatchTableElement|YYYY-MM-DD|...}}`

用 `mwparserfromhell` 解析模板；`{{tt|显示值|提示}}` 取显示值。

### 规范化结构

```json
{
  "name": "Cassidy",
  "aliases": ["卡西迪", "麦克雷", "McCree", "cass"],
  "role": "Damage",
  "subrole": "Sharpshooter",
  "hp": {
    "role_queue": {"health": 250, "armor": 0, "shield": 0},
    "open_queue": {"health": 250, "armor": 0, "shield": 0},
    "6v6":        {"health": 250, "armor": 0, "shield": 0}
  },
  "weapons": [{
    "name": "Peacekeeper", "fire": "Primary Fire", "shot_type": "hitscan",
    "damage_max": 70, "damage_min": 21,
    "falloff_start": 25, "falloff_end": 35,
    "headshot": true, "crit_multiplier": 2.0, "pellets": 1,
    "projectile_radius": 0.07,
    "raw": {"damage": "70 - 21", "...": "..."}
  }],
  "patches": [{"date": "2026-08-11", "text": "Giddy Up - Minor Perk: New. ..."}],
  "source_url": "https://overwatch.fandom.com/wiki/Cassidy",
  "fetched_at": "2026-10-02T00:00:00Z"
}
```

- 模式血量：Role Queue 下坦克 `health` 额外 +150（护甲/护盾不变，见 Wiki「Tank」页）；`6v6` 取 `*6v6` 字段，缺失时回落到基础值；Open Queue 取基础值。
- 补丁文本：去掉 wiki 标记（`{{al|X}}`→X、链接取显示文本、列表符号保留为 `- `），按日期降序。
- 解析不了的数值字段一律 `null`，原文保留在 `raw`。不猜。
- `crit_multiplier`：默认 2.0；Wiki 字段给出其他倍率时用该值。
- `pellets`：从字段（如 `damage = 6 x 20` 或 pellet 相关字段）解析，默认 1。

### 英雄名解析

`resolve(name) -> Hero | list[str]`：依次匹配精确名（忽略大小写/空格/标点）→ `aliases.json` → 前缀/模糊匹配。唯一命中返回英雄；否则返回最多 5 个候选名。

### 缓存与刷新

- 快照写入 `HERO_DATA_PATH`（默认 `data/heroes.json`，Docker 中挂卷）。
- 启动：读缓存（若有）→ 后台立即刷新一次 → 之后每 `HERO_REFRESH_HOURS`（默认 24）刷新。
- 新快照替换旧快照的条件：英雄数 ≥ 旧快照的 90%，且「有武器数据的英雄占比」不比旧快照低超过 10 个百分点。不满足则保留旧快照并记 warning。无旧快照时只要求英雄数 > 0。
- 写文件用临时文件 + rename，避免写一半。

## 2. 计算器 `server/damage.py`

纯函数，规则常量集中在文件顶部并注明 Wiki 出处：

- `ARMOR_FLAT_REDUCTION = 7`, `ARMOR_MAX_REDUCTION = 0.5`（Wiki「Hit points」页 Armor 节）
- `ROLE_QUEUE_TANK_BONUS = 150`
- `STALWART_CRIT_REDUCTION`：从 Wiki 副职业页解析，解析失败用文件中记录的常量并在结果中注明

### `damage_at(weapon, distance) -> float`

- `distance ≤ falloff_start` → `damage_max`
- `distance ≥ falloff_end` → `damage_min`
- 之间线性插值
- 无衰减数据 → `damage_max`

### `shots_to_kill(weapon, target, distance, headshot, mode="role_queue") -> Result`

逐发模拟：

1. 单段伤害 = `damage_at` ×（`headshot` 且武器可爆头 ? `crit_multiplier` : 1）；若爆头且目标副职业为 Stalwart，再乘以 (1 − `STALWART_CRIT_REDUCTION`)。
2. 每一发 = `pellets` 段独立伤害（默认假设全部命中）。
3. 每段伤害按 护盾 → 护甲 → 生命值 结算：护盾不减伤；该段只要打到护甲，整段先减 `min(7, 50%)` 再结算（Wiki：护甲减免只要触及护甲就全额生效）。
4. 循环直到生命值 ≤ 0。

返回：`shots`、每发明细（单段伤害、剩余护盾/护甲/生命值）、`assumptions`（如「弹丸全部命中」「Role Queue」）。

不支持时返回 `unsupported` + 原因，不抛异常：
- `shot_type` 为 beam 或 melee
- 爆炸/溅射类：如果能解析出直击和溅射，就按「直击 + 满额溅射」算，并写入 assumptions；解析不出来就返回不支持
- 任一必需字段为 `null` → `数据缺失：<字段>`

## 3. 问答层 `server/hero_qa.py`

### 工具

| 工具 | 输入 | 返回 |
|---|---|---|
| `get_hero_stats` | `hero` | 角色/副职业、三种模式血量、武器参数、`source_url`、`fetched_at` |
| `get_patch_history` | `hero`, `limit`（默认 5） | 最近 N 条补丁 `{date, text}` |
| `shots_to_kill` | `attacker`, `weapon`（可选，默认主武器）, `target`, `distance`（米，默认 0）, `headshot`（bool）, `mode`（可选） | `damage.shots_to_kill` 的结果 |

英雄名在工具内部用 `resolve` 解析；未唯一命中时返回 `{"error": "unknown_hero", "candidates": [...]}`。

### 循环

使用 `AsyncAnthropic` 手写 tool use 循环，最多 5 轮；超过上限就回复「问题太复杂，请拆开问」。模型沿用 `CLAUDE_MODEL`。

### 系统提示要点

- 所有数字必须来自工具结果，禁止凭记忆补充数值。
- 判断 buff/nerf 时必须引用补丁原文和日期；只有 perk 改动时要说明。
- 写出结果里的 assumptions。
- 英雄名不明确时反问，不猜。
- 用提问的语言回答，≤ 约 150 词；结尾一行写数据来源（Wiki）和抓取日期。
- 问到范围之外的内容（TTK、技能伤害、perk 加成）时，说明暂不支持。

## 4. Bot 接入 `server/bot.py`

- 路由：`""`/`connect`/`status`/`disconnect`/`help` 不变；其他文本交给 `HeroQA.answer(text)`。
- 处理期间显示 `typing`；结果截断到 1990 字符后 reply。
- 每个用户 5 秒冷却；冷却期内回复简短提示。
- 未配置 `ANTHROPIC_API_KEY`：回复「英雄问答需要配置 ANTHROPIC_API_KEY」。
- 数据未就绪（无缓存且首次刷新尚未成功）：回复「英雄数据尚未就绪，请稍后再试」。
- Claude API 出错：回复简短错误并记日志。
- `HELP_TEXT` 增加问答示例。

## 配置

`.env.example` 新增：
- `HERO_DATA_PATH=data/heroes.json`
- `HERO_REFRESH_HOURS=24`

`compose.yml` 为 `data/` 加卷。`requirements.txt` 加 `mwparserfromhell`；HTTP 用 discord.py 已带的 aiohttp。

## 测试

全部离线，不需要 Discord 和 API Key：

- **解析**：`tests/fixtures/wiki/` 下保存 Cassidy、Reinhardt、Zenyatta 和一个霰弹枪英雄的 wikitext，断言规范化结果（血量、各模式血量、武器字段、补丁条目数与首条日期）。
- **计算器**（TDD，手算对照）：Cassidy 打 Tracer 身体 3 发、爆头 2 发；30 米衰减中值；护甲「减 7」与「50% 上限」的边界；霰弹弹丸逐颗受护甲减免；跨越护盾→护甲→生命值分界的那一发；beam/melee/字段缺失返回 unsupported。
- **刷新**：异常快照（英雄数骤降）不替换旧快照；原子写入。
- **名字解析**：「麦克雷」→ Cassidy；歧义输入返回候选。
- **问答循环**：用假 Anthropic client 回放预设的 tool_use 序列，验证工具分发、多轮、轮数上限、未知英雄的处理。
- **路由**：`status` 仍然走原有指令；其他文本进入问答；冷却生效。

## 文档

`README.md` 加「英雄问答」用法；`docs/ARCHITECTURE.md` 的模块表和数据流加入新模块。
