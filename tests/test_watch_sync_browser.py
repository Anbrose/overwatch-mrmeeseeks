"""一起看多客户端实测：真实 WatchServer + 真实 Chrome 里的房主、观众、慢网观众三个页面，对比进度和帧率。

运行：python tests/test_watch_sync_browser.py [--headed] [--seconds N] [--trace out.json]
需要：pip install playwright；本机装有 Google Chrome 或 Edge；ffmpeg 在 PATH（生成测试片）；
联网（hls.js 从 cdnjs 加载）。缺任何一样就打印 SKIP 并以 0 退出，不算失败。整个流程约 2.5 分钟。

做法（公共部分在 tests/watch_lab.py）：
- ffmpeg 生成一段 3 分钟、25 fps 的 HLS（2 秒一片），本地 aiohttp 假 CDN 提供（带 CORS，和资源站一样）；
- 起真实的 WatchServer（allow_http），用 prepare_vod 建一个 vod 房间；
- 无头 Chrome 开独立上下文：房主链接（?k=）和观众链接；两边点「点击开始观看」；
- 每 0.5 秒同时采每个页面 <video> 的 currentTime、readyState、playbackRate、总帧数 / 丢帧数
  （getVideoPlaybackQuality）、seek 事件（页面脚本在 <video> 上记每次 seeking 的起止位置；0.5 秒以内的算
  hls.js 自己的 nudge，不算跳转），以及服务器上房间的真值位置；
- 分阶段比较：正常播放、房主拖动、观众落后 3 秒、房主落后 3 秒、观众超前到片尾、慢网观众加入、暂停 / 继续。
  规则见 docs/ARCHITECTURE.md「同步」：观众绝不被自动跳转，房主是基准线。
- 服务器 WARNING 日志（playback failed / position jumped / stall）和页面 console 全部收下来打印。
操作类场景（重连、遥控器、中途加入、刷新、选集 / 换片源、房主交接）在 tests/test_watch_lab_ops.py。
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from watch_lab import (FPS, Bench, big_seeks, drifts, dropped_ratio, fmt, fps_of, gap, jumps,  # noqa: E402
                       max_abs, requirements_or_skip, segment_delayer, skip, Lab)

CLIP_SECONDS = 180
SLOW_DELAY_S = 2.6          # 慢网观众：每个分片都晚这么久才到（比分片本身还长，必然卡）
results = []


def check(name, cond, note=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{note}]" if note else ""), flush=True)


async def main(args):
    requirements_or_skip()
    async with Bench({"main": CLIP_SECONDS}, headed=args.headed) as b:
        tail = b.mount("main", "/main")
        await b.start_cdn()
        show = b.show("测试片", {"本地": [tail]})
        room = await b.make_room(show)
        duration = room.video.duration
        check("prepare_vod 认出本地测试片", abs(duration - CLIP_SECONDS) < 1, f"时长 {duration:.1f}")

        host = await b.open_client("房主", b.host_url(room))
        viewer = await b.open_client("观众", b.viewer_url(room))
        lab = Lab(room)
        lab.add("host", host)
        lab.add("viewer", viewer)

        # ---- 0. 两边都加载好播放器（hls.js 来自 cdnjs，要联网） ----
        try:
            await asyncio.gather(host.click_start(), viewer.click_start())
        except Exception as e:
            for c in (host, viewer):
                print(f"--- {c.name} console ---\n" + "\n".join(c.console[-15:]), flush=True)
            s = await lab.take()
            skip(f"播放器没就绪（可能没联网取不到 hls.js）：{e}；stage={s['host']['stage']!r}")
        s = await lab.take()
        check("两边都建出 <video>（hls.js 模式）", s["host"]["has_video"] and s["viewer"]["has_video"])
        check("房主页面拿到了房主身份（有 ▶ 控制）", s["host"]["can_control"])
        check("观众页面没有控制权（▶ 禁用）", not s["viewer"]["can_control"])

        # ---- 1. 房主按播放：两边都动起来 ----
        await host.page.click("#play")
        ok, seg = await lab.wait_until(lambda s: s["host"]["ct"] > 1 and s["viewer"]["ct"] > 1, 15, "起播")
        check("房主点播放后 15 秒内两边都开始走", ok,
              f"房主 {seg[-1]['host']['ct']:.1f}s 观众 {seg[-1]['viewer']['ct']:.1f}s 真值 {seg[-1]['room']:.1f}s")
        check("服务器房间进入播放状态", room.playback.playing)

        # ---- 2. 稳定播放 N 秒：进度、帧率、丢帧、无自发跳转 ----
        await lab.run(3, "起播后稳定")     # 给倍速微调一点时间
        seg = await lab.run(args.seconds, "正常播放")
        g = gap(seg)
        dh, dv = drifts(seg, "host"), drifts(seg, "viewer")
        print(f"  正常播放：房主−观众 {fmt(g)}；房主 vs 真值 {fmt(dh)}；观众 vs 真值 {fmt(dv)}", flush=True)
        check("正常播放时房主和观众相差不超过 0.5 秒", max_abs(g) <= 0.5, fmt(g))
        check("正常播放时房主贴着房间真值（≤ 0.5 秒）", max_abs(dh) <= 0.5, fmt(dh))
        check("正常播放时观众贴着房间真值（≤ 0.5 秒）", max_abs(dv) <= 0.5, fmt(dv))
        fh, fv = fps_of(seg, "host"), fps_of(seg, "viewer")
        print(f"  帧率：房主 {fh:.1f} fps 观众 {fv:.1f} fps（片源 {FPS}）；"
              f"丢帧 房主 {dropped_ratio(seg, 'host'):.1%} 观众 {dropped_ratio(seg, 'viewer'):.1%}", flush=True)
        check("两边帧率都接近片源帧率（±4）", abs(fh - FPS) <= 4 and abs(fv - FPS) <= 4, f"{fh:.1f} / {fv:.1f}")
        check("两边丢帧都低于 5%", dropped_ratio(seg, "host") < 0.05 and dropped_ratio(seg, "viewer") < 0.05)
        check("正常播放时观众没有自发跳转", big_seeks(seg, "viewer") == 0, f"{big_seeks(seg, 'viewer')} 次")
        check("正常播放时房主没有自发跳转", big_seeks(seg, "host") == 0, f"{big_seeks(seg, 'host')} 次")
        check("正常播放时没有「画面前跳」", not jumps(seg, "host") and not jumps(seg, "viewer"),
              f"{jumps(seg, 'host') + jumps(seg, 'viewer')}")
        check("正常播放时两边倍速都回到 1x",
              seg[-1]["host"]["rate"] == 1 and seg[-1]["viewer"]["rate"] == 1,
              f"{seg[-1]['host']['rate']} / {seg[-1]['viewer']['rate']}")
        check("正常播放时观众不显示「同步」按钮", not seg[-1]["viewer"]["sync_btn"], seg[-1]["viewer"]["sync_btn"])

        # ---- 3. 房主拖动到 30 秒：人为操作，两边都要跳过去 ----
        before = await lab.take()
        await host.seek_ui(30, duration)
        ok, seg = await lab.wait_until(lambda s: s["room"] >= 29.5 and abs(s["host"]["ct"] - s["room"]) < 1
                                       and abs(s["viewer"]["ct"] - s["room"]) < 1, 6, "房主拖动")
        check("房主拖到 30 秒后 6 秒内两边都到了房间位置", ok,
              f"真值 {seg[-1]['room']:.1f} 房主 {seg[-1]['host']['ct']:.1f} 观众 {seg[-1]['viewer']['ct']:.1f}")
        check("服务器记下了拖动（约 30 秒）", 29 <= seg[-1]["room"] <= 29 + 8, f"{seg[-1]['room']:.1f}")
        check("观众因房主拖动只跳了一次", seg[-1]["viewer"]["big_seeks"] - before["viewer"]["big_seeks"] == 1,
              f"{seg[-1]['viewer']['big_seeks'] - before['viewer']['big_seeks']} 次")
        await lab.run(3, "拖动后稳定")

        # ---- 4. 观众落后 3 秒（像卡顿恢复后）：不能被自动跳，只能微调倍速 + 显示「同步」按钮 ----
        before = await lab.take()
        await viewer.nudge_video(-3)
        seg = await lab.run(4, "观众落后 3 秒")
        rates = {round(s["viewer"]["rate"], 2) for s in seg[1:]}
        check("观众落后时没有被自动跳回（只有我们自己拨的那一次）",
              seg[-1]["viewer"]["big_seeks"] - before["viewer"]["big_seeks"] == 1,
              f"{seg[-1]['viewer']['big_seeks'] - before['viewer']['big_seeks']} 次跳转")
        check("观众落后时倍速提到 1.05x", 1.05 in rates, f"倍速出现过 {sorted(rates)}")
        check("观众落后时显示「落后 N 秒 · 同步」", any("落后" in s["viewer"]["sync_btn"] for s in seg),
              repr(seg[-1]["viewer"]["sync_btn"]))
        check("观众落后时房主不受影响（贴着真值）", max_abs(drifts(seg, "host")) <= 0.5, fmt(drifts(seg, "host")))
        check("观众落后时服务器位置没被拉动",
              not any(abs((b2["room"] - a["room"]) - (b2["t"] - a["t"])) > 1 for a, b2 in zip(seg, seg[1:])))
        await viewer.page.click("#sync-btn")
        ok, seg2 = await lab.wait_until(lambda s: abs(s["viewer"]["ct"] - s["room"]) < 0.6, 4, "观众点同步")
        check("观众点「同步」后 4 秒内回到房间位置", ok, fmt(drifts(seg2, "viewer")))
        ok, _ = await lab.wait_until(lambda s: not s["viewer"]["sync_btn"] and s["viewer"]["rate"] == 1, 6, "同步后收尾")
        check("同步后「同步」按钮消失、倍速回 1x", ok)

        # ---- 5. 房主落后 3 秒：房主是基准线 → 房间被拉回来（auto），观众不跳、显示「超前」----
        before = await lab.take()
        truth_before = before["room"]
        await host.nudge_video(-3)
        seg = await lab.run(5, "房主落后 3 秒")
        pulled_by = min((s["room"] - truth_before) - (s["t"] - before["t"]) for s in seg)
        check("房主落后时房间位置被拉回到房主那里（anchorRoom）", pulled_by < -2, f"真值相对时间多退了 {pulled_by:+.1f} 秒")
        check("房主落后时房主自己没有再被跳转", seg[-1]["host"]["big_seeks"] - before["host"]["big_seeks"] == 1,
              f"{seg[-1]['host']['big_seeks'] - before['host']['big_seeks']} 次")
        check("房主拉回房间时观众没有被自动跳", seg[-1]["viewer"]["big_seeks"] == before["viewer"]["big_seeks"],
              f"{seg[-1]['viewer']['big_seeks'] - before['viewer']['big_seeks']} 次")
        rates = {round(s["viewer"]["rate"], 2) for s in seg[1:]}
        check("观众超前时倍速降到 0.95x", 0.95 in rates, f"倍速出现过 {sorted(rates)}")
        check("观众超前时显示「超前 N 秒 · 同步」", any("超前" in s["viewer"]["sync_btn"] for s in seg),
              repr(seg[-1]["viewer"]["sync_btn"]))
        await viewer.page.click("#sync-btn")
        ok, _ = await lab.wait_until(lambda s: abs(s["viewer"]["ct"] - s["room"]) < 0.6
                                     and abs(s["host"]["ct"] - s["room"]) < 0.6, 6, "重新对齐")
        check("观众点「同步」后两边都重新贴着真值", ok)

        # ---- 6. 观众超前到片尾：房间还在播，观众的视频先放完了。不能从头重播（play() 会让放完的视频从 0 开始），
        #         应该停在片尾、显示「超前」，点「同步」回到房间位置 ----
        await host.seek_ui(duration - 12, duration)
        ok, _ = await lab.wait_until(lambda s: abs(s["viewer"]["ct"] - s["room"]) < 1 and s["room"] > duration - 13, 6, "拖到片尾前")
        before = await lab.take()
        await viewer.nudge_video(to=duration - 1)
        seg = await lab.run(5, "观众超前到片尾")
        restarted = any(s["viewer"]["ct"] is not None and s["viewer"]["ct"] < 5 for s in seg)
        check("观众先放完时没有从头重播", not restarted,
              f"观众位置 {min(s['viewer']['ct'] for s in seg):.1f}–{max(s['viewer']['ct'] for s in seg):.1f}")
        check("观众先放完时停在片尾", seg[-1]["viewer"]["ct"] >= duration - 0.6 and seg[-1]["viewer"]["paused"],
              f"位置 {seg[-1]['viewer']['ct']:.1f} paused {seg[-1]['viewer']['paused']}")
        check("观众先放完时显示「超前 N 秒 · 同步」", "超前" in seg[-1]["viewer"]["sync_btn"], repr(seg[-1]["viewer"]["sync_btn"]))
        check("观众先放完时没有被自动跳（只有我们拨的那一次）",
              seg[-1]["viewer"]["big_seeks"] - before["viewer"]["big_seeks"] == 1,
              f"{seg[-1]['viewer']['big_seeks'] - before['viewer']['big_seeks']} 次")
        check("观众先放完时房主不受影响", max_abs(drifts(seg, "host")) <= 0.5, fmt(drifts(seg, "host")))
        await viewer.page.click("#sync-btn")
        ok, _ = await lab.wait_until(lambda s: abs(s["viewer"]["ct"] - s["room"]) < 0.6 and not s["viewer"]["paused"], 5, "片尾同步")
        check("观众点「同步」后回到房间位置并继续播", ok)
        await host.seek_ui(60, duration)
        ok, _ = await lab.wait_until(lambda s: abs(s["viewer"]["ct"] - s["room"]) < 1 and abs(s["host"]["ct"] - s["room"]) < 1
                                     and 59 < s["room"] < 70, 6, "拖回 60 秒")
        check("房主拖回 60 秒后两边都跟上", ok)

        # ---- 7. 慢网观众加入：每个分片都晚 SLOW_DELAY_S 秒才到（比分片本身还长），必然反复卡；
        #         卡了也不能被自动跳；房主不受影响；健康点变黄 / 红；网络恢复后能追上 ----
        delay, slow_on = segment_delayer(SLOW_DELAY_S)

        async def install_delay(page):
            await page.route("**/seg*.ts", delay)

        slow = await b.open_client("慢网观众", b.viewer_url(room), install_delay)
        lab.add("slow", slow)
        await slow.click_start()
        # 起播那一下 seek 到房间位置是页面自己的；分片慢，浏览器要等到有数据才真的 seek，等它画出第一帧再开始算
        ok, _ = await lab.wait_until(lambda s: (s["slow"]["frames"] or 0) > 0, 20, "慢网观众起播")
        check("慢网观众 20 秒内画出第一帧", ok)
        before = await lab.take()
        seg = await lab.run(14, "慢网观众")
        stalled = any(s["slow"]["rs"] is not None and s["slow"]["rs"] < 3 and s["playing"] for s in seg) \
            or seg[-1]["slow"]["waits"] > before["slow"]["waits"]
        ds = drifts(seg, "slow")
        print(f"  慢网观众：vs 真值 {fmt(ds)}；waiting {seg[-1]['slow']['waits'] - before['slow']['waits']} 次；"
              f"同步按钮 {seg[-1]['slow']['sync_btn']!r}", flush=True)
        check("慢网观众确实卡住过（readyState < 3 或 waiting）", stalled)
        check("慢网观众卡顿期间没有被自动跳", seg[-1]["slow"]["big_seeks"] == before["slow"]["big_seeks"],
              f"{seg[-1]['slow']['big_seeks'] - before['slow']['big_seeks']} 次")
        check("慢网观众卡顿期间没有「画面前跳」", not jumps(seg, "slow"), f"{jumps(seg, 'slow')}")
        check("慢网观众落在房间后面（真值 − 位置 > 0）", ds and ds[-1] > 0.3, fmt(ds))
        check("慢网观众落后 2 秒以上时显示「落后 N 秒 · 同步」",
              max(ds) <= 2.2 or any("落后" in s["slow"]["sync_btn"] for s in seg), repr(seg[-1]["slow"]["sync_btn"]))
        check("慢网观众卡顿期间房主不受影响（贴着真值）", max_abs(drifts(seg, "host")) <= 0.5, fmt(drifts(seg, "host")))
        check("慢网观众卡顿期间另一位观众也不受影响", max_abs(drifts(seg, "viewer")) <= 0.5, fmt(drifts(seg, "viewer")))
        healths = {c.health for c in b.conns(room)}
        check("有人向服务器报了黄 / 红的网络状况", healths & {"yellow", "red"}, f"收到 {sorted(healths)}")
        slow_on["on"] = False
        await slow.page.unroute("**/seg*.ts")
        s = await lab.take()
        if s["slow"]["sync_btn"]:
            await slow.page.click("#sync-btn")
        ok, _ = await lab.wait_until(lambda s: abs(s["slow"]["ct"] - s["room"]) < 0.6, 15, "慢网恢复")
        check("网络恢复后慢网观众 15 秒内贴着真值", ok)
        lab.remove("slow")
        await b.close_client(slow)

        # ---- 8. 房主暂停 / 继续 ----
        await host.page.click("#play")
        ok, seg = await lab.wait_until(lambda s: s["host"]["paused"] and s["viewer"]["paused"], 3, "暂停")
        check("房主暂停后 3 秒内两边都停了", ok)
        g = gap(seg[-1:])
        check("暂停时两边位置相差不超过 0.5 秒", max_abs(g) <= 0.5, fmt(g))
        check("暂停时两边都在房间位置（≤ 0.5 秒）",
              abs(seg[-1]["host"]["ct"] - seg[-1]["room"]) <= 0.5 and abs(seg[-1]["viewer"]["ct"] - seg[-1]["room"]) <= 0.5)
        await lab.run(2, "暂停中")
        await host.page.click("#play")
        ok, seg = await lab.wait_until(lambda s: not s["host"]["paused"] and not s["viewer"]["paused"]
                                       and abs(s["host"]["ct"] - s["viewer"]["ct"]) <= 0.5, 5, "继续")
        check("房主继续后 5 秒内两边都在播且相差不超过 0.5 秒", ok, fmt(gap(seg[-1:])))

        # ---- 服务器日志、浏览器 console ----
        warnings = b.capture.client_warnings()
        print(f"  服务器 WARNING（watch client …）{len(warnings)} 条", flush=True)
        for w in warnings:
            print("   " + w, flush=True)
        check("服务器没有收到「position jumped」回报", not any("position jumped" in w for w in warnings))
        check("服务器没有收到「playback failed」回报", not any("playback failed" in w for w in warnings))
        for c in (host, viewer, slow):
            errors = [m for m in c.console if m.startswith("pageerror")]
            hls_errors = [m for m in c.console if "hls.js error" in m]
            if errors or hls_errors:
                print(f"--- {c.name} console ---\n" + "\n".join((errors + hls_errors)[-10:]), flush=True)
            check(f"{c.name}页面没有脚本错误", not errors)
        check("房主和观众的 hls.js 没有报错（慢网观众的 bufferStalledError 是预期的）",
              not any("hls.js error" in m for c in (host, viewer) for m in c.console))

        if args.trace:
            with open(args.trace, "w", encoding="utf-8") as f:
                json.dump({"phases": lab.phases, "samples": lab.samples, "server_warnings": warnings,
                           "console": {c.name: c.console for c in (host, viewer, slow)}},
                          f, ensure_ascii=False, indent=1)
            print(f"  采样已写到 {args.trace}", flush=True)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed:\n  " + "\n  ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认无头）")
    ap.add_argument("--seconds", type=float, default=10, help="正常播放阶段采样多少秒（默认 10）")
    ap.add_argument("--trace", help="把全部采样写成 JSON 到这个文件")
    asyncio.run(main(ap.parse_args()))
