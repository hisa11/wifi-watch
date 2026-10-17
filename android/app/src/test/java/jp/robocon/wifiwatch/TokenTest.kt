package jp.robocon.wifiwatch

import jp.robocon.wifiwatch.data.cleanToken
import jp.robocon.wifiwatch.data.tokenProblem
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class TokenTest {
    private val tok = "Ab3_-xY9zQ0123456789abcdefghijklmnopqrstuvw" // secrets.token_urlsafe(32) と同じ形（43文字）

    @Test
    fun copiedNewlineAndSpacesAreDropped() {
        // 端末の画面から写すと末尾に改行が付く（実機で「Unexpected char 0x0a at 50 in Authorization value」になった）
        assertEquals(tok, cleanToken("$tok\n"))
        assertEquals(tok, cleanToken("  $tok\r\n"))
        assertEquals(tok, cleanToken("${tok.take(20)}\n${tok.drop(20)}"))
        assertNull(tokenProblem(cleanToken("$tok\n")))
    }

    @Test
    fun badTokensAreRefused() {
        assertNotNull(tokenProblem("short"))
        assertNotNull(tokenProblem("ａｂｃ" + tok)) // 全角はヘッダに入らない
        assertNull(tokenProblem(tok))
    }
}
