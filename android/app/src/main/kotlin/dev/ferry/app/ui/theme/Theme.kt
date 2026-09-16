package dev.ferry.app.ui.theme

import android.app.Activity
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat

// Enforce a strict dark monochrome theme regardless of system setting
private val FerryColorScheme = darkColorScheme(
    primary = Primary,
    onPrimary = OnPrimary,
    primaryContainer = SurfaceContainerHighest,
    onPrimaryContainer = OnSurface,
    secondary = Secondary,
    onSecondary = OnSecondary,
    secondaryContainer = SurfaceContainerHigh,
    onSecondaryContainer = OnSurface,
    tertiary = Primary,
    onTertiary = OnPrimary,
    tertiaryContainer = SurfaceContainerLow,
    onTertiaryContainer = OnSurface,
    background = Background,
    onBackground = OnBackground,
    surface = Surface,
    onSurface = OnSurface,
    surfaceVariant = SurfaceVariant,
    onSurfaceVariant = OnSurfaceVariant,
    outline = Outline,
    outlineVariant = OutlineVariant,
    error = Error,
    onError = OnError,
    errorContainer = SurfaceContainerHighest,
    onErrorContainer = Error
)

@Composable
fun FerryTheme(
    darkTheme: Boolean = isSystemInDarkTheme(), // Ignored, always dark
    dynamicColor: Boolean = false, // Ignored, always strict palette
    content: @Composable () -> Unit
) {
    val colorScheme = FerryColorScheme
    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as Activity).window
            window.statusBarColor = colorScheme.background.toArgb()
            window.navigationBarColor = colorScheme.background.toArgb()
            WindowCompat.getInsetsController(window, view).isAppearanceLightStatusBars = false
            WindowCompat.getInsetsController(window, view).isAppearanceLightNavigationBars = false
        }
    }

    MaterialTheme(
        colorScheme = colorScheme,
        typography = Typography,
        content = content
    )
}
