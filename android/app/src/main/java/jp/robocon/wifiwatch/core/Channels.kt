package jp.robocon.wifiwatch.core

/**
 * 日本で使えるch（MHz）。chは周波数で持つ（ch番号は帯域をまたいで衝突する）。
 * PC側の server/wifiwatch/channels.py と同じ表。
 */
object Channels {
    const val B24 = "2.4"
    const val B5 = "5"
    const val B6 = "6"
    val BANDS = listOf(B6, B5, B24)
    val BAND_LABEL = mapOf(B24 to "2.4GHz", B5 to "5GHz", B6 to "6GHz")

    private val N24 = (1..13).toList()
    private val N5 = listOf(36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144)
    private val N6 = (1..93 step 4).toList()
    val PSC6 = setOf(5, 21, 37, 53, 69, 85)

    private val CENTERS5 = mapOf(
        40 to listOf(38, 46, 54, 62, 102, 110, 118, 126, 134, 142),
        80 to listOf(42, 58, 106, 122, 138),
        160 to listOf(50, 114),
    )
    private val CENTERS6 = mapOf(
        40 to listOf(3, 11, 19, 27, 35, 43, 51, 59, 67, 75, 83, 91),
        80 to listOf(7, 23, 39, 55, 71, 87),
        160 to listOf(15, 47, 79),
        320 to listOf(31, 63),
    )

    fun numberToFreq(band: String, n: Int): Int = when (band) {
        B24 -> if (n == 14) 2484 else 2407 + 5 * n
        B5 -> 5000 + 5 * n
        else -> if (n == 2) 5935 else 5950 + 5 * n
    }

    fun bandOf(freq: Number): String? {
        val f = freq.toDouble()
        return when {
            f in 2400.0..2500.0 -> B24
            f in 5150.0..5925.0 -> B5
            f > 5925.0 && f <= 7125.0 -> B6
            else -> null
        }
    }

    fun freqToNumber(freq: Int): Int? = when (bandOf(freq)) {
        B24 -> if (freq == 2484) 14 else (freq - 2407) / 5
        B5 -> (freq - 5000) / 5
        B6 -> if (freq == 5935) 2 else (freq - 5950) / 5
        else -> null
    }

    fun freqs(band: String): List<Int> = when (band) {
        B24 -> N24
        B5 -> N5
        else -> N6
    }.map { numberToFreq(band, it) }

    val ALL: List<Int> = BANDS.reversed().flatMap { freqs(it) }

    fun isKnown(freq: Int) = freq in ALL

    fun label(freq: Int): String {
        val b = bandOf(freq) ?: return "${freq}MHz"
        return "${BAND_LABEL[b]} ch${freqToNumber(freq)}"
    }

    fun short(freq: Int): String {
        val b = bandOf(freq) ?: return "$freq"
        return "${if (b == B24) "2.4G" else "${b}G"} ch${freqToNumber(freq)}"
    }

    data class Span(val lo: Double, val hi: Double) {
        fun overlaps(o: Span) = lo < o.hi && o.lo < hi
        val center get() = (lo + hi) / 2
        companion object {
            fun around(center: Double, width: Double) = Span(center - width / 2, center + width / 2)
        }
    }

    fun span20(freq: Int) = Span.around(freq.toDouble(), 20.0)

    /** 主chを含む幅widthのブロックの中心（MHz）。320MHzで2つ候補があるときは低い方。 */
    fun blockCenter(freq: Int, width: Int): Int? {
        val band = bandOf(freq) ?: return null
        val table = when (band) {
            B5 -> CENTERS5
            B6 -> CENTERS6
            else -> return null
        }
        for (c in table[width] ?: return null) {
            val cf = numberToFreq(band, c)
            if (Span.around(cf.toDouble(), width.toDouble()).let { it.lo < freq && freq < it.hi }) return cf
        }
        return null
    }

    fun subchannels(span: Span): List<Int> = ALL.filter { span.lo < it && it < span.hi }
}
