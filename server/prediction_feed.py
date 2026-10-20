"""赛事竞猜接入推送：每场比赛一条带按钮的竞猜消息，开赛截止公布名单，赛果揭晓谁猜中，每周一发周榜。

EsportsFeed.tick 先用 esports_feed.plan 算出普通推送，再交给 PredictionFeed.augment：
  - decorate：提醒附上竞猜链接和票数；赛果附上剧透揭晓，并带上"发出后结算"的标记
  - plan_predictions（纯函数）：竞猜 / 截止 / 作废 / 周榜
非文本消息由 execute 执行（发带按钮的消息、登记、截止时公布名单、编辑原消息）。
按钮只用描述字典 view_spec 表示，真正的 discord.ui 组件在 prediction_ui.build_view 里生成，
所以这里不依赖 discord，可以离线测试。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

import logos as lg
import predictions as pr
import roster_card as rc
import rosters as rs
from esports_feed import (ATTRIBUTION, DIGEST_HOUR, DIGEST_WINDOW, PREVIEW_PREFIX, TZ, Post, _bo, _vs)

log = logging.getLogger("mrmeeseeks.predictions")

PICKS_MIN_LEAD = timedelta(minutes=15)    # 离开赛不足 15 分钟就不再发竞猜消息
VOID_AFTER = timedelta(hours=48)          # 开赛 48 小时后还没结算就作废
NAMES_MAX_CHARS = 700                     # 名单每段最多这么长，超出写 "and N more"，保证消息不超过 2000 字
TBD = "TBD"


def has_tbd(m: dict[str, Any]) -> bool:
    """有一方还没定（TBD）：不开竞猜，已经开的作废。"""
    return TBD in (m["team1"], m["team2"])


ROSTER_NOTE = "-# Registered event roster — the starting lineup may differ. " + ATTRIBUTION


# ---------- 格式 ----------
def view_spec(key: str, team1: str, team2: str, counts: tuple[int, int], disabled: bool = False) -> dict[str, Any]:
    """按钮的描述：prediction_ui.build_view 据此生成 discord 组件。"""
    return {"key": key, "labels": [team1, team2], "counts": list(counts), "disabled": disabled}


def format_header(m: dict[str, Any]) -> str:
    """竞猜消息的标题和开赛时间；图片模式下只发这两行，阵容在对阵图里。"""
    return "\n".join([
        f"🎯 **Who wins?** {_vs(m)} · {m['label']}{_bo(m)}",
        f"Starts <t:{m['start']}:t> (<t:{m['start']}:R>) — picks close at start",
    ])


def format_prediction(m: dict[str, Any], roster1: list | None, roster2: list | None) -> str:
    """文字版竞猜消息（没开图片模式或画图失败时用）。"""
    return "\n".join([
        format_header(m),
        "",
        f"**{m['team1']}**  {rs.format_roster(roster1)}",
        f"**{m['team2']}**  {rs.format_roster(roster2)}",
        ROSTER_NOTE,
    ])


def join_names(names: list[str], limit: int = NAMES_MAX_CHARS) -> str:
    out: list[str] = []
    for i, name in enumerate(names):
        if len(", ".join(out + [name])) > limit:
            return ", ".join(out) + f" and {len(names) - i} more"
        out.append(name)
    return ", ".join(out) or "nobody"


def format_lock(entry: dict[str, Any], names: dict[int, list[str]]) -> str:
    def side(n: int) -> str:
        return join_names(names[n])
    return (f"🔒 Picks closed: {entry['team1']} vs {entry['team2']} — "
            f"{entry['team1']}: {side(1)} · {entry['team2']}: {side(2)}")


def reveal_line(lines: list[pr.ScoreLine]) -> str | None:
    if not lines:
        return None
    hit = join_names([f"{s.user_name} +{s.points:g}" for s in lines if s.correct])
    miss = join_names([s.user_name for s in lines if not s.correct])
    return f"🎯 ||Correct: {hit} · Missed: {miss}||"


def winner_of(m: dict[str, Any]) -> int | None:
    s1, s2 = m["score"]
    return 1 if s1 > s2 else 2 if s2 > s1 else None


def week_start(now: datetime) -> datetime:
    """本周一 00:00（悉尼时间）。"""
    local = now.astimezone(TZ)
    return (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


# ---------- 计划（纯函数） ----------
def plan_predictions(now: datetime, matches: list[dict[str, Any]] | None, snap: dict[str, Any],
                     state: dict[str, Any], channel_for: Callable[[dict[str, Any]], int | None]) -> list[Post]:
    """snap 由 PredictionFeed.snapshot 生成：entries（比赛 ID -> 已登记的竞猜）、active、rosters、weekly、week。"""
    posts: list[Post] = []
    ts = now.timestamp()
    local = now.astimezone(TZ)
    if matches is not None and local.hour >= DIGEST_HOUR:
        candidates = sorted((m for m in matches if not m["finished"]
                             and m["start"] - ts >= PICKS_MIN_LEAD.total_seconds()
                             and m["start"] <= ts + DIGEST_WINDOW.total_seconds()
                             and not has_tbd(m)
                             and snap["entries"].get(m["id"]) is None), key=lambda m: m["start"])
        for m in candidates:
            channel = channel_for(m)
            if channel is None:
                continue
            r1, r2 = snap["rosters"].get(m["id"], (None, None))
            posts.append(Post(format_prediction(m, r1, r2), channel=channel, kind="prediction",
                              data={"match": m, "view": view_spec(pr.key_for(m["id"]), m["team1"], m["team2"], (0, 0))}))
    current_start = {e["key"]: m["start"] for mid, e in snap["entries"].items() if e
                     for m in (matches or []) if m["id"] == mid}
    for e in snap["active"]:
        start = current_start.get(e["key"], e["start"])
        if ts >= start + VOID_AFTER.total_seconds() or (e["status"] == pr.OPEN and has_tbd(e)):
            posts.append(Post(None, channel=e["channel_id"], kind="void", data={"entry": e}))
        elif e["status"] == pr.OPEN and ts >= start:
            posts.append(Post(None, channel=e["channel_id"], kind="lock", data={"entry": e}))
    if local.weekday() == 0 and local.hour >= DIGEST_HOUR and state.get("leaderboard_week") != snap["week"]:
        posts.append(Post(snap["weekly"], {"leaderboard_week": snap["week"]}))
    return posts


def decorate(posts: list[Post], snap: dict[str, Any]) -> list[Post]:
    """提醒附上竞猜链接和票数；赛果附上剧透揭晓，并标记发出后结算（平局或没有比分就作废）。"""
    out = []
    for p in posts:
        m = p.data.get("match") if p.kind == "text" else None
        e = snap["entries"].get(m["id"]) if m else None
        if not e or p.text is None:
            out.append(p)
            continue
        if not m["finished"]:
            link = f" · {e['url']}" if e.get("url") else ""
            p = replace(p, text=f"{p.text}\n🎯 Picks: {e['team1']} · {e['counts'][0]} — "
                                f"{e['team2']} · {e['counts'][1]}{link}")
        elif e["status"] in (pr.OPEN, pr.LOCKED):
            winner = winner_of(m)
            line = reveal_line(snap["previews"].get(m["id"], []))
            text = p.text
            if line:
                body, _, last = text.rpartition("\n")
                text = f"{body}\n{line}\n{last}" if body else f"{text}\n{line}"
            p = replace(p, text=text, data={**p.data, "resolve": (e["key"], winner)})
        out.append(p)
    return out


# ---------- 运行 ----------
class PredictionFeed:
    """logos 不为 None 时是图片模式：竞猜消息附上对阵图（roster_card），队徽缓存在 logos 里。"""

    def __init__(self, store: pr.PredictionStore, rosters: rs.RosterCache, logos: lg.LogoCache | None = None):
        self.store, self.rosters, self.logos = store, rosters, logos
        self._roster_task: asyncio.Task | None = None

    def prepare(self, matches: list[dict[str, Any]]) -> None:
        """后台为未来 24 小时的比赛准备阵容（RosterCache 自己限速），不阻塞推送。"""
        if self._roster_task is not None and not self._roster_task.done():
            return
        upcoming = [m for m in matches if not m["finished"] and not (m["team1"] == "TBD" and m["team2"] == "TBD")]
        self._roster_task = asyncio.get_running_loop().create_task(self._ensure(upcoming))

    async def _ensure(self, matches: list[dict[str, Any]]) -> None:
        try:
            await self.rosters.ensure(matches)
            if self.logos is not None:
                await self.logos.ensure(url for m in matches for url in self.rosters.logos(m))
        except Exception:
            log.exception("Roster refresh failed")

    def card(self, m: dict[str, Any]) -> bytes | None:
        """对阵图（PNG）；没开图片模式或画图失败时返回 None，调用方退回文字版。"""
        if self.logos is None:
            return None
        try:
            r1, r2 = self.rosters.lookup(m)
            l1, l2 = (self.logos.path(u) for u in self.rosters.logos(m))
            return rc.render_card(m["team1"], m["team2"], r1, r2, l1, l2)
        except Exception:
            log.exception("Could not render roster card for %s", m.get("id"))
            return None

    def snapshot(self, now: datetime, matches: list[dict[str, Any]] | None) -> dict[str, Any]:
        listed = {m["id"] for m in matches or []}
        entries: dict[str, dict | None] = {}
        previews: dict[str, list] = {}
        roster_map: dict[str, tuple] = {}
        for m in matches or []:
            e = self.store.find(m, listed)
            if e and e["status"] == pr.OPEN and not m["finished"] and e["start"] != m["start"]:
                self.store.reschedule(e["key"], m["start"])           # 改期：按新的开赛时间截止
                e = self.store.get(e["key"])
            if e:
                e = {**e, "counts": self.store.counts(e["key"])}
                if m["finished"] and e["status"] in (pr.OPEN, pr.LOCKED) and winner_of(m):
                    previews[m["id"]] = self.store.score_preview(e["key"], winner_of(m))
            else:
                roster_map[m["id"]] = self.rosters.lookup(m)
            entries[m["id"]] = e
        week = week_start(now)
        rows = self.store.leaderboard((week - timedelta(days=7)).timestamp(), week.timestamp())
        weekly = None
        if rows:
            since = (week - timedelta(days=7)).timestamp()
            lines = pr.format_board(f"🏆 **Prediction leaderboard — week of {(week - timedelta(days=7)).date()}**", rows)
            titles = pr.format_titles(self.store.titles(since, week.timestamp()))
            weekly = "\n".join(lines + ([titles] if titles else []))
        return {"entries": entries, "active": self.store.active(), "previews": previews, "rosters": roster_map,
                "weekly": weekly, "week": week.date().isoformat()}

    def augment(self, now: datetime, matches: list[dict[str, Any]] | None, posts: list[Post],
                state: dict[str, Any], channel_for: Callable[[dict[str, Any]], int | None]) -> list[Post]:
        snap = self.snapshot(now, matches)
        planned = plan_predictions(now, matches, snap, state, channel_for)
        closing = [p for p in planned if p.kind in ("lock", "void")]   # 截止先于赛果（停机跨过开赛和赛果时）
        return closing + decorate(posts, snap) + [p for p in planned if p.kind not in ("lock", "void")]

    async def execute(self, post: Post, channel_id: int | None, send: Callable[..., Awaitable[Any]],
                      edit: Callable[..., Awaitable[None]] | None) -> None:
        """发送失败就抛出，让 tick 停在这里、下一轮重试；编辑失败只记日志。"""
        if post.kind == "prediction":
            if channel_id is None:
                return
            m = post.data["match"]
            png = await asyncio.to_thread(self.card, m)
            if png:
                sent = await send(channel_id, format_header(m), post.data["view"], png)
            else:
                sent = await send(channel_id, post.text, post.data["view"])
            message_id, url = sent if sent else (None, None)
            self.store.register(m, channel_id, message_id, url)
        elif post.kind == "lock":
            e = post.data["entry"]
            names = self.store.names(e["key"])
            await send(e["channel_id"], format_lock(e, names))
            if self.store.lock(e["key"]) is not None:
                await self._edit(edit, e, disabled=True)
        elif post.kind == "void":
            e = post.data["entry"]
            if self.store.void(e["key"]):
                await self._edit(edit, e, disabled=True, labels=("Voided", "Voided"))

    async def _edit(self, edit, e: dict[str, Any], disabled: bool, labels: tuple[str, str] | None = None) -> None:
        if edit is None or not e.get("message_id"):
            return
        t1, t2 = labels or (e["team1"], e["team2"])
        try:
            await edit(e["channel_id"], e["message_id"], view_spec(e["key"], t1, t2, self.store.counts(e["key"]), disabled))
        except Exception:
            log.exception("Could not edit prediction message %s", e.get("message_id"))

    def after_sent(self, post: Post, now: datetime) -> None:
        resolve = post.data.get("resolve")
        if not resolve:
            return
        key, winner = resolve
        if winner is None:
            self.store.void(key)
        else:
            self.store.resolve(key, winner, now.timestamp())

    def preview(self, now: datetime, matches: list[dict[str, Any]] | None,
                routed_here: Callable[[dict[str, Any]], bool]) -> tuple[str, dict, bytes | None] | None:
        """下一场路由到当前频道的比赛的竞猜消息样例（按钮禁用，不登记）：(文字, 按钮, 对阵图或 None)。"""
        ts = now.timestamp()
        upcoming = sorted((m for m in matches or [] if routed_here(m) and not m["finished"] and m["start"] >= ts
                           and not has_tbd(m)), key=lambda m: m["start"])
        if not upcoming:
            return None
        m = upcoming[0]
        spec = view_spec(pr.key_for(m["id"]), m["team1"], m["team2"], (0, 0), disabled=True)
        png = self.card(m)
        if png:
            return PREVIEW_PREFIX + format_header(m), spec, png
        r1, r2 = self.rosters.lookup(m)
        return PREVIEW_PREFIX + format_prediction(m, r1, r2), spec, None
