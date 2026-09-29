package com.aitrade.ui.theme

import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

// ---------------------------------------------------------------------------
// Palette
// ---------------------------------------------------------------------------
// Un terminal financier vit sur un fond neutre profond : la hiérarchie vient des
// SURFACES (quatre niveaux) et de la TYPOGRAPHIE, pas de bordures partout. Les
// accents restent réservés au sens (haussier / baissier / action / alerte).
//
// Les anciens noms publics sont conservés : une quarantaine d'écrans s'y
// réfèrent directement. Leur VALEUR est ce qui change ici.

// Surfaces, du plus profond au plus élevé.
val CharcoalBackground = Color(0xFF0A0D13)
val DarkCard = Color(0xFF121823)
val ElevatedCard = Color(0xFF18202E)
val SurfaceHigh = Color(0xFF212C3D)
val BorderColor = Color(0xFF283449)
val BorderSubtle = Color(0xFF1A2331)

// Accents — lisibles sur fond sombre, sans fluo.
val CyberBlue = Color(0xFF3B82F6)
val CyberBlueBright = Color(0xFF7FB0FF)
val ProfitGreen = Color(0xFF16C784)
val LossRed = Color(0xFFEA3943)
val GoldYellow = Color(0xFFF0B90B)

// Texte, trois niveaux de contraste.
val LightGrayText = Color(0xFFE7EBF3)
val MutedText = Color(0xFF93A0B8)
val DisabledText = Color(0xFF5B6779)

// Alias sémantiques (préférés dans le code neuf).
val TextPrimary = LightGrayText
val TextSecondary = MutedText
val OnAccent = Color(0xFF08111F)

// Couleurs de statut, avec leur variante « fond translucide ».
val WarningAmber = GoldYellow

private val DarkColorScheme =
    darkColorScheme(
        primary = CyberBlue,
        onPrimary = Color.White,
        primaryContainer = SurfaceHigh,
        onPrimaryContainer = LightGrayText,
        secondary = ProfitGreen,
        onSecondary = OnAccent,
        secondaryContainer = Color(0xFF123528),
        onSecondaryContainer = ProfitGreen,
        tertiary = GoldYellow,
        onTertiary = OnAccent,
        background = CharcoalBackground,
        onBackground = LightGrayText,
        surface = DarkCard,
        onSurface = LightGrayText,
        surfaceVariant = ElevatedCard,
        onSurfaceVariant = MutedText,
        error = LossRed,
        onError = Color.White,
        outline = BorderColor,
        outlineVariant = BorderSubtle,
        surfaceTint = CyberBlue,
    )

// ---------------------------------------------------------------------------
// Typographie
// ---------------------------------------------------------------------------
// La police est SANS EMPATTEMENT pour tout ce qui se lit (titres, libellés) :
// le monospace était appliqué à l'écran entier, ce qui donnait cet aspect
// « sortie de console ». Il est désormais réservé aux VALEURS (prix, montants),
// via `NumericTextStyle` — un chiffre aligné reste monospace.
private val Sans = FontFamily.SansSerif
private val Mono = FontFamily.Monospace

/** Style des valeurs numériques (prix, solde, P&L) : chiffres alignés. */
val NumericTextStyle =
    TextStyle(
        fontFamily = Mono,
        fontWeight = FontWeight.SemiBold,
        fontSize = 14.sp,
        lineHeight = 20.sp,
    )

val AppTypography =
    Typography(
        displayLarge =
            TextStyle(
                fontFamily = Sans,
                fontWeight = FontWeight.Bold,
                fontSize = 40.sp,
                lineHeight = 46.sp,
                letterSpacing = (-0.5).sp,
            ),
        displayMedium =
            TextStyle(
                fontFamily = Sans,
                fontWeight = FontWeight.Bold,
                fontSize = 32.sp,
                lineHeight = 38.sp,
                letterSpacing = (-0.25).sp,
            ),
        displaySmall =
            TextStyle(
                fontFamily = Sans,
                fontWeight = FontWeight.SemiBold,
                fontSize = 27.sp,
                lineHeight = 33.sp,
            ),
        headlineMedium =
            TextStyle(
                fontFamily = Sans,
                fontWeight = FontWeight.SemiBold,
                fontSize = 23.sp,
                lineHeight = 29.sp,
            ),
        headlineSmall =
            TextStyle(
                fontFamily = Sans,
                fontWeight = FontWeight.SemiBold,
                fontSize = 20.sp,
                lineHeight = 26.sp,
            ),
        titleLarge =
            TextStyle(
                fontFamily = Sans,
                fontWeight = FontWeight.SemiBold,
                fontSize = 18.sp,
                lineHeight = 24.sp,
            ),
        titleMedium =
            TextStyle(
                fontFamily = Sans,
                fontWeight = FontWeight.SemiBold,
                fontSize = 16.sp,
                lineHeight = 22.sp,
                letterSpacing = 0.1.sp,
            ),
        titleSmall =
            TextStyle(
                fontFamily = Sans,
                fontWeight = FontWeight.Medium,
                fontSize = 14.sp,
                lineHeight = 20.sp,
            ),
        bodyLarge =
            TextStyle(
                fontFamily = Sans,
                fontWeight = FontWeight.Normal,
                fontSize = 15.sp,
                lineHeight = 22.sp,
            ),
        bodyMedium =
            TextStyle(
                fontFamily = Sans,
                fontWeight = FontWeight.Normal,
                fontSize = 14.sp,
                lineHeight = 20.sp,
            ),
        bodySmall =
            TextStyle(
                fontFamily = Sans,
                fontWeight = FontWeight.Normal,
                fontSize = 13.sp,
                lineHeight = 18.sp,
            ),
        labelLarge =
            TextStyle(
                fontFamily = Sans,
                fontWeight = FontWeight.SemiBold,
                fontSize = 13.sp,
                lineHeight = 18.sp,
                letterSpacing = 0.2.sp,
            ),
        labelMedium =
            TextStyle(
                fontFamily = Sans,
                fontWeight = FontWeight.Medium,
                fontSize = 12.sp,
                lineHeight = 16.sp,
                letterSpacing = 0.4.sp,
            ),
        labelSmall =
            TextStyle(
                fontFamily = Sans,
                fontWeight = FontWeight.Medium,
                fontSize = 11.sp,
                lineHeight = 15.sp,
                letterSpacing = 0.4.sp,
            ),
    )

// ---------------------------------------------------------------------------
// Frange dense de l'échelle (`labelSmall` et en dessous)
// ---------------------------------------------------------------------------
// Un terminal financier affiche beaucoup de texte très petit : légendes de
// métriques, badges de statut, sous-libellés de tarifs, puces de résultats. Ces
// tailles vivent SOUS le plancher de Material (`labelSmall` = 11.sp), qui s'y
// arrête. Elles sont définies ici plutôt qu'écrites `9.sp` dans un écran, pour
// deux raisons :
//
// 1. l'échelle typographique reste **définie à un seul endroit** — la retoucher
//    se fait d'un geste au lieu d'une chasse au `fontSize` dans 40 fichiers ;
// 2. les fichiers d'écran ne portent plus aucune taille en dur : ils nomment un
//    rôle, comme pour les tailles Material.
//
// Le nom suit la même logique descendante que Material (`labelLarge` >
// `labelMedium` > `labelSmall`), prolongée vers le bas. Chaque style indique sa
// taille : aucune ambiguïté, même hors de ce fichier.

/** 10.sp — libellé court : en-tête de métrique, puce de statut, chip dense. */
val LabelExtraSmall =
    TextStyle(
        fontFamily = Sans,
        fontWeight = FontWeight.Medium,
        fontSize = 10.sp,
        lineHeight = 14.sp,
        letterSpacing = 0.3.sp,
    )

/** 9.sp — légende de métrique, sous-titre de carte, détail secondaire dense. */
val LabelTiny =
    TextStyle(
        fontFamily = Sans,
        fontWeight = FontWeight.Medium,
        fontSize = 9.sp,
        lineHeight = 13.sp,
        letterSpacing = 0.3.sp,
    )

/**
 * 8.sp — la plus petite taille de l'échelle : pastilles et badges courts
 * (« LONG », « 87% »). En dessous, aucun texte n'est lisible sur mobile ;
 * allonger le libellé plutôt que descendre encore.
 */
val LabelMicro =
    TextStyle(
        fontFamily = Sans,
        fontWeight = FontWeight.Medium,
        fontSize = 8.sp,
        lineHeight = 12.sp,
        letterSpacing = 0.4.sp,
    )

// ---------------------------------------------------------------------------
// Formes, espacement, élévation
// ---------------------------------------------------------------------------

val AppShapes =
    Shapes(
        extraSmall = RoundedCornerShape(8.dp),
        small = RoundedCornerShape(12.dp),
        medium = RoundedCornerShape(16.dp),
        large = RoundedCornerShape(22.dp),
        extraLarge = RoundedCornerShape(28.dp),
    )

/** Échelle d'espacement — évite les `8.dp`/`14.dp`/`18.dp` improvisés partout. */
object Spacing {
    val xs = 4.dp
    val sm = 8.dp
    val md = 12.dp
    val lg = 16.dp
    val xl = 24.dp
    val xxl = 32.dp
}

/** Rayons nommés, pour les formes qui ne passent pas par `MaterialTheme.shapes`. */
object Radius {
    /** Boutons : arrondi franc, jamais une pilule (voir [ButtonShape]). */
    val button = 10.dp
    val badge = 8.dp
    val card = 16.dp
    val sheet = 22.dp
}

/**
 * Forme de **tous** les boutons de l'application.
 *
 * Material 3 donne par défaut aux boutons la forme `corner full`, c'est-à-dire
 * entièrement arrondie : les quarante boutons de l'app étaient donc des pilules
 * sans que personne ne l'ait décidé. Un rayon fixe les rattache à la grille des
 * cartes, et
 * `scripts/kotlin_ui_check.py` refuse un bouton qui reprendrait la forme par
 * défaut. Oublier de la passer ne se voit qu'en regardant l'écran : ce contrôle
 * est donc confié à un test.
 */
val ButtonShape = RoundedCornerShape(Radius.button)

@Composable
fun AiTradeTheme(
    darkTheme: Boolean = true,
    content: @Composable () -> Unit,
) {
    MaterialTheme(
        colorScheme = DarkColorScheme,
        typography = AppTypography,
        shapes = AppShapes,
        content = content,
    )
}
