"""端到端测试：真实的 WSServer + 真实的 client.py（--no-capture），Discord 部分用假对象代替。"""
import asyncio, base64, json, subprocess, time
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, os.path.join(ROOT, "client"))

from registry import Registry, MAX_CODE_ATTEMPTS
from ws_server import WSServer
from websockets.asyncio.client import connect

PORT = 18765
results = []

def check(name, cond):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name, flush=True)


class FakeEvents:
    def __init__(self):
        self.log = []
    async def client_paired(self, c): self.log.append(("paired", c.client_id, c.channel_id))
    async def pair_failed(self, c, ch, reason): self.log.append(("failed", c.client_id, reason))
    async def client_disconnected(self, c): self.log.append(("disc", c.client_id, c.paired))
    async def snapshot_received(self, c, sb, hud):
        self.log.append(("snap", c.client_id, sb, hud))
        await asyncio.sleep(0.5)
        return "Advice: stay as is"


async def wait_for(pred, timeout=5):
    t = time.time()
    while time.time() - t < timeout:
        if pred(): return True
        await asyncio.sleep(0.05)
    return False


async def main():
    reg = Registry()
    ev = FakeEvents()
    srv = WSServer(reg, ev, "127.0.0.1", PORT, min_snapshot_interval=1.0)
    await srv.start()

    # ---------- 1. 没有客户端 ----------
    check("无客户端时 available 为空", reg.available() == [])

    # ---------- 2. 真实 client.py 握手 ----------
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-u", os.path.join(ROOT, "client", "client.py"), "--no-capture",
        "--server", f"ws://127.0.0.1:{PORT}", "--name", "test-pc",
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = []
    async def pump():
        while True:
            line = await proc.stdout.readline()
            if not line: break
            out.append(line.decode()); print("   client>", line.decode().rstrip(), flush=True)
    pump_task = asyncio.create_task(pump())

    check("客户端握手成功", await wait_for(lambda: len(reg.available()) == 1))
    c = reg.available()[0]
    check("分配了 MEE-XXXX 标识", c.client_id.startswith("MEE-") and len(c.client_id) == 8)
    check("主机名正确", c.hostname == "test-pc")
    check("客户端打印了自己的标识", await wait_for(lambda: any(c.client_id in l for l in out)))

    # ---------- 3. 发起配对 ----------
    pending = reg.start_pairing(c.client_id, 42, "tester#0001", 999)
    check("配对码为 8 位数字", len(pending.code) == 8 and pending.code.isdigit())
    await c.send(type="pair_request", user="tester#0001", channel="ow", expires_in=300)
    await asyncio.sleep(0.3)

    wrong = "00000000" if pending.code != "00000000" else "11111111"
    proc.stdin.write(b"abc\n"); await proc.stdin.drain()           # 格式错误，客户端本地拦截
    proc.stdin.write(wrong.encode() + b"\n"); await proc.stdin.drain()
    check("错误配对码被拒绝并提示剩余次数",
          await wait_for(lambda: any("4 attempts left" in l for l in out)))
    check("格式错误本地拦截", any("Invalid format" in l for l in out))
    check("错误后尚未绑定", not c.paired)

    proc.stdin.write(pending.code.encode() + b"\n"); await proc.stdin.drain()
    check("正确配对码配对成功", await wait_for(lambda: c.paired))
    check("绑定到正确频道和用户", c.channel_id == 999 and c.user_id == 42)
    check("bot 收到 paired 事件", ("paired", c.client_id, 999) in ev.log)
    check("客户端显示配对成功", await wait_for(lambda: any("Paired" in l for l in out)))
    check("已绑定客户端不再出现在可选列表", reg.available() == [])

    # ---------- 4. 断开绑定 ----------
    reg.unpair(c.client_id)
    await c.send(type="unpaired", reason="test disconnect")
    check("客户端收到解绑通知", await wait_for(lambda: any("Unpaired" in l for l in out)))
    check("解绑后重新可选", reg.available() == [c])

    # ---------- 5. 连续错 5 次作废 ----------
    p2 = reg.start_pairing(c.client_id, 42, "tester", 999)
    await c.send(type="pair_request", user="tester", channel="ow", expires_in=300)
    await asyncio.sleep(0.2)
    bad = "00000000" if p2.code != "00000000" else "11111111"
    for _ in range(MAX_CODE_ATTEMPTS):
        proc.stdin.write(bad.encode() + b"\n"); await proc.stdin.drain()
        await asyncio.sleep(0.25)
    check("错 5 次后配对作废", await wait_for(lambda: any(e[0] == "failed" for e in ev.log)))
    check("作废后不能再用原配对码", reg.verify(c.client_id, p2.code)[0] is False)

    # ---------- 6. 多客户端 + 截图流程（原始 websocket 模拟） ----------
    async with connect(f"ws://127.0.0.1:{PORT}") as ws2:
        await ws2.send(json.dumps({"type": "hello", "hostname": "second-pc"}))
        cid2 = json.loads(await ws2.recv())["client_id"]
        check("第二个客户端拿到不同标识", cid2 != c.client_id and len(reg.available()) == 2)

        img = base64.b64encode(b"\xff\xd8fakejpeg").decode()
        await ws2.send(json.dumps({"type": "snapshot", "scoreboard": img, "hud": img}))
        r = json.loads(await ws2.recv())
        check("未配对时截图被拒绝", r["type"] == "error" and "Not paired" in r["message"])

        p3 = reg.start_pairing(cid2, 7, "other", 555)
        await ws2.send(json.dumps({"type": "pair_code", "code": p3.code}))
        r = json.loads(await ws2.recv())
        check("第二个客户端配对成功", r["ok"] is True)

        await ws2.send(json.dumps({"type": "snapshot", "scoreboard": img, "hud": img}))
        r = json.loads(await ws2.recv())
        check("截图被接收", r["type"] == "info" and "analyzing" in r["message"])
        await ws2.send(json.dumps({"type": "snapshot", "scoreboard": img, "hud": img}))
        r = json.loads(await ws2.recv())
        check("分析中时新截图被跳过", "still being analyzed" in r["message"])
        r = json.loads(await ws2.recv())
        check("分析结果回传给客户端", r["type"] == "analysis" and "stay as is" in r["summary"])
        snap = [e for e in ev.log if e[0] == "snap"][0]
        check("图片字节原样送达", snap[2] == b"\xff\xd8fakejpeg")

        await ws2.send(json.dumps({"type": "snapshot", "scoreboard": img, "hud": img}))
        r = json.loads(await ws2.recv())
        check("冷却时间内截图被跳过", "too often" in r["message"])

        await ws2.send(json.dumps({"type": "snapshot", "scoreboard": "!!!", "hud": img}))
        await asyncio.sleep(1.1)
        await ws2.send(json.dumps({"type": "snapshot", "scoreboard": "!!!notb64", "hud": img}))
        r = json.loads(await ws2.recv())
        # 第一条 "!!!" 也会在冷却内被跳过或报格式错误
        msgs = [r]
        try:
            while True:
                msgs.append(json.loads(await asyncio.wait_for(ws2.recv(), 0.5)))
        except asyncio.TimeoutError:
            pass
        check("非法 base64 被拒绝", any("Invalid screenshot data" in m.get("message", "") for m in msgs))

    check("客户端断开时 bot 收到事件且带绑定信息",
          await wait_for(lambda: ("disc", cid2, True) in ev.log))
    check("断开后从注册表移除", reg.get(cid2) is None)

    # ---------- 7. 未握手的连接被关闭 ----------
    async with connect(f"ws://127.0.0.1:{PORT}") as ws3:
        await ws3.send(json.dumps({"type": "snapshot"}))
        try:
            await asyncio.wait_for(ws3.recv(), 2); closed = False
        except Exception:
            closed = True
    check("不先 hello 的连接被关闭", closed)

    # ---------- 8. 配对码过期 ----------
    p4 = reg.start_pairing(c.client_id, 1, "x", 1)
    reg.get(c.client_id).pending.expires_at = time.time() - 1
    ok, reason, _ = reg.verify(c.client_id, p4.code)
    check("过期配对码被拒绝", not ok and "expired" in reason)

    # ---------- 9. Discord 下拉菜单可构造 ----------
    import tempfile
    from pathlib import Path
    from bot import ClientSelectView, MeeseeksBot, HELP_TEXT
    from label_ui import LabelView, build_roster
    from labeling import LabelStore
    from vision.heroes import HeroMatcher
    from vision.recognize import Recognizer
    tmp = tempfile.TemporaryDirectory()
    matcher = HeroMatcher([])
    labels = LabelStore(Path(tmp.name), matcher)
    roster = build_roster([{"key": "ana", "name": "Ana", "role": "support"},
                           {"key": "dva", "name": "D.Va", "role": "tank"}])
    bot = MeeseeksBot(reg, None, Recognizer(matcher, [], ocr=lambda img: []), labels, roster)
    view = ClientSelectView(bot, 42, reg.available())
    sel = view.children[0]
    check("下拉菜单列出可用客户端", [o.value for o in sel.options] == [x.client_id for x in reg.available()])
    check("英雄显示名来自名单", bot.hero_name("dva") == "D.Va" and bot.hero_name("soldier-76") == "Soldier: 76")

    # ---------- 10. 标注菜单可构造 ----------
    lv = LabelView(labels, roster, bot.hero_name, "pid", {42})
    first = [o.value for o in lv.children[0].options]
    check("标注菜单第一级：三个职责 + 阵亡/未选/丢弃",
          first == ["tank", "damage", "support", "_dead", "_empty", "_discard"])

    proc.kill(); await proc.wait(); pump_task.cancel()
    await srv.close()
    await bot.close()

    passed = sum(r for _, r in results)
    print("\n%d/%d passed" % (passed, len(results)))
    return passed == len(results)


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
