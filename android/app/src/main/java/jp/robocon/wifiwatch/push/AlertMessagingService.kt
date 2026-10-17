package jp.robocon.wifiwatch.push

import com.google.firebase.messaging.FirebaseMessagingService
import com.google.firebase.messaging.RemoteMessage

/**
 * FCM の受け口。アプリが閉じているときは、届いた通知（notification）を Android がそのまま通知欄に出す
 * （channel_id のチャネルの鳴り方で）。アプリが開いているときはここに来るので、自分で通知欄に出す。
 */
class AlertMessagingService : FirebaseMessagingService() {
    override fun onMessageReceived(message: RemoteMessage) {
        val d = message.data
        val title = d["title"] ?: message.notification?.title ?: return
        val body = d["body"] ?: message.notification?.body ?: ""
        val channel = d["channel"] ?: message.notification?.channelId ?: Push.CH_WARN
        Push.show(this, channel, title, body, d["tag"] ?: "fcm")
    }

    override fun onNewToken(token: String) {
        Push.subscribe(this) // トピックの登録は端末の登録に付いているので、作り直されたら登録し直す
    }
}
