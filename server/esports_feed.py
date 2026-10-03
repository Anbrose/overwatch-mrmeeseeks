"""赛事推送频道：每日预告、开赛提醒、赛果（剧透遮罩）、官方新闻。

plan() 是纯函数：给定当前时间、比赛、新闻和状态，返回要发的消息，每条消息附带"发出后要记的账"。
EsportsFeed 负责抓取、持久化状态、调用 plan 并发送。没有设置频道时照常记账、不发送，
这样之后再设置频道也不会补发一堆旧消息。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import esports

log = logging.getLogger("mrmeeseeks.esports")

TZ = ZoneInfo("Australia/Sydney")
DIGEST_HOUR = 10                 # 悉尼时间每天 10:00 发未来 24 小时的预告
DIGEST_WINDOW = timedelta(hours=24)
REMIND_BEFORE = timedelta(minutes=15)
REMIND_LATE = timedelta(minutes=5)      # 开赛超过 5 分钟就不再补发提醒
RESULT_MAX_AGE = timedelta(hours=24)    # 只发最近 24 小时内开始的比赛的赛果
STATE_KEEP = timedelta(days=14)
MATCH_REFRESH = 600              # 比赛每 10 分钟抓一次
NEWS_REFRESH = 3 * 3600          # 新闻每 3 小时抓一次
TICK_SECONDS = 60
WATCH_URL = "https://www.twitch.tv/ow_esports"
ATTRIBUTION = "Data: Liquipedia (CC-BY-SA)"
MAX_MESSAGE = 1990


def new_state() -> dict[str, Any]:
    return {"channel_id": None, "reminded": {}, "resulted": {}, "digest_date": None,
            "seen_news": [], "initialized": False, "news_initialized": False}


@dataclass
class Post:
    text: str | None                      # None 表示只记账、不发消息
    marks: dict[str, Any] = field(default_factory=dict)


def apply_marks(state: dict[str, Any], marks: dict[str, Any]) -> None:
    """把一条消息的记账合并进状态（原地修改）。"""
    state["reminded"].update(marks.get("reminded", {}))
    state["resulted"].update(marks.get("resulted", {}))
    for url in marks.get("seen_news", []):
        if url not in state["seen_news"]:
            state["seen_news"].append(url)
    if "digest_date" in marks:
        state["digest_date"] = marks["digest_date"]
    for flag in ("initialized", "news_initialized"):
        if marks.get(flag):
            state[flag] = True


def prune(state: dict[str, Any], now: datetime) -> None:
    cutoff = (now - STATE_KEEP).timestamp()
    for key in ("reminded", "resulted"):
        state[key] = {k: v for k, v in state[key].items() if v >= cutoff}
    state["seen_news"] = state["seen_news"][-200:]


# ---------- 消息格式 ----------
def _vs(m: dict[str, Any]) -> str:
    return f"{m['team1']} vs {m['team2']}"


def _bo(m: dict[str, Any]) -> str:
    return f" · Bo{m['best_of']}" if m.get("best_of") else ""


def format_digest(matches: list[dict[str, Any]]) -> list[str]:
    lines = [f"• <t:{m['start']}:t> (<t:{m['start']}:R>) {_vs(m)} · {m['label']}{_bo(m)}" for m in matches]
    return split_lines(["📅 **Overwatch esports — next 24h**", *lines, ATTRIBUTION])


def format_reminder(m: dict[str, Any]) -> str:
    return f"🔴 Starting <t:{m['start']}:R>: **{_vs(m)}** · {m['label']}{_bo(m)} — watch: {WATCH_URL} · <{m['url']}>"


def format_result(m: dict[str, Any]) -> str:
    s1, s2 = m["score"]
    return (f"✅ {_vs(m)} · {m['label']} — result: ||{m['team1']} {s1} : {s2} {m['team2']}||"
            f" · <{m['url']}>\n{ATTRIBUTION}")


def format_news(n: dict[str, Any]) -> str:
    return f"📰 {n['title'] or 'Overwatch esports news'} {n['url']}"


def split_lines(lines: list[str], limit: int = MAX_MESSAGE) -> list[str]:
    """按行拼成不超过 limit 的若干条消息。"""
    out, cur = [], ""
    for line in lines:
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


# ---------- 计划 ----------
def plan(now: datetime, matches: list[dict[str, Any]] | None, news: list[dict[str, Any]] | None,
         state: dict[str, Any]) -> list[Post]:
    """now 必须带时区。matches/news 为 None 表示还没抓到（这一类本轮什么都不做）。"""
    posts: list[Post] = []
    ts = now.timestamp()
    # 首次启用：已经结束的比赛、已有新闻只记账，不刷屏。比赛和新闻各自初始化，互不阻塞
    if matches is not None and not state["initialized"]:
        posts.append(Post(None, {"initialized": True,
                                 "resulted": {m["id"]: m["start"] for m in matches if m["finished"]}}))
    elif matches is not None:
        local = now.astimezone(TZ)
        today = local.date().isoformat()
        if local.hour >= DIGEST_HOUR and state["digest_date"] != today:
            upcoming = sorted((m for m in matches if not m["finished"]
                               and ts <= m["start"] <= ts + DIGEST_WINDOW.total_seconds()),
                              key=lambda m: m["start"])
            texts = format_digest(upcoming) if upcoming else [None]
            posts += [Post(t) for t in texts[:-1]] + [Post(texts[-1], {"digest_date": today})]
        for m in sorted(matches, key=lambda m: m["start"]):
            if (not m["finished"] and m["id"] not in state["reminded"]
                    and m["start"] - REMIND_BEFORE.total_seconds() <= ts <= m["start"] + REMIND_LATE.total_seconds()):
                posts.append(Post(format_reminder(m), {"reminded": {m["id"]: m["start"]}}))
            if (m["finished"] and m["id"] not in state["resulted"]
                    and ts - RESULT_MAX_AGE.total_seconds() <= m["start"] <= ts):
                posts.append(Post(format_result(m), {"resulted": {m["id"]: m["start"]}}))
    if news is not None and not state["news_initialized"]:
        posts.append(Post(None, {"news_initialized": True, "seen_news": [n["url"] for n in news]}))
    elif news is not None:
        for n in news:
            if n["url"] not in state["seen_news"]:
                posts.append(Post(format_news(n), {"seen_news": [n["url"]]}))
    return posts


# ---------- 运行 ----------
class EsportsFeed:
    def __init__(self, path: str,
                 fetch_matches: Callable[[], Awaitable[list[dict]]] = esports.fetch_matches,
                 fetch_news: Callable[[], Awaitable[list[dict]]] = esports.fetch_news):
        self.path = path
        self.fetch_matches, self.fetch_news = fetch_matches, fetch_news
        self.state = new_state()
        self.matches: list[dict[str, Any]] | None = None
        self.news: list[dict[str, Any]] | None = None
        self._matches_at = self._news_at = float("-inf")

    @property
    def channel_id(self) -> int | None:
        return self.state["channel_id"]

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            state = new_state()
            state.update({k: data[k] for k in state if k in data})
            self.state = state
        except (OSError, ValueError, TypeError) as e:
            log.info("No usable esports state at %s (%r)", self.path, e)

    def save(self) -> None:
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write esports state to %s", self.path)

    def set_channel(self, channel_id: int | None) -> None:
        self.state["channel_id"] = channel_id
        self.save()

    def upcoming(self, n: int = 3) -> list[dict[str, Any]]:
        now = time.time()
        return sorted((m for m in self.matches or [] if not m["finished"] and m["start"] >= now),
                      key=lambda m: m["start"])[:n]

    async def refresh(self, mono: float) -> None:
        """到点就重新抓；抓取失败保留上一次的数据。"""
        if mono - self._matches_at >= MATCH_REFRESH:
            self._matches_at = mono
            try:
                self.matches = await self.fetch_matches()
            except Exception:
                log.exception("Esports match fetch failed; keeping previous data")
        if mono - self._news_at >= NEWS_REFRESH:
            self._news_at = mono
            try:
                self.news = await self.fetch_news()
            except Exception:
                log.exception("Esports news fetch failed; keeping previous data")

    async def tick(self, now: datetime, send: Callable[[int, str], Awaitable[None]], mono: float) -> None:
        await self.refresh(mono)
        state = copy.deepcopy(self.state)
        changed = False
        for post in plan(now, self.matches, self.news, state):
            if post.text is not None and state["channel_id"] is not None:
                try:
                    await send(state["channel_id"], post.text)
                except Exception:
                    log.exception("Could not post to esports channel %s; will retry", state["channel_id"])
                    break   # 停止本 tick，下一轮重试剩余消息
            apply_marks(state, post.marks)
            changed = True
        if changed:
            prune(state, now)
            state["channel_id"] = self.state["channel_id"]   # 发送期间可能被指令改过
            self.state = state
            self.save()

    async def run(self, send: Callable[[int, str], Awaitable[None]]) -> None:
        while True:
            try:
                await self.tick(datetime.now(timezone.utc), send, time.monotonic())
            except Exception:
                log.exception("Esports feed tick failed")
            await asyncio.sleep(TICK_SECONDS)
