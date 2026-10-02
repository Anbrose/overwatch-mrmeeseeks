"""英雄问答：Claude 只负责理解问题和组织回答，所有数字来自工具（herodata 快照 + damage 计算）。"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import damage
from herodata import HeroStore

log = logging.getLogger("mrmeeseeks.hero_qa")

MAX_TURNS = 5
TOO_COMPLEX = "That question needs too many lookups. Try splitting it into smaller questions."

SYSTEM = """You are mrmeeseeks, an Overwatch hero data assistant in a Discord channel.
Rules:
1. Every number in your answer must come from a tool result in this conversation. Never use numbers from memory.
2. To say whether a hero was buffed or nerfed, call get_patch_history and quote the relevant patch lines with their dates. If the recent changes only touch perks, say so.
3. For damage or "how many shots to kill" questions, call shots_to_kill. If a weapon has several damage variants (e.g. charge levels), pick the one the user means via `variant` and say which one you used.
4. State the assumptions returned by the tools (e.g. all pellets hit, Role Queue health).
5. If a tool returns candidates for an unclear hero name, ask the user which hero they meant. Do not guess.
6. If a tool returns "unsupported", explain why. Time-to-kill, ability damage (non-weapon) and perk damage bonuses are not supported yet.
7. Answer in the same language as the question, in at most about 150 words.
8. End with one line: "Source: Overwatch Wiki, data fetched <fetched_at date>"."""

_HERO = {"type": "string", "description": "Hero name in any language, e.g. Cassidy, 卡西迪, McCree"}
TOOLS = [
    {
        "name": "get_hero_stats",
        "description": "Base stats of a hero: role, sub-role, health/armor/shield per mode, and weapon data "
                       "(damage variants, falloff range in meters, headshot, pellets, projectile radius in meters).",
        "input_schema": {"type": "object", "properties": {"hero": _HERO}, "required": ["hero"]},
    },
    {
        "name": "get_patch_history",
        "description": "Most recent balance changes for a hero (live PvP only), newest first, as quoted patch text.",
        "input_schema": {"type": "object", "properties": {
            "hero": _HERO,
            "limit": {"type": "integer", "description": "Number of patches, 1-20, default 5"},
        }, "required": ["hero"]},
    },
    {
        "name": "shots_to_kill",
        "description": "Damage per shot at a distance and the number of shots for attacker's weapon to kill target, "
                       "simulating shields, armor and Bruiser crit reduction. Returns a per-shot breakdown.",
        "input_schema": {"type": "object", "properties": {
            "attacker": _HERO,
            "target": _HERO,
            "weapon": {"type": "string", "description": "Weapon name or index from get_hero_stats; default is the first non-melee weapon"},
            "distance": {"type": "number", "description": "Distance in meters, default 0"},
            "headshot": {"type": "boolean", "description": "Every shot is a headshot; default false"},
            "mode": {"type": "string", "enum": list(damage.MODES), "description": "Default role_queue"},
            "variant": {"type": "integer", "description": "Index into the weapon's damage variants"},
        }, "required": ["attacker", "target"]},
    },
]


def _pick_weapon(hero: dict[str, Any], weapon: str | None) -> dict[str, Any] | None:
    weapons = hero["weapons"]
    if weapon is None or str(weapon).strip() == "":
        # 默认取第一把有伤害数据、能逐发计算的武器（跳过治疗武器、光束、近战）
        usable = (w for w in weapons
                  if w["damage"] and "beam" not in w["shot_type"] and "melee" not in w["shot_type"])
        return next(usable, weapons[0] if weapons else None)
    if str(weapon).isdigit():
        i = int(weapon)
        return weapons[i] if i < len(weapons) else None
    q = str(weapon).casefold()
    return next((w for w in weapons if w["name"].casefold() == q), None) or \
        next((w for w in weapons if q in w["name"].casefold()), None)


def _as_bool(value: Any) -> bool | None:
    """模型偶尔会把布尔值写成字符串；bool("false") 是 True，所以要显式转换。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


class HeroQA:
    def __init__(self, client: Any, model: str, store: HeroStore, max_turns: int = MAX_TURNS):
        self.client = client
        self.model = model
        self.store = store
        self.max_turns = max_turns

    # ---------- 工具 ----------
    def _hero(self, name: str) -> dict[str, Any]:
        found = self.store.resolve(name)
        if isinstance(found, dict):
            return found
        return {"error": "unknown_hero", "query": name, "candidates": found}

    def run_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "get_hero_stats":
            hero = self._hero(args["hero"])
            if "error" in hero:
                return hero
            stats = {k: v for k, v in hero.items() if k != "patches"}
            stats["weapons"] = [{"index": i, **{k: v for k, v in w.items() if k != "raw"}}
                                for i, w in enumerate(hero["weapons"])]
            return {**stats, "fetched_at": self.store.fetched_at}
        if name == "get_patch_history":
            hero = self._hero(args["hero"])
            if "error" in hero:
                return hero
            limit = args.get("limit")
            limit = max(1, min(5 if limit is None else int(limit), 20))
            return {"hero": hero["name"], "patches": hero["patches"][:limit],
                    "source_url": hero["source_url"], "fetched_at": self.store.fetched_at}
        if name == "shots_to_kill":
            attacker, target = self._hero(args["attacker"]), self._hero(args["target"])
            for h in (attacker, target):
                if "error" in h:
                    return h
            weapon = _pick_weapon(attacker, args.get("weapon"))
            if weapon is None:
                return {"error": "unknown_weapon", "weapons": [w["name"] for w in attacker["weapons"]]}
            try:
                distance = float(args.get("distance") or 0)
                variant = None if args.get("variant") is None else int(args["variant"])
            except (TypeError, ValueError):
                return {"error": "distance must be a number of meters and variant an integer index"}
            headshot = _as_bool(args.get("headshot", False))
            if headshot is None:
                return {"error": "headshot must be true or false"}
            result = damage.shots_to_kill(
                attacker, weapon, target, distance=distance, headshot=headshot,
                mode=args.get("mode") or "role_queue",
                variant=variant,
            )
            if "unsupported" in result:  # 让模型能换一把武器重试
                result["weapons"] = [w["name"] for w in attacker["weapons"]]
            return {**result, "fetched_at": self.store.fetched_at}
        return {"error": f"unknown tool {name}"}

    # ---------- 对话循环 ----------
    async def answer(self, question: str) -> str:
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        for _ in range(self.max_turns):
            resp = await self.client.messages.create(
                model=self.model, max_tokens=1024, system=SYSTEM, tools=TOOLS, messages=messages)
            if resp.stop_reason != "tool_use":
                return "".join(b.text for b in resp.content if b.type == "text").strip()
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                try:
                    output = self.run_tool(block.name, block.input)
                except Exception as e:  # 工具出错交给模型解释，不中断整个回答
                    log.exception("Tool %s failed", block.name)
                    output = {"error": f"tool failed: {e}"}
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(output, ensure_ascii=False)})
            messages.append({"role": "user", "content": results})
        return TOO_COMPLEX


class Cooldown:
    """每个用户两次提问之间的最短间隔。"""
    def __init__(self, seconds: float):
        self.seconds = seconds
        self.last: dict[int, float] = {}

    def allow(self, user_id: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if now - self.last.get(user_id, float("-inf")) < self.seconds:
            return False
        self.last[user_id] = now
        return True
