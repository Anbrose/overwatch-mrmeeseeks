"""补丁推送频道：定时检查官方补丁说明，有新补丁就用 Claude 写成摘要发到频道。

摘要格式：全部是要点列表，改动内容用英文；英雄、武器、技能、威能名写成「英文/简体中文/韩文」。
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

log = logging.getLogger("mrmeeseeks.patches")

CHECK_INTERVAL = 3 * 3600     # 每 3 小时检查一次官方补丁页
RETRY_INTERVAL = 600          # 抓取或发送失败后 10 分钟重试
MAX_MESSAGE = 1990
MAX_TOKENS = 16000            # adaptive thinking 也计入 max_tokens，见 hero_qa.py
SUMMARY_INPUT_LIMIT = 40000   # 补丁原文最多喂给模型多少字符

Send = Callable[[int, str], Awaitable[None]]

SUMMARY_FORMAT = "v2"          # 摘要格式变了就改它，旧格式的缓存不再使用
SUMMARY_SYSTEM = """You write the patch-notes post for an Overwatch community Discord server whose members \
read English, Chinese and Korean. Summarize the official patch notes you are given.

Format: Discord markdown, bullet points only (no paragraphs), in English, under 3500 characters:
- First bullet: what this patch is (new season, balance patch, bug fixes, ...).
**New content**: one bullet per new season, hero, mode, map or event. Omit if none.
**Hero changes**: one top-level bullet per hero, starting with ⬆️ (buff), ⬇️ (nerf) or 🔄 (rework or \
mixed), then the hero's name tag in bold, e.g. "- 🔄 **[[Roadhog]]**". Under it, one nested bullet \
("  - ") per changed weapon, ability or perk: its name tag, then the change in English with numbers \
(from → to), e.g. "  - [[Roadhog|Chain Hook]]: cooldown 7s → 6s (5v5)". Add "(Minor Perk)" or \
"(Major Perk)" after a perk's tag, "(new)" or "(removed)" where it applies. Leave hit-volume-only \
changes out of these bullets; if there is a hit-volume pass, add one bullet for it. Put a blank line \
between heroes.
**Other**: at most 3 bullets for the most noticeable remaining changes. Omit if none.

Name tags: never write a hero, weapon, ability or perk name as plain text. Write [[Hero]] for a hero and \
[[Hero|Name]] for one of its weapons, abilities or perks, copying the English names exactly from the \
list below; they are replaced with English/Chinese/Korean names for you. Maps, modes and events stay \
plain English.
Rules: only use facts and numbers from the notes. No opinions or tier predictions. No greeting, no \
sign-off, no links (the title and link are added for you).

Heroes in this patch and their changed weapons, abilities and perks:
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


def name_list(patch: dict[str, Any], table: dict[str, Any]) -> str:
    """给模型看的英文名单：每个英雄一行，后面跟这次改动到的武器/技能/威能（威能已去掉后缀）。"""
    lines = []
    for hero in patch["heroes"]:
        entries = table.get("names", {}).get(hero, {})
        lines.append(f"{hero}: {' | '.join(entries)}" if entries else hero)
    return "\n".join(lines) or "(none)"


def trilingual(en: str, tr: dict[str, str] | None, zh_fallback: str = "") -> str:
    tr = tr or {}
    return f"{en}/{tr.get('zh') or zh_fallback}/{tr.get('ko', '')}"


def fill_names(text: str, table: dict[str, Any], fallback_zh: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """把 [[Hero]]、[[Hero|Name]] 换成「英文/中文/韩文」，中韩文只取对照表里的官方名称。

    国服页面没对上时英雄中文名退回 aliases.json（fallback_zh）；对照表里没有的名称只写英文、留空其余。
    返回 (替换后的文本, 对照表里找不到的标签)。"""
    fallback_zh = fallback_zh or {}
    heroes, names = table.get("heroes", {}), table.get("names", {})
    unknown: list[str] = []

    def entry_anywhere(name: str) -> dict[str, str] | None:
        hits = [t[name] for t in names.values() if name in t]
        return hits[0] if len(hits) == 1 else None

    def sub(m: re.Match) -> str:
        first, second = m.group(1).strip(), (m.group(2) or "").strip()
        if not second:
            if first in heroes:
                return trilingual(first, heroes[first], fallback_zh.get(first, ""))
            tr = entry_anywhere(first)     # 模型偶尔把技能写成 [[Name]]
            if tr is None:
                unknown.append(m.group(0))
                return trilingual(first, None, fallback_zh.get(first, ""))
            return trilingual(first, tr)
        tr = names.get(first, {}).get(second) or entry_anywhere(second)
        if tr is None:
            unknown.append(m.group(0))
        return trilingual(second, tr)

    return _TAG.sub(sub, text), unknown


class PatchSummarizer:
    def __init__(self, client, model: str, effort: str | None = None, fallback_zh: dict[str, str] | None = None):
        self.client = client
        self.model = model
        self.effort = effort
        self.fallback_zh = fallback_zh or {}   # aliases.json 的中文英雄名，国服页面缺失时用

    async def summarize(self, patch: dict[str, Any], table: dict[str, Any] | None = None) -> str:
        table = table or patchnotes.translations(patch)
        system = SUMMARY_SYSTEM.format(names=name_list(patch, table))
        notes = f"{patch['title']}\n\n{patch['text']}"[:SUMMARY_INPUT_LIMIT]
        resp = await self.client.messages.create(
            model=self.model, max_tokens=MAX_TOKENS, system=system,
            messages=[{"role": "user", "content": notes}],
            **({"output_config": {"effort": self.effort}} if self.effort else {}))
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if resp.stop_reason == "max_tokens" or not text:
            raise RuntimeError(f"no summary text (stop_reason={resp.stop_reason})")
        text, unknown = fill_names(text, table, self.fallback_zh)
        if unknown:
            log.warning("Summary of %s used names not in the name table: %s", patch["id"], ", ".join(unknown[:10]))
        return text


class PatchFeed:
    def __init__(self, path: str,
                 fetch: Callable[[], Awaitable[list[dict[str, Any]]]] = patchnotes.fetch_recent,
                 summarizer: PatchSummarizer | None = None,
                 translate: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] = patchnotes.fetch_translations):
        self.path = path
        self.fetch = fetch
        self.summarizer = summarizer
        self.translate = translate
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
        """摘要只生成一次并缓存；失败时用只列英雄的简短版本（不缓存，下次再试）。"""
        summary = self.state["summaries"].get(self._key(patch["id"]))
        if summary is None and self.summarizer is not None:
            try:
                try:
                    table = await self.translate(patch)
                except Exception:
                    log.exception("Could not load Chinese/Korean names for %s; leaving them empty", patch["id"])
                    table = patchnotes.translations(patch)
                summary = await self.summarizer.summarize(patch, table)
                self.state["summaries"][self._key(patch["id"])] = summary
            except Exception:
                log.exception("Could not summarize %s; posting the short version", patch["id"])
        return render(patch, summary)

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
