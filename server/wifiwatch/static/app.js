import { drawClock } from "./segclock.js";
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
  selectedMany: null, // 重なっているところをクリックしたときの局（2つ以上）
  tab: prefs.get("tab", "stations"),
  stFilter: prefs.get("stFilter", { band: "all", kind: "all", present: true, q: "" }),
  stSort: prefs.get("stSort", { key: "rssi", asc: false }),
  pointer: {},
  bandView: prefs.get("bandView", "all"),
  clockMetric: prefs.get("clockMetric", "stations"),
  verdictLevel: "blind",
  records: { list: [], selected: null, detail: null, filter: { kind: "", location: "", band: "" }, lastFetch: 0 },
  events: { list: [], category: "", lastFetch: 0 },
  clients: { list: [], lastFetch: 0 },
  bands: null,
  drawerDetail: null,
  drawerDetails: null,
};
const isSel = (b) => (ui.selectedMany ? ui.selectedMany.includes(b) : b === ui.selected);
const selSet = () => new Set(ui.selectedMany || (ui.selected ? [ui.selected] : []));

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

/** 「いる」「いない」の理由（時間ではなく、主chを見に行って聞こえたか）。 */
function presenceText(x) {
  const missed = x.missed || 0;
  if (!x.present) return missed >= 2 ? "いない扱い（このchを見に行って2回続けて聞こえない）" : "いない扱い（このchを長く見に行けていない・聞こえない）";
  if (x.age_s <= 5) return "いる";
  if (missed === 0) return "いる扱い（このchをまだ見に行っていない。次に見て聞こえなければ減る）";
  return `いる扱い（見に行って${missed}回聞こえなかった。2回で「いない」）`;
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
  wrap.append(h("div", { class: "r", text: `最終確認 ${ago(st.age_s)}・${presenceText(st)}` }));
  wrap.append(h("div", { class: "r", text: st.bssid }));
  if (st.alerts && st.alerts.length) wrap.append(h("div", { class: "r", text: `警報：${st.alerts.map((a) => ALERT_TYPE_JP[a] || a).join("、")}` }));
  if (st.new) wrap.append(h("div", { class: "r", text: "新規参入（自分の帯域に新しく現れた）" }));
  wrap.append(h("div", { class: "r", text: "クリックで詳細" }));
  return [wrap];
}

function overlapNode(hits) {
  const out = h("div");
  out.append(h("div", { class: "v", text: `ここに重なる局 ${hits.length}` }));
  for (const st of hits.slice(0, 6)) {
    const k = kindOf(st.kind);
    out.append(h("div", { class: "row" }, h("span", { class: `glyph ${k.cls}`, text: k.glyph }),
      h("b", { text: ssidText(st) }), h("span", { class: "r", text: `${st.rssi}dBm・${chLabel(st.freq)}・${st.width_label}MHz` })));
  }
  if (hits.length > 6) out.append(h("div", { class: "r", text: `ほか ${hits.length - 6}局` }));
  out.append(h("div", { class: "r", text: "クリックで全部の詳細" }));
  return [out];
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

const UNKNOWN_RSSI_Y = -88; // 強さ不明の局を描く高さ（点線。強さの目安ではない）

function renderSpectrum(container, band, opt) {
  const { stations, channels, selected = null, height = 300, maxLabels = 14, rows = true, ownSpans = [], blockSpan = null, strong = -70, interactive = true } = opt;
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
  // 強さ不明の局があれば、そう添える（強い・弱いを見せられない理由）
  const nUnknown = stations.filter((q) => q.present && q.rssi == null).length;
  if (nUnknown) {
    svg.append(s("text", { class: "unknown-note", x: L + 4, y: T + 12,
      text: `強さ不明 ${nUnknown}局（点線）：無線が強さを返していません` }));
  }
  // 「強い」の目安
  svg.append(s("line", { class: "thresh", x1: L, x2: W - R, y1: y(strong), y2: y(strong) }));
  svg.append(s("text", { class: "thresh-label", x: W - R - 2, y: y(strong) - 3, "text-anchor": "end", text: `強い ${strong}dBm` }));

  // 局（弱い順に描いて強い局を上に。選んだ局は最後）。選んだ局は1つとは限らない（重なりをクリックしたとき）
  const chosen = selected instanceof Set ? selected : new Set(selected ? [selected] : []);
  const isChosen = (b) => chosen.has(b);
  const anySel = stations.some((q) => isChosen(q.bssid));
  const ordered = [...stations].sort((p, q) => isChosen(p.bssid) - isChosen(q.bssid) || (p.rssi ?? -200) - (q.rssi ?? -200));
  const geo = [];
  for (const st of ordered) {
    const k = kindOf(st.kind);
    const yt = y(st.rssi ?? UNKNOWN_RSSI_Y);
    const cls = ["st-shape", k.cls];
    if (st.rssi == null) cls.push("unknown"); // 強さ不明（無線が強さを返さない）：低い点線で描く
    if (st.alerts.length) cls.push("alerting");
    if (!st.present) cls.push("gone");
    if (anySel && !isChosen(st.bssid)) cls.push("dim");
    if (isChosen(st.bssid)) cls.push("selected");
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
  const prio = (st) => (isChosen(st.bssid) ? 0 : st.alerts.length ? 1 : st.kind === "own" ? 2 : st.new ? 3 : 4);
  const cands = [...stations].filter((q) => q.present)
    .sort((p, q) => prio(p) - prio(q) || (q.rssi ?? -200) - (p.rssi ?? -200));
  let nLabels = 0;
  const labeled = new Set();
  for (const st of cands) {
    if (nLabels >= maxLabels) break;
    const keyName = st.ssid || st.bssid;
    if (labeled.has(keyName) && prio(st) >= 4) continue; // 同じ名前は1回だけ（RoboconFreeの乱立など）
    const [lo, hi] = st.spans[0];
    let cx = (x(lo) + x(hi)) / 2;
    const yt = y(st.rssi ?? UNKNOWN_RSSI_Y);
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
    if (anySel && !isChosen(st.bssid)) t.setAttribute("opacity", 0.3);
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
      const under = new Map(); // ポインタの下にある局（台形の中か、上の辺の近く）。重なっていれば全部
      for (const gq of geo) {
        if (px < gq.xa - 2 || px > gq.xz + 2) continue;
        const d = Math.abs(py - gq.yt);
        if (d <= 14 && (!best || d < best.d)) best = { d, st: gq.st };
        if (py >= gq.yt - 14 && py <= xb) under.set(gq.st.bssid, gq.st);
      }
      const hits = [...under.values()].sort((p, q) => (q.rssi ?? -200) - (p.rssi ?? -200));
      let row = null;
      for (const r of channels) {
        const [a, b] = colX(r.freq);
        if (px >= a && px <= b) { row = r; if (band !== "2.4") break; }
      }
      return { best, row, hits };
    };
    const onMove = (ev) => {
      ui.pointer[band] = { x: ev.clientX, y: ev.clientY };
      const { best, row, hits } = pick(ev);
      overlay.style.cursor = best || hits.length ? "pointer" : "crosshair";
      if (hits.length > 1) tip.show(ev.clientX, ev.clientY, overlapNode(hits));
      else if (best || hits.length) tip.show(ev.clientX, ev.clientY, stationNode((best && best.st) || hits[0]));
      else if (row) tip.show(ev.clientX, ev.clientY, channelNode(row));
      else tip.hide();
    };
    overlay.addEventListener("pointermove", onMove);
    overlay.addEventListener("pointerleave", () => { delete ui.pointer[band]; tip.hide(); });
    overlay.addEventListener("click", (ev) => {
      const { best, hits } = pick(ev);
      if (hits.length > 1) selectMany(hits.map((q) => q.bssid));
      else select(best ? best.st.bssid : hits.length ? hits[0].bssid : null);
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
  ui.verdictLevel = lvl;
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

  renderFollowBar(st);
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

/** 自chを自APに合わせたこと（30分）と、どれに合わせるか選ぶとき（自APが別々のchにいる）。 */
function renderFollowBar(st) {
  const bar = $("followbar");
  const f = st.own.follow || {};
  const key = JSON.stringify([f.pending && f.pending.key, f.last && f.last.at]);
  if (bar.dataset.key === key) return; // ボタンがあるので、変わったときだけ作り直す
  bar.dataset.key = key;
  clear(bar);
  if (f.pending) {
    bar.className = "followbar warn";
    bar.append(h("span", { text: "▲ 自APが別々のchにいます。どれに自chを合わせますか？" }));
    for (const c of f.pending.choices) {
      const names = c.stations.map((x) => x.ssid || x.bssid).join("、");
      bar.append(h("button", { class: "btn", type: "button", text: `${c.label}・${c.width}MHz（${names}）に合わせる`, onclick: async () => {
        try { await api("/api/own", { method: "POST", body: { channel: c.freq, width: c.width } }); toast(`自chを ${c.label}・${c.width}MHz にしました`); ui.bands = null; }
        catch (e) { toast(e.message, true); }
      } }));
    }
    bar.hidden = false;
    return;
  }
  const last = f.last;
  if (last && prefs.get("followDismissed", 0) !== last.at) {
    bar.className = last.reserved ? "followbar" : "followbar warn";
    bar.append(
      h("span", { text: `${last.reserved ? "ℹ" : "▲"} ${tJST(last.at)} 自AP ${last.ap} が ${last.to_label} に移ったので、自chを ${last.from_label}・${last.from_width}MHz → ${last.to_label}・${last.to_width}MHz に合わせました` }),
      last.reserved ? null : h("span", { class: "sub", text: `移った先は予約ch（${last.reserved_labels.join("、")}）ではありません` }),
      h("button", { class: "x", type: "button", "aria-label": "閉じる", text: "×", onclick: () => { prefs.set("followDismissed", last.at); bar.hidden = true; } }));
    bar.hidden = false;
    return;
  }
  bar.hidden = true;
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
    h("span", { class: "item", text: "⚠ 警報の当事者" }));
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

// ------------------------------------------------------------------ 表示する帯域（全部か、1つだけ大きく）
const BAND_VIEWS = [["all", "全部"], ["6", "6GHz"], ["5", "5GHz"], ["2.4", "2.4GHz"]];
const SOLO_CLASS = { "6": "solo-6", "5": "solo-5", "2.4": "solo-24" };
const BAND_CARD = { "6": "h6", "5": "h5", "2.4": "h24" };

function buildViewbar() {
  const bar = clear($("viewbar"));
  bar.append(h("span", { text: "表示する帯域" }), segControl(BAND_VIEWS, ui.bandView, setBandView),
    h("span", { class: "hint", text: "グラフの見出しを押しても切り替わります（キー 6・5・2、0で全部）" }));
  for (const [band, id] of Object.entries(BAND_CARD)) {
    const t = $(id);
    t.classList.add("band-title");
    t.title = "押すと、この帯域だけを大きく表示（もう一度で全部）";
    t.addEventListener("click", () => setBandView(ui.bandView === band ? "all" : band));
  }
}

function setBandView(v) {
  if (!BAND_VIEWS.some(([k]) => k === v)) return;
  ui.bandView = v;
  prefs.set("bandView", v);
  applyBandView();
  if (ui.state) renderCharts(ui.state);
  positionSplitters();
}

function applyBandView() {
  const lay = $("layout");
  lay.classList.remove("solo", ...Object.values(SOLO_CLASS));
  const solo = ui.bandView !== "all";
  if (solo) lay.classList.add("solo", SOLO_CLASS[ui.bandView]);
  for (const b of $("viewbar").querySelectorAll(".seg button")) b.setAttribute("aria-pressed", String(b.textContent === BAND_VIEWS.find(([k]) => k === ui.bandView)[1]));
  // 凡例は、見えているグラフのカードに置く
  const card = $(BAND_CARD[solo ? ui.bandView : "6"]).closest(".card");
  const lg = $("legend");
  if (lg.parentElement !== card) card.insertBefore(lg, card.querySelector(".chart"));
}

/** 1つだけ表示のときのグラフの高さ：画面の高さに合わせる（下のタブの見出しが少し見えるくらい）。 */
function soloHeight() {
  return Math.round(Math.max(300, Math.min(window.innerHeight - 300, 760)));
}

// ------------------------------------------------------------------ 時計（7セグメント。segclock.js）
// 下の段の値：温度の代わりに、押すたびに いる局 → 自APの強さ → 強い局 と変わる
const CLOCK_METRICS = ["stations", "own_rssi", "strong"];

function clockValue(st) {
  const m = ui.clockMetric;
  if (!st) return { text: "--", unit: m === "own_rssi" ? "dBm" : "局", caption: "PCの\n状態なし" };
  const present = st.stations.filter((q) => q.present);
  if (m === "own_rssi") {
    const own = present.filter((q) => q.kind === "own" && q.rssi != null).sort((a, b) => b.rssi - a.rssi)[0];
    return { text: own ? String(Math.max(own.rssi, -99)) : "--", unit: "dBm", caption: own ? "自APの\n強さ" : "自APが\n見えない" };
  }
  if (m === "strong") {
    const n = present.filter((q) => q.rssi != null && q.rssi >= st.thresholds.strong_rssi_dbm).length;
    return { text: String(Math.min(n, 999)), unit: "局", caption: `強い局\n${st.thresholds.strong_rssi_dbm}dBm以上` };
  }
  return { text: String(Math.min(present.length, 999)), unit: "局", caption: "いる局\n全帯域" };
}

function renderClock() {
  const box = $("segclock");
  if (!box) return;
  const stale = document.body.classList.contains("stale") || !ui.state;
  drawClock(box, new Date(), clockValue(stale ? null : ui.state), stale ? "blind" : ui.verdictLevel);
}

// ------------------------------------------------------------------ 境目をドラッグして大きさを変える
// 6GHzの高さ・5GHzと2.4GHzの幅の割合・5/2.4GHzの高さ・右の欄の幅・1つだけ表示のときの高さ。ダブルクリックで元に戻す
const SPLIT_DEF = { sideW: 360, ratio5: 0.5, h6: 300, h5: 150, hSolo: null };
ui.split = { ...SPLIT_DEF, ...prefs.get("split", {}) };
const SPLIT_LIM = { sideW: [240, 760], ratio5: [0.2, 0.8], h6: [140, 1000], h5: [90, 800], hSolo: [200, 1400] };
const clampK = (k, v) => Math.max(SPLIT_LIM[k][0], Math.min(SPLIT_LIM[k][1], v));
let chartRaf = 0;

function applySplit() {
  const lay = $("layout");
  lay.style.setProperty("--side-w", `${ui.split.sideW}px`);
  lay.style.setProperty("--c1", `${ui.split.ratio5}fr`);
  lay.style.setProperty("--c2", `${1 - ui.split.ratio5}fr`);
}

function scheduleCharts() {
  if (chartRaf) return;
  chartRaf = requestAnimationFrame(() => {
    chartRaf = 0;
    if (ui.state) renderCharts(ui.state);
    positionSplitters();
  });
}

function buildSplitters() {
  const lay = $("layout");
  const defs = [
    ["side", "v", "右の欄の幅"], ["mid", "v", "5GHzと2.4GHzの幅"], ["h6", "h", "上のグラフの高さ"], ["h5", "h", "5GHz・2.4GHzのグラフの高さ"],
  ];
  for (const [key, dir, name] of defs) {
    const sp = h("div", { class: `splitter ${dir}`, role: "separator", tabindex: 0, "aria-orientation": dir === "v" ? "vertical" : "horizontal",
      "aria-label": name, title: `ドラッグで${name}を変える（ダブルクリックで元に戻す）`, dataset: { key } });
    sp.addEventListener("pointerdown", (e) => startDrag(e, sp));
    sp.addEventListener("dblclick", () => {
      const k = splitKey(sp);
      ui.split[k] = SPLIT_DEF[k];
      prefs.set("split", ui.split);
      applySplit();
      scheduleCharts();
    });
    sp.addEventListener("keydown", (e) => {
      const k = splitKey(sp);
      const step = k === "ratio5" ? 0.05 : 20;
      const d = { ArrowLeft: -step, ArrowUp: -step, ArrowRight: step, ArrowDown: step }[e.key];
      if (d == null) return;
      e.preventDefault();
      const cur = ui.split[k] ?? soloHeight();
      ui.split[k] = clampK(k, cur + (k === "sideW" ? -d : d));
      prefs.set("split", ui.split);
      applySplit();
      scheduleCharts();
    });
    lay.append(sp);
  }
}

function splitKey(sp) {
  const k = sp.dataset.key;
  if (k === "side") return "sideW";
  if (k === "mid") return "ratio5";
  if (k === "h6") return ui.bandView !== "all" ? "hSolo" : "h6";
  return "h5";
}

function startDrag(e, sp) {
  e.preventDefault();
  sp.setPointerCapture(e.pointerId);
  const k = splitKey(sp);
  const start = { x: e.clientX, y: e.clientY, v: ui.split[k] ?? soloHeight() };
  const five = $("chart5").closest(".card").getBoundingClientRect();
  const two4 = $("chart24").closest(".card").getBoundingClientRect();
  sp.classList.add("dragging");
  document.body.classList.add("resizing");
  const move = (ev) => {
    if (k === "ratio5") ui.split.ratio5 = clampK(k, (ev.clientX - five.left) / Math.max(1, two4.right - five.left));
    else if (k === "sideW") ui.split.sideW = clampK(k, start.v - (ev.clientX - start.x));
    else ui.split[k] = clampK(k, start.v + (ev.clientY - start.y));
    applySplit();
    scheduleCharts();
  };
  const up = () => {
    sp.removeEventListener("pointermove", move);
    sp.removeEventListener("pointerup", up);
    sp.removeEventListener("pointercancel", up);
    sp.classList.remove("dragging");
    document.body.classList.remove("resizing");
    prefs.set("split", ui.split);
  };
  sp.addEventListener("pointermove", move);
  sp.addEventListener("pointerup", up);
  sp.addEventListener("pointercancel", up);
}

/** 境目のつまみを、いまのカードの位置に置く（描くたびに呼ぶ）。 */
function positionSplitters() {
  const lay = $("layout");
  if (!lay) return;
  const L = lay.getBoundingClientRect();
  const card = (id) => $(id).closest(".card");
  const R = (e) => e.getBoundingClientRect();
  const single = getComputedStyle(lay).gridTemplateColumns.trim().split(/\s+/).length === 1;
  const solo = ui.bandView !== "all";
  const top = solo ? card(BAND_CARD[ui.bandView]) : card("chart6");
  const place = (key, show, x, y, w, hh) => {
    const sp = lay.querySelector(`.splitter[data-key="${key}"]`);
    if (!sp) return;
    sp.hidden = !show;
    if (!show) return;
    sp.style.left = `${x - L.left}px`; sp.style.top = `${y - L.top}px`;
    if (w != null) sp.style.width = `${w}px`;
    if (hh != null) sp.style.height = `${hh}px`;
  };
  const gap = 12;
  const tR = R(top);
  const sR = R(document.querySelector(".side"));
  place("side", !single, sR.left - gap / 2 - 6, tR.top, null, sR.bottom - tR.top);
  place("h6", true, tR.left, tR.bottom + gap / 2 - 6, tR.width, null);
  if (!solo) {
    const f = R(card("chart5")), t = R(card("chart24"));
    place("mid", !single, (f.right + t.left) / 2 - 6, f.top, null, f.height);
    const last = single ? t : (f.bottom > t.bottom ? f : t);
    place("h5", true, single ? t.left : f.left, last.bottom + gap / 2 - 6, single ? t.width : t.right - f.left, null);
  } else {
    place("mid", false); place("h5", false);
  }
}

function renderCharts(st) {
  const ownRows = st.channels.filter((r) => r.own_band);
  const ownSpans = mergeSpans(ownRows.map((r) => [r.freq - 10, r.freq + 10]));
  const blk = st.own.block;
  const blockSpan = blk.width > 20 ? [blk.center - blk.width / 2, blk.center + blk.width / 2] : null;
  const ownBand = bandOf(st.own.freq);
  const common = { selected: selSet(), strong: st.thresholds.strong_rssi_dbm };
  const solo = ui.bandView !== "all";
  for (let [band, el, hgt, ml] of [["6", "chart6", ui.split.h6, 14], ["5", "chart5", ui.split.h5, 5], ["2.4", "chart24", ui.split.h5, 4]]) {
    if (solo && band !== ui.bandView) continue; // 隠しているグラフは描かない
    if (solo) { hgt = ui.split.hSolo ?? soloHeight(); ml = 24; }
    renderSpectrum($(el), band, {
      ...common, height: hgt, maxLabels: ml,
      // いない扱いの局はグラフに描かない（選んでいる局だけは薄く残す）。表では「いる局だけ」を外すと見える
      stations: st.stations.filter((q) => q.band === band && (q.present || isSel(q.bssid))),
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
  ui.selectedMany = null;
  prefs.set("selected", bssid);
  if (ui.state) { renderCharts(ui.state); if (ui.tab === "stations") renderStations(ui.state); }
  if (bssid) openDrawer(bssid); else $("drawer").hidden = true;
}

/** 重なっているところをクリックしたとき：重なる局を全部選び、全部の詳細を出す（強い順）。 */
function selectMany(bssids) {
  ui.selected = bssids[0];
  ui.selectedMany = bssids;
  if (ui.state) { renderCharts(ui.state); if (ui.tab === "stations") renderStations(ui.state); }
  openDrawerMany(bssids);
}

async function openDrawerMany(bssids) {
  $("drawer").hidden = false;
  const got = await Promise.all(bssids.map((b) => api(`/api/stations/${encodeURIComponent(b)}`).catch(() => null)));
  if (ui.selectedMany !== bssids) return; // 読んでいる間に別のところを選んだ
  ui.drawerDetails = got.filter(Boolean);
  renderDrawerMany(bssids.length - ui.drawerDetails.length);
}

function renderDrawerMany(missing) {
  const ds = ui.drawerDetails || [];
  $("drawerTitle").textContent = `重なる局（${ds.length}）`;
  const body = clear($("drawerBody"));
  // くらべやすいように、まず一覧（強い順）。行を押すとその局の詳細へ
  const tb = h("tbody");
  ds.forEach((d, i) => {
    const k = kindOf(d.kind);
    tb.append(h("tr", { onclick: () => $(`multi-${i}`).scrollIntoView({ behavior: "smooth", block: "start" }) },
      h("td", null, h("span", { class: `glyph ${k.cls}`, text: k.glyph })),
      h("td", { text: d.ssid || "（非公開SSID）" }),
      h("td", { class: "num", text: d.rssi_avg ?? d.rssi ?? "—" }),
      h("td", { text: `ch${chNum(d.freq)}・${d.width_label}MHz` })));
  });
  body.append(h("table", { class: "multi-table" },
    h("thead", null, h("tr", null, h("th", { text: "" }), h("th", { text: "SSID" }), h("th", { class: "num", text: "dBm" }), h("th", { text: "主ch・幅" }))), tb));
  if (missing > 0) body.append(h("p", { class: "sub", text: `${missing}局は読めませんでした（いなくなった可能性）` }));
  ds.forEach((d, i) => {
    const card = h("div", { class: "multi-card", id: `multi-${i}` }, ...detailNodes(d));
    card.append(h("button", { class: "btn", type: "button", text: "この局だけ開く（タグを付ける）", onclick: () => select(d.bssid) }));
    body.append(card);
  });
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
  $("drawerTitle").textContent = "局の詳細";
  const body = clear($("drawerBody"));
  body.append(...detailNodes(d));
  drawerControls(body, d);
}

/** 局の詳細（事実・警報・手がかり・RNR）。1局のときも、重なる局を並べるときも使う。 */
function detailNodes(d) {
  const k = kindOf(d.kind);
  const body = h("div");
  body.append(
    h("div", { class: "row" }, h("span", { class: `glyph ${k.cls}`, text: k.glyph + " " }), h("span", { text: k.label })),
    h("div", { class: "ssid", text: d.ssid || "（非公開SSID）" }),
    h("div", { class: "mono", text: d.bssid }));
  const facts = h("dl", { class: "facts" });
  const add = (a, b) => facts.append(h("dt", { text: a }), h("dd", { text: b ?? "—" }));
  add("主ch・幅", `${chLabel(d.freq)}・${d.width_label}MHz（${spanLabel(d.spans)}）${d.dup_beacon ? "・重複ビーコン" : ""}${d.suspect ? "・IEが矛盾" : ""}`);
  add("強さ", d.rssi_avg != null ? `平均 ${d.rssi_avg}dBm（最新 ${d.rssi}・最大 ${d.rssi_max}）` : "—");
  add("チーム", d.team ? `${d.team}（${{ config: "設定", tag: "手動タグ", ssid: "SSIDのパターン", rnr: "RNRで紐付け", sibling: "同じ機器（BSSIDの並び）", signature: "署名" }[d.team_source] || d.team_source}）` : "—");
  add("署名", d.signature);
  add("機種推定", d.model_guess);
  if (d.model_fp) add("指紋", `${d.model_fp}（同じ指紋の局 ${d.same_fp ?? "—"}）`);
  add("規格・暗号", `802.11${d.standard}・${d.security}${d.pmf_required ? "（PMF必須）" : ""}`);
  add("最終確認", `${ago(d.age_s)}（${tJST(d.last_seen)}）・${presenceText(d)}`);
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
  if (d.linked.length || d.rnr.length || (d.siblings || []).length) {
    const sec = h("section", null, h("h3", { text: "同じ機器（RNR・BSSIDの並び）" }));
    for (const b of d.linked) sec.append(h("button", { class: "party", type: "button", onclick: () => select(b), text: b }));
    for (const b of d.siblings || []) if (!d.linked.includes(b)) sec.append(h("button", { class: "party", type: "button", title: "同じchでBSSIDが並んでいる（非公開SSIDの双子など）", onclick: () => select(b), text: `${b}（並び）` }));
    for (const r of d.rnr) sec.append(h("div", { class: "sub", text: `知らせている局：${r.bssid || "（BSSIDなし）"}・${r.freq ? chLabel(r.freq) : `op${r.op_class} ch${r.channel}`}` }));
    body.append(sec);
  }
  return [...body.childNodes];
}

/** 1局のときだけ：機種の覚え帳、チーム名のタグ、端末の一覧。 */
function drawerControls(body, d) {
  if (d.model_fp) {
    const inp = h("input", { type: "text", maxlength: 60, placeholder: "例：Buffalo WSR-3200AX4S、操縦用スマホ", "aria-label": "機種の名前" });
    if (d.model_book) inp.value = d.model_book;
    const remember = h("button", { class: "btn primary", type: "button", text: "覚える", onclick: async () => {
      const label = inp.value.trim();
      if (!label) return toast("機種の名前を入れてください", true);
      if ((d.same_fp || 0) > 1) {
        // 指紋は機種ごとに必ず違うとは限らない（同じチップ・同じファームウェアなど）。何局に効くかを見せてから
        const ok = await confirmDialog("機種を覚える", `同じ指紋の局がいま${d.same_fp}あります。WPSで型番を名乗らない局には、すべて「${label}」と出ます。覚えますか？`, "覚える");
        if (!ok) return;
      }
      try { await api("/api/models", { method: "POST", body: { fp: d.model_fp, label, bssid: d.bssid } }); toast(`「${label}」と覚えました（同じ指紋の局にも出ます）`); openDrawer(d.bssid); }
      catch (e) { toast(e.message, true); }
    } });
    const forget = h("button", { class: "btn", type: "button", text: "忘れる", onclick: async () => {
      try { await api("/api/models", { method: "POST", body: { fp: d.model_fp, label: null } }); toast("忘れました"); openDrawer(d.bssid); }
      catch (e) { toast(e.message, true); }
    } });
    body.append(h("section", null, h("h3", { text: "機種を覚える（覚え帳）" }),
      h("p", { class: "sub", text: `指紋 ${d.model_fp}（IEの並びと能力から作る）。覚えると、同じ指紋の局にも同じ機種と出ます。見えた局の情報は外に送りません。` }),
      h("div", { class: "toolbar" }, ...[inp, remember, d.model_book ? forget : null].filter(Boolean))));
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
    tb.append(h("tr", { class: [isSel(x.bssid) ? "sel" : "", x.present ? "" : "gone"].join(" "), onclick: () => select(x.bssid) },
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
    h("h3", { text: "自チームのWi-Fi（AP）" }), buildOwnAp(),
    h("h3", { style: "margin-top:14px", text: "自分のch（再起動なしで変更）" }), h("div", { class: "toolbar" }, chSel, wSel, apply),
    buildFollowToggle(),
    h("h3", { style: "margin-top:14px", text: "画面の明るさ" }), theme,
    h("h3", { style: "margin-top:14px", text: "チームと予約ch（config.json）" }), h("div", { id: "setTeams" }),
    h("h3", { style: "margin-top:14px", text: "通知と中継" }), h("div", { id: "setLinks" }),
    h("h3", { style: "margin-top:14px", text: "機種の覚え帳" }), h("div", { id: "setModels" }));
  renderSettingsLive();
  renderModelBook();
}

/** 覚え帳の一覧。機種の表（models.json）に移すときは「表の形で書き出す」をそのまま貼る。 */
async function renderModelBook() {
  const el = $("setModels");
  if (!el) return;
  let b;
  try { b = await api("/api/models"); } catch (e) { clear(el).append(h("p", { class: "sub", text: `読めませんでした：${e.message}` })); return; }
  const t = h("table", { class: "data" }, h("thead", null, h("tr", null, h("th", { text: "機種" }), h("th", { text: "指紋" }), h("th", { class: "num", text: "いま見えている" }), h("th", { text: "" }))));
  const tb = h("tbody");
  for (const e of b.entries) {
    tb.append(h("tr", null, h("td", { text: e.label }), h("td", { class: "mono", text: e.fp }), h("td", { class: "num", text: e.now }),
      h("td", null, h("button", { class: "btn", type: "button", text: "忘れる", onclick: async (ev) => {
        ev.stopPropagation();
        try { await api("/api/models", { method: "POST", body: { fp: e.fp, label: null } }); renderModelBook(); } catch (er) { toast(er.message, true); }
      } }))));
  }
  t.append(tb);
  const dl = h("button", { class: "btn", type: "button", text: "表の形で書き出す（models.json に貼る）", disabled: !b.entries.length, onclick: () => {
    const blob = new Blob([JSON.stringify(b.export, null, 2)], { type: "application/json" });
    const a = h("a", { href: URL.createObjectURL(blob), download: "model_book_export.json" });
    document.body.append(a); a.click(); a.remove();
  } });
  clear(el).append(...[
    h("p", { class: "sub", text: `覚え帳 ${b.entries.length}件・機種の表 ${b.rules}行（models.json）。局の詳細の「機種を覚える」で増やせます。` }),
    b.entries.length ? h("div", { class: "table-wrap" }, t) : null, h("div", { class: "toolbar" }, dl)].filter(Boolean));
}
function buildFollowToggle() {
  const cb = h("input", { type: "checkbox", id: "followToggle" });
  cb.checked = !!(ui.state && ui.state.own.follow && ui.state.own.follow.enabled);
  cb.addEventListener("change", async () => {
    try {
      const r = await api("/api/own_follow", { method: "POST", body: { enabled: cb.checked } });
      if (ui.state) ui.state.own.follow = r;
      toast(cb.checked ? "自APのchに自動で合わせます" : "自動で合わせるのをやめました（自chは手で変えます）");
    } catch (e) { toast(e.message, true); cb.checked = !cb.checked; }
  });
  return h("div", { class: "toolbar" }, h("label", { for: "followToggle" }, cb, " 自APのchに自動で合わせる"),
    h("span", { class: "sub", text: "自チームのAPが別のchに移ったら、自chもそこに合わせて知らせます。手で自chを変えたあとは、自APが動いたときだけ合わせます。" }));
}

// ------------------------------------------------------------------ 自チームのAP（MACアドレスかSSIDで登録）
const OWN_AP_MODE = {
  bssid: "MACアドレス（BSSID）で見分けています。登録したAPだけが自チームです。同じSSIDの別の局は「SSIDのなりすまし」の警報になります。",
  ssid: "SSIDで見分けています。この名前の局はすべて自チームです（ルータを替えても追従します）。MACアドレスを1つでも登録すると、MACアドレスで見分けます。",
  none: "未設定です。自チームのAPを見分けられません（自APの強さの記録、なりすましの見張りが使えません）。",
};

async function saveOwnAp(bssids, ssids, msg) {
  try {
    const r = await api("/api/own_ap", { method: "POST", body: { bssids, ssids } });
    if (ui.state) ui.state.own.ap = r;
    renderOwnApCurrent();
    toast(msg);
    // 周辺の一覧の「種類」（自チームか）は、次に状態を読んでから描き直す
    setTimeout(() => ui.ownApNearby && ui.ownApNearby(), 2500);
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

function buildOwnAp() {
  const wrap = h("div", { class: "own-ap" });
  // 手で入力
  const kind = h("select", { "aria-label": "入力する種類" }, h("option", { value: "bssid", text: "MACアドレス" }), h("option", { value: "ssid", text: "SSID" }));
  const val = h("input", { type: "text", maxlength: 40, placeholder: "例：aa:bb:cc:dd:ee:ff または OUR-ROBOT-6G", "aria-label": "MACアドレスかSSID" });
  const add = h("button", { class: "btn primary", type: "button", text: "追加", onclick: async () => {
    const ap = ui.state.own.ap;
    const v = val.value.trim();
    if (!v) return toast("MACアドレスかSSIDを入れてください", true);
    const ok = kind.value === "bssid"
      ? await saveOwnAp([...ap.bssids, v], ap.ssids, `MACアドレス ${v} を自チームにしました`)
      : await saveOwnAp(ap.bssids, [...ap.ssids, v], `SSID「${v}」を自チームにしました`);
    if (ok) val.value = "";
  } });
  // 周辺のWi-Fiから選ぶ（押したときの一覧。2秒ごとには並べ替えない）
  const q = h("input", { type: "search", placeholder: "SSID・MACアドレス・機種で絞り込み", "aria-label": "周辺のWi-Fiを絞り込み" });
  const list = h("div", { class: "table-wrap own-ap-list" });
  const refresh = h("button", { class: "btn", type: "button", text: "周辺のWi-Fiを読み直す", onclick: () => renderOwnApNearby(list, q.value) });
  q.addEventListener("input", () => renderOwnApNearby(list, q.value));
  ui.ownApNearby = () => renderOwnApNearby(list, q.value);
  wrap.append(h("div", { id: "ownApCurrent" }),
    h("div", { class: "toolbar" }, kind, val, add),
    h("h4", { text: "周辺のWi-Fiから選ぶ（強い順）" }), h("div", { class: "toolbar" }, q, refresh), list);
  setTimeout(() => { renderOwnApCurrent(); renderOwnApNearby(list, ""); }, 0);
  return wrap;
}

function renderOwnApCurrent() {
  const el = $("ownApCurrent");
  if (!el || !ui.state) return;
  const ap = ui.state.own.ap;
  const seen = (ui.state.stations || []).filter((q2) => q2.kind === "own" && q2.present);
  // ボタンのある部分は、登録が変わったときだけ作り直す（2秒ごとに作り直すと、押した瞬間のボタンが消えることがある）
  const key = JSON.stringify(ap);
  if (el.dataset.key !== key) {
    el.dataset.key = key;
    const chips = h("div", { class: "toolbar" });
    const chip = (label, title, onRemove) => h("span", { class: "party own-chip", title },
      h("span", { text: label }), h("button", { type: "button", class: "chip-x", "aria-label": `${label} を外す`, text: "×", onclick: onRemove }));
    for (const b of ap.bssids) chips.append(chip(`MAC ${b}`, "このAPだけが自チーム", () => saveOwnAp(ap.bssids.filter((x) => x !== b), ap.ssids, `MACアドレス ${b} を外しました`)));
    for (const x of ap.ssids) chips.append(chip(`SSID ${x}`, ap.match === "bssid" ? "なりすましの見張りに使う（同じSSIDの別の局を警報に）" : "この名前の局はすべて自チーム",
      () => saveOwnAp(ap.bssids, ap.ssids.filter((y) => y !== x), `SSID「${x}」を外しました`)));
    clear(el).append(h("p", { class: "sub", text: OWN_AP_MODE[ap.match] }), chips, h("p", { class: "sub", id: "ownApSeen" }));
  }
  $("ownApSeen").textContent = seen.length
    ? `いま見えている自チームのAP：${seen.map((x) => `${ssidText(x)}（${chLabel(x.freq)}・${x.rssi}dBm）`).join("、")}`
    : "いま見えている自チームのAPはありません";
}

function renderOwnApNearby(list, filter) {
  const st = ui.state;
  if (!st) return;
  const f = filter.trim().toLowerCase();
  const rows = st.stations.filter((x) => x.present && x.rssi != null)
    .filter((x) => !f || [x.ssid, x.bssid, x.model].some((v) => v && v.toLowerCase().includes(f)))
    .sort((a, b) => b.rssi - a.rssi).slice(0, 60);
  const t = h("table", { class: "data" }, h("thead", null, h("tr", null,
    ...["強さ", "SSID", "MACアドレス", "主ch・幅", "種類・機種", ""].map((x) => h("th", { text: x })))));
  const tb = h("tbody");
  for (const x of rows) {
    const k = kindOf(x.kind);
    const ap = st.own.ap;
    const byMac = h("button", { class: "btn", type: "button", text: "MACで登録", disabled: ap.bssids.includes(x.bssid), onclick: (ev) => { ev.stopPropagation(); pickOwnAp(x, "bssid"); } });
    const bySsid = h("button", { class: "btn", type: "button", text: "SSIDで登録", disabled: !x.ssid || ap.ssids.includes(x.ssid), title: x.ssid ? "" : "非公開SSIDはSSIDで登録できません", onclick: (ev) => { ev.stopPropagation(); pickOwnAp(x, "ssid"); } });
    tb.append(h("tr", null,
      h("td", { class: "num", text: x.rssi }), h("td", { text: ssidText(x) }), h("td", { class: "mono", text: x.bssid }),
      h("td", { text: `${chLabel(x.freq)}・${x.width_label}MHz` }),
      h("td", null, h("span", { class: `glyph ${k.cls}`, text: k.glyph + " " }), h("span", { text: `${k.label}${x.model ? `・${x.model}` : ""}` })),
      h("td", { class: "wrap" }, byMac, " ", bySsid)));
  }
  t.append(tb);
  clear(list).append(rows.length ? t : h("p", { class: "sub", text: "当てはまる局がありません（いる局だけ）" }));
}

/** 周辺の局を自チームに登録する。自chと違えば、自chも合わせるか聞く。 */
async function pickOwnAp(x, by) {
  const ap = ui.state.own.ap;
  const ok = by === "bssid"
    ? await saveOwnAp([...ap.bssids, x.bssid], ap.ssids, `${ssidText(x)}（${x.bssid}）を自チームにしました`)
    : await saveOwnAp(ap.bssids, [...ap.ssids, x.ssid], `SSID「${x.ssid}」を自チームにしました`);
  if (!ok) return;
  const own = ui.state.own;
  if (own.follow && own.follow.enabled) {
    // 自動で合わせる設定なら、聞かずに合わせる（数秒あとに自chが変わる）
    if (x.freq !== own.freq || x.width !== own.width) toast(`自chは自APに自動で合わせます（${chLabel(x.freq)}・${x.width}MHz）`);
    return;
  }
  if (x.freq !== own.freq || x.width !== own.width) {
    const go = await confirmDialog("自chも合わせますか？", `この局は ${chLabel(x.freq)}・${x.width}MHz です（いまの自chは ${own.label}・${own.width}MHz）。自chを変えると、監視する範囲（自ブロック）と警報の基準が変わります。`, "合わせる");
    if (go) {
      try { await api("/api/own", { method: "POST", body: { channel: x.freq, width: x.width } }); toast("自chも合わせました"); ui.bands = null; }
      catch (e) { toast(e.message, true); }
    }
  }
}

function renderSettingsLive() {
  const st = ui.state;
  renderOwnApCurrent();
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
  positionSplitters();
  renderAlerts(st);
  renderOwnTile(st);
  renderSuggestions(st);
  renderCalibration(st);
  $("cntStations").textContent = st.stations.filter((q) => q.present).length;
  $("cntClients").textContent = st.clients;
  renderTab(false);
  // 引き出しで入力している間（タグ・機種の名前）は作り直さない（打った文字が消えないように）
  const typing = $("drawer").contains(document.activeElement) && /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
  if (!$("drawer").hidden && ui.selected && !typing && Date.now() - (ui._drawerAt || 0) > 2000) {
    ui._drawerAt = Date.now();
    if (ui.selectedMany) openDrawerMany(ui.selectedMany); else openDrawer(ui.selected);
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
  // 表示する帯域のキー（入力欄で打っているときは使わない）
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey || /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName || "")) return;
    const v = { "0": "all", "6": "6", "5": "5", "2": "2.4" }[e.key];
    if (v) setBandView(v);
  });
  buildViewbar();
  applyBandView();
  applySplit();
  buildSplitters();
  // 下の段（タブの中身）や右の欄の長さが変わったら、境目の位置を合わせ直す
  const ro = new ResizeObserver(() => positionSplitters());
  for (const e of [$("layout"), document.querySelector(".side"), document.querySelector(".lower")]) ro.observe(e);
  $("segclock").addEventListener("click", () => {
    ui.clockMetric = CLOCK_METRICS[(CLOCK_METRICS.indexOf(ui.clockMetric) + 1) % CLOCK_METRICS.length];
    prefs.set("clockMetric", ui.clockMetric);
    renderClock();
  });
  renderClock();
  setInterval(renderClock, 1000);
  let rt = null;
  addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => ui.state && render(ui.state), 150); });
  setTab(ui.tab);
  if (ui.selected) $("drawer").hidden = true;
  ui.selected = null;
  poll();
}
init();
