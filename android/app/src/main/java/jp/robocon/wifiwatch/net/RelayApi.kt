package jp.robocon.wifiwatch.net

import android.net.Network
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.Dns
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.net.InetAddress
import java.util.concurrent.TimeUnit

class RelayException(val status: Int, val code: String?, override val message: String) : Exception(message) {
    /** 送り直しても変わらない誤り（中身が不正など） */
    val permanent get() = status in 400..499 && status != 408 && status != 429
}

class NoNetworkException : IOException("モバイル回線が使えません")

/** 中継のURLとトークン（Settings が持つ）。テストでは差し替える。 */
interface RelayConfigSource {
    val relayUrl: String
    val token: String
    val pinCellular: Boolean
    val configured: Boolean
}

/**
 * 中継との通信。通信はモバイル回線に固定する：OkHttpのソケットと名前解決の両方を、
 * requestNetwork で得たNetworkに結びつける（ソケット＝Network.socketFactory、名前解決＝Network.getAllByName）。
 */
class RelayApi(private val settings: RelayConfigSource, private val awaitCellular: suspend (Long) -> Network?) {
    private val base = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS)
        .writeTimeout(15, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()
    private var cached: Pair<Network?, OkHttpClient>? = null

    private fun clientFor(n: Network?): OkHttpClient {
        cached?.let { if (it.first == n) return it.second }
        val c = if (n == null) base else base.newBuilder()
            .socketFactory(n.socketFactory)
            .dns(object : Dns {
                override fun lookup(hostname: String): List<InetAddress> = n.getAllByName(hostname).toList()
            })
            .build()
        cached = n to c
        return c
    }

    private suspend fun client(): OkHttpClient {
        if (!settings.pinCellular) return clientFor(null)
        val n = awaitCellular(8000) ?: throw NoNetworkException()
        return clientFor(n)
    }

    suspend fun call(method: String, path: String, body: JSONObject? = null): JSONObject = withContext(Dispatchers.IO) {
        if (!settings.configured) throw RelayException(0, "not_configured", "設定で中継のURLとトークンを入れてください")
        val c = client()
        val rb = body?.toString()?.toRequestBody("application/json; charset=utf-8".toMediaType())
        val req = Request.Builder()
            .url(settings.relayUrl + path)
            .header("Authorization", "Bearer ${settings.token}")
            .method(method, if (method == "GET") null else (rb ?: "{}".toRequestBody("application/json".toMediaType())))
            .build()
        c.newCall(req).execute().use { r ->
            val text = r.body?.string() ?: ""
            val json = runCatching { JSONObject(text) }.getOrNull()
            if (!r.isSuccessful) {
                throw RelayException(r.code, json?.optString("error"), json?.optString("message")?.ifEmpty { null } ?: "HTTP ${r.code}")
            }
            json ?: JSONObject()
        }
    }

    suspend fun poll(opened: Boolean) = call("POST", "/api/app/poll", JSONObject().put("opened", opened))
    suspend fun command(body: JSONObject) = call("POST", "/api/app/commands", body)
    suspend fun label(recordId: String, body: JSONObject) = call("PATCH", "/api/app/records/$recordId", body)
    suspend fun walk(body: JSONObject) = call("POST", "/api/app/records", body)
    suspend fun records(since: Long, limit: Int = 200) = call("GET", "/api/app/records?since=$since&limit=$limit")
    suspend fun record(id: String) = call("GET", "/api/app/records/$id")
    /** PCが今見ている局を頼む（指示ではない。PCが次の問い合わせで気づいて1回置く）。 */
    suspend fun askPcView() = call("POST", "/api/app/pcview", JSONObject())
    suspend fun pcView() = call("GET", "/api/app/pcview")
}
