package jp.robocon.wifiwatch.core

import org.json.JSONArray
import org.json.JSONObject

/** PCの状態の写し（中継の app/poll の state）。形は testdata/relay/pc_state.json。 */
data class PcState(
    val at: Double,
    val device: String,
    val mode: String,
    val ownFreq: Int,
    val ownLabel: String,
    val ownWidth: Int,
    val ownRatio: Double,
    val blockLabel: String,
    val blockFreqs: List<Int>,
    val rxState: String,
    val rxReason: String,
    val crit: Int,
    val warn: Int,
    val alerts: List<Alert>,
    val manual: Manual?,
    val excludedBands: List<String>,
    val excludedFreqs: List<Int>,
    val commands: Map<String, CommandStatus>,
    val channels: List<ChannelState>,
    /** PCの無線では受けられないch（6GHz非対応の無線など）。無い版のPCでは空 */
    val unreceivableFreqs: List<Int> = emptyList(),
    /** 自chを自APに合わせた（30分だけ載る）。無ければ null */
    val followed: Followed? = null,
    /** 自APが別々のchにいて、どれに合わせるかPCの画面で選ぶ必要がある（ch・幅の文字）。無ければ空 */
    val followPending: List<String> = emptyList(),
) {
    data class Followed(val at: Long, val from: String, val to: String, val ap: String, val reserved: Boolean)
    data class Alert(val level: String, val type: String, val title: String, val message: String, val since: Long)
    data class Manual(val id: String, val state: String, val origin: String, val requestId: String?, val done: Int, val total: Int, val remainingS: Double)
    data class CommandStatus(val state: String, val at: Double, val type: String?, val error: String?, val message: String?, val record: String?, val delayS: Double?)

    /** code：o 自ch・i 自ブロック内・p 巡回・x 巡回外・z 手動スキャンで停止・s スキャン中・t スキャン待ち・! 受信停止・u 未観測 */
    data class ChannelState(val freq: Int, val code: String, val nOverlap: Int, val maxRssi: Int?, val nStrong: Int) {
        val blind get() = code in setOf("x", "z", "t", "!", "u")
    }

    companion object {
        fun parse(o: JSONObject): PcState {
            val own = o.getJSONObject("own")
            val rx = o.optJSONObject("rx") ?: JSONObject()
            val counts = o.optJSONObject("counts") ?: JSONObject()
            val bands = o.optJSONObject("bands") ?: JSONObject()
            val cmds = o.optJSONObject("commands") ?: JSONObject()
            return PcState(
                at = o.optDouble("at", 0.0),
                device = o.optString("device", ""),
                mode = o.optString("mode", "?"),
                ownFreq = own.getInt("freq"),
                ownLabel = own.optString("label"),
                ownWidth = own.optInt("width", 20),
                ownRatio = own.optDouble("ratio", 0.0),
                blockLabel = own.optString("block"),
                blockFreqs = own.optJSONArray("block_freqs").ints(),
                rxState = rx.optString("state", "ok"),
                rxReason = rx.optString("reason", ""),
                crit = counts.optInt("crit"),
                warn = counts.optInt("warn"),
                alerts = o.optJSONArray("alerts").objects().map {
                    Alert(it.optString("level"), it.optString("type"), it.optString("title"), it.optString("message"), it.optLong("since"))
                },
                manual = o.optJSONObject("manual")?.let {
                    Manual(it.optString("id"), it.optString("state"), it.optString("origin"), it.optStringOrNull("request_id"),
                        it.optInt("done"), it.optInt("total"), it.optDouble("remaining_s", 0.0))
                },
                excludedBands = bands.optJSONArray("excluded_bands").strings(),
                excludedFreqs = bands.optJSONArray("excluded_freqs").ints(),
                unreceivableFreqs = bands.optJSONArray("unreceivable").ints(),
                followed = own.optJSONObject("followed")?.let {
                    Followed(it.optLong("at"), it.optString("from"), it.optString("to"), it.optString("ap"), it.optBoolean("reserved", true))
                },
                followPending = own.optJSONArray("follow_pending").strings(),
                commands = cmds.keys().asSequence().associateWith { k ->
                    val c = cmds.getJSONObject(k)
                    CommandStatus(c.optString("state"), c.optDouble("at", 0.0), c.optStringOrNull("type"), c.optStringOrNull("error"),
                        c.optStringOrNull("message"), c.optStringOrNull("record"), if (c.has("delay_s")) c.optDouble("delay_s") else null)
                },
                channels = o.optJSONArray("channels").arrays().map {
                    ChannelState(it.getInt(0), it.getString(1), it.optInt(2), if (it.isNull(3)) null else it.getInt(3), it.optInt(4))
                },
            )
        }
    }
}

/** app/poll の応答。古さは中継の時計どうしで比べた値（state_age_ms）。 */
data class PollResult(val now: Long, val state: PcState?, val stateAgeMs: Long?) {
    companion object {
        fun parse(o: JSONObject) = PollResult(
            o.optLong("now"),
            o.optJSONObject("state")?.let { PcState.parse(it) },
            if (o.isNull("state_age_ms")) null else o.optLong("state_age_ms"),
        )
    }
}

/**
 * PCが今見ている局（アプリが頼んだときだけ、PCが中継に置く）。形は testdata/relay/app_pcview.json。
 * forMs：その一覧が応えた「頼んだ時刻」（中継の時計）。古い頼みへの一覧かどうかを見分ける。
 */
data class PcView(
    val forMs: Long?, val receivedAtMs: Long?, val nowMs: Long, val state: PcState?, val strong: Int,
    val stations: List<Station>, val nTotal: Int, val scanOnly: Boolean,
) {
    data class Station(
        val bssid: String, val ssid: String?, val hidden: Boolean, val freq: Int, val widthLabel: String,
        val spans: List<Channels.Span>, val rssi: Int?, val kind: String?, val team: String?, val model: String?,
        val standard: String?, val present: Boolean, val alerting: Boolean, val ageS: Int,
    )

    companion object {
        fun parse(o: JSONObject): PcView {
            val v = o.optJSONObject("view")
            val sts = v?.optJSONArray("stations").objects().map {
                val f = it.optInt("f")
                Station(
                    it.optString("b"), it.optStringOrNull("s"), it.optBoolean("h"), f, it.optString("w", "20"),
                    it.optJSONArray("sp").arrays().map { a -> Channels.Span(a.getDouble(0), a.getDouble(1)) }
                        .ifEmpty { listOf(Channels.span20(f)) },
                    if (it.isNull("r") || !it.has("r")) null else it.optInt("r"), it.optStringOrNull("k"), it.optStringOrNull("t"),
                    it.optStringOrNull("m"), it.optStringOrNull("x"), it.optBoolean("p", true), it.optBoolean("a"), it.optInt("ag"),
                )
            }
            return PcView(
                if (o.isNull("for") || !o.has("for")) null else o.optLong("for"),
                if (o.isNull("received_at") || !o.has("received_at")) null else o.optLong("received_at"),
                o.optLong("now"), v?.optJSONObject("state")?.let { PcState.parse(it) }, v?.optInt("strong", -70) ?: -70,
                sts, v?.optInt("n_total", sts.size) ?: 0, v?.optBoolean("scan_only") ?: false,
            )
        }
    }
}

/** 記録の一覧用の情報（中継の写し）。 */
data class RecordMeta(
    val id: String, val kind: String, val createdAt: Double, val device: String?, val origin: String?,
    val bands: List<String>, val location: String?, val tags: List<String>, val memo: String,
    val labelUpdatedAt: Double?, val nStations: Int, val updatedAt: Long,
) {
    companion object {
        fun parse(o: JSONObject): RecordMeta {
            val lab = o.optJSONObject("label") ?: JSONObject()
            return RecordMeta(
                o.getString("id"), o.optString("kind"), o.optDouble("created_at", 0.0), o.optStringOrNull("device"),
                o.optStringOrNull("origin"), o.optJSONArray("bands").strings(), lab.optStringOrNull("location"),
                lab.optJSONArray("tags").strings(), lab.optString("memo", ""),
                if (lab.isNull("updated_at") || !lab.has("updated_at")) null else lab.optDouble("updated_at"),
                o.optInt("n_stations"), o.optLong("updated_at"),
            )
        }
    }
}

/** 記録の中の局（PCの手動スキャンと歩測で共通に読む）。 */
data class StationRow(
    val bssid: String, val ssid: String?, val freq: Int, val width: Int, val widthLabel: String,
    val spans: List<Channels.Span>, val rssi: Int?, val standard: String?, val model: String?, val kind: String?, val team: String?,
) {
    companion object {
        fun parse(o: JSONObject): StationRow {
            val freq = o.optInt("primary_freq", o.optInt("freq"))
            val width = o.optInt("width", 20)
            val spans = o.optJSONArray("spans").arrays().map { Channels.Span(it.getDouble(0), it.getDouble(1)) }
                .ifEmpty { listOf(Channels.Span.around(freq.toDouble(), width.toDouble())) }
            return StationRow(
                o.optString("bssid"), o.optStringOrNull("ssid"), freq, width, o.optString("width_label", "$width"), spans,
                if (o.isNull("rssi") || !o.has("rssi")) null else o.optInt("rssi"), o.optStringOrNull("standard"),
                o.optStringOrNull("model_guess") ?: o.optStringOrNull("model"), o.optStringOrNull("kind"), o.optStringOrNull("team"),
            )
        }
    }
}

// ------------------------------------------------------------------ JSONの小物
fun JSONObject.optStringOrNull(k: String): String? = if (!has(k) || isNull(k)) null else optString(k)
fun JSONArray?.ints(): List<Int> = if (this == null) emptyList() else (0 until length()).map { getInt(it) }
fun JSONArray?.strings(): List<String> = if (this == null) emptyList() else (0 until length()).map { getString(it) }
fun JSONArray?.objects(): List<JSONObject> = if (this == null) emptyList() else (0 until length()).mapNotNull { optJSONObject(it) }
fun JSONArray?.arrays(): List<JSONArray> = if (this == null) emptyList() else (0 until length()).mapNotNull { optJSONArray(it) }

/** 中継への要求の本文（形は testdata/relay/app_*.json）。 */
object Bodies {
    fun scan(requestId: String, bands: List<String>?, freqs: List<Int>?, confirm: Boolean): JSONObject {
        val p = JSONObject().put("confirm", confirm)
        if (bands != null) p.put("bands", JSONArray(bands))
        if (freqs != null) p.put("freqs", JSONArray(freqs))
        return JSONObject().put("request_id", requestId).put("type", "scan").put("params", p)
    }

    fun bands(requestId: String, excludedBands: List<String>, excludedFreqs: List<Int>): JSONObject =
        JSONObject().put("request_id", requestId).put("type", "bands")
            .put("params", JSONObject().put("excluded_bands", JSONArray(excludedBands)).put("excluded_freqs", JSONArray(excludedFreqs)))

    fun label(requestId: String, location: String?, tags: List<String>, memo: String, editedAt: Double): JSONObject =
        JSONObject().put("request_id", requestId).put("edited_at", editedAt)
            .put("label", JSONObject().put("location", location ?: JSONObject.NULL).put("tags", JSONArray(tags)).put("memo", memo))

    fun walk(requestId: String, record: JSONObject): JSONObject = JSONObject().put("request_id", requestId).put("record", record)
}
