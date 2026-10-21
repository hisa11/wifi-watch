package jp.robocon.wifiwatch.push

import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

/**
 * FCM の受け口。PCは data だけで送るので、アプリが閉じていてもここに来る（緊急・警告は優先度高でスマホを起こす）。
 * 鳴らし方（アラームとして鳴らすか）はこのスマホの設定で決めて、通知欄に出す。
 */
class AlertMessagingService : FirebaseMessagingService() {
    override fun onMessageReceived(message: RemoteMessage) {
        val d = message.data
        val title = d["title"] ?: message.notification?.title ?: return
        val body = d["body"] ?: message.notification?.body ?: ""
        val channel = Push.channelFor(this, d["level"], d["kind"], d["channel"] ?: message.notification?.channelId)
        Push.show(this, channel, title, body, d["tag"] ?: "fcm")
    }

    override fun onNewToken(token: String) {
        Push.subscribe(this) // トピックの登録は端末の登録に付いているので、作り直されたら登録し直す
    }
}
