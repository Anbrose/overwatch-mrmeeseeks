"""单元测试：分析器的两步流程（用假的 Claude 客户端）+ 客户端 Tab 截图时序（用假截图函数）。

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


# ---------------- 分析器 ----------------
FACTS = {
    "map": {"name": "国王大道", "confidence": 0.9}, "mode": "运载", "side": "进攻",
    "segment": {"checkpoint": 2, "progress": "约40%", "detail": None, "confidence": 0.8},
    "allies": [{"player": "A", "hero": "D.Va", "role": "坦克", "confidence": 0.9}],
    "enemies": [{"player": None, "hero": "温斯顿", "role": "坦克", "confidence": 0.95}, {"hero": "猎空"}],
    "unreadable": [],
}


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


def test_analyzer():
    calls = []

    class FakeMessages:
        async def create(self, **kw):
            calls.append(kw)
            if "system" not in kw:   # 第 1 步：识别，故意包在 ```json 代码块里
                body = "```json\n" + json.dumps(FACTS, ensure_ascii=False) + "\n```"
            else:                    # 第 2 步：建议
                body = "维持现状\n- 理由（依据：敌方阵容）"
            return types.SimpleNamespace(content=[_Block(body)])

    a = analyzer.Analyzer.__new__(analyzer.Analyzer)
    a.model = "test-model"
    a.client = types.SimpleNamespace(messages=FakeMessages())

    facts, advice = asyncio.run(a.analyze(b"\xff\xd8x", b"\xff\xd8y"))
    check("识别结果 JSON 解析正确（含代码块包裹）", facts == FACTS)
    images = [b for b in calls[0]["messages"][0]["content"] if b["type"] == "image"]
    check("识别步骤发送了两张图", len(images) == 2)
    check("建议步骤不看图，只看事实", "image" not in json.dumps(calls[1]["messages"]))
    check("建议步骤带上了约束规则", "只能使用下面给出的数据" in calls[1]["system"])
    check("建议文本返回", advice.startswith("维持现状"))
    check("识别摘要格式",
          analyzer.format_facts(facts) == "地图 国王大道 ｜ 运载/进攻 ｜ 第2段 约40% ｜ 我方 D.Va ｜ 敌方 温斯顿、猎空")
    check("空识别结果不报错", "地图 ?" in analyzer.format_facts({}))
    try:
        analyzer._parse_json("没有 JSON")
        check("无 JSON 时抛错", False)
    except ValueError:
        check("无 JSON 时抛错", True)


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
    asyncio.run(_capture_flow())
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
