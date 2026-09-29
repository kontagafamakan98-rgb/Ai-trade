package com.aitrade.ui

import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import com.aitrade.api.AdminProbeFailure
import com.aitrade.api.AdminProbeOutcome
import com.aitrade.api.AdminProbeReport
import com.aitrade.ui.theme.*

// --- LE RÉSULTAT DE LA SONDE : LE RAPPORT, OU LA RAISON DE SON ABSENCE ---
//
// Séparé de l'écran pour une raison de fond autant que de taille : ce fichier ne
// connaît ni les champs de saisie ni la confirmation de l'aller-retour, il ne
// fait que **rendre** ce que le backend a répondu.
//
// Les noms et les détails des vérifications sont affichés **tels quels** : ce sont
// les mots de `scripts/check_supabase.py`, pas des libellés d'interface. Les
// traduire ici les ferait diverger de ce que dit la commande équivalente, qui est
// affichée juste en dessous — et deux formulations d'un même verdict finissent
// toujours par se contredire.

/**
 * Le résultat : le rapport du backend, ou la raison de son absence.
 *
 * Un échec n'est pas rendu comme un rapport vide : « la sonde n'a pas répondu »
 * et « la sonde a répondu que tout va bien » ne doivent pas se ressembler à
 * l'écran. C'est la même règle que celle du rapport, dont le verdict est porté
 * par la réponse elle-même.
 */
@Composable
fun AdminOutcomeCard(
    outcome: AdminProbeOutcome,
    modifier: Modifier = Modifier,
) {
    val strings = LocalAppStrings.current

    AppCard(modifier = modifier) {
        when (outcome) {
            is AdminProbeOutcome.Report -> AdminReportBody(report = outcome.report)
            is AdminProbeOutcome.Failed -> {
                SectionHeader(strings.adminErrorTitle)
                Text(
                    text = adminFailureText(outcome, strings),
                    style = MaterialTheme.typography.labelMedium,
                    color = LossRed,
                )
            }
        }
    }
}

/** Chaque échec a sa phrase : ils n'appellent pas la même conduite. */
private fun adminFailureText(
    failure: AdminProbeOutcome.Failed,
    strings: AppStrings,
): String =
    when (failure.kind) {
        AdminProbeFailure.NOT_CONFIGURED -> strings.adminNotConfigured
        AdminProbeFailure.BAD_URL -> String.format(strings.adminErrorBadUrl, failure.detail)
        AdminProbeFailure.REFUSED ->
            String.format(strings.adminErrorRefused, failure.code.toString(), failure.detail)
        AdminProbeFailure.UNREACHABLE -> String.format(strings.adminErrorUnreachable, failure.detail)
    }

@Composable
private fun AdminReportBody(report: AdminProbeReport) {
    val strings = LocalAppStrings.current

    SectionHeader(strings.adminReportTitle)
    StatusPill(
        text = if (report.ok) strings.adminReportPassed else strings.adminReportFailed,
        color = if (report.ok) ProfitGreen else LossRed,
    )
    report.selection?.tables?.let { tables ->
        if (tables.isNotEmpty()) {
            Text(
                String.format(strings.adminReportSelection, tables.joinToString(", ")),
                style = LabelTiny,
                color = MutedText,
            )
        }
    }
    // `writes` est `null` pour une consultation et une liste pour un aller-retour :
    // c'est ce qui distingue « je regarde » de « j'écris ». Une liste **vide** est
    // une troisième chose — l'aller-retour n'a trouvé, dans la sélection, aucune
    // table qu'il sache éprouver — et elle se dit, plutôt que de disparaître.
    report.writes?.let { written ->
        Text(
            String.format(
                strings.adminReportWrites,
                if (written.isEmpty()) strings.adminReportNone else written.joinToString(", "),
            ),
            style = LabelTiny,
            color = WarningAmber,
        )
    }
    // Le reste en base est toujours affiché, vide compris : c'est la seule ligne du
    // rapport qui parle d'un **dégât possible** dans la base, et une ligne absente
    // se lirait comme un « rien à signaler » qu'on n'a pas vérifié.
    Text(
        String.format(
            strings.adminReportLeftovers,
            if (report.leftovers.isEmpty()) {
                strings.adminReportNone
            } else {
                report.leftovers.joinToString(", ")
            },
        ),
        style = LabelTiny,
        color = if (report.leftovers.isEmpty()) MutedText else LossRed,
    )
    if (report.command.isNotBlank()) {
        Text(
            String.format(strings.adminReportCommand, report.command),
            style = LabelTiny,
            color = MutedText,
        )
    }
    report.sections.forEach { section ->
        SectionHeader(section.title)
        section.checks.forEach { check ->
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.Top,
            ) {
                StatusPill(
                    text = if (check.ok) strings.adminCheckOk else strings.adminCheckFailed,
                    color = if (check.ok) ProfitGreen else LossRed,
                )
                Spacer(modifier = Modifier.width(Spacing.sm))
                Text(
                    text = if (check.detail.isBlank()) check.name else "${check.name}: ${check.detail}",
                    style = MaterialTheme.typography.labelSmall,
                    color = if (check.ok) LightGrayText else LossRed,
                )
            }
        }
    }
}
