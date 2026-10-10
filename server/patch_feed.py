"""补丁推送频道：定时检查官方补丁说明，有新补丁就用 Claude 写成摘要发到频道。

摘要格式：全部是要点列表，改动内容用英文；英雄、武器、技能、威能名写成「英文/简体中文/韩文」。
每个英雄下面固定分 Basic Info / Abilities / Perks 三项（normalize_sections 兜底补 "None"），
英雄名前面是头像表情（hero_emojis.py）。
模型只写 [[Hero]] / [[Hero|Ability]] 标签，由 fill_names 换成官方页面上的中文、韩文名称
（patchnotes.fetch_translations），缺哪种语言就留空，模型碰不到中韩文名称，也就没法写错。

同时把最近几次官方补丁留在内存（和状态文件）里，供英雄问答的 get_official_patch_notes 使用：
官方补丁当天就有，Wiki 的英雄页要过几天才更新。

没有设置频道时照常记账、不发送；第一次启动只记下已有的补丁，不补发旧补丁。
`@mrmeeseeks patches here` 设置频道时会立刻发一次最新的补丁。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import patchnotes
from keybinds import KeyBinds, name_key

log = logging.getLogger("mrmeeseeks.patches")

CHECK_INTERVAL = 3 * 3600     # 每 3 小时检查一次官方补丁页
RETRY_INTERVAL = 600          # 抓取或发送失败后 10 分钟重试
MAX_MESSAGE = 1990
MAX_TOKENS = 16000            # adaptive thinking 也计入 max_tokens，见 hero_qa.py
SUMMARY_INPUT_LIMIT = 40000   # 补丁原文最多喂给模型多少字符

Send = Callable[[int, str], Awaitable[None]]

SUMMARY_FORMAT = "v4"          # 摘要格式变了就改它，旧格式的缓存不再使用
SECTIONS = ("Basic Info", "Abilities", "Perks")
SUMMARY_SYSTEM = """You write the patch-notes post for an Overwatch community Discord server whose members \
read English, Chinese and Korean. Summarize the official patch notes you are given.

Format: Discord markdown, bullet points only (no paragraphs), in English, under 4000 characters:
- First bullet: what this patch is (new season, balance patch, bug fixes, ...).
**New content**: one bullet per new season, hero, mode, map or event. Omit if none.
**Hero changes**: one top-level bullet per hero, starting with ⬆️ (buff), ⬇️ (nerf) or 🔄 (rework or \
mixed), then the hero's name tag in bold, e.g. "- 🔄 **[[Roadhog]]**". Under every hero, always these \
three sub-bullets in this order, even when empty:
  - **Basic Info**: changes to the hero itself (role, health, armor, movement, ultimate cost, hit \
volumes, ...).
  - **Abilities**: weapons, abilities, ultimate and passive.
  - **Perks**: minor and major perks.
Write a sub-bullet with nothing in it as "  - **Perks**: None". Otherwise put one nested bullet \
("    - ") per change under it, even when there is only one: the name tag, then the change in English \
with numbers (from → to), e.g. "    - [[Roadhog|Chain Hook]]: cooldown 7s → 6s (5v5)". Keep each nested \
line under 160 characters: only the most important numbers, not every detail. Mark perks like \
"(Minor, new)" or "(Major, removed)" and other new or removed abilities "(new)" or "(removed)". Basic Info changes have no name tag. The list below says which \
names are abilities and which are perks. Only list heroes with changes other than hit volumes; if \
there is a hit-volume pass, mention it once under Other. Put a blank line between heroes.
**Other**: at most 3 bullets for the most noticeable remaining changes. Omit if none.

Name tags: never write a hero, weapon, ability or perk name as plain text, not even inside a change \
description. Write [[Hero]] for a hero and [[Hero|Name]] for one of its weapons, abilities or perks, \
copying the English names exactly from the list below, e.g. "    - [[Shion|Survival Instinct]] (Minor, \
new): [[Shion|Evade]] overhealth duration +1s". When you mention another hero's ability, name that hero \
too: "attaching to [[Jetpack Cat]]'s [[Jetpack Cat|Lifeline]]". Tags are replaced with \
English/Chinese/Korean names, default key bindings and hero icons for you, so never write key bindings \
or "(Passive)" yourself. Maps, modes and \
events stay plain English.
Rules: only use facts and numbers from the notes. No opinions or tier predictions. No greeting, no \
sign-off, no links (the title and link are added for you).

Heroes in this patch:
{names}"""
_TAG = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")


def new_state() -> dict[str, Any]:
    return {"channel_id": None, "seen": [], "initialized": False, "patches": [], "summaries": {},
            "checked_at": None}


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """切成不超过 limit 的几段：优先在空行（英雄之间）断开，段落太长再按行，单行太长硬切。"""
    def by_lines(block: str) -> list[str]:
        out, cur = [], ""
        for line in block.split("\n"):
            while len(line) > limit:
                if cur:
                    out.append(cur)
                    cur = ""
                out.append(line[:limit])
                line = line[limit:]
            candidate = f"{cur}\n{line}" if cur else line
            if len(candidate) > limit:
                out.append(cur)
                cur = line
            else:
                cur = candidate
        return out + ([cur] if cur else [])

    chunks, cur = [], ""
    for block in text.split("\n\n"):
        candidate = f"{cur}\n\n{block}" if cur else block
        if len(candidate) <= limit:
            cur = candidate
            continue
        if cur:
            chunks.append(cur)
        pieces = by_lines(block) if len(block) > limit else [block]
        chunks += pieces[:-1]
        cur = pieces[-1]
    return chunks + ([cur] if cur else [])


# 第一个中文别名不是官方中文名的英雄（宋哈娜是 D.Va 的本名，国服就叫 D.Va）
NAME_OVERRIDES = {"D.Va": "D.Va"}


def chinese_names(aliases: dict[str, list[str]]) -> dict[str, str]:
    """aliases.json -> {英文名: 第一个中文别名}，再套用 NAME_OVERRIDES。"""
    names = {}
    for hero, alts in aliases.items():
        zh = next((a for a in alts if re.search(r"[一-鿿]", a)), None)
        if zh:
            names[hero] = zh
    return {**names, **NAME_OVERRIDES}


def fallback_text(patch: dict[str, Any]) -> str:
    """没有摘要（没配 API Key 或摘要失败）时的简短版本：只列改动的英雄。"""
    heroes = ", ".join(sorted(patch["heroes"])) or "none"
    return f"Heroes changed: {heroes}"


def render(patch: dict[str, Any], summary: str | None) -> str:
    body = summary or fallback_text(patch)
    return f"🛠️ **{patch['title']}**\n\n{body}\n\n🔗 Full patch notes: <{patch['url']}>"


def name_list(patch: dict[str, Any], kit: Callable[[str], list[str]] | None = None) -> str:
    """给模型看的英文名单：每个英雄一行，按官方结构分出技能（带图标的条目）和威能；
    kit(英雄) 给出它的全部技能（Wiki），描述里提到没改动的技能（如 Evade）时也能打标签。"""
    lines = []
    for hero in patch["heroes"]:
        abilities, perks = [], []
        for block in patch.get("blocks", []):
            if block["hero"] != hero:
                continue
            for entry in block["entries"]:
                name, suffix = patchnotes.split_perk(entry["name"])
                if patchnotes.entry_kind(entry) == "perk":
                    perks.append(f"{name} ({suffix.split()[0]})" if suffix and suffix.split()[0] in ("Minor", "Major")
                                 else name)
                else:
                    abilities.append(name)
        # 官方 "Scrap Gun – Primary Fire" 改动了，就不再提供 Wiki 的 "Scrap Gun" / "Scrap Gun Alt Fire"，免得模型挑错名字
        changed = {name_key(_FIRE_MODE.sub("", a)) for a in abilities}
        others = [a for a in (kit(hero) if kit else [])
                  if not any(name_key(a).startswith(c) or c.startswith(name_key(a)) for c in changed)]
        parts = [f"Abilities: {' | '.join(dict.fromkeys(abilities))}" if abilities else "",
                 f"Perks: {' | '.join(dict.fromkeys(perks))}" if perks else "",
                 f"Other abilities: {' | '.join(dict.fromkeys(others))}" if others else ""]
        detail = "; ".join(p for p in parts if p)
        lines.append(f"{hero} — {detail}" if detail else hero)
    return "\n".join(lines) or "(none)"


_FIRE_MODE = re.compile(r"\s*(?:–|-|——)\s*(?:Primary|Secondary) Fire\s*$", re.I)
_SPLIT_FIRE = re.compile(r"\[\[([^\]|]+)\|([^\]]+?)\]\]\s*\((Primary|Secondary) Fire\)", re.I)
_PASSIVE_TWICE = re.compile(r"`Passive`( \((?:new )?passive\))", re.I)
_HERO_BULLET = re.compile(r"^- .*\*\*\s*\[\[([^\]|]+)\]\]\s*\*\*")
_SECTION = re.compile(r"^\s*-\s*\*\*\s*(basic info|abilities|perks)\s*\*\*\s*:?\s*(.*)$", re.I)
_FIRST_TAG = re.compile(r"\[\[([^\]|]+?)\|([^\]]+?)\]\]")


def entry_kinds(patch: dict[str, Any]) -> dict[str, dict[str, str]]:
    """{英雄: {名称键: "Abilities" 或 "Perks"}}，按官方页面结构（见 patchnotes.entry_kind）。"""
    kinds: dict[str, dict[str, str]] = {}
    for block in patch.get("blocks", []):
        for entry in block["entries"]:
            name = patchnotes.split_perk(entry["name"])[0]
            perk = patchnotes.entry_kind(entry) == "perk"
            kinds.setdefault(block["hero"], {})[name_key(name)] = "Perks" if perk else "Abilities"
    return kinds


def normalize_sections(text: str, kinds: dict[str, dict[str, str]] | None = None) -> str:
    """每个英雄下面固定 Basic Info / Abilities / Perks 三项、这个顺序，没有内容的写 "None"。

    - 写在标题同一行的改动挪到下一级要点，统一格式；
    - 按官方结构（kinds）把放错大类的技能/威能挪回去，不带标签的行算 Basic Info。
    只处理 **Hero changes** 段里、名字是 [[Hero]] 标签的英雄条目（在替换名称之前调用）。"""
    kinds = kinds or {}
    lines, out, in_heroes, i = text.split("\n"), [], False, 0
    while i < len(lines):
        line = lines[i]
        if re.fullmatch(r"\*\*[^*]+\*\*:?", line.strip()) and not line.startswith(" "):
            in_heroes = "hero changes" in line.lower()
        hero = _HERO_BULLET.match(line) if in_heroes else None
        if hero is None:
            out.append(line)
            i += 1
            continue
        j = i + 1
        while j < len(lines) and lines[j].startswith(" "):
            j += 1
        hero_kinds = kinds.get(hero.group(1).strip(), {})
        items: dict[str, list[str]] = {name: [] for name in SECTIONS}
        current = last = "Basic Info"

        def add(item: str) -> None:
            nonlocal last
            tag = _FIRST_TAG.search(item)
            last = (hero_kinds.get(name_key(tag.group(2))) if tag else None) or current
            items[last].append(item)

        for child in lines[i + 1:j]:
            m = _SECTION.match(child)
            if m:
                current = last = next(s for s in SECTIONS if s.lower() == m.group(1).lower())
                rest = m.group(2).strip()
                if rest and rest.rstrip(".").lower() != "none":
                    add(f"    - {rest}")      # 写在标题同一行的改动挪到下一级
                continue
            if not child.strip():
                continue
            if child.startswith("      "):        # 更深一层的说明跟着上一项（它可能被挪到别的大类）
                items[last].append(child)
                continue
            body = child.strip()
            add(f"    {body}" if body.startswith("- ") else f"    - {body}")
        out.append(line)
        for name in SECTIONS:
            out += [f"  - **{name}**"] + items[name] if items[name] else [f"  - **{name}**: None"]
        i = j
    return "\n".join(out)


def trilingual(en: str, tr: dict[str, str] | None, zh_fallback: str = "") -> str:
    tr = tr or {}
    return f"{en}/{tr.get('zh') or zh_fallback}/{tr.get('ko', '')}"


def fill_names(text: str, table: dict[str, Any], fallback_zh: dict[str, str] | None = None,
               icons: dict[str, str] | None = None, keys: Callable[[str, str], str | None] | None = None,
               kinds: dict[str, dict[str, str]] | None = None) -> tuple[str, list[str]]:
    """把 [[Hero]]、[[Hero|Name]] 换成「英文/中文/韩文」，中韩文只取对照表里的官方名称；英雄名前加头像表情，
    技能名后加默认键位（keys，威能不加）。

    一条改动开头的技能名（标题）总是写成三语，缺的语言留空；描述里顺带提到的技能有译名才写三语，
    没有就只写英文，免得一串空斜杠。
    国服页面没对上时英雄中文名退回 aliases.json（fallback_zh）。
    返回 (替换后的文本, 对照表里找不到的标签)。"""
    fallback_zh, icons, kinds = fallback_zh or {}, icons or {}, kinds or {}
    heroes, names = table.get("heroes", {}), table.get("names", {})
    unknown: list[str] = []

    def entry_in(hero_names: dict[str, dict[str, str]], name: str) -> dict[str, str] | None:
        if name in hero_names:
            return hero_names[name]
        key = name_key(name)          # 模型偶尔省掉 "(Passive)" 这类括号
        return next((t for n, t in hero_names.items() if name_key(n) == key), None)

    def entry_anywhere(name: str) -> dict[str, str] | None:
        hits = [t for t in (entry_in(h, name) for h in names.values()) if t is not None]
        return hits[0] if len(hits) == 1 else None

    def owner(name: str) -> str | None:
        hits = [h for h, t in names.items() if entry_in(t, name) is not None]
        return hits[0] if len(hits) == 1 else None

    def ability(hero: str | None, name: str, tr: dict[str, str] | None, heading: bool) -> str:
        shown = trilingual(name, tr) if heading or (tr and (tr.get("zh") or tr.get("ko"))) else name
        perk = hero is not None and kinds.get(hero, {}).get(name_key(name)) == "Perks"
        key = keys(hero, name) if keys and hero and not perk else None
        return f"{shown} `{key}`" if key else shown

    def sub(m: re.Match) -> str:
        first, second = m.group(1).strip(), (m.group(2) or "").strip()
        line_start = text.rfind("\n", 0, m.start()) + 1
        heading = re.fullmatch(r"\s*- ", text[line_start:m.start()]) is not None
        if not second:
            if first in heroes:
                name = trilingual(first, heroes[first], fallback_zh.get(first, ""))
                return f"{icons[first]} {name}" if first in icons else name
            tr = entry_anywhere(first)     # 模型偶尔把技能写成 [[Name]]
            if tr is None:
                unknown.append(m.group(0))
                return trilingual(first, None, fallback_zh.get(first, ""))
            return ability(owner(first), first, tr, heading)
        tr = entry_in(names.get(first, {}), second) or entry_anywhere(second)
        if tr is None and heading:      # 描述里提到本次没改动的技能（如 Evade）本来就不在对照表里
            unknown.append(m.group(0))
        return ability(first, second, tr, heading)

    # 模型常把 "Scrap Gun – Primary Fire" 拆成 "[[Roadhog|Scrap Gun]] (Primary Fire)"，先合回官方全名
    text = _SPLIT_FIRE.sub(lambda m: f"[[{m.group(1)}|{m.group(2)} – {m.group(3).title()} Fire]]", text)
    # 模型有时在被动技能后面再写一遍 "(Passive)"，和键位重复
    filled = _PASSIVE_TWICE.sub(lambda m: "`Passive`" + (" (new)" if "new" in m.group(1).lower() else ""), _TAG.sub(sub, text))
    return filled, unknown


class PatchSummarizer:
    def __init__(self, client, model: str, effort: str | None = None, fallback_zh: dict[str, str] | None = None,
                 keybinds: KeyBinds | None = None):
        self.client = client
        self.model = model
        self.effort = effort
        self.fallback_zh = fallback_zh or {}   # aliases.json 的中文英雄名，国服页面缺失时用
        self.keybinds = keybinds or KeyBinds()

    def _kit(self, hero: str) -> list[str]:
        """英雄的全部技能名（Wiki），给模型在描述里打标签用。"""
        store = self.keybinds.store
        found = store.resolve(hero) if store is not None else None
        return [a["name"] for a in found.get("abilities", [])] if isinstance(found, dict) else []

    async def summarize(self, patch: dict[str, Any]) -> str:
        """模型写的原始摘要：名称还是 [[...]] 标签，由 finish() 在发送时替换。"""
        system = SUMMARY_SYSTEM.format(names=name_list(patch, self._kit))
        notes = f"{patch['title']}\n\n{patch['text']}"[:SUMMARY_INPUT_LIMIT]
        resp = await self.client.messages.create(
            model=self.model, max_tokens=MAX_TOKENS, system=system,
            messages=[{"role": "user", "content": notes}],
            **({"output_config": {"effort": self.effort}} if self.effort else {}))
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if resp.stop_reason == "max_tokens" or not text:
            raise RuntimeError(f"no summary text (stop_reason={resp.stop_reason})")
        return text

    def finish(self, patch: dict[str, Any], raw: str, table: dict[str, Any], icons: dict[str, str] | None = None) -> str:
        """补全三个分类，再把标签换成三语名称、键位和头像。"""
        kinds = entry_kinds(patch)
        text, unknown = fill_names(normalize_sections(raw, kinds), table, self.fallback_zh, icons,
                                   self.keybinds.for_patch(patch), kinds)
        if unknown:
            log.warning("Summary of %s used names not in the name table: %s", patch["id"], ", ".join(unknown[:10]))
        return text


class PatchFeed:
    def __init__(self, path: str,
                 fetch: Callable[[], Awaitable[list[dict[str, Any]]]] = patchnotes.fetch_recent,
                 summarizer: PatchSummarizer | None = None,
                 translate: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] = patchnotes.fetch_translations,
                 icons: Callable[[dict[str, Any]], Awaitable[dict[str, str]]] | None = None):
        self.path = path
        self.fetch = fetch
        self.summarizer = summarizer
        self.translate = translate
        self.icons = icons            # 补丁 -> {英雄: 头像表情}，见 hero_emojis.py；None 表示不加头像
        self.state = new_state()
        self.lock = asyncio.Lock()     # 定时检查和 `patches here` 不能同时发同一个补丁

    # ---------- 状态 ----------
    @property
    def channel_id(self) -> int | None:
        return self.state["channel_id"]

    @property
    def patches(self) -> list[dict[str, Any]]:
        """最近的官方补丁，新的在前。"""
        return self.state["patches"]

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            state = new_state()
            state.update({k: data[k] for k in state if k in data})
            self.state = state
        except (OSError, ValueError, TypeError) as e:
            log.info("No usable patch state at %s (%r)", self.path, e)

    def save(self) -> None:
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write patch state to %s", self.path)

    def set_channel(self, channel_id: int | None) -> None:
        self.state["channel_id"] = channel_id
        self.save()

    # ---------- 抓取与发送 ----------
    async def refresh(self) -> bool:
        try:
            patches = await self.fetch()
        except Exception:
            log.exception("Patch notes fetch failed; keeping previous data")
            return False
        self.state["patches"] = patches
        self.state["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ids = {p["id"] for p in patches}
        keep = {self._key(i) for i in ids}    # 也顺便丢掉旧格式的摘要
        self.state["summaries"] = {k: v for k, v in self.state["summaries"].items() if k in keep}
        if not self.state["initialized"]:   # 第一次只记账，不补发旧补丁
            self.state["seen"] = sorted(ids)
            self.state["initialized"] = True
            log.info("Patch feed initialized with %d existing patches", len(ids))
        self.save()
        return True

    @staticmethod
    def _key(patch_id: str) -> str:
        return f"{SUMMARY_FORMAT}:{patch_id}"

    async def text_for(self, patch: dict[str, Any]) -> str:
        """模型摘要和名称对照表只生成一次并缓存，每次发送时再填名称和头像（头像表情可能是后来才有的）。
        失败时用只列英雄的简短版本（不缓存，下次再试）。"""
        if self.summarizer is None:
            return render(patch, None)
        cached = self.state["summaries"].get(self._key(patch["id"]))
        if cached is None:
            try:
                try:
                    table = await self.translate(patch)
                except Exception:
                    log.exception("Could not load Chinese/Korean names for %s; leaving them empty", patch["id"])
                    table = patchnotes.translations(patch)
                cached = {"raw": await self.summarizer.summarize(patch), "table": table}
                self.state["summaries"][self._key(patch["id"])] = cached
            except Exception:
                log.exception("Could not summarize %s; posting the short version", patch["id"])
                return render(patch, None)
        icons = {}
        if self.icons is not None:
            try:
                icons = await self.icons(patch)
            except Exception:
                log.exception("Could not load hero icons for %s; posting without them", patch["id"])
        return render(patch, self.summarizer.finish(patch, cached["raw"], cached["table"], icons))

    async def _post(self, channel_id: int, patch: dict[str, Any], send: Send) -> None:
        for chunk in split_message(await self.text_for(patch)):
            await send(channel_id, chunk)

    def _mark_seen(self, patch_id: str) -> None:
        if patch_id not in self.state["seen"]:
            self.state["seen"].append(patch_id)
        self.state["seen"] = self.state["seen"][-50:]
        self.save()

    async def check(self, send: Send) -> bool:
        """抓一次；把没发过的补丁按从旧到新发出去。返回 False 表示要早点重试。"""
        async with self.lock:
            if not await self.refresh():
                return False
            for patch in sorted(self.patches, key=lambda p: p["date"]):
                if patch["id"] in self.state["seen"]:
                    continue
                if self.channel_id is not None:
                    try:
                        await self._post(self.channel_id, patch, send)
                    except Exception:
                        log.exception("Could not post %s to channel %s; will retry", patch["id"], self.channel_id)
                        return False
                    log.info("Posted %s", patch["id"])
                self._mark_seen(patch["id"])
            return True

    async def post_latest(self, channel_id: int, send: Send) -> dict[str, Any] | None:
        """设置频道后立刻发最新的一个补丁；还没抓过就先抓。"""
        async with self.lock:
            if not self.patches and not await self.refresh():
                return None
            if not self.patches:
                return None
            latest = self.patches[0]
            await self._post(channel_id, latest, send)
            self._mark_seen(latest["id"])
            return latest

    async def run(self, send: Send) -> None:
        while True:
            try:
                ok = await self.check(send)
            except Exception:
                log.exception("Patch feed check failed")
                ok = False
            await asyncio.sleep(CHECK_INTERVAL if ok else RETRY_INTERVAL)
