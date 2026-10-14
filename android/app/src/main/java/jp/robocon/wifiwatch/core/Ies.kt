package jp.robocon.wifiwatch.core

import java.security.MessageDigest

/**
 * 情報要素（IE）の解析と、主ch・帯域幅の決定。PC側の server/wifiwatch/capture/ies.py と同じ答えを出す
 * （testdata/ies_cases.json で照合する）。
 *
 * Android の ScanResult.InformationElement は (id, idExt, 中身) で、拡張（id=255）の中身は拡張IDを除いたもの。
 * 生のバイト列からも同じ形に直して読む。
 */
object Ies {
    data class Ie(val id: Int, val idExt: Int, val body: ByteArray)

    data class HtOp(val primary: Int, val secOffset: Int, val staWidthAny: Boolean)
    data class VhtOp(val width: Int, val ccfs0: Int, val ccfs1: Int)
    data class SixOp(val primary: Int, val widthCode: Int, val dupBeacon: Boolean, val ccfs0: Int, val ccfs1: Int)
    data class HeOp(val bssColor: Int, val vht: VhtOp?, val six: SixOp?)
    data class EhtOp(val infoPresent: Boolean, val widthCode: Int?, val ccfs0: Int?, val ccfs1: Int?)
    data class Rsn(val akms: List<String>, val mfpr: Boolean, val mfpc: Boolean)
    data class Wps(val manufacturer: String?, val modelName: String?, val modelNumber: String?, val deviceName: String?)
    data class Rnr(val opClass: Int, val channel: Int, val freq: Int?, val bssid: String?, val colocated: Boolean, val sameSsid: Boolean)

    class Info {
        var ssid: String? = null
        var ssidHidden = false
        var ds: Int? = null
        var ht: HtOp? = null
        var vht: VhtOp? = null
        var he: HeOp? = null
        var eht: EhtOp? = null
        var hasHtCap = false
        var hasVhtCap = false
        var hasHeCap = false
        var hasEhtCap = false
        var rsn: Rsn? = null
        var wpa = false
        var wps: Wps? = null
        var country: String? = null
        val rnr = mutableListOf<Rnr>()
        val vendorOuis = mutableListOf<String>()
        var fingerprint: String? = null

        val standard: String
            get() = when {
                hasEhtCap || eht != null -> "be"
                hasHeCap || he != null -> "ax"
                hasVhtCap || vht != null -> "ac"
                hasHtCap || ht != null -> "n"
                else -> "legacy"
            }

        val security: String
            get() {
                val r = rsn
                if (r != null) {
                    val a = r.akms
                    if (a.any { it.startsWith("SAE") }) return "WPA3-SAE" + if (a.any { "PSK" in it }) "/PSK" else ""
                    if ("OWE" in a) return "OWE"
                    if (a.any { "PSK" in it }) return "WPA2-PSK"
                    if (a.any { "802.1X" in it }) return "WPA2/3-Enterprise"
                    return "RSN"
                }
                return if (wpa) "WPA" else "open"
            }
    }

    private val AKM = mapOf(
        1 to "802.1X", 2 to "PSK", 3 to "FT-802.1X", 4 to "FT-PSK", 5 to "802.1X-SHA256", 6 to "PSK-SHA256",
        8 to "SAE", 9 to "FT-SAE", 11 to "802.1X-SuiteB", 12 to "802.1X-SuiteB-192", 13 to "FT-802.1X-SHA384",
        14 to "FILS-SHA256", 15 to "FILS-SHA384", 18 to "OWE", 19 to "FT-PSK-SHA384", 20 to "PSK-SHA384",
        24 to "SAE-EXT-KEY", 25 to "FT-SAE-EXT-KEY",
    )

    private fun u(b: ByteArray, i: Int) = b[i].toInt() and 0xFF
    private fun le16(b: ByteArray, i: Int) = u(b, i) or (u(b, i + 1) shl 8)
    private fun be16(b: ByteArray, i: Int) = (u(b, i) shl 8) or u(b, i + 1)
    private fun le32(b: ByteArray, i: Int): Long = (le16(b, i).toLong()) or (le16(b, i + 2).toLong() shl 16)
    private fun oui(b: ByteArray, i: Int) = "%02x:%02x:%02x".format(u(b, i), u(b, i + 1), u(b, i + 2))
    private fun mac(b: ByteArray, i: Int) = (0 until 6).joinToString(":") { "%02x".format(u(b, i + it)) }

    /** 生のIE列（ビーコンの固定部の後ろ）を (id, idExt, 中身) の並びにする。壊れた末尾では止まる。 */
    fun split(raw: ByteArray): List<Ie> {
        val out = mutableListOf<Ie>()
        var off = 0
        while (off + 2 <= raw.size) {
            val id = u(raw, off)
            val ln = u(raw, off + 1)
            val start = off + 2
            val stop = start + ln
            if (stop > raw.size) break
            if (id == 255 && ln >= 1) out += Ie(255, u(raw, start), raw.copyOfRange(start + 1, stop))
            else out += Ie(id, 0, raw.copyOfRange(start, stop))
            off = stop
        }
        return out
    }

    fun parseRaw(raw: ByteArray): Info = parse(split(raw))

    fun parse(elements: List<Ie>): Info {
        val info = Info()
        val toks = mutableListOf<String>()
        for (ie in elements) {
            val b = ie.body
            when {
                ie.id == 255 -> toks += "255.${ie.idExt}"
                ie.id == 221 && b.size >= 4 -> toks += "221.${oui(b, 0)}.${u(b, 3)}"
                else -> toks += "${ie.id}"
            }
            try {
                when (ie.id) {
                    0 -> if (b.isEmpty() || b.all { it.toInt() == 0 }) { info.ssidHidden = true; info.ssid = null }
                    else info.ssid = String(b, Charsets.UTF_8)
                    3 -> if (b.isNotEmpty()) info.ds = u(b, 0)
                    7 -> if (b.size >= 2) info.country = String(b, 0, 2, Charsets.US_ASCII)
                    45 -> info.hasHtCap = true
                    48 -> info.rsn = parseRsn(b)
                    61 -> if (b.size >= 2) info.ht = HtOp(u(b, 0), u(b, 1) and 0x03, (u(b, 1) and 0x04) != 0)
                    191 -> info.hasVhtCap = true
                    192 -> if (b.size >= 3) info.vht = VhtOp(u(b, 0), u(b, 1), u(b, 2))
                    201 -> info.rnr += parseRnr(b)
                    221 -> if (b.size >= 4) {
                        val o = oui(b, 0)
                        if (o !in info.vendorOuis) info.vendorOuis += o
                        if (o == "00:50:f2" && u(b, 3) == 4) info.wps = parseWps(b, 4)
                        if (o == "00:50:f2" && u(b, 3) == 1) info.wpa = true
                    }
                    255 -> when (ie.idExt) {
                        35 -> info.hasHeCap = true
                        36 -> info.he = parseHeOp(b)
                        106 -> info.eht = parseEhtOp(b)
                        108 -> info.hasEhtCap = true
                    }
                }
            } catch (_: IndexOutOfBoundsException) {
                // 壊れたIEは飛ばす
            }
        }
        val md = MessageDigest.getInstance("SHA-1").digest(toks.joinToString(",").toByteArray())
        info.fingerprint = md.take(5).joinToString("") { "%02x".format(it) }
        return info
    }

    private fun parseRsn(b: ByteArray): Rsn? {
        if (b.size < 2) return null
        var off = 2
        val akms = mutableListOf<String>()
        var caps = 0
        if (off + 4 <= b.size) off += 4
        if (off + 2 <= b.size) {
            val n = le16(b, off); off += 2
            repeat(n) { if (off + 4 <= b.size) off += 4 }
        }
        if (off + 2 <= b.size) {
            val n = le16(b, off); off += 2
            repeat(n) {
                if (off + 4 <= b.size) {
                    val o = oui(b, off)
                    val t = u(b, off + 3)
                    akms += if (o == "00:0f:ac") AKM[t] ?: "type$t" else "$o/$t"
                    off += 4
                }
            }
        }
        if (off + 2 <= b.size) caps = le16(b, off)
        return Rsn(akms, (caps and 0x40) != 0, (caps and 0x80) != 0)
    }

    private fun parseWps(b: ByteArray, start: Int): Wps {
        var off = start
        var m: String? = null; var mn: String? = null; var num: String? = null; var dev: String? = null
        while (off + 4 <= b.size) {
            val t = be16(b, off)
            val ln = be16(b, off + 2)
            val v = off + 4
            if (v + ln > b.size) break
            val s = String(b, v, ln, Charsets.UTF_8).trim('\u0000').trim().take(64)
            when (t) {
                0x1021 -> m = s
                0x1023 -> mn = s
                0x1024 -> num = s
                0x1011 -> dev = s
            }
            off = v + ln
        }
        return Wps(m, mn, num, dev)
    }

    private fun parseHeOp(b: ByteArray): HeOp? {
        if (b.size < 6) return null
        val params = u(b, 0) or (u(b, 1) shl 8) or (u(b, 2) shl 16)
        var off = 6
        var vht: VhtOp? = null
        var six: SixOp? = null
        if (params and 0x004000 != 0) {
            if (off + 3 > b.size) return null
            vht = VhtOp(u(b, off), u(b, off + 1), u(b, off + 2)); off += 3
        }
        if (params and 0x008000 != 0) off += 1
        if (params and 0x020000 != 0) {
            if (off + 5 > b.size) return null
            val ctrl = u(b, off + 1)
            six = SixOp(u(b, off), ctrl and 0x03, (ctrl and 0x04) != 0, u(b, off + 2), u(b, off + 3))
        }
        return HeOp(u(b, 3) and 0x3F, vht, six)
    }

    private fun parseEhtOp(b: ByteArray): EhtOp? {
        if (b.size < 5) return null
        val params = u(b, 0)
        if (params and 0x01 != 0 && b.size >= 8) return EhtOp(true, u(b, 5) and 0x07, u(b, 6), u(b, 7))
        return EhtOp(false, null, null, null)
    }

    private val OPCLASS_BAND: Map<Int, String> = buildMap {
        (81..84).forEach { put(it, Channels.B24) }
        (115..130).forEach { put(it, Channels.B5) }
        (131..137).forEach { put(it, Channels.B6) }
    }

    private fun parseRnr(b: ByteArray): List<Rnr> {
        val out = mutableListOf<Rnr>()
        var off = 0
        while (off + 4 <= b.size) {
            val hdr = u(b, off)
            val len = u(b, off + 1)
            val op = u(b, off + 2)
            val chn = u(b, off + 3)
            val count = (hdr shr 4) + 1
            val hdrColoc = (hdr and 0x08) != 0
            off += 4
            val band = OPCLASS_BAND[op]
            val freq = band?.let { Channels.numberToFreq(it, chn) }
            repeat(count) {
                if (len == 0 || off + len > b.size) return out
                var bssid: String? = null
                var params: Int? = null
                when {
                    len >= 7 -> {
                        bssid = mac(b, off + 1)
                        if (len >= 11) { if (len >= 12) params = u(b, off + 11) } else if (len >= 8) params = u(b, off + 7)
                    }
                    len >= 5 -> if (len >= 6) params = u(b, off + 5)
                    len >= 2 -> params = u(b, off + 1)
                }
                val coloc = hdrColoc || (params != null && params and 0x40 != 0)
                val same = params != null && params and 0x02 != 0
                out += Rnr(op, chn, freq, bssid, coloc, same)
                off += len
            }
        }
        return out
    }

    data class Operating(
        val band: String, val primaryFreq: Int, val width: Int, val widthLabel: String,
        val spans: List<Channels.Span>, val source: String, val suspect: Boolean,
    )

    fun operating(info: Info, rxFreq: Int): Operating {
        val band = Channels.bandOf(rxFreq) ?: Channels.B6
        val f = { n: Int -> Channels.numberToFreq(band, n).toDouble() }
        var primary: Int? = null
        var psrc = "rx"
        val six = info.he?.six
        when {
            band == Channels.B6 && six != null -> { primary = Channels.numberToFreq(band, six.primary); psrc = "he6" }
            band == Channels.B24 && info.ds != null && info.ds != 0 -> { primary = Channels.numberToFreq(band, info.ds!!); psrc = "ds" }
            info.ht != null && info.ht!!.primary != 0 -> { primary = Channels.numberToFreq(band, info.ht!!.primary); psrc = "ht" }
            info.ds != null && info.ds != 0 -> { primary = Channels.numberToFreq(band, info.ds!!); psrc = "ds" }
        }
        if (primary == null || Channels.bandOf(primary) != band) { primary = rxFreq; psrc = "rx" }
        val p = primary
        var res: Triple<Int, String, List<Channels.Span>>? = null
        var src = psrc
        val eht = info.eht
        if (eht != null && eht.infoPresent && eht.widthCode != null && band != Channels.B24) {
            val widths = mapOf(0 to 20, 1 to 40, 2 to 80, 3 to 160, 4 to 320)
            val w = widths[eht.widthCode]
            if (w != null) {
                val c = if (w >= 160) eht.ccfs1 else eht.ccfs0
                if (w == 20) res = Triple(20, "20", listOf(Channels.span20(p)))
                else if (c != null && c != 0) res = Triple(w, "$w", listOf(Channels.Span.around(f(c), w.toDouble())))
                src = "eht"
            }
        }
        if (res == null && band == Channels.B6 && six != null) {
            src = "he6"
            res = when (six.widthCode) {
                0 -> Triple(20, "20", listOf(Channels.span20(p)))
                1, 2 -> {
                    val w = if (six.widthCode == 1) 40 else 80
                    Triple(w, "$w", listOf(Channels.Span.around(f(six.ccfs0), w.toDouble())))
                }
                else -> when {
                    six.ccfs1 != 0 && kotlin.math.abs(six.ccfs1 - six.ccfs0) == 8 ->
                        Triple(160, "160", listOf(Channels.Span.around(f(six.ccfs1), 160.0)))
                    six.ccfs1 != 0 && kotlin.math.abs(six.ccfs1 - six.ccfs0) > 16 ->
                        Triple(160, "80+80", listOf(Channels.Span.around(f(six.ccfs0), 80.0), Channels.Span.around(f(six.ccfs1), 80.0)))
                    Channels.blockCenter(p, 160)?.toDouble() == f(six.ccfs0) ->
                        Triple(160, "160", listOf(Channels.Span.around(f(six.ccfs0), 160.0)))
                    else -> Triple(80, "80", listOf(Channels.Span.around(f(six.ccfs0), 80.0)))
                }
            }
        }
        if (res == null && band == Channels.B5) {
            val v = info.vht ?: info.he?.vht
            if (v != null && v.width != 0) {
                res = vhtSpans(v, f)
                if (res != null) src = "vht"
            }
        }
        val ht = info.ht
        if (res == null && ht != null && ht.staWidthAny && (ht.secOffset == 1 || ht.secOffset == 3)) {
            val center = if (ht.secOffset == 1) p + 10.0 else p - 10.0
            res = Triple(40, "40", listOf(Channels.Span.around(center, 40.0)))
            src = "ht"
        }
        if (res == null) res = Triple(20, "20", listOf(Channels.span20(p)))
        var (w, wl, spans) = res
        var suspect = false
        if (spans.none { it.lo <= p - 10 && p + 10 <= it.hi }) {
            w = 20; wl = "20"; spans = listOf(Channels.span20(p)); suspect = true
        }
        return Operating(band, p, w, wl, spans, src, suspect)
    }

    private fun vhtSpans(v: VhtOp, f: (Int) -> Double): Triple<Int, String, List<Channels.Span>>? {
        val f0 = f(v.ccfs0)
        return when (v.width) {
            1 -> when {
                v.ccfs1 == 0 -> Triple(80, "80", listOf(Channels.Span.around(f0, 80.0)))
                kotlin.math.abs(v.ccfs1 - v.ccfs0) == 8 -> Triple(160, "160", listOf(Channels.Span.around(f(v.ccfs1), 160.0)))
                kotlin.math.abs(v.ccfs1 - v.ccfs0) > 16 ->
                    Triple(160, "80+80", listOf(Channels.Span.around(f0, 80.0), Channels.Span.around(f(v.ccfs1), 80.0)))
                else -> Triple(80, "80", listOf(Channels.Span.around(f0, 80.0)))
            }
            2 -> Triple(160, "160", listOf(Channels.Span.around(f0, 160.0)))
            3 -> if (v.ccfs1 != 0) Triple(160, "80+80", listOf(Channels.Span.around(f0, 80.0), Channels.Span.around(f(v.ccfs1), 80.0))) else null
            else -> null
        }
    }

    fun hex(b: ByteArray) = b.joinToString("") { "%02x".format(it) }
    fun unhex(s: String): ByteArray = ByteArray(s.length / 2) { s.substring(it * 2, it * 2 + 2).toInt(16).toByte() }

    /** IE列をまとめて生のバイト列にする（歩測の記録に残して、PCでも同じ解析ができるように）。 */
    fun join(elements: List<Ie>): ByteArray {
        val out = java.io.ByteArrayOutputStream()
        for (ie in elements) {
            if (ie.id == 255) {
                out.write(255); out.write(ie.body.size + 1); out.write(ie.idExt); out.write(ie.body)
            } else {
                out.write(ie.id); out.write(ie.body.size); out.write(ie.body)
            }
        }
        return out.toByteArray()
    }
}
