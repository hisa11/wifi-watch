package jp.robocon.wifiwatch.wifi

import android.annotation.SuppressLint
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.location.LocationManager
import android.net.wifi.ScanResult
import android.net.wifi.WifiManager
import android.os.Build
import android.os.SystemClock
import jp.robocon.wifiwatch.core.Channels
import jp.robocon.wifiwatch.core.Ies
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull
import org.json.JSONArray
import org.json.JSONObject

/**
 * 歩測（Pixelを持って会場を歩いて測る）。1地点で数回スキャンし、局ごとにまとめる。
 * - getScanResults には ACCESS_FINE_LOCATION と位置情報サービスのオンが要る。startScan は CHANGE_WIFI_STATE。
 * - startScan は前面で2分に4回まで（開発者オプションで解除済みの前提）。結果の時刻（ScanResult.timestamp、
 *   起動からのマイクロ秒）で「新しい結果か」を確かめる。
 */
class WalkScanner(private val context: Context) {
    private val wifi = context.applicationContext.getSystemService(WifiManager::class.java)

    data class Seen(
        val bssid: String, val ssid: String?, val freq: Int, val androidWidth: Int, val center0: Int, val center1: Int,
        val rssiSamples: MutableList<Int>, var iesHex: String?, val standard: String, val op: Ies.Operating, var seenUs: Long,
    ) {
        val rssi get() = rssiSamples.sorted().let { it[it.size / 2] }
    }

    data class Progress(val scans: Int, val fresh: Int, val stale: Int, val stations: Int, val secondsLeft: Int)

    fun locationEnabled(): Boolean = context.getSystemService(LocationManager::class.java)?.isLocationEnabled ?: false

    @SuppressLint("MissingPermission")
    private suspend fun scanOnce(): Pair<List<ScanResult>, Boolean> {
        val done = CompletableDeferred<Boolean>()
        val rx = object : BroadcastReceiver() {
            override fun onReceive(c: Context, i: Intent) {
                done.complete(i.getBooleanExtra(WifiManager.EXTRA_RESULTS_UPDATED, false))
            }
        }
        val f = IntentFilter(WifiManager.SCAN_RESULTS_AVAILABLE_ACTION)
        if (Build.VERSION.SDK_INT >= 33) context.registerReceiver(rx, f, Context.RECEIVER_NOT_EXPORTED)
        else context.registerReceiver(rx, f)
        try {
            @Suppress("DEPRECATION")
            val started = wifi.startScan()
            val updated = if (started) withTimeoutOrNull(15_000) { done.await() } ?: false else false
            return wifi.scanResults to updated
        } finally {
            runCatching { context.unregisterReceiver(rx) }
        }
    }

    /** seconds 秒のあいだスキャンを繰り返し、局ごとにまとめる。 */
    suspend fun run(seconds: Int, onProgress: (Progress, Map<String, Seen>) -> Unit): Pair<Map<String, Seen>, Progress> {
        val seen = linkedMapOf<String, Seen>()
        val start = SystemClock.elapsedRealtime()
        var scans = 0
        var fresh = 0
        var stale = 0
        var prog = Progress(0, 0, 0, 0, seconds)
        while (SystemClock.elapsedRealtime() - start < seconds * 1000L) {
            val reqUs = SystemClock.elapsedRealtime() * 1000
            val (results, updated) = scanOnce()
            scans++
            if (updated) fresh++ else stale++
            for (r in results) {
                if (r.timestamp < reqUs - 30_000_000) continue // 30秒より古い結果（前のスキャンの残り）は使わない
                add(seen, r)
            }
            val left = (seconds - (SystemClock.elapsedRealtime() - start) / 1000).toInt().coerceAtLeast(0)
            prog = Progress(scans, fresh, stale, seen.size, left)
            onProgress(prog, seen)
            if (!updated) delay(1500)
        }
        return seen to prog
    }

    private fun add(seen: MutableMap<String, Seen>, r: ScanResult) {
        val bssid = r.BSSID?.lowercase() ?: return
        val elements = r.informationElements.map { Ies.Ie(it.id, it.idExt, ByteArray(it.bytes.remaining()).also { b -> it.bytes.duplicate().get(b) }) }
        val info = Ies.parse(elements)
        val op = if (elements.isNotEmpty()) Ies.operating(info, r.frequency) else fallback(r)
        val ssid = if (Build.VERSION.SDK_INT >= 33) r.wifiSsid?.toString()?.trim('"') else @Suppress("DEPRECATION") r.SSID
        val prev = seen[bssid]
        if (prev == null) {
            seen[bssid] = Seen(
                bssid, info.ssid ?: ssid?.ifEmpty { null }, r.frequency, r.channelWidth, r.centerFreq0, r.centerFreq1,
                mutableListOf(r.level), if (elements.isNotEmpty()) Ies.hex(Ies.join(elements)) else null,
                standard(r.wifiStandard, info), op, r.timestamp,
            )
        } else {
            prev.rssiSamples += r.level
            if (r.timestamp > prev.seenUs) prev.seenUs = r.timestamp
            if (prev.iesHex == null && elements.isNotEmpty()) prev.iesHex = Ies.hex(Ies.join(elements))
        }
    }

    /** IEが取れないときは、ScanResult の幅と中心周波数から決める。 */
    private fun fallback(r: ScanResult): Ies.Operating {
        val w = when (r.channelWidth) { 1 -> 40; 2 -> 80; 3, 4 -> 160; 5 -> 320; else -> 20 }
        val spans = if (w == 20 || r.centerFreq0 == 0) listOf(Channels.span20(r.frequency))
        else listOf(Channels.Span.around(r.centerFreq0.toDouble(), w.toDouble()))
        return Ies.Operating(Channels.bandOf(r.frequency) ?: Channels.B6, r.frequency, w, "$w", spans, "android", false)
    }

    private fun standard(ws: Int, info: Ies.Info): String = when (ws) {
        8 -> "be"; 6 -> "ax"; 5 -> "ac"; 4 -> "n"; 1 -> "legacy"
        else -> info.standard
    }

    companion object {
        /** 中継に送る記録（形は testdata/relay/app_walk.json の record）。 */
        fun record(id: String, device: String, seconds: Int, prog: Progress, seen: Collection<Seen>,
                   location: String?, tags: List<String>, memo: String, now: Double): JSONObject {
            val sts = JSONArray()
            for (s in seen.sortedWith(compareBy({ it.freq }, { -it.rssi }))) {
                sts.put(JSONObject()
                    .put("bssid", s.bssid).put("ssid", s.ssid ?: JSONObject.NULL).put("freq", s.freq)
                    .put("ch", Channels.freqToNumber(s.op.primaryFreq) ?: JSONObject.NULL)
                    .put("width", s.op.width).put("width_label", s.op.widthLabel)
                    .put("center0", if (s.center0 == 0) JSONObject.NULL else s.center0)
                    .put("rssi", s.rssi).put("rssi_samples", s.rssiSamples.size).put("standard", s.standard)
                    .put("ies_hex", s.iesHex ?: JSONObject.NULL).put("primary_freq", s.op.primaryFreq)
                    .put("spans", JSONArray(s.op.spans.map { JSONArray(listOf(it.lo, it.hi)) })).put("seen_us", s.seenUs))
            }
            return JSONObject()
                .put("id", id).put("kind", "walk").put("created_at", now).put("device", device)
                .put("settings", JSONObject().put("seconds", seconds).put("scans", prog.scans).put("fresh_scans", prog.fresh)
                    .put("stale_scans", prog.stale).put("app", "wifiwatch-android"))
                .put("stations", sts)
                .put("label", JSONObject().put("location", location ?: JSONObject.NULL).put("tags", JSONArray(tags))
                    .put("memo", memo).put("updated_at", now))
        }
    }
}
