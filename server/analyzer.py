"""给换英雄建议：只看 vision 识别出的结构化 facts（纯文本），不看图。

识别见 vision/；这里要求每条建议注明依据，数据不足就直说。
克制关系只来自 counterwatch 对位数据（matchups.py）：代码先算好当前对位和换人候选，模型只负责组织语言。
"""
from __future__ import annotations

import json
from typing import Any

from anthropic import AsyncAnthropic

import matchups as mu
from herodata import _norm

ADVISE_SYSTEM = """You are mrmeeseeks, an Overwatch hero-swap advisor for the player's team (the "allies").
The data below is what was recognized from the in-game scoreboard. Rules:
1. Use only the facts provided below for the current game state (map, mode, side, stage, heroes). Do not invent facts about this game.
2. Counter relationships (which hero is favored against which) may only come from "matchup_data", and you must quote its number, e.g. (basis: Zarya vs Winston +7.1, counterwatch). If there is no matchup_data, or a pair is not in it, do not claim that one hero counters another. For everything else (map, positions, objective, team composition) use your general knowledge of Overwatch.
2b. When recommending a swap, prefer heroes from "swap_candidates". If a candidate's total is within 3 of the current hero's total, say the difference is small. Heroes listed in "likely_heroes" are probable, not certain; say so if you rely on them.
3. Always give your best recommendation with whatever is known. Some heroes may be unknown: reason around them and mention them in a few words at most; never refuse to advise just because some data is missing.
4. A player with status "unknown" and a "last_seen_hero" was most recently seen on that hero; treat it as likely but not certain. A "dead" player with a hero listed is still playing that hero.
5. After each reason, cite its basis in parentheses, e.g. (basis: enemy comp Winston + Tracer).
Output format (English, at most 120 words):
First line: a one-sentence conclusion (which ally should swap to what, or stay as is)
Then at most 3 reasons, one per line, each starting with "- "."""

# 模型默认开启 adaptive thinking，思考也计入 max_tokens；给小了会只剩 thinking 块、没有正文
MAX_TOKENS = 16000


def _text(resp: Any) -> str:
    if resp.stop_reason == "max_tokens":
        raise ValueError(f"model output truncated at max_tokens={MAX_TOKENS}")
    return "".join(b.text for b in resp.content if b.type == "text")


Roles = dict[str, tuple[str, str]]   # 归一化英雄名 -> (职责, 显示名)


def roles_from_roster(roster: dict[str, list[tuple[str, str]]]) -> Roles:
    """label_ui.build_roster 的结果 {role: [(key, name)]} -> {归一化名: (role, name)}。"""
    return {_norm(name): (role, name) for role, heroes in roster.items() for _, name in heroes}


def _team(people: list[dict[str, Any]]) -> list[tuple[str | None, bool]]:
    """每个玩家 -> (英雄显示名, 是否只是"可能")。阵亡但记得的英雄算确定；认不出时用上次见到的英雄。"""
    out = []
    for p in people:
        if p.get("hero"):
            out.append((p["hero"], False))
        elif p.get("last_seen_hero"):
            out.append((p["last_seen_hero"], True))
        else:
            out.append((None, False))
    return out


def build_matchup_data(facts: dict[str, Any], matchups: mu.MatchupStore | None, roles: Roles,
                       top: int = 3) -> dict[str, Any] | None:
    """当前对局的对位评分和换人候选；没有可用的对位数据时返回 None（这时不给模型 matchup_data）。"""
    if matchups is None or not matchups.ready:
        return None
    allies = _team(facts.get("allies") or [])
    enemies = _team(facts.get("enemies") or [])
    known = [h for h, _ in enemies if h and _norm(h) in matchups.scores]
    if not known:
        return None

    def against_enemies(hero: str) -> tuple[float, list[dict[str, Any]]]:
        pairs = [{"enemy": e, "score": s["score"]} for e in known if (s := matchups.score(hero, e))]
        return round(sum(p["score"] for p in pairs), 2), pairs

    taken = {_norm(h) for h, _ in allies if h}
    current, swaps = [], []
    for hero, likely in allies:
        if not hero:
            continue
        total, pairs = against_enemies(hero)
        current.append({"ally": hero, "vs_enemies_total": total, "pairs_with_data": len(pairs), "pairs": pairs,
                        **({"likely": True} if likely else {})})
        role = roles.get(_norm(hero), (None, None))[0]
        if role is None:
            continue
        candidates = []
        for key, (r, name) in roles.items():
            if r != role or key in taken:
                continue
            c_total, c_pairs = against_enemies(name)
            if c_pairs:
                candidates.append({"hero": name, "vs_enemies_total": c_total, "pairs_with_data": len(c_pairs)})
        candidates.sort(key=lambda c: -c["vs_enemies_total"])
        swaps.append({"ally": hero, "role": role, "current_total": total, "best": candidates[:top]})

    updated = f", updated {matchups.source_updated}" if matchups.source_updated else ""
    return {
        "source": mu.SOURCE + updated,
        "unit": mu.UNIT,
        "current": current,
        "swap_candidates": swaps,
        "enemies_without_data": [h or "unknown" for h, _ in enemies if not h or _norm(h) not in matchups.scores],
        "likely_heroes": [h for h, likely in allies + enemies if h and likely],
    }


class Analyzer:
    def __init__(self, api_key: str, model: str, effort: str | None = "low",
                 matchups: mu.MatchupStore | None = None, roles: Roles | None = None):
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model
        self.effort = effort   # 纯文本推理，low 足够且快；None 为模型默认
        self.matchups = matchups
        self.roles = roles or {}

    async def advise(self, facts: dict[str, Any]) -> str:
        # 以后接入 OverFast 队友生涯数据、地图分段对照表时加到这里；没有的数据不要放占位，
        # 否则模型会以"缺数据"为由拒绝给建议
        payload: dict[str, Any] = {"screen_facts": facts}
        matchup_data = build_matchup_data(facts, self.matchups, self.roles)
        if matchup_data:
            payload["matchup_data"] = matchup_data
        extra = {"output_config": {"effort": self.effort}} if self.effort else {}
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=ADVISE_SYSTEM,
            messages=[{"role": "user", "content":
                       "Data:\n" + json.dumps(payload, ensure_ascii=False, indent=2)}],
            **extra,
        )
        return _text(resp).strip()


def _stage(seg: dict[str, Any]) -> str:
    cp = seg.get("checkpoint")
    if cp is None:
        return "stage ?"
    label = f"point {cp}" if not str(cp).isdigit() else f"checkpoint {cp}"
    if seg.get("stale"):
        return f"{label} (last seen)"
    return f"{label} ({seg['progress']})" if seg.get("progress") else label


def _people(people: list[dict[str, Any]]) -> str:
    out = []
    for p in people:
        hero = p.get("hero") or (f"{p['last_seen_hero']}?" if p.get("last_seen_hero") else "?")
        if p.get("status") == "dead":
            hero += "†"
        elif p.get("status") == "empty" and not p.get("hero"):
            hero = "(not picked)"
        out.append(hero)
    return ", ".join(out) or "?"


def format_facts(facts: dict[str, Any]) -> str:
    """识别结果摘要，贴在 Discord 建议上方，方便核对认出了什么。"""
    m = facts.get("map") or {}
    map_part = m.get("name") or "Map ?"
    if not m.get("name"):
        ocr = next((u for u in facts.get("unreadable", []) if u.startswith("map")), None)
        if ocr:
            map_part += f" {ocr[4:]}"
    head = " · ".join([map_part, facts.get("mode") or "mode ?", facts.get("side") or "side ?",
                       _stage(facts.get("segment") or {})])
    return (f"{head}\nAllies: {_people(facts.get('allies') or [])}"
            f"\nEnemies: {_people(facts.get('enemies') or [])}")
