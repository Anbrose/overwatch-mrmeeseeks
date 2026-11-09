"""一起看多客户端实测（操作类）：真实 WatchServer + 真实 Chrome，看各种「事件」发生时两边的进度有没有乱。

运行：python tests/test_watch_lab_ops.py [--headed] [--trace out.json]
需要和 tests/test_watch_sync_browser.py 一样：playwright、Chrome / Edge、ffmpeg、联网；缺了就 SKIP。约 3 分钟。

场景（规则见 docs/ARCHITECTURE.md「一起看 WebSocket 协议」和「同步」）：
- 中途加入：新观众在房间播到一半时进来，第一帧就在房间位置附近，只有起播那一次跳转；
- 慢网中途加入：同样的事发生在网络慢的观众身上，记下第一帧要多久；
- 观众断线重连：服务器掐掉观众的 WebSocket，页面 2 秒后带同一个 viewer_id 重连，没有跳转、身份不变；
- 房主断线重连：同上，房主身份不丢，观众不受影响；
- 观众按遥控器暂停：没有控制权 → 提示一句，同步把它拉回播放，房间不变；
- 房主按遥控器暂停 / 播放：当成 control 发出去，房间和观众都跟着停 / 走；
- 房主刷新页面：回来还是房主，站回房间位置，观众全程不跳；
- 选集：两边换到第 2 集、停在开头、暂停；房主播放后一起走；
- 换片源：同一集换线路，位置和播放状态都保持，两边都在 1 秒内接上；
- 房主交接：观众成为房主后是新的基准线（它落后时房间被拉回），原房主变成观众（不跳、有「超前」按钮）但保留控制权。
"""
import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from watch_lab import (Bench, big_seeks, drifts, fmt, gap, jumps, max_abs,  # noqa: E402
                       requirements_or_skip, segment_delayer, skip, Lab)

EP1_SECONDS = 240
EP2_SECONDS = 60
SLOW_DELAY_S = 2.6
results = []


def check(name, cond, note=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{note}]" if note else ""), flush=True)


async def steady(lab, host, viewer, seconds, name):
    """播放 seconds 秒，返回这段里两边贴不贴真值、有没有自发跳转。"""
    seg = await lab.run(seconds, name)
    return seg, max_abs(drifts(seg, host)), max_abs(drifts(seg, viewer)), big_seeks(seg, host), big_seeks(seg, viewer)


async def main(args):
    requirements_or_skip()
    async with Bench({"ep1": EP1_SECONDS, "ep2": EP2_SECONDS}, headed=args.headed) as b:
        # 两条线路、两集：线路 2 和线路 1 是同一份文件挂在不同前缀下
        l1 = [b.mount("ep1", "/l1/e1"), b.mount("ep2", "/l1/e2")]
        l2 = [b.mount("ep1", "/l2/e1"), b.mount("ep2", "/l2/e2")]
        await b.start_cdn()
        show = b.show("实验剧", {"线路一": l1, "线路二": l2})
        room = await b.make_room(show)
        rid = room.room_id
        check("建出两集两线路的 vod 房间", room.video.vod.episode == 0 and room.video.vod.line == 0
              and show.episode_count == 2 and len(show.lines) == 2)

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
        await host.page.click("#play")
        ok, _ = await lab.wait_until(lambda s: s["host"]["ct"] > 1 and s["viewer"]["ct"] > 1, 15, "起播")
        check("起播：两边都在走", ok)
        host_vid, viewer_vid = await host.viewer_id(rid), await viewer.viewer_id(rid)
        check("页面各自存了 viewer_id，房主的就是服务器记的房主", host_vid and viewer_vid and room.host_id == host_vid)
        await lab.run(3, "起播后稳定")

        # ---- 1. 中途加入 ----
        requested = []

        async def record(route):
            requested.append(route.request.url.rsplit("/", 1)[-1])
            await route.continue_()

        async def install_record(page):
            await page.route("**/seg*.ts", record)
        t0 = time.time()
        late = await b.open_client("中途加入", b.viewer_url(room), install_record)
        lab.add("late", late)
        await late.click_start()
        ok, seg = await lab.wait_until(lambda s: (s["late"]["frames"] or 0) > 0, 15, "中途加入起播")
        ttff = time.time() - t0
        first = seg[-1]
        check("中途加入 15 秒内画出第一帧", ok, f"{ttff:.1f} 秒")
        check("中途加入的第一帧就在房间位置附近（≤ 1.5 秒）", abs(first["late"]["ct"] - first["room"]) <= 1.5,
              f"位置 {first['late']['ct']:.1f} 真值 {first['room']:.1f}")
        check("中途加入只有起播那一次跳转", first["late"]["big_seeks"] <= 1, f"{first['late']['big_seeks']} 次")
        expected_seg = int(first["room"] // 2)
        check("中途加入时 hls.js 第一个分片就取在房间位置附近（不是 seg000）",
              requested and abs(int(requested[0][3:6]) - expected_seg) <= 2, f"先取了 {requested[:3]}，房间在第 {expected_seg} 片")
        seg = await lab.run(4, "中途加入稳定")
        check("中途加入 4 秒后贴着真值（≤ 0.5 秒）", max_abs(drifts(seg[-2:], "late")) <= 0.5, fmt(drifts(seg, "late")))
        check("中途加入时原来两位不受影响", max_abs(drifts(seg, "host")) <= 0.5 and max_abs(drifts(seg, "viewer")) <= 0.5
              and big_seeks(seg, "host") == 0 and big_seeks(seg, "viewer") == 0)
        lab.remove("late")
        await b.close_client(late)

        # ---- 2. 慢网中途加入（只记录，不设硬指标：每个分片晚 2.6 秒才到） ----
        delay, slow_on = segment_delayer(SLOW_DELAY_S)
        requested.clear()

        async def install_delay(page):
            await page.route("**/seg*.ts", record)
            await page.route("**/seg*.ts", delay)
        t0 = time.time()
        slow = await b.open_client("慢网中途加入", b.viewer_url(room), install_delay)
        lab.add("slow", slow)
        await slow.click_start()
        ok, seg = await lab.wait_until(lambda s: (s["slow"]["frames"] or 0) > 0, 25, "慢网中途加入起播")
        ttff_slow = time.time() - t0
        print(f"  慢网中途加入：第一帧 {ttff_slow:.1f} 秒（分片各晚 {SLOW_DELAY_S} 秒）；"
              f"位置 {seg[-1]['slow']['ct']:.1f} 真值 {seg[-1]['room']:.1f}", flush=True)
        check("慢网中途加入 25 秒内画出第一帧", ok, f"{ttff_slow:.1f} 秒")
        check("慢网中途加入起播时没有多余的跳转", seg[-1]["slow"]["big_seeks"] <= 1, f"{seg[-1]['slow']['big_seeks']} 次")
        check("慢网中途加入只等一个分片就出画面（< 2 个分片延迟）", ttff_slow < SLOW_DELAY_S * 2, f"{ttff_slow:.1f} 秒")
        check("慢网中途加入的第一帧离房间位置不超过一个分片延迟 + 1 秒",
              abs(seg[-1]["slow"]["ct"] - seg[-1]["room"]) <= SLOW_DELAY_S + 1, f"差 {seg[-1]['room'] - seg[-1]['slow']['ct']:.1f} 秒")
        slow_on["on"] = False
        lab.remove("slow")
        await b.close_client(slow)

        # ---- 3. 观众断线重连 ----
        before = await lab.take()
        await b.drop_connection(room, viewer_vid)
        ok, seg = await lab.wait_until(lambda s: any(c.viewer_id == viewer_vid for c in b.conns(room)) and s["t"] - before["t"] > 2.5, 10, "观众重连")
        check("观众被掐线后 10 秒内重连上", ok)
        seg = await lab.run(3, "观众重连后")
        check("观众重连没有跳转", seg[-1]["viewer"]["big_seeks"] == before["viewer"]["big_seeks"],
              f"{seg[-1]['viewer']['big_seeks'] - before['viewer']['big_seeks']} 次")
        check("观众重连后仍贴着真值", max_abs(drifts(seg, "viewer")) <= 0.5, fmt(drifts(seg, "viewer")))
        online = sum(1 for m in room.members.values() if m.connections > 0)
        check("观众重连后还是同一个成员（没有多出来一个人）", online == 2
              and await viewer.viewer_id(rid) == viewer_vid, f"在线 {online} 人")
        check("观众重连期间房主不受影响", max_abs(drifts(seg, "host")) <= 0.5 and big_seeks(seg, "host") == 0)

        # ---- 4. 房主断线重连 ----
        before = await lab.take()
        await b.drop_connection(room, host_vid)
        ok, seg = await lab.wait_until(lambda s: any(c.viewer_id == host_vid for c in b.conns(room)) and s["t"] - before["t"] > 2.5, 10, "房主重连")
        check("房主被掐线后 10 秒内重连上", ok)
        seg = await lab.run(3, "房主重连后")
        check("房主重连后还是房主", room.host_id == host_vid and seg[-1]["host"]["can_control"])
        check("房主重连没有跳转、观众也没有",
              big_seeks([before] + seg, "host") == 0 and big_seeks([before] + seg, "viewer") == 0)
        check("房主重连后两边都贴着真值", max_abs(drifts(seg, "host")) <= 0.5 and max_abs(drifts(seg, "viewer")) <= 0.5,
              f"房主 {fmt(drifts(seg, 'host'))} 观众 {fmt(drifts(seg, 'viewer'))}")
        check("房主重连期间房间一直在播", all(s["playing"] for s in seg))

        # ---- 5. 观众按遥控器暂停（没有控制权） ----
        before = await lab.take()
        await viewer.native("pause")
        ok, seg = await lab.wait_until(lambda s: not s["viewer"]["paused"] and s["t"] - before["t"] > 1, 5, "观众遥控暂停")
        check("观众遥控器暂停后 5 秒内被同步拉回播放", ok)
        check("观众遥控器暂停时房间没有停", all(s["playing"] for s in seg) and room.playback.playing)
        check("观众看到「只有房主或有控制权的人能暂停」的提示",
              any("只有房主" in s["viewer"]["last_toast"] for s in seg), repr(seg[-1]["viewer"]["last_toast"]))
        seg = await lab.run(3, "观众遥控暂停后")
        check("观众遥控器暂停被撤销时直接站回房间位置（3 秒内 ≤ 0.5 秒，不用慢慢追）",
              max_abs(drifts(seg[-2:], "viewer")) <= 0.5, fmt(drifts(seg, "viewer")))
        check("观众遥控器暂停后没有「画面前跳」", not jumps([before] + seg, "viewer"))

        # ---- 6. 房主按遥控器暂停 / 播放 ----
        before = await lab.take()
        await host.native("pause")
        ok, seg = await lab.wait_until(lambda s: not s["playing"] and s["viewer"]["paused"], 4, "房主遥控暂停")
        check("房主遥控器暂停 → 房间暂停、观众也停", ok)
        check("暂停时两边位置一致（≤ 0.5 秒）", max_abs(gap(seg[-1:])) <= 0.5, fmt(gap(seg[-1:])))
        await lab.run(2, "房主遥控暂停中")
        await host.native("play")
        ok, seg = await lab.wait_until(lambda s: s["playing"] and not s["viewer"]["paused"] and not s["host"]["paused"], 4, "房主遥控播放")
        check("房主遥控器播放 → 房间继续、观众跟着走", ok)
        seg = await lab.run(3, "房主遥控播放后")
        check("遥控播放后两边贴着真值", max_abs(drifts(seg[-2:], "host")) <= 0.5 and max_abs(drifts(seg[-2:], "viewer")) <= 0.5,
              f"房主 {fmt(drifts(seg, 'host'))} 观众 {fmt(drifts(seg, 'viewer'))}")

        # ---- 7. 房主刷新页面 ----
        before = await lab.take()
        await host.page.reload()
        await host.click_start()
        ok, seg = await lab.wait_until(lambda s: (s["host"]["frames"] or 0) > 0 and abs(s["host"]["ct"] - s["room"]) < 1, 15, "房主刷新")
        check("房主刷新后 15 秒内回到房间位置", ok, f"房主 {seg[-1]['host']['ct']:.1f} 真值 {seg[-1]['room']:.1f}")
        check("房主刷新后还是房主", room.host_id == host_vid and seg[-1]["host"]["can_control"]
              and await host.viewer_id(rid) == host_vid)
        check("房主刷新期间观众没有跳转", big_seeks([before] + seg, "viewer") == 0,
              f"{seg[-1]['viewer']['big_seeks'] - before['viewer']['big_seeks']} 次")
        check("房主刷新期间房间一直在播、观众贴着真值", all(s["playing"] for s in seg) and max_abs(drifts(seg, "viewer")) <= 0.5)
        seg = await lab.run(3, "房主刷新后")
        check("房主刷新后两边贴着真值", max_abs(drifts(seg[-2:], "host")) <= 0.5 and max_abs(drifts(seg[-2:], "viewer")) <= 0.5)

        # ---- 8. 选集 ----
        await host.open_panel()
        v0 = room.version
        await host.page.click("#episodes button:nth-child(2)")
        ok, seg = await lab.wait_until(lambda s: s["version"] == v0 + 1 and s["host"]["has_video"] and s["viewer"]["has_video"]
                                       and s["host"]["rs"] >= 1 and s["viewer"]["rs"] >= 1, 15, "选第 2 集")
        check("选第 2 集：15 秒内两边都建好新播放器", ok)
        check("选第 2 集：房间换到第 2 集、停在开头、暂停",
              room.video.vod.episode == 1 and not room.playback.playing and lab.truth() < 0.5
              and abs(room.video.duration - EP2_SECONDS) < 1, f"集 {room.video.vod.episode} 时长 {room.video.duration:.0f}")
        s = seg[-1]
        check("选第 2 集：两边都停在开头", s["host"]["paused"] and s["viewer"]["paused"] and s["host"]["ct"] < 1 and s["viewer"]["ct"] < 1,
              f"{s['host']['ct']:.1f} / {s['viewer']['ct']:.1f}")
        check("选第 2 集：标题变了", "第 2 集" in s["host"]["title"] and "第 2 集" in s["viewer"]["title"], repr(s["viewer"]["title"]))
        await host.page.click("#play")
        ok, seg = await lab.wait_until(lambda s: s["host"]["ct"] > 1 and s["viewer"]["ct"] > 1, 10, "第 2 集起播")
        check("第 2 集播放后两边都在走", ok)
        seg = await lab.run(3, "第 2 集稳定")
        check("第 2 集两边贴着真值、相差 ≤ 0.5 秒", max_abs(drifts(seg[-2:], "host")) <= 0.5
              and max_abs(drifts(seg[-2:], "viewer")) <= 0.5 and max_abs(gap(seg[-2:])) <= 0.5, fmt(gap(seg)))

        # ---- 9. 换片源 ----
        before = await lab.take()
        v0 = room.version
        await host.page.click("#next-line")
        ok, seg = await lab.wait_until(lambda s: s["version"] == v0 + 1 and s["host"]["videos"] > before["host"]["videos"]
                                       and s["viewer"]["videos"] > before["viewer"]["videos"]
                                       and abs(s["host"]["ct"] - s["room"]) < 1 and abs(s["viewer"]["ct"] - s["room"]) < 1
                                       and not s["host"]["paused"] and not s["viewer"]["paused"], 12, "换片源")
        check("换片源：12 秒内两边都换好播放器并接上房间位置", ok,
              f"真值 {seg[-1]['room']:.1f} 房主 {seg[-1]['host']['ct']:.1f} 观众 {seg[-1]['viewer']['ct']:.1f}")
        check("换片源：换到线路二、还是第 2 集、仍在播放",
              room.video.vod.line == 1 and room.video.vod.episode == 1 and room.playback.playing)
        check("换片源：位置没有回到开头（真值连续）",
              seg[-1]["room"] >= before["room"] - 0.5, f"换前 {before['room']:.1f} 换后 {seg[-1]['room']:.1f}")
        seg = await lab.run(3, "换片源后稳定")
        check("换片源后两边贴着真值、相差 ≤ 0.5 秒", max_abs(drifts(seg[-2:], "host")) <= 0.5
              and max_abs(drifts(seg[-2:], "viewer")) <= 0.5 and max_abs(gap(seg[-2:])) <= 0.5, fmt(gap(seg)))
        check("换片源后没有「画面前跳」", not jumps(seg, "host") and not jumps(seg, "viewer"))

        # ---- 10. 房主交接 ----
        await host.open_panel()
        await host.page.evaluate("() => { window.confirm = () => true; }")
        await host.page.click("#members .make-host")
        ok, seg = await lab.wait_until(lambda s: room.host_id == viewer_vid and s["viewer"]["can_control"], 5, "房主交接")
        check("房主交接：观众成为房主、拿到控制", ok)
        check("房主交接：原房主保留控制权", seg[-1]["host"]["can_control"] and room.members[host_vid].can_control)
        check("房主交接时没人跳转", big_seeks(seg, "host") == 0 and big_seeks(seg, "viewer") == 0)
        # 新房主落后 3 秒 → 房间被拉到它那里；原房主（现在是观众）不跳、显示「超前」
        before = await lab.take()
        truth_before = before["room"]
        await viewer.nudge_video(-3)
        seg = await lab.run(5, "新房主落后 3 秒")
        pulled_by = min((s["room"] - truth_before) - (s["t"] - before["t"]) for s in seg)
        check("新房主落后时房间被拉回到新房主那里", pulled_by < -2, f"真值相对时间多退了 {pulled_by:+.1f} 秒")
        check("原房主（现在是观众）没有被自动跳", big_seeks([before] + seg, "host") == 0,
              f"{seg[-1]['host']['big_seeks'] - before['host']['big_seeks']} 次")
        check("原房主显示「超前 N 秒 · 同步」", any("超前" in s["host"]["sync_btn"] for s in seg), repr(seg[-1]["host"]["sync_btn"]))
        check("新房主自己没有被跳（只有我们拨的那一次）", big_seeks([before] + seg, "viewer") == 1)
        await host.page.click("#sync-btn")
        ok, _ = await lab.wait_until(lambda s: abs(s["host"]["ct"] - s["room"]) < 0.6 and abs(s["viewer"]["ct"] - s["room"]) < 0.6, 6, "交接后对齐")
        check("原房主点「同步」后两边重新贴着真值", ok)

        # ---- 服务器日志、浏览器 console ----
        warnings = b.capture.client_warnings()
        print(f"  服务器 WARNING（watch client …）{len(warnings)} 条", flush=True)
        for w in warnings:
            print("   " + w, flush=True)
        check("服务器没有收到「position jumped」回报", not any("position jumped" in w for w in warnings))
        check("服务器没有收到「playback failed」回报", not any("playback failed" in w for w in warnings))
        for c in (host, viewer):
            errors = [m for m in c.console if m.startswith("pageerror")]
            hls_errors = [m for m in c.console if "hls.js error" in m]
            if errors or hls_errors:
                print(f"--- {c.name} console ---\n" + "\n".join((errors + hls_errors)[-10:]), flush=True)
            check(f"{c.name}页面没有脚本错误", not errors)
            check(f"{c.name}的 hls.js 没有报错", not hls_errors)

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
