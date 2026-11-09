"""一起看后台标签页实测：观众的页面切到后台（hidden + 定时器被限到每分钟一次）时同步会不会乱。

运行：python tests/test_watch_lab_background.py [--headed] [--trace out.json]
需要和 tests/test_watch_sync_browser.py 一样：playwright、Chrome / Edge、ffmpeg、联网；缺了就 SKIP。约 2 分钟。

Playwright 控制下的 Chrome 不会真的把页面标成 hidden、也不会限速（试过最小化窗口、同窗口开新标签、
Page.setWebLifecycleState 冻结），所以这里由 tests/watch_lab.py 的 BG_JS 模拟后台的两个可观察效果：
document.visibilityState 变 hidden，setInterval 的回调最多每分钟跑一次（Chrome 对隐藏超过 5 分钟、没在出声
的页面就是这样；在出声的页面只限到每秒一次，对我们每秒一次的同步没有影响）。视频本身在后台继续播，这和 Chrome 一致。

场景：
- 观众在后台 30 秒（同步最多跑一次）：视频自己以 1x 走，切回来时仍贴着真值，没有跳转、没有「同步」按钮、
  WebSocket 没断、没有「画面前跳」回报；
- 观众在后台时房主拖动 / 暂停 / 继续：这些是 WebSocket 消息直接触发的，不走定时器，后台的观众也要立刻跟；
- 观众在后台时自己卡了 4 秒（模拟）：切回来后按观众规则处理——不跳、1.05x、显示「落后 N 秒 · 同步」；
- 房主在后台 20 秒：房主是基准线，它的同步不跑也不影响任何人；切回来后不发多余的 control。
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from watch_lab import (Bench, big_seeks, drifts, fmt, gap, jumps, max_abs, requirements_or_skip, skip, Lab)  # noqa: E402

CLIP_SECONDS = 240
MINUTE = 60000
results = []


def check(name, cond, note=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{note}]" if note else ""), flush=True)


async def main(args):
    requirements_or_skip()
    async with Bench({"main": CLIP_SECONDS}, headed=args.headed) as b:
        tail = b.mount("main", "/main")
        await b.start_cdn()
        room = await b.make_room(b.show("测试片", {"本地": [tail]}))
        duration = room.video.duration

        host = await b.open_client("房主", b.host_url(room), bg_shim=True)
        viewer = await b.open_client("观众", b.viewer_url(room), bg_shim=True)
        lab = Lab(room)
        lab.add("host", host)
        lab.add("viewer", viewer)
        try:
            await asyncio.gather(host.click_start(), viewer.click_start())
        except Exception as e:
            s = await lab.take()
            skip(f"播放器没就绪（可能没联网取不到 hls.js）：{e}；stage={s['host']['stage']!r}")
        await host.page.click("#play")
        ok, _ = await lab.wait_until(lambda s: s["host"]["ct"] > 1 and s["viewer"]["ct"] > 1, 15, "起播")
        check("起播：两边都在走", ok)
        await lab.run(3, "起播后稳定")
        s = await lab.take()
        check("两边都报告 visible、已连接", s["host"]["visibility"] == "visible" and s["viewer"]["visibility"] == "visible"
              and s["host"]["connected"] and s["viewer"]["connected"])

        # ---- 1. 观众切到后台 30 秒 ----
        before = await lab.take()
        await viewer.background(True, MINUTE)
        seg = await lab.run(30, "观众后台 30 秒")
        check("后台时页面报告 hidden", all(s["viewer"]["visibility"] == "hidden" for s in seg))
        check("后台 30 秒视频自己照常前进（约 1x）",
              abs((seg[-1]["viewer"]["ct"] - seg[0]["viewer"]["ct"]) - (seg[-1]["t"] - seg[0]["t"])) <= 1.0,
              f"走了 {seg[-1]['viewer']['ct'] - seg[0]['viewer']['ct']:.1f} 秒 / {seg[-1]['t'] - seg[0]['t']:.1f} 秒")
        check("后台 30 秒没有跳转", big_seeks([before] + seg, "viewer") == 0)
        vid = await viewer.viewer_id(room.room_id)
        check("后台 30 秒 WebSocket 没断", all(s["viewer"]["connected"] for s in seg)
              and any(c.viewer_id == vid for c in b.conns(room)))
        await viewer.background(False, 0)
        seg = await lab.run(4, "观众切回来")
        print(f"  切回来：观众 vs 真值 {fmt(drifts(seg, 'viewer'))}；倍速 {seg[-1]['viewer']['rate']}；按钮 {seg[-1]['viewer']['sync_btn']!r}", flush=True)
        check("切回来后仍贴着真值（≤ 0.5 秒）", max_abs(drifts(seg[-2:], "viewer")) <= 0.5, fmt(drifts(seg, "viewer")))
        check("切回来后没有跳转、没有「同步」按钮", big_seeks(seg, "viewer") == 0 and not seg[-1]["viewer"]["sync_btn"])
        check("切回来后没有「画面前跳」", not jumps(seg, "viewer"))

        # ---- 2. 观众在后台时房主拖动 / 暂停 / 继续 ----
        await viewer.background(True, MINUTE)
        before = await lab.take()
        await host.seek_ui(90, duration)
        ok, seg = await lab.wait_until(lambda s: abs(s["viewer"]["ct"] - s["room"]) < 1 and s["room"] >= 89.5, 5, "后台观众跟拖动")
        check("后台的观众 5 秒内跟上房主的拖动（消息直达，不等定时器）", ok,
              f"观众 {seg[-1]['viewer']['ct']:.1f} 真值 {seg[-1]['room']:.1f}")
        check("跟拖动只跳一次", big_seeks([before] + seg, "viewer") == 1)
        await host.page.click("#play")
        ok, seg = await lab.wait_until(lambda s: s["viewer"]["paused"] and not s["playing"], 4, "后台观众跟暂停")
        check("后台的观众 4 秒内跟着暂停", ok)
        check("暂停时后台观众停在房间位置（≤ 0.5 秒）", abs(seg[-1]["viewer"]["ct"] - seg[-1]["room"]) <= 0.5)
        await lab.run(3, "后台暂停中")
        await host.page.click("#play")
        ok, seg = await lab.wait_until(lambda s: not s["viewer"]["paused"] and s["playing"], 4, "后台观众跟继续")
        check("后台的观众 4 秒内跟着继续", ok)
        await viewer.background(False, 0)
        seg = await lab.run(3, "切回来 2")
        check("切回来后贴着真值", max_abs(drifts(seg[-2:], "viewer")) <= 0.5, fmt(drifts(seg, "viewer")))

        # ---- 3. 观众在后台时卡了 4 秒（直接把位置拨回去模拟） ----
        await viewer.background(True, MINUTE)
        before = await lab.take()
        await viewer.nudge_video(-4)
        await lab.run(3, "后台落后 4 秒")
        await viewer.background(False, 0)
        seg = await lab.run(4, "切回来 3")
        rates = {round(s["viewer"]["rate"], 2) for s in seg[1:]}
        check("后台落后后切回来：不被自动跳（只有我们拨的那一次）", big_seeks([before] + seg, "viewer") == 1)
        check("后台落后后切回来：1.05x 追", 1.05 in rates, f"{sorted(rates)}")
        check("后台落后后切回来：显示「落后 N 秒 · 同步」", any("落后" in s["viewer"]["sync_btn"] for s in seg), repr(seg[-1]["viewer"]["sync_btn"]))
        check("房主全程不受影响", max_abs(drifts(seg, "host")) <= 0.5 and big_seeks([before] + seg, "host") == 0)
        await viewer.page.click("#sync-btn")
        ok, _ = await lab.wait_until(lambda s: abs(s["viewer"]["ct"] - s["room"]) < 0.6 and not s["viewer"]["sync_btn"], 6, "点同步")
        check("点「同步」后回到房间位置", ok)

        # ---- 4. 房主切到后台 20 秒 ----
        before = await lab.take()
        truth_before = before["room"]
        await host.background(True, MINUTE)
        seg = await lab.run(20, "房主后台 20 秒")
        drift_room = max(abs((s["room"] - truth_before) - (s["t"] - before["t"])) for s in seg)
        check("房主在后台时房间位置按时间平稳前进（没有被拉动）", drift_room <= 0.5, f"最多偏 {drift_room:.2f} 秒")
        check("房主在后台时观众贴着真值", max_abs(drifts(seg, "viewer")) <= 0.5, fmt(drifts(seg, "viewer")))
        check("房主在后台时没人跳转", big_seeks([before] + seg, "host") == 0 and big_seeks([before] + seg, "viewer") == 0)
        await host.background(False, 0)
        seg = await lab.run(4, "房主切回来")
        drift_room = max(abs((s["room"] - truth_before) - (s["t"] - before["t"])) for s in seg)
        check("房主切回来后房间位置仍平稳（没有多余的拉回）", drift_room <= 0.5, f"最多偏 {drift_room:.2f} 秒")
        check("房主切回来后两边贴着真值、相差 ≤ 0.5 秒",
              max_abs(drifts(seg[-2:], "host")) <= 0.5 and max_abs(drifts(seg[-2:], "viewer")) <= 0.5 and max_abs(gap(seg[-2:])) <= 0.5,
              f"房主 {fmt(drifts(seg, 'host'))} 观众 {fmt(drifts(seg, 'viewer'))}")

        # ---- 日志 ----
        warnings = b.capture.client_warnings()
        print(f"  服务器 WARNING（watch client …）{len(warnings)} 条", flush=True)
        for w in warnings:
            print("   " + w, flush=True)
        check("服务器没有收到「position jumped」回报", not any("position jumped" in w for w in warnings))
        for c in (host, viewer):
            errors = [m for m in c.console if m.startswith("pageerror")]
            check(f"{c.name}页面没有脚本错误", not errors, "\n".join(errors[:3]))

        if args.trace:
            with open(args.trace, "w", encoding="utf-8") as f:
                json.dump({"phases": lab.phases, "samples": lab.samples, "server_warnings": warnings,
                           "console": {c.name: c.console for c in (host, viewer)}}, f, ensure_ascii=False, indent=1)
            print(f"  采样已写到 {args.trace}", flush=True)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed:\n  " + "\n  ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认无头）")
    ap.add_argument("--trace", help="把全部采样写成 JSON 到这个文件")
    asyncio.run(main(ap.parse_args()))
