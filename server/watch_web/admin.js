// 一起看管理后台：每 POLL_MS 轮询 /admin/api，渲染房间、「想看但没有」的片和服务器资源。
// 登录在 /admin 的登录页完成（密钥 + 密码），之后请求都靠 cookie；401 就刷新回到登录页。
(function () {
  "use strict";
  const POLL_MS = 5000;
  const $ = (id) => document.getElementById(id);
  let latest = null;

  // ---------- 格式 ----------
  const pad = (n) => String(n).padStart(2, "0");
  function fmtTime(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
  }
  function fmtAge(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    if (sec < 60) return `${sec} 秒`;
    if (sec < 3600) return `${Math.floor(sec / 60)} 分钟`;
    if (sec < 86400) return `${Math.floor(sec / 3600)} 小时 ${Math.floor((sec % 3600) / 60)} 分`;
    return `${Math.floor(sec / 86400)} 天 ${Math.floor((sec % 86400) / 3600)} 小时`;
  }
  function fmtBytes(n) {
    if (n == null) return "—";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
  }
  function fmtRate(bytesPerSec) {
    if (bytesPerSec == null) return "—";
    const mbps = bytesPerSec * 8 / 1e6;
    return mbps >= 10 ? `${mbps.toFixed(0)} Mbps` : mbps >= 0.1 ? `${mbps.toFixed(1)} Mbps` : `${(bytesPerSec * 8 / 1e3).toFixed(0)} kbps`;
  }
  function fmtPct(v) { return v == null ? "—" : `${v.toFixed(v < 10 ? 1 : 0)}%`; }
  function fmtDate(ts) {
    const d = new Date(ts * 1000);
    return `${d.getMonth() + 1}月${d.getDate()}日 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function clock() {
    const d = new Date();
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  // ---------- DOM ----------
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function tile(label, value, detail, ratio) {
    const t = el("div", "tile");
    t.appendChild(el("div", "label", label));
    t.appendChild(el("div", "value", value));
    if (detail) t.appendChild(el("div", "detail", detail));
    if (ratio != null) {
      const bar = el("div", "bar" + (ratio >= 0.9 ? " hot" : ""));
      const fill = el("i");
      fill.style.width = `${Math.max(0, Math.min(100, ratio * 100))}%`;
      bar.appendChild(fill);
      t.appendChild(bar);
    }
    return t;
  }
  function replace(parent, children) {
    parent.replaceChildren(...children);
  }
  function channelText(ch) {
    if (!ch) return "—";
    return ch.name ? `#${ch.name}` : `频道 ${ch.id}`;
  }

  // ---------- 渲染 ----------
  function renderSummary(d) {
    const s = d.summary;
    replace($("summary"), [
      tile("房间", String(s.rooms), s.bili_rooms ? `其中 B 站 ${s.bili_rooms} 个` : ""),
      tile("在看", `${s.viewers} 人`),
      tile("今日 B 站转流", fmtBytes(s.bili_bytes_today), "UTC 0 点清零"),
      tile("bot 已运行", fmtAge(d.server_time - d.started_at)),
    ]);
  }

  function renderStats(d) {
    const st = d.stats;
    if (!st) {
      replace($("stats"), [tile("资源用量", "—", "这台机器上读不到 /proc")]);
      return;
    }
    const tiles = [];
    tiles.push(tile("CPU（整机）", fmtPct(st.cpu_percent),
      st.load ? `负载 ${st.load.map((x) => x.toFixed(2)).join(" / ")} · ${st.cpu_count || "?"} 核` : `${st.cpu_count || "?"} 核`,
      st.cpu_percent == null ? null : st.cpu_percent / 100));
    if (st.mem) {
      tiles.push(tile("内存（整机）", fmtBytes(st.mem.used), `共 ${fmtBytes(st.mem.total)} · 可用 ${fmtBytes(st.mem.available)}`,
        st.mem.used / st.mem.total));
    } else {
      tiles.push(tile("内存（整机）", "—"));
    }
    if (st.container_mem) {
      const c = st.container_mem;
      tiles.push(tile("容器内存", fmtBytes(c.current), c.max ? `上限 ${fmtBytes(c.max)}` : "没有上限",
        c.max ? c.current / c.max : null));
    }
    tiles.push(tile("bot 进程", fmtBytes(st.process_rss), `CPU ${fmtPct(st.process_cpu_percent)}`));
    if (st.disk) {
      tiles.push(tile("磁盘", fmtBytes(st.disk.used), `共 ${fmtBytes(st.disk.total)} · 剩 ${fmtBytes(st.disk.free)}`,
        st.disk.used / st.disk.total));
    }
    if (st.net) {
      tiles.push(tile("网络 ↑ 出", fmtRate(st.net.tx_rate), `累计 ${fmtBytes(st.net.tx_bytes)}`));
      tiles.push(tile("网络 ↓ 入", fmtRate(st.net.rx_rate), `累计 ${fmtBytes(st.net.rx_bytes)}`));
    }
    if (st.uptime != null) tiles.push(tile("服务器开机", fmtAge(st.uptime)));
    replace($("stats"), tiles);
  }

  function renderRooms(d) {
    const rows = d.rooms.map((r) => {
      const tr = el("tr");
      const idCell = el("td");
      const a = el("a", "room", r.room_id);
      a.href = r.url;
      a.target = "_blank";
      a.rel = "noopener";
      idCell.appendChild(a);
      tr.appendChild(idCell);
      tr.appendChild(el("td", "nowrap", channelText(r.channel)));
      const title = el("td", "title-cell");
      title.appendChild(el("div", null, r.title || "（无标题）"));
      title.appendChild(el("div", "muted", r.source_label + (r.episode_count ? ` · 共 ${r.episode_count} 集` : "")));
      tr.appendChild(title);
      const state = r.finished ? "⏹ 已播完" : r.playing ? "▶ 播放中" : "⏸ 已暂停";
      const pos = fmtTime(r.position) + (r.duration ? ` / ${fmtTime(r.duration)}` : "");
      tr.appendChild(el("td", "nowrap", `${state} ${pos}`));
      const v = el("td");
      v.appendChild(el("div", null, `${r.viewers.length} 人`));
      if (r.viewers.length) {
        const names = el("div");
        for (const m of r.viewers) {
          const cls = "tag" + (m.is_host ? " host" : m.can_control ? " ctl" : "") + (m.online ? "" : " off");
          names.appendChild(el("span", cls, m.name + (m.online ? "" : "（断线）")));
        }
        v.appendChild(names);
      }
      tr.appendChild(v);
      tr.appendChild(el("td", "nowrap", r.host || `—（发起人 ${r.creator}）`));
      tr.appendChild(el("td", "nowrap", fmtAge(r.age)));
      return tr;
    });
    replace($("rooms"), rows);
    $("rooms-empty").hidden = rows.length > 0;
    $("rooms-sub").textContent = rows.length ? `${rows.length} 个` : "";
  }

  function renderRequests(d) {
    const days = Number($("period").value);
    const cutoff = days ? d.server_time - days * 86400 : -Infinity;
    const groups = new Map();
    for (const r of d.requests) {
      if (r.at < cutoff) continue;
      let g = groups.get(r.key);
      if (!g) {
        g = { title: r.query, count: 0, kinds: {}, users: [], channels: [], last: 0 };
        groups.set(r.key, g);
      }
      g.count++;
      g.kinds[r.kind_label] = (g.kinds[r.kind_label] || 0) + 1;
      if (!g.users.includes(r.user_name)) g.users.push(r.user_name);
      const ch = channelText(r.channel);
      if (!g.channels.includes(ch)) g.channels.push(ch);
      g.last = Math.max(g.last, r.at);
    }
    const list = [...groups.values()].sort((a, b) => b.count - a.count || b.last - a.last);
    const rows = list.map((g) => {
      const tr = el("tr");
      tr.appendChild(el("td", "title-cell", g.title));
      tr.appendChild(el("td", "num", String(g.count)));
      const kinds = el("td");
      for (const [label, n] of Object.entries(g.kinds)) {
        kinds.appendChild(el("span", "tag" + (label === "没搜到" ? " kind-no_results" : ""), n > 1 ? `${label} ×${n}` : label));
      }
      tr.appendChild(kinds);
      tr.appendChild(el("td", null, g.users.join("、")));
      tr.appendChild(el("td", null, g.channels.join("、")));
      tr.appendChild(el("td", "nowrap", fmtDate(g.last)));
      return tr;
    });
    replace($("requests"), rows);
    $("requests-empty").hidden = rows.length > 0;
    const total = list.reduce((n, g) => n + g.count, 0);
    $("requests-sub").textContent = (rows.length ? `${rows.length} 部 · ${total} 次` : "")
      + (d.request_limit ? ` · 每人每天最多 ${d.request_limit} 次` : "");
  }

  const COUNTRY = {
    AU: "澳大利亚", NZ: "新西兰", CN: "中国大陆", HK: "香港", TW: "台湾", MO: "澳门", US: "美国", CA: "加拿大",
    GB: "英国", JP: "日本", KR: "韩国", SG: "新加坡", MY: "马来西亚", ID: "印尼", TH: "泰国", VN: "越南", PH: "菲律宾",
    IN: "印度", DE: "德国", FR: "法国", NL: "荷兰", RU: "俄罗斯", BR: "巴西", MX: "墨西哥", IT: "意大利", ES: "西班牙",
    SE: "瑞典", FI: "芬兰", NO: "挪威", DK: "丹麦", IE: "爱尔兰", CH: "瑞士", PL: "波兰", TR: "土耳其", AE: "阿联酋",
    SA: "沙特", IL: "以色列", ZA: "南非", AR: "阿根廷", CL: "智利", KH: "柬埔寨", MM: "缅甸", LA: "老挝", MN: "蒙古",
    PK: "巴基斯坦", BD: "孟加拉", LK: "斯里兰卡", NP: "尼泊尔", FJ: "斐济", PG: "巴布亚新几内亚", LOCAL: "内网 / 本机",
  };
  function countryText(cc) {
    if (!cc) return "未知（不在任何分配表里）";
    return COUNTRY[cc] ? `${COUNTRY[cc]}（${cc}）` : cc;
  }

  function renderBlocked(d) {
    const g = d.geo || {};
    let sub;
    if (!g.enabled) {
      sub = "没有开地区限制（WATCH_ALLOWED_COUNTRIES 为空）";
    } else if (!g.loaded) {
      sub = `只放行 ${g.allowed.join("、")} · ⚠️ 地区数据还没加载，暂时没有拦截` + (g.last_error ? `（上次下载失败：${g.last_error}）` : "");
    } else {
      sub = `只放行 ${g.allowed.join("、")} · 地区数据 ${g.ranges} 段，${fmtDate(g.fetched_at)} 更新`
        + (g.refreshing ? " · 正在刷新…" : "") + (g.last_error ? ` · ⚠️ 上次刷新失败：${g.last_error}` : "");
    }
    $("geo-sub").textContent = sub;
    const rows = (d.blocked || []).map((b) => {
      const tr = el("tr");
      tr.appendChild(el("td", "nowrap", b.id));
      tr.appendChild(el("td", "nowrap", b.ip));
      tr.appendChild(el("td", null, countryText(b.country)));
      const rooms = el("td");
      for (const rid of b.rooms) {
        const a = el("a", "room", rid);
        a.href = `/r/${rid}`;
        a.target = "_blank";
        a.rel = "noopener";
        rooms.appendChild(a);
        rooms.appendChild(document.createTextNode(" "));
      }
      tr.appendChild(rooms);
      tr.appendChild(el("td", "num", String(b.hits)));
      tr.appendChild(el("td", "nowrap", fmtDate(b.first_at)));
      tr.appendChild(el("td", "nowrap", fmtDate(b.last_at)));
      return tr;
    });
    replace($("blocked"), rows);
    $("blocked-empty").hidden = rows.length > 0;
  }

  function render(d) {
    latest = d;
    renderSummary(d);
    renderStats(d);
    renderRooms(d);
    renderBlocked(d);
    renderRequests(d);
    $("status").textContent = `更新于 ${clock()}`;
    $("status").classList.remove("err");
  }

  async function poll() {
    try {
      const resp = await fetch("/admin/api", { cache: "no-store", credentials: "same-origin" });
      if (resp.status === 401 || resp.status === 404) {
        location.reload();      // 登录过期：/admin 会显示输入密钥的页面
        return;
      }
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      render(await resp.json());
    } catch (e) {
      $("status").textContent = `连接失败（${e.message || e}），稍后重试`;
      $("status").classList.add("err");
    }
  }

  $("period").addEventListener("change", () => { if (latest) renderRequests(latest); });
  poll();
  setInterval(poll, POLL_MS);
})();
