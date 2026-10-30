// Watch-together synced player. Plain ES2020, no build step.
//
// Echo-loop rule: `control` messages are sent ONLY from the user's own clicks on
// our controls (play button, seek bar). Nothing listens to player events
// (play/pause/seeked/onStateChange) to send anything, so state the server pushes
// to us can never bounce back. The native YouTube controls are off, the <video>
// has no `controls`, and a transparent shield covers the video area.
//
// Live TV (video.source === "tv"): the same HLS player, but no position. The
// room only says playing/paused; play jumps to the live edge. Any viewer whose
// stream stops moving sends `tv_stall` (once per version); the host can send
// `tv_next`. The server answers a switch with a new version and a `notice`.
"use strict";

(() => {
  const DASH_SRC = "https://cdnjs.cloudflare.com/ajax/libs/dashjs/4.7.4/dash.all.min.js";
  const YT_SRC = "https://www.youtube.com/iframe_api";
  const HLS_SRC = "https://cdnjs.cloudflare.com/ajax/libs/hls.js/1.5.20/hls.min.js";
  const QR_SRC = "https://cdnjs.cloudflare.com/ajax/libs/qrcode-generator/1.4.4/qrcode.min.js";
  const HLS_MIME = "application/vnd.apple.mpegurl";
  const PING_MS = 10000;
  const SYNC_MS = 1000;
  const RECONNECT_MS = 2000;
  const HARD_DRIFT = 2;      // seconds: jump instead of nudging
  const SOFT_DRIFT = 0.3;    // seconds: start nudging playbackRate
  const CAUGHT_UP = 0.1;     // seconds: nudging done, back to 1x
  const PAUSED_DRIFT = 0.25; // seconds: while paused, re-seek beyond this
  const RATE_STEPS = { bilibili: [0.95, 1.05], youtube: [0.75, 1.25], vod: [0.95, 1.05] };
  // Casting (AirPlay / Chromecast): the TV reports its position with ~0.5 s of
  // slack and ignores playbackRate, so sync by seeking, less often and with a
  // wider band. REMOTE_SEEK_LEAD covers the seek round trip.
  const REMOTE_DRIFT = 1.5;        // seconds while playing: re-seek beyond this
  const REMOTE_PAUSED_DRIFT = 1.0; // seconds while paused
  const REMOTE_SEEK_LEAD = 0.5;    // seconds added to a playing seek target
  const REMOTE_SEEK_GAP_MS = 3000; // never seek a remote more often than this
  const UNPLAYABLE_MSG = "这个视频暂时无法播放";
  const VOD_FAILED_MSG = "视频加载失败";
  const CAST_FAILED_MSG = "投屏失败，电视播不了这个片源";
  const CAST_ONLY_VOD_MSG = "投屏只支持按片名搜到的剧集，已停止投屏";
  const CAST_STOPPED_MSG = "已停止投屏";
  const PREPARING_MSG = "正在准备…";
  const PREPARING_TIMEOUT_MS = 50000;   // outlasts the server's 45 s VOD_PREPARE_TIMEOUT
  const RESUME_NEAR = 30;    // seconds: resume line hidden this close to the snapshot
  const LIVE_STALL_MS = 10000; // live: currentTime stuck this long while playing → tv_stall
  const LIVE_RETRY_MS = 5000;  // live: rebuild a failed player (same version) after this
  const NOTICE_MS = 3000;
  const TOAST_MS = 3500;

  const $ = (id) => document.getElementById(id);
  const ui = {
    title: $("title"), conn: $("conn"), permBtn: $("perm-btn"),
    stageWrap: $("stage-wrap"), host: $("player-host"), stageMsg: $("stage-msg"), start: $("start"),
    play: $("play"), seek: $("seek"), time: $("time"), mute: $("mute"), volume: $("volume"), fs: $("fs"),
    cast: $("cast"),
    me: $("me"), panel: $("perm-panel"), members: $("members"),
    changeForm: $("change-form"), changeUrl: $("change-url"), toast: $("toast"),
    resume: $("resume"), vodPanel: $("vod-panel"), episodes: $("episodes"),
    lineLabel: $("line-label"), nextLine: $("next-line"),
    bsBtn: $("bs-btn"), bsPanel: $("bs-panel"), bsClose: $("bs-close"),
    qr: $("qr"), bsHost: $("bs-host"), bsCode: $("bs-code"),
    liveBadge: $("live-badge"), tvPanel: $("tv-panel"), tvLineLabel: $("tv-line-label"), tvNext: $("tv-next"),
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
  // Live TV, reset by buildPlayer:
  let liveWasPlaying = false; // room was playing at the last live sync tick
  let liveEdgePending = false; // jump to the live edge as soon as the player knows where it is
  let stallSentFor = null;    // version we already sent tv_stall for (kept across same-version rebuilds)
  let stallLastTime = -1;     // currentTime at the last advance check
  let stallSince = 0;         // performance.now() when currentTime last moved (or the check restarted)

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
    if (player.live) { syncLive(); return; }
    const remote = isRemote();
    // A TV that is still seeking/buffering reports a stale position: wait.
    if (remote && player.busy && player.busy()) return;
    const target = expectedPosition(room);
    const dur = currentDuration();
    // Calling play() on an ended video restarts it from 0; don't.
    const atEnd = dur > 0 && target >= dur - 0.5;

    if (room.playing && player.isPaused() && !atEnd) player.play();
    if (!room.playing && !player.isPaused()) player.pause();

    const drift = target - player.getTime();
    if (!room.playing) {
      // A paused target doesn't depend on the clock, so no pong is needed.
      if (Math.abs(drift) > (remote ? REMOTE_PAUSED_DRIFT : PAUSED_DRIFT)) {
        if (remote) remoteSeek(target, true); else player.seek(target, { keepPaused: true });
      }
      player.setRate(1);
      return;
    }
    // While playing, the target depends on clock offset: wait for a real sample.
    if (atEnd || !clockReady()) { player.setRate(1); return; }
    if (remote) {
      // No rate nudging on a TV: jump (slightly ahead, the seek takes a moment).
      if (Math.abs(drift) > REMOTE_DRIFT) remoteSeek(target + REMOTE_SEEK_LEAD, false);
      return;
    }
    if (Math.abs(drift) > HARD_DRIFT) {
      player.seek(target);
      player.setRate(1);
      return;
    }
    player.setRate(chooseRate(drift, player.getRate(), RATE_STEPS[player.source]));
  }

  // Playback is happening on a TV (AirPlay target or a Cast session).
  function isRemote() {
    return !!(player && player.remote && player.remote());
  }

  let lastRemoteSeek = -Infinity;
  function remoteSeek(target, keepPaused) {
    const now = performance.now();
    if (now - lastRemoteSeek < REMOTE_SEEK_GAP_MS) return;
    lastRemoteSeek = now;
    player.seek(Math.max(0, target), { keepPaused });
  }

  // Live: follow playing/paused only; no position, no drift correction.
  function syncLive() {
    player.setRate(1);
    if (!room.playing) {
      liveWasPlaying = false;
      if (!player.isPaused()) player.pause();
      resetStallCheck();
      return;
    }
    if (!liveWasPlaying) { liveWasPlaying = true; liveEdgePending = true; }
    if (liveEdgePending && player.seekLive()) liveEdgePending = false;
    if (player.isPaused()) player.play();
    checkLiveStall();
  }

  function resetStallCheck() {
    stallLastTime = -1;
    stallSince = performance.now();
  }

  // Called every sync tick while the room plays. A hidden tab may be throttled
  // or paused by the browser, so it neither counts nor reports.
  function checkLiveStall() {
    if (document.visibilityState !== "visible") { resetStallCheck(); return; }
    const t = player.getTime();
    if (t !== stallLastTime) { stallLastTime = t; stallSince = performance.now(); return; }
    if (performance.now() - stallSince >= LIVE_STALL_MS) reportStall();
  }

  function reportStall() {
    const version = playerVersion;
    if (version === null || stallSentFor === version) return;
    if (!room || !room.video || room.video.version !== version) return;
    stallSentFor = version;
    send({ type: "tv_stall", version });
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
  // where the browser has it (Safari/iOS), hls.js elsewhere. Vod failures are
  // local only: we show a message and never tell the server. With `live` (TV)
  // the playlist is our relay's live window; a failure also reports tv_stall.
  function createVodPlayer(hlsUrl, live = false) {
    const video = document.createElement("video");
    video.playsInline = true;
    video.setAttribute("playsinline", "");
    video.setAttribute("x-webkit-airplay", "allow");
    video.preload = "auto";
    ui.host.appendChild(video);
    // Safari (the only browser with webkitShowPlaybackTargetPicker) keeps native
    // HLS for vod so AirPlay can take the stream; hls.js feeds a MediaSource,
    // which AirPlay cannot forward. Live TV stays on hls.js everywhere.
    const airplay = !live && typeof video.webkitShowPlaybackTargetPicker === "function"
      && !!video.canPlayType(HLS_MIME);
    const useHls = !airplay && !!(window.Hls && window.Hls.isSupported());
    const native = !useHls && !!video.canPlayType(HLS_MIME);
    const mode = useHls ? `hls.js ${window.Hls.version || "?"}` : native ? "native HLS" : "no HLS support";
    let hls = null;
    let lastHlsError = "";   // last non-fatal hls.js error: context when the <video> errors first

    const adapter = {
      source: live ? "tv" : "vod",
      live,
      ready: true,
      failed: false,
      failMsg: VOD_FAILED_MSG,
      video,
      native,
      remoteAvailable: false,              // an AirPlay / Remote Playback target exists
      remote: () => remotePlaying(video),  // playing on that target right now
      busy: () => video.seeking,
      promptRemote: () => promptRemote(video),
      // Same element, new playlist: keeps an AirPlay session alive across episodes.
      load: (url) => { if (native) video.src = url; else if (hls) hls.loadSource(url); },
      // Jump to the live edge; false while the player doesn't know it yet.
      seekLive: () => {
        let edge = null;
        if (hls) {
          edge = hls.liveSyncPosition;
        } else if (video.seekable && video.seekable.length) {
          edge = video.seekable.end(video.seekable.length - 1);
        }
        if (typeof edge !== "number" || !Number.isFinite(edge)) return false;
        video.currentTime = edge;
        return true;
      },
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
      failPlayer(VOD_FAILED_MSG, reason, hlsUrl, !live, mode);
      if (live) {
        reportStall();
        // The server may not switch (cooldown, no other source) and the network
        // may come back: try the same stream again instead of staying dead.
        const version = playerVersion;
        setTimeout(() => {
          if (player === adapter && !ended && room && room.video.version === version) buildPlayer(room.video);
        }, LIVE_RETRY_MS);
        return;
      }
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
      hls = new window.Hls(live ? { liveSyncDurationCount: 3 } : {});
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
      // Only a playlist the browser plays itself can be handed to a TV.
      if (!live) watchRemote(video, adapter);
    } else {
      // No way to play HLS here; report once the caller has installed us.
      Promise.resolve().then(() => onError("这个浏览器不支持 HLS 播放"));
    }
    return adapter;
  }

  // ---- AirPlay / Remote Playback API (Safari, iOS; Chrome on Android) ----
  // The <video> keeps playing "through" the TV: currentTime, play() and pause()
  // still work, so the vod adapter stays in charge; only the sync policy changes.

  function remotePlaying(video) {
    if (video.remote && video.remote.state === "connected") return true;
    return !!video.webkitCurrentPlaybackTargetIsWireless;
  }

  // Opens the system picker; it is also the only way to stop (pick "iPhone" again).
  function promptRemote(video) {
    if (video.remote && typeof video.remote.prompt === "function") {
      return video.remote.prompt().catch(() => {});
    }
    if (typeof video.webkitShowPlaybackTargetPicker === "function") video.webkitShowPlaybackTargetPicker();
    return Promise.resolve();
  }

  function watchRemote(video, adapter) {
    const onChange = () => { if (player === adapter) onRemoteChange(adapter); };
    const setAvailable = (v) => {
      if (adapter.remoteAvailable === v) return;
      adapter.remoteAvailable = v;
      if (player === adapter) renderCastButton();
    };
    if (video.remote && typeof video.remote.watchAvailability === "function") {
      video.remote.watchAvailability((v) => setAvailable(!!v))
        // NotSupportedError: this browser can't tell in advance; show the button anyway.
        .catch(() => setAvailable(true));
      for (const ev of ["connecting", "connect", "disconnect"]) video.remote.addEventListener(ev, onChange);
    } else if (typeof video.webkitShowPlaybackTargetPicker === "function") {
      video.addEventListener("webkitplaybacktargetavailabilitychanged", (e) => {
        setAvailable(e.availability === "available");
      });
    }
    video.addEventListener("webkitcurrentplaybacktargetiswirelesschanged", onChange);
  }

  let wasRemote = false;
  function onRemoteChange(adapter) {
    const now = adapter.remote();
    if (now !== wasRemote) {
      wasRemote = now;
      lastRemoteSeek = -Infinity;
      if (now) toast("正在投屏到电视");
    }
    renderCastButton();
    syncTick();
  }

  // ---- Chromecast: the receiver plays the playlist, we only steer it ----
  // Volume is the TV's own: on connect we take its level instead of pushing
  // ours, so a page at 100% never blasts the living room.
  function createCastPlayer(video) {
    const C = window.WatchCast;
    const url = new URL(video.hls, location.href).href;
    let loading = true;
    let cancelled = false;
    const adapter = {
      source: "vod",
      ready: false,
      failed: false,
      failMsg: CAST_FAILED_MSG,
      cast: true,
      remote: () => true,
      busy: () => loading || C.player.isBuffering(),
      getTime: () => C.player.getTime(),
      getDuration: () => C.player.getDuration(),
      isPaused: () => C.player.isPaused(),
      play: () => C.player.play(),
      pause: () => C.player.pause(),
      seek: (t) => C.player.seek(t),
      getRate: () => 1,
      setRate: () => {},
      setVolume: (v, m) => C.player.setVolume(v, m),
      destroy: () => { cancelled = true; },
    };
    (async () => {
      let fmp4 = false;
      try {
        // The receiver wants to know the segment container; our playlist says.
        const text = await (await fetch(video.hls, { cache: "no-store" })).text();
        fmp4 = text.includes("#EXT-X-MAP");
      } catch { /* let the receiver guess */ }
      if (cancelled) return;
      try {
        await C.load({
          url, title: video.title, fmp4,
          startTime: room ? expectedPosition(room) : 0,
          autoplay: !!(room && room.playing),
        });
      } catch {
        if (cancelled) return;
        // The receiver refused the playlist (codec, CORS on the CDN…): drop the
        // session; its end event brings the local player back.
        adapter.failed = true;
        setStageMessage(CAST_FAILED_MSG);
        toast(CAST_FAILED_MSG);
        C.stop();
        return;
      }
      if (cancelled) return;
      loading = false;
      adapter.ready = true;
      const v = C.player.getVolume();
      volume = v.level;
      muted = v.muted;
      applyVolume();
      wasRemote = true;
      lastRemoteSeek = -Infinity;
      onPlayerReady(adapter);
    })();
    return adapter;
  }

  function castConnected() {
    return !!(window.WatchCast && window.WatchCast.connected());
  }

  // A Cast session came or went (user picked a TV, TV stopped, auto-rejoin on reload).
  function onCastSession(connected) {
    if (connected) {
      if (ended || !room) return;  // onState builds the cast player once a state arrives
      if (!isVod()) {
        window.WatchCast.stop();
        toast(CAST_ONLY_VOD_MSG);
        return;
      }
      started = true;              // the TV needs no tap to start
      ui.start.hidden = true;
      buildPlayer(room.video);
    } else {
      if (player && player.cast) {
        toast(CAST_STOPPED_MSG);
        if (room && !ended) buildPlayer(room.video);
      }
      wasRemote = false;
      renderCastButton();
    }
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
    // AirPlay is bound to the <video> element: for the next episode swap the
    // playlist instead of the element, or the TV would drop the session.
    if (video.source === "vod" && player && player.source === "vod" && player.native
        && !player.failed && player.remote() && !castConnected()) {
      buildToken++;
      playerVersion = video.version;
      player.load(video.hls);
      lastRemoteSeek = -Infinity;
      syncTick();
      return;
    }
    destroyPlayer();
    const token = buildToken;
    playerVersion = video.version;
    liveWasPlaying = false;
    liveEdgePending = false;
    resetStallCheck();
    setStageMessage("加载中…");
    renderStartButton();
    if (castConnected() && video.source !== "vod") {
      window.WatchCast.stop();
      toast(CAST_ONLY_VOD_MSG);
    }
    try {
      if (video.source === "bilibili") {
        await loadScript(DASH_SRC);
        if (token !== buildToken) return;
        player = createBiliPlayer(video.mpd);
        onPlayerReady(player);
      } else if (video.source === "vod" && castConnected()) {
        player = createCastPlayer(video);   // reports ready itself once the TV accepted
        renderCastButton();
      } else if (video.source === "vod" || video.source === "tv") {
        // Prefer hls.js (MSE) wherever it works: browsers that claim native HLS
        // (Android Chrome, some TVs) often fail on real streams with a bare
        // "format not supported". Native is only for browsers without MSE
        // (iPhone Safari), when the hls.js script cannot be loaded, or Safari
        // playing vod (native keeps AirPlay possible; see createVodPlayer).
        try {
          await loadScript(HLS_SRC);
        } catch (err) {
          if (!document.createElement("video").canPlayType(HLS_MIME)) throw err;
        }
        if (token !== buildToken) return;
        player = createVodPlayer(video.hls, video.source === "tv");
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
    renderCastButton();
    syncTick();
  }

  // ---------------------------------------------------------------- UI

  function setStageMessage(text) {
    stageText = text;
    renderStageMessage();
  }

  function renderStageMessage() {
    // While a Cast session plays, the stage is empty: say where the picture went.
    const casting = player && player.cast && !player.failed
      ? `正在投屏到 ${window.WatchCast.deviceName() || "电视"}` : "";
    const text = preparing ? PREPARING_MSG : stageText || casting;
    ui.stageMsg.textContent = text;
    ui.stageMsg.hidden = !text;
  }

  // 📺 button: shown when a TV can be reached (Cast device on the network, or
  // an AirPlay target); lit while casting.
  function renderCastButton() {
    const casting = isRemote();
    const C = window.WatchCast;
    const reachable = isVod() && !ended && ((C && C.available) || !!(player && player.remoteAvailable));
    ui.cast.hidden = !(reachable || casting);
    ui.cast.classList.toggle("on", casting);
    const label = casting ? "停止投屏" : "投屏";
    ui.cast.setAttribute("aria-label", label);
    ui.cast.title = label;
    renderStageMessage();
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
    renderTvPanel();
  }

  function stopPreparing() {
    clearTimeout(preparingTimer);
    preparingTimer = null;
    if (!preparing) return;
    preparing = false;
    renderStageMessage();
    renderVodPanel();
    renderTvPanel();
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
    if (isVod() || isLive()) return 0; // vod: only the server's value; live: none
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
    if (isLive()) return;
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
    const live = isLive();
    ui.seek.hidden = live;
    ui.time.hidden = live;
    ui.liveBadge.hidden = !live;
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
    ui.bsBtn.hidden = ended || !room;
    if (ui.bsBtn.hidden) ui.bsPanel.hidden = true;
    renderResume();
    renderVodPanel();
    renderCastButton();
    renderTvPanel();
  }

  // ---- 在电视上看: QR code + room code for a TV browser (joins as a viewer) ----
  // The QR carries the plain viewer link, never the host key.
  function openBigScreen() {
    if (!/^[a-z0-9]{1,12}$/.test(roomId)) return;
    const url = `${location.origin}/r/${roomId}`;
    ui.bsHost.textContent = location.host;
    ui.bsCode.textContent = roomId;
    ui.qr.textContent = "";
    ui.bsPanel.hidden = false;
    ui.bsClose.focus();
    loadScript(QR_SRC).then(() => {
      if (ui.bsPanel.hidden) return;
      const qr = window.qrcode(0, "M");
      qr.addData(url);
      qr.make();
      ui.qr.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 0, scalable: true });
    }).catch(() => { ui.qr.textContent = url; });
  }

  ui.bsBtn.addEventListener("click", openBigScreen);
  ui.bsClose.addEventListener("click", () => { ui.bsPanel.hidden = true; });
  ui.bsPanel.addEventListener("click", (e) => { if (e.target === ui.bsPanel) ui.bsPanel.hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") ui.bsPanel.hidden = true; });

  function isVod() {
    return !!(room && room.video && room.video.source === "vod");
  }

  function isLive() {
    return !!(room && room.video && room.video.source === "tv");
  }

  // "上次一起看到…": hidden when the room is already at the snapshot.
  function renderResume() {
    const r = room && room.resume;
    const atSnapshot = r && isVod() && room.video.episode === r.episode
      && Math.abs(expectedPosition(room) - r.position) < RESUME_NEAR;
    ui.resume.textContent = "";
    ui.resume.hidden = !r || ended || atSnapshot || isLive();
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

  // Host-only source switch for live TV, inside the 🔑 panel.
  function renderTvPanel() {
    const tv = me.isHost && !ended && isLive();
    ui.tvPanel.hidden = !tv;
    if (!tv) return;
    const count = Number(room.video.line_count) || 0;
    const index = Number(room.video.line) || 0;
    ui.tvLineLabel.textContent = `直播源：第 ${index + 1} 个（共 ${count} 个）`;
    ui.tvNext.disabled = preparing || count <= 1;
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
  function toast(text, opts) {
    ui.toast.textContent = text;
    ui.toast.classList.toggle("info", !!(opts && opts.info));
    ui.toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { ui.toast.hidden = true; }, (opts && opts.ms) || TOAST_MS);
  }

  function showEnded() {
    ended = true;
    stopPreparing();
    if (castConnected()) window.WatchCast.stop();
    destroyPlayer();
    ui.start.hidden = true;
    setStageMessage("房间已结束");
    renderControls();
  }

  // ---------------------------------------------------------------- user actions
  // These are the ONLY places that send `control`.

  function sendControl(action, position) {
    if (!me.canControl || !room) return;
    if (isLive()) {
      if (action === "seek") return;   // the server refuses it anyway
      position = 0;
    }
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

  // Chromecast first (its picker also lists AirPlay-less TVs); otherwise the
  // system AirPlay picker, which is where an AirPlay session is ended too.
  ui.cast.addEventListener("click", () => {
    const C = window.WatchCast;
    if (player && player.cast) { C.stop(); return; }
    if (C && C.available && !isRemote()) {
      C.request().catch(() => {});  // cancelled picker: nothing to do
      return;
    }
    if (player && player.promptRemote) player.promptRemote();
  });

  ui.start.addEventListener("click", () => {
    if (!player || !player.ready) return; // button is disabled until ready anyway
    started = true;
    ui.start.hidden = true;
    // Call play inside the click handler so the browser counts it as a gesture.
    if (player.live) {
      if (room && room.playing) {
        liveWasPlaying = true;
        liveEdgePending = !player.seekLive();
        player.play();
      }
    } else if (room && room.playing) {
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

  ui.tvNext.addEventListener("click", () => {
    if (preparing) return;
    send({ type: "tv_next" });
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
      case "notice":
        toast(String(msg.message || ""), { info: true, ms: NOTICE_MS });
        break;
      case "error":
        stopPreparing();
        toast(String(msg.message || "出错了") + (msg.detail ? `（${msg.detail}）` : ""));
        revertOptimistic();
        break;
      case "ended":
        showEnded();
        break;
      case "blocked":
        showBlocked(msg);
        break;
    }
  }

  // 地区不在放行名单：服务器只发这一条就断开。不重连，不建播放器；把编号给用户看，方便找管理员对照。
  function showBlocked(msg) {
    ended = true;
    stopPreparing();
    destroyPlayer();
    ui.start.hidden = true;
    const id = msg.id ? String(msg.id) : "";
    setStageMessage(String(msg.message || "你所在的地区暂时不能观看") + (id ? `\n访客编号：${id}` : ""));
    renderControls();
    ui.me.textContent = id ? `房间 ${roomId} · 访客编号 ${id}` : `房间 ${roomId}`;
    ui.title.textContent = "一起看 · 地区受限";
    document.title = "一起看 · 地区受限";
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
    // The Cast SDK is only worth loading once there is something castable.
    if (msg.video.source === "vod" && window.WatchCast) window.WatchCast.init();
    if (msg.video.version !== playerVersion) buildPlayer(msg.video);
    if (firstState && !started) ui.start.hidden = false;
    renderControls();
    syncTick();
  }

  // ---------------------------------------------------------------- boot

  if (window.WatchCast) {
    window.WatchCast.on("available", renderCastButton);
    window.WatchCast.on("session", onCastSession);
  }
  applyVolume();
  renderControls();
  renderStartButton();
  setInterval(syncTick, SYNC_MS);
  setInterval(renderProgress, 250);
  connect();
})();
