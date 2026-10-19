"""竞猜按钮：custom_id 模板、View 生成、点击（押注、改选、截止后、ephemeral 回复）、票数编辑节流。
用真实的 discord.ui 组件 + 假的 Interaction / Message。

运行：python tests/test_prediction_ui.py
"""
import asyncio
import os
import re
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import prediction_ui as ui  # noqa: E402
import predictions as pr  # noqa: E402

results = []
NS = types.SimpleNamespace
START = 1_791_000_000


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


class Clock:
    def __init__(self, t=START - 3600):
        self.t = t

    def __call__(self):
        return self.t


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, content, ephemeral=False):
        self.sent.append((content, ephemeral))


class FakeMessage:
    def __init__(self):
        self.views = []

    async def edit(self, view=None):
        self.views.append(view)


def interaction(uid, name, message):
    return NS(user=NS(id=uid, display_name=name), response=FakeResponse(), message=message)


def labels(view):
    return [c.item.label for c in view.children]      # DynamicItem 包着真正的 Button


def store_with_match():
    s = pr.PredictionStore(":memory:")
    key = s.register({"id": "m1", "start": START, "team1": "T1", "team2": "ZETA DIVISION", "label": "x"}, 42, 7)
    return s, key


def test_view_and_template():
    key = pr.key_for("m1")
    view = ui.build_view({"key": key, "labels": ["T1", "ZETA DIVISION"], "counts": [3, 1], "disabled": False})
    check("View 持久化（timeout=None）", view.timeout is None)
    check("两个按钮：队名 · 票数", labels(view) == ["T1 · 3", "ZETA DIVISION · 1"])
    check("custom_id 符合模板", [c.item.custom_id for c in view.children] == [f"pred:{key}:1", f"pred:{key}:2"]
          and all(re.fullmatch(ui.TEMPLATE, c.item.custom_id) for c in view.children))
    disabled = ui.build_view({"key": key, "labels": ["A", "B"], "counts": [0, 0], "disabled": True})
    check("禁用的按钮", all(c.item.disabled for c in disabled.children))
    check("长队名截到 80 字符", len(ui.label("X" * 100, 12)) == 80)
    item = asyncio.run(ui.PredictButton.from_custom_id(None, NS(label="T1 · 3", disabled=False),
                                                       re.fullmatch(ui.TEMPLATE, f"pred:{key}:2")))
    check("from_custom_id 解析出 key 和 side", item.key == key and item.side == 2)


def test_pick_flow():
    s, key = store_with_match()
    clock = Clock()
    handler = ui.PredictionUI(s, clock, min_edit_interval=0)
    msg = FakeMessage()
    it = interaction(1, "Alice", msg)
    asyncio.run(handler.on_pick(it, key, 1))
    check("押注成功：只回复本人", it.response.sent == [("You picked **T1** — you can change it until the match starts.", True)])
    check("更新按钮票数", labels(msg.views[-1]) == ["T1 · 1", "ZETA DIVISION · 0"])
    it = interaction(1, "Alice", msg)
    asyncio.run(handler.on_pick(it, key, 2))
    check("改选：提示 Changed", it.response.sent[0][0].startswith("Changed your pick to **ZETA DIVISION**")
          and labels(msg.views[-1]) == ["T1 · 0", "ZETA DIVISION · 1"])
    clock.t = START
    it = interaction(2, "Bob", msg)
    asyncio.run(handler.on_pick(it, key, 1))
    check("开赛后点击：拒绝并说明，不改票数", it.response.sent == [("Picks are closed for this match.", True)]
          and s.counts(key) == (0, 1))


def test_button_callback():
    s, key = store_with_match()
    ui.PredictButton.handler = None
    it = interaction(1, "Alice", FakeMessage())
    asyncio.run(ui.PredictButton(key, 1).callback(it))
    check("没有 handler 时提示未启用", "not enabled" in it.response.sent[0][0])
    ui.PredictButton.handler = ui.PredictionUI(s, Clock(), min_edit_interval=0)
    it = interaction(1, "Alice", FakeMessage())
    asyncio.run(ui.PredictButton(key, 2).callback(it))
    check("按钮回调交给 handler", s.counts(key) == (0, 1))
    ui.PredictButton.handler = None


def test_throttle():
    s, key = store_with_match()
    clock = Clock()
    handler = ui.PredictionUI(s, clock, min_edit_interval=0.05)
    msg = FakeMessage()

    async def burst():
        for uid in range(1, 6):
            await handler.on_pick(interaction(uid, f"u{uid}", msg), key, 1)
        await asyncio.sleep(0.01)
        n_immediate = len(msg.views)
        clock.t += 0.05
        await asyncio.sleep(0.1)
        return n_immediate

    immediate = asyncio.run(burst())
    check("连续 5 次点击：第一次立即编辑，其余合并", immediate == 1)
    check("合并后补一次编辑，显示最终票数", len(msg.views) == 2 and labels(msg.views[-1]) == ["T1 · 5", "ZETA DIVISION · 0"])



def test_click_during_deferred_edit():
    s, key = store_with_match()
    handler = ui.PredictionUI(s, Clock(), min_edit_interval=0.05)

    class SlowMessage(FakeMessage):
        gate = None

        async def edit(self, view=None):
            if self.gate is not None:
                await self.gate.wait()                     # 模拟 Discord 编辑还没返回
            self.views.append(view)

    msg = SlowMessage()

    async def scenario():
        await handler.on_pick(interaction(1, "u1", msg), key, 1)   # 立即编辑
        await handler.on_pick(interaction(2, "u2", msg), key, 1)   # 排一次延后编辑
        msg.gate = asyncio.Event()
        await asyncio.sleep(0.08)                                   # 延后编辑已读到 2 票，卡在 edit 里
        await handler.on_pick(interaction(3, "u3", msg), key, 1)   # 这时又来一票
        msg.gate.set()
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    check("延后编辑进行中又来一票：最终按钮显示 3 票", labels(msg.views[-1]) == ["T1 · 3", "ZETA DIVISION · 0"])


def test_edit_failure_and_closed_label():
    s, key = store_with_match()

    class Broken:
        async def edit(self, view=None):
            raise RuntimeError("unknown message")
    handler = ui.PredictionUI(s, Clock(), min_edit_interval=0)
    it = interaction(1, "Alice", Broken())
    asyncio.run(handler.on_pick(it, key, 1))
    check("编辑失败不影响押注", s.counts(key) == (1, 0) and it.response.sent[0][1] is True)
    s.lock(key)
    msg = FakeMessage()
    asyncio.run(handler.refresh(msg, key))
    check("截止后刷新：按钮禁用", all(c.item.disabled for c in msg.views[-1].children))


if __name__ == "__main__":
    test_view_and_template()
    test_pick_flow()
    test_button_callback()
    test_throttle()
    test_click_during_deferred_edit()
    test_edit_failure_and_closed_label()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
