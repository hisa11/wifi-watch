package jp.robocon.wifiwatch.wifi

import android.Manifest
import android.annotation.SuppressLint
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.location.LocationManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.SystemClock
import androidx.core.content.ContextCompat
import jp.robocon.wifiwatch.core.LiveTable
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull

/**
 * スマホのアナライザー：アプリが前面にいる間、Wi-Fiのスキャンを続けて LiveTable を更新する。
 * - startScan は前面のアプリで「2分に4回」まで（Android 9以降。2026-10-17 developer.android.com で確認）。
 *   制限があるかは端末に覚えておく（はじめは「ある」とみなす）。ある間は約30秒ごと（2分に4回の内）に頼み、
 *   2分に4回を使い切ったあとも30秒ごとに1回だけ試す。それが通れば制限は外れている（開発者向けオプションの
 *   「Wi-Fiスキャン調整」がオフ）ので、数秒ごとにする。断られたら制限ありに戻す（断られた分は回数に数えられない）。
 * - ほかのアプリやシステムがスキャンした結果も SCAN_RESULTS_AVAILABLE で受け取って使う（回数に数えない）。
 * - getScanResults には ACCESS_FINE_LOCATION と位置情報のオンが要る。無ければ理由を出して待つ。
 */
class LiveScanner(private val context: Context) {
    data class State(
        val active: Boolean = false,
        val stations: List<LiveTable.Station> = emptyList(),
        val nowMs: Long = 0,
        val lastResultMs: Long? = null,
        val requested: Int = 0, // 自分で頼んで通ったスキャン
        val updates: Int = 0, // 新しい結果が届いた回数（ほかのアプリのスキャンも含む）
        val throttled: Boolean = false,
        val nextScanMs: Long? = null,
        val problem: String? = null,
    )

    private val wifi = context.applicationContext.getSystemService(WifiManager::class.java)
    val table = LiveTable()
    private val _state = MutableStateFlow(State())
    val state: StateFlow<State> = _state

    private var job: Job? = null
    private var receiver: BroadcastReceiver? = null
    private val wake = Channel<Unit>(Channel.CONFLATED)
    private val recent = ArrayDeque<Long>() // 自分で頼んだスキャンの時刻（2分の窓）
    private val prefs = context.getSharedPreferences("live_scan", Context.MODE_PRIVATE)
    private var throttled = prefs.getBoolean("throttled", true)
        set(v) { if (field != v) prefs.edit().putBoolean("throttled", v).apply(); field = v }
    private var lastReq = 0L
    private var waitingSince: Long? = null
    private var requested = 0
    private var force = false
    private var problem: String? = null

    private fun now() = SystemClock.elapsedRealtime()

    fun start(scope: CoroutineScope) {
        if (job != null) return
        val rx = object : BroadcastReceiver() {
            override fun onReceive(c: Context, i: Intent) {
                waitingSince = null
                if (i.getBooleanExtra(WifiManager.EXTRA_RESULTS_UPDATED, false)) ingest()
                wake.trySend(Unit)
            }
        }
        val f = IntentFilter(WifiManager.SCAN_RESULTS_AVAILABLE_ACTION)
        if (Build.VERSION.SDK_INT >= 33) context.registerReceiver(rx, f, Context.RECEIVER_NOT_EXPORTED)
        else context.registerReceiver(rx, f)
        receiver = rx
        ingest() // 手元に残っている結果から描き始める
        job = scope.launch { loop() }
    }

    fun stop() {
        job?.cancel()
        job = null
        receiver?.let { runCatching { context.unregisterReceiver(it) } }
        receiver = null
        _state.value = _state.value.copy(active = false, nextScanMs = null)
    }

    /** 今すぐスキャンを頼む（制限の窓がいっぱいなら、空くのを待つ）。 */
    fun scanNow() {
        force = true
        wake.trySend(Unit)
    }

    private fun ready(): String? = when {
        ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED ->
            "位置情報の許可が要ります（Wi-Fiのスキャン結果を読むのに要ります）"
        context.getSystemService(LocationManager::class.java)?.isLocationEnabled != true ->
            "位置情報（設定 → 位置情報）をオンにしてください。オフだとスキャン結果を読めません"
        !wifi.isWifiEnabled && !wifi.isScanAlwaysAvailable ->
            "Wi-Fiがオフです。スキャンにはWi-Fiのオン（つながなくてよい）か「Wi-Fiスキャン」の設定が要ります"
        else -> null
    }

    @SuppressLint("MissingPermission")
    private fun ingest() {
        try {
            val obs = wifi.scanResults.mapNotNull(ScanParse::obs)
            table.add(obs, now())
        } catch (e: SecurityException) {
            problem = "位置情報の許可が要ります"
        }
        publish()
    }

    private fun publish(next: Long? = _state.value.nextScanMs) {
        val t = now()
        _state.value = State(
            active = job != null, stations = table.stations(t), nowMs = t, lastResultMs = table.lastResultMs,
            requested = requested, updates = table.updates, throttled = throttled, nextScanMs = next, problem = problem,
        )
    }

    private fun dueAt(t: Long): Long {
        val w = waitingSince
        return when {
            w != null && t - w < RESULT_WAIT_MS -> t + 500 // 前に頼んだスキャンの結果を待っている
            force -> t
            throttled -> lastReq + THROTTLED_GAP_MS // 窓を使い切っていても、30秒ごとに1回は試す（通れば制限は外れている）
            else -> lastReq + FAST_GAP_MS
        }
    }

    @Suppress("DEPRECATION")
    private suspend fun loop() {
        while (kotlin.coroutines.coroutineContext.isActive) {
            val t = now()
            while (recent.isNotEmpty() && t - recent.first() > WINDOW_MS) recent.removeFirst()
            problem = ready()
            if (problem != null) {
                publish(null)
                withTimeoutOrNull(2000) { wake.receive() }
                continue
            }
            val due = dueAt(t)
            publish(due)
            if (due > t) {
                withTimeoutOrNull(minOf(due - t, 1000)) { wake.receive() }
                continue
            }
            force = false
            // 2分に4回を確かに使い切っている（境目の数秒は数えない）ときに通ったら、制限は外れている
            val full = recent.count { t - it <= WINDOW_MS - 10_000 } >= WINDOW_SCANS
            val ok = try { wifi.startScan() } catch (e: SecurityException) { false }
            lastReq = t
            if (ok) {
                if (full) throttled = false // 2分に5回目が通った：制限は外れている
                recent.addLast(t)
                requested++
                waitingSince = t
            } else {
                throttled = true // 断られた：2分に4回の制限がある
            }
            publish(dueAt(now()))
        }
    }

    companion object {
        const val WINDOW_MS = 120_000L
        const val WINDOW_SCANS = 4
        const val FAST_GAP_MS = 3_000L
        const val THROTTLED_GAP_MS = 31_000L // 2分に4回に収まる間隔（境目で5回目にならないよう少しあける）
        const val RESULT_WAIT_MS = 12_000L
    }
}
