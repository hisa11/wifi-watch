package jp.robocon.wifiwatch

import jp.robocon.wifiwatch.core.PcView
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PcViewTest {
    @Test
    fun parsesWhatThePcPuts() {
        // testdata/relay/app_pcview.json は PC（偽の無線）の pc_view() から作った見本
        val v = PcView.parse(JSONObject(TestData.text("relay/app_pcview.json")))
        assertEquals(1791936101000, v.forMs)
        assertNotNull(v.state)
        assertEquals("6GHz ch37", v.state!!.ownLabel)
        assertTrue(v.stations.isNotEmpty())
        val s = v.stations.first()
        assertTrue(s.spans.isNotEmpty() && s.freq > 2000)
        assertTrue(v.state!!.channels.isNotEmpty()) // 見ていないchを網かけにするのに使う
        assertEquals(-70, v.strong)
    }

    @Test
    fun noViewYet() {
        val v = PcView.parse(JSONObject("""{"now": 5, "view": null, "for": null, "received_at": null}"""))
        assertNull(v.forMs)
        assertNull(v.state)
        assertTrue(v.stations.isEmpty())
    }
}
