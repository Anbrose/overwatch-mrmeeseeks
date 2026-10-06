"""/report 问题反馈测试：名单解析、报告 embed、表单提交（假 Discord 交互）。

运行：python tests/test_report.py
"""
import asyncio
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import discord  # noqa: E402

import bot  # noqa: E402
import report  # noqa: E402
from registry import Registry  # noqa: E402

results = []
NS = types.SimpleNamespace


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def test_parse_bot_list():
    check("逗号分隔、去空白", report.parse_bot_list(" mrmeeseeks, MusicBot ,ModBot") == ["mrmeeseeks", "MusicBot", "ModBot"])
    check("空字符串 -> 空名单", report.parse_bot_list("") == [] and report.parse_bot_list(" , ,") == [])
    check("忽略大小写去重，保留第一次的写法", report.parse_bot_list("ModBot,modbot,MODBOT") == ["ModBot"])
    check("用户写的 Other 被去掉（末尾会自动加）", report.parse_bot_list("A,other,B") == ["A", "B"])
    many = report.parse_bot_list(",".join(f"bot{i}" for i in range(40)))
    check("最多 24 个，给 Other 留位置", len(many) == 24 and many[-1] == "bot23")
    check("名字截断到 100 字符", report.parse_bot_list("x" * 150) == ["x" * 100])


def test_parse_channel_id():
    check("数字 ID", report.parse_channel_id(" 123456789012345678 ") == 123456789012345678)
    check("空 -> None", report.parse_channel_id("") is None)
    check("非数字 -> None", report.parse_channel_id("#bot-issues") is None)


def test_embed():
    e = report.build_report_embed("MusicBot", "It stopped playing", "9pm", "1. /play\n2. silence",
                                  "<@1>", "<#2>")
    fields = {f.name: f.value for f in e.fields}
    check("标题带 bot 名", "MusicBot" in e.title)
    check("问题描述放在正文", e.description == "It stopped playing")
    check("报告人与来源频道", fields.get("Reported by") == "<@1>" and fields.get("From") == "<#2>")
    check("选填项有内容时显示", fields.get("When") == "9pm" and fields.get("Steps / links") == "1. /play\n2. silence")
    e = report.build_report_embed("MusicBot", "x", "", "  ", "<@1>", "<#2>")
    names = [f.name for f in e.fields]
    check("选填项为空时不显示", "When" not in names and "Steps / links" not in names)


class FakeChannel:
    def __init__(self, error=None):
        self.sent, self.error = [], error

    async def send(self, **kw):
        if self.error:
            raise self.error
        self.sent.append(kw)


class FakeResponse:
    def __init__(self):
        self.messages = []

    async def send_message(self, content, ephemeral=False):
        self.messages.append((content, ephemeral))


def make_interaction(channel):
    async def fetch_channel(_):
        if channel is None:
            raise discord.NotFound(NS(status=404, reason="Not Found"), "Unknown Channel")
        return channel
    client = NS(get_channel=lambda _: None, fetch_channel=fetch_channel)
    return NS(client=client, response=FakeResponse(), user=NS(mention="<@7>"),
              guild=NS(), channel_id=99)


async def submit(channel, bot_choice="MusicBot"):
    modal = report.ReportModal(["mrmeeseeks", "MusicBot"], 555)
    modal.bot._values = [bot_choice]  # Discord 回填的下拉值
    modal.what._value = "It stopped playing"
    modal.when._value = ""
    modal.steps._value = ""
    inter = make_interaction(channel)
    await modal.on_submit(inter)
    return inter


def test_modal():
    async def run():
        modal = report.ReportModal(["mrmeeseeks", "MusicBot"], 555)
        check("下拉选项是名单 + Other", [o.label for o in modal.bot.options] == ["mrmeeseeks", "MusicBot", "Other"])
        check("最多 5 个组件", len(modal.children) <= 5)

        ch = FakeChannel()
        inter = await submit(ch)
        embed = ch.sent[0]["embed"]
        check("提交后发到报告频道", len(ch.sent) == 1 and "MusicBot" in embed.title)
        check("来源频道是提交时所在频道", {f.name: f.value for f in embed.fields}["From"] == "<#99>")
        check("报告频道发送时不 @ 任何人", ch.sent[0]["allowed_mentions"].users is False)
        check("提交人收到私密确认", inter.response.messages == [(report.THANKS_TEXT, True)])

        inter = await submit(None)
        check("报告频道不存在时提示提交人", inter.response.messages[0][1] is True
              and "couldn't" in inter.response.messages[0][0])
        ch = FakeChannel(error=discord.Forbidden(NS(status=403, reason="Forbidden"), "Missing Access"))
        inter = await submit(ch)
        check("没有发送权限时提示提交人", "couldn't" in inter.response.messages[0][0])
    asyncio.run(run())


def test_bot_wiring():
    def names(**kw):
        return [c.name for c in bot.MeeseeksBot(Registry(), None, **kw).tree.get_commands()]
    check("配置齐全时注册 /report", names(report_bots=["A"], report_channel_id=1) == ["report"])
    check("缺频道 ID 时不注册", names(report_bots=["A"]) == [])
    check("缺名单时不注册", names(report_channel_id=1) == [])
    check("帮助里列出 /report", "/report" in bot.HELP_TEXT)


if __name__ == "__main__":
    test_parse_bot_list()
    test_parse_channel_id()
    test_embed()
    test_modal()
    test_bot_wiring()
    passed = sum(r for _, r in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
