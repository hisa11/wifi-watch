package jp.robocon.wifiwatch

import java.io.File

/** リポジトリの testdata/（PC側・中継・アプリで共用）を読む。 */
object TestData {
    private val root: File by lazy {
        var d: File? = File("").absoluteFile
        while (d != null && !File(d, "testdata").isDirectory) d = d.parentFile
        File(d ?: error("testdata が見つかりません"), "testdata")
    }

    fun text(name: String): String = File(root, name).readText(Charsets.UTF_8)
}
