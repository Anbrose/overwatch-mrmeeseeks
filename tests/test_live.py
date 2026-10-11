"""开播通知测试：链接解析、B 站接口返回解析、开播/下播判定、直播源文件读写、轮询循环。

运行：python tests/test_live.py
不联网，不需要 Discord（live_watch 模块不依赖 discord.py）。
"""
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import live_watch as lw  # noqa: E402

results = []


def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


def room(room_id, live, title="t", uname="u", short_id=0):
    return {"room_id": room_id, "uid": 1, "live_status": live, "live_url": f"https://live.bilibili.com/{room_id}",
            "title": title, "uname": uname, "cover": "https://i0.hdslb.com/c.jpg", "short_id": short_id,
            "parent_area_name": "赛事", "area_name": "游戏赛事", "live_time": "2026-09-07 14:22:39"}


def api(*rooms):
    return {"code": 0, "message": "OK", "data": {"by_uids": {}, "by_room_ids": {str(r["room_id"]): r for r in rooms}}}


# ---------------- 解析 ----------------
def test_parsing():
    p = lw.parse_bilibili_target
    check("纯数字", p("7734200") == 7734200)
    check("完整链接带参数", p("https://live.bilibili.com/7734200?spm_id_from=x&visit_id=y") == 7734200)
    check("无协议链接", p("live.bilibili.com/6") == 6)
    check("Discord 包了 <> 的链接", p("<https://live.bilibili.com/21452505>") == 21452505)
    check("h5 链接", p("https://live.bilibili.com/h5/7734200") == 7734200)
    check("非 B 站链接返回 None", p("https://twitch.tv/overwatchleague") is None)
    check("乱写返回 None", p("abc") is None)

    st = lw.parse_room_info(api(room(7734200, 1, "OWCS 决赛", "守望先锋赛事", short_id=6), room(21452505, 0), room(3, 2)))
    check("解析出三个房间", set(st) == {7734200, 21452505, 3})
    check("live_status 1 为直播中", st[7734200].live and st[7734200].title == "OWCS 决赛" and st[7734200].uname == "守望先锋赛事")
    check("live_status 0 为未开播", not st[21452505].live)
    check("live_status 2 轮播算未开播", not st[3].live)
    check("分区拼接", st[7734200].area == "赛事 / 游戏赛事")
    check("接口返回空字典不报错", lw.parse_room_info({"code": 0, "data": {"by_room_ids": {}}}) == {})
    try:
        lw.parse_room_info({"code": -412, "message": "请求被拦截"})
        check("接口 code 非 0 抛 LiveError", False)
    except lw.LiveError as e:
        check("接口 code 非 0 抛 LiveError", "-412" in str(e))


# ---------------- 开播/下播判定 ----------------
def test_transitions():
    src_a = lw.Source("bilibili", 1, "cn", "A", "https://live.bilibili.com/1")
    src_b = lw.Source("bilibili", 2, "emea", "B", "https://live.bilibili.com/2")
    sources = {src_a.key: src_a, src_b.key: src_b}
    S = lambda rid, live: lw.RoomStatus(rid, live)  # noqa: E731

    state, events = lw.detect_transitions({}, sources, {1: S(1, True), 2: S(2, False)})
    check("第一轮只记基线不发事件", events == [] and state == {"bilibili:1": True, "bilibili:2": False})

    state, events = lw.detect_transitions(state, sources, {1: S(1, True), 2: S(2, True)})
    check("未开播→直播中 发 live 事件", [(e.kind, e.source.key) for e in events] == [("live", "bilibili:2")])

    state, events = lw.detect_transitions(state, sources, {1: S(1, False), 2: S(2, True)})
    check("直播中→未开播 发 offline 事件", [(e.kind, e.source.key) for e in events] == [("offline", "bilibili:1")])

    state, events = lw.detect_transitions(state, sources, {1: S(1, False), 2: S(2, True)})
    check("状态不变不发事件", events == [])

    state, events = lw.detect_transitions(state, sources, {2: S(2, True)})
    check("查询结果缺某房间时保留旧状态", events == [] and state["bilibili:1"] is False)

    state, events = lw.detect_transitions(state, {src_b.key: src_b}, {2: S(2, True)})
    check("移除的源从状态里清掉", "bilibili:1" not in state)


# ---------------- 存储 ----------------
def test_store():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "live_sources.json")
        store = lw.LiveStore(path)
        store.load()
        check("不存在的文件 → 空配置", store.sources == {})

        store.add(lw.Source("bilibili", 7734200, "cn", "守望先锋赛事", "https://live.bilibili.com/7734200", added_by="me"))
        store.add(lw.Source("bilibili", 5, "na", "NA restream", "https://live.bilibili.com/5"))
        check("自动创建目录并落盘", os.path.exists(path) and not [f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")])

        again = lw.LiveStore(path)
        again.load()
        check("重新加载后直播源一致（含中文）",
              again.sources["bilibili:7734200"].label == "守望先锋赛事" and again.sources["bilibili:7734200"].added_by == "me")
        groups = again.by_region()
        check("按赛区分组", set(groups) == {"cn", "na"} and groups["na"][0].room_id == 5)
        check("删除存在的源", again.remove("bilibili:5") is not None and "bilibili:5" not in json.load(open(path, encoding="utf-8"))["sources"])
        check("删除不存在的源返回 None", again.remove("bilibili:999") is None)

        with open(path, "w") as f:
            f.write("{broken")
        broken = lw.LiveStore(path)
        broken.load()
        check("文件损坏时用空配置、不抛错", broken.sources == {})

    mem = lw.LiveStore(None)
    mem.add(lw.Source("bilibili", 1, "cn", "x", "u"))
    check("path=None 只存内存", "bilibili:1" in mem.sources)


# ---------------- 客户端（假 fetch）+ Watcher ----------------
def test_client_and_watcher():
    calls = []
    live = {7734200: 0}

    def fake_fetch(url, params):
        calls.append((url, params))
        if url == lw.ROOM_INIT_URL:
            if params["id"] in (6, 7734200):
                return {"code": 0, "data": {"room_id": 7734200, "short_id": 6}}
            return {"code": 60004, "msg": "房间不存在", "data": []}
        return api(*[room(r, live.get(r, 0)) for r in params["room_ids"]])

    client = lw.BilibiliClient(fetch=fake_fetch)

    async def go():
        check("短号解析为真实房间号", await client.resolve_room(6) == 7734200)
        try:
            await client.resolve_room(999)
            check("不存在的房间抛 LiveError", False)
        except lw.LiveError:
            check("不存在的房间抛 LiveError", True)

        calls.clear()
        st = await client.statuses(range(1, 46))
        check("45 个房间分 3 批查询", len(calls) == 3 and len(st) == 45)
        check("去重且不重复请求", len(await client.statuses([1, 1, 2])) == 2)

        store = lw.LiveStore(None)
        notified = []

        async def notify(ev):
            notified.append((ev.kind, ev.source.key, ev.status.title))

        watcher = lw.LiveWatcher(store, client, notify, interval=0.01)
        check("轮询间隔不低于下限", watcher.interval == lw.MIN_POLL_INTERVAL)
        watcher.interval = 0.01
        n_calls = len(calls)
        check("没有源时 poll 返回空且不请求", await watcher.poll_once() == [] and len(calls) == n_calls)

        store.add(lw.Source("bilibili", 7734200, "cn", "OW", "https://live.bilibili.com/7734200"))
        ev = await watcher.poll_once()
        check("新源第一轮不发事件", ev == [] and watcher.state == {"bilibili:7734200": False})
        live[7734200] = 1
        ev = await watcher.poll_once()
        check("开播产生 live 事件", [e.kind for e in ev] == ["live"] and watcher.last_statuses[7734200].live)

        # run() 循环：开播 → 通知；接口报错 → 记录 last_error 且不崩
        live[7734200] = 0
        await watcher.poll_once()
        task = asyncio.create_task(watcher.run())
        live[7734200] = 1
        await asyncio.sleep(0.1)
        check("run 循环调用 notify", ("live", "bilibili:7734200", "t") in notified)

        def boom(url, params):
            raise lw.LiveError("Bilibili returned HTTP 412 (rate limited?)")
        client._fetch = boom
        await asyncio.sleep(0.1)
        check("查询失败记录 last_error 且循环仍在跑", watcher.last_error and "412" in watcher.last_error and not task.done())
        client._fetch = fake_fetch
        await asyncio.sleep(0.1)
        check("恢复后 last_error 清空", watcher.last_error is None)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())


if __name__ == "__main__":
    import logging
    logging.disable(logging.WARNING)
    test_parsing()
    test_transitions()
    test_store()
    test_client_and_watcher()
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)
