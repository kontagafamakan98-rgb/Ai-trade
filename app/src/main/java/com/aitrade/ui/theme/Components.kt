package com.aitrade.ui.theme

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp

/**
 * Carte standard : une seule forme, une seule bordure discrète, une seule
 * respiration interne. Les écrans codaient jusqu'ici leur `Card` à la main
 * (`containerColor = DarkCard`, `BorderStroke(1.dp, BorderColor)`, padding
 * variable) — d'où des cartes toutes légèrement différentes.
 */
@Composable
fun AppCard(
    modifier: Modifier = Modifier,
    containerColor: Color = DarkCard,
    content: @Composable ColumnScope.() -> Unit,
) {
    Surface(
        modifier = modifier.fillMaxWidth(),
        shape = MaterialTheme.shapes.medium,
        color = containerColor,
        border = BorderStroke(1.dp, BorderSubtle),
    ) {
        Column(
            modifier = Modifier.padding(Spacing.lg),
            verticalArrangement = Arrangement.spacedBy(Spacing.md),
            content = content,
        )
    }
}

/**
 * Titre de section : petites capitales espacées, contraste secondaire. Remplace
 * les `Text(..., fontSize = 14.sp, color = MutedText)` répétés et incohérents.
 */
@Composable
fun SectionHeader(
    text: String,
    modifier: Modifier = Modifier,
    trailing: (@Composable () -> Unit)? = null,
) {
    Row(
        modifier = modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = text.uppercase(),
            style = MaterialTheme.typography.labelMedium,
            color = MutedText,
            fontWeight = FontWeight.SemiBold,
            modifier = Modifier.weight(1f),
        )
        trailing?.invoke()
    }
}

/** Étiquette d'état (« LIVE », « SIM »…) : fond translucide, texte accentué. */
@Composable
fun StatusPill(
    text: String,
    color: Color,
    modifier: Modifier = Modifier,
) {
    Text(
        text = text,
        color = color,
        style = MaterialTheme.typography.labelSmall,
        fontWeight = FontWeight.Bold,
        modifier =
            modifier
                .clip(RoundedCornerShape(Radius.badge))
                .background(color.copy(alpha = 0.16f))
                .padding(horizontal = Spacing.sm, vertical = Spacing.xs),
    )
}

/** Valeur numérique (prix, solde, P&L) : chiffres alignés en monospace. */
@Composable
fun StatValue(
    value: String,
    modifier: Modifier = Modifier,
    color: Color = LightGrayText,
) {
    Text(
        text = value,
        color = color,
        style = NumericTextStyle,
        modifier = modifier,
    )
}
