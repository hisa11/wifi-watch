// 7セグメントの時計（ユーザーの時計プログラム /home/hisa/publish/Clock の見た目を、画面の中で作り直したもの）。
// 上の段：月/日・曜日・判定のドット絵（Wi-Fiの扇。色が判定）。下の段：値（温度の代わり。いる局の数など）・時:分。
// 数字は虹色のグラデーション（セグメントの位置で色が変わる）。背景は黒（明るい画面でも暗い画面でも同じ）。
const SVGNS = "http://www.w3.org/2000/svg";
const SEGS = {
  "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd", "6": "afgedc", "7": "abc",
  "8": "abcdefg", "9": "abcdfg", "-": "g", " ": "",
};
const W = 640;
const H = 360;

function el(tag, attrs, ...kids) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, String(v));
  for (const k of kids) if (k != null) e.append(k);
  return e;
}

/** 位置で決まる色（左上の黄色から、右へ緑・水色・青・紫・桃色へ。下へ少しずれる）。 */
function hue(cx, cy) {
  return (50 + (cx / W) * 290 + (cy / H) * 70) % 360;
}
const col = (cx, cy, l = 58) => `hsl(${hue(cx, cy).toFixed(0)} 90% ${l}%)`;

/** 1桁（x, y 左上、w×h、太さ t）。点いているセグメントだけ描く（黒い縁で隙間を見せる）。 */
function digit(g, ch, x, y, w, h, t) {
  const on = SEGS[ch] ?? "";
  const m = y + h / 2;
  const poly = {
    a: [[x, y], [x + w, y], [x + w - t, y + t], [x + t, y + t]],
    b: [[x + w, y], [x + w, m], [x + w - t, m - t / 2], [x + w - t, y + t]],
    c: [[x + w, m], [x + w, y + h], [x + w - t, y + h - t], [x + w - t, m + t / 2]],
    d: [[x + t, y + h - t], [x + w - t, y + h - t], [x + w, y + h], [x, y + h]],
    e: [[x, m], [x + t, m + t / 2], [x + t, y + h - t], [x, y + h]],
    f: [[x, y], [x + t, y + t], [x + t, m - t / 2], [x, m]],
    g: [[x + t * 0.5, m], [x + t, m - t / 2], [x + w - t, m - t / 2], [x + w - t * 0.5, m], [x + w - t, m + t / 2], [x + t, m + t / 2]],
  };
  for (const s of on) {
    const pts = poly[s];
    const cx = pts.reduce((a, p) => a + p[0], 0) / pts.length;
    const cy = pts.reduce((a, p) => a + p[1], 0) / pts.length;
    g.append(el("polygon", { points: pts.map((p) => p.join(",")).join(" "), fill: col(cx, cy), stroke: "#000", "stroke-width": 3, "stroke-linejoin": "round" }));
  }
}

/** 文字。max（幅の上限）を超えそうなら、詰めて収める（全角は1文字、半角は0.6文字で見積もる）。 */
function text(g, s, x, y, size, max = null) {
  const est = [...s].reduce((a, c) => a + (c.charCodeAt(0) < 0x2000 ? 0.6 : 1), 0) * size;
  const fit = max != null && est > max ? { textLength: max, lengthAdjust: "spacingAndGlyphs" } : {};
  g.append(el("text", { x, y, "font-size": size, "font-weight": 800, fill: col(x, y, 66),
    "font-family": "system-ui, sans-serif", ...fit }, s));
}

/** 判定のドット絵（Wi-Fiの扇、9×8）。色は判定：緊急＝赤、警告＝黄、検知なし＝緑、見ていない・届かない＝灰。 */
const WIFI = [
  "..#####..",
  ".#.....#.",
  "#..###..#",
  "..#...#..",
  ".#..#..#.",
  "...#.#...",
  "....#....",
  "....#....",
];
const LEVEL_HUES = { crit: [350, 10], warn: [45, 30], none: [140, 180], blind: [0, 0] };

function wifiIcon(g, x, y, px, level) {
  const [h0, h1] = LEVEL_HUES[level] || LEVEL_HUES.blind;
  const gray = level === "blind" || !LEVEL_HUES[level];
  WIFI.forEach((row, r) => [...row].forEach((c, k) => {
    if (c !== "#") return;
    const hh = h0 + ((h1 - h0) * r) / (WIFI.length - 1);
    g.append(el("rect", { x: x + k * px, y: y + r * px, width: px - 3, height: px - 3,
      fill: gray ? `hsl(0 0% ${70 - r * 4}%)` : `hsl(${hh} 90% ${62 - r * 2}%)` }));
  }));
}

/**
 * 時計を描く。
 * now：Date、value：{ text: "36", unit: "局", caption: "いる局\n全帯域" }（caption は2行まで）、level：判定（crit/warn/none/blind）
 */
export function drawClock(container, now, value, level) {
  const tz = { timeZone: "Asia/Tokyo" };
  const parts = Object.fromEntries(new Intl.DateTimeFormat("ja-JP", { ...tz, month: "2-digit", day: "2-digit", hour: "2-digit",
    minute: "2-digit", weekday: "short", hour12: false }).formatToParts(now).map((p) => [p.type, p.value]));
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", class: "segclock-svg",
    "aria-label": `${parts.month}月${parts.day}日（${parts.weekday}）${parts.hour}時${parts.minute}分・${value.caption.replace("\n", " ")} ${value.text}${value.unit}` });
  svg.append(el("rect", { x: 0, y: 0, width: W, height: H, rx: 14, fill: "#000" }));
  const g = el("g");
  // 上の段：月/日
  const [mo, da] = [parts.month.padStart(2, " "), parts.day.padStart(2, " ")];
  digit(g, mo[0] === "0" ? " " : mo[0], 14, 14, 92, 172, 22);
  digit(g, mo[1], 116, 14, 92, 172, 22);
  g.append(el("line", { x1: 222, y1: 182, x2: 254, y2: 22, stroke: col(238, 100), "stroke-width": 14, "stroke-linecap": "round" }));
  text(g, parts.weekday, 268, 40, 26);
  digit(g, da[0] === "0" ? " " : da[0], 272, 14, 92, 172, 22);
  digit(g, da[1], 374, 14, 92, 172, 22);
  g.append(el("rect", { x: 480, y: 22, width: 7, height: 160, rx: 3, fill: col(483, 100) }));
  wifiIcon(g, 503, 46, 15, level);
  // 仕切りの線（赤→桃→紫）
  const defs = el("defs", null, el("linearGradient", { id: "segclock-line", x1: 0, x2: 1, y1: 0, y2: 0 },
    el("stop", { offset: "0", "stop-color": "hsl(355 90% 60%)" }), el("stop", { offset: "0.5", "stop-color": "hsl(320 90% 62%)" }),
    el("stop", { offset: "1", "stop-color": "hsl(280 90% 64%)" })));
  svg.append(defs);
  g.append(el("rect", { x: 14, y: 204, width: W - 28, height: 7, rx: 3, fill: "url(#segclock-line)" }));
  // 下の段：値（温度の代わり）
  const v = String(value.text).slice(-3).padStart(3, " ");
  [...v].forEach((c, i) => digit(g, c, 14 + i * 62, 230, 52, 110, 15));
  text(g, value.unit, 206, 270, value.unit.length > 2 ? 22 : 30, 72);
  value.caption.split("\n").slice(0, 2).forEach((line, i) => text(g, line, 206, 312 + i * 22, 17, 72));
  g.append(el("rect", { x: 286, y: 232, width: 6, height: 106, rx: 3, fill: col(289, 285) }));
  // 時:分
  const hh = parts.hour.padStart(2, "0");
  const mm = parts.minute.padStart(2, "0");
  digit(g, hh[0], 306, 230, 62, 110, 16);
  digit(g, hh[1], 378, 230, 62, 110, 16);
  for (const cy of [262, 306]) g.append(el("rect", { x: 451, y: cy, width: 13, height: 13, fill: col(457, cy) }));
  digit(g, mm[0], 474, 230, 62, 110, 16);
  digit(g, mm[1], 548, 230, 62, 110, 16);
  svg.append(g);
  container.replaceChildren(svg);
}
