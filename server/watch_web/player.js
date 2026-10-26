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
  const HLS_SRC = "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.20/hls.min.js";
  const HLS_MIME = "application/vnd.apple.mpegurl";
  const PING_MS = 10000;
  const SYNC_MS = 1000;
  const RECONNECT_MS = 2000;
  const HARD_DRIFT = 2;      // seconds: jump instead of nudging
  const SOFT_DRIFT = 0.3;    // seconds: start nudging playbackRate
  const CAUGHT_UP = 0.1;     // seconds: nudging done, back to 1x
  const PAUSED_DRIFT = 0.25; // seconds: while paused, re-seek beyond this
  const RATE_STEPS = { bilibili: [0.95, 1.05], youtube: [0.75, 1.25], vod: [0.95, 1.05] };
  const UNPLAYABLE_MSG = "这个视频暂时无法播放";
  const VOD_FAILED_MSG = "视频加载失败";
  const PREPARING_MSG = "正在准备…";
  const PREPARING_TIMEOUT_MS = 50000;   // outlasts the server's 45 s VOD_PREPARE_TIMEOUT
  const RESUME_NEAR = 30;    // seconds: resume line hidden this close to the snapshot

  const $ = (id) => document.getElementById(id);
  const ui = {
    title: $("title"), conn: $("conn"), permBtn: $("perm-btn"),
    stageWrap: $("stage-wrap"), host: $("player-host"), stageMsg: $("stage-msg"), start: $("start"),
    play: $("play"), seek: $("seek"), time: $("time"), mute: $("mute"), volume: $("volume"), fs: $("fs"),
    me: $("me"), panel: $("perm-panel"), members: $("members"),
    changeForm: $("change-form"), changeUrl: $("change-url"), toast: $("toast"),
    resume: $("resume"), vodPanel: $("vod-panel"), episodes: $("episodes"),
    lineLabel: $("line-label"), nextLine: $("next-line"),
  };

  // ---------------------------------------------------------------- storage

  // One viewer_id per room: it is a credential for that room only.
  function loadViewerId() {
    try { return localStorage.getItem(VIEWER_KEY) || null; } catch { return null; }
  }
  function saveViewerId(id) {
    try { localStorage.setItem(VIEWER_KEY, id); } catch { /* private mode etc. */ }
  }

  // ---------------------------------------------------------------- app state

  const roomId = location.pathname.split("/").filter(Boolean).pop() || "";
  const VIEWER_KEY = `watch_viewer_id:${roomId}`;
  let hostKey = new URLSearchParams(location.search).get("k");
  let viewerId = loadViewerId();

  let ws = null;
  let connected = false;
  let ended = false;
  let pingTimer = null;
  let reconnectTimer = null;

  let me = { name: "", isHost: false, canControl: false };
  let room = null;            // state we sync to (may be an optimistic local edit)
  let serverState = null;     // last `state` the server actually sent
  let started = false;        // user clicked 点击开始观看
  let player = null;          // current player adapter
  let playerVersion = null;   // state.video.version the player was built for
  let buildToken = 0;         // invalidates stale async builds
  let durationSentFor = null; // version we reported a YouTube duration for
  let seeking = false;        // user is dragging the seek bar
  let volume = 1;
  let muted = false;
  let stageText = "";         // stage message underneath the 正在准备… indicator
  let preparing = false;      // host asked for an episode/source switch, awaiting state or error
  let preparingTimer = null;
  let episodesKey = null;     // which show/episode count the episode grid was built for

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

  function clockReady() { return clockSamples.length > 0; }

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
      // A paused target doesn't depend on the clock, so no pong is needed.
      if (Math.abs(drift) > PAUSED_DRIFT) player.seek(target, { keepPaused: true });
      player.setRate(1);
      return;
    }
    // While playing, the target depends on clock offset: wait for a real sample.
    if (atEnd || !clockReady()) { player.setRate(1); return; }
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

    const adapter = {
      source: "bilibili",
      ready: true,
      failed: false,
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
        try { dash.off(window.dashjs.MediaPlayer.events.ERROR, onError); } catch { /* ignore */ }
        video.removeEventListener("error", onVideoError);
        try { dash.reset(); } catch { /* ignore */ }
        video.removeAttribute("src");
        video.remove();
      },
    };
    // MPD 502 (B站 refused the stream) or a broken media segment: tell the
    // viewer instead of leaving a black stage, and stop syncing this player.
    function onError(e) {
      if (player !== adapter || adapter.failed) return;
      adapter.failed = true;
      adapter.ready = false;
      failPlayer(UNPLAYABLE_MSG, describeDashError(e) || describeMediaError(video.error), mpdUrl, false, "dash.js");
    }
    const onVideoError = () => onError(null);
    dash.on(window.dashjs.MediaPlayer.events.ERROR, onError);
    video.addEventListener("error", onVideoError);
    dash.initialize(video, mpdUrl, false);
    return adapter;
  }

  // Searched shows: an HLS playlist served (rewritten) by our server. Native HLS
  // where the browser has it (Safari/iOS), hls.js elsewhere. Failures are local
  // only: we show a message and never tell the server.
  function createVodPlayer(hlsUrl) {
    const video = document.createElement("video");
    video.playsInline = true;
    video.setAttribute("playsinline", "");
    video.preload = "auto";
    ui.host.appendChild(video);
    const useHls = !!(window.Hls && window.Hls.isSupported());
    const native = !useHls && !!video.canPlayType(HLS_MIME);
    const mode = useHls ? `hls.js ${window.Hls.version || "?"}` : native ? "native HLS" : "no HLS support";
    let hls = null;
    let lastHlsError = "";   // last non-fatal hls.js error: context when the <video> errors first

    const adapter = {
      source: "vod",
      ready: true,
      failed: false,
      failMsg: VOD_FAILED_MSG,
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
        video.removeEventListener("error", onVideoError);
        if (hls) { try { hls.destroy(); } catch { /* ignore */ } }
        video.removeAttribute("src");
        try { video.load(); } catch { /* ignore */ }  // stop native HLS downloading
        video.remove();
      },
    };
    // `reason` says which step failed and why (see describeHlsError); it goes on
    // the stage and to the server log so the failure can be triaged without the
    // viewer's console.
    function onError(reason, data) {
      if (player !== adapter || adapter.failed) return;
      adapter.failed = true;
      adapter.ready = false;
      failPlayer(VOD_FAILED_MSG, reason, hlsUrl, true, mode);
      // Our own playlist and key routes answer 4xx/5xx with the server-side
      // reason in the body (e.g. which source lines failed and why); hls.js
      // only keeps the status code, so fetch it once and add it to the stage.
      const code = data && data.response && data.response.code;
      const failedUrl = data ? errorUrl(data) : "";
      if (data && code >= 400 && failedUrl && sameOrigin(failedUrl)) {
        fetch(failedUrl, { cache: "no-store" }).then((r) => r.text()).then((t) => {
          t = (t || "").trim();
          if (player === adapter && t && t.length < 600 && !t.startsWith("#")) {
            setStageMessage(stageText + "\n服务器说：" + t);
          }
        }).catch(() => { /* already failed; nothing more to say */ });
      }
    }
    const onVideoError = () => onError(describeMediaError(video.error)
      + (lastHlsError ? `；hls.js 之前的错误：${lastHlsError}` : ""));
    video.addEventListener("error", onVideoError);
    if (useHls) {
      hls = new window.Hls();
      hls.on(window.Hls.Events.ERROR, (_event, data) => {
        if (!data) return;
        // Non-fatal errors are retried by hls.js; keep them in the console for triage.
        console.warn("[watch] hls.js error" + (data.fatal ? " (fatal)" : ""), data.details, data);
        if (data.fatal) onError(describeHlsError(data), data);
        else lastHlsError = describeHlsError(data);
      });
      hls.loadSource(hlsUrl);
      hls.attachMedia(video);
    } else if (native) {
      video.src = hlsUrl;
    } else {
      // No way to play HLS here; report once the caller has installed us.
      Promise.resolve().then(() => onError("这个浏览器不支持 HLS 播放"));
    }
    return adapter;
  }

  // ---- why a player failed, in words the viewer (and whoever reads the server
  // log) can act on: which step, the HTTP status, the host, then the raw code.

  // The URL whose request failed. For key errors that is the key's URL (from
  // the response, or the fragment's decrypt data), not the fragment's.
  function errorUrl(data) {
    const resp = data.response || {};
    const frag = data.frag || {};
    if (/key/i.test(String(data.details || ""))) {
      return resp.url || (frag.decryptdata && frag.decryptdata.uri) || data.url || "";
    }
    return frag.url || resp.url || data.url || "";
  }

  function sameOrigin(url) {
    try { return new URL(url, location.href).origin === location.origin; } catch { return false; }
  }

  function describeHlsError(data) {
    const details = String(data.details || data.type || "unknown");
    const resp = data.response || {};
    const code = typeof resp.code === "number" ? resp.code : null;
    const url = errorUrl(data);
    const step = /manifest|level|playlist/i.test(details) ? "播放列表"
      : /key/i.test(details) ? "密钥" : /frag/i.test(details) ? "片段" : details;
    let why = "";
    if (data.type === "networkError") {
      if (/timeout/i.test(details)) why = "超时";
      else if (code === null || code === 0) why = "请求被拦截或连不上（跨域 CORS、网络或广告拦截器）";
      else why = `HTTP ${code}`;
    } else if (data.type === "mediaError") {
      why = /parsing|demux|fragParsing/i.test(details) ? "内容不是有效的视频片段" : "无法解码";
    } else if (data.type === "muxError") {
      why = "转封装失败";
    } else if (data.error && data.error.message) {
      why = String(data.error.message);
    }
    return `${step} ${why}`.trim() + hostSuffix(url) + ` [${details}]`;
  }

  function describeDashError(e) {
    const err = e && e.error;
    if (!err) return "";
    if (typeof err === "string") return err;
    return [err.code, err.message].filter(Boolean).join(" ");
  }

  function describeMediaError(err) {
    if (!err) return "";
    const names = { 1: "加载被中止", 2: "网络错误", 3: "解码失败", 4: "格式不支持" };
    return (names[err.code] || `MediaError ${err.code}`) + (err.message ? `：${err.message}` : "");
  }

  function hostSuffix(url) {
    try { return url ? ` · ${new URL(url, location.href).host}` : ""; } catch { return ""; }
  }

  // Shows "<msg>：<reason>" on the stage, logs it, and sends it to the server
  // as `report` (with the player mode and browser) so it lands in the server
  // log with the room and video. Never changes room state; others are not told.
  function failPlayer(msg, reason, srcUrl, hostCanSwitch, mode) {
    const text = reason ? `${msg}：${reason}` : msg;
    console.error("[watch] " + text, srcUrl || "", mode || "");
    const hint = hostCanSwitch && me.isHost ? "\n房主可以在 🔑 面板里「换一个片源」" : "";
    setStageMessage(text + hint);
    renderStartButton();
    send({ type: "report", detail: text, url: String(srcUrl || "").slice(0, 300),
           env: `${mode || "?"}; ${navigator.userAgent}`.slice(0, 300) });
  }

  function createYouTubePlayer(videoId, version, onReady) {
    const mount = document.createElement("div");
    ui.host.appendChild(mount);
    // -1 unstarted, 0 ended, 1 playing, 2 paused, 3 buffering, 5 cued
    const notStarted = () => { const s = adapter.yt.getPlayerState(); return s === -1 || s === 5; };
    let cuedAt = null;      // startSeconds of the last cueVideoById
    let playCheck = null;   // timer that detects a silently refused playVideo()
    const adapter = {
      source: "youtube",
      ready: false,
      yt: null,
      getTime: () => {
        if (!adapter.ready) return 0;
        if (cuedAt !== null && notStarted()) return cuedAt;
        return adapter.yt.getCurrentTime() || 0;
      },
      getDuration: () => (adapter.ready ? adapter.yt.getDuration() || 0 : 0),
      isPaused: () => { const s = adapter.yt.getPlayerState(); return s !== 1 && s !== 3; },
      play: () => {
        adapter.yt.playVideo();
        // Outside a user gesture (e.g. iOS) playVideo() can be refused silently,
        // leaving the player unstarted/cued. Then ask for a tap again.
        if (playCheck) return;
        playCheck = setTimeout(() => {
          playCheck = null;
          if (player === adapter && room && room.playing && notStarted()) showStartOverlay();
        }, 2000);
      },
      pause: () => adapter.yt.pauseVideo(),
      seek: (t, opts) => {
        // seekTo on an unstarted/cued player starts playback; cue instead when
        // the room is paused.
        if (opts && opts.keepPaused && notStarted()) {
          cuedAt = t;
          adapter.yt.cueVideoById({ videoId, startSeconds: t });
        } else {
          cuedAt = null;
          adapter.yt.seekTo(t, true);
        }
      },
      getRate: () => adapter.yt.getPlaybackRate(),
      setRate: (r) => { if (adapter.yt.getPlaybackRate() !== r) adapter.yt.setPlaybackRate(r); },
      setVolume: (v, m) => {
        if (!adapter.ready) return;
        adapter.yt.setVolume(Math.round(v * 100));
        if (m) adapter.yt.mute(); else adapter.yt.unMute();
      },
      destroy: () => {
        clearTimeout(playCheck);
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
    // The server only accepts `duration` from the host or someone with control.
    if (!me.canControl || durationSentFor === version) return;
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
    renderStartButton();
    try {
      if (video.source === "bilibili") {
        await loadScript(DASH_SRC);
        if (token !== buildToken) return;
        player = createBiliPlayer(video.mpd);
        onPlayerReady(player);
      } else if (video.source === "vod") {
        // Prefer hls.js (MSE) wherever it works: browsers that claim native HLS
        // (Android Chrome, some TVs) often fail on real streams with a bare
        // "format not supported". Native is only for browsers without MSE
        // (iPhone Safari), or when the hls.js script cannot be loaded.
        try {
          await loadScript(HLS_SRC);
        } catch (err) {
          if (!document.createElement("video").canPlayType(HLS_MIME)) throw err;
        }
        if (token !== buildToken) return;
        player = createVodPlayer(video.hls);
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
    renderStartButton();
    syncTick();
  }

  // ---------------------------------------------------------------- UI

  function setStageMessage(text) {
    stageText = text;
    renderStageMessage();
  }

  function renderStageMessage() {
    const text = preparing ? PREPARING_MSG : stageText;
    ui.stageMsg.textContent = text;
    ui.stageMsg.hidden = !text;
  }

  // Shown on the stage from a vod switch request until ANY state or error
  // arrives (the server may drop a request silently, e.g. when auto-next won),
  // or until the timeout.
  function startPreparing() {
    preparing = true;
    clearTimeout(preparingTimer);
    preparingTimer = setTimeout(stopPreparing, PREPARING_TIMEOUT_MS);
    renderStageMessage();
    renderVodPanel();
  }

  function stopPreparing() {
    clearTimeout(preparingTimer);
    preparingTimer = null;
    if (!preparing) return;
    preparing = false;
    renderStageMessage();
    renderVodPanel();
  }

  function showStartOverlay() {
    started = false;
    ui.start.hidden = ended;
    renderStartButton();
  }

  // The start tap must reach a ready player so play() runs inside the gesture.
  function renderStartButton() {
    const ready = !!(player && player.ready);
    const failed = !!(player && player.failed);
    ui.start.disabled = !ready;
    ui.start.classList.toggle("failed", failed);
    ui.start.textContent = ready ? "点击开始观看" : failed ? player.failMsg || UNPLAYABLE_MSG : "加载中…";
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
    if (room && room.video && room.video.source === "vod") return 0; // only the server's value
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
    ui.me.textContent = me.name ? `ID：「${me.name}」` : "";
    ui.permBtn.hidden = !me.isHost || ended;
    if (!me.isHost || ended) {
      ui.panel.hidden = true;
      ui.permBtn.setAttribute("aria-expanded", "false");
    }
    ui.conn.hidden = connected || ended;
    renderResume();
    renderVodPanel();
  }

  function isVod() {
    return !!(room && room.video && room.video.source === "vod");
  }

  // "上次一起看到…": hidden when the room is already at the snapshot.
  function renderResume() {
    const r = room && room.resume;
    const atSnapshot = r && isVod() && room.video.episode === r.episode
      && Math.abs(expectedPosition(room) - r.position) < RESUME_NEAR;
    ui.resume.textContent = "";
    ui.resume.hidden = !r || ended || atSnapshot;
    if (ui.resume.hidden) return;
    const text = String(r.text || "");
    if (me.canControl) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "resume-btn";
      btn.textContent = text;
      btn.addEventListener("click", () => {
        send({ type: "resume" });
        if (room && room.video.episode !== r.episode) startPreparing();
      });
      ui.resume.appendChild(btn);
    } else {
      ui.resume.textContent = text;
    }
  }

  // Host-only episode list and source switch inside the 🔑 panel.
  function renderVodPanel() {
    const vod = me.isHost && !ended && isVod();
    ui.vodPanel.hidden = !vod;
    if (!vod) return;
    const v = room.video;
    const count = Number(v.episode_count) || 0;
    const key = `${v.id}\n${count}`;
    if (key !== episodesKey) {
      episodesKey = key;
      ui.episodes.textContent = "";
      for (let i = 0; i < count; i++) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "ep";
        btn.textContent = `第 ${i + 1} 集`;
        btn.addEventListener("click", () => {
          if (preparing) return;
          send({ type: "episode", index: i });
          startPreparing();
        });
        ui.episodes.appendChild(btn);
      }
    }
    Array.from(ui.episodes.children).forEach((btn, i) => {
      const current = i === v.episode;
      btn.classList.toggle("current", current);
      if (current) btn.setAttribute("aria-current", "true"); else btn.removeAttribute("aria-current");
      btn.disabled = preparing;
    });
    // Sources are numbered, never named: viewers should not see which site a stream comes from.
    ui.lineLabel.textContent = `来源${(Number(v.line) || 0) + 1}（共 ${v.line_count || 0} 个）`;
    ui.nextLine.disabled = preparing || (Number(v.line_count) || 0) <= 1;
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
        send({ type: "grant", target: m.id, allowed: !m.can_control });
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
    stopPreparing();
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
    // Apply optimistically; the server's broadcast `state` confirms it. A rejected
    // control gets a fresh `state` sent back to us only, which snaps us back.
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
    if (!player || !player.ready) return; // button is disabled until ready anyway
    started = true;
    ui.start.hidden = true;
    // Call play inside the click handler so the browser counts it as a gesture.
    if (room && room.playing) {
      if (clockReady()) player.seek(expectedPosition(room));
      player.play();
    } else if (room) {
      player.seek(expectedPosition(room), { keepPaused: true });
    }
    syncTick();
  });

  ui.permBtn.addEventListener("click", () => {
    const open = ui.panel.hidden;
    ui.panel.hidden = !open;
    ui.permBtn.setAttribute("aria-expanded", String(open));
  });

  ui.nextLine.addEventListener("click", () => {
    if (preparing) return;
    send({ type: "next_line" });
    startPreparing();
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
        stopPreparing();
        toast(String(msg.message || "出错了") + (msg.detail ? `（${msg.detail}）` : ""));
        revertOptimistic();
        break;
      case "ended":
        showEnded();
        break;
    }
  }

  function revertOptimistic() {
    if (!serverState || room === serverState) return;
    room = serverState;
    renderControls();
    syncTick();
  }

  function onState(msg) {
    stopPreparing();
    if (ended || !msg.video) return;
    const firstState = room === null;
    room = msg;
    serverState = msg;
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
  renderStartButton();
  setInterval(syncTick, SYNC_MS);
  setInterval(renderProgress, 250);
  connect();
})();
