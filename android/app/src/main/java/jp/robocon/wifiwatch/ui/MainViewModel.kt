package jp.robocon.wifiwatch.ui

import android.app.Application
import android.os.SystemClock
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import jp.robocon.wifiwatch.App
import jp.robocon.wifiwatch.core.Bodies
import jp.robocon.wifiwatch.core.PcState
import jp.robocon.wifiwatch.core.PcView
import jp.robocon.wifiwatch.core.PollResult
import jp.robocon.wifiwatch.core.RecordMeta
import jp.robocon.wifiwatch.data.RecordEntity
import jp.robocon.wifiwatch.data.WalkEntity
import jp.robocon.wifiwatch.net.NoNetworkException
import jp.robocon.wifiwatch.net.RelayException
import jp.robocon.wifiwatch.sync.Outbox
import jp.robocon.wifiwatch.wifi.LiveScanner
import jp.robocon.wifiwatch.core.LiveTable
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone
import java.util.UUID

class MainViewModel(application: Application) : AndroidViewModel(application) {
    private val app = App.of(application)
    val settings get() = app.settings

    // ------------------------------------------------------------ 状態（前面にいる間だけ2秒ごと）
    data class Live(
        val state: PcState? = null,
        val stateAgeMs: Long? = null,
        /** stateAgeMs を受け取った時刻（端末の起動からの経過） */
        val stateAtElapsed: Long = 0,
        val relayNow: Long? = null,
        /** relayNow を受け取った時刻。指示を送った応答でも更新するので、状態の古さとは別に持つ */
        val relayAtElapsed: Long = 0,
        val lastOkElapsed: Long? = null,
        val error: String? = null,
        val polling: Boolean = false,
        /** 1秒ごとに変わる値（中継に届かない間も「◯秒前」と判定を進めるため） */
        val tick: Long = 0,
    ) {
        /** 中継の今の時刻の見積もり（最後に受け取った now ＋ そこからの経過） */
        fun relayNowEstimate(): Long? = relayNow?.let { it + (SystemClock.elapsedRealtime() - relayAtElapsed) }

        /** 状態の写しの古さ（中継の時計どうしで比べた値に、受け取ってからの経過を足す） */
        fun ageMsNow(): Long? = stateAgeMs?.let { it + (SystemClock.elapsedRealtime() - stateAtElapsed) }
    }

    private val _live = MutableStateFlow(Live())
    val live: StateFlow<Live> = _live
    private var pollJob: Job? = null
    private var tickJob: Job? = null

    fun startPolling() {
        if (pollJob != null) return
        app.cellular.acquire() // 前面にいる間はモバイル回線を確保しておく
        _live.update { it.copy(polling = true) }
        tickJob = viewModelScope.launch {
            while (isActive) { delay(1000); _live.update { it.copy(tick = SystemClock.elapsedRealtime()) } }
        }
        pollJob = viewModelScope.launch {
            var opened = true // 開いた直後は版数を上げてもらう（PCが10秒以内に気づいて2秒間隔に入る）
            while (isActive) {
                if (settings.configured) {
                    try {
                        val r = PollResult.parse(app.api.poll(opened))
                        // 開いた直後か、届かなかったあとで届いたら、溜まっているラベルと歩測をすぐ送り直す
                        if ((opened || _live.value.error != null) && app.db.outbox().size() > 0) Outbox.kick(getApplication(), now = true)
                        opened = false
                        val now = SystemClock.elapsedRealtime()
                        _live.update {
                            it.copy(state = r.state ?: it.state, stateAgeMs = r.stateAgeMs ?: it.stateAgeMs, stateAtElapsed = if (r.stateAgeMs != null) now else it.stateAtElapsed,
                                relayNow = r.now, relayAtElapsed = now, lastOkElapsed = now, error = null)
                        }
                    } catch (e: Exception) {
                        _live.update { it.copy(error = describe(e)) }
                    }
                } else {
                    _live.update { it.copy(error = "設定で中継のURLとアプリ用のトークンを入れてください") }
                }
                delay(2000)
            }
        }
    }

    fun stopPolling() {
        pollJob?.cancel()
        pollJob = null
        tickJob?.cancel()
        tickJob = null
        app.cellular.release()
        _live.update { it.copy(polling = false) }
    }

    // ------------------------------------------------------------ 指示（手動スキャン・帯域ON/OFF）
    data class Cmd(
        val requestId: String, val type: String, val summary: String, val params: JSONObject,
        val sentElapsed: Long, val acceptedAt: Long? = null, val error: String? = null, val busy: Boolean = true,
    )

    private val _cmds = MutableStateFlow<List<Cmd>>(emptyList())
    val cmds: StateFlow<List<Cmd>> = _cmds

    /** 望む巡回の設定（PCに反映されるまで「反映待ち」と出す） */
    data class BandsWish(val excludedBands: Set<String>, val excludedFreqs: Set<Int>, val requestId: String)

    private val _bandsWish = MutableStateFlow<BandsWish?>(null)
    val bandsWish: StateFlow<BandsWish?> = _bandsWish

    fun sendScan(bands: List<String>, confirm: Boolean) {
        val rid = "app-scan-" + UUID.randomUUID().toString().take(12)
        val body = Bodies.scan(rid, bands, null, confirm)
        send(Cmd(rid, "scan", "手動スキャン（${bands.joinToString("/") { "${it}GHz" }}）${if (confirm) "・確認済み" else ""}", body.getJSONObject("params"), SystemClock.elapsedRealtime()), body)
    }

    fun sendBands(excludedBands: Set<String>, excludedFreqs: Set<Int>) {
        val rid = "app-bands-" + UUID.randomUUID().toString().take(12)
        val body = Bodies.bands(rid, excludedBands.sorted(), excludedFreqs.sorted())
        _bandsWish.value = BandsWish(excludedBands, excludedFreqs, rid)
        send(Cmd(rid, "bands", "巡回の帯域ON/OFF", body.getJSONObject("params"), SystemClock.elapsedRealtime()), body)
    }

    private fun send(c: Cmd, body: JSONObject) {
        _cmds.update { listOf(c) + it.take(9) }
        viewModelScope.launch {
            // 期限60秒の指示なので、待ち行列には入れず、すぐ送る（短い間だけ送り直す）
            val start = SystemClock.elapsedRealtime()
            var last: String? = null
            while (SystemClock.elapsedRealtime() - start < 20_000) {
                try {
                    val r = app.api.command(body)
                    update(c.requestId) { it.copy(acceptedAt = r.optLong("accepted_at"), busy = false, error = null) }
                    _live.update { it.copy(relayNow = r.optLong("now"), relayAtElapsed = SystemClock.elapsedRealtime()) }
                    return@launch
                } catch (e: RelayException) {
                    last = if (e.code == "busy") "PCがまだ受け取っていない手動スキャンがあります" else e.message
                    if (e.permanent) break
                } catch (e: Exception) {
                    last = describe(e)
                }
                delay(2000)
            }
            update(c.requestId) { it.copy(error = last ?: "送れませんでした", busy = false) }
        }
    }

    private fun update(rid: String, f: (Cmd) -> Cmd) = _cmds.update { l -> l.map { if (it.requestId == rid) f(it) else it } }

    enum class Step { SENDING, ACCEPTED, RECEIVED, RUNNING, DONE, FAILED }

    data class CmdView(val step: Step, val text: String, val needsConfirm: Boolean = false, val record: String? = null)

    fun view(c: Cmd, live: Live): CmdView {
        val st = live.state?.commands?.get(c.requestId)
        if (c.acceptedAt == null) {
            return if (c.error != null) CmdView(Step.FAILED, "送れなかった：${c.error}") else CmdView(Step.SENDING, "送信中…")
        }
        if (st != null) {
            return when (st.state) {
                "received" -> CmdView(Step.RECEIVED, "PCが受け取った")
                "running" -> {
                    val m = live.state.manual
                    val left = if (m != null && m.requestId == c.requestId && m.state != "done") "（残り${"%.1f".format(m.remainingS)}秒・${m.done}/${m.total}ch）" else ""
                    CmdView(Step.RUNNING, "実行中$left")
                }
                "done" -> CmdView(Step.DONE, if (st.record != null) "完了（記録 ${st.record}）" else "完了", record = st.record)
                "rejected" -> CmdView(Step.FAILED, "PCが断った：${st.message ?: st.error}", needsConfirm = st.error == "needs_confirm")
                "expired" -> CmdView(Step.FAILED, "期限（60秒）を過ぎていたので実行しなかった")
                else -> CmdView(Step.RECEIVED, st.state)
            }
        }
        val now = live.relayNowEstimate()
        if (now != null && now - c.acceptedAt > 60_000) return CmdView(Step.FAILED, "PCに届かなかった（60秒たっても受け取りなし）")
        return CmdView(Step.ACCEPTED, "中継が受け付けた（PCの受け取り待ち）")
    }

    // ------------------------------------------------------------ 記録（中継の写し）
    val records = app.db.records().all()
    val outboxCount = app.db.outbox().count()
    private val _recordsMsg = MutableStateFlow<String?>(null)
    val recordsMsg: StateFlow<String?> = _recordsMsg

    fun refreshRecords() = viewModelScope.launch {
        try {
            var since = app.db.records().maxUpdated() ?: 0L
            while (true) {
                val r = app.api.records(since)
                val arr = r.optJSONArray("records") ?: JSONArray()
                for (i in 0 until arr.length()) {
                    val o = arr.getJSONObject(i)
                    val m = RecordMeta.parse(o)
                    val old = app.db.records().get(m.id)
                    // アプリで変えたラベルが写しに反映されたら「反映待ち」を消す
                    val pendingDone = old?.pendingEditedAt != null && (m.labelUpdatedAt ?: 0.0) >= old.pendingEditedAt
                    val bodyStale = old?.bodyJson != null && old.updatedAt != m.updatedAt
                    app.db.records().put(RecordEntity(m.id, m.kind, m.createdAt, o.toString(), if (bodyStale) null else old?.bodyJson,
                        m.updatedAt, if (pendingDone) null else old?.pendingLabelJson, if (pendingDone) null else old?.pendingEditedAt))
                }
                since = r.optLong("next_since", since)
                if (!r.optBoolean("more")) break
            }
            _recordsMsg.value = null
        } catch (e: Exception) {
            _recordsMsg.value = describe(e)
        }
    }

    fun watchRecord(id: String) = app.db.records().watch(id)

    suspend fun openRecord(id: String): RecordEntity? {
        val cur = app.db.records().get(id)
        if (cur?.bodyJson != null) return cur
        return try {
            val body = app.api.record(id)
            // まだ一覧を読んでいない記録（スキャンの完了から開いたとき）は、本体に付いてくる一覧用の情報を使う
            val meta = body.optJSONObject("meta")
            val e = (cur ?: RecordEntity(id, body.optString("kind"), body.optDouble("created_at"),
                (meta ?: JSONObject().put("id", id)).toString(), null, meta?.optLong("updated_at") ?: 0))
                .copy(bodyJson = body.toString())
            app.db.records().put(e)
            e
        } catch (e: RelayException) {
            if (e.status == 404) app.db.records().delete(id)
            _recordsMsg.value = e.message
            cur
        } catch (e: Exception) {
            _recordsMsg.value = describe(e)
            cur
        }
    }

    fun saveLabel(id: String, location: String?, tags: List<String>, memo: String) = viewModelScope.launch {
        val rid = "app-label-" + UUID.randomUUID().toString().take(12)
        val editedAt = System.currentTimeMillis() / 1000.0
        val body = Bodies.label(rid, location, tags, memo, editedAt)
        app.db.records().get(id)?.let {
            app.db.records().put(it.copy(pendingLabelJson = body.getJSONObject("label").toString(), pendingEditedAt = editedAt))
        }
        Outbox.add(getApplication(), "label", id, rid, body)
    }

    // ------------------------------------------------------------ スマホのアナライザー（アプリが前面にいる間はスキャンし続ける）
    private val liveScanner = LiveScanner(application)
    val phone: StateFlow<LiveScanner.State> = liveScanner.state
    val walks = app.db.walks().all()
    private val _phoneMsg = MutableStateFlow<String?>(null)
    val phoneMsg: StateFlow<String?> = _phoneMsg

    val phonePresentMs get() = liveScanner.table.presentMs
    fun startPhoneScan() = liveScanner.start(viewModelScope)
    fun stopPhoneScan() = liveScanner.stop()
    fun phoneScanNow() = liveScanner.scanNow()

    /** その時点で聞こえている局を、PCに送る（記録の形は歩測と同じ。回線が無ければ端末に溜めて、あとで送る）。 */
    fun sendPhoneSnapshot(location: String?, tags: List<String>, memo: String) = viewModelScope.launch {
        val st = liveScanner.state.value
        val snap = liveScanner.table.snapshot(st.nowMs)
        if (snap.isEmpty()) { _phoneMsg.value = "送る局がありません（スキャンの結果を待ってください）"; return@launch }
        val now = System.currentTimeMillis() / 1000.0
        val fmt = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).apply { timeZone = TimeZone.getTimeZone("Asia/Tokyo") }
        val id = "walk-" + fmt.format(Date()) + "-" + UUID.randomUUID().toString().take(4)
        val rec = LiveTable.record(id, settings.deviceName, liveScanner.table, st.nowMs, st.updates, location, tags, memo, now)
        app.db.walks().put(WalkEntity(id, now, rec.toString(), location, snap.size))
        val rid = "app-walk-$id"
        Outbox.add(getApplication(), "walk", id, rid, Bodies.walk(rid, rec))
        _phoneMsg.value = "${snap.size}局をPCへ送ります（回線が無ければ端末に溜めて、あとで送ります）"
    }

    // ------------------------------------------------------------ PCが今見ている局（頼んだときだけ取り寄せる）
    data class PcViewUi(
        val loading: Boolean = false, val view: PcView? = null, val error: String? = null,
        val askedAtMs: Long? = null, val gotAtElapsed: Long? = null,
    )

    private val _pcView = MutableStateFlow(PcViewUi())
    val pcView: StateFlow<PcViewUi> = _pcView
    private var pcViewJob: Job? = null

    /** 中継に頼む → PCが次の問い合わせ（前面中は2秒ごと）で気づいて1回置く → 置かれるまで読む（30秒まで）。 */
    fun requestPcView() {
        if (pcViewJob?.isActive == true) return
        pcViewJob = viewModelScope.launch {
            _pcView.update { it.copy(loading = true, error = null) }
            try {
                val asked = app.api.askPcView().optLong("requested_at")
                _pcView.update { it.copy(askedAtMs = asked) }
                val start = SystemClock.elapsedRealtime()
                while (SystemClock.elapsedRealtime() - start < 30_000) {
                    delay(1500)
                    val v = PcView.parse(app.api.pcView())
                    if (v.forMs != null && v.forMs >= asked) {
                        _pcView.value = PcViewUi(view = v, askedAtMs = asked, gotAtElapsed = SystemClock.elapsedRealtime())
                        return@launch
                    }
                }
                _pcView.update { it.copy(loading = false, error = "30秒待ってもPCから届きません（PCが動いていて中継につながっているか確かめてください）") }
            } catch (e: Exception) {
                _pcView.update { it.copy(loading = false, error = describe(e)) }
            }
        }
    }

    suspend fun exportWalks(): String {
        val arr = JSONArray()
        for (w in app.db.walks().list()) arr.put(JSONObject(w.json))
        return arr.toString(1)
    }

    fun resendOutbox() = Outbox.kick(getApplication())

    // ------------------------------------------------------------ 設定
    suspend fun testConnection(): String = try {
        val r = PollResult.parse(app.api.poll(false))
        "つながりました（PCの状態：${r.state?.mode ?: "まだ無い"}、古さ ${r.stateAgeMs?.let { "%.0f秒".format(it / 1000.0) } ?: "—"}）"
    } catch (e: Exception) {
        "つながりません：${describe(e)}"
    }

    companion object {
        fun describe(e: Exception): String = when (e) {
            is NoNetworkException -> "モバイル回線が使えません（モバイルデータをオンに）"
            is RelayException -> when (e.status) {
                401 -> "トークンが違います"
                403 -> "このトークンでは使えません"
                else -> e.message
            }
            is IOException -> "中継に届きません（${e.message ?: e.javaClass.simpleName}）"
            else -> e.message ?: e.javaClass.simpleName
        }
    }
}
