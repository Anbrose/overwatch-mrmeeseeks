"""英雄页面解析测试：用 tests/fixtures/wiki/ 下保存的真实 wikitext（2026-10-02 抓取）。

运行：python tests/test_heroparse.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

from heroparse import parse_damage, parse_falloff, parse_hero  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "wiki")
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def hero(name):
    with open(os.path.join(FIXTURES, name + ".wikitext"), encoding="utf-8") as f:
        return parse_hero(name, f.read())


def weapon(h, name):
    return next(w for w in h["weapons"] if w["name"] == name)


def test_damage_strings():
    check("单值伤害", parse_damage("100") == [{"label": "", "max": 100.0, "min": 100.0}])
    check("衰减区间，en dash 也认", parse_damage("35 – 10.5") == [{"label": "", "max": 35.0, "min": 10.5}])
    check("多变体 + tt 模板取显示值",
          parse_damage("{{tt|45|125 total}} (direct hit)<br>80 - 10 (splash, enemy)") ==
          [{"label": "direct hit", "max": 45.0, "min": 45.0},
           {"label": "splash, enemy", "max": 80.0, "min": 10.0}])
    check("注释被去掉", parse_damage("14 - 4.2<!-- 30% min falloff-->") == [{"label": "", "max": 14.0, "min": 4.2}])
    check("解析不出数字时返回空列表", parse_damage("Varies") == [])
    check("衰减距离", parse_falloff("25 - 35 meters") == (25.0, 35.0))
    check("衰减距离只取第一段", parse_falloff("30 - 40 meters<br>10 - 20 meters (simultaneous fire)") == (30.0, 40.0))
    check("无衰减距离", parse_falloff(None) == (None, None) and parse_falloff("None") == (None, None))


def test_cassidy():
    h = hero("Cassidy")
    check("Cassidy 角色与副职业", h["role"] == "Damage" and h["subrole"] == "Sharpshooter")
    check("Cassidy 血量", h["hp"]["role_queue"] == {"health": 250, "armor": 0, "shield": 0})
    pk = weapon(h, "Peacekeeper")
    check("Peacekeeper 伤害", pk["damage"] == [{"label": "", "max": 70.0, "min": 21.0}])
    check("Peacekeeper 衰减", (pk["falloff_start"], pk["falloff_end"]) == (25.0, 35.0))
    check("Peacekeeper 可爆头、弹体半径", pk["headshot"] is True and pk["projectile_radius"] == 0.07)
    check("Peacekeeper 是主武器、hitscan", pk["fire"] == "Primary Fire" and pk["shot_type"] == "hitscan")
    check("Fan the Hammer 不能爆头", weapon(h, "Fan the Hammer")["headshot"] is False)
    check("补丁按日期倒序，首条 2026-08-11", h["patches"][0]["date"] == "2026-08-11"
          and all(a["date"] >= b["date"] for a, b in zip(h["patches"], h["patches"][1:])))
    check("perk 改动保留 perk 标签，方便说明只动了 perk", "Minor Perk" in h["patches"][0]["text"])
    check("补丁文本去掉了 wiki 标记", "{{" not in h["patches"][0]["text"] and "Giddy Up" in h["patches"][0]["text"])
    check("只取 owpvp 补丁（不含 stadium/ow1）", 15 <= len(h["patches"]) <= 40)
    check("来源链接", h["source_url"] == "https://overwatch.fandom.com/wiki/Cassidy")


def test_tank_modes():
    h = hero("Reinhardt")
    check("Reinhardt 副职业 Stalwart", h["role"] == "Tank" and h["subrole"] == "Stalwart")
    check("Open Queue 取基础值", h["hp"]["open_queue"] == {"health": 250, "armor": 300, "shield": 0})
    check("Role Queue 坦克 +150 生命值", h["hp"]["role_queue"] == {"health": 400, "armor": 300, "shield": 0})
    check("6v6 取 *6v6 字段", h["hp"]["6v6"] == {"health": 325, "armor": 225, "shield": 0})
    check("锤子是近战", weapon(h, "Rocket Hammer")["shot_type"] == "melee")


def test_other_heroes():
    z = hero("Zenyatta")
    check("Zenyatta 护盾", z["hp"]["role_queue"] == {"health": 75, "armor": 0, "shield": 175})
    r = weapon(hero("Reaper"), "Hellfire Shotguns")
    check("Reaper 霰弹：20 颗弹丸，第 0 个变体是单颗伤害",
          r["pellets"] == 20 and r["damage"][0] == {"label": "per pellet", "max": 5.75, "min": 1.725})
    check("Tracer 每发 2 颗子弹", weapon(hero("Tracer"), "Pulse Pistols")["pellets"] == 2)
    j = weapon(hero("Junkrat"), "Frag Launcher")
    check("Junkrat 直击 + 溅射两个变体", [v["label"] for v in j["damage"]] == ["direct hit", "splash, enemy"])
    m = hero("Moira")
    check("Moira：infobox 不规范也能解析", m is not None and m["hp"]["role_queue"]["health"] == 225)
    check("Moira 主武器是光束", "beam" in weapon(m, "Biotic Grasp")["shot_type"])
    mauga = hero("Mauga")
    check("Mauga：展开 #var 变量", weapon(mauga, "Volatile Chaingun")["damage"][0]["max"] == 4.0
          and weapon(mauga, "Volatile Chaingun")["falloff_start"] == 30.0)
    check("Mauga 副职业 Bruiser", mauga["subrole"] == "Bruiser")
    check("Ana：变量值里嵌套模板", weapon(hero("Ana"), "Biotic Rifle")["damage"][0]["max"] == 75.0)
    w = weapon(hero("Widowmaker"), "Widow's Kiss (ADS)")
    check("Widowmaker 开镜两个蓄力变体", [v["max"] for v in w["damage"]] == [12.0, 120.0])


def test_non_hero():
    check("不是英雄页面时返回 None", parse_hero("Heroes", "Some text\n==Heading==") is None)


def test_abilities():
    c = hero("Cassidy")
    names = [a["name"] for a in c["abilities"]]
    check("技能按页面顺序：武器、技能、终极", names == ["Peacekeeper", "Fan the Hammer", "Combat Roll", "Flashbang", "Deadeye"])
    check("已移除的技能不列出（Magnetic Grenade、(old)）",
          not any("Magnetic" in n or "(old)" in n for n in names))
    roll = next(a for a in c["abilities"] if a["name"] == "Combat Roll")
    check("技能类型", roll["type"] == "ability")
    check("按键 ability 1 → Shift", roll["key"] == "Shift")
    check("官方描述是纯文本", roll["description"] == "Roll in the direction you're moving to take reduced damage and reload.")
    check("冷却时间", roll["stats"].get("cooldown") == "5 seconds")
    check("tt 模板取显示值", roll["stats"].get("duration") == "0.4 seconds")
    check("长篇 ability_details / 视频 / 图片不放进 stats",
          not any(k in roll["stats"] for k in ("ability_details", "ability_video", "ability_image", "ability_keywords")))
    dead = next(a for a in c["abilities"] if a["name"] == "Deadeye")
    check("终极技能类型和按键 Q", dead["type"] == "ultimate" and dead["key"] == "Q")
    peace = next(a for a in c["abilities"] if a["name"] == "Peacekeeper")
    check("武器类型和主/副攻击", peace["type"] == "weapon" and peace["key"] == "Primary Fire")

    perks = c["perks"]
    check("天赋只列现行的（已移除的不列）",
          sorted((p["tier"], p["name"]) for p in perks) ==
          [("major", "Rollin' Round-Up"), ("major", "Silver Bullet"), ("minor", "Bang Bang"), ("minor", "Giddy Up")])
    check("天赋带描述", all(p["description"] for p in perks))

    j = hero("Junkrat")
    passive = [a for a in j["abilities"] if a["type"] == "passive"]
    check("被动技能", [a["name"] for a in passive] == ["Total Mayhem"] and passive[0]["key"] == "Passive")
    for name in ("Ana", "Mauga", "Moira", "Reaper", "Reinhardt", "Tracer", "Widowmaker", "Zenyatta"):
        h = hero(name)
        kinds = {a["type"] for a in h["abilities"]}
        if not ({"weapon", "ability", "ultimate"} <= kinds and len(h["perks"]) == 4):
            check(f"{name}：有武器/技能/终极，现行天赋 4 个", False)
            break
    else:
        check("其余 8 个英雄：都有武器/技能/终极，现行天赋都是 4 个", True)


def _page(*abilities):
    body = "\n".join("{{Ability details\n" + "\n".join(f"| {k} = {v}" for k, v in a.items()) + "\n}}" for a in abilities)
    return "{{Infobox character\n| role = [[Damage]]\n| health = 250\n}}\n== Abilities ==\n" + body


def test_ability_variants():
    # 线上页面里有、10 个 fixture 里没有的写法（D.Mon、Emre、Sierra、Symmetra，2026-10-03 核对）
    h = parse_hero("X", _page(
        {"ability_name": "Plasma Saber", "ability_type": "Weapon;;Mech", "official_description": "Slash."},
        {"ability_name": "Synthetic Burst Rifle", "ability_type": "Weapon (Hip Fire)", "key": "primary fire",
         "official_description": "Burst fire.", "damage": "20"},
        {"ability_name": "Propulsors", "ability_type": "Ability (Mech)", "key": "ability 1", "official_description": "Fly."},
        {"ability_name": "Limit Break", "ability_type": "Ultimate Ability (Mech)", "key": "ultimate", "official_description": "Ult."},
        {"ability_name": "Call Mech", "ability_type": "Ultimate Ability (Pilot)", "key": "ultimate", "official_description": "Mech."},
        {"ability_name": "Teleporter", "ability_type": "Ability", "key": "Ability 2", "official_description": "Teleport."},
        {"ability_name": "Shield Generator", "ability_type": "Ultimate Ability", "removed": "yes", "official_description": "Old."},
        {"ability_name": "Precision Fusion", "ability_type": "Major Perk", "key": "reload", "official_description": "Perk."},
        # Mercy 页面上忘了标 removed 的旧天赋
        {"ability_name": "Flash Heal (old)", "ability_type": "Major Perk", "official_description": "Old perk."},
    ))
    by = {a["name"]: a for a in h["abilities"]}
    check("括号写法的技能类型：Ability (Mech)", by.get("Propulsors", {}).get("type") == "ability")
    check("括号写法的武器类型：Weapon (Hip Fire)", by.get("Synthetic Burst Rifle", {}).get("type") == "weapon")
    check("括号写法的武器也进入武器数据（几枪击杀用）", any(w["name"] == "Synthetic Burst Rifle" for w in h["weapons"]))
    check("括号里的限定词作为形态/开火方式", by.get("Propulsors", {}).get("form") == "Mech"
          and by.get("Synthetic Burst Rifle", {}).get("key") == "Primary Fire")
    check("两个终极（机甲/驾驶员）都保留", by.get("Limit Break", {}).get("type") == "ultimate"
          and by.get("Call Mech", {}).get("form") == "Pilot")
    check("key = ultimate → Q", by.get("Limit Break", {}).get("key") == "Q")
    check("key 大小写不敏感（Ability 2 → E）", by.get("Teleporter", {}).get("key") == "E")
    check("removed = yes 也算已移除", "Shield Generator" not in by)
    check("名字带 (old) 但没标 removed 的也不列", [p["name"] for p in h["perks"]] == ["Precision Fusion"])
    check("分号写法的武器形态：Weapon;;Mech", by.get("Plasma Saber", {}).get("form") == "Mech")


if __name__ == "__main__":
    test_damage_strings()
    test_abilities()
    test_ability_variants()
    test_cassidy()
    test_tank_modes()
    test_other_heroes()
    test_non_hero()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
