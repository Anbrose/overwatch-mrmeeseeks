"""私信 bot：不用 @ 也当指令；频道设置类指令在私信里拒绝。"""
import asyncio
import os
import sys
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server"))
import bot  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def make_bot():
    b = bot.MeeseeksBot.__new__(bot.MeeseeksBot)
    b.wish_channel_id, b.wish_desk, b.wish_plans, b.announce_desk = None, None, None, None
    b._connection = NS(user=NS(id=1))
    b.calls = []
    for name in ("_ask", "_watch", "_esports", "_patches", "_region", "_live", "_predictions", "_player",
                 "_announce", "_connect_flow", "_status"):
        async def handler(message, *args, _name=name):
            b.calls.append((_name, args))
        setattr(b, name, handler)
    return b


def message(content, dm=True, mention=False):
    replies = []

    async def reply(content=None, **kw):
        replies.append(content)

    return NS(author=NS(id=7, bot=False, display_name="小红"), mentions=[NS(id=1)] if mention else [], content=content,
              channel=NS(id=99), guild=None if dm else NS(), reply=reply), replies


def run(content, **kw):
    b = make_bot()
    msg, replies = message(content, **kw)
    asyncio.run(b.on_message(msg))
    return b.calls, replies


def test_dm():
    calls, replies = run("卡西迪最近被削了吗")
    check("私信不用 @ 也走英雄问答", calls == [("_ask", ("卡西迪最近被削了吗",))] and replies == [])
    calls, _ = run("<@1> Tracer HP", mention=True)
    check("私信里写了 @ 也照常", calls == [("_ask", ("Tracer HP",))])
    calls, _ = run("一起看 湖南卫视")
    check("私信里一起看照样支持", calls == [("_watch", ("湖南卫视",))])
    calls, _ = run("player Name#1234")
    check("私信里 player 照常", calls == [("_player", ("Name#1234",))])
    calls, _ = run("predictions")
    check("私信里可以看竞猜榜", calls == [("_predictions", ("",))])
    calls, replies = run("")
    check("私信只发图片 → 帮助，不进入配对", calls == [] and replies == [bot.HELP_TEXT])
    calls, _ = run("connect")
    check("私信里明说 connect 才配对", calls == [("_connect_flow", ())])
    for text in ("esports here", "patches status", "region cn", "live list", "announce", "predictions reset"):
        calls, replies = run(text)
        check(f"私信里拒绝 `{text}`", calls == [] and replies == [bot.DM_GUILD_ONLY])


def test_guild_unchanged():
    calls, replies = run("卡西迪最近被削了吗", dm=False)
    check("频道里没 @ 不理会", calls == [] and replies == [])
    calls, _ = run("<@1> esports here", dm=False, mention=True)
    check("频道里 esports 照常", calls == [("_esports", ("here",))])
    calls, _ = run("<@1>", dm=False, mention=True)
    check("频道里只 @ 仍是配对", calls == [("_connect_flow", ())])


def test_help():
    check("帮助提到私信用法", "DM" in bot.HELP_TEXT)
    check("帮助不超过 Discord 2000 字上限", len(bot.HELP_TEXT) < 2000)


if __name__ == "__main__":
    test_dm()
    test_guild_unchanged()
    test_help()
    passed = sum(ok for _, ok in results)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
