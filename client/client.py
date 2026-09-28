"""mrmeeseeks 本地客户端。

运行：python client.py --server ws://<server-ip>:8765

流程：
  1. 连上服务器握手，拿到本机标识（如 MEE-7K3Q）
  2. 在 Discord 里 @mrmeeseeks，选择这个标识，你会私下收到 8 位配对码
  3. 在本窗口输入配对码，配对成功
  4. 游戏中按住 Tab：截计分板；松开 Tab：截顶部进度条；两张图一起发给服务器
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import re
import socket
import sys
import time

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

PROTOCOL_VERSION = 1


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------- 截图 ----------------
def grab_jpeg(monitor: int, top_ratio: float | None = None, quality: int = 85) -> bytes:
    """截取指定显示器；top_ratio 不为空时只截顶部这一比例的区域。"""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        mon = dict(sct.monitors[monitor])
        if top_ratio:
            mon["height"] = max(1, int(mon["height"] * top_ratio))
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


# ---------------- Tab 监听 ----------------
class TabWatcher:
    """在 pynput 的线程里监听 Tab，把 按下/松开 事件送进 asyncio 队列。
    按住时系统会重复触发按下事件，这里只取第一次。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self.loop = loop
        self.queue = queue
        self.down = False
        self.listener = None

    def start(self) -> None:
        from pynput import keyboard

        def on_press(key):
            if key == keyboard.Key.tab and not self.down:
                self.down = True
                self.loop.call_soon_threadsafe(self.queue.put_nowait, ("down", time.monotonic()))

        def on_release(key):
            if key == keyboard.Key.tab and self.down:
                self.down = False
                self.loop.call_soon_threadsafe(self.queue.put_nowait, ("up", time.monotonic()))

        self.listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self.listener.daemon = True
        self.listener.start()


# ---------------- 客户端主体 ----------------
class MeeseeksClient:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.ws: ClientConnection | None = None
        self.client_id: str | None = None
        self.paired = False
        self.prompting = False
        self.last_sent = 0.0

    async def send(self, **msg) -> None:
        if self.ws is not None:
            await self.ws.send(json.dumps(msg, ensure_ascii=False))

    # ----- 配对码输入 -----
    async def prompt_code(self) -> None:
        if self.prompting:
            return
        self.prompting = True
        loop = asyncio.get_running_loop()
        try:
            while True:
                code = (await loop.run_in_executor(None, input, "Enter the 8-digit pairing code: ")).strip()
                if re.fullmatch(r"\d{8}", code):
                    break
                print("Invalid format: expected 8 digits.")
            await self.send(type="pair_code", code=code)
        except (EOFError, ConnectionClosed):
            pass
        finally:
            self.prompting = False

    # ----- 处理服务器消息 -----
    async def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "pair_request":
            log(f"Pairing request from Discord user {msg.get('user')} (channel #{msg.get('channel')}), "
                f"valid for {int(msg.get('expires_in', 300)) // 60} minutes.")
            asyncio.create_task(self.prompt_code())
        elif kind == "pair_result":
            if msg.get("ok"):
                self.paired = True
                log("✅ Paired! In game, hold Tab and release it.")
            else:
                left = msg.get("attempts_left", 0)
                log(f"❌ {msg.get('reason')}" + (f" ({left} attempts left)" if left else ""))
                if left:
                    asyncio.create_task(self.prompt_code())
        elif kind == "unpaired":
            self.paired = False
            log(f"Unpaired: {msg.get('reason')}. @mrmeeseeks in Discord to pair again; "
                f"this client's ID is still {self.client_id}.")
        elif kind == "analysis":
            log(f"💡 {msg.get('summary')}")
        elif kind in ("info", "error"):
            log(msg.get("message", ""))

    # ----- Tab 两段截图 -----
    async def capture_loop(self, queue: asyncio.Queue) -> None:
        a = self.args
        loop = asyncio.get_running_loop()
        score_task: asyncio.Future | None = None
        pressed_at = 0.0

        async def delayed_grab(delay: float, top_ratio: float | None) -> bytes:
            await asyncio.sleep(delay)
            return await loop.run_in_executor(None, grab_jpeg, a.monitor, top_ratio, a.quality)

        while True:
            kind, ts = await queue.get()
            if kind == "down":
                pressed_at = ts
                ready = self.paired and self.ws is not None
                cooling = time.monotonic() - self.last_sent < a.cooldown
                score_task = (asyncio.ensure_future(delayed_grab(a.score_delay, None))
                              if ready and not cooling else None)
                continue

            # 松开 Tab
            if score_task is None:
                continue
            held = ts - pressed_at
            if held < a.min_hold or not self.paired or self.ws is None:
                score_task.cancel()
                score_task = None
                continue
            if time.monotonic() - self.last_sent < a.cooldown:
                score_task.cancel()
                score_task = None
                continue
            try:
                scoreboard = await score_task
                hud = await delayed_grab(a.hud_delay, a.hud_ratio)
            except asyncio.CancelledError:
                continue
            except Exception as e:
                log(f"Screenshot failed: {e}")
                continue
            finally:
                score_task = None

            self.last_sent = time.monotonic()
            try:
                await self.send(type="snapshot",
                                scoreboard=base64.b64encode(scoreboard).decode(),
                                hud=base64.b64encode(hud).decode())
                log(f"Screenshots sent (scoreboard {len(scoreboard)//1024} KB, HUD {len(hud)//1024} KB)")
            except ConnectionClosed:
                log("Send failed: connection closed")

    # ----- 连接与重连 -----
    async def run(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        if not self.args.no_capture:
            TabWatcher(asyncio.get_running_loop(), queue).start()
            asyncio.create_task(self.capture_loop(queue))

        while True:
            try:
                async with connect(self.args.server, max_size=4 * 1024 * 1024,
                                   ping_interval=20, ping_timeout=20) as ws:
                    self.ws = ws
                    await self.send(type="hello", hostname=self.args.name, version=PROTOCOL_VERSION)
                    welcome = json.loads(await ws.recv())
                    self.client_id = welcome.get("client_id")
                    self.paired = False
                    log("=" * 44)
                    log(f"Connected to server. Client ID: {self.client_id}")
                    log("@mrmeeseeks in a Discord channel and pick this ID.")
                    log("=" * 44)
                    async for raw in ws:
                        await self.handle(json.loads(raw))
            except (OSError, ConnectionClosed) as e:
                log(f"Lost connection to server ({e.__class__.__name__}); reconnecting in {self.args.retry}s…")
            finally:
                self.ws = None
                self.paired = False
            await asyncio.sleep(self.args.retry)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="mrmeeseeks local screenshot client")
    p.add_argument("--server", default="ws://127.0.0.1:8765", help="Server URL, e.g. ws://1.2.3.4:8765 or wss://your-domain")
    p.add_argument("--name", default=socket.gethostname(), help="Device name shown in Discord")
    p.add_argument("--monitor", type=int, default=1, help="Monitor to capture (1 = primary)")
    p.add_argument("--score-delay", type=float, default=0.15, help="Delay after pressing Tab before capturing the scoreboard (s)")
    p.add_argument("--hud-delay", type=float, default=0.4, help="Delay after releasing Tab before capturing the HUD (s)")
    p.add_argument("--hud-ratio", type=float, default=0.18, help="Fraction of the screen top captured for the HUD")
    p.add_argument("--min-hold", type=float, default=0.2, help="Ignore Tab presses shorter than this (s)")
    p.add_argument("--cooldown", type=float, default=5.0, help="Minimum interval between screenshot uploads (s)")
    p.add_argument("--quality", type=int, default=85, help="JPEG quality")
    p.add_argument("--retry", type=float, default=5.0, help="Reconnect interval after disconnect (s)")
    p.add_argument("--no-capture", action="store_true", help="Only test connection and pairing; do not watch Tab")
    return p.parse_args(argv)


if __name__ == "__main__":
    try:
        asyncio.run(MeeseeksClient(parse_args()).run())
    except KeyboardInterrupt:
        sys.exit(0)
