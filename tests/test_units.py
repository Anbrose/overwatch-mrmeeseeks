"""单元测试：分析器给建议（用假的 Claude 客户端）+ 客户端 Tab 截图时序（用假截图函数）。

运行：python tests/test_units.py
不需要 Discord、Claude API，也不需要显示器。
"""
import asyncio
import json
import os
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.join(ROOT, "client"))

import analyzer  # noqa: E402
import client as cl  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


# ---------------- 分析器（只给建议） ----------------
def _p(player, hero, status="alive"):
    return {"player": player, "hero": hero, "hero_key": hero and hero.lower(), "status": status}


FACTS = {
    "map": {"name": "King's Row"}, "mode": "Hybrid", "side": "Attack",
    "segment": {"checkpoint": "2", "progress": "~40%"},
    "allies": [_p("A", "D.Va"), _p("B", None, "unknown"), _p("C", None, "empty")],
    "enemies": [_p("D", "Winston"), _p("E", "Tracer", "dead")],
    "unreadable": [],
}


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


def _analyzer(create, effort="low"):
    a = analyzer.Analyzer.__new__(analyzer.Analyzer)
    a.model, a.effort = "test-model", effort
    a.client = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    return a


def test_analyzer():
    calls = []

    async def create(**kw):
        calls.append(kw)
        return types.SimpleNamespace(content=[_Block("Stay as is\n- reason (basis: enemy comp)")],
                                     stop_reason="end_turn")

    advice = asyncio.run(_analyzer(create).advise(FACTS))
    check("建议文本返回", advice.startswith("Stay as is"))
    check("只发文字、不发图", "image" not in json.dumps(calls[0]["messages"]))
    check("带上识别出的 facts", "King's Row" in calls[0]["messages"][0]["content"])
    check("带上约束规则", "Use only the data provided below" in calls[0]["system"])
    check("effort 传给 API", calls[0]["output_config"] == {"effort": "low"})
    asyncio.run(_analyzer(create, effort=None).advise(FACTS))
    check("effort 为 None 时不传 output_config", "output_config" not in calls[1])

    text = analyzer.format_facts(FACTS)
    check("摘要第一行：地图 · 模式 · 攻防 · 阶段",
          text.splitlines()[0] == "King's Row · Hybrid · Attack · checkpoint 2 (~40%)")
    check("我方：未知显示 ?，未选显示 (not picked)", text.splitlines()[1] == "Allies: D.Va, ?, (not picked)")
    check("敌方：阵亡的补全英雄带 †", text.splitlines()[2] == "Enemies: Winston, Tracer†")
    check("阵亡且没有记忆显示 ?†", analyzer.format_facts(
        {**FACTS, "enemies": [_p("X", None, "dead")]}).splitlines()[2] == "Enemies: ?†")
    check("沿用的阶段标注 last seen", "checkpoint 1 (last seen)" in analyzer.format_facts(
        {**FACTS, "segment": {"checkpoint": "1", "progress": None, "stale": True}}))
    check("A 点阶段显示 point A",
          "point A" in analyzer.format_facts({**FACTS, "segment": {"checkpoint": "A"}}))
    check("地图读不出时附 OCR 原文", "Map ? (OCR: 'xx')" in analyzer.format_facts(
        {**FACTS, "map": {"name": None}, "unreadable": ["map (OCR: 'xx')"]}))
    check("空 facts 不报错", "Map ?" in analyzer.format_facts({}))


def test_thinking_budget():
    """adaptive thinking 的思考也计入 max_tokens：截断时要明确报 max_tokens。"""
    async def truncated(**kw):
        return types.SimpleNamespace(content=[types.SimpleNamespace(type="thinking", thinking="")],
                                     stop_reason="max_tokens")
    try:
        asyncio.run(_analyzer(truncated).advise(FACTS))
        check("输出被截断时报错说明 max_tokens", False)
    except ValueError as e:
        check("输出被截断时报错说明 max_tokens", "max_tokens" in str(e))
    check("max_tokens 足够大，留给思考", analyzer.MAX_TOKENS >= 16000)


# ---------------- 客户端截图时序 ----------------
async def _capture_flow():
    grabs = []

    def fake_grab(monitor, top_ratio=None, quality=85):
        grabs.append(top_ratio)
        return b"top" if top_ratio else b"full"

    cl.grab_jpeg = fake_grab
    args = cl.parse_args(["--cooldown", "1", "--score-delay", "0.05", "--hud-delay", "0.05"])
    c = cl.MeeseeksClient(args)
    sent = []

    async def fake_send(**m):
        sent.append(m)

    c.send = fake_send
    c.ws = object()
    c.paired = True
    q = asyncio.Queue()
    task = asyncio.create_task(c.capture_loop(q))
    now = time.monotonic

    async def hold(seconds=0.3):
        q.put_nowait(("down", now()))
        await asyncio.sleep(seconds)
        q.put_nowait(("up", now()))
        await asyncio.sleep(0.3)

    await hold()
    check("正常按住松开 → 发送 1 组截图", len(sent) == 1 and sent[0]["type"] == "snapshot")
    check("先截全屏计分板，再截顶部进度条", grabs[:2] == [None, 0.18])
    await hold()
    check("冷却时间内不重复发送", len(sent) == 1)
    await asyncio.sleep(1)
    t = now()
    q.put_nowait(("down", t))
    q.put_nowait(("up", t + 0.05))
    await asyncio.sleep(0.3)
    check("轻点 Tab 视为误触", len(sent) == 1)
    c.paired = False
    await hold()
    check("未配对时不发送", len(sent) == 1)
    c.paired = True
    await hold()
    check("重新配对后恢复发送", len(sent) == 2)
    task.cancel()


if __name__ == "__main__":
    test_analyzer()
    test_thinking_budget()
    asyncio.run(_capture_flow())
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
