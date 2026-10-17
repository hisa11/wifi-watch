package jp.robocon.wifiwatch

import jp.robocon.wifiwatch.core.Channels
import jp.robocon.wifiwatch.core.Ies
import jp.robocon.wifiwatch.core.PcState
import jp.robocon.wifiwatch.ui.MainViewModel
import jp.robocon.wifiwatch.ui.verdictOf
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class VerdictTest {
    private val base = PcState.parse(JSONObject(TestData.text("relay/pc_state.json")))

    private fun live(s: PcState?, ageMs: Long?) = MainViewModel.Live(state = s, stateAgeMs = ageMs, relayNow = 0, stateAtElapsed = 0, relayAtElapsed = 0)

    @Test
    fun staleStateIsNotNoDetection() {
        val v = verdictOf(live(base.copy(alerts = emptyList(), warn = 0, crit = 0), 45_000))
        assertEquals("PCから応答なし", v.title) // 古い状態を「検知なし」と出さない
        assertEquals("blind", v.level)
        // 端末が中継に届かないときは、PCのせいにしない
        val off = verdictOf(live(base, 45_000).copy(error = "モバイル回線が使えません"))
        assertEquals("中継に届かない", off.title)
        assertEquals("blind", off.level)
    }

    @Test
    fun levels() {
        assertEquals("警告 1", verdictOf(live(base, 1000)).title)
        val crit = base.copy(crit = 1, alerts = listOf(PcState.Alert("crit", "sig6_crit", "CineEyeの6GHz出現", "m", 0)))
        assertEquals("緊急 1", verdictOf(live(crit, 1000)).title)
        val none = base.copy(warn = 0, alerts = emptyList(), excludedBands = listOf("2.4"))
        val v = verdictOf(live(none, 1000))
        assertEquals("検知なし", v.title)
        assertTrue(v.sub.contains("2.4GHz（巡回外）")) // 見ていないところを添える
        // PCの無線が6GHz非対応なら、6GHzは「見ていない」の理由として添える
        val no6 = none.copy(excludedBands = emptyList(), unreceivableFreqs = Channels.freqs(Channels.B6))
        assertTrue(verdictOf(live(no6, 1000)).sub.contains("6GHz（この無線では受けられない）"))
        assertEquals("PCの状態がまだありません", verdictOf(live(null, null)).title)
        assertEquals("受信停止", verdictOf(live(base.copy(rxState = "failed"), 1000)).title)
    }

    @Test
    fun followNoticeIsReadFromStateCopy() {
        val o = JSONObject(TestData.text("relay/pc_state.json"))
        assertEquals(null, PcState.parse(o).followed) // ふだんは付いていない
        o.getJSONObject("own").put("followed", JSONObject().put("at", 1792056347).put("from", "6GHz ch37")
            .put("to", "6GHz ch21").put("ap", "hinji-great-hinji").put("reserved", false))
            .put("follow_pending", org.json.JSONArray().put("6GHz ch21・20MHz").put("6GHz ch53・20MHz"))
        val s = PcState.parse(o)
        assertEquals("6GHz ch21", s.followed?.to)
        assertEquals(false, s.followed?.reserved)
        assertEquals(listOf("6GHz ch21・20MHz", "6GHz ch53・20MHz"), s.followPending)
    }
}
