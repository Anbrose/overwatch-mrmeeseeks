"""对阵图：用 tests/fixtures/esports/liquipedia_owcs_korea_stage3.json 的真实阵容画图，
检查尺寸、队徽大小一致、长队名缩字号、没有队徽 / 队徽损坏 / 没有阵容时照样出图。

运行：python tests/test_roster_card.py
"""
import io
import json
import os
import sys
import tempfile

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import roster_card as rc  # noqa: E402
import rosters  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "esports", "liquipedia_owcs_korea_stage3.json")
results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def teams():
    with open(FIXTURE, encoding="utf-8") as f:
        return rosters.parse_rosters(json.load(f)["parse"]["text"])


def logo_file(d, name, size, color):
    path = os.path.join(d, name)
    Image.new("RGBA", size, color).save(path)
    return path


def png(data):
    im = Image.open(io.BytesIO(data))
    im.load()
    return im


def test_layout_helpers():
    t = teams()
    rows = rc.role_rows(t["T1"], t["Crazy Raccoon"])
    check("职责行：坦克、输出、全能、支援、教练，行数取两队较多的一方",
          [(r, n) for r, n in rows] == [("tank", 2), ("dps", 2), ("flex", 1), ("support", 2), ("coach", 3)])
    check("只有一队有阵容时按那一队排", [r for r, _ in rc.role_rows(t["T1"], None)] == ["tank", "dps", "support", "coach"])
    check("没有职责的选手单独一行放在最后",
          rc.role_rows([{"id": "a", "role": None, "flag": None}], None) == [(None, 1)])
    wide, square, tall = rc.logo_size(300, 119), rc.logo_size(300, 300), rc.logo_size(40, 400)
    check("方形队徽 140×140", square == (140, 140))
    check("横条队徽面积和方形接近，宽不超过 240",
          wide[0] <= 240 and abs(wide[0] * wide[1] - 140 * 140) / (140 * 140) < 0.1)
    check("竖长队徽高不超过 160", tall[1] <= 160)
    check("首字母", [rc.initials(n) for n in ("ZETA DIVISION", "T1", "Crazy Raccoon", "O2 Blast", "")]
          == ["ZD", "T1", "CR", "OB", "?"])
    text, f = rc.fit_text("Twisted Minds Orchid Esports", 420, 46, 800)
    check("长队名缩小字号放进一栏", text == "Twisted Minds Orchid Esports" and f.getlength(text) <= 420 and f.size < 46)
    check("短队名保持原字号", rc.fit_text("T1", 420, 46, 800) == ("T1", rc.font(46, 800)))
    huge = "A VERY VERY LONG TEAM NAME ESPORTS CLUB INTERNATIONAL"
    text, f = rc.fit_text(huge, 420, 46, 800)
    check("最小字号也放不下：截断加省略号", text.endswith("…") and huge.startswith(text[:-1])
          and f.getlength(text) <= 420)


def test_render():
    t = teams()
    with tempfile.TemporaryDirectory() as d:
        red = logo_file(d, "red.png", (300, 119), (230, 20, 40, 255))
        white = logo_file(d, "white.png", (300, 300), (255, 255, 255, 255))
        im = png(rc.render_card("T1", "ZETA DIVISION", t["T1"], t["ZETA DIVISION"], red, white))
        check("宽 1000 的 PNG", im.format == "PNG" and im.width == rc.WIDTH)
        check("高度随行数增加（4 类 × 2 人 + 3 名教练）", im.height == rc.height_for(rc.role_rows(t["T1"], t["ZETA DIVISION"])))
        left = im.crop((0, 0, rc.WIDTH // 2, rc.HEADER)).convert("RGB")
        right = im.crop((rc.WIDTH // 2, 0, rc.WIDTH, rc.HEADER)).convert("RGB")
        reds = sum(1 for p in left.getdata() if p[0] > 200 and p[1] < 60 and p[2] < 80)
        whites = sum(1 for p in right.getdata() if min(p) > 245)
        check("左队队徽画在左上", reds > 5000)
        check("两个队徽面积接近（按面积统一大小）", 0.7 < reds / max(whites, 1) < 1.3)

        broken = os.path.join(d, "broken.png")
        with open(broken, "wb") as f:
            f.write(b"not a png")
        im2 = png(rc.render_card("T1", "ZETA DIVISION", t["T1"], t["ZETA DIVISION"], broken, None))
        check("队徽损坏或没有队徽：用首字母代替，照样出图", im2.width == rc.WIDTH and im2.height == im.height)

        huge = logo_file(d, "huge.png", (2000, 1100), (230, 20, 40, 255))
        im5 = png(rc.render_card("T1", "ZETA DIVISION", t["T1"], t["ZETA DIVISION"], huge, white))
        reds5 = sum(1 for p in im5.crop((0, 0, rc.WIDTH // 2, rc.HEADER)).convert("RGB").getdata()
                    if p[0] > 200 and p[1] < 60 and p[2] < 80)
        check("像素过多的队徽不解码，改用首字母", reds5 == 0)

        im3 = png(rc.render_card("T1", "Mystery Team", t["T1"], None, red, None))
        check("一队没有阵容：照样出图，按有阵容的一队排", im3.height == rc.height_for(rc.role_rows(t["T1"], None)))
        im4 = png(rc.render_card("TBD", "Mystery Team", None, None, None, None))
        check("双方都没有阵容：只有表头和一行说明", im4.height == rc.height_for([]) and im4.height < im3.height)


if __name__ == "__main__":
    test_layout_helpers()
    test_render()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
