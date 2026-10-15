@file:OptIn(androidx.compose.foundation.layout.ExperimentalLayoutApi::class)

package jp.robocon.wifiwatch.ui

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import jp.robocon.wifiwatch.core.Channels
import jp.robocon.wifiwatch.core.PcState
import jp.robocon.wifiwatch.core.RecordMeta
import jp.robocon.wifiwatch.core.StationRow
import jp.robocon.wifiwatch.core.strings
import jp.robocon.wifiwatch.data.RecordEntity
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

private val jst = SimpleDateFormat("MM/dd HH:mm:ss", Locale.JAPAN).apply { timeZone = TimeZone.getTimeZone("Asia/Tokyo") }
fun jstOf(epochS: Double): String = jst.format(Date((epochS * 1000).toLong()))

@Composable
private fun Screen(content: @Composable () -> Unit) {
    Column(Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(horizontal = 16.dp, vertical = 10.dp)) { content() }
}

// ================================================================== 1. 状態
@Composable
fun StatusScreen(vm: MainViewModel) {
    val live by vm.live.collectAsStateWithLifecycle()
    val w = LocalW.current
    Screen {
        VerdictCard(verdictOf(live))
        val s = live.state
        val age = live.ageMsNow()
        Text(
            buildString {
                append(if (age != null) "PCの状態：${ago(age / 1000.0)}" else "PCの状態：—")
                if (live.error != null) append("・中継：${live.error}")
            },
            fontSize = 12.sp, color = if (live.error != null) w.crit else w.ink2, modifier = Modifier.padding(top = 6.dp),
        )
        if (s == null) return@Screen
        val stale = age != null && age > STALE_MS
        if (stale) Text("以下は${ago(age!! / 1000.0)}の状態です（いまの様子ではありません）", fontSize = 13.sp, fontWeight = FontWeight.SemiBold,
            color = w.ink, modifier = Modifier.padding(top = 8.dp))
        // 古い状態は薄く出す（見間違えないように）
        Column(Modifier.alpha(if (stale) 0.45f else 1f)) { StatusBody(s) }
    }
}

@Composable
private fun StatusBody(s: PcState) {
    val w = LocalW.current
    Column {
        Spacer(Modifier.height(10.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(14.dp)) {
            Fact("モード", s.mode)
            Fact("自ch", "${s.ownLabel}・${s.ownWidth}MHz")
            Fact("受信", mapOf("ok" to "正常", "suspect" to "様子見", "recovering" to "復旧中", "failed" to "停止")[s.rxState] ?: s.rxState)
        }
        Text("受信しているブロック：${s.blockLabel}${if (s.mode != "PRE") "・自chの受信率 ${(s.ownRatio * 100).toInt()}%" else ""}", fontSize = 12.sp, color = w.ink2)
        SectionTitle("chごとの様子（見ているか・強い局）")
        ChannelStrip(s)
        // 警報が出ていても、見ていないところは警報より上に出す（「検知なし」と「見ていない」を混ぜない）
        val blind = blindList(s)
        if (blind.isNotEmpty()) {
            Text("▨ 見ていない：" + blind.joinToString("、") + "。ここで起きたことは検知できません。",
                color = w.ink, fontSize = 13.sp, fontWeight = FontWeight.SemiBold,
                modifier = Modifier.fillMaxWidth().padding(top = 8.dp).background(w.surface3, RoundedCornerShape(8.dp)).padding(8.dp))
        }
        SectionTitle("発生中の警報（${s.alerts.size}）")
        if (s.alerts.isEmpty()) Text("検知なし（ピットから見えた範囲で）", color = w.ink2)
        for (a in s.alerts) AlertRow(a)
    }
}

@Composable
private fun Fact(label: String, value: String) {
    Column {
        Text(label, fontSize = 11.sp, color = LocalW.current.muted)
        Text(value, fontSize = 15.sp, fontWeight = FontWeight.SemiBold)
    }
}

@Composable
private fun AlertRow(a: PcState.Alert) {
    val w = LocalW.current
    val crit = a.level == "crit"
    Row(
        Modifier.fillMaxWidth().padding(vertical = 4.dp)
            .background(if (crit) w.crit.copy(alpha = 0.10f) else w.warn.copy(alpha = 0.16f), RoundedCornerShape(10.dp)).padding(10.dp),
    ) {
        Text(if (crit) "■" else "▲", color = if (crit) w.crit else w.warnInk, fontWeight = FontWeight.Bold, modifier = Modifier.width(22.dp))
        Column {
            Text((if (crit) "緊急｜" else "警告｜") + a.title, fontWeight = FontWeight.Bold)
            Text(a.message, fontSize = 13.sp)
            Text("${jstOf(a.since.toDouble())}から", fontSize = 11.sp, color = w.ink2)
        }
    }
}

// ================================================================== 2. 手動スキャン
@Composable
fun ScanScreen(vm: MainViewModel, onOpenRecord: (String) -> Unit = {}) {
    val live by vm.live.collectAsStateWithLifecycle()
    val cmds by vm.cmds.collectAsStateWithLifecycle()
    val w = LocalW.current
    var sel by remember { mutableStateOf(setOf("6", "5", "2.4")) }
    var confirmFor by remember { mutableStateOf<List<String>?>(null) }
    val n = mapOf("6" to 24, "5" to 20, "2.4" to 13)
    Screen {
        Text("PCに1回だけのスキャンを頼みます。その間、PCは自chと巡回を止めます。", color = w.ink2, fontSize = 13.sp)
        SectionTitle("スキャンする帯域")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            for (b in listOf("6", "5", "2.4")) {
                Pick(selected = b in sel, onClick = { sel = if (b in sel) sel - b else sel + b }, label = { Text("${b}GHz（${n[b]}ch）") })
            }
        }
        val k = sel.sumOf { n[it] ?: 0 }
        Text("${k}ch・約${"%.1f".format(k * 0.25)}秒", color = w.ink2, fontSize = 13.sp)
        Spacer(Modifier.height(8.dp))
        val running = live.state?.manual?.let { it.state != "done" } == true
        Button(
            enabled = sel.isNotEmpty() && !running,
            onClick = {
                val bands = listOf("6", "5", "2.4").filter { it in sel }
                if (live.state?.mode == "QUIET") confirmFor = bands else vm.sendScan(bands, false)
            },
        ) { Text(if (running) "PCで手動スキャン中" else "手動スキャンを送る") }
        if (live.state?.mode == "QUIET") Text("いまQUIET（山場）です。送る前に確認します。", color = w.warnInk, fontSize = 12.sp)
        val age = live.ageMsNow()
        if (age != null && age > STALE_MS) Text("PCの状態が${ago(age / 1000.0)}のままです。送っても、60秒以内にPCが受け取らなければ実行されません。",
            color = w.warnInk, fontSize = 12.sp)
        SectionTitle("送った指示")
        if (cmds.isEmpty()) Text("まだありません", color = w.ink2)
        for (c in cmds) {
            val v = vm.view(c, live)
            CommandCard(c.summary, v, onOpenRecord) { vm.sendScan(c.params.optJSONArray("bands").strings(), true) }
        }
    }
    confirmFor?.let { bands ->
        AlertDialog(
            onDismissRequest = { confirmFor = null },
            title = { Text("QUIET中の手動スキャン") },
            text = { Text("山場の試合中です。手動スキャンの間は自chを見ません。送りますか？") },
            confirmButton = { TextButton(onClick = { vm.sendScan(bands, true); confirmFor = null }) { Text("送る") } },
            dismissButton = { TextButton(onClick = { confirmFor = null }) { Text("やめる") } },
        )
    }
}

@Composable
private fun CommandCard(title: String, v: MainViewModel.CmdView, onOpenRecord: ((String) -> Unit)? = null, onConfirmResend: () -> Unit) {
    val w = LocalW.current
    val steps = listOf("送った", "中継が受付", "PCが受け取り", "実行中", "完了")
    val idx = when (v.step) {
        MainViewModel.Step.SENDING -> 0; MainViewModel.Step.ACCEPTED -> 1; MainViewModel.Step.RECEIVED -> 2
        MainViewModel.Step.RUNNING -> 3; MainViewModel.Step.DONE -> 4; MainViewModel.Step.FAILED -> -1
    }
    Column(Modifier.fillMaxWidth().padding(vertical = 4.dp).background(w.surface2, RoundedCornerShape(10.dp)).padding(10.dp)) {
        Text(title, fontWeight = FontWeight.SemiBold)
        Row(horizontalArrangement = Arrangement.spacedBy(4.dp), modifier = Modifier.padding(vertical = 4.dp)) {
            steps.forEachIndexed { i, s ->
                val on = idx >= i
                Text(s, fontSize = 11.sp, color = if (on) w.surface else w.ink2,
                    modifier = Modifier.background(if (on) w.accent else w.surface3, RoundedCornerShape(6.dp)).padding(horizontal = 6.dp, vertical = 2.dp))
            }
        }
        Text(v.text, fontSize = 13.sp, color = if (v.step == MainViewModel.Step.FAILED) w.crit else w.ink)
        if (v.needsConfirm) TextButton(onClick = onConfirmResend) { Text("確認して送り直す") }
        if (v.record != null && onOpenRecord != null) TextButton(onClick = { onOpenRecord(v.record) }) { Text("記録を開く") }
    }
}

// ================================================================== 3. 帯域ON/OFF
@Composable
fun BandsScreen(vm: MainViewModel) {
    val live by vm.live.collectAsStateWithLifecycle()
    val wish by vm.bandsWish.collectAsStateWithLifecycle()
    val cmds by vm.cmds.collectAsStateWithLifecycle()
    val w = LocalW.current
    val s = live.state
    Screen {
        if (s == null) { Text("PCの状態がまだありません", color = w.ink2); return@Screen }
        val curB = s.excludedBands.toSet()
        val curF = s.excludedFreqs.toSet()
        var exB by remember(wish?.requestId) { mutableStateOf(wish?.excludedBands ?: curB) }
        var exF by remember(wish?.requestId) { mutableStateOf(wish?.excludedFreqs ?: curF) }
        val pending = wish != null && (wish!!.excludedBands != curB || wish!!.excludedFreqs != curF)
        Text("外した帯域・chはPCが見なくなります（画面では「巡回外」と灰色の斜線で出ます）。自ブロックは外せません。", color = w.ink2, fontSize = 13.sp)
        if (pending) {
            val c = cmds.firstOrNull { it.requestId == wish!!.requestId }
            val v = c?.let { vm.view(it, live) }
            Text("反映待ち：${v?.text ?: "送信中"}", color = w.warnInk, fontWeight = FontWeight.SemiBold, modifier = Modifier.padding(vertical = 6.dp))
        } else if (wish != null) {
            Text("✓ PCに反映済み（PCの状態で確かめた）", color = w.ink2, fontSize = 13.sp, modifier = Modifier.padding(vertical = 6.dp))
        }
        for (band in Channels.BANDS) {
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 12.dp)) {
                Switch(checked = band !in exB, onCheckedChange = { on -> exB = if (on) exB - band else exB + band })
                Spacer(Modifier.width(10.dp))
                Text("${Channels.BAND_LABEL[band]}を巡回する", fontWeight = FontWeight.SemiBold)
            }
            FlowRow(horizontalArrangement = Arrangement.spacedBy(4.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                for (f in Channels.freqs(band)) {
                    val locked = f in s.blockFreqs
                    val off = !locked && (band in exB || f in exF)
                    val bg = when { f == s.ownFreq -> w.own; locked -> w.own.copy(alpha = 0.18f); off -> w.surface3; else -> w.surface2 }
                    Text(
                        (if (locked) "🔒" else "") + "${Channels.freqToNumber(f)}",
                        fontSize = 13.sp, color = if (f == s.ownFreq) w.surface else if (off) w.muted else w.ink,
                        textDecoration = if (off) TextDecoration.LineThrough else null,
                        modifier = Modifier.background(bg, RoundedCornerShape(6.dp)).clickable(enabled = !locked && band !in exB) {
                            exF = if (f in exF) exF - f else exF + f
                        }.padding(horizontal = 8.dp, vertical = 4.dp),
                    )
                }
            }
        }
        Spacer(Modifier.height(14.dp))
        val changed = exB != curB || exF.filter { it !in s.blockFreqs } .toSet() != curF
        Button(enabled = changed, onClick = { vm.sendBands(exB, exF.filter { it !in s.blockFreqs }.toSet()) }) { Text("PCに送る") }
    }
}

// ================================================================== 4. 記録
@Composable
fun RecordsScreen(vm: MainViewModel, open: String? = null, onOpened: () -> Unit = {}) {
    val recs by vm.records.collectAsStateWithLifecycle(emptyList())
    val msg by vm.recordsMsg.collectAsStateWithLifecycle()
    val w = LocalW.current
    var openId by remember { mutableStateOf<String?>(null) }
    var kind by remember { mutableStateOf("") }
    var loc by remember { mutableStateOf("") }
    var band by remember { mutableStateOf("") }
    var today by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) { vm.refreshRecords() }
    LaunchedEffect(open) { if (open != null) { openId = open; onOpened() } }
    if (openId != null) { RecordDetail(vm, openId!!) { openId = null }; return }
    val metas = recs.map { it to RecordMeta.parse(JSONObject(it.metaJson)) }
    val dayStart = (System.currentTimeMillis() / 1000.0).let { it - (it + 9 * 3600) % 86400 }
    val shown = metas.filter { (_, m) ->
        (kind.isEmpty() || m.kind == kind) && (loc.isEmpty() || m.location == loc) && (band.isEmpty() || band in m.bands) && (!today || m.createdAt >= dayStart)
    }
    Column(Modifier.fillMaxSize().padding(horizontal = 16.dp)) {
        // 絞り込み：種類・日・帯域の行と、場所の行（どちらも横にスクロール）
        Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            Pick(kind == "", { kind = "" }, { Text("全部") })
            Pick(kind == "pc_scan", { kind = "pc_scan" }, { Text("PCの手動スキャン") })
            Pick(kind == "walk", { kind = "walk" }, { Text("歩測") })
            Pick(today, { today = !today }, { Text("今日だけ") })
            for (b in listOf("6", "5", "2.4")) Pick(band == b, { band = if (band == b) "" else b }, { Text("${b}GHz") })
        }
        Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            Text("場所", fontSize = 12.sp, color = w.ink2, modifier = Modifier.align(Alignment.CenterVertically))
            for (l in vm.settings.locations) Pick(loc == l, { loc = if (loc == l) "" else l }, { Text(l) })
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text("${shown.size}件（PCの写し）", color = w.ink2, fontSize = 12.sp, modifier = Modifier.weight(1f))
            TextButton(onClick = { vm.refreshRecords() }) { Text("読み直す") }
        }
        if (msg != null) Text(msg!!, color = w.crit, fontSize = 12.sp)
        LazyColumn(contentPadding = PaddingValues(bottom = 24.dp)) {
            items(shown, key = { it.first.id }) { (e, m) ->
                Column(Modifier.fillMaxWidth().clickable { openId = e.id }.padding(vertical = 8.dp)) {
                    Text("${if (m.kind == "walk") "歩測" else "手動スキャン"}・${jstOf(m.createdAt)}", fontWeight = FontWeight.SemiBold)
                    Text("${m.location ?: "場所なし"}・${m.nStations}局・${m.bands.joinToString("/") { "${it}GHz" }}・${if (m.origin == "app") "アプリ" else "画面"}から" +
                        if (e.pendingLabelJson != null) "・ラベル反映待ち" else "", fontSize = 12.sp, color = w.ink2)
                    if (m.tags.isNotEmpty()) Text(m.tags.joinToString("  ") { "#$it" }, fontSize = 12.sp, color = w.accent)
                }
                HorizontalDivider(color = w.grid)
            }
        }
    }
}

@Composable
private fun RecordDetail(vm: MainViewModel, id: String, onBack: () -> Unit) {
    val w = LocalW.current
    // 写し（DB）を見張る：ラベルの「反映待ち」や、PCで更新された本文がそのまま画面に出る
    val rec by remember(id) { vm.watchRecord(id) }.collectAsStateWithLifecycle(null)
    var tried by remember(id) { mutableStateOf(false) }
    LaunchedEffect(id, rec?.bodyJson == null) { if (rec?.bodyJson == null) { vm.openRecord(id); tried = true } }
    // 反映待ちの間だけ、一覧の写しを読み直して消えるのを待つ
    LaunchedEffect(id, rec?.pendingEditedAt) {
        if (rec?.pendingEditedAt != null) repeat(24) { delay(5_000); vm.refreshRecords().join() }
    }
    Screen {
        TextButton(onClick = onBack) { Text("← 一覧へ") }
        val r = rec ?: run { Text(if (tried) "この記録はありません（中継の写しから消えた可能性）" else "読み込み中…", color = w.ink2); return@Screen }
        val meta = RecordMeta.parse(JSONObject(r.metaJson))
        val body = r.bodyJson?.let { JSONObject(it) }
        val pending = r.pendingLabelJson?.let { JSONObject(it) }
        Text("${if (r.kind == "walk") "歩測" else "PCの手動スキャン"}・${jstOf(r.createdAt)}", fontWeight = FontWeight.Bold, fontSize = 17.sp)
        Text(listOfNotNull(meta.device, if (meta.origin == "app") "アプリから" else "画面から").joinToString("・"), color = w.ink2, fontSize = 12.sp)
        SectionTitle("ラベル")
        var location by remember(r.id, r.pendingLabelJson) { mutableStateOf(pending?.optString("location")?.takeIf { it != "null" } ?: meta.location ?: "") }
        var tags by remember(r.id, r.pendingLabelJson) { mutableStateOf((pending?.optJSONArray("tags").strings().ifEmpty { null } ?: meta.tags).joinToString("、")) }
        var memo by remember(r.id, r.pendingLabelJson) { mutableStateOf(pending?.optString("memo") ?: meta.memo) }
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            for (l in vm.settings.locations) Pick(location == l, { location = l }, { Text(l) })
        }
        OutlinedTextField(location, { location = it.take(40) }, label = { Text("場所") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(tags, { tags = it }, label = { Text("タグ（読点で区切る）") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(memo, { memo = it.take(1000) }, label = { Text("メモ") }, modifier = Modifier.fillMaxWidth())
        Row(verticalAlignment = Alignment.CenterVertically) {
            Button(onClick = {
                vm.saveLabel(r.id, location.trim().ifEmpty { null }, tags.split('、', ',').map { it.trim() }.filter { it.isNotEmpty() }.take(10), memo)
            }) { Text("ラベルを保存") }
            if (pending != null) Text("  反映待ち（PCの正本に入ると消えます）", color = w.warnInk, fontSize = 12.sp)
        }
        if (body == null) { Text("本文を読めませんでした（回線を確かめてください）", color = w.crit); return@Screen }
        val sts = body.optJSONArray("stations")?.let { a -> (0 until a.length()).map { StationRow.parse(a.getJSONObject(it)) } } ?: emptyList()
        for (band in Channels.BANDS) {
            val bs = sts.filter { Channels.bandOf(it.freq) == band }
            if (bs.isEmpty()) continue
            SectionTitle("${Channels.BAND_LABEL[band]}（${bs.size}局）")
            Spectrum(band, bs.map { SpecStation(it.ssid ?: "（非公開）", it.kind, it.spans, it.rssi) })
        }
        KindLegend()
        SectionTitle("局（${sts.size}）")
        for (s in sts.sortedByDescending { it.rssi ?: -200 }) StationLine(s.ssid, s.bssid, s.freq, s.widthLabel, s.rssi, s.kind, s.model)
    }
}

@Composable
private fun StationLine(ssid: String?, bssid: String, freq: Int, width: String, rssi: Int?, kind: String?, model: String?) {
    val w = LocalW.current
    val k = kindStyle(kind)
    Row(Modifier.fillMaxWidth().padding(vertical = 3.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(k.glyph, color = kindColor(w, kind), modifier = Modifier.width(18.dp))
        Column(Modifier.weight(1f)) {
            Text(ssid ?: "（非公開SSID）", fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
            Text("${Channels.short(freq)}・${width}MHz・$bssid" + (model?.let { "・$it" } ?: ""), fontSize = 11.sp, color = w.ink2, fontFamily = FontFamily.Monospace)
        }
        Text(rssi?.let { "$it" } ?: "—", fontWeight = FontWeight.Bold)
    }
}

// ================================================================== 5. 歩測
@Composable
fun WalkScreen(vm: MainViewModel) {
    val ctx = LocalContext.current
    val w = LocalW.current
    val ui by vm.walk.collectAsStateWithLifecycle()
    val walks by vm.walks.collectAsStateWithLifecycle(emptyList())
    val outbox by vm.outboxCount.collectAsStateWithLifecycle(0)
    val scope = rememberCoroutineScope()
    var granted by remember { mutableStateOf(ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED) }
    val ask = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted = it }
    var exportText by remember { mutableStateOf<String?>(null) }
    val save = rememberLauncherForActivityResult(ActivityResultContracts.CreateDocument("application/json")) { uri ->
        val t = exportText
        if (uri != null && t != null) ctx.contentResolver.openOutputStream(uri)?.use { it.write(t.toByteArray()) }
    }
    var location by remember { mutableStateOf(vm.settings.locations.firstOrNull() ?: "") }
    var tags by remember { mutableStateOf("歩測") }
    var memo by remember { mutableStateOf("") }
    var seconds by remember { mutableStateOf(30) }
    Screen {
        Text("Pixelを固定の高さと向きで持ち、1地点30〜60秒測ります。自チームの試合中はしません。", color = w.ink2, fontSize = 13.sp)
        if (!granted) {
            Button(onClick = { ask.launch(Manifest.permission.ACCESS_FINE_LOCATION) }) { Text("位置情報の許可（Wi-Fiのスキャン結果を読むのに要ります）") }
            return@Screen
        }
        if (!vm.locationEnabled()) Text("位置情報（設定 → 位置情報）をオンにしてください。オフだとスキャン結果を読めません。", color = w.crit)
        SectionTitle("場所")
        FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
            for (l in vm.settings.locations) Pick(location == l, { location = l }, { Text(l) })
        }
        OutlinedTextField(location, { location = it.take(40) }, label = { Text("場所") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Row(horizontalArrangement = Arrangement.spacedBy(6.dp), modifier = Modifier.padding(top = 6.dp)) {
            for (sec in listOf(30, 45, 60)) Pick(seconds == sec, { seconds = sec }, { Text("${sec}秒") })
        }
        Spacer(Modifier.height(8.dp))
        Button(enabled = !ui.running, onClick = { vm.startWalk(seconds) }) { Text(if (ui.running) "測っています…" else "スキャンを始める") }
        ui.progress?.let { p ->
            if (ui.running) LinearProgressIndicator(progress = { 1f - p.secondsLeft / seconds.toFloat() }, modifier = Modifier.fillMaxWidth().padding(vertical = 6.dp))
            Text("スキャン ${p.scans}回（新しい結果 ${p.fresh}・古い ${p.stale}）・局 ${p.stations}${if (ui.running) "・残り ${p.secondsLeft}秒" else "・測り終わり"}", fontSize = 12.sp, color = w.ink2)
        }
        ui.message?.let { Text(it, color = w.warnInk, fontSize = 13.sp) }
        if (ui.seen.isNotEmpty()) {
            for (band in Channels.BANDS) {
                val bs = ui.seen.filter { Channels.bandOf(it.freq) == band }
                if (bs.isEmpty()) continue
                SectionTitle("${Channels.BAND_LABEL[band]}（${bs.size}局）")
                Spectrum(band, bs.map { SpecStation(it.ssid ?: "（非公開）", null, it.op.spans, it.rssi) })
            }
            if (!ui.running) {
                OutlinedTextField(tags, { tags = it }, label = { Text("タグ（読点で区切る）") }, singleLine = true, modifier = Modifier.fillMaxWidth())
                OutlinedTextField(memo, { memo = it.take(1000) }, label = { Text("メモ") }, modifier = Modifier.fillMaxWidth())
                Button(onClick = { vm.saveWalk(location.trim().ifEmpty { null }, tags.split('、', ',').map { it.trim() }.filter { it.isNotEmpty() }, memo) },
                    modifier = Modifier.padding(top = 6.dp)) { Text("保存して送る") }
            }
            SectionTitle("局（強い順）")
            for (s in ui.seen.sortedByDescending { it.rssi }.take(40)) StationLine(s.ssid, s.bssid, s.op.primaryFreq, s.op.widthLabel, s.rssi, null, s.standard.let { "802.11$it" })
        }
        // 未送は歩測だけを数える（送る待ちの行列にはラベルの変更も入る）
        val unsent = walks.count { it.sentAt == null && it.lastError == null }
        SectionTitle("自分の歩測（${walks.size}件・未送 $unsent）")
        if (outbox > 0) Text("端末に溜めて送る待ち：${outbox}件（ラベルの変更を含む）。中継に届けば自動で送ります。", color = w.ink2, fontSize = 12.sp)
        Row {
            OutlinedButton(onClick = { vm.resendOutbox() }) { Text("送り直す") }
            Spacer(Modifier.width(8.dp))
            OutlinedButton(onClick = { scope.launch { exportText = vm.exportWalks(); save.launch("wifiwatch-walks.json") } }) { Text("JSONで書き出す") }
        }
        for (e in walks) {
            Text("${jstOf(e.createdAt)}・${e.location ?: "場所なし"}・${e.nStations}局・" +
                when { e.sentAt != null -> "送信済み"; e.lastError != null -> "送れない：${e.lastError}"; else -> "未送（端末に溜めています）" },
                fontSize = 13.sp, modifier = Modifier.padding(vertical = 3.dp))
        }
    }
}

// ================================================================== 6. 設定
@Composable
fun SettingsScreen(vm: MainViewModel, onDone: () -> Unit) {
    val s = vm.settings
    val w = LocalW.current
    val scope = rememberCoroutineScope()
    var url by remember { mutableStateOf(s.relayUrl) }
    var token by remember { mutableStateOf("") }
    var locs by remember { mutableStateOf(s.locations.joinToString("、")) }
    var device by remember { mutableStateOf(s.deviceName) }
    var pin by remember { mutableStateOf(s.pinCellular) }
    var msg by remember { mutableStateOf<String?>(null) }
    Screen {
        TextButton(onClick = onDone) { Text("← 戻る") }
        OutlinedTextField(url, { url = it }, label = { Text("中継のURL（本番ドメイン）") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(token, { token = it }, label = { Text(if (s.token.isNotEmpty()) "アプリ用のトークン（入れ直すときだけ）" else "アプリ用のトークン") },
            singleLine = true, visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
        Text("トークンは端末の中で暗号化して保存します（Android Keystore）。", fontSize = 11.sp, color = w.ink2)
        OutlinedTextField(locs, { locs = it }, label = { Text("場所ラベルの選択肢（読点で区切る）") }, modifier = Modifier.fillMaxWidth())
        OutlinedTextField(device, { device = it }, label = { Text("端末の名前") }, singleLine = true, modifier = Modifier.fillMaxWidth())
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 8.dp)) {
            Switch(pin, { pin = it })
            Spacer(Modifier.width(8.dp))
            Column {
                Text("通信をモバイル回線に固定する", fontWeight = FontWeight.SemiBold)
                Text("本番は必ずオン（ロボットのWi-Fiに流さない）。モバイル回線のない端末で試すときだけオフ。", fontSize = 11.sp, color = w.ink2)
            }
        }
        Spacer(Modifier.height(10.dp))
        Row {
            Button(onClick = {
                val err = s.validateUrl(url)
                if (err != null) { msg = err; return@Button }
                s.relayUrl = url
                if (token.isNotEmpty()) {
                    if (token.length < 16) { msg = "トークンは16文字以上です"; return@Button }
                    s.token = token; token = ""
                }
                s.locations = locs.split('、', ',')
                s.deviceName = device
                s.pinCellular = pin
                msg = "保存しました"
            }) { Text("保存") }
            Spacer(Modifier.width(8.dp))
            OutlinedButton(onClick = { scope.launch { msg = vm.testConnection() } }) { Text("つないでみる") }
        }
        msg?.let { Text(it, modifier = Modifier.padding(top = 8.dp)) }
    }
}
