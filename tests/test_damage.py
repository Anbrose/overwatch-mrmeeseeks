"""伤害计算测试：用手写的英雄/武器 dict，预期值都可以手算核对。

运行：python tests/test_damage.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import damage  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def hp(health, armor=0, shield=0):
    pool = {"health": health, "armor": armor, "shield": shield}
    return {"role_queue": pool, "open_queue": pool, "6v6": pool}


def mk_hero(name, pool, subrole=None, weapons=()):
    return {"name": name, "role": "Damage", "subrole": subrole, "hp": pool, "weapons": list(weapons)}


def mk_weapon(name, variants, falloff=(None, None), shot_type="hitscan", headshot=True, pellets=1):
    return {"name": name, "shot_type": shot_type, "headshot": headshot, "pellets": pellets,
            "damage": [{"label": l, "max": hi, "min": lo} for l, hi, lo in variants],
            "falloff_start": falloff[0], "falloff_end": falloff[1]}


PEACEKEEPER = mk_weapon("Peacekeeper", [("", 70, 21)], falloff=(25, 35))
SHOTGUN = mk_weapon("Hellfire Shotguns", [("per pellet", 5.75, 1.725), ("per shot", 115, 34.5)],
                    falloff=(10, 20), shot_type="shotgun", pellets=20)
FRAG = mk_weapon("Frag Launcher", [("direct hit", 45, 45), ("splash, enemy", 80, 10)],
                 shot_type="arc+aoe", headshot=False)
ADS = mk_weapon("Widow's Kiss (ADS)", [("at 0% power", 12, 6), ("at 100% power", 120, 60)], falloff=(50, 70))
CASSIDY = mk_hero("Cassidy", hp(250), weapons=[PEACEKEEPER])
WIDOW = mk_hero("Widowmaker", hp(225), weapons=[ADS])
TRACER = mk_hero("Tracer", hp(175))
ZEN = mk_hero("Zenyatta", hp(75, shield=175))
REIN = mk_hero("Reinhardt", {"role_queue": {"health": 400, "armor": 300, "shield": 0},
                             "open_queue": {"health": 250, "armor": 300, "shield": 0},
                             "6v6": {"health": 325, "armor": 225, "shield": 0}}, subrole="Stalwart")
MAUGA = mk_hero("Mauga", hp(575, armor=125), subrole="Bruiser")


def test_damage_at():
    v = PEACEKEEPER["damage"][0]
    check("衰减起点前满伤害", damage.damage_at(v, 25, 35, 10) == 70)
    check("衰减中点线性插值 (70+21)/2", abs(damage.damage_at(v, 25, 35, 30) - 45.5) < 1e-9)
    check("衰减终点后最低伤害", damage.damage_at(v, 25, 35, 50) == 21)
    check("无衰减数据恒为最大值", damage.damage_at(v, None, None, 100) == 70)


def test_armor_rule():
    pool = {"health": 100.0, "armor": 100.0, "shield": 0.0}
    damage.apply_instance(pool, 70)
    check("护甲：70 伤害减 7 → 63", pool["armor"] == 37)
    pool = {"health": 100.0, "armor": 100.0, "shield": 0.0}
    damage.apply_instance(pool, 10)
    check("护甲：10 伤害按 50% 上限 → 5", pool["armor"] == 95)
    pool = {"health": 100.0, "armor": 100.0, "shield": 0.0}
    damage.apply_instance(pool, 14)
    check("护甲：14 伤害正好减 7", pool["armor"] == 93)
    pool = {"health": 100.0, "armor": 10.0, "shield": 0.0}
    damage.apply_instance(pool, 70)
    check("护甲被打穿：整段减 7 后溢出到生命值 (63-10=53)", pool["armor"] == 0 and pool["health"] == 47)
    pool = {"health": 75.0, "armor": 0.0, "shield": 20.0}
    damage.apply_instance(pool, 70)
    check("护盾不减伤，溢出到生命值", pool["shield"] == 0 and pool["health"] == 25)


def test_shots_to_kill():
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, TRACER)
    check("Cassidy 身体 3 枪杀 Tracer", r["shots"] == 3 and r["damage_per_instance"] == 70)
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, TRACER, headshot=True)
    check("Cassidy 爆头 2 枪杀 Tracer", r["shots"] == 2 and r["damage_per_instance"] == 140)
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, TRACER, distance=30)
    check("30 米 45.5 伤害，4 枪杀 Tracer", r["shots"] == 4 and r["damage_per_instance"] == 45.5)
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, ZEN)
    check("Zenyatta 护盾+血量 250，4 枪", r["shots"] == 4)
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, REIN)
    check("Role Queue Reinhardt 11 枪（前 5 枪打护甲）", r["shots"] == 11
          and r["breakdown"][4] == {"shot": 5, "health": 385, "armor": 0, "shield": 0})
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, REIN, mode="open_queue")
    check("Open Queue Reinhardt 9 枪", r["shots"] == 9 and "open_queue health values" in r["assumptions"])
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, MAUGA, headshot=True)
    check("爆头打 Bruiser 减 25%：140 → 105", r["damage_per_instance"] == 105
          and any("Bruiser" in a for a in r["assumptions"]))
    r = damage.shots_to_kill(CASSIDY, PEACEKEEPER, REIN, headshot=True)
    check("Stalwart 不减爆头伤害", r["damage_per_instance"] == 140)


def test_pellets_and_variants():
    reaper = mk_hero("Reaper", hp(275), weapons=[SHOTGUN])
    r = damage.shots_to_kill(reaper, SHOTGUN, TRACER)
    check("Reaper 近距离 2 发杀 Tracer (115/发)", r["shots"] == 2 and r["instances_per_shot"] == 20)
    check("霰弹 assumptions 写明弹丸全中", any("20 pellets" in a for a in r["assumptions"]))
    r = damage.shots_to_kill(reaper, SHOTGUN, REIN)
    first = r["breakdown"][0]
    check("霰弹每颗弹丸单独受护甲减免 (5.75→2.875, ×20=57.5)", first["armor"] == 242.5)
    junk = mk_hero("Junkrat", hp(250), weapons=[FRAG])
    r = damage.shots_to_kill(junk, FRAG, TRACER)
    check("爆炸武器默认直击+满额溅射 125，2 发", r["shots"] == 2 and r["damage_per_instance"] == 125
          and any("splash" in a for a in r["assumptions"]))
    r = damage.shots_to_kill(junk, FRAG, TRACER, variant=1)
    check("指定 variant 只用溅射 80", r["damage_per_instance"] == 80)
    r = damage.shots_to_kill(WIDOW, ADS, TRACER, headshot=True, variant=1)
    check("Widowmaker 满蓄力爆头 2.5x = 300，一枪", r["shots"] == 1 and r["damage_per_instance"] == 300)
    check("结果标明用了哪个变体", r["variant"] == "at 100% power")


def test_variant_guards():
    reaper = mk_hero("Reaper", hp(275), weapons=[SHOTGUN])
    r = damage.shots_to_kill(reaper, SHOTGUN, MAUGA, variant=1)
    check("多弹丸武器选'每发总伤'变体被拒绝（否则 115×20）", "unsupported" in r and "variant 0" in r["unsupported"])
    volley = mk_weapon("Orb Alt Fire", [("per orb", 50, 50), ("per volley", 250, 250)], shot_type="proj")
    check("'per volley' 变体被拒绝", "unsupported" in damage.shots_to_kill(CASSIDY, volley, TRACER, variant=1))
    dot = mk_weapon("Chaingun", [("direct", 4, 1.2), ("per second (damage over time", 15, 15)], falloff=(30, 40))
    check("'per second' 变体被拒绝", "unsupported" in damage.shots_to_kill(CASSIDY, dot, TRACER, variant=1))
    check("负数 variant 被拒绝而不是取最后一个",
          "unsupported" in damage.shots_to_kill(WIDOW, ADS, TRACER, variant=-1))
    dart = mk_weapon("Biotic Rifle", [("over 0.59 seconds", 75, 75)], shot_type="proj", headshot=False)
    check("'over N seconds' 的单发伤害仍可计算", damage.shots_to_kill(CASSIDY, dart, TRACER)["shots"] == 3)


def test_unsupported():
    beam = mk_weapon("Biotic Grasp", [("", 65, 65)], shot_type="beam")
    melee = mk_weapon("Rocket Hammer", [("", 100, 100)], shot_type="melee", headshot=False)
    empty = mk_weapon("Mystery", [])
    check("光束不支持", "unsupported" in damage.shots_to_kill(CASSIDY, beam, TRACER))
    check("近战不支持", "unsupported" in damage.shots_to_kill(CASSIDY, melee, TRACER))
    check("没有伤害数据", "missing data" in damage.shots_to_kill(CASSIDY, empty, TRACER)["unsupported"])
    check("不能爆头的武器要求爆头",
          "cannot headshot" in damage.shots_to_kill(CASSIDY, FRAG, TRACER, headshot=True)["unsupported"])
    check("负距离", "distance" in damage.shots_to_kill(CASSIDY, PEACEKEEPER, TRACER, distance=-5)["unsupported"])
    check("未知模式", "unknown mode" in damage.shots_to_kill(CASSIDY, PEACEKEEPER, TRACER, mode="ffa")["unsupported"])
    check("variant 越界", "no damage variant" in damage.shots_to_kill(CASSIDY, PEACEKEEPER, TRACER, variant=3)["unsupported"])
    no_hp = mk_hero("Ghost", hp(None))
    check("目标血量缺失", "missing data" in damage.shots_to_kill(CASSIDY, PEACEKEEPER, no_hp)["unsupported"])


if __name__ == "__main__":
    test_damage_at()
    test_armor_rule()
    test_shots_to_kill()
    test_pellets_and_variants()
    test_variant_guards()
    test_unsupported()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
