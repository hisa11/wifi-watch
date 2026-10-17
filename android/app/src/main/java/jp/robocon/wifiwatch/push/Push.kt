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
 * 閉じていても通知欄に出る。音・バイブ・画面の上に出すかは、ここで作る通知チャネルで決まる（PCの wifiwatch/fcm.py と同じ名前）。
 * google-services.json が無い組み立てでは、Firebase が初期化されないので、登録もしない（設定に「未設定」と出す）。
 */
object Push {
    const val TOPIC = "wifiwatch-alerts" // PCの config.json の notify.fcm_topic の既定と同じ
    const val CH_CRIT = "alert_crit"
    const val CH_WARN = "alert_warn"
    const val CH_INFO = "alert_info"
    const val CH_RESOLVED = "alert_resolved"

    data class Status(val configured: Boolean, val subscribed: Boolean?, val error: String?)

    private val _status = MutableStateFlow(Status(false, null, null))
    val status: StateFlow<Status> = _status

    fun configured(c: Context) = FirebaseApp.getApps(c).isNotEmpty()

    fun permitted(c: Context) = Build.VERSION.SDK_INT < 33 ||
        ContextCompat.checkSelfPermission(c, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    /** 通知チャネルを作る（緊急・警告は音とバイブと画面の上。解消は静か）。何度呼んでもよい。 */
    fun createChannels(c: Context) {
        val nm = c.getSystemService(NotificationManager::class.java) ?: return
        val sound = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
        val aa = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_NOTIFICATION_EVENT)
            .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION).build()
        fun ch(id: String, name: String, imp: Int, desc: String, vib: LongArray?) = NotificationChannel(id, name, imp).apply {
            description = desc
            if (vib != null) { enableVibration(true); vibrationPattern = vib; setSound(sound, aa) } else { enableVibration(false); setSound(null, null) }
            lockscreenVisibility = android.app.Notification.VISIBILITY_PUBLIC
        }
        nm.createNotificationChannels(listOf(
            ch(CH_CRIT, "緊急の警報", NotificationManager.IMPORTANCE_HIGH, "PCが緊急の警報を出したとき（長いバイブ）",
                longArrayOf(0, 900, 300, 900, 300, 900)),
            ch(CH_WARN, "警告", NotificationManager.IMPORTANCE_HIGH, "PCが警告を出したとき", longArrayOf(0, 600, 250, 600)),
            ch(CH_INFO, "お知らせ", NotificationManager.IMPORTANCE_DEFAULT, "自chを自APに合わせた、など", longArrayOf(0, 250)),
            ch(CH_RESOLVED, "解消", NotificationManager.IMPORTANCE_LOW, "警報が解消したとき（音なし）", null),
        ))
    }

    /** トピックに登録する（アプリを開くたびに呼ぶ。登録済みでも害はない）。 */
    fun subscribe(c: Context) {
        if (!configured(c)) {
            _status.value = Status(false, null, null)
            return
        }
        _status.value = _status.value.copy(configured = true)
        FirebaseMessaging.getInstance().subscribeToTopic(TOPIC).addOnCompleteListener { t ->
            _status.value = Status(true, t.isSuccessful, t.exception?.message)
        }
    }

    /** 通知欄に出す（アプリが開いているときに届いたもの、と、このスマホでの試し）。 */
    fun show(c: Context, channel: String, title: String, body: String, tag: String) {
        if (!permitted(c)) return
        val open = PendingIntent.getActivity(c, 0, Intent(c, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val n = NotificationCompat.Builder(c, channel)
            .setSmallIcon(R.drawable.ic_stat_alert)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setPriority(if (channel == CH_CRIT || channel == CH_WARN) NotificationCompat.PRIORITY_HIGH else NotificationCompat.PRIORITY_DEFAULT)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC) // ロック画面でも中身が見える（警報の題名だけで、個人の情報は無い）
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        @Suppress("MissingPermission")
        NotificationManagerCompat.from(c).notify(tag, 1, n)
    }

    /** 設定画面の「このスマホで試しに鳴らす」（PCもFCMも使わない。チャネルの音とバイブの確かめ）。 */
    fun testLocal(c: Context) = show(c, CH_WARN, "wifiwatch・通知のテスト", "このスマホで鳴らしました（警告と同じ鳴り方）。", "local-test")
}
