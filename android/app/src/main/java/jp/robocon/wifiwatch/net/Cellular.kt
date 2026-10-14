package jp.robocon.wifiwatch.net

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.filterNotNull
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.withTimeoutOrNull

/**
 * モバイル回線のNetworkを要求して持っておく。アプリの通信はこのNetworkに結びつける
 * （ロボットのWi-Fi＝インターネットなし、には流さない。操縦用アプリの通信にも干渉しない）。
 * ConnectivityManager.requestNetwork（CHANGE_NETWORK_STATE が要る。unregister するまで続く）。
 */
class Cellular(context: Context) {
    private val cm = context.getSystemService(ConnectivityManager::class.java)
    private val _network = MutableStateFlow<Network?>(null)
    val network: StateFlow<Network?> = _network
    private var callback: ConnectivityManager.NetworkCallback? = null
    private var users = 0

    @Synchronized
    fun acquire() {
        users++
        if (callback != null) return
        val req = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_CELLULAR)
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .build()
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(n: Network) {
                _network.value = n
            }

            override fun onLost(n: Network) {
                if (_network.value == n) _network.value = null
            }

            override fun onUnavailable() {
                _network.value = null
            }
        }
        callback = cb
        cm.requestNetwork(req, cb)
    }

    @Synchronized
    fun release() {
        users = (users - 1).coerceAtLeast(0)
        if (users == 0) {
            callback?.let { runCatching { cm.unregisterNetworkCallback(it) } }
            callback = null
            _network.value = null
        }
    }

    /** モバイル回線が使えるまで待つ（使えなければ null）。 */
    suspend fun await(timeoutMs: Long): Network? {
        acquire()
        try {
            return _network.value ?: withTimeoutOrNull(timeoutMs) { _network.filterNotNull().first() }
        } finally {
            release()
        }
    }
}
