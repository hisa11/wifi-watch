package jp.robocon.wifiwatch

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import jp.robocon.wifiwatch.ui.BandsScreen
import jp.robocon.wifiwatch.ui.MainViewModel
import jp.robocon.wifiwatch.ui.RecordsScreen
import jp.robocon.wifiwatch.ui.ScanScreen
import jp.robocon.wifiwatch.ui.SettingsScreen
import jp.robocon.wifiwatch.ui.StatusScreen
import jp.robocon.wifiwatch.ui.WalkScreen
import jp.robocon.wifiwatch.ui.WifiTheme
import jp.robocon.wifiwatch.ui.verdictOf

class MainActivity : ComponentActivity() {
    private val vm: MainViewModel by viewModels()

    @OptIn(ExperimentalMaterial3Api::class)
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        applyDebugExtras()
        enableEdgeToEdge()
        setContent {
            WifiTheme {
                // 前面にいる間だけ2秒ごとに状態を取る（背面では取らない）
                LifecycleEventEffect(Lifecycle.Event.ON_START) { vm.startPolling() }
                LifecycleEventEffect(Lifecycle.Event.ON_STOP) { vm.stopPolling() }
                var tab by rememberSaveable { mutableStateOf(if (vm.settings.configured) "status" else "settings") }
                var openRec by rememberSaveable { mutableStateOf<String?>(null) }
                val live by vm.live.collectAsStateWithLifecycle()
                val v = verdictOf(live)
                Scaffold(
                    topBar = {
                        TopAppBar(
                            title = { Text("wifiwatch・${v.title}", fontWeight = FontWeight.Bold, fontSize = 17.sp) },
                            actions = { TextButton(onClick = { tab = "settings" }) { Text("設定") } },
                        )
                    },
                    bottomBar = {
                        NavigationBar {
                            for ((key, glyph, label) in TABS) {
                                NavigationBarItem(
                                    selected = tab == key, onClick = { tab = key },
                                    icon = { Text(glyph, fontSize = 18.sp) }, label = { Text(label) },
                                )
                            }
                        }
                    },
                ) { pad ->
                    Box(Modifier.padding(pad)) {
                        when (tab) {
                            "status" -> StatusScreen(vm)
                            "scan" -> ScanScreen(vm) { openRec = it; tab = "records" }
                            "bands" -> BandsScreen(vm)
                            "records" -> RecordsScreen(vm, openRec) { openRec = null }
                            "walk" -> WalkScreen(vm)
                            else -> SettingsScreen(vm) { tab = "status" }
                        }
                    }
                }
            }
        }
    }

    /**
     * デバッグ版だけ：起動の引数で設定を入れる（エミュレータでの通しの確認用）。リリース版では何もしない。
     *   adb shell am start -n jp.robocon.wifiwatch/.MainActivity --es relay_url http://10.0.2.2:8787 --es token XXX [--ez pin_cellular false]
     */
    private fun applyDebugExtras() {
        if (!BuildConfig.DEBUG) return
        val i = intent ?: return
        val s = vm.settings
        i.getStringExtra("relay_url")?.let { if (s.validateUrl(it) == null) s.relayUrl = it }
        i.getStringExtra("token")?.let { if (it.length >= 16) s.token = it }
        if (i.hasExtra("pin_cellular")) s.pinCellular = i.getBooleanExtra("pin_cellular", true)
    }

    private companion object {
        val TABS = listOf(
            Triple("status", "◉", "状態"), Triple("scan", "⟳", "スキャン"), Triple("bands", "☰", "帯域"),
            Triple("records", "▤", "記録"), Triple("walk", "⌖", "歩測"),
        )
    }
}
