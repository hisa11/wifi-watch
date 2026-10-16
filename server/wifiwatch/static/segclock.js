// 7セグメントの時計（ユーザーの時計プログラム /home/hisa/publish/Clock の見た目を、画面の中で作り直したもの）。
// 上の段：月/日・曜日・判定のドット絵（Wi-Fiの扇。色が判定）。下の段：値（温度の代わり。いる局の数など）・時:分。
// 色は元の時計に合わせる：ブロック（セグメント・ドット）はそれぞれ単色で、ブロックごとに色を変えてグラデーションに見せる。
// 背景は黒（明るい画面でも暗い画面でも同じ）。
const SVGNS = "http://www.w3.org/2000/svg";
const SEGS = {
  "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd", "6": "afgedc", "7": "abc",
  "8": "abcdefg", "9": "abcdfg", "-": "g", " ": "",
};
// 全体は横長（約2.2:1）にして右の欄の幅いっぱいに収める。数字・文字の形（縦横の比）は元の時計のまま
// （上の段の数字 0.54、下の段の値 0.47、時:分 0.56）。上の段は数字の高さを下げ、そのぶん横に詰めて並べる
const W = 626;
const H = 282;

function el(tag, attrs, ...kids) {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null) e.setAttribute(k, String(v));
  for (const k of kids) if (k != null) e.append(k);
  return e;
}

/**
 * 桁ごとのセグメントの色（a〜g の順）。ユーザーの時計の写真から、点いているセグメントの色を読み取り、
 * 点いていなかったセグメントは近くの色から決めた。上の段：月の十の位は黄→緑、一の位も黄→緑、
 * 日の十の位はサーモン、一の位は桃→紫。下の段：値は黄・黄緑・緑、時は赤・赤紫、分は桃→赤→緑・黄緑→緑。
 */
const pal = (s) => Object.fromEntries(s.split(" ").map((c, i) => ["abcdefg"[i], `#${c}`]));
const PAL = {
  mo0: pal("f6dc28 f8da25 41eb8a 43eba6 44ec98 f2d82a a8ec50"),
  mo1: pal("f7e325 e8f328 4cef9d 43ebb9 42fa9e f3dc1d b4f046"),
  da0: pal("f68c62 f78665 fb7f6d f87575 fa7a70 f59066 f9806a"),
  da1: pal("eb53a5 ec56ac dd64f7 a854f3 ef67c8 ef5d87 f860b6"),
  v0: pal("f5e030 e6f030 b8f040 c8ee3c e8e638 f4d830 f0e434"),
  v1: pal("c0fc3a b2ec3d a6ee44 b1f439 d1e838 c8f23a c3f02e"),
  v2: pal("4ced8c 45eb85 43fb6e 44f070 46f475 50ea92 48f07c"),
  h0: pal("f0507a f25173 e95a43 e85e44 ea5c48 f05480 ee5660"),
  h1: pal("ed53e3 f76bfa d961f5 dc5cf2 f160ed fb69ed f661f0"),
  m0: pal("ec61a0 ee649a 9cf841 93ee51 c0ec4c f266be f76963"),
  m1: pal("c7ed2c daf630 8ef25a 43f17e 47fa46 cdee34 b1f83e"),
};
const C = { slash: "#55e0f2", bar: "#cc50eb", colon: "#c963eb", weekday: "#55e0f2", unit: "#619bf2", caption: "#8861f2" };

/** 1桁（x, y 左上、w×h、太さ t、色 p）。点いているセグメントだけ描く（黒い縁で隙間を見せる）。 */
function digit(g, ch, x, y, w, h, t, p) {
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
    g.append(el("polygon", { points: poly[s].map((q) => q.join(",")).join(" "), fill: p[s], stroke: "#000", "stroke-width": 3, "stroke-linejoin": "round" }));
  }
}

/** 文字。max（幅の上限）を超えそうなら、詰めて収める（全角は1文字、半角は0.6文字で見積もる）。 */
function text(g, s, x, y, size, fill, max = null) {
  const est = [...s].reduce((a, c) => a + (c.charCodeAt(0) < 0x2000 ? 0.6 : 1), 0) * size;
  const fit = max != null && est > max ? { textLength: max, lengthAdjust: "spacingAndGlyphs" } : {};
  g.append(el("text", { x, y, "font-size": size, "font-weight": 800, fill,
    "font-family": "system-ui, sans-serif", ...fit }, s));
}

/**
 * 判定のドット絵（Wi-Fiの扇、9×8）。ドットはそれぞれ単色で、右上から左下へ色を少しずつ変える（元の時計のドット絵と同じ見せ方）。
 * 色の幅は判定で変える：緊急＝橙→赤→桃、警告＝黄→橙、検知なし＝黄緑→緑→青緑、見ていない・届かない＝灰。
 */
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
const LEVEL_HUES = { crit: [22, -35], warn: [52, 14], none: [80, 170] };

function wifiIcon(g, x, y, px, level) {
  const range = LEVEL_HUES[level];
  const rows = WIFI.length, cols = WIFI[0].length;
  WIFI.forEach((row, r) => [...row].forEach((c, k) => {
    if (c !== "#") return;
    const t = ((cols - 1 - k) / (cols - 1)) * 0.55 + (r / (rows - 1)) * 0.45; // 右上 0 → 左下 1
    const jh = (((r * 7 + k * 13) % 5) - 2) * 3; // ドットごとに少しずらす（同じ位置はいつも同じ色）
    const jl = (((r * 5 + k * 3) % 3) - 1) * 3;
    const fill = range
      ? `hsl(${(((range[0] + (range[1] - range[0]) * t + jh) % 360) + 360) % 360} 90% ${66 + jl}%)`
      : `hsl(0 0% ${74 - t * 22 + jl}%)`;
    g.append(el("rect", { x: x + k * px, y: y + r * px, width: px - 3, height: px - 3, fill }));
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
  svg.append(el("rect", { x: 0, y: 0, width: W, height: H, rx: 12, fill: "#000" }));
  const g = el("g");
  // 上の段：月/日・曜日・判定のドット絵
  const [mo, da] = [parts.month.padStart(2, " "), parts.day.padStart(2, " ")];
  const top = 10, th = 130, tw = 70, tt = 17;
  digit(g, mo[0] === "0" ? " " : mo[0], 10, top, tw, th, tt, PAL.mo0);
  digit(g, mo[1], 90, top, tw, th, tt, PAL.mo1);
  g.append(el("line", { x1: 176, y1: top + th - 6, x2: 200, y2: top + 6, stroke: C.slash, "stroke-width": 12, "stroke-linecap": "round" }));
  digit(g, da[0] === "0" ? " " : da[0], 216, top, tw, th, tt, PAL.da0);
  digit(g, da[1], 296, top, tw, th, tt, PAL.da1);
  text(g, parts.weekday, 380, 42, 26, C.weekday);
  g.append(el("rect", { x: 424, y: top + 4, width: 7, height: th - 8, rx: 3, fill: C.bar }));
  wifiIcon(g, 455, top + 4, 16, level);
  // 仕切りの線（写真のとおり、赤→桃→赤紫→紫）
  const defs = el("defs", null, el("linearGradient", { id: "segclock-line", x1: 0, x2: 1, y1: 0, y2: 0 },
    el("stop", { offset: "0", "stop-color": "#ed343d" }), el("stop", { offset: "0.4", "stop-color": "#ed348f" }),
    el("stop", { offset: "0.78", "stop-color": "#ed34e5" }), el("stop", { offset: "1", "stop-color": "#cc34ed" })));
  svg.append(defs);
  g.append(el("rect", { x: 10, y: 148, width: W - 20, height: 7, rx: 3, fill: "url(#segclock-line)" }));
  // 下の段：値（温度の代わり）
  const by = 164, bh = 110;
  const v = String(value.text).slice(-3).padStart(3, " ");
  [...v].forEach((c, i) => digit(g, c, 10 + i * 62, by, 52, bh, 15, [PAL.v0, PAL.v1, PAL.v2][i]));
  text(g, value.unit, 194, by + 44, value.unit.length > 2 ? 22 : 30, C.unit, 76);
  value.caption.split("\n").slice(0, 2).forEach((line, i) => text(g, line, 194, by + 82 + i * 22, 17, C.caption, 76));
  g.append(el("rect", { x: 280, y: by + 2, width: 6, height: bh - 4, rx: 3, fill: C.bar }));
  // 時:分
  const hh = parts.hour.padStart(2, "0");
  const mm = parts.minute.padStart(2, "0");
  digit(g, hh[0], 300, by, 62, bh, 16, PAL.h0);
  digit(g, hh[1], 374, by, 62, bh, 16, PAL.h1);
  for (const cy of [by + 30, by + 68]) g.append(el("rect", { x: 451, y: cy, width: 13, height: 13, fill: C.colon }));
  digit(g, mm[0], 480, by, 62, bh, 16, PAL.m0);
  digit(g, mm[1], 554, by, 62, bh, 16, PAL.m1);
  svg.append(g);
  container.replaceChildren(svg);
}
