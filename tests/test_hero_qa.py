"""英雄问答测试：工具分发、tool use 循环（假 Claude 客户端）、bot 路由与冷却（假 Discord 消息）。

运行：python tests/test_hero_qa.py
"""
import asyncio
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import bot  # noqa: E402
from hero_qa import TOO_COMPLEX, TRUNCATED, TOOLS, Cooldown, HeroQA  # noqa: E402
from herodata import HeroStore, load_aliases  # noqa: E402
from heroparse import parse_hero  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures", "wiki")
results = []
NS = types.SimpleNamespace


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def make_store():
    store = HeroStore("unused.json", load_aliases())
    for name in ("Cassidy", "Tracer", "Reinhardt", "Moira", "Mauga"):
        with open(os.path.join(FIXTURES, name + ".wikitext"), encoding="utf-8") as f:
            store.heroes[name] = parse_hero(name, f.read())
    store.fetched_at = "2026-10-02T00:00:00Z"
    return store


def tool_use(id_, name, args):
    return NS(type="tool_use", id=id_, name=name, input=args)


def text(t):
    return NS(type="text", text=t)


class ScriptedClient:
    """按顺序返回预设的 Claude 响应，并记录每次请求。"""
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    async def create(self, **kw):
        self.requests.append(kw)
        return self.responses.pop(0)


def test_tools():
    qa = HeroQA(None, "m", make_store())
    stats = qa.run_tool("get_hero_stats", {"hero": "麦克雷"})
    check("get_hero_stats 用中文别名", stats["name"] == "Cassidy" and stats["fetched_at"] == "2026-10-02T00:00:00Z")
    check("get_hero_stats 不含补丁和 raw，武器带下标",
          "patches" not in stats and stats["weapons"][1]["index"] == 1 and "raw" not in stats["weapons"][0])
    check("未知英雄返回候选", qa.run_tool("get_hero_stats", {"hero": "xyz"}) ==
          {"error": "unknown_hero", "query": "xyz", "candidates": []})
    p = qa.run_tool("get_patch_history", {"hero": "Cassidy", "limit": 2})
    check("get_patch_history 限制条数、最新在前", len(p["patches"]) == 2 and p["patches"][0]["date"] == "2026-08-11")
    check("limit 超出范围被夹到 1..20", len(qa.run_tool("get_patch_history", {"hero": "Cassidy", "limit": 999})["patches"]) == 20
          and len(qa.run_tool("get_patch_history", {"hero": "Cassidy", "limit": 0})["patches"]) == 1)
    k = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "headshot": True})
    check("shots_to_kill 默认主武器 Peacekeeper，爆头 2 枪", k["weapon"] == "Peacekeeper" and k["shots"] == 2)
    k = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "weapon": "fan"})
    check("按名字片段选武器", k["weapon"] == "Fan the Hammer")
    k = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "weapon": "1"})
    check("按下标选武器", k["weapon"] == "Fan the Hammer")
    check("未知武器列出可选武器", qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "weapon": "rpg"})
          == {"error": "unknown_weapon", "weapons": ["Peacekeeper", "Fan the Hammer"]})
    k = qa.run_tool("shots_to_kill", {"attacker": "Reinhardt", "target": "Tracer"})
    check("只有近战武器的英雄：返回 unsupported", "unsupported" in k)
    k = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Mauga", "headshot": True, "mode": "open_queue"})
    check("模式参数传给计算器", k["mode"] == "open_queue")
    bad = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "distance": "30m"})
    check("距离不是数字时返回可读错误", "distance must be a number" in bad["error"])
    k = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "distance": "30"})
    check("字符串数字距离可以用", k["distance_m"] == 30.0 and k["shots"] == 4)
    check("未知工具", "error" in qa.run_tool("nope", {}))
    k = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "headshot": "false"})
    check("headshot 字符串 'false' 不算爆头", k["headshot"] is False and k["shots"] == 3)
    k = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "headshot": "True"})
    check("headshot 字符串 'True' 算爆头", k["headshot"] is True and k["shots"] == 2)
    bad = qa.run_tool("shots_to_kill", {"attacker": "Cassidy", "target": "Tracer", "headshot": "maybe"})
    check("headshot 无法识别时返回错误", "headshot" in bad.get("error", ""))


def test_default_weapon():
    store = make_store()
    gun = {"name": "Kunai", "fire": None, "shot_type": "proj", "headshot": True, "pellets": 1,
           "damage": [{"label": "", "max": 45.0, "min": 45.0}], "falloff_start": None, "falloff_end": None,
           "projectile_radius": 0.1, "raw": {}}
    heal = {**gun, "name": "Healing Ofuda", "damage": []}
    store.heroes["Healer"] = {**store.heroes["Tracer"], "name": "Healer", "weapons": [heal, gun]}
    qa = HeroQA(None, "m", store)
    k = qa.run_tool("shots_to_kill", {"attacker": "Healer", "target": "Tracer"})
    check("默认武器跳过没有伤害数据的治疗武器", k.get("weapon") == "Kunai" and k["shots"] == 4)
    k = qa.run_tool("shots_to_kill", {"attacker": "Healer", "target": "Tracer", "weapon": "ofuda"})
    check("unsupported 结果附带可选武器列表", "unsupported" in k and k["weapons"] == ["Healing Ofuda", "Kunai"])


def test_thinking_budget():
    client = ScriptedClient([NS(stop_reason="end_turn", content=[text("ok")])])
    asyncio.run(HeroQA(client, "m", make_store()).answer("q"))
    req = client.requests[0]
    check("max_tokens 给思考留足空间（默认开启 adaptive thinking）", req["max_tokens"] >= 8000)
    check("默认 effort low", req.get("output_config") == {"effort": "low"})
    client = ScriptedClient([NS(stop_reason="end_turn", content=[text("ok")])])
    asyncio.run(HeroQA(client, "m", make_store(), effort=None).answer("q"))
    check("effort=None 时不传 output_config", "output_config" not in client.requests[0])
    thinking_only = ScriptedClient([NS(stop_reason="max_tokens", content=[NS(type="thinking", thinking="")])])
    answer = asyncio.run(HeroQA(thinking_only, "m", make_store()).answer("站瑞希身边奶多少啊"))
    check("思考耗尽 max_tokens 时返回明确提示而不是空字符串", answer == TRUNCATED)


def test_tool_schema():
    desc = TOOLS[0]["input_schema"]["properties"]["hero"]["description"]
    check("工具说明要求英雄名按原话传、不要翻译", "exactly as the user wrote" in desc)


def test_loop():
    client = ScriptedClient([
        NS(stop_reason="tool_use", content=[
            tool_use("t1", "shots_to_kill", {"attacker": "卡西迪", "target": "猎空", "headshot": True}),
            tool_use("t2", "get_patch_history", {"hero": "Cassidy", "limit": 1})]),
        NS(stop_reason="end_turn", content=[text("2 发爆头即可击杀猎空。")]),
    ])
    answer = asyncio.run(HeroQA(client, "test-model", make_store()).answer("卡西迪爆头几枪杀猎空？"))
    check("循环结束后返回最终文本", answer == "2 发爆头即可击杀猎空。")
    first = client.requests[0]
    check("请求带 system 和 3 个工具", "system" in first and [t["name"] for t in first["tools"]] ==
          ["get_hero_stats", "get_patch_history", "shots_to_kill"])
    second = client.requests[1]["messages"]
    check("第二轮带上 assistant 的 tool_use 和 user 的 tool_result",
          second[1]["role"] == "assistant" and second[2]["role"] == "user")
    tr = second[2]["content"]
    check("每个 tool_use 对应一个 tool_result", [r["tool_use_id"] for r in tr] == ["t1", "t2"])
    check("tool_result 是计算器的 JSON", json.loads(tr[0]["content"])["shots"] == 2)

    looping = ScriptedClient([NS(stop_reason="tool_use", content=[tool_use(f"t{i}", "get_hero_stats", {"hero": "Ana"})])
                              for i in range(5)])
    check("超过 5 轮返回 TOO_COMPLEX", asyncio.run(HeroQA(looping, "m", make_store()).answer("x")) == TOO_COMPLEX)

    class Boom(HeroQA):
        def run_tool(self, name, args):
            raise ValueError("bad input")
    client = ScriptedClient([
        NS(stop_reason="tool_use", content=[tool_use("t1", "get_hero_stats", {"hero": "Cassidy"})]),
        NS(stop_reason="end_turn", content=[text("sorry")]),
    ])
    asyncio.run(Boom(client, "m", make_store()).answer("x"))
    err = json.loads(client.requests[1]["messages"][2]["content"][0]["content"])
    check("工具抛异常时把错误交给模型，不中断", err == {"error": "tool failed: bad input"})


def test_cooldown():
    c = Cooldown(5)
    check("第一次允许", c.allow(1, now=0))
    check("5 秒内拒绝", not c.allow(1, now=3))
    check("其他用户不受影响", c.allow(2, now=3))
    check("过了 5 秒允许", c.allow(1, now=5.1))


def test_routing():
    check("空文本 → connect", bot.parse_command("  ") == ("connect", ""))
    check("指令忽略大小写", bot.parse_command(" Status ") == ("status", "Status"))
    check("disconnect / help 不变", bot.parse_command("disconnect")[0] == "disconnect" and bot.parse_command("help")[0] == "help")
    check("其他文本 → ask，保留原文", bot.parse_command("Was Cassidy nerfed?") == ("ask", "Was Cassidy nerfed?"))
    check("回复 bot（没有显式 @）的普通文本不进问答", bot.parse_command("thanks!", explicit=False) == ("help", "thanks!"))
    check("回复 bot 时指令仍然可用", bot.parse_command("status", explicit=False) == ("status", "status"))


class FakeChannel:
    def typing(self):
        class _Ctx:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *a):
                return False
        return _Ctx()


class FakeMessage:
    def __init__(self, user_id=1):
        self.author = NS(id=user_id)
        self.channel = FakeChannel()
        self.replies = []

    async def reply(self, content):
        self.replies.append(content)


def make_bot(qa, store):
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)  # 不连 Discord，只测 _ask
    b.hero_qa, b.store, b.ask_cooldown = qa, store, Cooldown(5)
    return b


def test_ask():
    store = make_store()

    class FakeQA:
        def __init__(self, answer="x" * 3000, error=None):
            self.answer_text, self.error, self.questions = answer, error, []

        async def answer(self, q):
            self.questions.append(q)
            if self.error:
                raise self.error
            return self.answer_text

    m = FakeMessage()
    asyncio.run(make_bot(None, store)._ask(m, "q"))
    check("没有 API Key 时提示", "ANTHROPIC_API_KEY" in m.replies[0])
    m = FakeMessage()
    asyncio.run(make_bot(FakeQA(), HeroStore("unused.json"))._ask(m, "q"))
    check("数据未就绪时提示", "not ready" in m.replies[0])
    qa, m = FakeQA(), FakeMessage()
    b = make_bot(qa, store)
    asyncio.run(b._ask(m, "Cassidy HP?"))
    check("问题原样交给 HeroQA，回答截断到 1990 字符", qa.questions == ["Cassidy HP?"] and len(m.replies[0]) == 1990)
    asyncio.run(b._ask(m, "again"))
    check("冷却期内拒绝", "wait" in m.replies[1] and qa.questions == ["Cassidy HP?"])
    m = FakeMessage()
    asyncio.run(make_bot(FakeQA(error=RuntimeError("api down")), store)._ask(m, "q"))
    check("Claude 出错时回复简短错误", m.replies[0].startswith("Sorry"))


if __name__ == "__main__":
    test_tools()
    test_default_weapon()
    test_thinking_budget()
    test_tool_schema()
    test_loop()
    test_cooldown()
    test_routing()
    test_ask()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
