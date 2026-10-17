package jp.robocon.wifiwatch.data

import android.content.Context
import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONArray
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * トークンから空白・改行を取り除く。トークンに空白は入らないので、コピーで付いた改行（端末の画面から写すと末尾に付く）を落とす。
 * 改行が残ると Authorization ヘッダに入れられず、中継に届かない（OkHttp が「Unexpected char 0x0a」で断る）。
 */
fun cleanToken(v: String): String = v.filterNot { it.isWhitespace() || it.isISOControl() }

/** 入れられないトークンなら理由を返す（16文字以上、ヘッダに入る半角の英数字・記号だけ）。 */
fun tokenProblem(v: String): String? = when {
    v.length < 16 -> "トークンは16文字以上です"
    v.any { it.code !in 0x21..0x7e } -> "トークンに使えない文字が入っています（半角の英数字と記号だけ）。全角や余計な文字が入っていないか確かめてください"
    else -> null
}

/**
 * 設定：中継のURL、アプリ用のトークン（Android Keystoreの鍵で暗号化して保存）、場所ラベルの選択肢、端末名。
 */
class Settings(context: Context) : jp.robocon.wifiwatch.net.RelayConfigSource {
    private val prefs = context.getSharedPreferences("settings", Context.MODE_PRIVATE)

    override var relayUrl: String
        get() = prefs.getString("relay_url", "") ?: ""
        set(v) = prefs.edit().putString("relay_url", v.trim().trimEnd('/')).apply()

    override var token: String
        get() = prefs.getString("token_enc", null)?.let { decrypt(it) }?.let(::cleanToken) ?: "" // 前に改行ごと保存したものも使えるように
        set(v) = cleanToken(v).let { t -> prefs.edit().putString("token_enc", if (t.isEmpty()) null else encrypt(t)).apply() }

    var locations: List<String>
        get() = prefs.getString("locations", null)?.let { s -> JSONArray(s).let { a -> (0 until a.length()).map { a.getString(it) } } }
            ?: listOf("ピット", "フィールド北", "フィールド南", "客席東", "客席西")
        set(v) = prefs.edit().putString("locations", JSONArray(v.map { it.trim() }.filter { it.isNotEmpty() }.distinct()).toString()).apply()

    var deviceName: String
        get() = prefs.getString("device_name", null) ?: Build.MODEL.lowercase().replace(' ', '-')
        set(v) = prefs.edit().putString("device_name", v.trim()).apply()

    /** 本番はオン。モバイル回線のない端末で試すときだけオフにする（エミュレータは模擬のモバイル回線があるのでオンのままでよい）。 */
    override var pinCellular: Boolean
        get() = prefs.getBoolean("pin_cellular", true)
        set(v) = prefs.edit().putBoolean("pin_cellular", v).apply()

    override val configured get() = relayUrl.isNotEmpty() && token.length >= 16

    fun validateUrl(u: String): String? {
        val s = u.trim().trimEnd('/')
        if (s.startsWith("https://")) return null
        if (Regex("^http://(10\\.0\\.2\\.2|127\\.0\\.0\\.1|localhost)(:\\d+)?$").matches(s)) return null
        return "中継のURLは https:// で始めてください（開発用の http://10.0.2.2:ポート は可）"
    }

    // ------------------------------------------------------------ Keystore
    private fun key(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getEntry(ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return gen.generateKey()
    }

    private fun encrypt(plain: String): String {
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, key())
        val ct = c.doFinal(plain.toByteArray(Charsets.UTF_8))
        return Base64.encodeToString(c.iv + ct, Base64.NO_WRAP)
    }

    private fun decrypt(s: String): String? = try {
        val all = Base64.decode(s, Base64.NO_WRAP)
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, all.copyOfRange(0, 12)))
        String(c.doFinal(all.copyOfRange(12, all.size)), Charsets.UTF_8)
    } catch (e: Exception) {
        null // 鍵が消えた（端末の初期化など）ときは入れ直してもらう
    }

    private companion object {
        const val ALIAS = "wifiwatch-app-token"
    }
}
