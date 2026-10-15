// wifiwatch ダッシュボード（127.0.0.1だけ）。外部の資源は読まない。
// 外から来た文字（SSIDなど）は必ず textContent で入れる（innerHTMLに入れない）。

const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  for (const k of kids.flat()) if (k != null && k !== false) el.append(k.nodeType ? k : document.createTextNode(String(k)));
  return el;
}
function s(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  setAttrs(el, attrs);
  for (const k of kids.flat()) if (k != null && k !== false) el.append(k.nodeType ? k : document.createTextNode(String(k)));
  return el;
}
function setAttrs(el, attrs) {
  if (!attrs) return;
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "text") el.textContent = v;
    else if (k === "style") el.style.cssText = v; // CSPでstyle属性は使えないので、CSSOMで入れる
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
}
const clear = (el) => { while (el.firstChild) el.firstChild.remove(); return el; };

// ------------------------------------------------------------------ 表示の約束
const KIND = {
  own: { cls: "k-own", glyph: "●", label: "自チーム" },
  team: { cls: "k-team", glyph: "▲", label: "他高専（交渉できる）" },
  official_infra: { cls: "k-nonneg", glyph: "■", label: "公式インフラ（交渉できない）" },
  broadcast: { cls: "k-nonneg", glyph: "■", label: "放送機材（交渉できない）" },
  unknown: { cls: "k-unknown", glyph: "◆", label: "所属不明" },
  known_ok: { cls: "k-ok", glyph: "○", label: "既知（問題なし）" },
};
const kindOf = (k) => KIND[k] || KIND.unknown;
const LEGEND = [
  ["own", "自チーム"], ["team", "他高専（交渉できる）"], ["official_infra", "公式・放送（交渉できない）"],
  ["unknown", "所属不明"], ["known_ok", "既知"],
];
const ALERT_TYPE_JP = {
  offchannel: "予約外ch", encroach: "帯域の食い込み", newcomer: "新規参入", unknown_strong: "正体不明",
  sig6_once: "6GHzに初出現", sig6_crit: "6GHzに出現", activity: "活動量", retry: "リトライ率",
  ssid_clone: "同名SSID", deauth: "deauth", rx_stop: "受信停止",
};
const STATE_JP = {
  own: "自ch（受信中）", in_block: "自ブロック内（巡回で見る）", patrol: "巡回中", excluded: "巡回外（見ていない）",
  paused: "手動スキャン中で止めている（見ていない）", scanning: "手動スキャンで今見ている",
  scan_target: "手動スキャンの順番待ち（今は見ていない）", stopped: "受信停止中（見ていない）", unseen: "まだ一度も見ていない",
};
const BLIND = new Set(["excluded", "paused", "scan_target", "stopped", "unseen"]);
const MODE_JP = { PRE: "PRE（下見）", LIVE: "LIVE", QUIET: "QUIET（山場）" };
const BAND_JP = { "2.4": "2.4GHz", "5": "5GHz", "6": "6GHz" };
const CHANNEL_COUNT = { "2.4": 13, "5": 20, "6": 24 };
const RX_JP = { ok: "受信 正常", suspect: "受信 様子見", recovering: "受信 復旧中", failed: "受信停止" };

const jstFmt = new Intl.DateTimeFormat("ja-JP", { timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const jstDayFmt = new Intl.DateTimeFormat("ja-JP", { timeZone: "Asia/Tokyo", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const tJST = (ep) => (ep ? jstFmt.format(new Date(ep * 1000)) : "—");
const dJST = (ep) => (ep ? jstDayFmt.format(new Date(ep * 1000)) : "—");
function ago(sec) {
  if (sec == null) return "—";
  if (sec < 1.5) return "今";
  if (sec < 90) return `${Math.round(sec)}秒前`;
  if (sec < 5400) return `${Math.round(sec / 60)}分前`;
  return `${(sec / 3600).toFixed(1)}時間前`;
}
const bandOf = (f) => (f < 3000 ? "2.4" : f <= 5925 ? "5" : "6");
function chNum(f) {
  if (f < 3000) return f === 2484 ? 14 : (f - 2407) / 5;
  if (f <= 5925) return (f - 5000) / 5;
  return (f - 5950) / 5;
}
const chLabel = (f) => `${BAND_JP[bandOf(f)]} ch${chNum(f)}`;
function spanLabel(spans) {
  const parts = spans.map(([lo, hi]) => {
    const a = lo + 10, b = hi - 10;
    return a === b ? `ch${chNum(a)}` : `ch${chNum(a)}〜${chNum(b)}`;
  });
  return parts.join("＋");
}
const ssidText = (st) => (st.ssid ? st.ssid : "（非公開SSID）");

// ------------------------------------------------------------------ 保存（この画面だけの好み）
const prefs = (() => {
  let p = {};
  try { p = JSON.parse(localStorage.getItem("wifiwatch.prefs") || "{}"); } catch { p = {}; }
  return {
    get: (k, d) => (k in p ? p[k] : d),
    set: (k, v) => { p[k] = v; try { localStorage.setItem("wifiwatch.prefs", JSON.stringify(p)); } catch { /* 使えなくても動く */ } },
  };
})();

// ------------------------------------------------------------------ API
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (init.method !== "GET") {
    init.headers["X-Wifiwatch"] = "1";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body || {});
  }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch { data = null; }
  if (!r.ok) {
    const e = new Error((data && data.message) || `HTTP ${r.status}`);
    e.code = data && data.error;
    e.status = r.status;
    throw e;
  }
  return data;
}

function toast(msg, err = false) {
  const t = h("div", { class: "toast" + (err ? " err" : ""), text: msg });
  $("toasts").append(t);
  setTimeout(() => t.remove(), err ? 7000 : 3500);
}

function confirmDialog(title, text, okLabel = "実行する") {
  const d = $("confirm");
  $("confirmTitle").textContent = title;
  $("confirmText").textContent = text;
  $("confirmOk").textContent = okLabel;
  return new Promise((resolve) => {
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true });
    d.returnValue = "cancel";
    d.showModal();
  });
}

// ------------------------------------------------------------------ ツールチップ
const tip = {
  el: null,
  show(x, y, nodes) {
    const el = (this.el ||= $("tooltip"));
    clear(el).append(...nodes);
    el.hidden = false;
    const w = el.offsetWidth, hgt = el.offsetHeight;
    let left = x + 14, top = y + 14;
    if (left + w > innerWidth - 8) left = x - w - 14;
    if (top + hgt > innerHeight - 8) top = y - hgt - 14;
    el.style.left = `${Math.max(8, left)}px`;
    el.style.top = `${Math.max(8, top)}px`;
  },
  hide() { if (this.el) this.el.hidden = true; },
};

// ------------------------------------------------------------------ 状態
const ui = {
  state: null,
  selected: prefs.get("selected", null),
  tab: prefs.get("tab", "stations"),
  stFilter: prefs.get("stFilter", { band: "all", kind: "all", present: true, q: "" }),
  stSort: prefs.get("stSort", { key: "rssi", asc: false }),
  pointer: {},
  records: { list: [], selected: null, detail: null, filter: { kind: "", location: "", band: "" }, lastFetch: 0 },
  events: { list: [], category: "", lastFetch: 0 },
  clients: { list: [], lastFetch: 0 },
  bands: null,
  drawerDetail: null,
};

// ------------------------------------------------------------------ 共通のSVGの模様（斜線＝見ていない）
function installDefs() {
  const svg = s("svg", { width: 0, height: 0, style: "position:absolute", "aria-hidden": "true" },
    s("defs", null,
      s("pattern", { id: "hatch", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" },
        s("rect", { width: 6, height: 6, fill: "var(--hatch-bg)" }),
        s("line", { x1: 0, y1: 0, x2: 0, y2: 6, stroke: "var(--hatch)", "stroke-width": 1.5 })),
      s("pattern", { id: "hatchRed", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" },
        s("rect", { width: 6, height: 6, fill: "rgba(208,59,59,.10)" }),
        s("line", { x1: 0, y1: 0, x2: 0, y2: 6, stroke: "var(--crit)", "stroke-width": 1.5 }))));
  document.body.append(svg);
}

// ------------------------------------------------------------------ アナライザーのグラフ
const BAND_AXIS = {
  "6": { segments: [[5925, 6425]], gap: 0 },
  "5": { segments: [[5170, 5330], [5490, 5730]], gap: 18 },
  "2.4": { segments: [[2401, 2483]], gap: 0 },
};
const Y_TOP = -20, Y_BOT = -95;
const measureCtx = document.createElement("canvas").getContext("2d");
function textWidth(t, font = "600 11px system-ui") { measureCtx.font = font; return measureCtx.measureText(t).width; }

function xScale(band, x0, x1) {
  const ax = BAND_AXIS[band];
  const total = ax.segments.reduce((a, [lo, hi]) => a + hi - lo, 0);
  const avail = x1 - x0 - ax.gap * (ax.segments.length - 1);
  return (f) => {
    let off = x0;
    for (let i = 0; i < ax.segments.length; i++) {
      const [lo, hi] = ax.segments[i];
      const w = ((hi - lo) / total) * avail;
      if (f <= hi || i === ax.segments.length - 1) {
        const t = Math.max(0, Math.min(1, (f - lo) / (hi - lo)));
        if (f < lo && i > 0) return off - ax.gap / 2;
        return off + t * w;
      }
      off += w + ax.gap;
    }
    return x1;
  };
}

function stationNode(st) {
  const k = kindOf(st.kind);
  const wrap = h("div");
  wrap.append(
    h("div", { class: "v", text: st.rssi != null ? `${st.rssi} dBm` : "強さ不明" }),
    h("div", { class: "row" }, h("span", { class: `glyph ${k.cls}`, text: k.glyph }), h("b", { text: ssidText(st) })),
    h("div", { class: "r", text: `${k.label}${st.team ? `・${st.team}` : ""}${st.signature ? `・${st.signature}` : ""}` }),
    h("div", { class: "r", text: `主${chLabel(st.freq)}・${st.width_label}MHz（${spanLabel(st.spans)}）` }),
  );
  if (st.model) wrap.append(h("div", { class: "r", text: `機種推定：${st.model}` }));
  wrap.append(h("div", { class: "r", text: `最終確認 ${ago(st.age_s)}${st.present ? "" : "（いない扱い）"}・${st.bssid}` }));
  if (st.alerts && st.alerts.length) wrap.append(h("div", { class: "r", text: `警報：${st.alerts.map((a) => ALERT_TYPE_JP[a] || a).join("、")}` }));
  if (st.new) wrap.append(h("div", { class: "r", text: "新規参入（自分の帯域に新しく現れた）" }));
  wrap.append(h("div", { class: "r", text: "クリックで詳細" }));
  return [wrap];
}

function channelNode(row, nStations) {
  const out = h("div");
  out.append(h("div", { class: "v", text: chLabel(row.freq) + (row.psc ? "（PSC）" : "") }));
  out.append(h("div", { class: "r", text: STATE_JP[row.state] || row.state }));
  out.append(h("div", { class: "r", text: `重なる局 ${row.n_overlap}（主chが ${row.n_primary}・強い ${row.n_strong}）${row.max_rssi != null ? `・最大 ${row.max_rssi}dBm` : ""}` }));
  if (row.rate != null) out.append(h("div", { class: "r", text: `活動量 約${row.rate}フレーム/秒（受信した秒数あたり）` }));
  out.append(h("div", { class: "r", text: `最終受信 ${ago(row.observed_ago)}${row.revisit_s ? `・再訪 ${row.revisit_s}秒ごと` : ""}` }));
  if (row.reserved.length) out.append(h("div", { class: "r", text: `予約：${row.reserved.join("、")}` }));
  if (row.problem) out.append(h("div", { class: "r", text: "強い局が3つ以上重なっている（問題ch）" }));
  return [out];
}

function renderSpectrum(container, band, opt) {
  const { stations, channels, selected, height = 300, maxLabels = 14, rows = true, ownSpans = [], blockSpan = null, strong = -70, interactive = true } = opt;
  const W = Math.max(320, container.clientWidth || 800);
  const L = 40, R = 10, T = 26, H = height;
  const xb = T + H;
  const rowsH = rows ? 40 : 18;
  const totalH = xb + rowsH;
  const x = xScale(band, L, W - R);
  const y = (d) => T + ((Y_TOP - Math.max(Y_BOT, Math.min(Y_TOP, d))) / (Y_TOP - Y_BOT)) * H;
  const svg = s("svg", { viewBox: `0 0 ${W} ${totalH}`, height: totalH, role: "img",
    "aria-label": `${BAND_JP[band]}のアナライザー。局${stations.filter((q) => q.present).length}`});
  const cw = band === "2.4" ? 5 : 20;
  const colX = (f) => [x(f - cw / 2), x(f + cw / 2)];

  // 格子と軸
  for (let d = -90; d <= -30; d += 10) {
    svg.append(s("line", { class: "gridline", x1: L, x2: W - R, y1: y(d), y2: y(d) }));
    svg.append(s("text", { class: "ax-label", x: L - 6, y: y(d) + 4, "text-anchor": "end", text: d }));
  }
  svg.append(s("text", { class: "ax-label", x: L - 6, y: T - 10, "text-anchor": "end", text: "dBm" }));
  // 自分の帯域・受信中の160MHz
  for (const [lo, hi] of ownSpans) {
    svg.append(s("rect", { class: "ownband", x: x(lo), y: T, width: Math.max(2, x(hi) - x(lo)), height: H }));
    svg.append(s("line", { class: "ownband-edge", x1: x(lo), x2: x(lo), y1: T, y2: xb }));
    svg.append(s("line", { class: "ownband-edge", x1: x(hi), x2: x(hi), y1: T, y2: xb }));
    svg.append(s("text", { class: "ownband-label", x: (x(lo) + x(hi)) / 2, y: T - 3, "text-anchor": "middle", text: "自分" }));
  }
  if (blockSpan) {
    const [lo, hi] = blockSpan, yy = xb - 3;
    svg.append(s("path", { class: "blockbracket", d: `M${x(lo) + 1},${yy - 5} V${yy} H${x(hi) - 1} V${yy - 5}` }));
  }
  // 問題chと「見ていない」ch
  for (const r of channels) {
    const [a, b] = colX(r.freq);
    if (r.problem) {
      svg.append(s("rect", { class: "problem-col", x: a, y: T, width: b - a, height: H }));
      svg.append(s("text", { class: "problem-mark", x: (a + b) / 2, y: T - 12, "text-anchor": "middle", text: "!" }));
    }
    const st = r.observed_ago == null && !BLIND.has(r.state) && r.state !== "own" ? "unseen" : r.state;
    if (BLIND.has(st)) {
      svg.append(s("rect", { class: st === "stopped" ? "blind-col cell stopped" : "blind-col", x: a, y: T, width: b - a, height: H, opacity: 0.85 }));
      if (b - a >= 16 && band !== "2.4") {
        const label = { excluded: "巡回外", paused: "停止中", scan_target: "待ち", stopped: "受信停止", unseen: "未観測" }[st];
        svg.append(s("text", { class: "blind-text", x: (a + b) / 2, y: T + H / 2, "text-anchor": "middle",
          transform: `rotate(-90 ${(a + b) / 2} ${T + H / 2})`, text: label }));
      }
    }
  }
  svg.append(s("line", { class: "axisline", x1: L, x2: W - R, y1: xb, y2: xb }));
  // 「強い」の目安
  svg.append(s("line", { class: "thresh", x1: L, x2: W - R, y1: y(strong), y2: y(strong) }));
  svg.append(s("text", { class: "thresh-label", x: W - R - 2, y: y(strong) - 3, "text-anchor": "end", text: `強い ${strong}dBm` }));

  // 局（弱い順に描いて強い局を上に。選んだ局は最後）
  const anySel = selected && stations.some((q) => q.bssid === selected);
  const ordered = [...stations].sort((p, q) => (p.bssid === selected) - (q.bssid === selected) || (p.rssi ?? -200) - (q.rssi ?? -200));
  const geo = [];
  for (const st of ordered) {
    if (st.rssi == null) continue;
    const k = kindOf(st.kind);
    const yt = y(st.rssi);
    const cls = ["st-shape", k.cls];
    if (st.alerts.length) cls.push("alerting");
    if (!st.present) cls.push("gone");
    if (anySel && st.bssid !== selected) cls.push("dim");
    if (st.bssid === selected) cls.push("selected");
    const g = s("g", { class: cls.join(" ") });
    for (const [lo, hi] of st.spans) {
      const xa = x(lo), xz = x(hi);
      const sl = Math.min(6, (xz - xa) * 0.12);
      g.append(s("path", { d: `M${xa},${xb} L${xa + sl},${yt} L${xz - sl},${yt} L${xz},${xb} Z`, fill: "currentColor", stroke: "none" }));
      g.append(s("path", { d: `M${xa},${xb} L${xa + sl},${yt} L${xz - sl},${yt} L${xz},${xb}`, fill: "none", stroke: "currentColor" }));
      geo.push({ st, xa, xz, yt });
    }
    svg.append(g);
  }
  // 選んで付ける名前（全部には付けない）
  const placed = [];
  const prio = (st) => (st.bssid === selected ? 0 : st.alerts.length ? 1 : st.kind === "own" ? 2 : st.new ? 3 : 4);
  const cands = [...stations].filter((q) => q.rssi != null && q.present)
    .sort((p, q) => prio(p) - prio(q) || q.rssi - p.rssi);
  let nLabels = 0;
  const labeled = new Set();
  for (const st of cands) {
    if (nLabels >= maxLabels) break;
    const keyName = st.ssid || st.bssid;
    if (labeled.has(keyName) && prio(st) >= 4) continue; // 同じ名前は1回だけ（RoboconFreeの乱立など）
    const [lo, hi] = st.spans[0];
    let cx = (x(lo) + x(hi)) / 2;
    const yt = y(st.rssi);
    let label = ssidText(st);
    if (label.length > 18) label = label.slice(0, 17) + "…";
    if (st.new) label += " NEW";
    if (st.alerts.length) label = "⚠ " + label;
    const tw = textWidth(label) + 4;
    cx = Math.max(L + tw / 2, Math.min(W - R - tw / 2, cx));
    let placedY = null;
    for (const dy of [5, 19, 33]) {
      const box = { x0: cx - tw / 2, x1: cx + tw / 2, y0: yt - dy - 11, y1: yt - dy + 2 };
      if (box.y0 < 2) continue;
      if (!placed.some((b) => b.x0 < box.x1 && box.x0 < b.x1 && b.y0 < box.y1 && box.y0 < b.y1)) {
        placed.push(box);
        placedY = yt - dy;
        break;
      }
    }
    if (placedY == null) continue;
    nLabels++;
    labeled.add(keyName);
    const t = s("text", { class: "st-label", x: cx, y: placedY, "text-anchor": "middle", text: label });
    if (anySel && st.bssid !== selected) t.setAttribute("opacity", 0.3);
    svg.append(t);
  }

  // 軸の下：ch番号・見ているか・予約
  const colW = (() => { const [a, b] = colX(channels[0]?.freq ?? 0); return b - a; })();
  const step = colW >= textWidth(band === "5" ? "144" : "88", "11px system-ui") + 5 ? 1 : 2;
  channels.forEach((r, i) => {
    const [a, b] = colX(r.freq);
    const cx = (a + b) / 2;
    if (i % step === 0 || r.own) {
      svg.append(s("text", { class: "ax-label" + (r.own ? " own" : ""), x: cx, y: xb + 12, "text-anchor": "middle", text: r.ch }));
    }
    if (rows) {
      const st = r.observed_ago == null && !BLIND.has(r.state) && r.state !== "own" ? "unseen" : r.state;
      const fresh = r.state === "patrol" && r.observed_ago != null && r.revisit_s && r.observed_ago <= r.revisit_s * 1.2;
      svg.append(s("rect", { class: `cell ${st}${fresh ? " fresh" : ""}`, x: a + 1, y: xb + 17, width: Math.max(1, b - a - 2), height: 9, rx: 2 }));
      if (r.reserved.length) {
        const own = r.reserved.includes(ui.state?.own?.team);
        svg.append(s("rect", { class: `res-mark ${own ? "res-own" : "res-team"}`, x: a + 3, y: xb + 30, width: Math.max(2, b - a - 6), height: 6, rx: 3 }));
      }
    }
  });
  if (rows) {
    svg.append(s("text", { class: "ax-row", x: L - 6, y: xb + 25, "text-anchor": "end", text: "見る" }));
    svg.append(s("text", { class: "ax-row", x: L - 6, y: xb + 36, "text-anchor": "end", text: "予約" }));
  }

  // ホバー：いちばん近い局（台形の上の辺）を拾う。無ければch
  if (interactive) {
    const overlay = s("rect", { class: "hit", x: L, y: 0, width: W - R - L, height: totalH });
    const pick = (ev) => {
      const pt = svg.getBoundingClientRect();
      const sx = W / pt.width;
      const px = (ev.clientX - pt.left) * sx, py = (ev.clientY - pt.top) * sx;
      let best = null;
      for (const gq of geo) {
        if (px < gq.xa - 2 || px > gq.xz + 2) continue;
        const d = Math.abs(py - gq.yt);
        if (d <= 14 && (!best || d < best.d)) best = { d, st: gq.st };
      }
      let row = null;
      for (const r of channels) {
        const [a, b] = colX(r.freq);
        if (px >= a && px <= b) { row = r; if (band !== "2.4") break; }
      }
      return { best, row };
    };
    const onMove = (ev) => {
      ui.pointer[band] = { x: ev.clientX, y: ev.clientY };
      const { best, row } = pick(ev);
      overlay.style.cursor = best ? "pointer" : "crosshair";
      if (best) tip.show(ev.clientX, ev.clientY, stationNode(best.st));
      else if (row) tip.show(ev.clientX, ev.clientY, channelNode(row));
      else tip.hide();
    };
    overlay.addEventListener("pointermove", onMove);
    overlay.addEventListener("pointerleave", () => { delete ui.pointer[band]; tip.hide(); });
    overlay.addEventListener("click", (ev) => {
      const { best } = pick(ev);
      select(best ? best.st.bssid : null);
    });
    svg.append(overlay);
    svg._refreshTip = () => {
      const p = ui.pointer[band];
      if (p) onMove({ clientX: p.x, clientY: p.y });
    };
  }
  clear(container).append(svg);
  if (svg._refreshTip) svg._refreshTip();
}

// ------------------------------------------------------------------ 上の帯
function renderTop(st) {
  const v = $("verdict");
  const c = st.alert_counts;
  const blind = blindSummary(st);
  let lvl = "none", icon = "○", text = "検知なし", sub = blind.length ? `見ていない：${blind.join("・")}` : "ピットから見えた範囲で";
  if (st.rx.state === "failed") { lvl = "crit"; icon = "■"; text = "受信停止"; sub = st.rx.reason || "2.4/5GHzでフレームなし"; }
  else if (c.crit) { lvl = "crit"; icon = "■"; text = `緊急 ${c.crit}`; sub = st.alerts.find((a) => a.level === "crit")?.title || ""; if (c.warn) sub += `・警告 ${c.warn}`; }
  else if (c.warn) { lvl = "warn"; icon = "▲"; text = `警告 ${c.warn}`; sub = st.alerts.find((a) => a.level === "warn")?.title || ""; }
  else if (st.manual && st.manual.state !== "done") { lvl = "blind"; icon = "◌"; text = "検知なし"; sub = "手動スキャン中は自chを見ていません"; }
  v.className = `verdict lvl-${lvl}`;
  v.querySelector(".verdict-icon").textContent = icon;
  v.querySelector(".verdict-text").textContent = text;
  v.querySelector(".verdict-sub").textContent = sub;

  const own = st.own;
  clear($("ownSummary")).append(
    h("span", { class: "big", text: `自ch ${own.label}・${own.width}MHz` }),
    h("span", { class: "small", text: `${MODE_JP[st.mode]}・受信 ${own.block.label}${st.mode !== "PRE" ? `・自chの受信率 ${Math.round(own.ratio * 100)}%` : "・自chは約14秒に0.25秒"}` }));
  const rx = st.rx.state;
  clear($("rxState")).append(h("span", { class: `dot ${rx === "ok" ? "ok" : rx === "failed" ? "crit" : "warn"}` }),
    h("span", { text: RX_JP[rx] || rx }));
  for (const b of $("modeSwitch").querySelectorAll("button")) b.setAttribute("aria-checked", String(b.dataset.mode === st.mode));
  clear($("coverage")).append(...blind.map((t) => h("span", { class: "chip-blind", title: "この帯域・chは見ていません（検知なし、ではありません）", text: `見ていない：${t}` })));
  const links = clear($("links"));
  if (st.notify) {
    const n = st.notify;
    const ok = n.backlog === 0;
    links.append(h("span", { title: n.last_error && Object.values(n.last_error).join(" / ") || "" },
      h("span", { class: `dot ${!n.ntfy ? "" : ok ? "ok" : "warn"}` }), n.ntfy ? (ok ? "通知 送信済み" : `通知 未送 ${n.backlog}`) : "通知 未設定"));
    if (n.healthcheck) links.append(h("span", null, h("span", { class: `dot ${n.health_ok ? "ok" : n.health_ok === false ? "warn" : ""}` }), "死活監視"));
  }
  const r = st.relay || {};
  links.append(h("span", { title: r.last_error || "" }, h("span", { class: `dot ${!r.configured ? "" : r.ok ? "ok" : "warn"}` }),
    !r.configured ? "中継 未設定" : r.ok ? `中継 ${r.fg ? "アプリ表示中" : "接続"}` : "中継 届かない"));
  $("clock").textContent = tJST(st.now);

  const sb = $("scanbar");
  if (st.manual && st.manual.state !== "done") {
    const m = st.manual;
    clear(sb).append(h("span", { text: `手動スキャン中（${m.origin === "app" ? "アプリ" : "画面"}から）` }),
      h("span", { class: "bar" }, h("i", { style: `width:${Math.round((m.done / m.total) * 100)}%` })),
      h("span", { text: `${m.done}/${m.total}ch・残り ${m.remaining_s.toFixed(1)}秒` }),
      h("span", { text: "（この間、自chと巡回は止めています）" }));
    sb.hidden = false;
  } else sb.hidden = true;
}

function blindSummary(st) {
  const out = [];
  if (st.radio && st.radio.scan_only) out.push("活動量・リトライ率・deauth・160MHz受信（スキャンだけのモード）");
  const unrx = (st.radio && st.radio.unreceivable) || [];
  for (const b of ["6", "5", "2.4"]) {
    const n = unrx.filter((f) => bandOf(f) === b).length;
    if (!n) continue;
    out.push(n === CHANNEL_COUNT[b] ? `${BAND_JP[b]}（この無線では受けられない）` : `${BAND_JP[b]}の${n}ch（この無線では受けられない）`);
  }
  for (const b of st.excluded_bands) out.push(`${BAND_JP[b]}（巡回外）`);
  const ex = st.excluded_freqs.filter((f) => !st.excluded_bands.includes(bandOf(f)));
  if (ex.length) out.push(`${ex.length}ch（巡回外）`);
  if (st.rx.state === "failed") out.push("全帯域（受信停止）");
  if (st.manual && st.manual.state !== "done") out.push("自ch（手動スキャン中）");
  return out;
}

// ------------------------------------------------------------------ 凡例とグラフの見出し
function renderLegend() {
  const lg = clear($("legend"));
  for (const [k, label] of LEGEND) {
    const kk = kindOf(k);
    lg.append(h("span", { class: "item" }, h("span", { class: `key ${kk.cls}` }), h("span", { class: `glyph ${kk.cls}`, text: kk.glyph }), label));
  }
  lg.append(h("span", { class: "item" }, h("span", { class: "sw-own" }), "自分の帯域"),
    h("span", { class: "item" }, h("span", { class: "sw-hatch" }), "見ていないch（巡回外・停止中・未観測）"),
    h("span", { class: "item" }, h("span", { class: "sw-problem" }), "強い局が3つ以上（!）"),
    h("span", { class: "item", text: "⚠ 警報の当事者・薄い線は最近いない局" }));
}

function bandStats(el, st, band) {
  const sts = st.stations.filter((q) => q.band === band && q.present);
  const rows = st.channels.filter((r) => r.band === band);
  const strong = sts.filter((q) => q.rssi != null && q.rssi >= st.thresholds.strong_rssi_dbm).length;
  const blind = rows.filter((r) => BLIND.has(r.state) || (r.observed_ago == null && r.state !== "own")).length;
  const wide = sts.filter((q) => q.width >= 160).length;
  clear(el).append(...[
    h("span", null, "いる局 ", h("b", { text: sts.length })),
    h("span", null, "強い ", h("b", { text: strong })),
    wide ? h("span", null, "160MHz以上 ", h("b", { text: wide })) : null,
    h("span", null, "見ていないch ", h("b", { text: `${blind}/${rows.length}` }))].filter(Boolean));
}

function renderCharts(st) {
  const ownRows = st.channels.filter((r) => r.own_band);
  const ownSpans = mergeSpans(ownRows.map((r) => [r.freq - 10, r.freq + 10]));
  const blk = st.own.block;
  const blockSpan = blk.width > 20 ? [blk.center - blk.width / 2, blk.center + blk.width / 2] : null;
  const ownBand = bandOf(st.own.freq);
  const common = { selected: ui.selected, strong: st.thresholds.strong_rssi_dbm };
  for (const [band, el, hgt, ml] of [["6", "chart6", 300, 14], ["5", "chart5", 150, 5], ["2.4", "chart24", 150, 4]]) {
    renderSpectrum($(el), band, {
      ...common, height: hgt, maxLabels: ml,
      stations: st.stations.filter((q) => q.band === band),
      channels: st.channels.filter((r) => r.band === band),
      ownSpans: band === ownBand ? ownSpans : [], blockSpan: band === ownBand ? blockSpan : null,
    });
  }
  bandStats($("stats6"), st, "6");
  bandStats($("stats5"), st, "5");
  bandStats($("stats24"), st, "2.4");
}

function mergeSpans(spans) {
  const sorted = spans.sort((a, b) => a[0] - b[0]);
  const out = [];
  for (const sp of sorted) {
    const last = out[out.length - 1];
    if (last && sp[0] <= last[1]) last[1] = Math.max(last[1], sp[1]);
    else out.push([...sp]);
  }
  return out;
}

// ------------------------------------------------------------------ 右の列
function renderAlerts(st) {
  const el = clear($("alerts"));
  $("alertsSub").textContent = st.alerts.length ? `${st.alerts.length}件` : "";
  if (!st.alerts.length) {
    const blind = blindSummary(st);
    el.append(h("div", { class: "empty-ok" }, h("div", { class: "big", text: "検知なし" }),
      h("div", { class: "small", text: blind.length ? `ただし見ていない：${blind.join("・")}` : "ピットから見えた範囲で、警報の条件は成り立っていません（安全という意味ではありません）" })));
    return;
  }
  const byB = new Map(st.stations.map((q) => [q.bssid, q]));
  for (const a of st.alerts) {
    const card = h("div", { class: `alert-card ${a.level}` },
      h("div", { class: "ico", "aria-hidden": "true", text: a.level === "crit" ? "■" : "▲" }),
      h("div", { class: "t" }, h("span", { class: "lvl", text: a.level === "crit" ? "緊急" : "警告" }), a.title),
      h("div", { class: "m", text: a.message }),
      h("div", { class: "meta" }, h("span", { text: `${tJST(a.since)}から（${ago(st.now - a.since)}）` })));
    const meta = card.querySelector(".meta");
    for (const p of a.parties.slice(0, 6)) {
      const q = byB.get(p.id);
      const k = kindOf(p.kind);
      meta.append(h("button", { type: "button", class: "party", title: p.bssid || "", onclick: () => p.bssid && select(p.bssid) },
        h("span", { class: `glyph ${k.cls}`, text: k.glyph }), `${p.ssid || p.bssid || "?"}・${(p.ch || "").replace("GHz ", "G ")}${q && q.rssi != null ? `・${q.rssi}dBm` : ""}`));
    }
    if (a.parties.length > 6) meta.append(h("span", { text: `ほか${a.parties.length - 6}` }));
    el.append(card);
  }
}

function renderOwnTile(st) {
  const own = st.own;
  const el = clear($("ownTile"));
  $("ownSub").textContent = `${own.label}・受信した秒数あたり`;
  const tile = h("div", { class: "own-tile" },
    h("div", null, h("div", { class: "fig" }, own.rate != null ? String(Math.round(own.rate)) : "—", h("span", { class: "unit", text: " /秒" })), h("div", { class: "lab", text: "フレーム（自分以外）" })),
    h("div", null, h("div", { class: "fig" }, own.retry != null ? String(Math.round(own.retry * 100)) : "—", h("span", { class: "unit", text: " %" })), h("div", { class: "lab", text: "リトライ率" })),
    h("div"));
  const sp = h("div", { class: "spark" });
  tile.append(sp);
  el.append(tile);
  const pts = own.series.filter((p) => p.rate != null);
  const W = Math.max(240, sp.clientWidth || 320), H = 64, P = 4;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H + 14}`, height: H + 14, role: "img", "aria-label": "自chの活動量（直近10分）" });
  if (pts.length >= 2) {
    const t0 = st.now - 600, t1 = st.now;
    const max = Math.max(100, ...pts.map((p) => p.rate)) * 1.1;
    const X = (t) => P + ((t - t0) / (t1 - t0)) * (W - 2 * P);
    const Y = (v) => P + (1 - v / max) * (H - 2 * P);
    const d = pts.map((p, i) => `${i ? "L" : "M"}${X(p.t).toFixed(1)},${Y(p.rate).toFixed(1)}`).join(" ");
    svg.append(s("line", { class: "gridline", x1: P, x2: W - P, y1: H - P, y2: H - P }));
    svg.append(s("path", { class: "area", d: `${d} L${X(pts[pts.length - 1].t)},${H - P} L${X(pts[0].t)},${H - P} Z` }));
    svg.append(s("path", { class: "line", d }));
    const last = pts[pts.length - 1];
    svg.append(s("circle", { class: "end", cx: X(last.t), cy: Y(last.rate), r: 4 }));
    svg.append(s("text", { class: "ax-label", x: P, y: H + 12, text: "10分前" }));
    svg.append(s("text", { class: "ax-label", x: W - P, y: H + 12, "text-anchor": "end", text: "今" }));
    svg.append(s("text", { class: "ax-label", x: P + 2, y: P + 9, text: `${Math.round(max)}/秒` }));
  } else {
    svg.append(s("text", { class: "ax-label", x: 4, y: 30, text: "まだ十分なデータがありません" }));
  }
  sp.append(svg);
}

function renderSuggestions(st) {
  const el = clear($("suggestions"));
  if (!st.suggestions.length) { el.append(h("li", { text: "候補なし" })); return; }
  for (const c of st.suggestions) {
    el.append(h("li", null,
      h("div", { class: "l" }, c.label, c.own_reserved ? h("span", { class: "badge own", text: "自チームの予約" }) : null,
        c.reserved_by.length ? h("span", { class: "badge res", text: "他チームの予約" }) : null,
        c.unverified ? h("span", { class: "badge unv", text: "未確認" }) : null),
      h("div", { class: "w", text: c.why })));
  }
}

function renderCalibration(st) {
  const card = $("calCard");
  const cal = st.calibration;
  if (!cal || st.mode !== "PRE") { card.hidden = true; return; }
  card.hidden = false;
  $("calSub").textContent = `${tJST(cal.at)}・${cal.rounds}周`;
  const el = clear($("calibration"));
  const t = h("table", { class: "data" }, h("thead", null, h("tr", null, ...["帯域", "局", "強い", "中央値", "最大"].map((x) => h("th", { text: x })))));
  const tb = h("tbody");
  for (const b of ["6", "5", "2.4"]) {
    const r = cal.bands[b];
    tb.append(h("tr", null, h("td", { text: BAND_JP[b] }), h("td", { class: "num", text: r.stations }), h("td", { class: "num", text: r.strong }),
      h("td", { class: "num", text: r.rssi_p50 ?? "—" }), h("td", { class: "num", text: r.rssi_max ?? "—" })));
  }
  t.append(tb);
  el.append(t, h("ul", { class: "hints" }, ...cal.hints.map((x) => h("li", { text: x }))));
}

// ------------------------------------------------------------------ 選択と詳細
function select(bssid) {
  ui.selected = bssid;
  prefs.set("selected", bssid);
  if (ui.state) { renderCharts(ui.state); if (ui.tab === "stations") renderStations(ui.state); }
  if (bssid) openDrawer(bssid); else $("drawer").hidden = true;
}

async function openDrawer(bssid) {
  $("drawer").hidden = false;
  try {
    ui.drawerDetail = await api(`/api/stations/${encodeURIComponent(bssid)}`);
    renderDrawer();
  } catch (e) {
    clear($("drawerBody")).append(h("p", { text: `読めませんでした：${e.message}` }));
  }
}

function renderDrawer() {
  const d = ui.drawerDetail;
  if (!d) return;
  const k = kindOf(d.kind);
  const body = clear($("drawerBody"));
  body.append(
    h("div", { class: "row" }, h("span", { class: `glyph ${k.cls}`, text: k.glyph + " " }), h("span", { text: k.label })),
    h("div", { class: "ssid", text: d.ssid || "（非公開SSID）" }),
    h("div", { class: "mono", text: d.bssid }));
  const facts = h("dl", { class: "facts" });
  const add = (a, b) => facts.append(h("dt", { text: a }), h("dd", { text: b ?? "—" }));
  add("主ch・幅", `${chLabel(d.freq)}・${d.width_label}MHz（${spanLabel(d.spans)}）${d.dup_beacon ? "・重複ビーコン" : ""}${d.suspect ? "・IEが矛盾" : ""}`);
  add("強さ", d.rssi_avg != null ? `平均 ${d.rssi_avg}dBm（最新 ${d.rssi}・最大 ${d.rssi_max}）` : "—");
  add("チーム", d.team ? `${d.team}（${{ config: "設定", tag: "手動タグ", ssid: "SSIDのパターン", rnr: "RNRで紐付け", signature: "署名" }[d.team_source] || d.team_source}）` : "—");
  add("署名", d.signature);
  add("機種推定", d.model_guess);
  add("規格・暗号", `802.11${d.standard}・${d.security}${d.pmf_required ? "（PMF必須）" : ""}`);
  add("最終確認", `${ago(d.age_s)}（${tJST(d.last_seen)}）・この局を「いる」とみなす時間 ${d.window_s}秒`);
  add("初めて見た", dJST(d.first_seen));
  add("受けたch", (d.seen_on || []).map((f) => `ch${chNum(f)}`).join("、"));
  add("ビーコン・データ", `${d.beacons}・${d.data_frames}`);
  if (d.moved_from) add("chの移動", `${chLabel(d.moved_from)} → ${chLabel(d.freq)}（${tJST(d.moved_at)}）`);
  body.append(h("section", null, facts));
  if (d.alerts.length) {
    body.append(h("section", null, h("h3", { text: "関係する警報" }),
      ...d.alerts.map((a) => h("div", { class: `alert-card ${a.level}` }, h("div", { class: "ico", text: a.level === "crit" ? "■" : "▲" }),
        h("div", { class: "t", text: a.title }), h("div", { class: "m", text: a.message })))));
  }
  if (d.hints.length) body.append(h("section", null, h("h3", { text: "機種推定の手がかり" }), h("ul", { class: "hints" }, ...d.hints.map((x) => h("li", { text: x })))));
  if (d.linked.length || d.rnr.length) {
    const sec = h("section", null, h("h3", { text: "同じ機器（RNR）" }));
    for (const b of d.linked) sec.append(h("button", { class: "party", type: "button", onclick: () => select(b), text: b }));
    for (const r of d.rnr) sec.append(h("div", { class: "sub", text: `知らせている局：${r.bssid || "（BSSIDなし）"}・${r.freq ? chLabel(r.freq) : `op${r.op_class} ch${r.channel}`}` }));
    body.append(sec);
  }
  // チーム名のタグ
  const teams = (ui.state?.teams || []).map((t) => t.name);
  const sel = h("select", { "aria-label": "チーム名" }, h("option", { value: "", text: "（選ぶ）" }), ...teams.map((t) => h("option", { value: t, text: t })));
  const free = h("input", { type: "text", placeholder: "または名前を入力", maxlength: 40 });
  if (d.team && d.team_source === "tag") { if (teams.includes(d.team)) sel.value = d.team; else free.value = d.team; }
  const save = h("button", { class: "btn primary", type: "button", text: "タグを付ける", onclick: async () => {
    const team = free.value.trim() || sel.value;
    if (!team) return toast("チーム名を選ぶか入力してください", true);
    try { await api("/api/tags", { method: "POST", body: { bssid: d.bssid, team } }); toast(`${team} のタグを付けました（以後は割当違反の判定に回ります）`); openDrawer(d.bssid); }
    catch (e) { toast(e.message, true); }
  } });
  const rm = h("button", { class: "btn", type: "button", text: "タグを外す", onclick: async () => {
    try { await api("/api/tags", { method: "POST", body: { bssid: d.bssid, team: null } }); toast("タグを外しました"); openDrawer(d.bssid); }
    catch (e) { toast(e.message, true); }
  } });
  body.append(h("section", null, h("h3", { text: "チーム名のタグ（手動）" }), h("div", { class: "toolbar" }, sel, free), h("div", { class: "toolbar" }, save, d.team_source === "tag" ? rm : null)));
  if (d.clients.length) {
    body.append(h("section", null, h("h3", { text: `端末（${d.clients.length}）` }),
      ...d.clients.slice(0, 20).map((c) => h("div", { class: "mono", text: `${c.mac}${c.random_mac ? "（ランダム）" : ""}・${c.rssi ?? "—"}dBm・${ago(ui.state.now - c.last_seen)}` }))));
  }
}

// ------------------------------------------------------------------ 下のタブ：局
const ST_COLS = [
  ["kind", "種類"], ["ssid", "SSID"], ["team", "チーム・署名"], ["model", "機種推定"], ["freq", "主ch"], ["width", "幅"],
  ["rssi", "強さ"], ["standard", "規格"], ["security", "暗号"], ["age_s", "最終"], ["alerts", "警報"],
];
function renderStations(st) {
  const el = $("pStations");
  const f = ui.stFilter;
  if (!el.dataset.built) {
    el.dataset.built = "1";
    const segBand = segControl([["all", "全部"], ["6", "6GHz"], ["5", "5GHz"], ["2.4", "2.4GHz"]], f.band, (v) => { f.band = v; prefs.set("stFilter", f); renderStations(ui.state); });
    const segKind = segControl([["all", "全部"], ["team", "▲他高専"], ["nonneg", "■交渉不可"], ["unknown", "◆不明"], ["own", "●自チーム"]], f.kind, (v) => { f.kind = v; prefs.set("stFilter", f); renderStations(ui.state); });
    const pres = h("label", { class: "switch" }, h("input", { type: "checkbox", checked: f.present, onchange: (e) => { f.present = e.target.checked; prefs.set("stFilter", f); renderStations(ui.state); } }), "いる局だけ");
    const q = h("input", { type: "search", placeholder: "SSID・BSSID・チーム・機種で絞り込み", value: f.q, oninput: (e) => { f.q = e.target.value; prefs.set("stFilter", f); renderStations(ui.state); } });
    el.append(h("div", { class: "toolbar" }, segBand, segKind, pres, h("span", { class: "grow" }), q), h("div", { class: "table-wrap", id: "stTable" }));
  }
  let rows = st.stations.filter((x) => (f.band === "all" || x.band === f.band)
    && (f.kind === "all" || (f.kind === "nonneg" ? ["official_infra", "broadcast"].includes(x.kind) : x.kind === f.kind))
    && (!f.present || x.present));
  if (f.q) {
    const q = f.q.toLowerCase();
    rows = rows.filter((x) => [x.ssid, x.bssid, x.team, x.model, x.signature].some((v) => v && String(v).toLowerCase().includes(q)));
  }
  const { key, asc } = ui.stSort;
  const val = (x) => (key === "alerts" ? x.alerts.length : key === "kind" ? x.kind : x[key]);
  rows.sort((a, b) => {
    const va = val(a), vb = val(b);
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    return (va < vb ? -1 : va > vb ? 1 : 0) * (asc ? 1 : -1);
  });
  const table = h("table", { class: "data" });
  const tr = h("tr");
  for (const [k, label] of ST_COLS) {
    tr.append(h("th", { class: key === k ? `sorted${asc ? " asc" : ""}` : null, text: label, onclick: () => {
      ui.stSort = { key: k, asc: key === k ? !asc : k !== "rssi" && k !== "alerts" };
      prefs.set("stSort", ui.stSort);
      renderStations(ui.state);
    } }));
  }
  table.append(h("thead", null, tr));
  const tb = h("tbody");
  for (const x of rows.slice(0, 400)) {
    const k = kindOf(x.kind);
    const w = Math.max(2, Math.min(60, ((x.rssi ?? -95) + 95) * 0.9));
    tb.append(h("tr", { class: [x.bssid === ui.selected ? "sel" : "", x.present ? "" : "gone"].join(" "), onclick: () => select(x.bssid) },
      h("td", null, h("span", { class: "kind-cell" }, h("span", { class: `glyph ${k.cls}`, text: k.glyph }), k.label.replace(/（.*）/, ""))),
      h("td", { title: x.bssid }, ssidText(x), x.new ? h("span", { class: "badge new", text: "NEW" }) : null),
      h("td", { text: x.team || x.signature || "—" }),
      h("td", { text: x.model || "—" }),
      h("td", { class: "num", text: chLabel(x.freq).replace("GHz ", "G ") }),
      h("td", { class: "num", text: `${x.width_label}` }),
      h("td", { class: "num" }, h("span", { class: "rssi" }, h("i", { style: `width:${w}px` }), x.rssi != null ? `${x.rssi}` : "—")),
      h("td", { text: x.standard }),
      h("td", { text: x.security }),
      h("td", { class: "num", text: ago(x.age_s) }),
      h("td", { text: x.alerts.map((a) => ALERT_TYPE_JP[a] || a).join("、") || "" })));
  }
  table.append(tb);
  clear($("stTable")).append(table);
  if (!rows.length) $("stTable").append(h("p", { class: "sub", style: "padding:8px", text: "該当する局はありません" }));
}

function segControl(items, value, onChange) {
  const seg = h("div", { class: "seg", role: "group" });
  for (const [v, label] of items) {
    seg.append(h("button", { type: "button", "aria-pressed": String(v === value), text: label, onclick: (e) => {
      for (const b of seg.children) b.setAttribute("aria-pressed", String(b === e.currentTarget));
      onChange(v);
    } }));
  }
  return seg;
}

// ------------------------------------------------------------------ 端末
async function renderClients(force = false) {
  const el = $("pClients");
  if (force || Date.now() - ui.clients.lastFetch > 3000) {
    try { ui.clients.list = await api("/api/clients"); ui.clients.lastFetch = Date.now(); } catch { /* 次回 */ }
  }
  const byB = new Map((ui.state?.stations || []).map((q) => [q.bssid, q]));
  const t = h("table", { class: "data" }, h("thead", null, h("tr", null, ...["MAC", "つながっている局", "強さ", "フレーム", "最終", "ランダムMAC"].map((x) => h("th", { text: x })))));
  const tb = h("tbody");
  for (const c of ui.clients.list) {
    const st = byB.get(c.bssid);
    tb.append(h("tr", { onclick: () => c.bssid && select(c.bssid) },
      h("td", { class: "mono", text: c.mac }), h("td", { text: st ? `${ssidText(st)}（${c.bssid}）` : c.bssid || "—" }),
      h("td", { class: "num", text: c.rssi ?? "—" }), h("td", { class: "num", text: c.frames }),
      h("td", { class: "num", text: ago((ui.state?.now || 0) - c.last_seen) }), h("td", { text: c.random_mac ? "はい" : "" })));
  }
  t.append(tb);
  clear(el).append(h("p", { class: "sub", text: "データフレームのMACヘッダだけから作った一覧です（中身は読みません）。" }), h("div", { class: "table-wrap" }, t));
}

// ------------------------------------------------------------------ 記録
async function renderRecords(force = false) {
  const el = $("pRecords");
  const R = ui.records;
  if (!el.dataset.built) {
    el.dataset.built = "1";
    const locs = ui.state?.locations || [];
    const kind = h("select", { onchange: (e) => { R.filter.kind = e.target.value; renderRecords(true); } },
      h("option", { value: "", text: "種類：全部" }), h("option", { value: "pc_scan", text: "PCの手動スキャン" }), h("option", { value: "walk", text: "歩測" }));
    const loc = h("select", { onchange: (e) => { R.filter.location = e.target.value; renderRecords(true); } },
      h("option", { value: "", text: "場所：全部" }), ...locs.map((l) => h("option", { value: l, text: l })));
    const band = h("select", { onchange: (e) => { R.filter.band = e.target.value; renderRecords(true); } },
      h("option", { value: "", text: "帯域：全部" }), ...["6", "5", "2.4"].map((b) => h("option", { value: b, text: BAND_JP[b] })));
    el.append(h("div", { class: "toolbar" }, kind, loc, band, h("span", { class: "grow" }),
      h("a", { class: "btn", href: "/api/records/export?format=json", text: "JSONで書き出す" }),
      h("a", { class: "btn", href: "/api/records/export?format=csv", text: "CSVで書き出す" })),
      h("div", { class: "records" }, h("div", { class: "rec-list", id: "recList" }), h("div", { id: "recDetail" }, h("p", { class: "sub", text: "左の一覧から記録を選んでください。" }))));
  }
  if (force || Date.now() - R.lastFetch > 5000) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(R.filter)) if (v) q.set(k, v);
    try { R.list = await api(`/api/records?${q}`); R.lastFetch = Date.now(); } catch (e) { toast(e.message, true); }
  }
  const list = clear($("recList"));
  if (!R.list.length) list.append(h("p", { class: "sub", style: "padding:8px", text: "記録はまだありません（手動スキャンか歩測で作られます）" }));
  for (const r of R.list) {
    list.append(h("div", { class: `rec-item${r.id === R.selected ? " sel" : ""}`, onclick: () => { R.selected = r.id; loadRecord(r.id); renderRecords(); } },
      h("div", { class: "t", text: `${r.kind === "walk" ? "歩測" : "手動スキャン"}・${dJST(r.created_at)}` }),
      h("div", { class: "s" }, `${r.label.location || "場所なし"}・${r.n_stations}局・${r.bands.map((b) => BAND_JP[b]).join("/")}・${r.origin === "app" ? "アプリ" : "画面"}`),
      r.label.tags.length ? h("div", null, ...r.label.tags.map((t) => h("span", { class: "tag", text: t }))) : null));
  }
}

async function loadRecord(id) {
  try { ui.records.detail = await api(`/api/records/${encodeURIComponent(id)}`); renderRecordDetail(); }
  catch (e) { toast(e.message, true); }
}

function renderRecordDetail() {
  const r = ui.records.detail;
  const el = clear($("recDetail"));
  if (!r) return;
  const lab = r.label || {};
  const locs = ui.state?.locations || [];
  const loc = h("input", { type: "text", list: "locList", value: lab.location || "", maxlength: 40 });
  const dl = h("datalist", { id: "locList" }, ...locs.map((l) => h("option", { value: l })));
  const tags = h("input", { type: "text", value: (lab.tags || []).join("、"), placeholder: "読点かカンマで区切る" });
  const memo = h("textarea", { maxlength: 1000 }, lab.memo || "");
  const save = h("button", { class: "btn primary", type: "button", text: "ラベルを保存", onclick: async () => {
    const t = tags.value.split(/[、,]/).map((x) => x.trim()).filter(Boolean);
    try {
      const res = await api(`/api/records/${encodeURIComponent(r.id)}`, { method: "PATCH", body: { location: loc.value.trim() || null, tags: t, memo: memo.value } });
      toast(res.applied ? "保存しました" : "もっと新しい編集があったので、そちらを残しました");
      loadRecord(r.id);
      renderRecords(true);
    } catch (e) { toast(e.message, true); }
  } });
  el.append(h("h3", { text: `${r.kind === "walk" ? "歩測" : "PCの手動スキャン"}・${dJST(r.created_at)}` }),
    h("div", { class: "sub", text: `${r.device || ""}・${r.origin === "app" ? "アプリから" : "画面から"}・${(r.settings?.freqs || []).length || ""}${r.settings?.freqs ? "ch" : ""}・最終更新 ${lab.updated_at ? dJST(lab.updated_at) : "—"}${lab.updated_by ? `（${lab.updated_by === "app" ? "アプリ" : "画面"}）` : ""}` }),
    h("div", { class: "label-editor" }, h("label", { text: "場所" }), h("div", null, loc, dl), h("label", { text: "タグ" }), tags, h("label", { text: "メモ" }), memo, h("span"), h("div", null, save)));
  const sts = (r.stations || []).map((x) => ({
    bssid: x.bssid, ssid: x.ssid, band: x.band || bandOf(x.freq), freq: x.freq, width: x.width || 20,
    width_label: x.width_label || String(x.width || 20), spans: x.spans || [[x.freq - (x.width || 20) / 2, x.freq + (x.width || 20) / 2]],
    rssi: x.rssi, kind: x.kind || "unknown", team: x.team, model: x.model_guess || x.model, present: true, alerts: [], age_s: 0,
  }));
  for (const band of ["6", "5", "2.4"]) {
    const bs = sts.filter((x) => x.band === band);
    if (!bs.length) continue;
    const box = h("div", { class: "chart" });
    el.append(h("h3", { text: `${BAND_JP[band]}（${bs.length}局）` }), box);
    const rows = (r.channels || []).filter((c) => (c.band || bandOf(c.freq)) === band).map((c) => ({
      freq: c.freq, band, ch: chNum(c.freq), state: "patrol", observed_ago: 0, revisit_s: null, n_overlap: c.bss_overlap ?? 0,
      n_primary: c.bss_primary ?? 0, n_strong: 0, max_rssi: c.max_rssi, rate: c.rate, reserved: [], problem: false, psc: false,
    }));
    const allRows = rows.length ? rows : chRowsFor(band);
    renderSpectrum(box, band, { stations: bs, channels: allRows, selected: null, height: 140, maxLabels: 8, rows: false, interactive: true });
  }
  const t = h("table", { class: "data" }, h("thead", null, h("tr", null, ...["SSID", "BSSID", "主ch", "幅", "強さ", "規格", "機種推定"].map((x) => h("th", { text: x })))));
  const tb = h("tbody");
  for (const x of sts.sort((a, b) => a.freq - b.freq)) {
    tb.append(h("tr", null, h("td", { text: ssidText(x) }), h("td", { class: "mono", text: x.bssid }), h("td", { class: "num", text: chLabel(x.freq) }),
      h("td", { class: "num", text: x.width_label }), h("td", { class: "num", text: x.rssi ?? "—" }), h("td", { text: (r.stations.find((q) => q.bssid === x.bssid) || {}).standard || "—" }), h("td", { text: x.model || "—" })));
  }
  t.append(tb);
  el.append(h("h3", { text: "局の一覧" }), h("div", { class: "table-wrap" }, t));
}

function chRowsFor(band) {
  const nums = band === "6" ? Array.from({ length: 24 }, (_, i) => 1 + 4 * i)
    : band === "5" ? [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144]
    : Array.from({ length: 13 }, (_, i) => i + 1);
  const toF = (n) => (band === "6" ? 5950 + 5 * n : band === "5" ? 5000 + 5 * n : 2407 + 5 * n);
  return nums.map((n) => ({ freq: toF(n), band, ch: n, state: "patrol", observed_ago: 0, n_overlap: 0, n_primary: 0, n_strong: 0, max_rssi: null, rate: null, reserved: [], problem: false }));
}

// ------------------------------------------------------------------ イベント
const EV_CAT = [["", "全部"], ["alert", "警報"], ["scan", "手動スキャン"], ["bands", "帯域ON/OFF"], ["mode", "モード"], ["radio", "受信"], ["label", "ラベル"], ["walk", "歩測"], ["tag", "タグ"], ["own", "自ch"], ["system", "起動"]];
async function renderEvents(force = false) {
  const el = $("pEvents");
  const E = ui.events;
  if (!el.dataset.built) {
    el.dataset.built = "1";
    el.append(h("div", { class: "toolbar" }, segControl(EV_CAT, E.category, (v) => { E.category = v; renderEvents(true); })), h("div", { id: "evList" }));
  }
  if (force || Date.now() - E.lastFetch > 3000) {
    try { E.list = await api(`/api/events?limit=300${E.category ? `&category=${E.category}` : ""}`); E.lastFetch = Date.now(); } catch { /* 次回 */ }
  }
  const list = clear($("evList"));
  for (const e of E.list) {
    const kindJP = { raised: "発生", added: "追加", cleared: "条件が消えた", resolved: "解消を通知" }[e.kind] || "";
    list.append(h("div", { class: "ev" },
      h("span", { class: "tm", text: tJST(e.ts) }),
      h("span", { class: `lv-${e.level}`, text: e.level === "crit" ? "■" : e.level === "warn" ? "▲" : "・" }),
      h("span", null, h("span", { class: "ttl", text: `${e.title || e.category}${kindJP ? `（${kindJP}）` : ""}` }), " ", e.message || "",
        e.origin ? h("span", { class: "sub", text: `（${e.origin === "app" ? "アプリ" : "画面"}から）` }) : null)));
  }
  if (!E.list.length) list.append(h("p", { class: "sub", text: "イベントはまだありません" }));
}

// ------------------------------------------------------------------ 巡回と手動スキャン
async function renderPatrol(force = false) {
  const el = $("pPatrol");
  if (force || !ui.bands) {
    try { ui.bands = await api("/api/bands"); } catch (e) { toast(e.message, true); return; }
  }
  const B = ui.bands;
  const st = ui.state;
  const put = async (bands, freqs) => {
    try { ui.bands = await api("/api/bands", { method: "PUT", body: { excluded_bands: bands, excluded_freqs: freqs } }); toast("巡回の設定を変えました（再起動しても残ります）"); renderPatrol(); }
    catch (e) { toast(e.message, true); renderPatrol(true); }
  };
  const grid = h("div", { class: "patrol-grid" });
  for (const b of ["6", "5", "2.4"]) {
    const info = B.bands[b];
    const sw = h("label", { class: "switch" }, h("input", { type: "checkbox", checked: !info.excluded, onchange: (e) => {
      const bands = new Set(B.excluded_bands);
      if (e.target.checked) bands.delete(b); else bands.add(b);
      put([...bands], B.excluded_freqs);
    } }), `${info.label}を巡回する`);
    const chips = h("div", { class: "chips" });
    for (const c of info.channels) {
      const cls = c.own ? "chch own" : c.locked ? "chch locked" : c.excluded ? "chch off" : "chch";
      chips.append(h("button", { type: "button", class: cls, disabled: c.locked, title: c.locked ? "自ブロックは外せません" : c.excluded ? "クリックで巡回に戻す" : "クリックで巡回から外す",
        text: `${c.locked ? "🔒" : ""}${c.ch}`, onclick: () => {
          if (info.excluded) return toast(`${info.label}ごと外しています。先に帯域のスイッチを入れてください`);
          const fs = new Set(B.excluded_freqs);
          if (fs.has(c.freq)) fs.delete(c.freq); else fs.add(c.freq);
          put(B.excluded_bands, [...fs]);
        } }));
    }
    grid.append(sw, chips);
  }
  const a = st?.allocation || {};
  const alloc = h("div", { class: "alloc" },
    h("span", null, "10秒あたりの枠：自ブロック内 ", h("b", { text: a.in_block ?? 0 }), "・6GHz ", h("b", { text: a.six ?? 0 }), "・5GHz ", h("b", { text: (a.five ?? 0) + (a.spare ? `＋予備ch ${a.spare}` : "") }), "・2.4GHz ", h("b", { text: a.two4 ?? 0 })),
    h("span", null, "自chの受信率 ", h("b", { text: st ? `${Math.round(st.own.ratio * 100)}%` : "—" })),
    h("span", { text: "外した帯域の枠は残りに配り直します。生存確認は止めません。" }));
  // 手動スキャン
  const sel = { "6": !B.excluded_bands.includes("6"), "5": !B.excluded_bands.includes("5"), "2.4": !B.excluded_bands.includes("2.4") };
  const boxes = ["6", "5", "2.4"].map((b) => h("label", { class: "switch" }, h("input", { type: "checkbox", checked: sel[b], onchange: (e) => { sel[b] = e.target.checked; upd(); } }), BAND_JP[b]));
  const est = h("span", { class: "sub" });
  const n = { "6": 24, "5": 20, "2.4": 13 };
  const upd = () => { const k = Object.entries(sel).filter(([, v]) => v).reduce((x, [b]) => x + n[b], 0); est.textContent = `${k}ch・約${(k * 0.25).toFixed(1)}秒（その間、自chと巡回を止めます）`; };
  upd();
  const running = st?.manual && st.manual.state !== "done";
  const go = h("button", { class: "btn primary", type: "button", disabled: running, text: running ? "実行中" : "手動スキャンを始める", onclick: () => startScan(Object.entries(sel).filter(([, v]) => v).map(([b]) => b)) });
  clear(el).append(
    h("h3", { text: "巡回の帯域ON/OFF" }),
    h("p", { class: "sub", text: "外した帯域・chは「見ていない」扱いになり、グラフでは斜線で出ます（検知なし、とは違います）。" }),
    grid, alloc,
    h("h3", { style: "margin-top:16px", text: "手動スキャン（1回だけ）" }),
    h("div", { class: "scan-launch" }, ...boxes, est, go));
}

async function startScan(bands, confirm = false) {
  if (!bands.length) return toast("帯域を1つ以上選んでください", true);
  if (ui.state?.mode === "QUIET" && !confirm) {
    const ok = await confirmDialog("QUIET中の手動スキャン", "山場の試合中です。手動スキャンの間は自chを見ません。実行しますか？");
    if (!ok) return;
    confirm = true;
  }
  try {
    const r = await api("/api/scan", { method: "POST", body: { bands, confirm } });
    toast(`手動スキャンを始めました（${r.total}ch・約${r.seconds.toFixed(1)}秒）`);
  } catch (e) {
    if (e.code === "needs_confirm") return startScan(bands, false);
    toast(e.message, true);
  }
}

// ------------------------------------------------------------------ 設定
function renderSettings() {
  const el = $("pSettings");
  const st = ui.state;
  if (!st) return;
  if (el.dataset.built) { renderSettingsLive(); return; }
  el.dataset.built = "1";
  const chSel = h("select", { "aria-label": "自分のch" });
  for (const b of ["6", "5", "2.4"]) {
    const og = h("optgroup", { label: BAND_JP[b] });
    for (const r of chRowsFor(b)) og.append(h("option", { value: r.freq, text: `${BAND_JP[b]} ch${r.ch}`, selected: r.freq === st.own.freq }));
    chSel.append(og);
  }
  const wSel = h("select", { "aria-label": "幅" }, ...[20, 40, 80, 160, 320].map((w) => h("option", { value: w, text: `${w}MHz`, selected: w === st.own.width })));
  const apply = h("button", { class: "btn primary", type: "button", text: "自chを変える", onclick: async () => {
    const f = Number(chSel.value), w = Number(wSel.value);
    const ok = await confirmDialog("自chの変更", `${chLabel(f)}・${w}MHz に変えます。監視する範囲（自ブロック）と警報の基準が変わります。`);
    if (!ok) return;
    try { await api("/api/own", { method: "POST", body: { channel: f, width: w } }); toast("自chを変えました"); ui.bands = null; }
    catch (e) { toast(e.message, true); }
  } });
  const theme = segControl([["auto", "OSに合わせる"], ["light", "明るい"], ["dark", "暗い"]], prefs.get("theme", "auto"), (v) => { prefs.set("theme", v); applyTheme(); });
  el.append(
    h("h3", { text: "自分のch（再起動なしで変更）" }), h("div", { class: "toolbar" }, chSel, wSel, apply),
    h("h3", { style: "margin-top:14px", text: "画面の明るさ" }), theme,
    h("h3", { style: "margin-top:14px", text: "チームと予約ch（config.json）" }), h("div", { id: "setTeams" }),
    h("h3", { style: "margin-top:14px", text: "通知と中継" }), h("div", { id: "setLinks" }));
  renderSettingsLive();
}
function renderSettingsLive() {
  const st = ui.state;
  const t = h("table", { class: "data" }, h("thead", null, h("tr", null, h("th", { text: "チーム" }), h("th", { text: "予約ch（割当と予備）" }))));
  const tb = h("tbody", null, h("tr", null, h("td", { text: `${st.own.team}（自チーム）` }), h("td", { text: [st.own.label, ...st.own.spare.map((s2) => s2.label)].join("、") })));
  for (const tm of st.teams) tb.append(h("tr", null, h("td", { text: tm.name }), h("td", { text: tm.channels.map(chLabel).join("、") || "—" })));
  t.append(tb);
  clear($("setTeams")).append(h("div", { class: "table-wrap" }, t));
  const n = st.notify || {};
  const r = st.relay || {};
  clear($("setLinks")).append(h("dl", { class: "facts" },
    h("dt", { text: "ntfy" }), h("dd", { text: n.ntfy ? `設定あり・未送 ${n.backlog}` : "未設定" }),
    h("dt", { text: "Discord" }), h("dd", { text: n.discord ? "設定あり（critだけ）" : "未設定" }),
    h("dt", { text: "死活監視" }), h("dd", { text: n.healthcheck ? (n.health_ok ? "報告できている" : "報告できていない") : "未設定" }),
    h("dt", { text: "警報音" }), h("dd", { text: n.buzzer ? (n.buzzer_error ? `エラー：${n.buzzer_error}` : "設定あり") : "未設定" }),
    h("dt", { text: "中継" }), h("dd", { text: r.configured ? (r.ok ? `つながっている（最後 ${tJST(r.last_ok)}）` : `届かない：${r.last_error || ""}`) : "未設定" })));
}

function applyTheme() {
  const t = prefs.get("theme", "auto");
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", t);
  if (ui.state) render(ui.state);
}

// ------------------------------------------------------------------ タブ
function setTab(tab) {
  ui.tab = tab;
  prefs.set("tab", tab);
  for (const b of $("tabs").querySelectorAll("button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const p of document.querySelectorAll(".tabpanel")) p.hidden = p.dataset.panel !== tab;
  renderTab(true);
}
function renderTab(force = false) {
  const st = ui.state;
  if (!st) return;
  if (ui.tab === "stations") renderStations(st);
  else if (ui.tab === "clients") renderClients(force);
  else if (ui.tab === "records") renderRecords(force);
  else if (ui.tab === "events") renderEvents(force);
  else if (ui.tab === "patrol") { if (force) renderPatrol(true); }
  else if (ui.tab === "settings") renderSettings();
}

// ------------------------------------------------------------------ まとめて描く
function render(st) {
  renderTop(st);
  renderCharts(st);
  renderAlerts(st);
  renderOwnTile(st);
  renderSuggestions(st);
  renderCalibration(st);
  $("cntStations").textContent = st.stations.filter((q) => q.present).length;
  $("cntClients").textContent = st.clients;
  renderTab(false);
  if (!$("drawer").hidden && ui.selected && Date.now() - (ui._drawerAt || 0) > 2000) {
    ui._drawerAt = Date.now();
    openDrawer(ui.selected);
  }
}

let lastMode = null;
async function poll() {
  try {
    const st = await api("/api/state");
    ui.state = st;
    $("offline").hidden = true;
    document.body.classList.remove("stale");
    if (lastMode && lastMode !== st.mode) ui.bands = null;
    lastMode = st.mode;
    render(st);
    if (ui.tab === "patrol" && (!ui.bands || st.manual?.state === "running")) renderPatrol(!ui.bands);
  } catch (e) {
    $("offline").hidden = false;
    document.body.classList.add("stale");
  } finally {
    setTimeout(poll, 1000);
  }
}

// ------------------------------------------------------------------ はじめ
function init() {
  installDefs();
  applyTheme();
  renderLegend();
  for (const b of $("modeSwitch").querySelectorAll("button")) {
    b.addEventListener("click", async () => {
      const m = b.dataset.mode;
      if (ui.state && ui.state.mode === m) return;
      try { await api("/api/mode", { method: "POST", body: { mode: m } }); toast(`${MODE_JP[m]}に切り替えました`); }
      catch (e) { toast(e.message, true); }
    });
  }
  for (const b of $("tabs").querySelectorAll("button")) b.addEventListener("click", () => setTab(b.dataset.tab));
  $("drawerClose").addEventListener("click", () => select(null));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("drawer").hidden) select(null); });
  let rt = null;
  addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => ui.state && render(ui.state), 150); });
  setTab(ui.tab);
  if (ui.selected) $("drawer").hidden = true;
  ui.selected = null;
  poll();
}
init();
