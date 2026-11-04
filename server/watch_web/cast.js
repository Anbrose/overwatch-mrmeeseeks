// Google Cast (Chromecast) sender glue for the watch-together player.
//
// Exposes window.WatchCast. The Cast sender SDK is loaded lazily and only in
// Chrome-family browsers (it is the only place the SDK works; elsewhere it just
// reports "unavailable"). The TV plays the room's HLS playlist itself through
// Google's default media receiver; this page only sends load / play / pause /
// seek and reads the position back. player.js wraps `WatchCast.player` in a
// player adapter and keeps the room in sync with seeks instead of rate nudges.
"use strict";

(() => {
  const SDK_SRC = "https://www.gstatic.com/cv/js/sender/v1/cast_sender.js?loadCastFramework=1";
  const HLS_MIME = "application/x-mpegurl";

  let state = "idle";          // idle | loading | ready | unsupported
  let context = null;          // cast.framework.CastContext
  let remote = null;           // cast.framework.RemotePlayer
  let controller = null;       // cast.framework.RemotePlayerController
  let available = false;       // at least one Cast device on the network
  const listeners = { available: [], session: [], media: [] };

  function emit(kind, ...args) {
    for (const cb of listeners[kind]) {
      try { cb(...args); } catch { /* a listener must not break the others */ }
    }
  }

  function on(kind, cb) {
    if (listeners[kind]) listeners[kind].push(cb);
  }

  // Chrome on iOS (CriOS) has window.chrome but no Cast support.
  function supported() {
    return typeof window.chrome === "object" && window.chrome !== null
      && !/CriOS|FxiOS/.test(navigator.userAgent);
  }

  function init() {
    if (state !== "idle") return;
    if (!supported()) { state = "unsupported"; return; }
    state = "loading";
    window.__onGCastApiAvailable = (ok) => {
      if (ok) {
        try { setup(); } catch { state = "unsupported"; }
      } else {
        state = "unsupported";
      }
    };
    const el = document.createElement("script");
    el.src = SDK_SRC;
    el.async = true;
    el.onerror = () => { state = "unsupported"; el.remove(); };
    document.head.appendChild(el);
  }

  function setup() {
    const cf = window.cast.framework;
    const cc = window.chrome.cast;
    context = cf.CastContext.getInstance();
    context.setOptions({
      receiverApplicationId: cc.media.DEFAULT_MEDIA_RECEIVER_APP_ID,
      autoJoinPolicy: cc.AutoJoinPolicy.ORIGIN_SCOPED,  // a reload rejoins the running session
    });
    remote = new cf.RemotePlayer();
    controller = new cf.RemotePlayerController(remote);

    context.addEventListener(cf.CastContextEventType.CAST_STATE_CHANGED, (e) => {
      const next = e.castState !== cf.CastState.NO_DEVICES_AVAILABLE;
      if (next === available) return;
      available = next;
      emit("available", available);
    });
    context.addEventListener(cf.CastContextEventType.SESSION_STATE_CHANGED, (e) => {
      const S = cf.SessionState;
      if (e.sessionState === S.SESSION_STARTED || e.sessionState === S.SESSION_RESUMED) emit("session", true);
      else if (e.sessionState === S.SESSION_ENDED) emit("session", false);
    });
    controller.addEventListener(cf.RemotePlayerEventType.ANY_CHANGE, () => emit("media"));

    state = "ready";
    available = context.getCastState() !== cf.CastState.NO_DEVICES_AVAILABLE;
    if (available) emit("available", true);
    if (context.getCurrentSession()) emit("session", true);
  }

  function session() {
    return context ? context.getCurrentSession() : null;
  }

  function connected() {
    return !!session();
  }

  function deviceName() {
    const s = session();
    const d = s && s.getCastDevice();
    return (d && d.friendlyName) || "";
  }

  // Must be called from a user gesture (the browser shows its device picker).
  function request() {
    if (!context) return Promise.reject(new Error("cast unavailable"));
    return context.requestSession();
  }

  function stop() {
    if (context) context.endCurrentSession(true);
  }

  // Load an HLS playlist on the receiver. Resolves when the receiver accepted
  // the media; rejects with the Cast error code otherwise.
  function load({ url, title, startTime, autoplay, fmp4 }) {
    const s = session();
    if (!s) return Promise.reject(new Error("no session"));
    const cm = window.chrome.cast.media;
    const info = new cm.MediaInfo(url, HLS_MIME);
    info.streamType = cm.StreamType.BUFFERED;
    info.hlsSegmentFormat = fmp4 ? cm.HlsSegmentFormat.FMP4 : cm.HlsSegmentFormat.TS;
    if (cm.HlsVideoSegmentFormat) {
      info.hlsVideoSegmentFormat = fmp4 ? cm.HlsVideoSegmentFormat.FMP4 : cm.HlsVideoSegmentFormat.MPEG2_TS;
    }
    info.metadata = new cm.GenericMediaMetadata();
    info.metadata.title = title || "";
    const req = new cm.LoadRequest(info);
    req.currentTime = Math.max(0, Number(startTime) || 0);
    req.autoplay = !!autoplay;
    return s.loadMedia(req);
  }

  // Remote playback state and controls. Positions are the SDK's estimate
  // (last status from the receiver plus elapsed time), refreshed about once
  // a second, so expect ~0.5 s of slack compared with a local <video>.
  const player = {
    getTime() {
      const s = session();
      const m = s && s.getMediaSession();
      if (m && typeof m.getEstimatedTime === "function") {
        const t = m.getEstimatedTime();
        if (Number.isFinite(t)) return Math.max(0, t);
      }
      return (remote && remote.currentTime) || 0;
    },
    getDuration() {
      return (remote && Number.isFinite(remote.duration) && remote.duration > 0) ? remote.duration : 0;
    },
    state() {
      return (remote && remote.playerState) || "IDLE";  // IDLE | PLAYING | PAUSED | BUFFERING
    },
    isLoaded() {
      return !!(remote && remote.isMediaLoaded);
    },
    isPaused() {
      const s = player.state();
      return s !== "PLAYING" && s !== "BUFFERING";
    },
    isBuffering() {
      return player.state() === "BUFFERING";
    },
    play() {
      if (remote && remote.isMediaLoaded && remote.isPaused) controller.playOrPause();
    },
    pause() {
      if (remote && remote.isMediaLoaded && !remote.isPaused) controller.playOrPause();
    },
    seek(t) {
      if (!remote || !remote.canSeek) return;
      remote.currentTime = Math.max(0, t);
      controller.seek();
    },
    getVolume() {
      return { level: remote ? remote.volumeLevel : 1, muted: !!(remote && remote.isMuted) };
    },
    setVolume(level, muted) {
      if (!remote) return;
      if (Math.abs(remote.volumeLevel - level) > 0.005) {
        remote.volumeLevel = level;
        controller.setVolumeLevel();
      }
      if (!!remote.isMuted !== !!muted) controller.muteOrUnmute();
    },
  };

  window.WatchCast = {
    init,
    on,
    get available() { return available; },
    get state() { return state; },
    connected,
    deviceName,
    request,
    stop,
    load,
    player,
  };
})();
