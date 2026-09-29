package com.aitrade.ui

import androidx.annotation.StringRes
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.R
import com.aitrade.ui.theme.*

/**
 * Documents légaux consultables depuis l'application.
 *
 * Les textes vivent dans les ressources de chaînes par défaut, en **français
 * seulement**, sous
 * `translatable="false"` : les traduire dans les dix langues de l'interface
 * demanderait dix rédactions juridiques, qui divergeraient les unes des autres.
 * Un texte unique et exact vaut mieux. La ressource reste dans le fichier de
 * base, donc elle s'affiche telle quelle quelle que soit la langue choisie.
 */
enum class LegalDocument(
    @StringRes val titleRes: Int,
    @StringRes val bodyRes: Int,
) {
    PRIVACY(R.string.legal_privacy_title, R.string.legal_privacy_body),
    TERMS(R.string.legal_terms_title, R.string.legal_terms_body),
}

/** Carte d'accès aux documents légaux, dans l'onglet « Synchro Cloud & VIP ». */
@Composable
fun LegalLinksCard(onOpen: (LegalDocument) -> Unit) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        Column(
            modifier = Modifier.padding(Spacing.lg),
            verticalArrangement = Arrangement.spacedBy(Spacing.sm),
        ) {
            Text(
                stringResource(R.string.legal_section_title),
                style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Bold),
                color = LightGrayText,
            )
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(Spacing.sm),
            ) {
                TextButton(shape = ButtonShape, onClick = { onOpen(LegalDocument.PRIVACY) }) {
                    Text(stringResource(R.string.legal_privacy_title))
                }
                TextButton(shape = ButtonShape, onClick = { onOpen(LegalDocument.TERMS) }) {
                    Text(stringResource(R.string.legal_terms_title))
                }
            }
        }
    }
}

/**
 * Un document légal, en boîte de dialogue défilable.
 *
 * Le corps fait plusieurs milliers de caractères : il est donc seul à défiler,
 * ce qui laisse le titre et le bouton de fermeture visibles en permanence.
 */
@Composable
fun LegalDocumentDialog(
    document: LegalDocument,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = {
            Text(
                stringResource(document.titleRes),
                color = LightGrayText,
                style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
            )
        },
        text = {
            Column(
                modifier =
                    Modifier
                        .verticalScroll(rememberScrollState())
                        .testTag("legal_body"),
                verticalArrangement = Arrangement.spacedBy(Spacing.sm),
            ) {
                Text(
                    stringResource(R.string.legal_updated),
                    color = MutedText,
                    style = MaterialTheme.typography.labelSmall,
                )
                Text(
                    stringResource(document.bodyRes),
                    color = LightGrayText,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        },
        confirmButton = {
            TextButton(shape = ButtonShape, onClick = onDismiss) {
                Text(stringResource(R.string.legal_close))
            }
        },
    )
}
