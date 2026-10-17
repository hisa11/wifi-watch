package jp.robocon.wifiwatch.core

import org.json.JSONArray
import org.json.JSONObject

/**
 * スマホのアナライザー：このスマホが受けたスキャン結果を、局ごとにまとめ続ける表（Androidに依存しない部分）。
 * - 時刻はすべて端末の起動からのミリ秒（ScanResult.timestamp を1000で割ったもの）。時計の秒ではなく、結果の時刻で古さを決める。
 * - 同じスキャン結果を読み直しても（getScanResults は前の結果を返し続ける）、時刻が進んでいなければ数え直さない。
 * - presentMs より前から聞こえていない局は「いない」（薄く描く）、keepMs を過ぎたら表から消す。
 */
class LiveTable(val presentMs: Long = 60_000, val keepMs: Long = 120_000) {
    /** スキャン結果1つ。 */
    data class Obs(
        val bssid: String, val ssid: String?, val freq: Int, val center0: Int, val rssi: Int, val iesHex: String?,
        val standard: String, val op: Ies.Operating, val atMs: Long,
    )

    data class Station(
        val bssid: String, val ssid: String?, val freq: Int, val center0: Int, val iesHex: String?, val standard: String,
        val op: Ies.Operating, val rssi: Int, val samples: List<Pair<Long, Int>>, val firstMs: Long, val lastMs: Long,
    ) {
        fun present(nowMs: Long, presentMs: Long) = nowMs - lastMs <= presentMs

        /** 送るときの強さ：最近（windowMs以内）の結果の中央値。無ければ最新。 */
        fun rssiRecent(nowMs: Long, windowMs: Long): Int {
            val xs = samples.filter { nowMs - it.first <= windowMs }.map { it.second }.sorted()
            return if (xs.isEmpty()) rssi else xs[xs.size / 2]
        }
    }

    private val map = LinkedHashMap<String, Station>()

    /** 新しい結果を取り込んだ回数（同じ結果の読み直しは数えない）。 */
    var updates = 0
        private set

    /** いちばん新しい結果の時刻。 */
    var lastResultMs: Long? = null
        private set

    /** 結果を取り込む。新しい結果が1つでもあれば true。 */
    fun add(obs: List<Obs>, nowMs: Long): Boolean {
        var changed = false
        for (o in obs) {
            if (nowMs - o.atMs > keepMs) continue // 古いスキャンの残り
            val prev = map[o.bssid]
            if (prev != null && o.atMs <= prev.lastMs) continue // 同じ結果の読み直し
            map[o.bssid] = if (prev == null) {
                Station(o.bssid, o.ssid, o.freq, o.center0, o.iesHex, o.standard, o.op, o.rssi, listOf(o.atMs to o.rssi), o.atMs, o.atMs)
            } else {
                prev.copy(
                    ssid = o.ssid ?: prev.ssid, freq = o.freq, center0 = o.center0, iesHex = o.iesHex ?: prev.iesHex,
                    standard = o.standard, op = o.op, rssi = o.rssi,
                    samples = (prev.samples + (o.atMs to o.rssi)).filter { nowMs - it.first <= keepMs }.takeLast(60),
                    lastMs = o.atMs,
                )
            }
            if (lastResultMs == null || o.atMs > lastResultMs!!) lastResultMs = o.atMs
            changed = true
        }
        map.values.removeAll { nowMs - it.lastMs > keepMs }
        if (changed) updates++
        return changed
    }

    /** 表の局（強い順）。いない局も keepMs までは入る（薄く描くため）。 */
    fun stations(nowMs: Long): List<Station> =
        map.values.filter { nowMs - it.lastMs <= keepMs }.sortedByDescending { it.rssi }

    /** 今いる局だけ（送るときに使う）。 */
    fun snapshot(nowMs: Long): List<Station> = stations(nowMs).filter { it.present(nowMs, presentMs) }

    companion object {
        /**
         * PCに送る記録（形は歩測と同じ。中継が受けるのは kind=walk だけなので、そのまま使う）。
         * 強さは最近（presentMs以内）の結果の中央値。
         */
        fun record(
            id: String, device: String, table: LiveTable, nowMs: Long, scans: Int,
            location: String?, tags: List<String>, memo: String, nowEpoch: Double,
        ): JSONObject {
            val sts = JSONArray()
            val snap = table.snapshot(nowMs)
            for (s in snap.sortedWith(compareBy({ it.freq }, { -it.rssi }))) {
                val recent = s.samples.filter { nowMs - it.first <= table.presentMs }
                sts.put(JSONObject()
                    .put("bssid", s.bssid).put("ssid", s.ssid ?: JSONObject.NULL).put("freq", s.freq)
                    .put("ch", Channels.freqToNumber(s.op.primaryFreq) ?: JSONObject.NULL)
                    .put("width", s.op.width).put("width_label", s.op.widthLabel)
                    .put("center0", if (s.center0 == 0) JSONObject.NULL else s.center0)
                    .put("rssi", s.rssiRecent(nowMs, table.presentMs)).put("rssi_samples", recent.size.coerceAtLeast(1))
                    .put("standard", s.standard).put("ies_hex", s.iesHex ?: JSONObject.NULL).put("primary_freq", s.op.primaryFreq)
                    .put("spans", JSONArray(s.op.spans.map { JSONArray(listOf(it.lo, it.hi)) }))
                    .put("seen_us", s.lastMs * 1000).put("age_s", (nowMs - s.lastMs) / 1000))
            }
            return JSONObject()
                .put("id", id).put("kind", "walk").put("created_at", nowEpoch).put("device", device)
                .put("settings", JSONObject().put("mode", "live").put("window_s", table.presentMs / 1000).put("scans", scans)
                    .put("app", "wifiwatch-android"))
                .put("stations", sts)
                .put("label", JSONObject().put("location", location ?: JSONObject.NULL).put("tags", JSONArray(tags))
                    .put("memo", memo).put("updated_at", nowEpoch))
        }
    }
}
