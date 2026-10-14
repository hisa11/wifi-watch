package jp.robocon.wifiwatch

import jp.robocon.wifiwatch.core.Bodies
import jp.robocon.wifiwatch.core.PcState
import jp.robocon.wifiwatch.core.PollResult
import jp.robocon.wifiwatch.core.StationRow
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** testdata/relay の見本（PC側・中継・アプリで共用）。 */
class SamplesTest {
    private fun j(n: String) = JSONObject(TestData.text("relay/$n"))

    @Test
    fun parsesStateCopy() {
        val s = PcState.parse(j("pc_state.json"))
        assertEquals("LIVE", s.mode)
        assertEquals(6135, s.ownFreq)
        assertEquals(57, s.channels.size)
        assertTrue(s.blockFreqs.contains(6135))
        assertEquals("done", s.commands["app-scan-0001"]?.state)
        val p = PollResult.parse(j("app_poll_response.json"))
        assertNotNull(p.state)
        assertEquals(1000L, p.stateAgeMs)
    }

    @Test
    fun buildsBodiesLikeSamples() {
        assertTrue(same(j("app_command_scan.json"), Bodies.scan("app-scan-0001", listOf("6"), null, false)))
        assertTrue(same(j("app_command_bands.json"), Bodies.bands("app-bands-0001", listOf("2.4"), listOf(5180))))
        val lab = Bodies.label("app-label-0001", "客席東", listOf("試合前", "下見"), "アプリから", 1_791_936_200.0)
        assertTrue(same(j("app_label.json"), lab))
    }

    @Test
    fun parsesRecordStations() {
        val rec = j("pc_record.json")
        val rows = (0 until rec.getJSONArray("stations").length()).map { StationRow.parse(rec.getJSONArray("stations").getJSONObject(it)) }
        assertTrue(rows.isNotEmpty())
        assertTrue(rows.all { it.spans.isNotEmpty() })
        val walk = j("app_walk.json").getJSONObject("record")
        val w = StationRow.parse(walk.getJSONArray("stations").getJSONObject(1))
        assertEquals(320, w.width)
        assertEquals(5945.0, w.spans[0].lo, 0.0)
    }

    /** JSONの中身が同じか（キーの順は問わない。数は値で比べる）。 */
    private fun same(a: Any?, b: Any?): Boolean = when {
        a is org.json.JSONObject && b is org.json.JSONObject ->
            a.keys().asSequence().toSet() == b.keys().asSequence().toSet() && a.keys().asSequence().all { same(a.get(it), b.get(it)) }
        a is org.json.JSONArray && b is org.json.JSONArray -> a.length() == b.length() && (0 until a.length()).all { same(a.get(it), b.get(it)) }
        a is Number && b is Number -> a.toDouble() == b.toDouble()
        else -> a == b
    }
}
