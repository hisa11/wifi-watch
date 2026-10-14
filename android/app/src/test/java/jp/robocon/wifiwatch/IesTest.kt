package jp.robocon.wifiwatch

import jp.robocon.wifiwatch.core.Ies
import org.json.JSONArray
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

/** testdata/ies_cases.json（PC側と同じ見本）で、PC側と同じ答えになるか。 */
class IesTest {
    @Test
    fun matchesPcOnAllCases() {
        val cases = JSONArray(TestData.text("ies_cases.json"))
        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            val name = c.getString("name")
            val e = c.getJSONObject("expect")
            val info = Ies.parseRaw(Ies.unhex(c.getString("ies_hex")))
            val op = Ies.operating(info, c.getInt("rx_freq"))
            assertEquals(name, e.getInt("primary_freq"), op.primaryFreq)
            assertEquals(name, e.getInt("width"), op.width)
            assertEquals(name, e.getString("width_label"), op.widthLabel)
            val spans = e.getJSONArray("spans")
            assertEquals(name, spans.length(), op.spans.size)
            for (j in 0 until spans.length()) {
                assertEquals(name, spans.getJSONArray(j).getDouble(0), op.spans[j].lo, 0.0)
                assertEquals(name, spans.getJSONArray(j).getDouble(1), op.spans[j].hi, 0.0)
            }
            assertFalse(name, op.suspect)
            if (e.has("standard")) assertEquals(name, e.getString("standard"), info.standard)
            if (e.has("security")) assertEquals(name, e.getString("security"), info.security)
            if (e.has("ssid")) assertEquals(name, if (e.isNull("ssid")) null else e.getString("ssid"), info.ssid)
            if (e.has("ssid_hidden")) assertEquals(name, e.getBoolean("ssid_hidden"), info.ssidHidden)
            if (e.has("dup_beacon")) assertEquals(name, e.getBoolean("dup_beacon"), info.he?.six?.dupBeacon ?: false)
            if (e.has("pmf_required")) assertEquals(name, e.getBoolean("pmf_required"), info.rsn?.mfpr)
            if (e.has("wps")) {
                val w = e.getJSONObject("wps")
                assertEquals(name, w.getString("manufacturer"), info.wps?.manufacturer)
                assertEquals(name, w.getString("model_name"), info.wps?.modelName)
                assertEquals(name, w.getString("model_number"), info.wps?.modelNumber)
            }
            if (e.has("rnr")) {
                val r = e.getJSONArray("rnr")
                assertEquals(name, r.length(), info.rnr.size)
                for (j in 0 until r.length()) {
                    val x = r.getJSONObject(j)
                    assertEquals(name, x.getInt("freq"), info.rnr[j].freq)
                    assertEquals(name, if (x.isNull("bssid")) null else x.getString("bssid"), info.rnr[j].bssid)
                    assertEquals(name, x.getBoolean("colocated"), info.rnr[j].colocated)
                    assertEquals(name, x.getBoolean("same_ssid"), info.rnr[j].sameSsid)
                }
            }
        }
    }

    @Test
    fun splitAndJoinRoundTrip() {
        val cases = JSONArray(TestData.text("ies_cases.json"))
        for (i in 0 until cases.length()) {
            val raw = Ies.unhex(cases.getJSONObject(i).getString("ies_hex"))
            assertEquals(Ies.hex(raw), Ies.hex(Ies.join(Ies.split(raw))))
        }
    }
}
