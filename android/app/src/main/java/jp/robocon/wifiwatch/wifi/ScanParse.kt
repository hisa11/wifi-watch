package jp.robocon.wifiwatch.wifi

import android.net.wifi.ScanResult
import android.os.Build
import jp.robocon.wifiwatch.core.Channels
import jp.robocon.wifiwatch.core.Ies
import jp.robocon.wifiwatch.core.LiveTable

/** ScanResult を、IEを読んで使う形（LiveTable.Obs）にする。IEの読み方はPC側と同じ（core/Ies.kt）。 */
object ScanParse {
    fun obs(r: ScanResult): LiveTable.Obs? {
        val bssid = r.BSSID?.lowercase() ?: return null
        val elements = r.informationElements.map { Ies.Ie(it.id, it.idExt, ByteArray(it.bytes.remaining()).also { b -> it.bytes.duplicate().get(b) }) }
        val info = Ies.parse(elements)
        val op = if (elements.isNotEmpty()) Ies.operating(info, r.frequency) else fallback(r)
        val ssid = if (Build.VERSION.SDK_INT >= 33) r.wifiSsid?.toString()?.trim('"') else @Suppress("DEPRECATION") r.SSID
        return LiveTable.Obs(
            bssid, info.ssid ?: ssid?.ifEmpty { null }, r.frequency, r.centerFreq0, r.level,
            if (elements.isNotEmpty()) Ies.hex(Ies.join(elements)) else null, standard(r.wifiStandard, info), op,
            r.timestamp / 1000, // 起動からのマイクロ秒 → ミリ秒
        )
    }

    /** IEが取れないときは、ScanResult の幅と中心周波数から決める。 */
    private fun fallback(r: ScanResult): Ies.Operating {
        val w = when (r.channelWidth) { 1 -> 40; 2 -> 80; 3, 4 -> 160; 5 -> 320; else -> 20 }
        val spans = if (w == 20 || r.centerFreq0 == 0) listOf(Channels.span20(r.frequency))
        else listOf(Channels.Span.around(r.centerFreq0.toDouble(), w.toDouble()))
        return Ies.Operating(Channels.bandOf(r.frequency) ?: Channels.B6, r.frequency, w, "$w", spans, "android", false)
    }

    private fun standard(ws: Int, info: Ies.Info): String = when (ws) {
        8 -> "be"; 6 -> "ax"; 5 -> "ac"; 4 -> "n"; 1 -> "legacy"
        else -> info.standard
    }
}
