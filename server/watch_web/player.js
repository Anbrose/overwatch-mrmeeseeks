// Watch-together synced player. Plain ES2020, no build step.
//
// Echo-loop rule: `control` messages are sent ONLY from the user's own clicks on
// our controls (play button, seek bar). Nothing listens to player events
// (play/pause/seeked/onStateChange) to send anything, so state the server pushes
// to us can never bounce back. The native YouTube controls are off, the <video>
// has no `controls`, and a transparent shield covers the video area.
"use strict";

(() => {
  const DASH_SRC = "https://cdnjs.cloudflare.com/ajax/libs/dashjs/4.7.4/dash.all.min.js";
  const YT_SRC = "https://www.youtube.com/iframe_api";
  const VIEWER_KEY = "watch_viewer_id";
  const PING_MS = 10000;
  const SYNC_MS = 1000;
  const RECONNECT_MS = 2000;
  const HARD_DRIFT = 2;      // seconds: jump instead of nudging
  const SOFT_DRIFT = 0.3;    // seconds: start nudging playbackRate
  const CAUGHT_UP = 0.1;     // seconds: nudging done, back to 1x
  const PAUSED_DRIFT = 0.25; // seconds: while paused, re-seek beyond this
  const RATE_STEPS = { bilibili: [0.95, 1.05], youtube: [0.75, 1.25] };

  const $ = (id) => document.getElementById(id);
  const ui = {
    title: $("title"), conn: $("conn"), permBtn: $("perm-btn"),
    stageWrap: $("stage-wrap"), host: $("player-host"), stageMsg: $("stage-msg"), start: $("start"),
    play: $("play"), seek: $("seek"), time: $("time"), mute: $("mute"), volume: $("volume"), fs: $("fs"),
    me: $("me"), panel: $("perm-panel"), members: $("members"),
    changeForm: $("change-form"), changeUrl: $("change-url"), toast: $("toast"),
  };

  // ---------------------------------------------------------------- storage

  function loadViewerId() {
    try { return localStorage.getItem(VIEWER_KEY) || null; } catch { return null; }
  }
  function saveViewerId(id) {
    try { localStorage.setItem(VIEWER_KEY, id); } catch { /* private mode etc. */ }
  }

  // ---------------------------------------------------------------- app state

  const roomId = location.pathname.split("/").filter(Boolean).pop() || "";
  let hostKey = new URLSearchParams(location.search).get("k");
  let viewerId = loadViewerId();

  let ws = null;
  let connected = false;
  let ended = false;
  let pingTimer = null;
  let reconnectTimer = null;

  let me = { name: "", isHost: false, canControl: false };
  let room = null;            // last `state` message
  let started = false;        // user clicked 点击开始观看
  let player = null;          // current player adapter
  let playerVersion = null;   // state.video.version the player was built for
  let buildToken = 0;         // invalidates stale async builds
  let durationSentFor = null; // version we reported a YouTube duration for
  let seeking = false;        // user is dragging the seek bar
  let volume = 1;
  let muted = false;

  // ---------------------------------------------------------------- clock sync

  const clockSamples = []; // {rtt, offset} in seconds, newest last
  // Before the first pong, assume the server's wall clock matches ours.
  let clockOffset = Date.now() / 1000 - performance.now() / 1000;

  function localNow() { return performance.now() / 1000; }
  function serverNow() { return localNow() + clockOffset; }

  function addClockSample(sentMs, serverTime) {
    const now = localNow();
    const rtt = now - sentMs / 1000;
    if (!(rtt >= 0)) return;
    clockSamples.push({ rtt, offset: serverTime + rtt / 2 - now });
    while (clockSamples.length > 5) clockSamples.shift();
    clockOffset = bestClockSample(clockSamples).offset;
  }

  function bestClockSample(samples) {
    return samples.reduce((best, s) => (s.rtt < best.rtt ? s : best));
  }

  // ---------------------------------------------------------------- sync math

  function expectedPosition(state) {
    const elapsed = state.playing ? serverNow() - state.server_time : 0;
    let pos = state.position + Math.max(0, elapsed);
    const dur = state.video && state.video.duration;
    if (dur && pos > dur) pos = dur;
    return Math.max(0, pos);
  }

  // drift > 0: we are behind the room; drift < 0: we are ahead.
  function chooseRate(drift, currentRate, steps) {
    const [slow, fast] = steps;
    const abs = Math.abs(drift);
    if (abs >= SOFT_DRIFT) return drift > 0 ? fast : slow;
    if (currentRate === 1) return 1;
    const overshot = (currentRate > 1 && drift <= 0) || (currentRate < 1 && drift >= 0);
    return abs < CAUGHT_UP || overshot ? 1 : currentRate;
  }

  function syncTick() {
    if (!started || !room || !player || !player.ready || ended) return;
    const target = expectedPosition(room);
    const dur = currentDuration();
    // Calling play() on an ended video restarts it from 0; don't.
    const atEnd = dur > 0 && target >= dur - 0.5;

    if (room.playing && player.isPaused() && !atEnd) player.play();
    if (!room.playing && !player.isPaused()) player.pause();

    const drift = target - player.getTime();
    if (!room.playing) {
      if (Math.abs(drift) > PAUSED_DRIFT) player.seek(target);
      player.setRate(1);
      return;
    }
    if (atEnd) { player.setRate(1); return; }
    if (Math.abs(drift) > HARD_DRIFT) {
      player.seek(target);
      player.setRate(1);
      return;
    }
    player.setRate(chooseRate(drift, player.getRate(), RATE_STEPS[player.source]));
  }

  // ---------------------------------------------------------------- script loading

  const scriptPromises = {};
  function loadScript(src) {
    if (!scriptPromises[src]) {
      scriptPromises[src] = new Promise((resolve, reject) => {
        const el = document.createElement("script");
        el.src = src;
        el.async = true;
        el.onload = resolve;
        el.onerror = () => { delete scriptPromises[src]; el.remove(); reject(new Error("load failed: " + src)); };
        document.head.appendChild(el);
      });
    }
    return scriptPromises[src];
  }

  let ytApiPromise = null;
  function loadYouTubeApi() {
    if (window.YT && window.YT.Player) return Promise.resolve();
    if (!ytApiPromise) {
      ytApiPromise = new Promise((resolve, reject) => {
        const prev = window.onYouTubeIframeAPIReady;
        window.onYouTubeIframeAPIReady = () => { if (prev) prev(); resolve(); };
        loadScript(YT_SRC).catch((e) => { ytApiPromise = null; reject(e); });
      });
    }
    return ytApiPromise;
  }

  // ---------------------------------------------------------------- player adapters
  // Common interface: source, ready, getTime(), getDuration(), isPaused(), play(),
  // pause(), seek(t), getRate(), setRate(r), setVolume(v 0..1, muted), destroy().

  function createBiliPlayer(mpdUrl) {
    const video = document.createElement("video");
    video.playsInline = true;
    video.setAttribute("playsinline", "");
    video.preload = "auto";
    ui.host.appendChild(video);
    const dash = window.dashjs.MediaPlayer().create();
    dash.initialize(video, mpdUrl, false);

    return {
      source: "bilibili",
      ready: true,
      video,
      getTime: () => video.currentTime || 0,
      getDuration: () => (Number.isFinite(video.duration) ? video.duration : 0),
      isPaused: () => video.paused,
      play: () => {
        const p = video.play();
        if (p && p.catch) {
          p.catch((err) => { if (err && err.name === "NotAllowedError") showStartOverlay(); });
        }
      },
      pause: () => video.pause(),
      seek: (t) => { video.currentTime = t; },
      getRate: () => video.playbackRate,
      setRate: (r) => { if (video.playbackRate !== r) video.playbackRate = r; },
      setVolume: (v, m) => { video.volume = v; video.muted = m; },
      destroy: () => {
        try { dash.reset(); } catch { /* ignore */ }
        video.removeAttribute("src");
        video.remove();
      },
    };
  }

  function createYouTubePlayer(videoId, version, onReady) {
    const mount = document.createElement("div");
    ui.host.appendChild(mount);
    const adapter = {
      source: "youtube",
      ready: false,
      yt: null,
      getTime: () => (adapter.ready ? adapter.yt.getCurrentTime() || 0 : 0),
      getDuration: () => (adapter.ready ? adapter.yt.getDuration() || 0 : 0),
      // -1 unstarted, 0 ended, 1 playing, 2 paused, 3 buffering, 5 cued
      isPaused: () => { const s = adapter.yt.getPlayerState(); return s !== 1 && s !== 3; },
      play: () => adapter.yt.playVideo(),
      pause: () => adapter.yt.pauseVideo(),
      seek: (t) => adapter.yt.seekTo(t, true),
      getRate: () => adapter.yt.getPlaybackRate(),
      setRate: (r) => { if (adapter.yt.getPlaybackRate() !== r) adapter.yt.setPlaybackRate(r); },
      setVolume: (v, m) => {
        if (!adapter.ready) return;
        adapter.yt.setVolume(Math.round(v * 100));
        if (m) adapter.yt.mute(); else adapter.yt.unMute();
      },
      destroy: () => {
        try { if (adapter.yt) adapter.yt.destroy(); } catch { /* ignore */ }
        mount.remove();
        ui.host.textContent = "";
      },
    };
    adapter.yt = new window.YT.Player(mount, {
      videoId,
      width: "100%",
      height: "100%",
      playerVars: { controls: 0, disablekb: 1, playsinline: 1, rel: 0, fs: 0, iv_load_policy: 3, modestbranding: 1 },
      events: {
        onReady: () => { adapter.ready = true; onReady(adapter); maybeReportDuration(adapter, version); },
        onStateChange: () => maybeReportDuration(adapter, version),
      },
    });
    return adapter;
  }

  function maybeReportDuration(adapter, version) {
    if (!room || room.video.version !== version || room.video.duration != null) return;
    if (durationSentFor === version) return;
    const value = adapter.getDuration();
    if (!(value > 0)) return;
    durationSentFor = version;
    send({ type: "duration", value });
  }

  function destroyPlayer() {
    buildToken++;
    if (player) player.destroy();
    player = null;
    playerVersion = null;
    ui.host.textContent = "";
  }

  async function buildPlayer(video) {
    destroyPlayer();
    const token = buildToken;
    playerVersion = video.version;
    setStageMessage("加载中…");
    try {
      if (video.source === "bilibili") {
        await loadScript(DASH_SRC);
        if (token !== buildToken) return;
        player = createBiliPlayer(video.mpd);
        onPlayerReady(player);
      } else if (video.source === "youtube") {
        await loadYouTubeApi();
        if (token !== buildToken) return;
        player = createYouTubePlayer(video.id, video.version, (p) => {
          if (token === buildToken) onPlayerReady(p);
        });
      } else {
        setStageMessage("不支持的视频来源");
      }
    } catch (err) {
      if (token !== buildToken) return;
      playerVersion = null; // allow a retry on the next state
      setStageMessage("播放器加载失败");
      toast("播放器加载失败，刷新页面试试");
    }
  }

  function onPlayerReady(p) {
    p.setVolume(volume, muted);
    setStageMessage("");
    syncTick();
  }

  // ---------------------------------------------------------------- UI

  function setStageMessage(text) {
    ui.stageMsg.textContent = text;
    ui.stageMsg.hidden = !text;
  }

  function showStartOverlay() {
    started = false;
    ui.start.hidden = ended;
  }

  function formatTime(sec) {
    if (!Number.isFinite(sec) || sec < 0) sec = 0;
    sec = Math.floor(sec);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = String(sec % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
  }

  function currentDuration() {
    const d = room && room.video && room.video.duration;
    if (d) return d;
    return player && player.ready ? player.getDuration() : 0;
  }

  function currentPosition() {
    if (player && player.ready && started) return player.getTime();
    return room ? expectedPosition(room) : 0;
  }

  function setRangeFill(input, fraction) {
    input.style.setProperty("--pct", `${Math.min(100, Math.max(0, fraction * 100))}%`);
  }

  function renderProgress() {
    const dur = currentDuration();
    if (seeking) {
      const pos = (ui.seek.value / 1000) * dur;
      ui.time.textContent = `${formatTime(pos)} / ${formatTime(dur)}`;
      setRangeFill(ui.seek, ui.seek.value / 1000);
      return;
    }
    const pos = currentPosition();
    const frac = dur ? pos / dur : 0;
    ui.seek.value = String(Math.round(frac * 1000));
    setRangeFill(ui.seek, frac);
    ui.time.textContent = `${formatTime(pos)} / ${formatTime(dur)}`;
  }

  function renderControls() {
    const can = me.canControl && !ended && !!room;
    ui.play.disabled = !can;
    ui.seek.disabled = !can;
    const playing = room && room.playing;
    ui.play.textContent = playing ? "⏸" : "▶";
    ui.play.setAttribute("aria-label", playing ? "暂停" : "播放");
    ui.me.textContent = me.name ? `你是「${me.name}」` : "";
    ui.permBtn.hidden = !me.isHost || ended;
    if (!me.isHost || ended) {
      ui.panel.hidden = true;
      ui.permBtn.setAttribute("aria-expanded", "false");
    }
    ui.conn.hidden = connected || ended;
  }

  function renderMembers(members) {
    ui.members.textContent = "";
    if (!members.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "还没有其他人";
      ui.members.appendChild(li);
      return;
    }
    for (const m of members) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = m.name;
      const sw = document.createElement("button");
      sw.type = "button";
      sw.className = "switch";
      sw.setAttribute("role", "switch");
      sw.setAttribute("aria-checked", String(!!m.can_control));
      sw.setAttribute("aria-label", `${m.name} 控制播放`);
      sw.textContent = m.can_control ? "● 开" : "○ 关";
      sw.addEventListener("click", () => {
        send({ type: "grant", target: m.viewer_id, allowed: !m.can_control });
      });
      li.append(name, sw);
      ui.members.appendChild(li);
    }
  }

  let toastTimer = null;
  function toast(text) {
    ui.toast.textContent = text;
    ui.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { ui.toast.hidden = true; }, 3500);
  }

  function showEnded() {
    ended = true;
    destroyPlayer();
    ui.start.hidden = true;
    setStageMessage("房间已结束");
    renderControls();
  }

  // ---------------------------------------------------------------- user actions
  // These are the ONLY places that send `control`.

  function sendControl(action, position) {
    if (!me.canControl || !room) return;
    send({ type: "control", action, position });
    // Apply optimistically; the server's broadcast `state` will confirm or correct.
    room = { ...room, playing: action === "play" ? true : action === "pause" ? false : room.playing,
             position, server_time: serverNow() };
    renderControls();
    syncTick();
  }

  ui.play.addEventListener("click", () => {
    if (!room) return;
    const pos = currentPosition();
    sendControl(room.playing ? "pause" : "play", pos);
  });

  ui.seek.addEventListener("input", () => { seeking = true; renderProgress(); });
  ui.seek.addEventListener("change", () => {
    seeking = false;
    const dur = currentDuration();
    if (dur) sendControl("seek", (ui.seek.value / 1000) * dur);
    renderProgress();
  });

  function applyVolume() {
    ui.volume.value = String(Math.round(volume * 100));
    setRangeFill(ui.volume, muted ? 0 : volume);
    ui.mute.textContent = muted || volume === 0 ? "🔇" : "🔊";
    if (player && player.ready) player.setVolume(volume, muted);
  }
  ui.volume.addEventListener("input", () => {
    volume = ui.volume.value / 100;
    muted = volume === 0;
    applyVolume();
  });
  ui.mute.addEventListener("click", () => {
    muted = !muted;
    if (!muted && volume === 0) volume = 1;
    applyVolume();
  });

  ui.fs.addEventListener("click", () => {
    const el = ui.stageWrap;
    const fsEl = document.fullscreenElement || document.webkitFullscreenElement;
    if (fsEl) {
      (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    } else if (el.requestFullscreen) {
      el.requestFullscreen().catch(() => {});
    } else if (el.webkitRequestFullscreen) {
      el.webkitRequestFullscreen();
    } else if (player && player.video && player.video.webkitEnterFullscreen) {
      player.video.webkitEnterFullscreen(); // iOS Safari
    }
  });

  ui.start.addEventListener("click", () => {
    started = true;
    ui.start.hidden = true;
    // Call play inside the click handler so the browser counts it as a gesture.
    if (player && player.ready && room) {
      player.seek(expectedPosition(room));
      if (room.playing) player.play();
    }
    syncTick();
  });

  ui.permBtn.addEventListener("click", () => {
    const open = ui.panel.hidden;
    ui.panel.hidden = !open;
    ui.permBtn.setAttribute("aria-expanded", String(open));
  });

  ui.changeForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const url = ui.changeUrl.value.trim();
    if (!url) return;
    send({ type: "change_video", url });
    ui.changeUrl.value = "";
  });

  // ---------------------------------------------------------------- WebSocket

  function send(msg) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
  }

  function connect() {
    if (ended) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    try {
      ws = new WebSocket(`${proto}//${location.host}/ws/${roomId}`);
    } catch {
      scheduleReconnect();
      return;
    }
    ws.addEventListener("open", () => {
      send({ type: "hello", viewer_id: viewerId || null, key: hostKey || null });
    });
    ws.addEventListener("message", (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg && typeof msg.type === "string") handleMessage(msg);
    });
    ws.addEventListener("close", onSocketClosed);
  }

  function onSocketClosed() {
    connected = false;
    clearInterval(pingTimer);
    pingTimer = null;
    ws = null;
    renderControls();
    scheduleReconnect();
  }

  function scheduleReconnect() {
    if (ended || reconnectTimer) return;
    reconnectTimer = setTimeout(() => { reconnectTimer = null; connect(); }, RECONNECT_MS);
  }

  function sendPing() { send({ type: "ping", t: performance.now() }); }

  function handleMessage(msg) {
    switch (msg.type) {
      case "welcome": {
        connected = true;
        viewerId = msg.viewer_id;
        saveViewerId(viewerId);
        me = { name: msg.name, isHost: !!msg.is_host, canControl: !!msg.can_control };
        if (hostKey && location.search) {
          const url = new URL(location.href);
          url.searchParams.delete("k");
          history.replaceState(null, "", url.pathname + url.search + url.hash);
        }
        sendPing();
        clearInterval(pingTimer);
        pingTimer = setInterval(sendPing, PING_MS);
        renderControls();
        break;
      }
      case "pong":
        addClockSample(msg.t, msg.server_time);
        break;
      case "state":
        onState(msg);
        break;
      case "you":
        me = { ...me, isHost: !!msg.is_host, canControl: !!msg.can_control };
        renderControls();
        break;
      case "members":
        renderMembers(Array.isArray(msg.members) ? msg.members : []);
        break;
      case "error":
        toast(String(msg.message || "出错了"));
        break;
      case "ended":
        showEnded();
        break;
    }
  }

  function onState(msg) {
    if (ended || !msg.video) return;
    const firstState = room === null;
    room = msg;
    const title = msg.video.title || "";
    ui.title.textContent = title;
    document.title = title ? `${title} · 一起看` : "一起看";
    if (msg.video.version !== playerVersion) buildPlayer(msg.video);
    if (firstState && !started) ui.start.hidden = false;
    renderControls();
    syncTick();
  }

  // ---------------------------------------------------------------- boot

  applyVolume();
  renderControls();
  setInterval(syncTick, SYNC_MS);
  setInterval(renderProgress, 250);
  connect();
})();
