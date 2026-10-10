"""补丁推送频道：定时检查官方补丁说明，有新补丁就用 Claude 写成摘要发到频道。

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

SUMMARY_SYSTEM = """You write the patch-notes post for an Overwatch community Discord server.
Summarize the official patch notes you are given, in {language}. Discord markdown, under 1600 characters:
- One opening line saying what this patch is (new season, balance patch, bug fixes, ...).
- **New content**: one short bullet per new season, hero, mode, map or event. Omit if none.
- **Hero changes**: one bullet per hero, starting with ⬆️ (buff), ⬇️ (nerf) or 🔄 (rework or mixed), \\
then the hero name and the key changes with numbers (from → to). Leave hit-volume-only changes out of \\
these bullets; if there is a hit-volume pass, summarize it in one extra bullet.
- **Other**: at most 3 bullets for the most noticeable remaining changes (competitive, bug fixes). Omit if none.
Rules: only use facts and numbers from the notes. No opinions or tier predictions. Keep ability, perk, \\
map and mode names in English. Hero names: use the names from the list below when one is given, \\
otherwise keep the official English name; never invent a translation. No greeting, no sign-off, \\
no links (the link is added for you).
Hero names to use:
{names}"""


def new_state() -> dict[str, Any]:
    return {"channel_id": None, "seen": [], "initialized": False, "patches": [], "summaries": {},
            "checked_at": None}


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """按行切成不超过 limit 的几段；单行超长时硬切。"""
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


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


class PatchSummarizer:
    def __init__(self, client, model: str, effort: str | None = None,
                 language: str = "Simplified Chinese", names: dict[str, str] | None = None):
        self.client = client
        self.model = model
        self.effort = effort
        self.language = language
        self.names = names or {}

    async def summarize(self, patch: dict[str, Any]) -> str:
        names = "\n".join(f"{en} = {zh}" for en, zh in sorted(self.names.items()) if en in patch["heroes"])
        system = SUMMARY_SYSTEM.format(language=self.language, names=names or "(none)")
        notes = f"{patch['title']}\n\n{patch['text']}"[:SUMMARY_INPUT_LIMIT]
        resp = await self.client.messages.create(
            model=self.model, max_tokens=MAX_TOKENS, system=system,
            messages=[{"role": "user", "content": notes}],
            **({"output_config": {"effort": self.effort}} if self.effort else {}))
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if resp.stop_reason == "max_tokens" or not text:
            raise RuntimeError(f"no summary text (stop_reason={resp.stop_reason})")
        return text


class PatchFeed:
    def __init__(self, path: str,
                 fetch: Callable[[], Awaitable[list[dict[str, Any]]]] = patchnotes.fetch_recent,
                 summarizer: PatchSummarizer | None = None):
        self.path = path
        self.fetch = fetch
        self.summarizer = summarizer
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
        self.state["summaries"] = {k: v for k, v in self.state["summaries"].items() if k in ids}
        if not self.state["initialized"]:   # 第一次只记账，不补发旧补丁
            self.state["seen"] = sorted(ids)
            self.state["initialized"] = True
            log.info("Patch feed initialized with %d existing patches", len(ids))
        self.save()
        return True

    async def text_for(self, patch: dict[str, Any]) -> str:
        """摘要只生成一次并缓存；失败时用只列英雄的简短版本（不缓存，下次再试）。"""
        summary = self.state["summaries"].get(patch["id"])
        if summary is None and self.summarizer is not None:
            try:
                summary = await self.summarizer.summarize(patch)
                self.state["summaries"][patch["id"]] = summary
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
