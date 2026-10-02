"""识别管线测试：合成计分板（种子模板拼图）、文本解析、进度条、局面状态、标注队列。

运行：python tests/test_vision.py
不需要真实截图（仓库公开，截图含其他玩家昵称）、不需要网络。
"""
import os
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from labeling import LabelStore  # noqa: E402
from situation import Situation, fingerprint  # noqa: E402
from vision import layout, progress, text  # noqa: E402
from vision.heroes import DEAD, HeroMatcher  # noqa: E402
from vision.recognize import Recognizer, to_facts  # noqa: E402
from vision.text import Line  # noqa: E402

DATA = ROOT / "server" / "data"
MAPS = text.load_maps(DATA / "maps.json")
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def _tpl(label):
    return cv2.imread(str(next((DATA / "templates" / label).glob("*.png"))))


def synth_scoreboard(rows: dict, dx=0.0195, width=3440, height=1440):
    """在深色背景上把模板贴到 (team,row) 位置，表格整体横向偏移 dx。"""
    img = np.full((height, width, 3), (40, 30, 25), np.uint8)
    rng = np.random.default_rng(0)
    for (team, row), content in rows.items():
        x0, y0, x1, y1 = layout.portrait_box(width, team, row, dx)
        patch = content if isinstance(content, np.ndarray) else _tpl(content)
        img[y0:y0 + patch.shape[0], x0:x0 + patch.shape[1]] = patch
    img += rng.integers(0, 6, img.shape, dtype=np.uint8)     # 轻微噪声，避免完美像素匹配
    return cv2.imencode(".jpg", img)[1].tobytes()


def fake_ocr(header="攻击/护送|霓虹枢纽时间：2:48", objective="护送运载目标5:10", names=()):
    """按裁剪区域的大小判断是哪一次 OCR 调用：标题条最扁、昵称列最高。"""
    def ocr(img):
        h, w = img.shape[:2]
        if h > 0.5 * layout.H:                       # 昵称列
            _, _, oy = 0, 0, layout.row_center_y("ally", 1) - layout.HALF * layout.H
            keys = [(t, r) for t in layout.TEAMS for r in layout.ROWS]
            return [Line(n, 10, layout.row_center_y(t, r) - oy) for (t, r), n in zip(keys, names) if n]
        if w / max(h, 1) > 15:                       # 右上角标题（放大 2 倍后仍是长条）
            return [Line(header, w / 2, h / 2)]
        return [Line(objective, w / 2, h / 2)] if objective else []
    return ocr


ROWS = {("ally", 1): "dva", ("ally", 2): "vendetta", ("ally", 3): "venture", ("ally", 4): "illari",
        ("ally", 5): "kiriko", ("enemy", 1): "mauga", ("enemy", 2): "pharah", ("enemy", 3): "reaper",
        ("enemy", 4): "kiriko", ("enemy", 5): "_dead"}
NAMES = ["MUFFIN", "JCFRIDGE", "TROJI", "FAKEONE", "PIGBIE", "THIZZLE", "GOATGLENMAN", "SHORTERARROW",
         "ESILSZ", "PIRATEWORKER"]
HUD = cv2.imencode(".jpg", np.zeros((259, 3440, 3), np.uint8))[1].tobytes()


def test_recognize():
    matcher = HeroMatcher([DATA / "templates"])
    r = Recognizer(matcher, MAPS, fake_ocr(names=NAMES))
    rec = r.recognize(synth_scoreboard(ROWS), HUD)
    check("看到计分表", rec.table_found)
    got = {(s.team, s.row): s.label for s in rec.slots}
    check("10 行英雄全部正确（含横向偏移）", got == ROWS)
    check("昵称按行分配", [s.player for s in rec.slots] == NAMES)
    check("地图和模式来自地图表", rec.map and rec.map.en == "Neon Junction" and rec.map.mode == "Hybrid")
    check("目标文字判断进攻", rec.side == "Attack")

    bar_hud = cv2.imencode(".png", _bar(0.3))[1].tobytes()
    rec = Recognizer(matcher, MAPS, fake_ocr(names=NAMES, objective="护送运载目标5:10")).recognize(synth_scoreboard(ROWS), bar_hud)
    check("推车阶段读进度条", rec.stage == "1" and rec.progress is not None and abs(rec.progress - 0.3) < 0.03)
    for objective in ("闪点即将解锁：0:27", "46%", "加时！", "防守目标点B"):
        rec = Recognizer(matcher, MAPS, fake_ocr(names=NAMES, objective=objective)).recognize(synth_scoreboard(ROWS), bar_hud)
        ok = rec.progress is None and (rec.stage == "B" if "目标点" in objective else rec.stage is None)
        check(f"非推车阶段不读进度条：{objective}", ok)

    facts = to_facts(rec, lambda k: k.title())
    check("facts 里阵亡状态", facts["enemies"][4]["status"] == "dead" and facts["enemies"][4]["hero"] is None)

    # 一行换成模板库里没有的图 → unknown，并带裁剪图供标注
    rows = {**ROWS, ("enemy", 3): np.random.default_rng(1).integers(0, 255, _tpl("reaper").shape, dtype=np.uint8)}
    rec = r.recognize(synth_scoreboard(rows), HUD)
    unknown = [s for s in rec.slots if s.status == "unknown"]
    check("没见过的头像判为 unknown 而不是猜", len(unknown) == 1 and unknown[0].row == 3)
    check("unknown 带头像裁剪，尺寸和模板一致", unknown[0].crop is not None and unknown[0].crop.shape == _tpl("reaper").shape)

    empty = cv2.imencode(".jpg", np.zeros((1440, 3440, 3), np.uint8))[1].tobytes()
    check("没有计分表时 table_found=False", not r.recognize(empty, HUD).table_found)

    # 1080p：同样的布局按比例缩小后仍能识别
    small = cv2.resize(cv2.imdecode(np.frombuffer(synth_scoreboard(ROWS), np.uint8), 1), (2560, 1080))
    rec = r.recognize(cv2.imencode(".jpg", small)[1].tobytes(), HUD)
    check("1080p 截图缩放后英雄仍正确", {(s.team, s.row): s.label for s in rec.slots} == ROWS)


def test_text():
    check("完整中文名", text.match_map("攻击/护送霓虹枢纽时间：2:48", MAPS).en == "Neon Junction")
    check("错一个字也能对上", text.match_map("攻击/护送霓虹枢组时间", MAPS).en == "Neon Junction")
    check("乱码不硬猜", text.match_map("时间：2:48", MAPS) is None)
    check("模式以地图表为准", text.match_map("国王大道", MAPS).mode == "Hybrid")
    check("占领要点地图", text.match_map("占领要点|尼泊尔", MAPS).mode == "Control")
    check("护送 → 进攻", text.parse_side("护送运载目标") == "Attack")
    check("准备进攻 → 进攻", text.parse_side("准备进攻2.9") == "Attack")
    check("阻止 → 防守", text.parse_side("阻止运载目标") == "Defense")
    check("读不到 → None", text.parse_side("5:10") is None)
    check("A 点阶段", text.point_phase("进攻目标点A4:34") == "A")
    lines = [Line("90", 0, 10), Line("MUFFIN", 0, 12), Line("真心挚友", 0, 40), Line("Troji", 0, 100)]
    check("昵称过滤数字和中文称号，按行分配", text.assign_names(lines, [10, 100, 200], tol=40) == ["MUFFIN", "TROJI", None])


ATTACK_COLORS = {"fill": (255, 197, 14), "track": (65, 55, 150), "checkpoint": (98, 77, 241)}     # BGR
DEFENSE_COLORS = {"fill": (89, 53, 243), "track": (147, 110, 7), "checkpoint": (255, 170, 40)}


def _bar(fill_frac, diamonds=(0.556,), background=(0, 0, 0), side="Attack", mode="Hybrid"):
    """按实测颜色画一条推车进度条。进攻：蓝色填充、暗红轨道、红色检查点；防守相反。"""
    colors = ATTACK_COLORS if side == "Attack" else DEFENSE_COLORS
    hud = np.full((259, 3440, 3), background, np.uint8)
    cx, H = 1720, layout.H
    y = int(0.11 * H)
    gx0, gx1 = progress.GEOMETRY[mode]
    x0, x1 = int(cx + gx0 * H), int(cx + gx1 * H)
    xf = int(x0 + (x1 - x0) * fill_frac)
    hud[y - 3:y + 3, x0:x1] = colors["track"]
    hud[y - 3:y + 3, x0:xf] = colors["fill"]
    for d in diamonds:
        xd = int(x0 + (x1 - x0) * d)
        hud[y + 8:y + 16, xd - 5:xd + 5] = colors["checkpoint"]
    hud[y + 8:y + 16, x1 - 5:x1 + 5] = (40, 140, 228)     # 终点橙色菱形
    return hud


def test_progress():
    frac, cp = progress.read_bar(_bar(0.26), "Hybrid", "Attack")
    check("进度约 26%，第 1 段", abs(frac - 0.26) < 0.02 and cp == 1)
    frac, cp = progress.read_bar(_bar(0.7), "Hybrid", "Attack")
    check("过了检查点就是第 2 段", abs(frac - 0.7) < 0.02 and cp == 2)
    check("看不到进度条 → None", progress.read_bar(np.zeros((259, 3440, 3), np.uint8), "Hybrid", "Attack") == (None, None))
    # 好莱坞：橙红色墙面会被当成红色进度条，把起点拉到很左边，进度读高（实测 10% 读成 35%）
    frac, cp = progress.read_bar(_bar(0.10, background=(49, 84, 148)), "Hybrid", "Attack")
    check("橙红色背景下进度仍正确（约 10%）", frac is not None and abs(frac - 0.10) < 0.02 and cp == 1)
    hud = _bar(0.10)
    cx, H = 1720, layout.H
    hud[int(0.11 * H) - 3:int(0.11 * H) + 3, int(cx + 0.2 * H):int(cx + 0.21 * H)] = (255, 197, 14)  # 进度条右侧别的蓝色物体
    frac, _ = progress.read_bar(hud, "Hybrid", "Attack")
    check("只取从起点连续的蓝色，远处的蓝色不算", abs(frac - 0.10) < 0.02)

    # 防守方：颜色反过来（红色填充、暗蓝轨道、蓝色检查点）；中城实测读不到
    frac, cp = progress.read_bar(_bar(0.47, side="Defense"), "Hybrid", "Defense")
    check("防守方进度（红色填充）约 47%，第 1 段", frac is not None and abs(frac - 0.47) < 0.02 and cp == 1)
    frac, cp = progress.read_bar(_bar(0.6, side="Defense"), "Hybrid", "Defense")
    check("防守方过了蓝色检查点是第 2 段", cp == 2)
    # 纯推车图：没有 A 点的勾选框，进度条更靠左，有两个检查点
    frac, cp = progress.read_bar(_bar(0.5, diamonds=(0.338, 0.70), mode="Escort"), "Escort", "Attack")
    check("推车图进度约 50%，第 2 段", frac is not None and abs(frac - 0.5) < 0.02 and cp == 2)
    frac, cp = progress.read_bar(_bar(0.8, diamonds=(0.338, 0.70), side="Defense", mode="Escort"), "Escort", "Defense")
    check("推车图防守方第 3 段", cp == 3)
    check("攻防未知时不读", progress.read_bar(_bar(0.3), "Hybrid", None) == (None, None))
    check("非推车模式不读", progress.read_bar(_bar(0.3), "Control", "Attack") == (None, None))


def _facts(enemy5="Lucio", status5="alive", side="Attack", map_name="Neon Junction", stage="1"):
    def p(name, hero, status="alive"):
        return {"player": name, "hero": hero if status == "alive" else None,
                "hero_key": hero.lower() if status == "alive" else None, "status": status}
    return {"map": {"name": map_name}, "mode": "Hybrid", "side": side,
            "segment": {"checkpoint": stage, "progress": "~30%"},
            "allies": [p(n, h) for n, h in zip("ABCDE", ["DVa", "Vendetta", "Venture", "Illari", "Kiriko"])],
            "enemies": [p(n, h) for n, h in zip("FGHI", ["Mauga", "Pharah", "Reaper", "Kiriko"])]
                       + [p("J", enemy5, status5)],
            "unreadable": []}


def test_situation():
    s = Situation()
    f1 = s.update(_facts())
    check("首次不跳过", not s.should_skip(f1))
    s.remember_advice(f1, "https://discord/msg/1")
    f2 = s.update({**_facts(), "segment": {"checkpoint": "1", "progress": "~45%"}})
    check("同一段、英雄没变、只是进度变了 → 跳过", s.should_skip(f2))

    f3 = s.update(_facts(status5="dead"))
    check("阵亡玩家用记忆补全英雄", f3["enemies"][4]["hero_key"] == "lucio" and f3["enemies"][4]["status"] == "dead")
    check("阵亡不算变化 → 跳过", s.should_skip(f3))

    f4 = s.update(_facts(enemy5="Ana"))
    check("有人换英雄 → 不跳过", not s.should_skip(f4))
    f5 = s.update(_facts(stage="2"))
    check("换段 → 不跳过", not s.should_skip(f5))

    unknown = _facts(); unknown["enemies"][0].update(hero=None, hero_key=None, status="unknown")
    check("有未知英雄时指纹为 None，不跳过", fingerprint(unknown) is None)
    f6 = s.update(unknown)
    check("认不出的头像带上该玩家上次的英雄（可能换了，所以只作参考）",
          f6["enemies"][0]["last_seen_hero"] == "Mauga" and f6["enemies"][0]["hero"] is None)
    check("仍然不跳过", not s.should_skip(f6))

    s.update(_facts(side=None))
    check("读不到攻防时沿用同图上次的攻防", s.last_facts["side"] == "Attack")

    def obj(side, text):
        f = _facts(side=side); f["segment"]["detail"] = text; return f
    s3 = Situation()
    s3.update(obj("Attack", "进攻目标点A4:27"))
    killcam = s3.update(obj("Defense", "防守目标点A4:03"))
    check("单独一次读到相反攻防（击杀回放是对方视角）不换边", killcam["side"] == "Attack")
    check("不换边就不清空英雄记忆", s3.memory.get("J") == ("lucio", "Lucio"))
    s3.update(obj("Defense", "防守目标点A3:50"))
    check("连续两次读到相反攻防才换边", s3.last_facts["side"] == "Defense")
    s4 = Situation()
    s4.update(obj("Attack", "护送运载目标0:20"))
    check("准备阶段的攻防立即生效（换边一定经过准备阶段）", s4.update(obj("Defense", "准备防守0:34"))["side"] == "Defense")

    s2 = Situation()
    s2.remember_advice(s2.update(_facts(stage="1")), "https://discord/msg/9")
    dead_screen = s2.update({**_facts(), "segment": {"checkpoint": None, "progress": None}})
    check("死亡画面读不到阶段时沿用上次的阶段", dead_screen["segment"]["checkpoint"] == "1")
    check("沿用的阶段标记为 last seen", dead_screen["segment"].get("stale") is True)
    check("沿用阶段后局面没变 → 跳过", s2.should_skip(dead_screen))
    s.update(_facts(map_name="King's Row", status5="dead"))
    check("换图清空记忆，阵亡者不再补全", s.last_facts["enemies"][4]["hero_key"] is None)


def test_labeling():
    with tempfile.TemporaryDirectory() as tmp:
        matcher = HeroMatcher([])
        store = LabelStore(Path(tmp), matcher)
        crop = _tpl("reaper")
        pid, prompt = store.add(crop, {"team": "enemy", "row": 3, "player": "SHORTERARROW"})
        check("未知头像加入队列，新的要弹标注", pid and prompt and len(store.pending()) == 1)
        dup, prompt = store.add(crop.copy(), {"team": "enemy", "row": 3, "player": "ICEFROSTY"})
        check("没弹过的重复头像：不重复入队，但要弹", dup == pid and prompt and store.get(pid).meta["seen"] == 2)
        check("重复时更新为最新的昵称", store.get(pid).meta["player"] == "ICEFROSTY")
        store.mark_prompted(pid)
        check("刚弹过的重复头像不再弹", store.add(crop.copy(), {"team": "enemy", "row": 3}) == (pid, False))
        meta = store.get(pid).meta; meta["prompted_at"] -= 600
        (Path(tmp) / "unlabeled" / f"{pid}.json").write_text(__import__("json").dumps(meta))
        check("弹过但已过几分钟（菜单可能错过或失效）→ 再弹", store.add(crop.copy(), {"team": "enemy", "row": 3}) == (pid, True))
        other_id, prompt = store.add(_tpl("ana"), {"team": "enemy", "row": 5})
        check("不同头像单独入队", other_id != pid and prompt and len(store.pending()) == 2)
        check("标注后存成模板文件", store.label(pid, "reaper") and (Path(tmp) / "templates" / "reaper" / f"{pid}.png").exists())
        check("标注后立刻进入匹配器", "reaper" in matcher.labels)
        check("标注后移出队列", len(store.pending()) == 1)
        check("重复标注同一个返回 False", store.label(pid, "reaper") is False)
        other = store.pending()[0].id
        store.label(other, DEAD)
        check("可以标成阵亡类别", (Path(tmp) / "templates" / DEAD / f"{other}.png").exists())
        reloaded = HeroMatcher([Path(tmp) / "templates"])
        check("重启后从磁盘加载已标注模板", reloaded.labels == {"reaper", DEAD})


if __name__ == "__main__":
    test_recognize()
    test_text()
    test_progress()
    test_situation()
    test_labeling()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
