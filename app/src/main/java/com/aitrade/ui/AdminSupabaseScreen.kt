package com.aitrade.ui

import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.*
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import com.aitrade.api.AdminProbeOutcome
import com.aitrade.data.AdminEndpoint
import com.aitrade.ui.theme.*

// --- ADMINISTRATION : LA SONDE SUPABASE DU BACKEND ---
//
// Cet écran ne fabrique **rien**. Il envoie ce que l'opérateur a saisi aux deux
// appels d'administration du backend — la consultation, qui ne lit que, et
// l'aller-retour, qui écrit puis supprime sur les tables nommées — et affiche la
// réponse telle quelle.
//
// C'est une différence assumée avec le reste de l'application, qui simule ses
// prix, ses actualités et son backtest. Un rapport **simulé** de vérification
// serait pire qu'aucun rapport : il ressemblerait à une base en bon état, et
// c'est exactement le silence que la sonde existe pour rompre. Sans point
// d'accès enregistré, l'écran le dit.
//
// Le point d'accès (URL + clé interne) se saisit ici et se conserve chiffré sur
// l'appareil, comme les identifiants courtier : la clé interne n'est jamais
// compilée dans l'application.
//
// L'aller-retour **écrit dans la base que le serveur utilise**, production
// comprise. Il passe donc par une confirmation qui **nomme** les tables visées —
// même verrou que le backend, qui refuse un nom inconnu avant la première
// écriture et ne replie jamais sur « toutes les tables ».

/** L'état d'un appel d'administration : en cours, ou le dernier résultat. */
data class AdminProbeUiState(
    val running: Boolean = false,
    val outcome: AdminProbeOutcome? = null,
)

/** Les noms saisis, séparés par des virgules, débarrassés du vide. */
private fun adminTableNames(raw: String): List<String> =
    raw.split(",").map { name -> name.trim() }.filter { name -> name.isNotEmpty() }

@Composable
fun AdminSupabaseScreen(
    endpoint: AdminEndpoint,
    state: AdminProbeUiState,
    onSaveEndpoint: (String, String) -> Unit,
    onRunCheck: () -> Unit,
    onRunRoundtrip: (List<String>) -> Unit,
    onClose: () -> Unit,
) {
    val strings = LocalAppStrings.current
    var baseUrl by remember { mutableStateOf(endpoint.baseUrl) }
    var apiKey by remember { mutableStateOf(endpoint.apiKey) }
    var tablesText by remember { mutableStateOf("") }
    var pendingTables by remember { mutableStateOf<List<String>?>(null) }

    LazyColumn(
        modifier =
            Modifier
                .fillMaxSize()
                .padding(horizontal = Spacing.lg)
                .testTag("admin_list"),
        verticalArrangement = Arrangement.spacedBy(Spacing.lg),
    ) {
        item {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    strings.adminScreenTitle,
                    style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                    color = Color.White,
                )
                TextButton(
                    shape = ButtonShape,
                    onClick = onClose,
                    colors = ButtonDefaults.textButtonColors(contentColor = CyberBlue),
                    modifier = Modifier.testTag("admin_close_button"),
                ) {
                    Text(strings.adminCloseButton, style = LabelExtraSmall)
                }
            }
        }

        item {
            Text(
                strings.adminScreenIntro,
                style = MaterialTheme.typography.labelMedium,
                color = MutedText,
            )
        }

        item {
            AdminEndpointCard(
                adminBaseUrl = baseUrl,
                adminApiKey = apiKey,
                onChangeUrl = { typed -> baseUrl = typed },
                onChangeKey = { typed -> apiKey = typed },
                onSave = { onSaveEndpoint(baseUrl, apiKey) },
            )
        }

        item {
            AppCard(modifier = Modifier.testTag("admin_actions_card")) {
                SectionHeader(strings.adminCheckTitle)
                if (!endpoint.configured) {
                    Text(
                        strings.adminNotConfigured,
                        style = LabelTiny,
                        color = WarningAmber,
                    )
                }
                TextButton(
                    shape = ButtonShape,
                    onClick = onRunCheck,
                    enabled = endpoint.configured && !state.running,
                    colors = ButtonDefaults.textButtonColors(contentColor = CyberBlue),
                    modifier = Modifier.testTag("admin_check_button"),
                ) {
                    Text(strings.adminCheckButton, style = LabelExtraSmall)
                }
            }
        }

        item {
            AppCard(modifier = Modifier.testTag("admin_roundtrip_card")) {
                SectionHeader(strings.adminRoundtripTitle)
                Text(
                    strings.adminRoundtripNote,
                    style = LabelTiny,
                    color = WarningAmber,
                )
                OutlinedTextField(
                    value = tablesText,
                    onValueChange = { tablesText = it },
                    label = { Text(strings.adminRoundtripTablesLabel) },
                    colors =
                        OutlinedTextFieldDefaults.colors(
                            focusedTextColor = Color.White,
                            unfocusedTextColor = Color.White,
                            focusedBorderColor = CyberBlue,
                            unfocusedBorderColor = BorderColor,
                        ),
                    modifier =
                        Modifier
                            .fillMaxWidth()
                            .testTag("admin_roundtrip_tables"),
                )
                Button(
                    shape = ButtonShape,
                    onClick = { pendingTables = adminTableNames(tablesText) },
                    enabled = endpoint.configured && !state.running && adminTableNames(tablesText).isNotEmpty(),
                    colors = ButtonDefaults.buttonColors(containerColor = LossRed),
                    modifier = Modifier.testTag("admin_roundtrip_button"),
                ) {
                    Text(strings.adminRoundtripButton, style = LabelExtraSmall)
                }
            }
        }

        if (state.running) {
            item {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.Center,
                ) {
                    CircularProgressIndicator(color = CyberBlue)
                }
            }
        }

        state.outcome?.let { outcome ->
            item {
                AdminOutcomeCard(outcome = outcome, modifier = Modifier.testTag("admin_outcome_card"))
            }
        }
    }

    pendingTables?.let { tables ->
        AdminRoundtripConfirmDialog(
            tables = tables,
            onConfirm = {
                pendingTables = null
                onRunRoundtrip(tables)
            },
            onDismiss = { pendingTables = null },
        )
    }
}

@Composable
private fun AdminEndpointCard(
    adminBaseUrl: String,
    adminApiKey: String,
    onChangeUrl: (String) -> Unit,
    onChangeKey: (String) -> Unit,
    onSave: () -> Unit,
) {
    val strings = LocalAppStrings.current

    AppCard(modifier = Modifier.testTag("admin_endpoint_card")) {
        SectionHeader(strings.adminConfigTitle)
        OutlinedTextField(
            value = adminBaseUrl,
            onValueChange = onChangeUrl,
            label = { Text(strings.adminBaseUrlLabel) },
            singleLine = true,
            colors =
                OutlinedTextFieldDefaults.colors(
                    focusedTextColor = Color.White,
                    unfocusedTextColor = Color.White,
                    focusedBorderColor = CyberBlue,
                    unfocusedBorderColor = BorderColor,
                ),
            modifier =
                Modifier
                    .fillMaxWidth()
                    .testTag("admin_url_field"),
        )
        OutlinedTextField(
            value = adminApiKey,
            onValueChange = onChangeKey,
            label = { Text(strings.adminApiKeyLabel) },
            singleLine = true,
            visualTransformation = PasswordVisualTransformation(),
            colors =
                OutlinedTextFieldDefaults.colors(
                    focusedTextColor = Color.White,
                    unfocusedTextColor = Color.White,
                    focusedBorderColor = CyberBlue,
                    unfocusedBorderColor = BorderColor,
                ),
            modifier =
                Modifier
                    .fillMaxWidth()
                    .testTag("admin_key_field"),
        )
        Text(
            strings.adminConfigNote,
            style = LabelTiny,
            color = MutedText,
        )
        Button(
            shape = ButtonShape,
            onClick = onSave,
            colors = ButtonDefaults.buttonColors(containerColor = CyberBlue, contentColor = OnAccent),
            modifier = Modifier.testTag("admin_save_button"),
        ) {
            Text(strings.adminSaveEndpoint, style = LabelExtraSmall)
        }
    }
}

/** La confirmation de l'aller-retour : les tables **nommées**, jamais « tout ». */
@Composable
private fun AdminRoundtripConfirmDialog(
    tables: List<String>,
    onConfirm: () -> Unit,
    onDismiss: () -> Unit,
) {
    val strings = LocalAppStrings.current

    AlertDialog(
        onDismissRequest = onDismiss,
        modifier = Modifier.testTag("admin_confirm_dialog"),
        title = { Text(strings.adminRoundtripConfirmTitle) },
        text = {
            Text(
                String.format(strings.adminRoundtripConfirmBody, tables.joinToString(", ")),
                style = MaterialTheme.typography.bodyMedium,
            )
        },
        confirmButton = {
            TextButton(
                shape = ButtonShape,
                onClick = onConfirm,
                colors = ButtonDefaults.textButtonColors(contentColor = LossRed),
                modifier = Modifier.testTag("admin_confirm_write"),
            ) {
                Text(strings.adminRoundtripConfirmWrite, style = LabelExtraSmall)
            }
        },
        dismissButton = {
            TextButton(
                shape = ButtonShape,
                onClick = onDismiss,
                colors = ButtonDefaults.textButtonColors(contentColor = MutedText),
            ) {
                Text(strings.cancelBtn, style = LabelExtraSmall)
            }
        },
    )
}
