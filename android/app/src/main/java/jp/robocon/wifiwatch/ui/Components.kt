package jp.robocon.wifiwatch.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.clipRect
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.drawText
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.rememberTextMeasurer
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import jp.robocon.wifiwatch.core.Channels
import jp.robocon.wifiwatch.core.PcState

/** 選んでいるかが色だけに頼らず分かるチップ（選んでいる間は ✓ を付ける）。 */
@Composable
fun Pick(selected: Boolean, onClick: () -> Unit, label: @Composable () -> Unit) = FilterChip(
    selected = selected, onClick = onClick, label = label,
    leadingIcon = if (selected) ({ Text("✓", fontWeight = FontWeight.Bold) }) else null,
)

// ------------------------------------------------------------------ 判定のカード（一目で）
data class Verdict(val level: String, val glyph: String, val title: String, val sub: String)

fun verdictOf(live: MainViewModel.Live): Verdict {
    val s = live.state
    val age = live.ageMsNow()
    if (s == null) return Verdict("blind", "◌", "PCの状態がまだありません",
        live.error ?: "PCが中継に状態を置くまで待っています（開いてから10秒ほど）")
    if (age != null && age > STALE_MS) {
        // 端末が中継に届かないのか、中継には届くがPCが状態を置いていないのかを分けて出す（直す場所が違う）
        return if (live.error != null) Verdict("blind", "◌", "中継に届かない",
            "最後の状態は${ago(age / 1000.0)}（この間のことは分かりません）・${live.error}")
        else Verdict("blind", "◌", "PCから応答なし",
            "中継には届いています。最後の状態は${ago(age / 1000.0)}（この間のことは分かりません）")
    }
    val blind = blindList(s)
    return when {
        s.rxState == "failed" -> Verdict("crit", "■", "受信停止", s.rxReason.ifEmpty { "2.4/5GHzでフレームなし" })
        s.crit > 0 -> Verdict("crit", "■", "緊急 ${s.crit}", s.alerts.firstOrNull { it.level == "crit" }?.title.orEmpty() + if (s.warn > 0) "・警告 ${s.warn}" else "")
        s.warn > 0 -> Verdict("warn", "▲", "警告 ${s.warn}", s.alerts.firstOrNull { it.level == "warn" }?.title.orEmpty())
        s.manual != null && s.manual.state != "done" -> Verdict("blind", "◌", "検知なし", "手動スキャン中は自chを見ていません")
        else -> Verdict("none", "○", "検知なし", if (blind.isEmpty()) "ピットから見えた範囲で（安全という意味ではありません）" else "見ていない：" + blind.joinToString("・"))
    }
}

/** これより古い状態は「いまの様子」として出さない */
const val STALE_MS = 30_000L

fun blindList(s: PcState): List<String> {
    val out = mutableListOf<String>()
    s.excludedBands.forEach { out += "${it}GHz（巡回外）" }
    val ex = s.excludedFreqs.filter { Channels.bandOf(it) !in s.excludedBands }
    if (ex.size in 1..3) ex.forEach { out += "${Channels.label(it)}（巡回外）" }
    else if (ex.isNotEmpty()) out += "${ex.size}ch（巡回外）"
    if (s.rxState == "failed") out += "全帯域（受信停止）"
    return out
}

fun ago(sec: Double): String = when {
    sec < 1.5 -> "今"
    sec < 90 -> "${sec.toInt()}秒前"
    sec < 5400 -> "${(sec / 60).toInt()}分前"
    else -> "%.1f時間前".format(sec / 3600)
}

@Composable
fun VerdictCard(v: Verdict, modifier: Modifier = Modifier) {
    val w = LocalW.current
    val (bg, fg) = when (v.level) {
        "crit" -> w.crit to Color.White
        "warn" -> w.warn to Color(0xFF0B0B0B)
        "blind" -> w.surface3 to w.ink
        else -> w.surface2 to w.ink
    }
    Row(
        modifier.fillMaxWidth().background(bg, RoundedCornerShape(14.dp)).padding(horizontal = 16.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(v.glyph, color = fg, fontSize = 30.sp, modifier = Modifier.width(40.dp))
        Column {
            Text(v.title, color = fg, fontSize = 26.sp, fontWeight = FontWeight.Bold)
            if (v.sub.isNotEmpty()) Text(v.sub, color = fg.copy(alpha = 0.9f), fontSize = 13.sp)
        }
    }
}

// ------------------------------------------------------------------ chの帯（見ているか・強い局）
private val NOT_WATCHING = setOf("x", "z", "t", "u", "!")

private val CODE_JP = mapOf(
    "o" to "自ch（受信中）", "i" to "自ブロック内（巡回で見る）", "p" to "巡回中", "x" to "巡回外（見ていない）",
    "z" to "手動スキャン中で止めている", "s" to "手動スキャンで今見ている", "t" to "手動スキャン待ち", "!" to "受信停止中", "u" to "まだ見ていない",
)

@Composable
fun ChannelStrip(state: PcState, modifier: Modifier = Modifier) {
    val w = LocalW.current
    val tm = rememberTextMeasurer()
    var picked by remember { mutableStateOf<PcState.ChannelState?>(null) }
    Column(modifier) {
        for (band in Channels.BANDS) {
            val rows = state.channels.filter { Channels.bandOf(it.freq) == band }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(Channels.BAND_LABEL[band]!!, fontSize = 11.sp, color = w.muted, modifier = Modifier.width(48.dp))
                Canvas(
                    Modifier.weight(1f).height(30.dp).pointerInput(rows) {
                        detectTapGestures { p ->
                            val i = (p.x / (size.width / rows.size.toFloat())).toInt().coerceIn(0, rows.lastIndex)
                            picked = rows[i]
                        }
                    },
                ) {
                    val cw = size.width / rows.size
                    rows.forEachIndexed { i, c ->
                        val x = i * cw
                        val cell = Size(cw - 2f, 16.dp.toPx())
                        val top = Offset(x + 1f, 0f)
                        when (c.code) {
                            "o" -> drawRect(w.own, top, cell)
                            "i" -> drawRect(w.own.copy(alpha = 0.35f), top, cell)
                            "s" -> drawRect(w.accent, top, cell)
                            "x", "z", "t", "u" -> hatch(top, cell, w.hatch, w.surface3)
                            "!" -> hatch(top, cell, w.crit, w.surface3)
                            else -> drawRect(if (c.nOverlap > 0) w.ink2.copy(alpha = (0.15f + 0.12f * c.nOverlap).coerceAtMost(0.6f)) else w.surface3, top, cell)
                        }
                        // 見ていないchの点は、前に見えた局（いまの様子ではない）なので白抜きにする
                        val seen = c.code !in NOT_WATCHING
                        val dot = Offset(x + cw / 2, 16.dp.toPx() + 5.dp.toPx())
                        if (c.nStrong > 0) {
                            val col = if (c.nStrong >= 3) w.warn else w.ink2
                            val rad = (if (c.nStrong >= 3) 3 else 2).dp.toPx()
                            if (seen) drawCircle(col, rad, dot) else drawCircle(col, rad, dot, style = Stroke(1.dp.toPx()))
                        }
                        if (c.freq == state.ownFreq) {
                            drawText(tm, "${Channels.freqToNumber(c.freq)}", Offset(x, 16.dp.toPx() + 2f), TextStyle(fontSize = 9.sp, color = w.own, fontWeight = FontWeight.Bold))
                        }
                    }
                }
            }
            Spacer(Modifier.height(2.dp))
        }
        val p = picked
        Text(
            if (p == null) "帯をタップするとchの様子が出ます。斜線＝見ていない、点＝強い局（黄＝3つ以上、白抜き＝前に見えた局）"
            else "${Channels.label(p.freq)}：${CODE_JP[p.code] ?: p.code}・重なる局 ${p.nOverlap}（強い ${p.nStrong}）${p.maxRssi?.let { "・最大 ${it}dBm" } ?: ""}" +
                if (p.code in NOT_WATCHING && p.nOverlap > 0) "（前に見えた局）" else "",
            fontSize = 12.sp, color = w.ink2,
        )
    }
}

private fun DrawScope.hatch(top: Offset, size: Size, line: Color, bg: Color) {
    drawRect(bg, top, size)
    clipRect(top.x, top.y, top.x + size.width, top.y + size.height) {
        var x = top.x - size.height
        while (x < top.x + size.width) {
            drawLine(line, Offset(x, top.y + size.height), Offset(x + size.height, top.y), 1.5f)
            x += 5f
        }
    }
}

// ------------------------------------------------------------------ アナライザーのグラフ（ch×強さ、台形の幅＝帯域幅）
data class SpecStation(val name: String, val kind: String?, val spans: List<Channels.Span>, val rssi: Int?)

private val SEGMENTS = mapOf(
    Channels.B6 to listOf(5925.0 to 6425.0),
    Channels.B5 to listOf(5170.0 to 5330.0, 5490.0 to 5730.0),
    Channels.B24 to listOf(2401.0 to 2483.0),
)

@Composable
fun Spectrum(band: String, stations: List<SpecStation>, modifier: Modifier = Modifier, ownFreq: Int? = null, maxLabels: Int = 6) {
    val w = LocalW.current
    val tm = rememberTextMeasurer()
    Canvas(modifier.fillMaxWidth().height(170.dp).border(1.dp, w.grid, RoundedCornerShape(8.dp))) {
        val l = 30.dp.toPx()
        val r = 6.dp.toPx()
        val t = 14.dp.toPx()
        val b = size.height - 16.dp.toPx()
        val segs = SEGMENTS[band]!!
        val total = segs.sumOf { it.second - it.first }
        val gap = if (segs.size > 1) 10.dp.toPx() else 0f
        val avail = size.width - l - r - gap * (segs.size - 1)
        fun x(f: Double): Float {
            var off = l
            segs.forEachIndexed { i, (lo, hi) ->
                val segW = ((hi - lo) / total * avail).toFloat()
                if (f <= hi || i == segs.lastIndex) return off + (((f - lo) / (hi - lo)).coerceIn(0.0, 1.0) * segW).toFloat()
                off += segW + gap
            }
            return size.width - r
        }
        fun y(d: Int): Float = t + ((-20 - d.coerceIn(-95, -20)) / 75f) * (b - t)
        for (d in -90..-30 step 20) {
            drawLine(w.grid, Offset(l, y(d)), Offset(size.width - r, y(d)), 1f)
            drawText(tm, "$d", Offset(2f, y(d) - 7.dp.toPx()), TextStyle(fontSize = 9.sp, color = w.muted))
        }
        if (ownFreq != null && Channels.bandOf(ownFreq) == band) {
            drawRect(w.ownBand, Offset(x(ownFreq - 10.0), t), Size(x(ownFreq + 10.0) - x(ownFreq - 10.0), b - t))
        }
        val sorted = stations.filter { it.rssi != null }.sortedBy { it.rssi }
        for (s in sorted) {
            val c = kindColor(w, s.kind)
            val yt = y(s.rssi!!)
            for (sp in s.spans) {
                val xa = x(sp.lo)
                val xz = x(sp.hi)
                val sl = minOf(5f, (xz - xa) * 0.12f)
                val path = Path().apply { moveTo(xa, b); lineTo(xa + sl, yt); lineTo(xz - sl, yt); lineTo(xz, b) }
                drawPath(Path().apply { addPath(path); close() }, c.copy(alpha = 0.10f))
                drawPath(path, c, style = Stroke(2.dp.toPx()))
            }
        }
        drawLine(w.ink2.copy(alpha = 0.4f), Offset(l, b), Offset(size.width - r, b), 1f)
        val labelled = mutableListOf<androidx.compose.ui.geometry.Rect>()
        for (s in sorted.reversed().distinctBy { it.name }.take(maxLabels)) {
            val sp = s.spans.first()
            val xa = x(sp.lo)
            val xz = x(sp.hi)
            val text = s.name.take(16)
            val m = tm.measure(text, TextStyle(fontSize = 10.sp, fontWeight = FontWeight.SemiBold))
            val mw = m.size.width.toFloat()
            val mh = m.size.height.toFloat()
            val yt = y(s.rssi!!)
            // 広い局（160/320MHz）は、真ん中が他の名前とぶつかったら上辺に沿ってずらす。それでもだめなら上辺のすぐ内側
            val wide = xz - xa > mw * 2
            val xs = (if (wide) listOf(0.5f, 0.25f, 0.75f, 0.12f, 0.88f) else listOf(0.5f)).map { xa + (xz - xa) * it }
            val ys = if (wide) listOf(yt - mh - 2f, yt + 2f) else listOf(yt - mh - 2f)
            val rect = ys.firstNotNullOfOrNull { top ->
                xs.firstNotNullOfOrNull { cx ->
                    val left = (cx - mw / 2f).coerceIn(l, size.width - r - mw)
                    androidx.compose.ui.geometry.Rect(Offset(left, top), Size(mw, mh))
                        .takeIf { c -> c.top >= 0 && labelled.none { it.overlaps(c) } }
                }
            } ?: continue
            labelled += rect
            drawText(m, w.ink, rect.topLeft)
        }
        // ch番号（少しだけ）
        val nums = Channels.freqs(band)
        val step = if (band == Channels.B24) 2 else if (band == Channels.B5) 4 else 4
        nums.forEachIndexed { i, f ->
            if (i % step == 0) drawText(tm, "${Channels.freqToNumber(f)}", Offset(x(f.toDouble()) - 6f, b + 2f), TextStyle(fontSize = 9.sp, color = w.muted))
        }
    }
}

@Composable
fun KindLegend() {
    val w = LocalW.current
    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        for (k in listOf("own", "team", "official_infra", "unknown")) {
            val s = kindStyle(k)
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(s.glyph, color = kindColor(w, k), fontSize = 11.sp)
                Text(if (k == "official_infra") "交渉不可" else s.label, fontSize = 11.sp, color = w.ink2)
            }
        }
    }
}

@Composable
fun SectionTitle(text: String, modifier: Modifier = Modifier) {
    Text(text, style = MaterialTheme.typography.titleSmall, fontWeight = FontWeight.Bold, modifier = modifier.padding(top = 14.dp, bottom = 6.dp))
}
