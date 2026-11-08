"""一起看网页同步策略测试：在 node 里加载真实的 player.js（假 DOM、假 WebSocket、假 <video>），
按秒驱动 syncTick，检查卡顿、落后、房间状态变化、房主基准线这些情况下的跳转 / 调速 / 按钮。

运行：python tests/test_player_sync.py
需要 node（没有就跳过并以 0 退出）。不联网，不需要 Discord。
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAYER_JS = os.path.join(ROOT, "server", "watch_web", "player.js")

HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");
const src = fs.readFileSync(process.argv[2], "utf8");

let nowMs = 100000;                 // 假的 performance.now()
const SERVER_T0 = 5000;             // 服务器墙钟（秒），和本地的差固定
const results = [];
function check(name, cond) {
  results.push(!!cond);
  console.log((cond ? "PASS " : "FAIL ") + name);
}
const near = (a, b, eps = 0.3) => Math.abs(a - b) <= eps;

// ---- 假 DOM
function makeElement(tag) {
  const listeners = {};
  const el = {
    tagName: tag, hidden: false, disabled: false, textContent: "", title: "", value: "0", className: "",
    style: { setProperty() {} }, dataset: {}, children: [],
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener() {},
    fire(type, ev) { for (const fn of listeners[type] || []) fn(ev || { preventDefault() {} }); },
    appendChild(c) { el.children.push(c); if (tag === "head" && typeof c.onload === "function") c.onload(); return c; },
    append(...cs) { el.children.push(...cs); },
    remove() {}, setAttribute() {}, removeAttribute() {}, getAttribute() { return null; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    focus() {}, select() {}, replaceChildren() { el.children = []; },
  };
  return el;
}

class FakeVideo {
  constructor() {
    this._t = 0; this.seeks = []; this.paused = true; this.playbackRate = 1; this.readyState = 4;
    this.seeking = false; this.duration = NaN; this.volume = 1; this.muted = false;
    this.style = { setProperty() {} }; this.error = null;
  }
  get currentTime() { return this._t; }
  set currentTime(v) { this._t = v; this.seeks.push(v); }
  canPlayType() { return ""; }
  addEventListener() {} removeEventListener() {}
  play() { this.paused = false; return Promise.resolve(); }
  pause() { this.paused = true; }
  setAttribute() {} removeAttribute() {} load() {} remove() {}
}

const elements = {};
let video = null;
const document = {
  head: makeElement("head"), title: "", visibilityState: "visible",
  getElementById(id) { return elements[id] ||= makeElement("div"); },
  createElement(tag) {
    if (tag === "video") return (video = new FakeVideo());
    return makeElement(tag);
  },
  addEventListener() {},
};

class Hls { on() {} loadSource() {} attachMedia() {} destroy() {} static isSupported() { return true; } }
Hls.Events = { ERROR: "hlsError" };
Hls.version = "test";

class FakeWS {
  constructor(url) { this.url = url; this.readyState = 1; this.sent = []; this.listeners = {}; FakeWS.last = this; }
  addEventListener(t, fn) { (this.listeners[t] ||= []).push(fn); }
  send(s) { this.sent.push(JSON.parse(s)); }
  fire(t, ev) { for (const fn of this.listeners[t] || []) fn(ev); }
  receive(msg) { this.fire("message", { data: JSON.stringify(msg) }); }
}
FakeWS.OPEN = 1;

const intervals = [];
const sandbox = {
  console, document, Hls, URL, URLSearchParams, WebSocket: FakeWS,
  location: { pathname: "/r/room1", search: "", protocol: "https:", host: "watch.test",
              origin: "https://watch.test", href: "https://watch.test/r/room1" },
  navigator: { userAgent: "node-test" }, history: { replaceState() {} },
  localStorage: { getItem() { return null; }, setItem() {} },
  performance: { now: () => nowMs },
  setInterval(fn, ms) { intervals.push({ fn, ms }); return intervals.length; },
  setTimeout() { return 1; }, clearInterval() {}, clearTimeout() {},
  confirm() { return true; }, alert() {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

// ---- 一场观看：建页面、连上、拿时钟、收 state、点开始
async function session({ host, position = 193, playing = true }) {
  for (const k of Object.keys(elements)) delete elements[k];
  intervals.length = 0;
  vm.createContext(sandbox);
  vm.runInContext(src, sandbox, { filename: "player.js" });
  const ws = FakeWS.last;
  ws.fire("open");
  ws.receive({ type: "welcome", viewer_id: "v1", name: host ? "房主" : "观众", is_host: host, can_control: host });
  const ping = ws.sent.find((m) => m.type === "ping");
  ws.receive({ type: "pong", t: ping.t, server_time: serverNow() });   // rtt 0：offset 精确
  let anchor = { pos: position, at: nowMs };   // 最近一条 state 说的位置和发出时刻
  const state = (pos, extra = {}) => {
    anchor = { pos, at: nowMs };
    return {
      type: "state", playing, position: pos, server_time: serverNow(),
      video: { source: "vod", id: "x", title: "剧", duration: 2400, version: 1,
               hls: "/vod/room1/1/index.m3u8", live: false, line: 0, line_count: 1 },
      ...extra,
    };
  };
  ws.receive(state(position));
  await new Promise((r) => setImmediate(r));   // buildPlayer 里的 await loadScript
  await new Promise((r) => setImmediate(r));
  elements["start"].fire("click");
  const syncTick = intervals.find((i) => i.ms === 1000).fn;
  const v = video;
  return {
    ws, video: v, syncTick, state,
    roomPos: () => anchor.pos + (playing ? (nowMs - anchor.at) / 1000 : 0),
    // 过 dt 秒：时钟前进；没卡的话画面按倍速前进；然后 sync 一次
    tick(dt = 1) {
      nowMs += dt * 1000;
      if (v.readyState >= 3 && !v.paused) v._t += dt * v.playbackRate;
      syncTick();
    },
    controls: () => ws.sent.filter((m) => m.type === "control"),
    syncBtn: () => elements["sync-btn"],
  };
}
function serverNow() { return SERVER_T0 + (nowMs - 100000) / 1000; }

(async () => {
  // ---------- 观众：卡顿不追、恢复后不跳、按钮
  {
    const s = await session({ host: false });
    check("开始时跳到房间位置", near(s.video.currentTime, 193));
    const seeksAtStart = s.video.seeks.length;
    for (let i = 0; i < 3; i++) s.tick();
    check("正常播放时不跳", s.video.seeks.length === seeksAtStart && s.video.playbackRate === 1);
    check("正常播放时没有同步按钮", s.syncBtn().hidden === true);

    s.video.readyState = 2;                        // 开始缓冲：画面停住
    for (let i = 0; i < 7; i++) s.tick();
    check("缓冲 7 秒期间不跳", s.video.seeks.length === seeksAtStart);
    check("缓冲期间不调速", s.video.playbackRate === 1);
    check("缓冲中落后超过 2 秒就有按钮", s.syncBtn().hidden === false && /落后 7 秒/.test(s.syncBtn().textContent));

    s.video.readyState = 4;                        // 缓冲完了，落后 7 秒
    s.tick();
    check("缓冲完落后 7 秒不自动跳", s.video.seeks.length === seeksAtStart);
    check("落后时稍微加速追", s.video.playbackRate === 1.05);
    check("按钮显示落后秒数", /落后 7 秒 · 同步/.test(s.syncBtn().textContent) && s.syncBtn().hidden === false);
    check("观众不发 control", s.controls().length === 0);

    s.syncBtn().fire("click");
    check("点同步才跳到房间位置", s.video.seeks.length === seeksAtStart + 1 && near(s.video.currentTime, s.roomPos()));
    check("跳完按钮消失", s.syncBtn().hidden === true);
    s.tick();
    check("跳完回到 1x", s.video.playbackRate === 1);

    // 房间状态变了（房主拖动）：立刻跟
    s.ws.receive(s.state(900));
    check("房主拖动后立刻跳过去", near(s.video.currentTime, s.roomPos()) && near(s.video.currentTime, 900));
    // 缓冲中房间又变了：也立刻跟，旧位置的下载没用了
    s.video.readyState = 2;
    s.tick();
    s.ws.receive(s.state(1200));
    check("缓冲中房间变了也立刻跳", near(s.video.currentTime, 1200));
    s.video.readyState = 4;

    // 超前（比如 hls.js 跳过了缓冲空洞）：不退回，放慢一点，出按钮
    s.tick();
    const before = s.video.seeks.length;
    s.video._t += 5;
    s.tick();
    check("超前 5 秒不自动退回", s.video.seeks.length === before);
    check("超前时稍微放慢", s.video.playbackRate === 0.95);
    check("超前也有按钮", s.syncBtn().hidden === false && /超前 5 秒 · 同步/.test(s.syncBtn().textContent));
    s.syncBtn().fire("click");
    check("点同步退回房间位置", s.video.seeks.length === before + 1 && near(s.video.currentTime, s.roomPos()));

    // 房主卡顿后把房间拉回去（state 带 auto）：观众不跳，只放慢
    s.tick();
    const before2 = s.video.seeks.length;
    s.ws.receive(s.state(s.video.currentTime - 7, { auto: true }));
    s.tick();
    check("房主自动拉回时观众不跳", s.video.seeks.length === before2);
    check("房主自动拉回后观众放慢追", s.video.playbackRate === 0.95);
    check("并显示超前按钮", s.syncBtn().hidden === false && /超前 [78] 秒/.test(s.syncBtn().textContent));
    // 接着房主亲手拖动（不带 auto）：观众立刻跟
    s.ws.receive(s.state(2000));
    check("房主亲手拖动观众照样跟", near(s.video.currentTime, 2000));

    // 服务器拒绝 control 时发回的 state 和当前一样：不该把落后的人拽走
    s.video.readyState = 2;
    for (let i = 0; i < 4; i++) s.tick();
    s.video.readyState = 4;
    s.tick();
    check("落后 4 秒出现按钮", s.syncBtn().hidden === false && /落后 [45] 秒/.test(s.syncBtn().textContent));
  }

  // ---------- 房主：自己卡了以后把房间拉到自己这里
  {
    const s = await session({ host: true });
    for (let i = 0; i < 2; i++) s.tick();
    const seeks0 = s.video.seeks.length;
    s.video.readyState = 2;
    for (let i = 0; i < 7; i++) s.tick();
    check("房主缓冲期间不跳、不发 control", s.video.seeks.length === seeks0 && s.controls().length === 0);
    check("房主从不显示同步按钮（缓冲中）", s.syncBtn().hidden === true);
    s.video.readyState = 4;
    s.tick();
    const ctl = s.controls();
    check("房主缓冲完不跳", s.video.seeks.length === seeks0);
    check("房主发 seek 把房间拉到自己的位置",
          ctl.length === 1 && ctl[0].action === "seek" && near(ctl[0].position, s.video.currentTime)
          && near(ctl[0].position, s.roomPos() - 7, 0.5));
    check("拉回的 seek 带 auto 标记", ctl[0].auto === true);
    // 服务器广播回来的 auto state：房主自己也不会被它弄跳
    const seeksBeforeEcho = s.video.seeks.length;
    s.ws.receive(s.state(s.video.currentTime, { auto: true }));   // onState 自己会 sync 一次
    check("auto state 回来房主不跳", s.video.seeks.length === seeksBeforeEcho && s.controls().length === 1);
    check("房主从不显示同步按钮（恢复后）", s.syncBtn().hidden === true);
    s.tick();
    check("拉回之后房主和房间一致，不再发", s.controls().length === 1 && s.video.playbackRate === 1);

    // 3 秒内再次落后不重复发；过了间隔才发
    s.video._t -= 5;
    s.tick();
    check("3 秒内不重复拉", s.controls().length === 1);
    s.tick();
    check("过了间隔再拉一次", s.controls().length === 2 && near(s.controls()[1].position, s.video.currentTime));
    s.tick();
    check("拉完一致，不再发", s.controls().length === 2);

    // 别人拖动（state）：房主也立刻跟过去，不把房间拉回来
    const n = s.controls().length;
    s.ws.receive(s.state(1500));
    s.tick();
    check("别人拖动时房主跟着跳", near(s.video.currentTime, s.roomPos()) && near(s.video.currentTime, 1501));
    check("跟着跳不算自己落后，不发 control", s.controls().length === n);
  }

  // ---------- 暂停的房间：照旧按位置对齐
  {
    const s = await session({ host: false, position: 100, playing: false });
    check("暂停时停在房间位置", s.video.paused && near(s.video.currentTime, 100));
    s.video._t = 103;
    s.tick();
    check("暂停时偏了就重定位", near(s.video.currentTime, 100));
    check("暂停时没有同步按钮", s.syncBtn().hidden === true);
  }

  const passed = results.filter(Boolean).length;
  console.log(`\n${passed}/${results.length} passed`);
  process.exit(passed === results.length ? 0 : 1);
})().catch((e) => { console.error(e); process.exit(2); });
"""


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("SKIP: 没有 node，跳过 player.js 同步测试")
        return 0
    with tempfile.TemporaryDirectory() as d:
        harness = os.path.join(d, "harness.js")
        with open(harness, "w", encoding="utf-8") as f:
            f.write(HARNESS)
        proc = subprocess.run([node, harness, PLAYER_JS], capture_output=True, text=True, encoding="utf-8")
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
