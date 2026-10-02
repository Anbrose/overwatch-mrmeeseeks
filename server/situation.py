"""每个频道的局面状态：昵称→英雄记忆、局面指纹、上一次建议。只存在内存里，重启清空。"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Situation:
    memory: dict[str, tuple[str, str]] = field(default_factory=dict)   # 昵称 -> (hero_key, 显示名)
    map_side: tuple[Any, Any] | None = None
    stage: Any = None
    last_facts: dict[str, Any] | None = None
    advised_fingerprint: tuple | None = None
    advice_url: str | None = None

    def update(self, facts: dict[str, Any]) -> dict[str, Any]:
        """用记忆补全阵亡/未选玩家的英雄，返回新的 facts（不修改入参）。"""
        facts = copy.deepcopy(facts)
        map_name = (facts.get("map") or {}).get("name")
        # 复活画面等情况下读不到攻防：同一张地图沿用上次的
        if facts.get("side") is None and self.map_side and self.map_side[0] == map_name:
            facts["side"] = self.map_side[1]
        key = (map_name, facts.get("side"))
        if self.map_side is not None and key != self.map_side:
            self.memory.clear()            # 换图或换边：新的一局
            self.advised_fingerprint = None
            self.stage = None
        self.map_side = key

        # 死亡画面进度条变暗读不到阶段：沿用上次的，等下次看得到进度条再更新
        seg = facts.setdefault("segment", {})
        if seg.get("checkpoint") is None and self.stage is not None:
            seg["checkpoint"], seg["stale"] = self.stage, True
        elif seg.get("checkpoint") is not None:
            self.stage = seg["checkpoint"]

        for p in facts.get("allies", []) + facts.get("enemies", []):
            name = p.get("player")
            if p.get("status") == "alive" and name:
                self.memory[name] = (p["hero_key"], p["hero"])
            elif p.get("status") in ("dead", "empty") and name in self.memory:
                p["hero_key"], p["hero"] = self.memory[name]
                p["remembered"] = True
            elif p.get("status") == "unknown" and name in self.memory:
                # 头像认不出（可能换了英雄，也可能是特效遮挡）：只作参考，英雄仍算未知
                p["last_seen_hero"] = self.memory[name][1]
        self.last_facts = facts
        return facts

    def should_skip(self, facts: dict[str, Any]) -> bool:
        fp = fingerprint(facts)
        return fp is not None and fp == self.advised_fingerprint and self.advice_url is not None

    def remember_advice(self, facts: dict[str, Any], url: str) -> None:
        self.advised_fingerprint = fingerprint(facts)
        self.advice_url = url


def fingerprint(facts: dict[str, Any]) -> tuple | None:
    """(地图, 攻防, 阶段, 我方英雄, 敌方英雄)。有任何一项未知就返回 None：不能拿识别失败当"没变化"。"""
    map_name = (facts.get("map") or {}).get("name")
    side = facts.get("side")
    stage = (facts.get("segment") or {}).get("checkpoint")
    teams = []
    for team in ("allies", "enemies"):
        heroes = [p.get("hero_key") for p in facts.get(team, [])]
        if len(heroes) != 5 or any(h is None for h in heroes):
            return None
        teams.append(tuple(sorted(heroes)))
    if not (map_name and side and stage):
        return None
    return (map_name, side, stage, *teams)
