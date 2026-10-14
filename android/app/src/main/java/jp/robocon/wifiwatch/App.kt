package jp.robocon.wifiwatch

import android.app.Application
import android.content.Context
import jp.robocon.wifiwatch.data.AppDb
import jp.robocon.wifiwatch.data.Settings
import jp.robocon.wifiwatch.net.Cellular
import jp.robocon.wifiwatch.net.RelayApi
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
    }

    companion object {
        fun of(c: Context): App = c.applicationContext as App
    }
}
