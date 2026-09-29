package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

// --- SYSTEM HEALTH & OBSERVABILITY CARD ---

@Composable
fun SystemHealthObservabilityCard(
    healthState: SystemHealthState,
    onResetCircuitBreaker: () -> Unit,
    onOpenAdmin: () -> Unit,
) {
    val strings = LocalAppStrings.current

    Card(
        modifier =
            Modifier
                .fillMaxWidth()
                .testTag("system_health_card"),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Box(
                        modifier =
                            Modifier
                                .size(8.dp)
                                .clip(CircleShape)
                                .background(ProfitGreen),
                    )
                    Spacer(modifier = Modifier.width(6.dp))
                    Text(
                        strings.systemHealthTitle,
                        style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Bold),
                        color = Color.White
                    )
                }

                TextButton(
                    shape = ButtonShape,
                    onClick = onResetCircuitBreaker,
                    colors = ButtonDefaults.textButtonColors(contentColor = CyberBlue),
                ) {
                    Text(strings.resetCircuitBreaker, style = LabelExtraSmall)
                }
            }

            Spacer(modifier = Modifier.height(8.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Column {
                    Text(strings.workerQueueThroughput, style = LabelExtraSmall, color = MutedText)
                    Text(
                        "${healthState.workerQueueThroughput} ${strings.jobsProcessed}",
                        style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                        color = Color.White
                    )
                }

                Column {
                    Text(strings.memoryUsage, style = LabelExtraSmall, color = MutedText)
                    Text(
                        String.format(strings.healthMemoryMb, String.format("%.1f", healthState.memoryUsageMb)),
                        style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                        color = Color.White
                    )
                }

                Column {
                    Text(strings.brokerState, style = LabelExtraSmall, color = MutedText)
                    // `brokerStatus` est un code interne stable
                    // (`BrokerStateCode`) : le libellé vient de la locale
                    // courante et la couleur est décidée sans comparer de
                    // texte traduit.
                    val displayStatus =
                        when (healthState.brokerStatus) {
                            BrokerStateCode.PAPER_SANDBOX -> strings.healthBrokerPaperSandbox
                            BrokerStateCode.ALPACA_PAPER -> strings.healthBrokerAlpacaPaper
                            BrokerStateCode.ALPACA_LIVE -> strings.healthBrokerAlpacaLive
                            else -> strings.healthBrokerUnknown
                        }
                    val displayColor =
                        when (healthState.brokerStatus) {
                            BrokerStateCode.ALPACA_LIVE -> LossRed
                            BrokerStateCode.ALPACA_PAPER -> ProfitGreen
                            else -> Color.Yellow
                        }
                    Text(
                        displayStatus,
                        style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                        color = displayColor,
                    )
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            // L'administration de la base se lance d'ici : c'est la carte qui
            // parle de l'état du système, et la sonde du backend en est le
            // prolongement direct. Un onglet de plus aurait porté la barre de
            // navigation à six destinations, au-delà de ce que Material
            // recommande.
            OutlinedButton(
                shape = ButtonShape,
                onClick = onOpenAdmin,
                colors = ButtonDefaults.outlinedButtonColors(contentColor = CyberBlue),
                modifier =
                    Modifier
                        .fillMaxWidth()
                        .testTag("admin_open_button"),
            ) {
                Text(strings.adminOpenButton, style = LabelExtraSmall)
            }
        }
    }
}
