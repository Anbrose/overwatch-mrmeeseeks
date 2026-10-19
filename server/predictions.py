"""赛事竞猜：押注、截止、结算、作废、周榜和赛季榜。存 STATE_DIR/predictions.db（SQLite）。

每场比赛用 key（比赛 ID 的 sha1 前 12 位）标识：Discord 按钮的 custom_id 最长 100 字符，
比赛 ID（赛事路径|时间|队1|队2）可能更长。

计分：猜错 0 分；猜中 1 + bonus，bonus = max(0, (0.5 - 押中一方的人数占比) * 4)，总分上限 3，
保留 1 位小数；这场总人数少于 3 时没有 bonus。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3

from esports_feed import SAME_MATCH_WINDOW, match_base
from dataclasses import dataclass
from typing import Any

OPEN, LOCKED, RESOLVED, VOID = "open", "locked", "resolved", "void"
BONUS_MIN_PICKS = 3
MAX_POINTS = 3.0
VOID_AFTER = 48 * 3600          # 开赛 48 小时后还没结算就作废

_SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    key TEXT PRIMARY KEY, match_id TEXT UNIQUE NOT NULL, team1 TEXT NOT NULL, team2 TEXT NOT NULL,
    label TEXT NOT NULL, start INTEGER NOT NULL, channel_id INTEGER NOT NULL, message_id INTEGER, url TEXT,
    status TEXT NOT NULL DEFAULT 'open', winner INTEGER);
CREATE TABLE IF NOT EXISTS picks (
    key TEXT NOT NULL, user_id INTEGER NOT NULL, user_name TEXT NOT NULL, side INTEGER NOT NULL,
    picked_at INTEGER NOT NULL, PRIMARY KEY (key, user_id));
CREATE TABLE IF NOT EXISTS scores (
    key TEXT NOT NULL, user_id INTEGER NOT NULL, user_name TEXT NOT NULL, correct INTEGER NOT NULL,
    points REAL NOT NULL, resolved_at INTEGER NOT NULL, PRIMARY KEY (key, user_id));
CREATE TABLE IF NOT EXISTS seasons (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at INTEGER NOT NULL);
"""


def key_for(match_id: str) -> str:
    return hashlib.sha1(match_id.encode("utf-8")).hexdigest()[:12]


@dataclass
class PickResult:
    ok: bool
    reason: str | None = None       # 拒绝时的原因，可以直接展示给用户
    changed: bool = False           # 是不是改选


@dataclass
class ScoreLine:
    user_id: int
    user_name: str
    correct: bool
    points: float


def points_for(share: float, total: int) -> float:
    """押中的一方占比 share、这场总人数 total 时，猜中的人每人得几分。"""
    bonus = max(0.0, (0.5 - share) * 4) if total >= BONUS_MIN_PICKS else 0.0
    return round(min(MAX_POINTS, 1 + bonus), 1)


class PredictionStore:
    def __init__(self, path: str | os.PathLike):
        self.path = str(path)
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        if self.db.execute("SELECT COUNT(*) FROM seasons").fetchone()[0] == 0:
            self.db.execute("INSERT INTO seasons (started_at) VALUES (0)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ---------- 比赛 ----------
    def register(self, match: dict[str, Any], channel_id: int, message_id: int | None, url: str | None = None) -> str:
        key = key_for(match["id"])
        self.db.execute(
            "INSERT OR IGNORE INTO matches (key, match_id, team1, team2, label, start, channel_id, message_id, url)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (key, match["id"], match["team1"], match["team2"], match["label"], int(match["start"]),
             int(channel_id), message_id, url))
        self.db.commit()
        return key

    def get(self, key: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM matches WHERE key = ?", (key,)).fetchone()
        return dict(row) if row else None

    def by_match_id(self, match_id: str) -> dict[str, Any] | None:
        return self.get(key_for(match_id))

    def find(self, match: dict[str, Any], listed: set[str] | frozenset[str] = frozenset()) -> dict[str, Any] | None:
        """这场比赛登记过的竞猜：ID 相同；或者同一赛事同样两队、开始时间相差不到 6 小时，
        并且旧 ID 已经不在当前比赛列表里（Liquipedia 改期）。规则和 esports_feed.already 一致。"""
        exact = self.by_match_id(match["id"])
        if exact:
            return exact
        base = match_base(match["id"])
        window = SAME_MATCH_WINDOW.total_seconds()
        rows = self.db.execute("SELECT * FROM matches WHERE team1 = ? AND team2 = ? AND ABS(start - ?) < ?",
                               (match["team1"], match["team2"], int(match["start"]), window))
        for r in rows:
            if r["match_id"] not in listed and match_base(r["match_id"]) == base:
                return dict(r)
        return None

    def active(self) -> list[dict[str, Any]]:
        """还没结算或作废的比赛（open / locked）。"""
        rows = self.db.execute("SELECT * FROM matches WHERE status IN (?, ?) ORDER BY start", (OPEN, LOCKED))
        return [dict(r) for r in rows]

    # ---------- 押注 ----------
    def pick(self, key: str, user_id: int, user_name: str, side: int, now: float) -> PickResult:
        m = self.get(key)
        if m is None:
            return PickResult(False, "This prediction no longer exists.")
        if m["status"] != OPEN or now >= m["start"]:
            return PickResult(False, "Picks are closed for this match.")
        if side not in (1, 2):
            return PickResult(False, "Unknown pick.")
        old = self.db.execute("SELECT side FROM picks WHERE key = ? AND user_id = ?", (key, user_id)).fetchone()
        self.db.execute(
            "INSERT INTO picks (key, user_id, user_name, side, picked_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(key, user_id) DO UPDATE SET side = excluded.side, user_name = excluded.user_name,"
            " picked_at = excluded.picked_at",
            (key, user_id, user_name, side, int(now)))
        self.db.commit()
        return PickResult(True, changed=old is not None and old["side"] != side)

    def counts(self, key: str) -> tuple[int, int]:
        rows = dict(self.db.execute("SELECT side, COUNT(*) FROM picks WHERE key = ? GROUP BY side", (key,)).fetchall())
        return rows.get(1, 0), rows.get(2, 0)

    def names(self, key: str) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {1: [], 2: []}
        for r in self.db.execute("SELECT side, user_name FROM picks WHERE key = ? ORDER BY picked_at, user_id", (key,)):
            out[r["side"]].append(r["user_name"])
        return out

    def lock(self, key: str) -> dict[int, list[str]] | None:
        """截止并返回双方押注名单；已经截止过（或不存在）返回 None。"""
        cur = self.db.execute("UPDATE matches SET status = ? WHERE key = ? AND status = ?", (LOCKED, key, OPEN))
        self.db.commit()
        return self.names(key) if cur.rowcount else None

    # ---------- 结算 ----------
    def score_preview(self, key: str, winner: int) -> list[ScoreLine]:
        picks = self.db.execute("SELECT user_id, user_name, side FROM picks WHERE key = ? ORDER BY picked_at, user_id",
                                (key,)).fetchall()
        total = len(picks)
        if not total:
            return []
        share = sum(1 for p in picks if p["side"] == winner) / total
        win_points = points_for(share, total)
        return [ScoreLine(p["user_id"], p["user_name"], p["side"] == winner,
                          win_points if p["side"] == winner else 0.0) for p in picks]

    def resolve(self, key: str, winner: int, now: float) -> list[ScoreLine]:
        """结算一次；已经结算或作废过返回空列表。"""
        cur = self.db.execute("UPDATE matches SET status = ?, winner = ? WHERE key = ? AND status IN (?, ?)",
                              (RESOLVED, winner, key, OPEN, LOCKED))
        if not cur.rowcount:
            self.db.commit()
            return []
        lines = self.score_preview(key, winner)
        self.db.executemany(
            "INSERT OR IGNORE INTO scores (key, user_id, user_name, correct, points, resolved_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(key, s.user_id, s.user_name, int(s.correct), s.points, int(now)) for s in lines])
        self.db.commit()
        return lines

    def void(self, key: str) -> bool:
        cur = self.db.execute("UPDATE matches SET status = ? WHERE key = ? AND status IN (?, ?)", (VOID, key, OPEN, LOCKED))
        self.db.commit()
        return bool(cur.rowcount)

    # ---------- 排行榜 ----------
    def leaderboard(self, since: float, until: float) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT user_id, MAX(user_name) AS name, ROUND(SUM(points), 1) AS points, SUM(correct) AS correct,"
            " COUNT(*) AS played FROM scores WHERE resolved_at >= ? AND resolved_at < ?"
            " GROUP BY user_id ORDER BY points DESC, correct DESC, name", (int(since), int(until)))
        return [dict(r) for r in rows]

    def titles(self, since: float, until: float) -> dict[str, Any]:
        """本周称号：预言家（第一）、冷门猎手（单场最高分）、最惨预言家（至少 3 场、有猜错、命中率最低）。"""
        board = self.leaderboard(since, until)
        best = self.db.execute(
            "SELECT user_name, points FROM scores WHERE resolved_at >= ? AND resolved_at < ? AND correct = 1"
            " ORDER BY points DESC, resolved_at, user_id LIMIT 1", (int(since), int(until))).fetchone()
        eligible = [r for r in board if r["played"] >= 3 and r["correct"] < r["played"]]   # 全部猜中的不算最惨
        worst = min(eligible, key=lambda r: (r["correct"] / r["played"], -r["played"]), default=None)
        return {"prophet": board[0] if board else None,
                "underdog": dict(best) if best and best["points"] > 1 else None,
                "worst": worst}

    def season_start(self) -> int:
        return self.db.execute("SELECT MAX(started_at) FROM seasons").fetchone()[0]

    def new_season(self, now: float) -> None:
        self.db.execute("INSERT INTO seasons (started_at) VALUES (?)", (int(now),))
        self.db.commit()


def format_board(title: str, rows: list[dict[str, Any]], limit: int = 10) -> list[str]:
    lines = [title]
    for i, r in enumerate(rows[:limit], 1):
        lines.append(f"{i}. {r['name']} — {r['points']:g} pts ({r['correct']}/{r['played']})")
    if len(lines) == 1:
        lines.append("No resolved predictions yet.")
    return lines


def format_titles(t: dict[str, Any]) -> str | None:
    parts = []
    if t["prophet"]:
        parts.append(f"🔮 Prophet of the week: {t['prophet']['name']}")
    if t["underdog"]:
        parts.append(f"🎯 Underdog hunter: {t['underdog']['user_name']} (+{t['underdog']['points']:g})")
    if t["worst"]:
        parts.append(f"🙈 Worst prophet: {t['worst']['name']} ({t['worst']['correct']}/{t['worst']['played']})")
    return " · ".join(parts) or None
