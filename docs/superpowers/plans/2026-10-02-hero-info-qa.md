# 英雄信息问答 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户在 Discord 里 `@mrmeeseeks <自然语言问题>`，查询英雄数值、补丁历史和"几枪击杀"，所有数字都来自 Overwatch Wiki 数据或确定性计算。

**Architecture:** `heroparse.py` 把 Wiki 英雄页面 wikitext 解析成规范化 dict；`herodata.py` 负责抓取、缓存到 JSON、定时刷新和按名字查英雄；`damage.py` 是纯函数伤害/击杀计算器；`hero_qa.py` 用 Claude tool use 循环把问题路由到 3 个工具；`bot.py` 把不认识的 @ 文本交给问答。

**Tech Stack:** Python 3.12（Docker）/ 3.10+，discord.py，anthropic SDK（`AsyncAnthropic`，手写 tool use 循环），aiohttp（discord.py 已依赖），mwparserfromhell。测试沿用仓库现有风格：`python tests/test_xxx.py`，自带 `check()`，不用 pytest。

**Spec:** `docs/superpowers/specs/2026-10-02-hero-info-design.md`

## Global Constraints

- 回答里的数字只能来自工具结果（Wiki 快照或 `damage.py` 计算），不能来自模型记忆。
- 本期不做：TTK、perk 对伤害的加成、技能（非 Weapon）伤害、把数据接入换英雄建议、独立的抓取定时任务。
- 数据源：`https://overwatch.fandom.com/api.php`，请求带 User-Agent，串行，间隔约 1 秒。
- 模式：`role_queue`（默认；坦克生命值 +150）、`open_queue`（infobox 基础值）、`6v6`（`*6v6` 字段，缺失回落基础值）。
- 护甲：每段伤害减 7，最多减 50%，只要该段打到护甲就整段减免；护盾不减伤；顺序 护盾 → 护甲 → 生命值。
- 爆头倍率默认 2.0；Sojourn Charged Shot / Illari Solar Rifle / Juno Mediblaster = 1.5；Widowmaker Widow's Kiss (ADS) = 2.5。
- 爆头减伤属于 **Bruiser**（-25%，1.5x 武器无视），不是 Stalwart。
- 新快照替换条件：英雄数 ≥ 旧的 90%，且有武器数据的英雄占比下降不超过 10 个百分点；无旧快照时非空即可。
- 刷新间隔 `HERO_REFRESH_HOURS` 默认 24；缓存 `HERO_DATA_PATH` 默认 `server/data/heroes.json`。
- tool use 循环最多 5 轮；回复截断到 1990 字符；每用户 5 秒冷却。
- 问答内容跟随提问语言；bot 自身的固定提示用英文（与现有提示一致）。
- 代码注释用中文，风格与现有 `server/*.py` 一致（模块 docstring 说明用途）。

## Review Focus

1. **缓存目录不可写**（Docker `read_only`、卷权限错误）：刷新后应继续使用内存中的新数据，刷新循环不能因此退出。→ Task 3 测试「缓存写不进去时仍然更新内存数据」。
2. **模型传来奇怪的参数**（`distance: "30m"`、负距离、`variant` 越界）：应把可读的错误返回给模型，不能抛异常导致整个回答失败。→ Task 2「负距离」「variant 越界」，Task 4「距离不是数字时返回可读错误」。
3. **多变体武器**（Widowmaker 蓄力、Mauga 直伤/燃烧、Ana 飞镖）：结果必须标明用了哪个变体，让回答可以说清前提。→ Task 2「结果标明用了哪个变体」。
4. **别名表之外的叫法**（新英雄的中文名、错别字）：应返回候选或空列表让模型反问，不能静默匹配到错的英雄。→ Task 3「拼写错误返回候选」「单字母不自动匹配」「完全不认识返回空列表」。
5. **只改了 perk 的补丁**：补丁文本必须保留 "Minor Perk / Major Perk" 标签，让模型能说明"只动了 perk"。→ Task 1「perk 改动保留 perk 标签」。

---

## 文件结构

| 文件 | 动作 | 职责 |
|---|---|---|
| `server/heroparse.py` | 新建 | wikitext → 规范化英雄 dict（纯函数） |
| `server/damage.py` | 新建 | 单发伤害、逐发击杀模拟（纯函数） |
| `server/herodata.py` | 新建 | `HeroStore`（缓存/刷新/名字解析）、`fetch_all`、`refresh_loop` |
| `server/aliases.json` | 新建 | 中文名和别名 |
| `server/hero_qa.py` | 新建 | 工具定义、`HeroQA` tool use 循环、`Cooldown` |
| `server/bot.py` | 修改 | `parse_command`、`_ask`、启动刷新任务 |
| `server/requirements.txt`、`server/.env.example`、`.gitignore`、`Dockerfile`、`compose.yml` | 修改 | 依赖与配置 |
| `README.md`、`docs/ARCHITECTURE.md` | 修改 | 用法与架构 |
| `tests/fixtures/wiki/*.wikitext` | 新建 | 固定修订版本的 Wiki 页面 |
| `tests/test_heroparse.py`、`tests/test_damage.py`、`tests/test_herodata.py`、`tests/test_hero_qa.py` | 新建 | 测试 |

### 共享数据结构（所有任务都用这个形状）

`parse_hero` 返回、`HeroStore.heroes[name]` 保存、`damage` 和 `hero_qa` 读取的英雄 dict：

```python
{
    "name": "Cassidy",
    "role": "Damage",                # "Tank" | "Damage" | "Support"
    "subrole": "Sharpshooter",       # 如 "Bruiser"、"Stalwart"；没有时为 None
    "hp": {                          # 三种模式，键固定；health 解析不出时为 None，armor/shield 缺省 0
        "role_queue": {"health": 250, "armor": 0, "shield": 0},
        "open_queue": {"health": 250, "armor": 0, "shield": 0},
        "6v6":        {"health": 250, "armor": 0, "shield": 0},
    },
    "weapons": [{
        "name": "Peacekeeper",
        "fire": "Primary Fire",       # ability_type 里 ";;" 后面的部分，没有为 None
        "shot_type": "hitscan",       # {{proj|x}} 用 "+" 连接，如 "arc+aoe"、"shotgun"、"beam"、"melee"
        "damage": [{"label": "", "max": 70.0, "min": 21.0}],  # 变体列表；pellets>1 时第 0 个是单颗弹丸
        "falloff_start": 25.0, "falloff_end": 35.0,           # 无衰减时为 None
        "headshot": True,
        "pellets": 1,
        "projectile_radius": 0.07,    # 米，无数据为 None
        "raw": {"damage": "70 - 21", "...": "..."},          # 原文，便于排查
    }],
    "patches": [{"date": "2026-08-11", "text": "Giddy Up - Minor Perk\n- New\n..."}],  # 最新在前
    "source_url": "https://overwatch.fandom.com/wiki/Cassidy",
}
```

---

### Task 1: Wiki 页面解析 `heroparse.py`

**Files:**
- Create: `server/heroparse.py`
- Create: `tests/fixtures/wiki/{Cassidy,Reinhardt,Zenyatta,Reaper,Tracer,Junkrat,Moira,Mauga,Widowmaker,Ana}.wikitext`
- Create: `tests/test_heroparse.py`
- Modify: `server/requirements.txt`

**Interfaces:**
- Consumes: 无
- Produces:
  - `parse_hero(title: str, wikitext: str) -> dict | None`（上面的共享结构；不是英雄页面返回 `None`）
  - `parse_damage(text: str) -> list[dict]`、`parse_falloff(text: str | None) -> tuple[float | None, float | None]`
  - `ROLE_QUEUE_TANK_BONUS = 150`

- [ ] **Step 1: 准备开发环境和依赖**

在 `server/requirements.txt` 末尾加一行：

```
mwparserfromhell>=0.6
```

然后：

```bash
python3 -m venv .venv
.venv/bin/pip install -r server/requirements.txt -r client/requirements.txt
```

（`.venv/` 已在 `.gitignore` 里。）

- [ ] **Step 2: 下载固定修订版本的 Wiki 页面作为 fixture**

用 `oldid` 固定版本，保证测试数据不会随 Wiki 编辑而变化：

```bash
mkdir -p tests/fixtures/wiki
.venv/bin/python - <<'EOF'
import json, time, urllib.parse, urllib.request
REVS = {"Cassidy": 380853, "Reinhardt": 380899, "Zenyatta": 381120, "Reaper": 380591, "Tracer": 380813,
        "Junkrat": 380944, "Moira": 379150, "Mauga": 380949, "Widowmaker": 380887, "Ana": 379570}
for name, rev in REVS.items():
    q = urllib.parse.urlencode({"action": "parse", "oldid": rev, "prop": "wikitext",
                                "format": "json", "formatversion": 2})
    req = urllib.request.Request("https://overwatch.fandom.com/api.php?" + q,
                                 headers={"User-Agent": "mrmeeseeks-discord-bot/1.0 (test fixtures)"})
    text = json.load(urllib.request.urlopen(req))["parse"]["wikitext"]
    with open(f"tests/fixtures/wiki/{name}.wikitext", "w", encoding="utf-8") as f:
        f.write(text)
    print(name, len(text))
    time.sleep(1)
EOF
```

Expected：打印 10 行，每个页面几万字符（Cassidy 约 99000）。

- [ ] **Step 3: 写失败的测试**

`tests/test_heroparse.py`：

```python
"""英雄页面解析测试：用 tests/fixtures/wiki/ 下保存的真实 wikitext（2026-10-02 抓取）。

运行：python tests/test_heroparse.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

from heroparse import parse_damage, parse_falloff, parse_hero  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "wiki")
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def hero(name):
    with open(os.path.join(FIXTURES, name + ".wikitext"), encoding="utf-8") as f:
        return parse_hero(name, f.read())


def weapon(h, name):
    return next(w for w in h["weapons"] if w["name"] == name)


def test_damage_strings():
    check("单值伤害", parse_damage("100") == [{"label": "", "max": 100.0, "min": 100.0}])
    check("衰减区间，en dash 也认", parse_damage("35 – 10.5") == [{"label": "", "max": 35.0, "min": 10.5}])
    check("多变体 + tt 模板取显示值",
          parse_damage("{{tt|45|125 total}} (direct hit)<br>80 - 10 (splash, enemy)") ==
          [{"label": "direct hit", "max": 45.0, "min": 45.0},
           {"label": "splash, enemy", "max": 80.0, "min": 10.0}])
    check("注释被去掉", parse_damage("14 - 4.2<!-- 30% min falloff-->") == [{"label": "", "max": 14.0, "min": 4.2}])
    check("解析不出数字时返回空列表", parse_damage("Varies") == [])
    check("衰减距离", parse_falloff("25 - 35 meters") == (25.0, 35.0))
    check("衰减距离只取第一段", parse_falloff("30 - 40 meters<br>10 - 20 meters (simultaneous fire)") == (30.0, 40.0))
    check("无衰减距离", parse_falloff(None) == (None, None) and parse_falloff("None") == (None, None))


def test_cassidy():
    h = hero("Cassidy")
    check("Cassidy 角色与副职业", h["role"] == "Damage" and h["subrole"] == "Sharpshooter")
    check("Cassidy 血量", h["hp"]["role_queue"] == {"health": 250, "armor": 0, "shield": 0})
    pk = weapon(h, "Peacekeeper")
    check("Peacekeeper 伤害", pk["damage"] == [{"label": "", "max": 70.0, "min": 21.0}])
    check("Peacekeeper 衰减", (pk["falloff_start"], pk["falloff_end"]) == (25.0, 35.0))
    check("Peacekeeper 可爆头、弹体半径", pk["headshot"] is True and pk["projectile_radius"] == 0.07)
    check("Peacekeeper 是主武器、hitscan", pk["fire"] == "Primary Fire" and pk["shot_type"] == "hitscan")
    check("Fan the Hammer 不能爆头", weapon(h, "Fan the Hammer")["headshot"] is False)
    check("补丁按日期倒序，首条 2026-08-11", h["patches"][0]["date"] == "2026-08-11"
          and all(a["date"] >= b["date"] for a, b in zip(h["patches"], h["patches"][1:])))
    check("perk 改动保留 perk 标签，方便说明只动了 perk", "Minor Perk" in h["patches"][0]["text"])
    check("补丁文本去掉了 wiki 标记", "{{" not in h["patches"][0]["text"] and "Giddy Up" in h["patches"][0]["text"])
    check("只取 owpvp 补丁（不含 stadium/ow1）", 15 <= len(h["patches"]) <= 40)
    check("来源链接", h["source_url"] == "https://overwatch.fandom.com/wiki/Cassidy")


def test_tank_modes():
    h = hero("Reinhardt")
    check("Reinhardt 副职业 Stalwart", h["role"] == "Tank" and h["subrole"] == "Stalwart")
    check("Open Queue 取基础值", h["hp"]["open_queue"] == {"health": 250, "armor": 300, "shield": 0})
    check("Role Queue 坦克 +150 生命值", h["hp"]["role_queue"] == {"health": 400, "armor": 300, "shield": 0})
    check("6v6 取 *6v6 字段", h["hp"]["6v6"] == {"health": 325, "armor": 225, "shield": 0})
    check("锤子是近战", weapon(h, "Rocket Hammer")["shot_type"] == "melee")


def test_other_heroes():
    z = hero("Zenyatta")
    check("Zenyatta 护盾", z["hp"]["role_queue"] == {"health": 75, "armor": 0, "shield": 175})
    r = weapon(hero("Reaper"), "Hellfire Shotguns")
    check("Reaper 霰弹：20 颗弹丸，第 0 个变体是单颗伤害",
          r["pellets"] == 20 and r["damage"][0] == {"label": "per pellet", "max": 5.75, "min": 1.725})
    check("Tracer 每发 2 颗子弹", weapon(hero("Tracer"), "Pulse Pistols")["pellets"] == 2)
    j = weapon(hero("Junkrat"), "Frag Launcher")
    check("Junkrat 直击 + 溅射两个变体", [v["label"] for v in j["damage"]] == ["direct hit", "splash, enemy"])
    m = hero("Moira")
    check("Moira：infobox 不规范也能解析", m is not None and m["hp"]["role_queue"]["health"] == 225)
    check("Moira 主武器是光束", "beam" in weapon(m, "Biotic Grasp")["shot_type"])
    mauga = hero("Mauga")
    check("Mauga：展开 #var 变量", weapon(mauga, "Volatile Chaingun")["damage"][0]["max"] == 4.0
          and weapon(mauga, "Volatile Chaingun")["falloff_start"] == 30.0)
    check("Mauga 副职业 Bruiser", mauga["subrole"] == "Bruiser")
    check("Ana：变量值里嵌套模板", weapon(hero("Ana"), "Biotic Rifle")["damage"][0]["max"] == 75.0)
    w = weapon(hero("Widowmaker"), "Widow's Kiss (ADS)")
    check("Widowmaker 开镜两个蓄力变体", [v["max"] for v in w["damage"]] == [12.0, 120.0])


def test_non_hero():
    check("不是英雄页面时返回 None", parse_hero("Heroes", "Some text\n==Heading==") is None)


if __name__ == "__main__":
    test_damage_strings()
    test_cassidy()
    test_tank_modes()
    test_other_heroes()
    test_non_hero()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
```

- [ ] **Step 4: 运行测试，确认失败**

Run: `.venv/bin/python tests/test_heroparse.py`
Expected: `ModuleNotFoundError: No module named 'heroparse'`

- [ ] **Step 5: 实现 `server/heroparse.py`**

```python
"""把 Overwatch fandom Wiki 的英雄页面 wikitext 解析成规范化 dict。纯函数，不联网。

数据来源格式（2026-10 核对）：
- Infobox：{{Infobox character | role = [[Tank]] | sub-role = [[Stalwart]] | health = 250 | armor = 300 | health6v6 = ...}}
- 武器：{{Ability_details | ability_type = Weapon... | shot_type | damage | damage_falloff_range | headshot | pellets | pradius}}
- 补丁：{{ChangelogsTabber | owpvp = {{PatchTableElement|YYYY-MM-DD| 文本 }} ... }}
"""
from __future__ import annotations

import re
from typing import Any

import mwparserfromhell

ROLE_QUEUE_TANK_BONUS = 150  # Wiki「Roles」页：All tanks have 150 more health in Role Queue
_NUM = r"\d+(?:\.\d+)?"
_RANGE = re.compile(rf"^\s*({_NUM})\s*(?:[-–—]\s*({_NUM}))?")


def _name(tpl) -> str:
    return str(tpl.name).strip().replace("_", " ").lower()


def _param(tpl, key: str) -> str | None:
    return str(tpl.get(key).value).strip() if tpl.has(key) else None


def parse_infobox(wikitext: str) -> dict[str, str]:
    """Infobox 用逐行扫描而不是 mwparserfromhell：部分页面（如 Moira）的 infobox 里有不规范标记，
    会让模板解析失败。只取 {{Infobox character 之后、第一个 == 标题之前，每个字段第一次出现的值。"""
    start = wikitext.find("{{Infobox character")
    if start < 0:
        return {}
    fields: dict[str, str] = {}
    for line in wikitext[start:].splitlines()[1:]:
        if line.startswith("=="):
            break
        m = re.match(r"^\|\s*([\w-]+)\s*=\s*(.*)$", line)
        if m and m.group(1) not in fields:
            fields[m.group(1)] = m.group(2).strip()
    return fields


def _resolve_vars(text: str, variables: dict[str, str]) -> str:
    """展开 {{#vardefineecho:name|value}} 为 value，{{#var:name}} 为已定义的值。value 里可能还有嵌套模板。"""
    code = mwparserfromhell.parse(text)
    for tpl in code.filter_templates(recursive=False):
        fn, _, key = str(tpl.name).strip().partition(":")
        fn, key = fn.strip().lower(), key.strip()
        if fn in ("#vardefineecho", "#vardefine"):
            value = str(tpl.params[0].value).strip() if tpl.params else ""
            variables[key] = value
            code.replace(tpl, value if fn == "#vardefineecho" else "")
        elif fn == "#var":
            code.replace(tpl, variables.get(key, ""))
    return str(code)


def _plain(text: str) -> str:
    """去掉注释，{{tt|显示|提示}} 取显示值，其他模板和链接取纯文本。"""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"\{\{tt\|([^|{}]*)\|[^{}]*\}\}", r"\1", text)
    text = re.sub(r"\{\{(?:al|proj)\|([^|{}]*)[^{}]*\}\}", r"\1", text)
    return mwparserfromhell.parse(text).strip_code().strip()


def _segments(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"<br\s*/?>", text) if s.strip()]


def _int(text: str | None) -> int | None:
    if text is None:
        return None
    m = re.match(rf"\s*({_NUM})\s*$", _plain(text))
    return int(float(m.group(1))) if m else None


def parse_damage(text: str) -> list[dict[str, Any]]:
    """'70 - 21' / '{{tt|45|125 total}} (direct hit)<br>80 - 10 (splash, enemy)' -> 变体列表。"""
    variants = []
    for seg in _segments(text):
        plain = _plain(seg)
        m = _RANGE.match(plain)
        if not m:
            continue
        hi = float(m.group(1))
        lo = float(m.group(2)) if m.group(2) else hi
        label = plain[m.end():].strip().strip("()").strip()
        variants.append({"label": label, "max": hi, "min": lo})
    return variants


def parse_falloff(text: str | None) -> tuple[float | None, float | None]:
    if not text:
        return None, None
    segs = _segments(text)
    m = _RANGE.match(_plain(segs[0])) if segs else None
    if not m or not m.group(2):
        return None, None
    return float(m.group(1)), float(m.group(2))


def _shot_type(text: str) -> str:
    kinds = re.findall(r"\{\{proj\|([^|}]+)", text)
    return "+".join(k.strip().lower() for k in kinds) or _plain(text).lower()


def _weapon(tpl, variables: dict[str, str]) -> dict[str, Any]:
    raw = {str(p.name).strip(): _resolve_vars(str(p.value).strip(), variables) for p in tpl.params}
    falloff_start, falloff_end = parse_falloff(raw.get("damage_falloff_range"))
    headshot = _plain(raw.get("headshot", ""))
    pr = _RANGE.match(_plain(raw.get("pradius", "")))
    return {
        "name": _plain(raw.get("ability_name", "")),
        "fire": (_plain(raw.get("ability_type", "")).split(";;") + [""])[1] or None,
        "shot_type": _shot_type(raw.get("shot_type", "")),
        "damage": parse_damage(raw.get("damage", "")),
        "falloff_start": falloff_start,
        "falloff_end": falloff_end,
        "headshot": headshot.startswith(("✓", "yes", "Yes")),
        "pellets": _int(raw.get("pellets")) or 1,
        "projectile_radius": float(pr.group(1)) if pr else None,
        "raw": {k: _plain(v) for k, v in raw.items()
                if k in ("damage", "damage_falloff_range", "headshot", "pellets", "pradius", "shot_type")},
    }


def parse_patches(code) -> list[dict[str, str]]:
    for tpl in code.filter_templates(recursive=False):
        if _name(tpl) == "changelogstabber" and tpl.has("owpvp"):
            out = []
            for pte in tpl.get("owpvp").value.filter_templates(recursive=False):
                if _name(pte) == "patchtableelement" and len(pte.params) >= 2:
                    date = str(pte.params[0].value).strip()
                    body = str(pte.params[1].value)
                    body = re.sub(r"\{\{DevComment\|(.*?)\}\}", r"Developer comment: \1", body, flags=re.S)
                    body = re.sub(r"^\*+\s*", "- ", body, flags=re.M)
                    text = _plain(body)
                    text = re.sub(r"\n{2,}", "\n", text).strip()
                    out.append({"date": date, "text": text})
            return sorted(out, key=lambda p: p["date"], reverse=True)
    return []


def parse_hero(title: str, wikitext: str) -> dict[str, Any] | None:
    """解析一个英雄页面；不是英雄页面（没有 Infobox character 或没有 role）时返回 None。"""
    infobox = parse_infobox(wikitext)
    if not infobox.get("role"):
        return None
    code = mwparserfromhell.parse(wikitext)
    role = _plain(infobox["role"])
    subrole = _plain(infobox.get("sub-role", "")) or None

    def pool(suffix: str = "") -> dict[str, int | None]:
        base = {k: _int(infobox.get(k)) for k in ("health", "armor", "shield")}
        over = {k: _int(infobox.get(k + suffix)) for k in ("health", "armor", "shield")} if suffix else {}
        merged = {k: (over.get(k) if over.get(k) is not None else base[k]) for k in base}
        return {k: (v if v is not None else (0 if k != "health" else None)) for k, v in merged.items()}

    open_queue = pool()
    role_queue = dict(open_queue)
    if role == "Tank" and role_queue["health"] is not None:
        role_queue["health"] += ROLE_QUEUE_TANK_BONUS

    variables: dict[str, str] = {}
    weapons = []
    for tpl in code.filter_templates():
        if _name(tpl) != "ability details":
            continue
        # 按页面顺序处理，让 {{#var:}} 能引用前面 {{#vardefineecho:}} 定义的值
        ability_type = _param(tpl, "ability_type") or ""
        if ability_type.strip().startswith("Weapon"):
            weapons.append(_weapon(tpl, variables))
        else:
            for p in tpl.params:
                _resolve_vars(str(p.value), variables)

    return {
        "name": title,
        "role": role,
        "subrole": subrole,
        "hp": {"role_queue": role_queue, "open_queue": open_queue, "6v6": pool("6v6")},
        "weapons": weapons,
        "patches": parse_patches(code),
        "source_url": "https://overwatch.fandom.com/wiki/" + title.replace(" ", "_"),
    }
```

- [ ] **Step 6: 运行测试，确认通过**

Run: `.venv/bin/python tests/test_heroparse.py`
Expected: 最后一行 `36/36 passed`

- [ ] **Step 7: Commit**

```bash
git add server/heroparse.py server/requirements.txt tests/test_heroparse.py tests/fixtures/wiki
git commit -m "Add Overwatch Wiki hero page parser"
```

---

### Task 2: 伤害计算 `damage.py`

**Files:**
- Create: `server/damage.py`
- Create: `tests/test_damage.py`

**Interfaces:**
- Consumes: 共享英雄/武器 dict 结构（不 import heroparse，测试用手写 dict）
- Produces:
  - `MODES = ("role_queue", "open_queue", "6v6")`
  - `damage_at(variant: dict, falloff_start: float | None, falloff_end: float | None, distance: float) -> float`
  - `apply_instance(pool: dict[str, float], raw: float) -> None`（原地修改 `{"health","armor","shield"}`）
  - `shots_to_kill(attacker: dict, weapon: dict, target: dict, distance: float = 0, headshot: bool = False, mode: str = "role_queue", variant: int | None = None, max_shots: int = 500) -> dict`
    - 成功：`{"attacker","weapon","variant","target","mode","distance_m","headshot","damage_per_instance","instances_per_shot","target_hp","shots","breakdown","assumptions"}`，`breakdown` 每项 `{"shot","health","armor","shield"}`
    - 不支持：`{"unsupported": "<原因>"}`，不抛异常

- [ ] **Step 1: 写失败的测试**

`tests/test_damage.py`：

```python
"""伤害计算测试：用手写的英雄/武器 dict，预期值都可以手算核对。

运行：python tests/test_damage.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import damage  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def hp(health, armor=0, shield=0):
    pool = {"health": health, "armor": armor, "shield": shield}
    return {"role_queue": pool, "open_queue": pool, "6v6": pool}


def mk_hero(name, pool, subrole=None, weapons=()):
    return {"name": name, "role": "Damage", "subrole": subrole, "hp": pool, "weapons": list(weapons)}


def mk_weapon(name, variants, falloff=(None, None), shot_type="hitscan", headshot=True, pellets=1):
    return {"name": name, "shot_type": shot_type, "headshot": headshot, "pellets": pellets,
            "damage": [{"label": l, "max": hi, "min": lo} for l, hi, lo in variants],
            "falloff_start": falloff[0], "falloff_end": falloff[1]}


PEACEKEEPER = mk_weapon("Peacekeeper", [("", 70, 21)], falloff=(25, 35))
SHOTGUN = mk_weapon("Hellfire Shotguns", [("per pellet", 5.75, 1.725), ("per shot", 115, 34.5)],
                    falloff=(10, 20), shot_type="shotgun", pellets=20)
FRAG = mk_weapon("Frag Launcher", [("direct hit", 45, 45), ("splash, enemy", 80, 10)],
                 shot_type="arc+aoe", headshot=False)
ADS = mk_weapon("Widow's Kiss (ADS)", [("at 0% power", 12, 6), ("at 100% power", 120, 60)], falloff=(50, 70))
CASSIDY = mk_hero("Cassidy", hp(250), weapons=[PEACEKEEPER])
WIDOW = mk_hero("Widowmaker", hp(225), weapons=[ADS])
TRACER = mk_hero("Tracer", hp(175))
ZEN = mk_hero("Zenyatta", hp(75, shield=175))
REIN = mk_hero("Reinhardt", {"role_queue": {"health": 400, "armor": 300, "shield": 0},
                             "open_queue": {"health": 250, "armor": 300, "shield": 0},
                             "6v6": {"health": 325, "armor": 225, "shield": 0}}, subrole="Stalwart")
MAUGA = mk_hero("Mauga", hp(575, armor=125), subrole="Bruiser")


def test_damage_at():
    v = PEACEKEEPER["damage"][0]
    check("衰减起点前满伤害", damage.damage_at(v, 25, 35, 10) == 70)
    check("衰减中点线性插值 (70+21)/2", abs(damage.damage_at(v, 25, 35, 30) - 45.5) < 1e-9)
    check("衰减终点后最低伤害", damage.damage_at(v, 25, 35, 50) == 21)
    check("无衰减数据恒为最大值", damage.damage_at(v, None, None, 100) == 70)


def test_armor_rule():
    pool = {"health": 100.0, "armor": 100.0, "shield": 0.0}
    damage.apply_instance(pool, 70)
    check("护甲：70 伤害减 7 → 63", pool["armor"] == 37)
    pool = {"health": 100.0, "armor": 100.0, "shield": 0.0}
    damage.apply_instance(pool, 10)
    check("护甲：10 伤害按 50% 上限 → 5", pool["armor"] == 95)
    pool = {"health": 100.0, "armor": 100.0, "shield": 0.0}
    damage.apply_instance(pool, 14)
    check("护甲：14 伤害正好减 7", pool["armor"] == 93)
    pool = {"health": 100.0, "armor": 10.0, "shield": 0.0}
    damage.apply_instance(pool, 70)
    check("护甲被打穿：整段减 7 后溢出到生命值 (63-10=53)", pool["armor"] == 0 and pool["health"] == 47)
    pool = {"health": 75.0, "armor": 0.0, "shield": 20.0}
    damage.apply_instance(pool, 70)
    check("护盾不减伤，溢出到生命值", pool["shield"] == 0 and pool["health"] == 25)


def test_shots_to_kill():
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, TRACER)
    check("Cassidy 身体 3 枪杀 Tracer", r["shots"] == 3 and r["damage_per_instance"] == 70)
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, TRACER, headshot=True)
    check("Cassidy 爆头 2 枪杀 Tracer", r["shots"] == 2 and r["damage_per_instance"] == 140)
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, TRACER, distance=30)
    check("30 米 45.5 伤害，4 枪杀 Tracer", r["shots"] == 4 and r["damage_per_instance"] == 45.5)
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, ZEN)
    check("Zenyatta 护盾+血量 250，4 枪", r["shots"] == 4)
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, REIN)
    check("Role Queue Reinhardt 11 枪（前 5 枪打护甲）", r["shots"] == 11
          and r["breakdown"][4] == {"shot": 5, "health": 385, "armor": 0, "shield": 0})
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, REIN, mode="open_queue")
    check("Open Queue Reinhardt 9 枪", r["shots"] == 9 and "open_queue health values" in r["assumptions"])
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, MAUGA, headshot=True)
    check("爆头打 Bruiser 减 25%：140 → 105", r["damage_per_instance"] == 105
          and any("Bruiser" in a for a in r["assumptions"]))
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, REIN, headshot=True)
    check("Stalwart 不减爆头伤害", r["damage_per_instance"] == 140)


def test_pellets_and_variants():
    reaper = mk_hero("Reaper", hp(275), weapons=[SHOTGUN])
    r = damage.shots_to_kill(reaper, SHOTGUN, TRACER)
    check("Reaper 近距离 2 发杀 Tracer (115/发)", r["shots"] == 2 and r["instances_per_shot"] == 20)
    check("霰弹 assumptions 写明弹丸全中", any("20 pellets" in a for a in r["assumptions"]))
    r = damage.shots_to_kill(reaper, SHOTGUN, REIN)
    first = r["breakdown"][0]
    check("霰弹每颗弹丸单独受护甲减免 (5.75→2.875, ×20=57.5)", first["armor"] == 242.5)
    junk = mk_hero("Junkrat", hp(250), weapons=[FRAG])
    r = damage.shots_to_kill(junk, FRAG, TRACER)
    check("爆炸武器默认直击+满额溅射 125，2 发", r["shots"] == 2 and r["damage_per_instance"] == 125
          and any("splash" in a for a in r["assumptions"]))
    r = damage.shots_to_kill(junk, FRAG, TRACER, variant=1)
    check("指定 variant 只用溅射 80", r["damage_per_instance"] == 80)
    r = damage.shots_to_kill(WIDOW, ADS, TRACER, headshot=True, variant=1)
    check("Widowmaker 满蓄力爆头 2.5x = 300，一枪", r["shots"] == 1 and r["damage_per_instance"] == 300)
    check("结果标明用了哪个变体", r["variant"] == "at 100% power")


def test_unsupported():
    beam = mk_weapon("Biotic Grasp", [("", 65, 65)], shot_type="beam")
    melee = mk_weapon("Rocket Hammer", [("", 100, 100)], shot_type="melee", headshot=False)
    empty = mk_weapon("Mystery", [])
    check("光束不支持", "unsupported" in damage.shots_to_kill(CASSIDY, beam, TRACER))
    check("近战不支持", "unsupported" in damage.shots_to_kill(CASSIDY, melee, TRACER))
    check("没有伤害数据", "missing data" in damage.shots_to_kill(CASSIDY, empty, TRACER)["unsupported"])
    check("不能爆头的武器要求爆头",
          "cannot headshot" in damage.shots_to_kill(CASSIDY, FRAG, TRACER, headshot=True)["unsupported"])
    check("负距离", "distance" in damage.shots_to_kill(CASSIDY, PEACEKEEPER, TRACER, distance=-5)["unsupported"])
    check("未知模式", "unknown mode" in damage.shots_to_kill(CASSIDY, PEACEKEEPER, TRACER, mode="ffa")["unsupported"])
    check("variant 越界", "no damage variant" in damage.shots_to_kill(CASSIDY, PEACEKEEPER, TRACER, variant=3)["unsupported"])
    no_hp = mk_hero("Ghost", hp(None))
    check("目标血量缺失", "missing data" in damage.shots_to_kill(CASSIDY, PEACEKEEPER, no_hp)["unsupported"])


if __name__ == "__main__":
    test_damage_at()
    test_armor_rule()
    test_shots_to_kill()
    test_pellets_and_variants()
    test_unsupported()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/bin/python tests/test_damage.py`
Expected: `ModuleNotFoundError: No module named 'damage'`

- [ ] **Step 3: 实现 `server/damage.py`**

```python
"""伤害与击杀计算。纯函数，输入是 heroparse.parse_hero 产出的英雄/武器 dict。

规则来源（Overwatch fandom Wiki，2026-10 核对）：
- 「Hit points」Armor 节：每段伤害减 7，最多减 50%；只要这段伤害打到护甲就全额减免；护盾不减伤
- 「Critical hit」：爆头默认 2.0x；Sojourn Charged Shot / Illari Solar Rifle / Juno Mediblaster 1.5x；
  Widowmaker Widow's Kiss (ADS) 2.5x
- 「Sub-Roles」Bruiser：受到的暴击伤害 -25%；1.5x 爆头倍率的武器无视这个减伤
"""
from __future__ import annotations

from typing import Any

ARMOR_FLAT_REDUCTION = 7.0
ARMOR_MAX_REDUCTION = 0.5
DEFAULT_CRIT_MULTIPLIER = 2.0
CRIT_MULTIPLIER_OVERRIDES = {
    ("Sojourn", "Charged Shot"): 1.5,
    ("Illari", "Solar Rifle"): 1.5,
    ("Juno", "Mediblaster"): 1.5,
    ("Widowmaker", "Widow's Kiss (ADS)"): 2.5,
}
BRUISER_CRIT_REDUCTION = 0.25
MODES = ("role_queue", "open_queue", "6v6")
_EPS = 1e-6


def damage_at(variant: dict[str, float], falloff_start: float | None,
              falloff_end: float | None, distance: float) -> float:
    """单段伤害随距离线性衰减；没有衰减数据时恒为最大值。"""
    hi, lo = variant["max"], variant["min"]
    if falloff_start is None or falloff_end is None or distance <= falloff_start:
        return hi
    if distance >= falloff_end:
        return lo
    t = (distance - falloff_start) / (falloff_end - falloff_start)
    return hi + (lo - hi) * t


def crit_multiplier(hero: str, weapon: dict[str, Any]) -> float:
    return CRIT_MULTIPLIER_OVERRIDES.get((hero, weapon["name"]), DEFAULT_CRIT_MULTIPLIER)


def _explosive_parts(weapon: dict[str, Any]) -> tuple[dict, dict] | None:
    direct = next((v for v in weapon["damage"] if "direct" in v["label"].lower()), None)
    splash = next((v for v in weapon["damage"] if "splash, enemy" in v["label"].lower()), None)
    return (direct, splash) if direct and splash else None


def shot_instances(attacker: str, weapon: dict[str, Any], target: dict[str, Any], distance: float,
                   headshot: bool, variant: int | None = None) -> dict[str, Any]:
    """一次开火产生的伤害段列表（霰弹每颗弹丸一段）。不支持时返回 {"unsupported": 原因}。"""
    kind = weapon.get("shot_type") or ""
    if "beam" in kind:
        return {"unsupported": "beam weapons deal damage per second, not per shot"}
    if "melee" in kind:
        return {"unsupported": "melee weapons are out of scope"}
    if not weapon.get("damage"):
        return {"unsupported": "missing data: damage"}

    assumptions: list[str] = []
    explosive = _explosive_parts(weapon) if variant is None else None
    if explosive:
        direct, splash = explosive
        per_hit = direct["max"] + splash["max"]
        assumptions.append("direct hit plus full splash damage")
        label = "direct hit + splash"
    else:
        idx = variant or 0
        if idx >= len(weapon["damage"]):
            return {"unsupported": f"weapon has no damage variant #{idx}"}
        chosen = weapon["damage"][idx]
        per_hit = damage_at(chosen, weapon.get("falloff_start"), weapon.get("falloff_end"), distance)
        label = chosen["label"]

    if headshot:
        if not weapon.get("headshot"):
            return {"unsupported": f"{weapon['name']} cannot headshot"}
        mult = crit_multiplier(attacker, weapon)
        per_hit *= mult
        if target.get("subrole") == "Bruiser" and mult > 1.5:
            per_hit *= 1 - BRUISER_CRIT_REDUCTION
            assumptions.append("Bruiser takes 25% less critical damage")

    pellets = 1 if explosive else weapon.get("pellets") or 1
    if pellets > 1:
        assumptions.append(f"all {pellets} pellets hit")
    return {"instances": [per_hit] * pellets, "variant": label, "assumptions": assumptions}


def apply_instance(pool: dict[str, float], raw: float) -> None:
    """一段伤害按 护盾 → 护甲 → 生命值 结算，原地修改 pool。"""
    dmg = raw
    absorbed = min(pool["shield"], dmg)
    pool["shield"] -= absorbed
    dmg -= absorbed
    if dmg <= 0:
        return
    if pool["armor"] > 0:
        dmg -= min(ARMOR_FLAT_REDUCTION, dmg * ARMOR_MAX_REDUCTION)
        absorbed = min(pool["armor"], dmg)
        pool["armor"] -= absorbed
        dmg -= absorbed
    pool["health"] -= dmg


def shots_to_kill(attacker: dict[str, Any], weapon: dict[str, Any], target: dict[str, Any],
                  distance: float = 0, headshot: bool = False, mode: str = "role_queue",
                  variant: int | None = None, max_shots: int = 500) -> dict[str, Any]:
    if distance < 0:
        return {"unsupported": "distance must be 0 or more meters"}
    if mode not in MODES:
        return {"unsupported": f"unknown mode {mode!r}, use one of {', '.join(MODES)}"}
    hp = target["hp"][mode]
    if hp.get("health") is None:
        return {"unsupported": f"missing data: {target['name']} health"}
    shot = shot_instances(attacker["name"], weapon, target, distance, headshot, variant)
    if "unsupported" in shot:
        return shot

    pool = {"health": float(hp["health"]), "armor": float(hp.get("armor") or 0),
            "shield": float(hp.get("shield") or 0)}
    start = dict(pool)
    log = []
    for n in range(1, max_shots + 1):
        for inst in shot["instances"]:
            apply_instance(pool, inst)
        log.append({"shot": n, **{k: round(max(v, 0), 2) for k, v in pool.items()}})
        if pool["health"] <= _EPS:
            break
    else:
        return {"unsupported": f"target not killed within {max_shots} shots"}

    return {
        "attacker": attacker["name"], "weapon": weapon["name"], "variant": shot["variant"],
        "target": target["name"], "mode": mode, "distance_m": distance, "headshot": headshot,
        "damage_per_instance": round(shot["instances"][0], 2),
        "instances_per_shot": len(shot["instances"]),
        "target_hp": start, "shots": len(log), "breakdown": log,
        "assumptions": shot["assumptions"] + [f"{mode} health values"],
    }
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/bin/python tests/test_damage.py`
Expected: `32/32 passed`

- [ ] **Step 5: Commit**

```bash
git add server/damage.py tests/test_damage.py
git commit -m "Add damage falloff and shots-to-kill calculator"
```

---

### Task 3: 数据存储与刷新 `herodata.py`

**Files:**
- Create: `server/herodata.py`
- Create: `server/aliases.json`
- Create: `tests/test_herodata.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `heroparse.parse_hero(title, wikitext)`（Task 1）
- Produces:
  - `load_aliases(path: str = ALIASES_PATH) -> dict[str, list[str]]`
  - `class HeroStore(path: str, aliases: dict[str, list[str]] | None = None)`
    - 属性 `heroes: dict[str, dict]`、`fetched_at: str | None`（ISO UTC，如 `2026-10-02T07:13:59Z`）、`ready: bool`
    - `load() -> bool`、`save() -> None`、`accept(new: dict) -> bool`
    - `async refresh(fetch: Callable[[], Awaitable[dict[str, dict]]]) -> bool`
    - `resolve(name: str) -> dict | list[str]`（唯一命中返回英雄 dict，否则最多 5 个候选名，可能为空）
  - `async fetch_all(interval: float = 1.0) -> dict[str, dict]`
  - `async refresh_loop(store: HeroStore, hours: float, fetch=fetch_all) -> None`

- [ ] **Step 1: 写失败的测试**

`tests/test_herodata.py`：

```python
"""英雄数据缓存/刷新/名字解析测试。不联网：刷新用假的 fetch 函数。

运行：python tests/test_herodata.py
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

from herodata import HeroStore, load_aliases  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def snapshot(names, with_weapons=None):
    with_weapons = names if with_weapons is None else with_weapons
    return {n: {"name": n, "weapons": [{"name": "gun"}] if n in with_weapons else [], "patches": []}
            for n in names}


NAMES = ["Cassidy", "Tracer", "Sojourn", "Soldier: 76", "Sombra", "Lúcio", "Reinhardt", "Mei", "Mercy", "Moira"]


def test_resolve():
    store = HeroStore("unused.json", load_aliases())
    store.heroes = snapshot(NAMES)
    name = lambda q: (lambda r: r["name"] if isinstance(r, dict) else r)(store.resolve(q))  # noqa: E731
    check("精确名，忽略大小写", name("cassidy") == "Cassidy")
    check("中文别名", name("麦克雷") == "Cassidy" and name("猎空") == "Tracer")
    check("英文别名", name("McCree") == "Cassidy")
    check("忽略标点：Soldier 76", name("soldier 76") == "Soldier: 76")
    check("数字别名 76", name("76") == "Soldier: 76")
    check("忽略重音：lucio", name("lucio") == "Lúcio")
    check("单字中文别名：美", name("美") == "Mei")
    check("唯一前缀：tr", name("tr") == "Tracer")
    check("歧义前缀返回候选", name("so") == ["Sojourn", "Soldier: 76", "Sombra"])
    check("拼写错误返回候选而不是猜", name("reinhart") == ["Reinhardt"])
    check("单字母不自动匹配", isinstance(store.resolve("m"), list))
    check("完全不认识返回空列表", name("xyz") == [])
    check("空字符串返回空列表", name("  ") == [])


def test_aliases_file():
    aliases = load_aliases()
    check("aliases.json 能读", "Cassidy" in aliases and "麦克雷" in aliases["Cassidy"])
    flat = [a for v in aliases.values() for a in v]
    check("别名之间没有重复", len(flat) == len(set(flat)))


def test_accept():
    store = HeroStore("unused.json")
    check("空快照不接受", not store.accept({}))
    check("没有旧快照时，非空就接受", store.accept(snapshot(["A"])))
    store.heroes = snapshot([f"H{i}" for i in range(50)])
    check("英雄数降到 90% 以下不接受", not store.accept(snapshot([f"H{i}" for i in range(44)])))
    check("英雄数 90% 接受", store.accept(snapshot([f"H{i}" for i in range(45)])))
    names = [f"H{i}" for i in range(50)]
    check("有武器的英雄占比掉 20 个百分点不接受", not store.accept(snapshot(names, names[:40])))
    check("占比掉 10 个百分点以内接受", store.accept(snapshot(names, names[:46])))


def test_refresh_and_cache():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "heroes.json")
        store = HeroStore(path)
        check("没有缓存文件时 load 返回 False", store.load() is False and not store.ready)

        async def good():
            return snapshot(["Cassidy", "Tracer"])

        async def broken():
            raise RuntimeError("wiki down")

        async def tiny():
            return snapshot(["Cassidy"])

        check("刷新成功", asyncio.run(store.refresh(good)) is True and store.ready)
        check("写入了缓存文件，含 fetched_at", os.path.exists(path) and store.fetched_at)
        check("目录里没有残留临时文件", os.listdir(os.path.dirname(path)) == ["heroes.json"])
        check("抓取异常时保留旧快照", asyncio.run(store.refresh(broken)) is False and len(store.heroes) == 2)
        check("异常快照被拒绝", asyncio.run(store.refresh(tiny)) is False and len(store.heroes) == 2)

        fresh = HeroStore(path)
        check("新进程能从缓存加载", fresh.load() is True and set(fresh.heroes) == {"Cassidy", "Tracer"}
              and fresh.fetched_at == store.fetched_at)

        blocker = os.path.join(d, "blocker")
        open(blocker, "w").close()
        unwritable = HeroStore(os.path.join(blocker, "heroes.json"))  # 父路径是文件，写缓存必然失败
        check("缓存写不进去时仍然更新内存数据", asyncio.run(unwritable.refresh(good)) is True and unwritable.ready)

        with open(path, "w") as f:
            f.write("{not json")
        check("缓存文件损坏时 load 返回 False", HeroStore(path).load() is False)
        with open(path, "w") as f:
            json.dump({"unexpected": True}, f)
        check("缓存格式不对时 load 返回 False", HeroStore(path).load() is False)


if __name__ == "__main__":
    test_resolve()
    test_aliases_file()
    test_accept()
    test_refresh_and_cache()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/bin/python tests/test_herodata.py`
Expected: `ModuleNotFoundError: No module named 'herodata'`

- [ ] **Step 3: 写 `server/aliases.json`**

只收录有把握的官方中文名和常见叫法。较新的英雄（Anran、Domina、Emre 等）暂时只认英文名，以后可以补充：

```json
{
  "Ana": ["安娜"],
  "Ashe": ["艾什"],
  "Baptiste": ["巴蒂斯特", "巴蒂"],
  "Bastion": ["堡垒"],
  "Brigitte": ["布丽吉塔", "Brig"],
  "Cassidy": ["卡西迪", "麦克雷", "McCree", "Cass"],
  "D.Va": ["DVa", "宋哈娜"],
  "Doomfist": ["末日铁拳", "铁拳"],
  "Echo": ["回声"],
  "Freja": ["弗蕾娅"],
  "Genji": ["源氏"],
  "Hanzo": ["半藏"],
  "Illari": ["伊拉锐"],
  "Junker Queen": ["渣客女王", "JQ"],
  "Junkrat": ["狂鼠"],
  "Juno": ["朱诺"],
  "Kiriko": ["雾子"],
  "Lifeweaver": ["生命之梭"],
  "Lúcio": ["卢西奥"],
  "Mauga": ["毛加"],
  "Mei": ["美"],
  "Mercy": ["天使"],
  "Moira": ["莫伊拉"],
  "Orisa": ["奥丽莎"],
  "Pharah": ["法老之鹰", "法鸡"],
  "Ramattra": ["拉玛刹"],
  "Reaper": ["死神"],
  "Reinhardt": ["莱因哈特", "大锤", "Rein"],
  "Roadhog": ["路霸", "Hog"],
  "Sigma": ["西格玛"],
  "Sojourn": ["索杰恩"],
  "Soldier: 76": ["士兵76", "76", "Soldier"],
  "Sombra": ["黑影"],
  "Symmetra": ["秩序之光", "Sym"],
  "Torbjörn": ["托比昂", "Torb"],
  "Tracer": ["猎空"],
  "Venture": ["探奇"],
  "Widowmaker": ["黑百合", "Widow"],
  "Winston": ["温斯顿", "猩猩"],
  "Wrecking Ball": ["破坏球", "仓鼠", "Ball", "Hammond"],
  "Zarya": ["查莉娅"],
  "Zenyatta": ["禅雅塔", "Zen"]
}
```

- [ ] **Step 4: 实现 `server/herodata.py`**

```python
"""英雄数据：从 Overwatch fandom Wiki 抓取、缓存到本地 JSON、定时刷新、按名字查英雄。

快照只在「看起来正常」时才替换旧的（防止 Wiki 改版导致解析出一堆空数据），
写文件用临时文件 + rename，进程中途被杀也不会留下半个文件。
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import tempfile
import time
import unicodedata
from typing import Any, Awaitable, Callable

import aiohttp

from heroparse import parse_hero

log = logging.getLogger("mrmeeseeks.herodata")

WIKI_API = "https://overwatch.fandom.com/api.php"
USER_AGENT = "mrmeeseeks-discord-bot/1.0 (hero stats lookup)"
REQUEST_INTERVAL = 1.0
MIN_HERO_RATIO = 0.9          # 新快照英雄数至少是旧快照的 90%
MAX_WEAPON_RATIO_DROP = 0.10  # 有武器数据的英雄占比最多下降 10 个百分点
ALIASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aliases.json")


def _norm(s: str) -> str:
    """忽略大小写、空格、标点和重音：'Soldier: 76' -> 'soldier76'，'Lúcio' -> 'lucio'。"""
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[\W_]+", "", s)


def load_aliases(path: str = ALIASES_PATH) -> dict[str, list[str]]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _weapon_ratio(heroes: dict[str, dict]) -> float:
    return sum(1 for h in heroes.values() if h["weapons"]) / len(heroes) if heroes else 0.0


class HeroStore:
    def __init__(self, path: str, aliases: dict[str, list[str]] | None = None):
        self.path = path
        self.aliases = aliases or {}
        self.heroes: dict[str, dict[str, Any]] = {}
        self.fetched_at: str | None = None

    @property
    def ready(self) -> bool:
        return bool(self.heroes)

    # ---------- 缓存 ----------
    def load(self) -> bool:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            heroes, fetched_at = data["heroes"], data["fetched_at"]
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.info("No usable hero cache at %s (%r)", self.path, e)
            return False
        self.heroes, self.fetched_at = heroes, fetched_at
        log.info("Loaded %d heroes from cache (fetched %s)", len(self.heroes), self.fetched_at)
        return True

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(self.path)), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": self.fetched_at, "heroes": self.heroes}, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ---------- 刷新 ----------
    def accept(self, new: dict[str, dict]) -> bool:
        """新快照是否可以替换当前快照。"""
        if not new:
            return False
        if not self.heroes:
            return True
        if len(new) < MIN_HERO_RATIO * len(self.heroes):
            return False
        return _weapon_ratio(new) >= _weapon_ratio(self.heroes) - MAX_WEAPON_RATIO_DROP

    async def refresh(self, fetch: Callable[[], Awaitable[dict[str, dict]]]) -> bool:
        try:
            new = await fetch()
        except Exception:
            log.exception("Hero data refresh failed; keeping the current snapshot")
            return False
        if not self.accept(new):
            log.warning("Rejected hero snapshot: %d heroes (%.0f%% with weapons) vs current %d (%.0f%%)",
                        len(new), 100 * _weapon_ratio(new), len(self.heroes), 100 * _weapon_ratio(self.heroes))
            return False
        self.heroes = new
        self.fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            self.save()
        except OSError:  # 缓存写不进去（只读文件系统、权限）也继续用内存里的新数据
            log.exception("Could not write hero cache to %s", self.path)
        log.info("Hero data refreshed: %d heroes", len(new))
        return True

    # ---------- 查英雄 ----------
    def resolve(self, name: str) -> dict[str, Any] | list[str]:
        """唯一命中返回英雄 dict；否则返回候选名列表（最多 5 个，可能为空）。"""
        q = _norm(name)
        if not q:
            return []
        keys: dict[str, set[str]] = {}
        for hero in self.heroes:
            for k in [hero, *self.aliases.get(hero, [])]:
                keys.setdefault(_norm(k), set()).add(hero)
        if q in keys and len(keys[q]) == 1:
            return self.heroes[next(iter(keys[q]))]
        for match in (lambda k: k.startswith(q), lambda k: q in k):
            hits = sorted({h for k, hs in keys.items() if match(k) for h in hs})
            if len(hits) == 1 and len(q) >= 2:
                return self.heroes[hits[0]]
            if hits:
                return hits[:5]
        close = difflib.get_close_matches(q, keys.keys(), n=5, cutoff=0.6)
        return sorted({h for k in close for h in keys[k]})[:5]


# ---------- 抓取 ----------
async def _get(session: aiohttp.ClientSession, **params: str) -> dict[str, Any]:
    params = {"format": "json", "formatversion": "2", **params}
    async with session.get(WIKI_API, params=params) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_all(interval: float = REQUEST_INTERVAL) -> dict[str, dict]:
    """抓取全部英雄并解析。单个英雄失败只记日志并跳过。"""
    headers = {"User-Agent": USER_AGENT}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        data = await _get(session, action="query", list="categorymembers",
                          cmtitle="Category:Heroes", cmnamespace="0", cmlimit="500")
        titles = [m["title"] for m in data["query"]["categorymembers"] if m["title"] != "Heroes"]
        heroes: dict[str, dict] = {}
        for title in titles:
            await asyncio.sleep(interval)
            try:
                page = await _get(session, action="parse", page=title, prop="wikitext")
                hero = parse_hero(title, page["parse"]["wikitext"])
            except Exception:
                log.exception("Failed to fetch or parse hero page %s", title)
                continue
            if hero:
                heroes[hero["name"]] = hero
        return heroes


async def refresh_loop(store: HeroStore, hours: float,
                       fetch: Callable[[], Awaitable[dict[str, dict]]] = fetch_all) -> None:
    while True:
        await store.refresh(fetch)
        await asyncio.sleep(hours * 3600)
```

- [ ] **Step 5: `.gitignore` 忽略本地缓存**

在 `.gitignore` 的 `# 密钥` 段后加：

```
# 英雄数据缓存（运行时从 Wiki 抓取）
server/data/
```

- [ ] **Step 6: 运行测试，确认通过**

Run: `.venv/bin/python tests/test_herodata.py`
Expected: `31/31 passed`（日志里出现 "wiki down" 的 traceback 是预期的，它测的是抓取失败的路径）

- [ ] **Step 7: 联网冒烟测试（手动，一次即可）**

```bash
cd server && ../.venv/bin/python -c "
import asyncio
from herodata import HeroStore, fetch_all, load_aliases
s = HeroStore('/tmp/heroes-smoke.json', load_aliases())
print(asyncio.run(s.refresh(fetch_all)), len(s.heroes))
print([n for n, h in s.heroes.items() if not h['weapons']])
print(s.resolve('麦克雷')['name'], s.resolve('so'))
"; cd ..
```

Expected：约 1 分钟后输出 `True 54`（数量可能随新英雄增加），第二行 `[]`，第三行 `Cassidy ['Sojourn', 'Soldier: 76', 'Sombra']`。

如果某些英雄没有武器数据，记下名字并报告，不要为了凑数字改代码。

- [ ] **Step 8: Commit**

```bash
git add server/herodata.py server/aliases.json tests/test_herodata.py .gitignore
git commit -m "Add hero data store with Wiki fetch, cache and name resolution"
```

---

### Task 4: 问答层 `hero_qa.py`

**Files:**
- Create: `server/hero_qa.py`
- Create: `tests/test_hero_qa.py`（本任务先写 `test_tools`、`test_loop`、`test_cooldown`；路由相关测试在 Task 5 加入）

**Interfaces:**
- Consumes: `HeroStore.resolve`、`HeroStore.fetched_at`（Task 3）；`damage.shots_to_kill`、`damage.MODES`（Task 2）
- Produces:
  - `SYSTEM: str`、`TOOLS: list[dict]`（工具名 `get_hero_stats`、`get_patch_history`、`shots_to_kill`）
  - `MAX_TURNS = 5`、`TOO_COMPLEX: str`
  - `class HeroQA(client, model: str, store: HeroStore, max_turns: int = MAX_TURNS)`
    - `run_tool(name: str, args: dict) -> dict`
    - `async answer(question: str) -> str`
  - `class Cooldown(seconds: float)`，`allow(user_id: int, now: float | None = None) -> bool`

- [ ] **Step 1: 写失败的测试**

先建 `tests/test_hero_qa.py`，内容是下面的完整文件，但**暂时**去掉 `import bot` 这一行、`test_routing`、`FakeChannel`、`FakeMessage`、`make_bot`、`test_ask` 这几段，以及 `__main__` 里对 `test_routing()`、`test_ask()` 的调用（Task 5 再加回来）：

```python
"""英雄问答测试：工具分发、tool use 循环（假 Claude 客户端）、bot 路由与冷却（假 Discord 消息）。

运行：python tests/test_hero_qa.py
"""
import asyncio
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import bot  # noqa: E402
from hero_qa import TOO_COMPLEX, Cooldown, HeroQA  # noqa: E402
from herodata import HeroStore, load_aliases  # noqa: E402
from heroparse import parse_hero  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "wiki")
results = []
NS = types.SimpleNamespace


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def make_store():
    store = HeroStore("unused.json", load_aliases())
    for name in ("Cassidy", "Tracer", "Reinhardt", "Moira", "Mauga"):
        with open(os.path.join(FIXTURES, name + ".wikitext"), encoding="utf-8") as f:
            store.heroes[name] = parse_hero(name, f.read())
    store.fetched_at = "2026-10-02T00:00:00Z"
    return store


def tool_use(id_, name, args):
    return NS(type="tool_use", id=id_, name=name, input=args)


def text(t):
    return NS(type="text", text=t)


class ScriptedClient:
    """按顺序返回预设的 Claude 响应，并记录每次请求。"""
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    async def create(self, **kw):
        self.requests.append(kw)
        return self.responses.pop(0)


def test_tools():
    qa = HeroQA(None, "m", make_store())
    stats = qa.run_tool("get_hero_stats", {"hero": "麦克雷"})
    check("get_hero_stats 用中文别名", stats["name"] == "Cassidy" and stats["fetched_at"] == "2026-10-02T00:00:00Z")
    check("get_hero_stats 不含补丁和 raw，武器带下标",
          "patches" not in stats and stats["weapons"][1]["index"] == 1 and "raw" not in stats["weapons"][0])
    check("未知英雄返回候选", qa.run_tool("get_hero_stats", {"hero": "xyz"}) ==
          {"error": "unknown_hero", "query": "xyz", "candidates": []})
    p = qa.run_tool("get_patch_history", {"hero": "Cassidy", "limit": 2})
    check("get_patch_history 限制条数、最新在前", len(p["patches"]) == 2 and p["patches"][0]["date"] == "2026-08-11")
    check("limit 超出范围被夹到 1..20", len(qa.run_tool("get_patch_history", {"hero": "Cassidy", "limit": 999})["patches"]) == 20
          and len(qa.run_tool("get_patch_history", {"hero": "Cassidy", "limit": 0})["patches"]) == 1)
    k = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "headshot": True})
    check("shots_to_kill 默认主武器 Peacekeeper，爆头 2 枪", k["weapon"] == "Peacekeeper" and k["shots"] == 2)
    k = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "weapon": "fan"})
    check("按名字片段选武器", k["weapon"] == "Fan the Hammer")
    k = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "weapon": "1"})
    check("按下标选武器", k["weapon"] == "Fan the Hammer")
    check("未知武器列出可选武器", qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "weapon": "rpg"})
          == {"error": "unknown_weapon", "weapons": ["Peacekeeper", "Fan the Hammer"]})
    k = qa.run_tool("shots_to_kill", {"attacker": "Reinhardt", "target": "Tracer"})
    check("只有近战武器的英雄：返回 unsupported", "unsupported" in k)
    k = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Mauga", "headshot": True, "mode": "open_queue"})
    check("模式参数传给计算器", k["mode"] == "open_queue")
    bad = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "distance": "30m"})
    check("距离不是数字时返回可读错误", "distance must be a number" in bad["error"])
    k = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "distance": "30"})
    check("字符串数字距离可以用", k["distance_m"] == 30.0 and k["shots"] == 4)
    check("未知工具", "error" in qa.run_tool("nope", {}))


def test_loop():
    client = ScriptedClient([
        NS(stop_reason="tool_use", content=[
            tool_use("t1", "shots_to_kill", {"attacker": "卡西迪", "target": "猎空", "headshot": True}),
            tool_use("t2", "get_patch_history", {"hero": "Cassidy", "limit": 1})]),
        NS(stop_reason="end_turn", content=[text("2 发爆头即可击杀猎空。")]),
    ])
    answer = asyncio.run(HeroQA(client, "test-model", make_store()).answer("卡西迪爆头几枪杀猎空？"))
    check("循环结束后返回最终文本", answer == "2 发爆头即可击杀猎空。")
    first = client.requests[0]
    check("请求带 system 和 3 个工具", "system" in first and [t["name"] for t in first["tools"]] ==
          ["get_hero_stats", "get_patch_history", "shots_to_kill"])
    second = client.requests[1]["messages"]
    check("第二轮带上 assistant 的 tool_use 和 user 的 tool_result",
          second[1]["role"] == "assistant" and second[2]["role"] == "user")
    tr = second[2]["content"]
    check("每个 tool_use 对应一个 tool_result", [r["tool_use_id"] for r in tr] == ["t1", "t2"])
    check("tool_result 是计算器的 JSON", json.loads(tr[0]["content"])["shots"] == 2)

    looping = ScriptedClient([NS(stop_reason="tool_use", content=[tool_use(f"t{i}", "get_hero_stats", {"hero": "Ana"})])
                              for i in range(5)])
    check("超过 5 轮返回 TOO_COMPLEX", asyncio.run(HeroQA(looping, "m", make_store()).answer("x")) == TOO_COMPLEX)

    class Boom(HeroQA):
        def run_tool(self, name, args):
            raise ValueError("bad input")
    client = ScriptedClient([
        NS(stop_reason="tool_use", content=[tool_use("t1", "get_hero_stats", {"hero": "Cassidy"})]),
        NS(stop_reason="end_turn", content=[text("sorry")]),
    ])
    asyncio.run(Boom(client, "m", make_store()).answer("x"))
    err = json.loads(client.requests[1]["messages"][2]["content"][0]["content"])
    check("工具抛异常时把错误交给模型，不中断", err == {"error": "tool failed: bad input"})


def test_cooldown():
    c = Cooldown(5)
    check("第一次允许", c.allow(1, now=0))
    check("5 秒内拒绝", not c.allow(1, now=3))
    check("其他用户不受影响", c.allow(2, now=3))
    check("过了 5 秒允许", c.allow(1, now=5.1))


def test_routing():
    check("空文本 → connect", bot.parse_command("  ") == ("connect", ""))
    check("指令忽略大小写", bot.parse_command(" Status ") == ("status", "Status"))
    check("disconnect / help 不变", bot.parse_command("disconnect")[0] == "disconnect" and bot.parse_command("help")[0] == "help")
    check("其他文本 → ask，保留原文", bot.parse_command("Was Cassidy nerfed?") == ("ask", "Was Cassidy nerfed?"))


class FakeChannel:
    def typing(self):
        class _Ctx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *a):
                return False
        return _Ctx()


class FakeMessage:
    def __init__(self, user_id=1):
        self.author = NS(id=user_id)
        self.channel = FakeChannel()
        self.replies = []

    async def reply(self, content):
        self.replies.append(content)


def make_bot(qa, store):
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)  # 不连 Discord，只测 _ask
    b.hero_qa, b.store, b.ask_cooldown = qa, store, Cooldown(5)
    return b


def test_ask():
    store = make_store()

    class FakeQA:
        def __init__(self, answer="x" * 3000, error=None):
            self.answer_text, self.error, self.questions = answer, error, []

        async def answer(self, q):
            self.questions.append(q)
            if self.error:
                raise self.error
            return self.answer_text

    m = FakeMessage()
    asyncio.run(make_bot(None, store)._ask(m, "q"))
    check("没有 API Key 时提示", "ANTHROPIC_API_KEY" in m.replies[0])
    m = FakeMessage()
    asyncio.run(make_bot(FakeQA(), HeroStore("unused.json"))._ask(m, "q"))
    check("数据未就绪时提示", "not ready" in m.replies[0])
    qa, m = FakeQA(), FakeMessage()
    b = make_bot(qa, store)
    asyncio.run(b._ask(m, "Cassidy HP?"))
    check("问题原样交给 HeroQA，回答截断到 1990 字符", qa.questions == ["Cassidy HP?"] and len(m.replies[0]) == 1990)
    asyncio.run(b._ask(m, "again"))
    check("冷却期内拒绝", "wait" in m.replies[1] and qa.questions == ["Cassidy HP?"])
    m = FakeMessage()
    asyncio.run(make_bot(FakeQA(error=RuntimeError("api down")), store)._ask(m, "q"))
    check("Claude 出错时回复简短错误", m.replies[0].startswith("Sorry"))


if __name__ == "__main__":
    test_tools()
    test_loop()
    test_cooldown()
    test_routing()
    test_ask()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/bin/python tests/test_hero_qa.py`
Expected: `ModuleNotFoundError: No module named 'hero_qa'`

- [ ] **Step 3: 实现 `server/hero_qa.py`**

```python
"""英雄问答：Claude 只负责理解问题和组织回答，所有数字来自工具（herodata 快照 + damage 计算）。"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import damage
from herodata import HeroStore

log = logging.getLogger("mrmeeseeks.hero_qa")

MAX_TURNS = 5
TOO_COMPLEX = "That question needs too many lookups. Try splitting it into smaller questions."

SYSTEM = """You are mrmeeseeks, an Overwatch hero data assistant in a Discord channel.
Rules:
1. Every number in your answer must come from a tool result in this conversation. Never use numbers from memory.
2. To say whether a hero was buffed or nerfed, call get_patch_history and quote the relevant patch lines with their dates. If the recent changes only touch perks, say so.
3. For damage or "how many shots to kill" questions, call shots_to_kill. If a weapon has several damage variants (e.g. charge levels), pick the one the user means via `variant` and say which one you used.
4. State the assumptions returned by the tools (e.g. all pellets hit, Role Queue health).
5. If a tool returns candidates for an unclear hero name, ask the user which hero they meant. Do not guess.
6. If a tool returns "unsupported", explain why. Time-to-kill, ability damage (non-weapon) and perk damage bonuses are not supported yet.
7. Answer in the same language as the question, in at most about 150 words.
8. End with one line: "Source: Overwatch Wiki, data fetched <fetched_at date>"."""

_HERO = {"type": "string", "description": "Hero name in any language, e.g. Cassidy, 卡西迪, McCree"}
TOOLS = [
    {
        "name": "get_hero_stats",
        "description": "Base stats of a hero: role, sub-role, health/armor/shield per mode, and weapon data "
                       "(damage variants, falloff range in meters, headshot, pellets, projectile radius in meters).",
        "input_schema": {"type": "object", "properties": {"hero": _HERO}, "required": ["hero"]},
    },
    {
        "name": "get_patch_history",
        "description": "Most recent balance changes for a hero (live PvP only), newest first, as quoted patch text.",
        "input_schema": {"type": "object", "properties": {
            "hero": _HERO,
            "limit": {"type": "integer", "description": "Number of patches, 1-20, default 5"},
        }, "required": ["hero"]},
    },
    {
        "name": "shots_to_kill",
        "description": "Damage per shot at a distance and the number of shots for attacker's weapon to kill target, "
                       "simulating shields, armor and Bruiser crit reduction. Returns a per-shot breakdown.",
        "input_schema": {"type": "object", "properties": {
            "attacker": _HERO,
            "target": _HERO,
            "weapon": {"type": "string", "description": "Weapon name or index from get_hero_stats; default is the first non-melee weapon"},
            "distance": {"type": "number", "description": "Distance in meters, default 0"},
            "headshot": {"type": "boolean", "description": "Every shot is a headshot; default false"},
            "mode": {"type": "string", "enum": list(damage.MODES), "description": "Default role_queue"},
            "variant": {"type": "integer", "description": "Index into the weapon's damage variants"},
        }, "required": ["attacker", "target"]},
    },
]


def _pick_weapon(hero: dict[str, Any], weapon: str | None) -> dict[str, Any] | None:
    weapons = hero["weapons"]
    if weapon is None or str(weapon).strip() == "":
        return next((w for w in weapons if "melee" not in w["shot_type"]), weapons[0] if weapons else None)
    if str(weapon).isdigit():
        i = int(weapon)
        return weapons[i] if i < len(weapons) else None
    q = str(weapon).casefold()
    return next((w for w in weapons if w["name"].casefold() == q), None) or \
        next((w for w in weapons if q in w["name"].casefold()), None)


class HeroQA:
    def __init__(self, client: Any, model: str, store: HeroStore, max_turns: int = MAX_TURNS):
        self.client = client
        self.model = model
        self.store = store
        self.max_turns = max_turns

    # ---------- 工具 ----------
    def _hero(self, name: str) -> dict[str, Any]:
        found = self.store.resolve(name)
        if isinstance(found, dict):
            return found
        return {"error": "unknown_hero", "query": name, "candidates": found}

    def run_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "get_hero_stats":
            hero = self._hero(args["hero"])
            if "error" in hero:
                return hero
            stats = {k: v for k, v in hero.items() if k != "patches"}
            stats["weapons"] = [{"index": i, **{k: v for k, v in w.items() if k != "raw"}}
                                for i, w in enumerate(hero["weapons"])]
            return {**stats, "fetched_at": self.store.fetched_at}
        if name == "get_patch_history":
            hero = self._hero(args["hero"])
            if "error" in hero:
                return hero
            limit = args.get("limit")
            limit = max(1, min(5 if limit is None else int(limit), 20))
            return {"hero": hero["name"], "patches": hero["patches"][:limit],
                    "source_url": hero["source_url"], "fetched_at": self.store.fetched_at}
        if name == "shots_to_kill":
            attacker, target = self._hero(args["attacker"]), self._hero(args["target"])
            for h in (attacker, target):
                if "error" in h:
                    return h
            weapon = _pick_weapon(attacker, args.get("weapon"))
            if weapon is None:
                return {"error": "unknown_weapon", "weapons": [w["name"] for w in attacker["weapons"]]}
            try:
                distance = float(args.get("distance") or 0)
                variant = None if args.get("variant") is None else int(args["variant"])
            except (TypeError, ValueError):
                return {"error": "distance must be a number of meters and variant an integer index"}
            result = damage.shots_to_kill(
                attacker, weapon, target, distance=distance,
                headshot=bool(args.get("headshot")),
                mode=args.get("mode") or "role_queue",
                variant=variant,
            )
            return {**result, "fetched_at": self.store.fetched_at}
        return {"error": f"unknown tool {name}"}

    # ---------- 对话循环 ----------
    async def answer(self, question: str) -> str:
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        for _ in range(self.max_turns):
            resp = await self.client.messages.create(
                model=self.model, max_tokens=1024, system=SYSTEM, tools=TOOLS, messages=messages)
            if resp.stop_reason != "tool_use":
                return "".join(b.text for b in resp.content if b.type == "text").strip()
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                try:
                    output = self.run_tool(block.name, block.input)
                except Exception as e:  # 工具出错交给模型解释，不中断整个回答
                    log.exception("Tool %s failed", block.name)
                    output = {"error": f"tool failed: {e}"}
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(output, ensure_ascii=False)})
            messages.append({"role": "user", "content": results})
        return TOO_COMPLEX


class Cooldown:
    """每个用户两次提问之间的最短间隔。"""
    def __init__(self, seconds: float):
        self.seconds = seconds
        self.last: dict[int, float] = {}

    def allow(self, user_id: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if now - self.last.get(user_id, float("-inf")) < self.seconds:
            return False
        self.last[user_id] = now
        return True
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/bin/python tests/test_hero_qa.py`
Expected: `25/25 passed`（日志里 "bad input" 的 traceback 是预期的）

- [ ] **Step 5: Commit**

```bash
git add server/hero_qa.py tests/test_hero_qa.py
git commit -m "Add hero Q&A tool-use loop over hero data and damage calculator"
```

---

### Task 5: 接入 bot、配置与文档

**Files:**
- Modify: `server/bot.py`
- Modify: `tests/test_hero_qa.py`（加回路由与 `_ask` 测试）
- Modify: `server/.env.example`、`Dockerfile`、`compose.yml`
- Modify: `README.md`、`docs/ARCHITECTURE.md`

**Interfaces:**
- Consumes: `HeroQA`、`Cooldown`（Task 4）；`HeroStore`、`load_aliases`、`refresh_loop`（Task 3）
- Produces:
  - `bot.parse_command(text: str) -> tuple[str, str]`：返回 `("connect"|"status"|"disconnect"|"help"|"ask", 原文去首尾空白)`
  - `MeeseeksBot(registry, analyzer, store: HeroStore | None = None, hero_qa: HeroQA | None = None)`（后两个参数可选，`tests/test_e2e.py` 里的 `MeeseeksBot(reg, None)` 保持可用）
  - `MeeseeksBot._ask(message, question) -> None`

- [ ] **Step 1: 把路由测试加回 `tests/test_hero_qa.py`**

恢复 Task 4 Step 1 里暂时去掉的部分：`import bot  # noqa: E402`、`test_routing`、`FakeChannel`、`FakeMessage`、`make_bot`、`test_ask`，以及 `__main__` 里的 `test_routing()` 和 `test_ask()`。恢复后文件与 Task 4 Step 1 中的完整文件一致。

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/bin/python tests/test_hero_qa.py`
Expected: `AttributeError: module 'bot' has no attribute 'parse_command'`

- [ ] **Step 3: 修改 `server/bot.py`**

把下面的补丁保存为 `/tmp/bot.diff`，然后执行 `git apply /tmp/bot.diff`。如果补丁应用失败（`bot.py` 在此期间被改过），就按补丁内容手动修改：

```diff
--- a/server/bot.py
+++ b/server/bot.py
@@ -5,6 +5,7 @@
   @mrmeeseeks status     查看所有在线客户端
   @mrmeeseeks disconnect 解绑当前频道里的客户端
   @mrmeeseeks help       显示帮助
+  @mrmeeseeks <问题>     英雄问答（数值、补丁、几枪击杀），见 hero_qa.py
 """
 from __future__ import annotations
 
@@ -15,9 +16,12 @@
 import time
 
 import discord
+from anthropic import AsyncAnthropic
 from dotenv import load_dotenv
 
 from analyzer import Analyzer, format_facts
+from hero_qa import Cooldown, HeroQA
+from herodata import HeroStore, load_aliases, refresh_loop
 from registry import CODE_TTL_SECONDS, ClientConn, PairingError, Registry
 from ws_server import WSServer
 
@@ -28,10 +32,25 @@
     "`@mrmeeseeks` or `@mrmeeseeks connect` Connect a local client (pick its ID and I'll DM you an 8-digit pairing code)\n"
     "`@mrmeeseeks status` List online clients\n"
     "`@mrmeeseeks disconnect` Unpair the client bound to this channel\n"
-    "`@mrmeeseeks help` Show this help"
+    "`@mrmeeseeks help` Show this help\n"
+    "`@mrmeeseeks <question>` Ask about heroes, e.g. `was Cassidy nerfed recently?`, "
+    "`Tracer HP`, `how many Cassidy headshots kill Mauga at 30m?`"
 )
+COMMANDS = ("connect", "status", "disconnect", "help")
+ASK_COOLDOWN_SECONDS = 5
 
 
+def parse_command(text: str) -> tuple[str, str]:
+    """去掉 @ 之后的文本 -> (指令, 原文)。空文本是 connect，不是已知指令的都当作英雄问答。"""
+    stripped = text.strip()
+    cmd = stripped.lower()
+    if cmd == "":
+        return "connect", stripped
+    if cmd in COMMANDS:
+        return cmd, stripped
+    return "ask", stripped
+
+
 def _age(seconds: float) -> str:
     minutes = int(seconds // 60)
     return f"{minutes} min" if minutes else f"{int(seconds)} s"
@@ -104,12 +123,16 @@
 
 # ---------------- Bot 本体 ----------------
 class MeeseeksBot(discord.Client):
-    def __init__(self, registry: Registry, analyzer: Analyzer | None):
+    def __init__(self, registry: Registry, analyzer: Analyzer | None,
+                 store: HeroStore | None = None, hero_qa: HeroQA | None = None):
         intents = discord.Intents.default()
         intents.message_content = True
         super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
         self.registry = registry
         self.analyzer = analyzer
+        self.store = store
+        self.hero_qa = hero_qa
+        self.ask_cooldown = Cooldown(ASK_COOLDOWN_SECONDS)
 
     async def on_ready(self) -> None:
         log.info("Logged in to Discord as %s", self.user)
@@ -127,17 +150,38 @@
         text = message.content
         for token in (f"<@{self.user.id}>", f"<@!{self.user.id}>"):
             text = text.replace(token, "")
-        cmd = text.strip().lower()
+        cmd, question = parse_command(text)
 
-        if cmd in ("", "connect"):
+        if cmd == "connect":
             await self._connect_flow(message)
         elif cmd == "status":
             await self._status(message)
         elif cmd == "disconnect":
             await self._disconnect(message)
-        else:
+        elif cmd == "help":
             await message.reply(HELP_TEXT)
+        else:
+            await self._ask(message, question)
 
+    async def _ask(self, message: discord.Message, question: str) -> None:
+        if self.hero_qa is None:
+            await message.reply("Hero Q&A needs ANTHROPIC_API_KEY to be set.")
+            return
+        if self.store is None or not self.store.ready:
+            await message.reply("Hero data is not ready yet, try again in a minute.")
+            return
+        if not self.ask_cooldown.allow(message.author.id):
+            await message.reply(f"Please wait {ASK_COOLDOWN_SECONDS} seconds between questions.")
+            return
+        try:
+            async with message.channel.typing():
+                answer = await self.hero_qa.answer(question)
+        except Exception:
+            log.exception("Hero Q&A failed for %r", question)
+            await message.reply("Sorry, I couldn't answer that right now. Please try again later.")
+            return
+        await message.reply(answer[:1990] or "I couldn't come up with an answer.")
+
     async def _connect_flow(self, message: discord.Message) -> None:
         clients = self.registry.available()
         if not clients:
@@ -217,8 +261,15 @@
     else:
         log.info("Analysis model: %s", model)
 
+    server_dir = os.path.dirname(os.path.abspath(__file__))
+    store = HeroStore(os.environ.get("HERO_DATA_PATH", "").strip() or os.path.join(server_dir, "data", "heroes.json"),
+                      load_aliases())
+    store.load()
+    hero_qa = HeroQA(AsyncAnthropic(api_key=api_key), model, store) if api_key else None
+    refresh_hours = float(os.environ.get("HERO_REFRESH_HOURS", "24"))
+
     registry = Registry()
-    bot = MeeseeksBot(registry, analyzer)
+    bot = MeeseeksBot(registry, analyzer, store, hero_qa)
     ws = WSServer(
         registry, bot,
         host=os.environ.get("WS_HOST", "0.0.0.0"),
@@ -228,6 +279,7 @@
 
     async with bot:
         await ws.start()
+        refresher = asyncio.create_task(refresh_loop(store, refresh_hours))
         try:
             await bot.start(token)
         except discord.LoginFailure:
@@ -235,6 +287,7 @@
         except discord.PrivilegedIntentsRequired:
             raise SystemExit("Discord refused the connection: enable Message Content Intent on the Bot page of the Developer Portal.")
         finally:
+            refresher.cancel()
             await ws.close()
 
 
```

改动要点：
- 不认识的 @ 文本原来回复帮助，现在交给 `_ask`；`help` 现在必须明确写出。
- `_ask` 依次检查：有没有 API Key → 数据是否就绪 → 是否在冷却期 → 调用 `HeroQA`；异常时回复简短错误并记日志。
- 启动时先读缓存，再起一个后台任务 `refresh_loop`：立即刷新一次，之后每 `HERO_REFRESH_HOURS` 小时刷新；退出时取消这个任务。

- [ ] **Step 4: 运行全部测试，确认通过**

```bash
.venv/bin/python tests/test_hero_qa.py   # Expected: 34/34 passed
.venv/bin/python tests/test_e2e.py       # Expected: 原有用例全部通过（MeeseeksBot(reg, None) 仍可构造）
.venv/bin/python tests/test_units.py     # Expected: 14/14 passed
```

- [ ] **Step 5: 配置文件**

`server/.env.example` 末尾加：

```
# 英雄问答：英雄数据缓存文件，留空默认 server/data/heroes.json
HERO_DATA_PATH=
# 每隔多少小时从 Overwatch Wiki 刷新一次英雄数据
HERO_REFRESH_HOURS=24
```

`Dockerfile`：把

```
RUN useradd -r -u 10001 meeseeks
USER meeseeks
```

改成

```
RUN useradd -r -u 10001 meeseeks && mkdir -p /app/data && chown meeseeks /app/data
USER meeseeks
```

`compose.yml`：容器是 `read_only: true`，缓存需要可写的卷。在 `mrmeeseeks` 服务的 `ports` 后面加：

```yaml
    volumes:
      - herodata:/app/data
```

并在文件末尾加顶层：

```yaml
volumes:
  herodata:
```

验证：`docker compose config` 没有报错（本机没有 Docker 就跳过，并在汇报里写明跳过了）。

- [ ] **Step 6: 文档**

`README.md`「Discord 指令」表格最后加一行：

```
| `@mrmeeseeks <问题>` | 英雄问答：数值（血量、子弹体积）、最近的补丁、N 米处几枪击杀。例如 `@mrmeeseeks 卡西迪最近被削了吗`、`@mrmeeseeks 卡西迪 30 米爆头几枪杀毛加`。需要 `ANTHROPIC_API_KEY` |
```

`README.md`「配置参考 → 服务器 `server/.env`」表格最后加两行：

```
| `HERO_DATA_PATH` | 否 | `server/data/heroes.json` | 英雄数据缓存文件。Docker 部署时位于 `herodata` 卷 |
| `HERO_REFRESH_HOURS` | 否 | `24` | 每隔多少小时从 Overwatch Wiki 刷新英雄数据 |
```

`README.md`「运行测试」的代码块改成：

```bash
pip install -r server/requirements.txt
python tests/test_units.py     # 分析器两步流程、Tab 截图时序
python tests/test_e2e.py       # 真实服务器 + 真实客户端：握手、配对、错码、过期、截图转发、限流
python tests/test_heroparse.py # Wiki 英雄页面解析
python tests/test_damage.py    # 伤害衰减、护甲、几枪击杀
python tests/test_herodata.py  # 英雄数据缓存、刷新、名字解析
python tests/test_hero_qa.py   # 英雄问答工具与对话循环、bot 路由
```

`docs/ARCHITECTURE.md`「模块」表格在 `analyzer.py` 行后加：

```
| `server/heroparse.py` | 把 Overwatch Wiki 英雄页面的 wikitext 解析成规范化 dict（纯函数） |
| `server/herodata.py` | 英雄数据：从 Wiki 抓取、缓存到 JSON、定时刷新、按中英文名查英雄 |
| `server/damage.py` | 伤害衰减、护甲/护盾、爆头倍率、几枪击杀（纯函数） |
| `server/hero_qa.py` | 英雄问答：Claude tool use 循环，数字只来自上面两个模块 |
```

`docs/ARCHITECTURE.md` 在「扩展点」之前加一节：

```markdown
## 英雄问答

`@mrmeeseeks` 后面跟的文本如果不是 connect/status/disconnect/help，就交给 `HeroQA.answer`。

1. **数据**：`herodata.fetch_all` 通过 MediaWiki API 抓取 `Category:Heroes` 下每个英雄的页面，`heroparse.parse_hero` 解析 infobox（血量/护甲/护盾/副职业）、`Ability_details` 中的武器、`ChangelogsTabber` 的 `owpvp` 补丁。快照写到 `HERO_DATA_PATH`；新快照明显变少（Wiki 改版）时不替换旧的。
2. **工具**：`get_hero_stats`、`get_patch_history`、`shots_to_kill`。英雄名在工具内解析，不唯一时返回候选，由模型反问。
3. **计算**：`damage.shots_to_kill` 逐发模拟 护盾 → 护甲 → 生命值，规则常量和 Wiki 出处写在 `damage.py` 顶部。
4. **约束**：系统提示要求所有数字来自工具结果、引用补丁原文和日期、写出假设（弹丸全中、Role Queue 等）。

本期不支持：TTK、技能伤害、perk 加成。
```

并把「扩展点」里 `版本数据` 那一行改成：

```
- `版本数据`：`herodata.HeroStore` 已有当前版本的英雄数值和补丁，可以直接作为 `patch_data` 接入
```

- [ ] **Step 7: 手动端到端验证（需要 Discord Token 和 API Key）**

```bash
cd server && ../.venv/bin/python bot.py
```

日志里应先出现 `Hero data refreshed: N heroes`（约 1 分钟）。然后在 Discord 里依次发：

1. `@mrmeeseeks 卡西迪最近被削弱过吗` → 引用补丁日期和原文，说明是否只改了 perk
2. `@mrmeeseeks Tracer HP and bullet size` → 175 血、0.04 m，英文回答
3. `@mrmeeseeks 卡西迪 30 米爆头几枪杀毛加` → 说明 30 米衰减后的单发伤害、Bruiser 减伤和枪数
4. `@mrmeeseeks so 的血量` → 反问是 Sojourn、Soldier: 76 还是 Sombra
5. `@mrmeeseeks status` → 原有行为不变

每条回答结尾都应有 "Source: Overwatch Wiki, data fetched …"。没有条件做这一步就跳过，并在汇报里写明跳过了。

- [ ] **Step 8: Commit**

```bash
git add server/bot.py server/.env.example tests/test_hero_qa.py Dockerfile compose.yml README.md docs/ARCHITECTURE.md
git commit -m "Route unrecognized @mentions to hero Q&A and refresh hero data in the background"
```
