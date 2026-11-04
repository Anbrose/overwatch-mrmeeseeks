"""一起看「想看但没有」的记录：搜索结果里没有想看的（没有我想看的）、搜不到（我正在寻找这一部影视资源）。

每个用户每天最多提 WATCH_REQUEST_DAILY_LIMIT 次（悉尼时间 0 点重置，和许愿一样），同一个人同一天重复提同一部不算、也不扣次数。
存储 STATE_DIR/watch_requests.json：
  {"date": "2026-10-25", "counts": {"<user_id>": 2}, "requests": [{query, kind, user_id, user_name, channel_id, at}, ...]}
记录只保留最近 MAX_ENTRIES 条。不依赖 Discord，管理后台（watch_server）直接读。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from vod_search import normalize_title

log = logging.getLogger("mrmeeseeks.watch_requests")

DAILY_LIMIT = 5
MAX_ENTRIES = 2000
QUERY_MAX = 50
KIND_NOT_LISTED = "not_listed"     # 搜到了结果，但没有想看的
KIND_NO_RESULTS = "no_results"     # 什么都没搜到
KINDS = (KIND_NOT_LISTED, KIND_NO_RESULTS)
KIND_LABEL = {KIND_NOT_LISTED: "搜到了但没有想看的", KIND_NO_RESULTS: "没搜到"}
_TZ = ZoneInfo("Australia/Sydney")


def clean_query(query: str) -> str:
    """压掉多余空白、截到 QUERY_MAX 个字符；用于按钮 custom_id 和记录。"""
    return re.sub(r"\s+", " ", query or "").strip()[:QUERY_MAX]


@dataclass
class Request:
    query: str
    kind: str
    user_id: int
    user_name: str
    channel_id: int
    at: float

    @property
    def key(self) -> str:
        return normalize_title(self.query)


@dataclass
class AddResult:
    status: str            # "ok" | "duplicate" | "limit"
    remaining: int


class RequestStore:
    """path=None 时只在内存里。clock 返回 Unix 时间（秒）。"""

    def __init__(self, path: Path | None, limit: int = DAILY_LIMIT, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path is not None else None
        self.limit = limit
        self.clock = clock
        self.date = ""
        self.counts: dict[str, int] = {}
        self.requests: list[Request] = []
        self._load()

    # ---------------- 存储 ----------------
    def _load(self) -> None:
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            date = raw.get("date", "")
            counts = {str(int(k)): int(v) for k, v in raw.get("counts", {}).items()}
            requests = []
            for r in raw.get("requests", []):
                kind = str(r["kind"])
                if kind not in KINDS:
                    continue
                requests.append(Request(str(r["query"]), kind, int(r["user_id"]), str(r.get("user_name", "")),
                                        int(r["channel_id"]), float(r["at"])))
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            log.warning("Could not read %s; starting with no watch requests", self.path)
            return
        self.date, self.counts, self.requests = str(date), counts, requests[-MAX_ENTRIES:]

    def _save(self) -> None:
        if self.path is None:
            return
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"date": self.date, "counts": self.counts,
                           "requests": [asdict(r) for r in self.requests]}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            log.exception("Could not write watch requests to %s", self.path)

    # ---------------- 限额 ----------------
    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock(), _TZ).date().isoformat()

    def _counts(self) -> dict[str, int]:
        today = self._today()
        if self.date != today:
            self.date, self.counts = today, {}
        return self.counts

    def remaining(self, user_id: int) -> int:
        return max(0, self.limit - self._counts().get(str(user_id), 0))

    def _same_day(self, at: float) -> bool:
        return datetime.fromtimestamp(at, _TZ).date().isoformat() == self._today()

    # ---------------- 对外 ----------------
    def add(self, query: str, kind: str, user_id: int, user_name: str, channel_id: int) -> AddResult:
        """记一条。同一个人同一天同一部（按规范化片名）只记一次；超过每日次数返回 limit。"""
        if kind not in KINDS:
            raise ValueError(kind)
        query = clean_query(query)
        if not query:
            raise ValueError("empty query")
        counts = self._counts()
        key = normalize_title(query)
        for r in reversed(self.requests):
            if r.user_id == user_id and r.key == key and self._same_day(r.at):
                return AddResult("duplicate", self.remaining(user_id))
        used = counts.get(str(user_id), 0)
        if used >= self.limit:
            return AddResult("limit", 0)
        counts[str(user_id)] = used + 1
        self.requests.append(Request(query, kind, int(user_id), str(user_name), int(channel_id), self.clock()))
        del self.requests[:-MAX_ENTRIES]
        self._save()
        return AddResult("ok", self.remaining(user_id))

    def all(self) -> list[Request]:
        return list(self.requests)

    def since(self, seconds: float) -> list[Request]:
        cutoff = self.clock() - seconds
        return [r for r in self.requests if r.at >= cutoff]


@dataclass
class Group:
    key: str
    title: str               # 第一次提交时的写法
    count: int
    by_kind: dict[str, int]
    users: list[str]         # 去重，按先后
    first_at: float
    last_at: float


def group_requests(requests: list[Request]) -> list[Group]:
    """按规范化片名合并，最近提过的排前面。"""
    groups: dict[str, Group] = {}
    for r in requests:
        g = groups.get(r.key)
        if g is None:
            groups[r.key] = Group(r.key, r.query, 1, {r.kind: 1}, [r.user_name], r.at, r.at)
            continue
        g.count += 1
        g.by_kind[r.kind] = g.by_kind.get(r.kind, 0) + 1
        if r.user_name not in g.users:
            g.users.append(r.user_name)
        g.first_at, g.last_at = min(g.first_at, r.at), max(g.last_at, r.at)
    return sorted(groups.values(), key=lambda g: -g.last_at)


# ---------------- 用户看到的文案 ----------------
def recorded_text(query: str, remaining: int, limit: int) -> str:
    return f"已记下「{query}」，管理员会看到。你今天还能提 {remaining} 次（每天最多 {limit} 次）。"


def duplicate_text(query: str) -> str:
    return f"你今天已经提过「{query}」了。"


def limit_text(limit: int) -> str:
    return f"你今天已经提了 {limit} 次，明天再来吧。"
