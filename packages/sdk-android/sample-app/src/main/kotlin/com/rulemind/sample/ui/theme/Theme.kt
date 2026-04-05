package com.rulemind.sample.ui.theme

import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.sp
import com.rulemind.sample.R

val RuleMindBlue = Color(0xFF005394)
val RuleMindBlueContainer = Color(0xFF2B6CB0)
val RuleMindInk = Color(0xFF181C1E)
val RuleMindSurface = Color(0xFFF7FAFC)
val RuleMindSurfaceLow = Color(0xFFF1F4F6)
val RuleMindSurfaceHigh = Color(0xFFE5E9EB)
val RuleMindOutline = Color(0xFFC1C7D2)
val ApproveGreen = Color(0xFF22C55E)
val ReviewAmber = Color(0xFFF59E0B)
val RejectRed = Color(0xFFEF4444)

private val LightColorScheme = lightColorScheme(
    primary = RuleMindBlue,
    onPrimary = Color.White,
    primaryContainer = RuleMindBlueContainer,
    secondary = RuleMindBlueContainer,
    onSurface = RuleMindInk,
    onSurfaceVariant = Color(0xFF6F6A73),
    surface = RuleMindSurface,
    background = RuleMindSurface,
    surfaceContainerLow = RuleMindSurfaceLow,
    surfaceContainerLowest = Color.White,
    surfaceContainerHigh = RuleMindSurfaceHigh,
    surfaceDim = Color(0xFFE9EEF2),
    outlineVariant = RuleMindOutline,
    error = RejectRed,
)

private val ManropeFamily = FontFamily(
    Font(R.font.manrope_variable, weight = FontWeight.Bold),
    Font(R.font.manrope_variable, weight = FontWeight.ExtraBold),
)

private val InterFamily = FontFamily(
    Font(R.font.inter_regular, weight = FontWeight.Normal),
    Font(R.font.inter_medium, weight = FontWeight.Medium),
    Font(R.font.inter_semibold, weight = FontWeight.SemiBold),
    Font(R.font.inter_bold, weight = FontWeight.Bold),
)

private val RuleMindTypography = Typography(
    displaySmall = TextStyle(fontFamily = ManropeFamily, fontSize = 34.sp, lineHeight = 40.sp, fontWeight = FontWeight.ExtraBold),
    headlineMedium = TextStyle(fontFamily = ManropeFamily, fontSize = 30.sp, lineHeight = 36.sp, fontWeight = FontWeight.ExtraBold),
    headlineSmall = TextStyle(fontFamily = ManropeFamily, fontSize = 24.sp, lineHeight = 30.sp, fontWeight = FontWeight.Bold),
    titleLarge = TextStyle(fontFamily = InterFamily, fontSize = 22.sp, lineHeight = 28.sp, fontWeight = FontWeight.Bold),
    titleMedium = TextStyle(fontFamily = InterFamily, fontSize = 16.sp, lineHeight = 22.sp, fontWeight = FontWeight.SemiBold),
    bodyLarge = TextStyle(fontFamily = InterFamily, fontSize = 16.sp, lineHeight = 24.sp, fontWeight = FontWeight.Normal),
    bodyMedium = TextStyle(fontFamily = InterFamily, fontSize = 14.sp, lineHeight = 22.sp, fontWeight = FontWeight.Normal),
    bodySmall = TextStyle(fontFamily = InterFamily, fontSize = 12.sp, lineHeight = 18.sp, fontWeight = FontWeight.Normal),
    labelMedium = TextStyle(fontFamily = InterFamily, fontSize = 12.sp, lineHeight = 16.sp, fontWeight = FontWeight.Medium),
    labelSmall = TextStyle(fontFamily = InterFamily, fontSize = 11.sp, lineHeight = 14.sp, fontWeight = FontWeight.SemiBold),
)

@Composable
fun RuleMindTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = LightColorScheme,
        typography = RuleMindTypography,
        content = content,
    )
}
