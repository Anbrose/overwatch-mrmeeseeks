"""给换英雄建议：只看 vision 识别出的结构化 facts（纯文本），不看图。

识别见 vision/；这里要求每条建议注明依据，数据不足就直说。
"""
from __future__ import annotations

import json
from typing import Any

from anthropic import AsyncAnthropic

ADVISE_SYSTEM = """You are mrmeeseeks, an Overwatch hero-swap advisor for the player's team (the "allies").
The data below is what was recognized from the in-game scoreboard. Rules:
1. Use only the facts provided below for the current game state (map, mode, side, stage, heroes). Do not invent facts about this game.
2. Use your general knowledge of Overwatch heroes, counters and map positions to reason about those facts.
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


class Analyzer:
    def __init__(self, api_key: str, model: str, effort: str | None = "low"):
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model
        self.effort = effort   # 纯文本推理，low 足够且快；None 为模型默认

    async def advise(self, facts: dict[str, Any]) -> str:
        # 以后接入 OverFast 队友生涯数据、地图分段对照表时加到这里；没有的数据不要放占位，
        # 否则模型会以"缺数据"为由拒绝给建议
        payload = {"screen_facts": facts}
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
