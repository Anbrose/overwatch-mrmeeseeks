"""英雄对位（克制）数据：从 counterwatch.gg 抓取、缓存到本地 JSON、定时刷新。

评分含义（counterwatch 页面原文）：来自社区对局的对决和团战结果，去掉了英雄本身的强弱，
不是整局胜率；全段位；至少 50 名玩家才展示；每天更新。
scores[a][b] 是 a 对 b 的评分，正数 = a 占优，约等于百分点（+7.1 ≈ +7%）。

每个英雄页面内嵌 "counterScoreData":{"threats":[...],"targets":[...]}，targets 是本页英雄对
各对手的评分；threats 是同一组数的反面（等于对手页面上 targets 的值）。只存 targets。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from typing import Any

import aiohttp

from herodata import REQUEST_INTERVAL, USER_AGENT, _norm

log = logging.getLogger("mrmeeseeks.matchups")

SITE = "https://www.counterwatch.gg"
SITEMAP_URL = SITE + "/sitemap.xml"
SOURCE = ("counterwatch.gg counter ratings: duel and teamfight outcomes from community matches, "
          "all ranks, hero strength removed; not match win rate")
UNIT = "positive = first hero favored; roughly percentage points (+7.1 ≈ +7%)"
MIN_HERO_RATIO = 0.9    # 新快照英雄数至少是旧快照的 90%
MIN_PAIR_RATIO = 0.9    # 平均每个英雄的对位数至少是旧快照的 90%
_HERO_URL = re.compile(r"<loc>(https://www\.counterwatch\.gg/stats/overwatch/heroes/[a-z0-9-]+)</loc>")
_UPDATED = re.compile(r"last updated ([A-Z][a-z]+ \d{1,2}, \d{4})")
_MARKER = '"counterScoreData":'


def hero_urls(sitemap_xml: str) -> list[str]:
    """sitemap 里的英雄页面地址，去重并保持顺序。"""
    return list(dict.fromkeys(_HERO_URL.findall(sitemap_xml)))


def parse_page(html: str) -> tuple[list[dict[str, Any]], str | None] | None:
    """英雄页面 -> (targets 列表, 页面上的更新日期)。找不到对位数据时返回 None。"""
    text = html.replace('\\"', '"')   # Next.js flight 数据里的引号是转义过的
    i = text.find(_MARKER)
    if i < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text, i + len(_MARKER))
    except ValueError:
        return None
    targets = data.get("targets") if isinstance(data, dict) else None
    if not isinstance(targets, list):
        return None
    updated = _UPDATED.search(text)
    return targets, updated.group(1) if updated else None


def page_scores(targets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {_norm(t["heroRawName"]): {"score": round(float(t["counterScore"]), 2),
                                      "type": t.get("counterType"), "users": t.get("distinctUsers")}
            for t in targets if t.get("heroRawName") and t.get("counterScore") is not None}


def _pair_count(scores: dict[str, dict]) -> float:
    return sum(len(v) for v in scores.values()) / len(scores) if scores else 0.0


class MatchupStore:
    def __init__(self, path: str):
        self.path = path
        self.scores: dict[str, dict[str, dict[str, Any]]] = {}
        self.fetched_at: str | None = None
        self.source_updated: str | None = None

    @property
    def ready(self) -> bool:
        return bool(self.scores)

    # ---------- 缓存 ----------
    def load(self) -> bool:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            scores, fetched_at, updated = data["scores"], data["fetched_at"], data.get("source_updated")
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.info("No usable matchup cache at %s (%r)", self.path, e)
            return False
        self.scores, self.fetched_at, self.source_updated = scores, fetched_at, updated
        log.info("Loaded matchups for %d heroes from cache (fetched %s)", len(scores), fetched_at)
        return True

    def save(self) -> None:
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": self.fetched_at, "source_updated": self.source_updated,
                       "scores": self.scores}, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    # ---------- 刷新 ----------
    def accept(self, new: dict[str, Any]) -> bool:
        scores = new.get("scores") or {}
        if not scores:
            return False
        if not self.scores:
            return True
        if len(scores) < MIN_HERO_RATIO * len(self.scores):
            return False
        return _pair_count(scores) >= MIN_PAIR_RATIO * _pair_count(self.scores)

    async def refresh(self, fetch) -> bool:
        try:
            new = await fetch()
        except Exception:
            log.exception("Matchup refresh failed; keeping the current snapshot")
            return False
        if not self.accept(new):
            log.warning("Rejected matchup snapshot: %d heroes (%.1f pairs each) vs current %d (%.1f)",
                        len(new.get("scores") or {}), _pair_count(new.get("scores") or {}),
                        len(self.scores), _pair_count(self.scores))
            return False
        self.scores, self.source_updated = new["scores"], new.get("source_updated")
        self.fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            self.save()
        except OSError:  # 缓存写不进去也继续用内存里的新数据
            log.exception("Could not write matchup cache to %s", self.path)
        log.info("Matchups refreshed: %d heroes", len(self.scores))
        return True

    # ---------- 查询 ----------
    def score(self, a: str, b: str) -> dict[str, Any] | None:
        """a 对 b 的评分（正数 = a 占优）；没有数据时返回 None。"""
        return self.scores.get(_norm(a), {}).get(_norm(b))

    def profile(self, hero: str, top: int = 5) -> dict[str, list[dict[str, Any]]] | None:
        """hero 最克制谁（它的评分最高）、最怕谁（对手对它的评分最高）。"""
        key = _norm(hero)
        if key not in self.scores:
            return None
        strong = sorted(self.scores[key].items(), key=lambda kv: -kv[1]["score"])
        weak = sorted(((opp, row[key]) for opp, row in self.scores.items() if key in row),
                      key=lambda kv: -kv[1]["score"])
        return {"strong_against": [{"hero": k, **v} for k, v in strong[:top] if v["score"] > 0],
                "weak_against": [{"hero": k, **v} for k, v in weak[:top] if v["score"] > 0]}


async def fetch_all(interval: float = REQUEST_INTERVAL) -> dict[str, Any]:
    """抓取全部英雄页面。单个页面失败或没有对位数据只记日志并跳过。"""
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=timeout) as session:
        async with session.get(SITEMAP_URL) as resp:
            resp.raise_for_status()
            urls = hero_urls(await resp.text())
        scores: dict[str, dict] = {}
        updated = None
        for url in urls:
            await asyncio.sleep(interval)
            try:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    parsed = parse_page(await resp.text())
            except Exception:
                log.exception("Failed to fetch matchup page %s", url)
                continue
            if parsed is None:
                log.warning("No counterScoreData on %s", url)
                continue
            targets, page_updated = parsed
            scores[_norm(url.rsplit("/", 1)[1])] = page_scores(targets)
            updated = updated or page_updated
        return {"scores": scores, "source_updated": updated}
