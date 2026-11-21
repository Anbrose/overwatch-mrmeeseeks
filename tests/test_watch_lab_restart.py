"""一起看多客户端实测（服务器重启）：真实 WatchServer + 真实 Chrome。部署时进程会重启，正在看的人不该被赶出去。

运行：python tests/test_watch_lab_restart.py [--headed]
需要和 tests/test_watch_sync_browser.py 一样：playwright、Chrome / Edge、ffmpeg、联网；缺了就 SKIP。约 1 分钟。

做的事：房主和观众在一个 vod 房间里播放；服务器把房间存盘后停掉，几秒后在同一个端口起一个全新的
（新的 RoomManager，只靠存盘文件恢复，见 server/watch_store.py）。然后看：
- 页面没有显示「已结束」，自己连回来了，还是原来的 viewer_id；房主还是房主；
- 停机期间画面一直在走（片段来自 CDN，不经过服务器），连回来没有跳转，仍贴着房间位置；
- 版本号没变（所以播放器不用重建）；
- 重启后房主的暂停照样同步给观众；
- 重启后新进来的人也能起播（m3u8 缓存没了，服务器会重新取）。
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from watch_lab import (Bench, Lab, big_seeks, drifts, fmt, max_abs, requirements_or_skip, skip)  # noqa: E402

CLIP_SECONDS = 240
DOWN_SECONDS = 4
results = []


def check(name, cond, note=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{note}]" if note else ""), flush=True)


async def main(args):
    requirements_or_skip()
    async with Bench({"ep1": CLIP_SECONDS}, headed=args.headed) as b:
        tail = b.mount("ep1", "/l1/e1")
        await b.start_cdn()
        room = await b.make_room(b.show("实验剧", {"线路一": [tail]}))
        rid = room.room_id
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
        await lab.run(3, "重启前稳定")
        before = await lab.take()
        old_version, old_names = room.version, {m.viewer_id: m.name for m in room.members.values()}

        # ---- 重启 ----
        restored = await b.restart_server(DOWN_SECONDS)
        new_room = b.rooms.get(rid)
        check("重启后房间接回来了", restored == 1 and new_room is not None and new_room is not room)
        if new_room is None:
            skip("房间没恢复，后面的检查没有意义")
        lab.room = new_room
        check("版本号没变（播放器不用重建）", new_room.version == old_version)

        def both_back(_s):
            ids = {c.viewer_id for c in b.conns(new_room)}
            return host_vid in ids and viewer_vid in ids
        ok, seg = await lab.wait_until(both_back, 15, "重启后重连")
        check("两个页面 15 秒内自己连回来，还是原来的 viewer_id", ok)
        check("没有多出新的成员，名字没变", {m.viewer_id: m.name for m in new_room.members.values()} == old_names)
        check("房主还是房主", new_room.host_id == host_vid)
        after = await lab.take()
        check("页面没有显示已结束", "结束" not in (after["host"]["stage"] or "") and "结束" not in (after["viewer"]["stage"] or ""),
              f"房主 {after['host']['stage']!r} 观众 {after['viewer']['stage']!r}")
        check("停机期间画面一直在走", after["host"]["ct"] - before["host"]["ct"] >= DOWN_SECONDS
              and after["viewer"]["ct"] - before["viewer"]["ct"] >= DOWN_SECONDS,
              f"房主 +{after['host']['ct'] - before['host']['ct']:.1f} 秒 观众 +{after['viewer']['ct'] - before['viewer']['ct']:.1f} 秒")
        seg = await lab.run(5, "重启后稳定")
        check("重启前后都没有跳转", after["host"]["big_seeks"] == before["host"]["big_seeks"]
              and after["viewer"]["big_seeks"] == before["viewer"]["big_seeks"]
              and big_seeks(seg, "host") == 0 and big_seeks(seg, "viewer") == 0,
              f"房主 {seg[-1]['host']['big_seeks'] - before['host']['big_seeks']} 次 "
              f"观众 {seg[-1]['viewer']['big_seeks'] - before['viewer']['big_seeks']} 次")
        check("重启后两边仍贴着房间位置（≤ 0.5 秒）", max_abs(drifts(seg, "host")) <= 0.5 and max_abs(drifts(seg, "viewer")) <= 0.5,
              f"房主 {fmt(drifts(seg, 'host'))}；观众 {fmt(drifts(seg, 'viewer'))}")

        # ---- 重启后新进来的人 ----
        late = await b.open_client("重启后加入", b.viewer_url(new_room))
        lab.add("late", late)
        await late.click_start()
        ok, seg = await lab.wait_until(lambda s: (s["late"]["frames"] or 0) > 0, 15, "重启后加入起播")
        check("重启后新进来的人 15 秒内画出第一帧", ok)
        seg = await lab.run(4, "重启后加入稳定")
        check("新进来的人贴着房间位置（≤ 0.5 秒）", max_abs(drifts(seg[-2:], "late")) <= 0.5, fmt(drifts(seg, "late")))

        # ---- 重启后的控制 ----
        await host.page.click("#play")
        ok, _ = await lab.wait_until(lambda s: not s["playing"] and s["viewer"]["paused"] and s["late"]["paused"], 8, "重启后暂停")
        check("重启后房主暂停，房间和观众都停", ok)

        for c in (host, viewer, late):
            errors = [m for m in c.console if m.startswith("pageerror")]
            if errors:
                print(f"--- {c.name} console ---\n" + "\n".join(errors[-10:]), flush=True)
            check(f"{c.name}页面没有脚本错误", not errors)

    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed:\n  " + "\n  ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--headed", action="store_true", help="显示浏览器窗口（默认无头）")
    asyncio.run(main(ap.parse_args()))
