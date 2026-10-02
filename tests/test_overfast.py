"""OverFast 玩家查询：BattleTag 规范化、结果格式化、错误映射、指令解析。

运行：python tests/test_overfast.py
不访问网络，用 tests/fixtures 里保存的真实 OverFast 响应。
"""
import asyncio
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import overfast  # noqa: E402
from bot import parse_command  # noqa: E402

FIX = os.path.join(ROOT, "tests", "fixtures")
SUMMARY = json.load(open(os.path.join(FIX, "overfast_summary.json")))
STATS = json.load(open(os.path.join(FIX, "overfast_stats_summary.json")))

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def test_normalize():
    n = overfast.normalize_battletag
    check("# 转成 -", n("TeKrop#2217") == "TeKrop-2217")
    check("已是 - 格式保持不变", n("TeKrop-2217") == "TeKrop-2217")
    check("去掉首尾空白、保留大小写", n("  TeKrop#2217 ") == "TeKrop-2217")
    check("只有用户名也允许", n("TeKrop") == "TeKrop")
    check("空字符串无效", n("") is None)
    check("含空格无效", n("Te Krop#2217") is None)
    check("含斜杠无效（防止拼出别的 API 路径）", n("../heroes") is None)


def test_format():
    text = overfast.format_player("TeKrop-2217", SUMMARY, STATS)
    check("显示玩家名", "TeKrop" in text)
    check("显示有段位的职责", "Support Silver 4" in text)
    check("没有段位的职责显示为 unranked", "Tank unranked" in text)
    hero_lines = [l for l in text.splitlines() if l.startswith("- ")]
    check("最多列 5 个英雄", len(hero_lines) == 5)
    check("按游戏时长排序，第一是 Reinhardt", "Reinhardt" in hero_lines[0])
    check("时长换算成小时", "371h" in hero_lines[0])
    check("显示胜率", "% win" in hero_lines[0])
    check("消息长度在 Discord 限制内", len(text) <= 2000)

    private = overfast.format_player("Hidden-1", {**SUMMARY, "competitive": None}, {})
    check("私密生涯给出提示", "private" in private.lower())
    check("私密生涯不列英雄", not [l for l in private.splitlines() if l.startswith("- ")])


def test_errors():
    class Fake(overfast.OverFast):
        """按路径返回预设的 (状态码, JSON)，不访问网络。"""
        def __init__(self, respond):
            super().__init__()
            self.respond = respond
            self.paths = []

        async def _get(self, path):
            self.paths.append(path)
            return self.respond(path)

    ok = Fake(lambda path: (200, STATS if "/stats/" in path else SUMMARY))
    s, st = asyncio.run(ok.player("TeKrop-2217"))
    check("正常查询返回 summary 和 stats", s == SUMMARY and st == STATS)
    check("请求了 summary 和 stats/summary 两个接口",
          sorted(ok.paths) == ["/players/TeKrop-2217/stats/summary", "/players/TeKrop-2217/summary"])

    nf = Fake(lambda path: (404, {"error": "Player not found"}))
    try:
        asyncio.run(nf.player("Nobody-1"))
        check("404 映射为 PlayerNotFound", False)
    except overfast.PlayerNotFound:
        check("404 映射为 PlayerNotFound", True)

    busy = Fake(lambda path: (429, {"error": "rate limited"}))
    try:
        asyncio.run(busy.player("TeKrop-2217"))
        check("429 映射为 OverFastUnavailable", False)
    except overfast.OverFastUnavailable:
        check("429 映射为 OverFastUnavailable", True)


def test_parse_command():
    check("无参数指令（参数是原文）", parse_command("  STATUS ") == ("status", "STATUS"))
    check("player 参数保留大小写", parse_command("player TeKrop#2217") == ("player", "TeKrop#2217"))
    check("指令名不区分大小写", parse_command("Player  TeKrop-2217 ") == ("player", "TeKrop-2217"))
    check("空消息是 connect", parse_command("") == ("connect", ""))
    check("只写 player 没有参数", parse_command("player") == ("player", ""))
    check("analyze / label 是指令", parse_command("analyze")[0] == "analyze" and parse_command("Label")[0] == "label")
    check("其他文本是英雄问答", parse_command("player stats are weird today?")[0] == "player" and parse_command("Tracer HP")[0] == "ask")


if __name__ == "__main__":
    test_normalize()
    test_format()
    test_errors()
    test_parse_command()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
