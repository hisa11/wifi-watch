package jp.robocon.wifiwatch

import android.app.Application
import android.content.Context
import jp.robocon.wifiwatch.data.AppDb
import jp.robocon.wifiwatch.data.Settings
import jp.robocon.wifiwatch.net.Cellular
import jp.robocon.wifiwatch.net.RelayApi
import jp.robocon.wifiwatch.push.Push
import jp.robocon.wifiwatch.sync.Outbox

/** アプリ全体で1つずつ持つもの。 */
class App : Application() {
    lateinit var settings: Settings
    lateinit var cellular: Cellular
    lateinit var api: RelayApi
    val db: AppDb by lazy { AppDb.open(this) }

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        cellular = Cellular(this)
        api = RelayApi(settings) { cellular.await(it) }
        Outbox.kick(this) // 溜まっているものがあれば送る
        Push.createChannels(this) // 警報の通知の鳴り方（緊急・警告は音とバイブと画面の上）
        Push.subscribe(this) // 警報のプッシュ通知を受け取る（google-services.json が無い組み立てでは何もしない）
    }

    companion object {
        fun of(c: Context): App = c.applicationContext as App
    }
}
