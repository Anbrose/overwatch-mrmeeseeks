"""一起看浏览器实验室：给 test_watch_sync_browser.py / test_watch_lab_ops.py 共用的部分。

真实 WatchServer（allow_http）+ 本地假 CDN（ffmpeg 生成的 HLS，带 CORS）+ Playwright 驱动本机的 Chrome / Edge，
每个客户端一个独立的浏览器上下文；页面里装探针记 <video> 的 seek / waiting / ratechange，采样时连同
服务器上房间的真值位置一起记下来。不是测试本身，没有 check。
"""
import asyncio
import logging
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import watch_server as ws_mod  # noqa: E402
from vod_search import Episode, Line, Show  # noqa: E402
from watch_rooms import RoomManager  # noqa: E402

SAMPLE_S = 0.5
FPS = 25
SEGMENT_S = 2
BIG_SEEK = 0.5              # seek 跨度小于这个的算 hls.js 的 nudge，不算跳转


def skip(reason):
    print(f"SKIP {reason}", flush=True)
    sys.exit(0)


def requirements_or_skip():
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        skip("没装 playwright（pip install playwright）")
    if shutil.which("ffmpeg") is None:
        skip("PATH 里没有 ffmpeg，生成不了测试片")


# ---------------------------------------------------------------- 测试片 + 假 CDN

def make_clip(out_dir, seconds, size="160x90"):
    """testsrc2 + 正弦音，H.264 baseline + AAC，SEGMENT_S 秒一片。"""
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={FPS}",
           "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
           "-t", str(seconds),
           "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.0", "-pix_fmt", "yuv420p",
           "-preset", "veryfast", "-crf", "32", "-g", str(FPS * SEGMENT_S), "-keyint_min", str(FPS * SEGMENT_S),
           "-sc_threshold", "0",
           "-c:a", "aac", "-b:a", "24k", "-ac", "1",
           "-f", "hls", "-hls_time", str(SEGMENT_S), "-hls_list_size", "0",
           "-hls_segment_filename", os.path.join(out_dir, "seg%03d.ts"),
           os.path.join(out_dir, "index.m3u8")]
    subprocess.run(cmd, check=True, timeout=300)


@web.middleware
async def cors(request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        resp = e
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Expose-Headers"] = "Content-Length, Content-Range"
    return resp


class NoSources:
    async def resolve(self, text):
        raise RuntimeError("实验室里不换 B 站 / YouTube 视频")


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(self.format(record))

    def client_warnings(self):
        return [r for r in self.records if "watch client" in r]


# ---------------------------------------------------------------- 页面探针

# 页面一加载就装上：在每个 <video> 上记 seeking（起止位置）、waiting、ratechange，供采样读取。
INIT_JS = r"""
(() => {
  window.__probe = { seeks: [], waits: 0, rates: [], videos: 0, toasts: [] };
  const seen = new WeakSet();
  const hook = (v) => {
    if (!(v instanceof HTMLVideoElement) || seen.has(v)) return;
    seen.add(v);
    window.__probe.videos++;
    let last = 0;
    v.addEventListener("timeupdate", () => { if (!v.seeking) last = v.currentTime; });
    v.addEventListener("seeking", () => {
      window.__probe.seeks.push({ t: performance.now() / 1000, from: last, to: v.currentTime });
      last = v.currentTime;
    });
    v.addEventListener("waiting", () => { window.__probe.waits++; });
    v.addEventListener("ratechange", () => { window.__probe.rates.push([performance.now() / 1000, v.playbackRate]); });
  };
  const orig = document.createElement.bind(document);
  document.createElement = function (tag, opts) {
    const el = orig(tag, opts);
    if (String(tag).toLowerCase() === "video") hook(el);
    return el;
  };
  new MutationObserver((muts) => {
    for (const m of muts) for (const n of m.addedNodes) {
      if (n instanceof HTMLVideoElement) hook(n);
      else if (n.querySelectorAll) n.querySelectorAll("video").forEach(hook);
    }
  }).observe(document, { childList: true, subtree: true });
  // toast 文案：#toast 的内容每次变化都记下来
  document.addEventListener("DOMContentLoaded", () => {
    const t = document.getElementById("toast");
    if (!t) return;
    new MutationObserver(() => {
      const text = t.textContent.trim();
      if (text && !t.hidden) window.__probe.toasts.push(text);
    }).observe(t, { childList: true, characterData: true, subtree: true, attributes: true });
  });
})();
"""

SAMPLE_JS = r"""
(big) => {
  const v = document.querySelector("#player-host video");
  const q = v && v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality() : null;
  const btn = document.getElementById("sync-btn");
  const stage = document.getElementById("stage-msg");
  const play = document.getElementById("play");
  let buffered = null;
  if (v && v.buffered.length) buffered = [v.buffered.start(0), v.buffered.end(v.buffered.length - 1)];
  const seeks = window.__probe.seeks;
  return {
    t: performance.now() / 1000,
    has_video: !!v,
    videos: window.__probe.videos,
    ct: v ? v.currentTime : null,
    paused: v ? v.paused : null,
    ended: v ? v.ended : null,
    rate: v ? v.playbackRate : null,
    rs: v ? v.readyState : null,
    frames: q ? q.totalVideoFrames : null,
    dropped: q ? q.droppedVideoFrames : null,
    buffered,
    seeks: seeks.length,
    big_seeks: seeks.filter((s) => Math.abs(s.to - s.from) >= big).length,
    waits: window.__probe.waits,
    toasts: window.__probe.toasts.length,
    last_toast: window.__probe.toasts[window.__probe.toasts.length - 1] || "",
    sync_btn: btn && !btn.hidden ? btn.textContent : "",
    stage: stage ? stage.textContent : "",
    can_control: !!(play && !play.disabled),
    title: document.getElementById("title").textContent,
  };
}
"""


class Client:
    def __init__(self, name, page, ctx):
        self.name = name
        self.page = page
        self.ctx = ctx
        self.console = []

    async def sample(self):
        return await self.page.evaluate(SAMPLE_JS, BIG_SEEK)

    async def click_start(self, timeout=30000):
        await self.page.wait_for_selector("#start:not([disabled])", state="visible", timeout=timeout)
        await self.page.click("#start")

    async def nudge_video(self, delta=None, to=None):
        """直接把 <video> 拨到别处（模拟卡顿后落后、标签页被限速后的偏差）。算一次自己的 seek。"""
        await self.page.evaluate(
            "([d, to]) => { const v = document.querySelector('#player-host video');"
            " v.currentTime = to === null ? v.currentTime + d : to; }", [delta, to])

    async def native(self, action):
        """像遥控器 / 媒体键那样直接对 <video> 调 play() / pause()。"""
        await self.page.evaluate(f"() => document.querySelector('#player-host video').{action}()")

    async def seek_ui(self, position, duration):
        """像人一样拖进度条。"""
        await self.page.evaluate("""([pos, dur]) => {
            const s = document.getElementById('seek');
            s.value = String(Math.round(pos / dur * 1000));
            s.dispatchEvent(new Event('change', { bubbles: true }));
        }""", [position, duration])

    async def viewer_id(self, room_id):
        return await self.page.evaluate("(k) => localStorage.getItem(k)", f"watch_viewer_id:{room_id}")

    async def open_panel(self):
        hidden = await self.page.evaluate("() => document.getElementById('perm-panel').hidden")
        if hidden:
            await self.page.click("#perm-btn")

    async def close(self):
        await self.ctx.close()


class Lab:
    """所有页面 + 服务器真值的采样器。samples: [{t, room, playing, version, <client key>: {...}}]"""

    def __init__(self, room):
        self.room = room
        self.clients = {}
        self.samples = []
        self.phases = []   # (name, start_index, end_index)

    def add(self, key, client):
        self.clients[key] = client

    def remove(self, key):
        self.clients.pop(key, None)

    def truth(self):
        return self.room.playback.position_at(time.time(), self.room.video.duration)

    async def take(self):
        keys = list(self.clients)
        got = await asyncio.gather(*(self.clients[k].sample() for k in keys))
        s = {"t": time.time(), "room": self.truth(), "playing": self.room.playback.playing,
             "version": self.room.version}
        s.update(zip(keys, got))
        self.samples.append(s)
        return s

    async def run(self, seconds, name):
        start = len(self.samples)
        end_at = time.time() + seconds
        while time.time() < end_at:
            await self.take()
            await asyncio.sleep(SAMPLE_S)
        self.phases.append((name, start, len(self.samples)))
        return self.samples[start:]

    async def wait_until(self, pred, timeout, name):
        """每 SAMPLE_S 采一次，直到 pred(sample) 为真或超时；返回 (是否满足, 这一段样本)。"""
        start = len(self.samples)
        end_at = time.time() + timeout
        ok = False
        while time.time() < end_at:
            s = await self.take()
            try:
                hit = pred(s)
            except (TypeError, KeyError):
                hit = False
            if hit:
                ok = True
                break
            await asyncio.sleep(SAMPLE_S)
        self.phases.append((name, start, len(self.samples)))
        return ok, self.samples[start:]


# ---------------------------------------------------------------- 指标

def drifts(samples, who):
    """who 相对房间真值的偏差（真值 − 页面位置；正数 = 落后）。"""
    return [s["room"] - s[who]["ct"] for s in samples if who in s and s[who]["ct"] is not None]


def gap(samples, a="host", b="viewer"):
    """两个页面之间的位置差（a − b）。"""
    return [s[a]["ct"] - s[b]["ct"] for s in samples
            if a in s and b in s and s[a]["ct"] is not None and s[b]["ct"] is not None]


def fps_of(samples, who):
    """按帧数差 / 时间差估算帧率（只算两端都在播放的区间）。"""
    rates = []
    for a, b in zip(samples, samples[1:]):
        pa, pb = a[who], b[who]
        if pa["frames"] is None or pb["frames"] is None or pa["paused"] or pb["paused"]:
            continue
        dt = pb["t"] - pa["t"]
        if dt > 0:
            rates.append((pb["frames"] - pa["frames"]) / dt)
    return statistics.median(rates) if rates else 0.0


def dropped_ratio(samples, who):
    first, last = samples[0][who], samples[-1][who]
    if first["frames"] is None or last["frames"] is None:
        return 1.0
    total = last["frames"] - first["frames"]
    return (last["dropped"] - first["dropped"]) / total if total > 0 else 0.0


def jumps(samples, who, threshold=1.5):
    """一个采样间隔内位置比时间（按倍速）多走了 threshold 秒以上，而且页面没 seek：这就是「画面前跳」。"""
    found = []
    for a, b in zip(samples, samples[1:]):
        pa, pb = a[who], b[who]
        if pa["ct"] is None or pb["ct"] is None or pb["seeks"] != pa["seeks"] or pa["videos"] != pb["videos"]:
            continue
        dt = pb["t"] - pa["t"]
        expected = 0 if pa["paused"] else dt * (pa["rate"] or 1)
        extra = (pb["ct"] - pa["ct"]) - expected
        if extra > threshold:
            found.append((round(pa["ct"], 1), round(pb["ct"], 1), round(extra, 1)))
    return found


def big_seeks(samples, who):
    return samples[-1][who]["big_seeks"] - samples[0][who]["big_seeks"]


def max_abs(xs):
    return max(abs(x) for x in xs) if xs else float("inf")


def fmt(xs):
    if not xs:
        return "无样本"
    return f"min {min(xs):+.2f} / 中位 {statistics.median(xs):+.2f} / max {max(xs):+.2f}"


# ---------------------------------------------------------------- 搭台

class Bench:
    """一次实验：假 CDN、WatchServer、浏览器。用 `async with Bench(clips) as b:`。
    clips: {名字: 秒数}，每段生成一次，`b.clip_url(名字, 前缀)` 给出可以挂在任意前缀下的 m3u8 地址
    （同一段片子可以挂在多个前缀下，当成不同的线路 / 剧集）。"""

    def __init__(self, clips, headed=False):
        self.clips = clips
        self.headed = headed
        self.capture = LogCapture()
        self.clients = []
        self.tmp = None
        self.cdn = None
        self.site = None
        self.rooms = None
        self.server = None
        self.pw = None
        self.browser = None
        self.base = None
        self.cdn_base = None
        self._mounts = {}

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self.capture.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(self.capture)
        logging.getLogger().setLevel(logging.INFO)

        self.tmp = tempfile.mkdtemp(prefix="watch_lab_")
        self._cdn_app = web.Application(middlewares=[cors])
        for name, seconds in self.clips.items():
            d = os.path.join(self.tmp, name)
            os.makedirs(d)
            make_clip(d, seconds)
            self._mounts[name] = d
        self.cdn = TestClient(TestServer(self._cdn_app))
        self.rooms = RoomManager()
        self.server = ws_mod.WatchServer(self.rooms, NoSources(), "127.0.0.1", 0, origin="http://127.0.0.1",
                                         allow_http=True)
        self.site = TestClient(TestServer(self.server.app))
        await self.site.start_server()
        self.base = str(self.site.make_url("/")).rstrip("/")

        self.pw = await async_playwright().start()
        last = None
        for channel in ("chrome", "msedge", None):
            try:
                self.browser = await self.pw.chromium.launch(
                    channel=channel, headless=not self.headed,
                    args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"])
                print(f"浏览器：{channel or 'playwright chromium'} {self.browser.version}", flush=True)
                break
            except Exception as e:   # 没装这个浏览器
                last = e
        if self.browser is None:
            await self.__aexit__(None, None, None)
            skip(f"没有可用的 Chrome / Edge / Chromium：{last}")
        return self

    async def __aexit__(self, *exc):
        try:
            for c in list(self.clients):
                try:
                    await c.close()
                except Exception:
                    pass
            if self.browser is not None:
                await self.browser.close()
        finally:
            if self.pw is not None:
                await self.pw.stop()
            if self.site is not None:
                await self.site.close()
            if self.cdn is not None and self.cdn.server.started:
                await self.cdn.close()
            logging.getLogger().removeHandler(self.capture)
            shutil.rmtree(self.tmp, ignore_errors=True)

    def mount(self, name, prefix):
        """把一段片子挂到 CDN 的某个前缀下（要在第一次 make_room 之前做完）。返回 m3u8 地址的尾部。"""
        self._cdn_app.router.add_static(prefix, self._mounts[name], show_index=False)
        return f"{prefix}/index.m3u8"

    async def start_cdn(self):
        if not self.cdn.server.started:
            await self.cdn.start_server()
            self.cdn_base = str(self.cdn.make_url("/")).rstrip("/")

    def show(self, title, lines):
        """lines: {线路名: [每集的 m3u8 尾部]}"""
        return Show(key=f"{title}|2026", title=title, year=2026, type_name="剧集", lines=[
            Line(name, [Episode(f"第{i + 1}集", f"{self.cdn_base}{tail}") for i, tail in enumerate(tails)])
            for name, tails in lines.items()])

    async def make_room(self, show, episode=0, line=0):
        await self.start_cdn()
        video, pl = await self.server.prepare_vod(show, episode, line)
        room = self.rooms.create(1, 2, "实验", video)
        self.server.attach_playlist(room, pl)
        return room

    def host_url(self, room):
        return f"{self.base}/r/{room.room_id}?k={room.owner_key}"

    def viewer_url(self, room):
        return f"{self.base}/r/{room.room_id}"

    async def open_client(self, name, url, before_goto=None):
        ctx = await self.browser.new_context(viewport={"width": 960, "height": 600})
        await ctx.add_init_script(INIT_JS)
        page = await ctx.new_page()
        c = Client(name, page, ctx)
        page.on("console", lambda m: c.console.append(f"{m.type}: {m.text}"))
        page.on("pageerror", lambda e: c.console.append(f"pageerror: {e}"))
        if before_goto:
            await before_goto(page)
        await page.goto(url)
        self.clients.append(c)
        return c

    async def close_client(self, c):
        self.clients.remove(c)
        await c.close()

    def conns(self, room):
        return list(self.server.conns.get(room.room_id, ()))

    async def drop_connection(self, room, viewer_id):
        """服务器这边把某人的 WebSocket 掐掉（模拟网络闪断）；页面 2 秒后会用同一个 viewer_id 重连。"""
        for c in self.conns(room):
            if c.viewer_id == viewer_id:
                await c.ws.close()


def segment_delayer(seconds):
    """给 page.route 用：每个分片都晚 seconds 秒才放行。返回 (handler, 开关 dict)。"""
    switch = {"on": True}

    async def handler(route):
        if switch["on"]:
            await asyncio.sleep(seconds)
        await route.continue_()
    return handler, switch
