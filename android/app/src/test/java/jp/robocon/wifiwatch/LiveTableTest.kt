package jp.robocon.wifiwatch

import jp.robocon.wifiwatch.core.Channels
import jp.robocon.wifiwatch.core.Ies
import jp.robocon.wifiwatch.core.LiveTable
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LiveTableTest {
    private val sample = JSONObject(TestData.text("relay/app_walk.json")).getJSONObject("record")
    private val ies = Ies.unhex(sample.getJSONArray("stations").getJSONObject(0).getString("ies_hex"))
    private val op = Ies.operating(Ies.parseRaw(ies), 5975)

    private fun obs(bssid: String, rssi: Int, atMs: Long) =
        LiveTable.Obs(bssid, "KOSEN-A-6G", 5975, 0, rssi, Ies.hex(ies), "ax", op, atMs)

    @Test
    fun rereadingTheSameResultDoesNotCount() {
        // getScanResults は新しいスキャンが無くても前の結果を返し続ける。時刻が進んでいなければ数え直さない
        val t = LiveTable()
        assertTrue(t.add(listOf(obs("02:a0:00:00:00:01", -60, 1_000)), 2_000))
        assertFalse(t.add(listOf(obs("02:a0:00:00:00:01", -60, 1_000)), 5_000))
        assertEquals(1, t.updates)
        assertTrue(t.add(listOf(obs("02:a0:00:00:00:01", -50, 6_000)), 7_000))
        val st = t.stations(7_000).single()
        assertEquals(-50, st.rssi) // 表は最新の強さ
        assertEquals(2, st.samples.size)
    }

    @Test
    fun staleStationsFadeThenDisappear() {
        val t = LiveTable(presentMs = 60_000, keepMs = 120_000)
        t.add(listOf(obs("02:a0:00:00:00:01", -60, 0), obs("02:a0:00:00:00:02", -70, 0)), 0)
        t.add(listOf(obs("02:a0:00:00:00:01", -61, 70_000)), 70_000)
        // 2つ目は70秒聞こえていない：表には残るが「いない」。送る記録には入れない
        assertEquals(2, t.stations(70_000).size)
        assertEquals(listOf("02:a0:00:00:00:01"), t.snapshot(70_000).map { it.bssid })
        t.add(emptyList(), 130_000)
        assertEquals(listOf("02:a0:00:00:00:01"), t.stations(130_000).map { it.bssid }) // 2分を過ぎたら消す
        // 2分より前の結果（前のスキャンの残り）は取り込まない
        assertFalse(t.add(listOf(obs("02:a0:00:00:00:03", -40, 1_000)), 200_000))
    }

    @Test
    fun recordHasWalkShapeAndRecentMedian() {
        val t = LiveTable()
        for ((i, r) in listOf(-60, -58, -57).withIndex()) t.add(listOf(obs("02:a0:00:00:00:01", r, 1_000L + i * 5_000)), 12_000)
        val rec = LiveTable.record("walk-1", "z6561j", t, 12_000, 3, "フィールド北", listOf("スマホ"), "", 1.0)
        // 中継が受けるのは歩測の記録（kind=walk）。形は見本と同じ
        assertEquals("walk", rec.getString("kind"))
        assertEquals(sample.keys().asSequence().sorted().toList(), rec.keys().asSequence().sorted().toList())
        val st = rec.getJSONArray("stations").getJSONObject(0)
        val ss = sample.getJSONArray("stations").getJSONObject(0)
        assertTrue(ss.keys().asSequence().all { st.has(it) })
        assertEquals(-58, st.getInt("rssi")) // 最近の結果の中央値
        assertEquals(3, st.getInt("rssi_samples"))
        assertEquals(Channels.freqToNumber(5975), st.getInt("ch"))
        assertEquals("live", rec.getJSONObject("settings").getString("mode"))
    }
}
