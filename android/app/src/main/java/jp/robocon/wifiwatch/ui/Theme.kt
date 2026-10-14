package jp.robocon.wifiwatch.ui

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color

/**
 * 色はデスクトップの画面と同じ役割で持つ（dataviz の validate_palette.js で明暗とも確認済み）。
 * 局の種類：自チーム=青● / 他高専=黄▲（交渉できる）/ 公式・放送=赤■（交渉できない）/ 所属不明=紫◆ / 既知=灰○。
 * 色だけに頼らず、記号と文字を必ず添える。
 */
@Immutable
data class WColors(
    val own: Color, val team: Color, val nonneg: Color, val unknown: Color, val ok: Color,
    val crit: Color, val warn: Color, val good: Color, val accent: Color,
    val ink: Color, val ink2: Color, val muted: Color, val grid: Color, val surface: Color, val surface2: Color,
    val surface3: Color, val hatch: Color, val ownBand: Color, val warnInk: Color,
)

val LightW = WColors(
    own = Color(0xFF2A78D6), team = Color(0xFFEDA100), nonneg = Color(0xFFD03B3B), unknown = Color(0xFF4A3AA7),
    ok = Color(0xFF898781), crit = Color(0xFFD03B3B), warn = Color(0xFFFAB219), good = Color(0xFF0CA30C),
    accent = Color(0xFF2A78D6), ink = Color(0xFF0B0B0B), ink2 = Color(0xFF52514E), muted = Color(0xFF7A7873),
    grid = Color(0xFFE1E0D9), surface = Color(0xFFFCFCFB), surface2 = Color(0xFFF3F2EE), surface3 = Color(0xFFEBEAE5),
    hatch = Color(0xFFB9B8B0), ownBand = Color(0x172A78D6), warnInk = Color(0xFF8A5A00),
)

val DarkW = WColors(
    own = Color(0xFF3987E5), team = Color(0xFFC98500), nonneg = Color(0xFFD03B3B), unknown = Color(0xFF9085E9),
    ok = Color(0xFF898781), crit = Color(0xFFD03B3B), warn = Color(0xFFFAB219), good = Color(0xFF0CA30C),
    accent = Color(0xFF3987E5), ink = Color(0xFFFFFFFF), ink2 = Color(0xFFC3C2B7), muted = Color(0xFF9A988F),
    grid = Color(0xFF2C2C2A), surface = Color(0xFF1A1A19), surface2 = Color(0xFF222220), surface3 = Color(0xFF2C2C2A),
    hatch = Color(0xFF55554F), ownBand = Color(0x213987E5), warnInk = Color(0xFFFAB219),
)

val LocalW = staticCompositionLocalOf { LightW }

data class KindStyle(val glyph: String, val label: String)

fun kindStyle(kind: String?): KindStyle = when (kind) {
    "own" -> KindStyle("●", "自チーム")
    "team" -> KindStyle("▲", "他高専")
    "official_infra" -> KindStyle("■", "公式インフラ")
    "broadcast" -> KindStyle("■", "放送機材")
    "known_ok" -> KindStyle("○", "既知")
    "unknown" -> KindStyle("◆", "所属不明")
    else -> KindStyle("◇", "未分類")
}

fun kindColor(w: WColors, kind: String?): Color = when (kind) {
    "own" -> w.own
    "team" -> w.team
    "official_infra", "broadcast" -> w.nonneg
    "known_ok" -> w.ok
    "unknown" -> w.unknown
    else -> w.ink2
}

@Composable
fun WifiTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val w = if (dark) DarkW else LightW
    val scheme = if (dark) darkColorScheme(
        primary = w.accent, background = Color(0xFF0D0D0D), surface = w.surface, surfaceVariant = w.surface2,
        onBackground = w.ink, onSurface = w.ink, onSurfaceVariant = w.ink2, error = w.crit,
    ) else lightColorScheme(
        primary = w.accent, background = Color(0xFFF9F9F7), surface = w.surface, surfaceVariant = w.surface2,
        onBackground = w.ink, onSurface = w.ink, onSurfaceVariant = w.ink2, error = w.crit,
    )
    androidx.compose.runtime.CompositionLocalProvider(LocalW provides w) {
        MaterialTheme(colorScheme = scheme, content = content)
    }
}
