package jp.robocon.wifiwatch.push

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.google.firebase.FirebaseApp
import com.google.firebase.messaging.FirebaseMessaging
import jp.robocon.wifiwatch.MainActivity
import jp.robocon.wifiwatch.R
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/**
 * 警報のプッシュ通知（FCM）。PCが FCM に直接送り（中継は通さない）、アプリはトピックに登録しておくだけで、
 * 閉じていても届く。PCは data だけで送るので、届いたらここで通知を作る（チャネルの名前は PCの wifiwatch/fcm.py と同じ）。
 * 「アラームとして知らせる」をオンにすると、緊急・警告を音なしのアラームのチャネル（用途・種類がアラーム）で出す。
 * おやすみモードでも「アラーム」を許可していれば通り、音は鳴らさず、バイブと画面の上の通知だけで知らせる。
 * google-services.json が無い組み立てでは、Firebase が初期化されないので、登録もしない（設定に「未設定」と出す）。
 */
object Push {
    const val TOPIC = "wifiwatch-alerts" // PCの config.json の notify.fcm_topic の既定と同じ
    const val CH_CRIT = "alert_crit"
    const val CH_WARN = "alert_warn"
    const val CH_INFO = "alert_info"
    const val CH_RESOLVED = "alert_resolved"
    // 音なしのアラーム（バイブと画面の上の通知だけ）。チャネルの音は作ったあと変えられないので、音ありで作っていた
    // alarm_crit・alarm_warn（2026-10-21 の版）は消して、別の名前で作り直す
    const val CH_ALARM_CRIT = "alarm_vib_crit"
    const val CH_ALARM_WARN = "alarm_vib_warn"
    private val OLD_CHANNELS = listOf("alarm_crit", "alarm_warn")

    /** subscribed：true 登録できた・false できなかった・null 登録中。slow：登録の返事が30秒来ない（初めての登録で起きた）。 */
    data class Status(val configured: Boolean, val subscribed: Boolean?, val error: String?, val slow: Boolean = false)

    private val _status = MutableStateFlow(Status(false, null, null))
    val status: StateFlow<Status> = _status
    private val main = android.os.Handler(android.os.Looper.getMainLooper())
    private var pendingSince = 0L

    fun configured(c: Context) = FirebaseApp.getApps(c).isNotEmpty()

    private fun prefs(c: Context) = c.applicationContext.getSharedPreferences("push", Context.MODE_PRIVATE)

    /** 緊急・警告をアラームとして知らせる（おやすみモード・マナーモードでも、音なしでバイブと通知）。スマホごとの設定。既定はオフ。 */
    fun alarmMode(c: Context) = prefs(c).getBoolean("alarm_mode", false)
    fun setAlarmMode(c: Context, on: Boolean) = prefs(c).edit().putBoolean("alarm_mode", on).apply()

    /** 届いたものの鳴らし方（チャネル）。解消とお知らせは、アラームのときも普通のまま。 */
    fun channelFor(c: Context, level: String?, kind: String?, fallback: String?): String {
        if (kind != "resolved" && alarmMode(c)) {
            if (level == "crit") return CH_ALARM_CRIT
            if (level == "warn") return CH_ALARM_WARN
        }
        return when {
            kind == "resolved" -> CH_RESOLVED
            level == "crit" -> CH_CRIT
            level == "warn" -> CH_WARN
            fallback != null -> fallback
            else -> CH_INFO
        }
    }

    fun permitted(c: Context) = Build.VERSION.SDK_INT < 33 ||
        ContextCompat.checkSelfPermission(c, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    /** 通知チャネルを作る（緊急・警告は音とバイブと画面の上。解消は静か）。何度呼んでもよい。 */
    fun createChannels(c: Context) {
        val nm = c.getSystemService(NotificationManager::class.java) ?: return
        val sound = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
        val aa = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_EVENT)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
        // アラーム：音は付けず、用途だけアラームにする。Android は用途か種類がアラームの通知を「アラーム」として扱い、
        // おやすみモードでも「アラーム」を許可していれば止めない（バイブと画面の上の通知が出る）
        val alarmAa = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
        fun ch(id: String, name: String, imp: Int, desc: String, vib: LongArray?, alarm: Boolean = false) = NotificationChannel(id, name, imp).apply {
            description = desc
            if (vib != null) {
                enableVibration(true); vibrationPattern = vib
                if (alarm) setSound(null, alarmAa) else setSound(sound, aa)
            } else { enableVibration(false); setSound(null, null) }
            lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
        }
        nm.createNotificationChannels(listOf(
            ch(CH_CRIT, "緊急の警報", NotificationManager.IMPORTANCE_HIGH, "PCが緊急の警報を出したとき（長いバイブ）",
                longArrayOf(0, 900, 300, 900, 300, 900)),
            ch(CH_WARN, "警告", NotificationManager.IMPORTANCE_HIGH, "PCが警告を出したとき", longArrayOf(0, 600, 250, 600)),
            ch(CH_INFO, "お知らせ", NotificationManager.IMPORTANCE_DEFAULT, "自chを自APに合わせた、など", longArrayOf(0, 250)),
            ch(CH_RESOLVED, "解消", NotificationManager.IMPORTANCE_LOW, "警報が解消したとき（音なし）", null),
            ch(CH_ALARM_CRIT, "緊急の警報（アラーム・音なし）", NotificationManager.IMPORTANCE_HIGH,
                "「アラームとして知らせる」がオンのとき。おやすみモードでも、音なしでバイブと通知",
                longArrayOf(0, 900, 300, 900, 300, 900), alarm = true),
            ch(CH_ALARM_WARN, "警告（アラーム・音なし）", NotificationManager.IMPORTANCE_HIGH,
                "「アラームとして知らせる」がオンのとき。おやすみモードでも、音なしでバイブと通知",
                longArrayOf(0, 600, 250, 600), alarm = true),
        ))
        for (id in OLD_CHANNELS) nm.deleteNotificationChannel(id)
    }

    /**
     * トピックに登録する。アプリを前面に出すたびに呼ぶ（登録済みでも害はない）。
     * 初めての登録は、端末の登録（トークン作り）を待つので、返事がなかなか来ないことがある（実機で「登録しています…」の
     * まま止まり、アプリを開き直したら届いた）。前の登録が終わっていなければ、前面に出たときにもう一度頼み直す。
     * 一度できたら端末に覚え、次に開いたときは返事を待つ間も「受け取り中」と出す。
     */
    fun subscribe(c: Context) {
        if (!configured(c)) {
            _status.value = Status(false, null, null)
            return
        }
        val prefs = c.applicationContext.getSharedPreferences("push", Context.MODE_PRIVATE)
        val before = prefs.getBoolean("subscribed", false)
        val now = android.os.SystemClock.elapsedRealtime()
        if (_status.value.subscribed == null && _status.value.configured && now - pendingSince < 10_000) return // 頼んだばかり
        pendingSince = now
        _status.value = Status(true, if (before) true else null, null)
        FirebaseMessaging.getInstance().subscribeToTopic(TOPIC).addOnCompleteListener { t ->
            pendingSince = 0
            if (t.isSuccessful) prefs.edit().putBoolean("subscribed", true).apply()
            _status.value = Status(true, t.isSuccessful, t.exception?.message)
        }
        main.postDelayed({
            // 30秒たっても返事が無い（この頼みがまだ終わっていない）。前に一度もできていなければ、そう出す
            if (pendingSince == now && !before) _status.value = _status.value.copy(slow = true)
        }, 30_000)
    }

    /** 通知欄に出す（届いた警報、と、このスマホでの試し）。 */
    fun show(c: Context, channel: String, title: String, body: String, tag: String) {
        if (!permitted(c)) return
        val open = PendingIntent.getActivity(c, 0, Intent(c, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = NotificationCompat.Builder(c, channel)
            .setSmallIcon(R.drawable.ic_stat_alert)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(if (channel in URGENT) NotificationCompat.PRIORITY_HIGH else NotificationCompat.PRIORITY_DEFAULT)
            // 種類「アラーム」はおやすみモードの「アラーム」の許可で通るので、アラームとして鳴らすときだけ付ける
            .setCategory(if (channel == CH_ALARM_CRIT || channel == CH_ALARM_WARN) NotificationCompat.CATEGORY_ALARM else NotificationCompat.CATEGORY_STATUS)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC) // ロック画面でも中身が見える（警報の題名だけで、個人の情報は無い）
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        @Suppress("MissingPermission")
        NotificationManagerCompat.from(c).notify(tag, 1, n)
    }

    private val URGENT = setOf(CH_CRIT, CH_WARN, CH_ALARM_CRIT, CH_ALARM_WARN)

    /** 設定画面の「このスマホで試しに鳴らす」（PCもFCMも使わない。今の設定での、警告の鳴り方の確かめ）。 */
    fun testLocal(c: Context) = show(c, channelFor(c, "warn", "raised", null), "wifiwatch・通知のテスト",
        if (alarmMode(c)) "このスマホで知らせました（警告をアラームとして・音なし）。" else "このスマホで鳴らしました（警告と同じ鳴り方）。", "local-test")
}
