"""伤害与击杀计算。纯函数，输入是 heroparse.parse_hero 产出的英雄/武器 dict。

规则来源（Overwatch fandom Wiki，2026-10 核对）：
- 「Hit points」Armor 节：每段伤害减 7，最多减 50%；只要这段伤害打到护甲就全额减免；护盾不减伤
- 「Critical hit」：爆头默认 2.0x；Sojourn Charged Shot / Illari Solar Rifle / Juno Mediblaster 1.5x；
  Widowmaker Widow's Kiss (ADS) 2.5x
- 「Sub-Roles」Bruiser：受到的暴击伤害 -25%；1.5x 爆头倍率的武器无视这个减伤
"""
from __future__ import annotations

from typing import Any

ARMOR_FLAT_REDUCTION = 7.0
ARMOR_MAX_REDUCTION = 0.5
DEFAULT_CRIT_MULTIPLIER = 2.0
CRIT_MULTIPLIER_OVERRIDES = {
    ("Sojourn", "Charged Shot"): 1.5,
    ("Illari", "Solar Rifle"): 1.5,
    ("Juno", "Mediblaster"): 1.5,
    ("Widowmaker", "Widow's Kiss (ADS)"): 2.5,
}
BRUISER_CRIT_REDUCTION = 0.25
MODES = ("role_queue", "open_queue", "6v6")
# 这些变体是多段伤害的合计或每秒伤害，不是单次命中的伤害，拿来逐发模拟会得出错误的枪数
_NON_PER_HIT_LABELS = ("per second", "per volley", "per burst", "per shot", "total")
_EPS = 1e-6


def damage_at(variant: dict[str, float], falloff_start: float | None,
              falloff_end: float | None, distance: float) -> float:
    """单段伤害随距离线性衰减；没有衰减数据时恒为最大值。"""
    hi, lo = variant["max"], variant["min"]
    if falloff_start is None or falloff_end is None or distance <= falloff_start:
        return hi
    if distance >= falloff_end:
        return lo
    t = (distance - falloff_start) / (falloff_end - falloff_start)
    return hi + (lo - hi) * t


def crit_multiplier(hero: str, weapon: dict[str, Any]) -> float:
    return CRIT_MULTIPLIER_OVERRIDES.get((hero, weapon["name"]), DEFAULT_CRIT_MULTIPLIER)


def _explosive_parts(weapon: dict[str, Any]) -> tuple[dict, dict] | None:
    direct = next((v for v in weapon["damage"] if "direct" in v["label"].lower()), None)
    splash = next((v for v in weapon["damage"] if "splash, enemy" in v["label"].lower()), None)
    return (direct, splash) if direct and splash else None


def shot_instances(attacker: str, weapon: dict[str, Any], target: dict[str, Any], distance: float,
                   headshot: bool, variant: int | None = None) -> dict[str, Any]:
    """一次开火产生的伤害段列表（霰弹每颗弹丸一段）。不支持时返回 {"unsupported": 原因}。"""
    kind = weapon.get("shot_type") or ""
    if "beam" in kind:
        return {"unsupported": "beam weapons deal damage per second, not per shot"}
    if "melee" in kind:
        return {"unsupported": "melee weapons are out of scope"}
    if not weapon.get("damage"):
        return {"unsupported": "missing data: damage"}

    assumptions: list[str] = []
    explosive = _explosive_parts(weapon) if variant is None else None
    if explosive:
        direct, splash = explosive
        per_hit = direct["max"] + splash["max"]
        assumptions.append("direct hit plus full splash damage")
        label = "direct hit + splash"
    else:
        idx = variant or 0
        if not 0 <= idx < len(weapon["damage"]):
            return {"unsupported": f"weapon has no damage variant #{idx}"}
        chosen = weapon["damage"][idx]
        pellets = weapon.get("pellets") or 1
        if idx > 0 and pellets > 1:
            return {"unsupported": f"variant {idx} is a total over {pellets} pellets; use variant 0 (per pellet)"}
        if idx > 0 and any(k in chosen["label"].lower() for k in _NON_PER_HIT_LABELS):
            return {"unsupported": f"variant {idx} ({chosen['label']}) is not damage per hit"}
        per_hit = damage_at(chosen, weapon.get("falloff_start"), weapon.get("falloff_end"), distance)
        label = chosen["label"]

    if headshot:
        if not weapon.get("headshot"):
            return {"unsupported": f"{weapon['name']} cannot headshot"}
        mult = crit_multiplier(attacker, weapon)
        per_hit *= mult
        if target.get("subrole") == "Bruiser" and mult > 1.5:
            per_hit *= 1 - BRUISER_CRIT_REDUCTION
            assumptions.append("Bruiser takes 25% less critical damage")

    pellets = 1 if explosive else weapon.get("pellets") or 1
    if pellets > 1:
        assumptions.append(f"all {pellets} pellets hit")
    return {"instances": [per_hit] * pellets, "variant": label, "assumptions": assumptions}


def apply_instance(pool: dict[str, float], raw: float) -> None:
    """一段伤害按 护盾 → 护甲 → 生命值 结算，原地修改 pool。"""
    dmg = raw
    absorbed = min(pool["shield"], dmg)
    pool["shield"] -= absorbed
    dmg -= absorbed
    if dmg <= 0:
        return
    if pool["armor"] > 0:
        dmg -= min(ARMOR_FLAT_REDUCTION, dmg * ARMOR_MAX_REDUCTION)
        absorbed = min(pool["armor"], dmg)
        pool["armor"] -= absorbed
        dmg -= absorbed
    pool["health"] -= dmg


def shots_to_kill(attacker: dict[str, Any], weapon: dict[str, Any], target: dict[str, Any],
                  distance: float = 0, headshot: bool = False, mode: str = "role_queue",
                  variant: int | None = None, max_shots: int = 500) -> dict[str, Any]:
    if distance < 0:
        return {"unsupported": "distance must be 0 or more meters"}
    if mode not in MODES:
        return {"unsupported": f"unknown mode {mode!r}, use one of {', '.join(MODES)}"}
    hp = target["hp"][mode]
    if hp.get("health") is None:
        return {"unsupported": f"missing data: {target['name']} health"}
    shot = shot_instances(attacker["name"], weapon, target, distance, headshot, variant)
    if "unsupported" in shot:
        return shot

    pool = {"health": float(hp["health"]), "armor": float(hp.get("armor") or 0),
            "shield": float(hp.get("shield") or 0)}
    start = dict(pool)
    log = []
    for n in range(1, max_shots + 1):
        for inst in shot["instances"]:
            apply_instance(pool, inst)
        log.append({"shot": n, **{k: round(max(v, 0), 2) for k, v in pool.items()}})
        if pool["health"] <= _EPS:
            break
    else:
        return {"unsupported": f"target not killed within {max_shots} shots"}

    return {
        "attacker": attacker["name"], "weapon": weapon["name"], "variant": shot["variant"],
        "target": target["name"], "mode": mode, "distance_m": distance, "headshot": headshot,
        "damage_per_instance": round(shot["instances"][0], 2),
        "instances_per_shot": len(shot["instances"]),
        "target_hp": start, "shots": len(log), "breakdown": log,
        "assumptions": shot["assumptions"] + [f"{mode} health values"],
    }
