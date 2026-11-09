"""一起看电视直播实测：真实 WatchServer 的直播中继 + 两路 ffmpeg 假直播源 + 真实 Chrome 里的房主和观众。

运行：python tests/test_watch_lab_tv.py [--headed] [--trace out.json]
需要和 tests/test_watch_sync_browser.py 一样：playwright、Chrome / Edge、ffmpeg、联网；缺了就 SKIP。约 2.5 分钟。

场景（规则见 docs/ARCHITECTURE.md「直播房间」）：
- 两路实时产出的 HLS 直播源（2 秒一片、窗口 8 片）挂在本地假 CDN；prepare_tv 探测后建直播房间；
- 房主播放：两边都从直播边缘开始、以 1x 前进、离边缘不超过几片、互相差不超过 4 秒；进度条禁用、有「直播」标记；
- 暂停 / 继续：两边一起停；继续时都跳回直播边缘；
- 房主换源（tv_next）：版本 +1、两边重建播放器继续播、都看到「已切换片源」；
- 上游断流（杀掉当前源的 ffmpeg）：服务器判死或网页报 tv_stall → 自动换回另一路，两边恢复播放，且只换一次。
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from watch_lab import (FPS, Bench, big_seeks, fmt, fps_of, gap, max_abs, requirements_or_skip, skip, Lab)  # noqa: E402

results = []


def check(name, cond, note=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{note}]" if note else ""), flush=True)


def edge_distance(sample):
    """离缓冲末尾（≈ 直播边缘）多远。"""
    if sample["buffered"] is None or sample["ct"] is None:
        return None
    return sample["buffered"][1] - sample["ct"]


def advance_rate(samples, who):
    """这段里位置前进了多少 / 过了多少秒。"""
    a, b = samples[0][who], samples[-1][who]
    dt = b["t"] - a["t"]
    return (b["ct"] - a["ct"]) / dt if dt > 0 else 0


async def main(args):
    requirements_or_skip()
    async with Bench({}, headed=args.headed, tv=True) as b:
        src_a = b.live_source("A", "/liveA")
        src_b = b.live_source("B", "/liveB")
        await b.start_cdn()
        ok = await src_a.ready() and await src_b.ready()
        if not ok:
            skip("ffmpeg 没能产出直播列表")
        channel = b.tv_channel("实验台", [src_a.url_tail, src_b.url_tail])
        room = await b.make_tv_room(channel)
        check("prepare_tv 探通两路本地直播源，选第一路", room.video.source == "tv" and room.video.tv.index == 0
              and len(room.video.tv.order) == 2 and not room.video.tv.bad)

        host = await b.open_client("房主", b.host_url(room))
        viewer = await b.open_client("观众", b.viewer_url(room))
        lab = Lab(room)
        lab.add("host", host)
        lab.add("viewer", viewer)
        try:
            await asyncio.gather(host.click_start(), viewer.click_start())
        except Exception as e:
            s = await lab.take()
            skip(f"播放器没就绪（可能没联网取不到 hls.js）：{e}；stage={s['host']['stage']!r}")
        s = await lab.take()
        check("两边都是直播模式（有「直播」标记、没有进度条）",
              s["host"]["live_badge"] and s["viewer"]["live_badge"] and s["host"]["seek_hidden"] and s["viewer"]["seek_hidden"])

        # ---- 1. 房主播放：两边从直播边缘开始 ----
        await host.page.click("#play")
        ok, seg = await lab.wait_until(lambda s: (s["host"]["frames"] or 0) > 10 and (s["viewer"]["frames"] or 0) > 10, 20, "直播起播")
        check("房主播放后 20 秒内两边都出画面", ok)
        seg = await lab.run(8, "直播稳定")
        rh, rv = advance_rate(seg, "host"), advance_rate(seg, "viewer")
        eh = [edge_distance(s["host"]) for s in seg if edge_distance(s["host"]) is not None]
        ev = [edge_distance(s["viewer"]) for s in seg if edge_distance(s["viewer"]) is not None]
        g = gap(seg)
        print(f"  直播：前进速度 房主 {rh:.2f}x 观众 {rv:.2f}x；离边缘 房主 {fmt(eh)} 观众 {fmt(ev)}；"
              f"房主−观众 {fmt(g)}；帧率 {fps_of(seg, 'host'):.1f} / {fps_of(seg, 'viewer'):.1f}", flush=True)
        check("直播两边都以约 1x 前进", 0.9 <= rh <= 1.1 and 0.9 <= rv <= 1.1, f"{rh:.2f} / {rv:.2f}")
        check("直播两边离边缘不超过 10 秒", eh and ev and max(eh) <= 10 and max(ev) <= 10, f"{fmt(eh)} | {fmt(ev)}")
        check("直播房主和观众相差不超过 4 秒", max_abs(g) <= 4, fmt(g))
        check("直播稳定时没有自发跳转", big_seeks(seg, "host") == 0 and big_seeks(seg, "viewer") == 0)
        check("直播帧率接近片源", abs(fps_of(seg, "host") - FPS) <= 4 and abs(fps_of(seg, "viewer") - FPS) <= 4)
        check("服务器发给网页的直播房间位置恒为 0", b.server._state_msg(room)["position"] == 0)
        check("起播和稳定阶段没有发生换源", room.version == 0, f"版本 {room.version}；日志：{b.capture.find('tv stream')[-1:]}")

        # ---- 2. 暂停 / 继续 ----
        await host.page.click("#play")
        ok, seg = await lab.wait_until(lambda s: s["host"]["paused"] and s["viewer"]["paused"] and not s["playing"], 4, "直播暂停")
        check("房主暂停后两边都停", ok)
        await lab.run(5, "直播暂停中")
        before = await lab.take()
        await host.page.click("#play")
        ok, seg = await lab.wait_until(lambda s: s["playing"] and not s["host"]["paused"] and not s["viewer"]["paused"]
                                       and (edge_distance(s["host"]) or 99) <= 10 and (edge_distance(s["viewer"]) or 99) <= 10, 10, "直播继续")
        check("继续后两边都回到直播边缘附近（停了 5 秒不补播）", ok,
              f"离边缘 房主 {edge_distance(seg[-1]['host'])} 观众 {edge_distance(seg[-1]['viewer'])}")
        check("继续时两边各跳了一次（跳到边缘）", big_seeks([before] + seg, "host") >= 1 and big_seeks([before] + seg, "viewer") >= 1)
        await lab.run(3, "继续后稳定")

        # ---- 3. 房主手动换源 ----
        await host.open_panel()
        v0, i0 = room.version, room.video.tv.index
        before = await lab.take()
        await host.page.click("#tv-next")
        ok, seg = await lab.wait_until(lambda s: s["version"] == v0 + 1 and s["host"]["videos"] > before["host"]["videos"]
                                       and s["viewer"]["videos"] > before["viewer"]["videos"]
                                       and (s["host"]["frames"] or 0) > 10 and (s["viewer"]["frames"] or 0) > 10
                                       and not s["host"]["paused"] and not s["viewer"]["paused"], 20, "手动换源")
        check("房主换源：20 秒内两边都在新源上出画面", ok)
        check("房主换源：换到另一路", room.video.tv.index == 1 - i0 and room.version == v0 + 1,
              f"{i0} → {room.video.tv.index}，版本 {room.version}")
        toasts = await viewer.page.evaluate("() => window.__probe.toasts")
        check("观众看到「已切换片源」", any("已切换片源" in t for t in toasts), repr(toasts[-2:]))
        seg = await lab.run(5, "换源后稳定")
        check("换源后两边都以约 1x 前进、相差不超过 4 秒",
              0.9 <= advance_rate(seg, "host") <= 1.1 and 0.9 <= advance_rate(seg, "viewer") <= 1.1 and max_abs(gap(seg)) <= 4,
              f"{advance_rate(seg, 'host'):.2f} / {advance_rate(seg, 'viewer'):.2f}，差 {fmt(gap(seg))}")

        # ---- 4. 上游断流：杀掉正在播的那一路的 ffmpeg ----
        v1, i1 = room.version, room.video.tv.index
        [src_a, src_b][i1].stop()
        t_dead = seg[-1]["t"]
        ok, seg = await lab.wait_until(lambda s: s["version"] == v1 + 1, 45, "断流等换源")
        took = seg[-1]["t"] - t_dead
        check("断流后 45 秒内自动换源", ok, f"{took:.0f} 秒；日志：{[l[-120:] for l in b.capture.find('tv stream')[-2:]]}")
        check("自动换到另一路、断掉的那路记为坏源", room.video.tv.index == 1 - i1 and i1 in room.video.tv.bad)
        ok, seg = await lab.wait_until(lambda s: (s["host"]["frames"] or 0) > 10 and (s["viewer"]["frames"] or 0) > 10
                                       and s["host"]["videos"] > before["host"]["videos"] + 1
                                       and s["viewer"]["videos"] > before["viewer"]["videos"] + 1
                                       and not s["host"]["paused"] and not s["viewer"]["paused"], 20, "换源后恢复")
        check("自动换源后 20 秒内两边都恢复画面", ok)
        seg = await lab.run(10, "恢复后稳定")
        check("恢复后两边以约 1x 前进、相差不超过 4 秒",
              0.9 <= advance_rate(seg, "host") <= 1.1 and 0.9 <= advance_rate(seg, "viewer") <= 1.1 and max_abs(gap(seg)) <= 4,
              f"{advance_rate(seg, 'host'):.2f} / {advance_rate(seg, 'viewer'):.2f}，差 {fmt(gap(seg))}")
        check("断流只触发了一次换源", room.version == v1 + 1, f"版本 {room.version}（断流前 {v1}）")

        # ---- 日志 ----
        for line in b.capture.find("tv stream") + b.capture.find("tv switch"):
            print("  服务器：" + line[-160:], flush=True)
        warnings = b.capture.client_warnings()
        print(f"  服务器 WARNING（watch client …）{len(warnings)} 条", flush=True)
        for w in warnings:
            print("   " + w, flush=True)
        check("服务器没有收到「position jumped」回报", not any("position jumped" in w for w in warnings))
        for c in (host, viewer):
            errors = [m for m in c.console if m.startswith("pageerror")]
            hls_errors = [m for m in c.console if "hls.js error" in m]
            print(f"  {c.name}：hls.js 报错 {len(hls_errors)} 条（断流阶段的是预期的）", flush=True)
            for m in hls_errors[-4:]:
                print("   " + m[:160], flush=True)
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
