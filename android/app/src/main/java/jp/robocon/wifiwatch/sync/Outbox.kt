package jp.robocon.wifiwatch.sync

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import jp.robocon.wifiwatch.App
import jp.robocon.wifiwatch.data.OutboxEntity
import jp.robocon.wifiwatch.net.RelayException
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * 送る待ち行列（ラベルの変更と歩測の記録）。回線が無ければ端末に溜め、つながったら送る。
 * 送るときもモバイル回線に固定する（RelayApi が Network に結びつける。WorkManager の
 * 「ネットワークあり」の条件だけには頼らない）。
 *
 * 指示（手動スキャン・帯域ON/OFF）はここに入れない：期限60秒の指示を、回線が戻ったあとで遅れて送らないため。
 */
object Outbox {
    private const val WORK = "wifiwatch-outbox"

    suspend fun add(context: Context, kind: String, refId: String, requestId: String, body: JSONObject) {
        App.of(context).db.outbox().put(OutboxEntity(requestId, kind, refId, body.toString(), System.currentTimeMillis()))
        kick(context)
    }

    /**
     * 送る仕事を入れる。now=true は、待ち（指数的に延びる間隔）を捨ててすぐ送り直す
     * （中継に届くようになったのが分かったとき）。送っている途中を取り消しても、要求IDで冪等なので二重にはならない。
     */
    fun kick(context: Context, now: Boolean = false) {
        val req = OneTimeWorkRequestBuilder<SendWorker>()
            .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 15, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(WORK, if (now) ExistingWorkPolicy.REPLACE else ExistingWorkPolicy.APPEND_OR_REPLACE, req)
    }
}

class SendWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        val app = App.of(applicationContext)
        val dao = app.db.outbox()
        while (true) {
            val items = dao.pending()
            if (items.isEmpty()) return Result.success()
            for (it in items) {
                try {
                    val body = JSONObject(it.bodyJson)
                    when (it.kind) {
                        "label" -> app.api.label(it.refId, body)
                        "walk" -> {
                            app.api.walk(body)
                            app.db.walks().markSent(it.refId, System.currentTimeMillis())
                        }
                    }
                    dao.delete(it.requestId)
                } catch (e: RelayException) {
                    if (e.permanent) {
                        // 中身が不正などで、送り直しても変わらない。捨てて理由を残す
                        dao.delete(it.requestId)
                        if (it.kind == "walk") app.db.walks().markError(it.refId, e.message)
                    } else {
                        dao.failed(it.requestId, e.message)
                        return Result.retry()
                    }
                } catch (e: IOException) {
                    dao.failed(it.requestId, e.message ?: "通信できません")
                    return Result.retry()
                }
            }
        }
    }
}
