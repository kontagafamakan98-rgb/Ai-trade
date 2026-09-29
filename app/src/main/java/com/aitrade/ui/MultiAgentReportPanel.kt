package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun MultiAgentReportPanel(
    report: MultiAgentResearchReport?,
    onInjectSignal: (MultiAgentResearchReport) -> Unit,
) {
    val strings = LocalAppStrings.current

    report?.let { rep ->
        // Libellé localisé : `consensusAction` reste un code stable.
        val actionLabel = actionLabel(strings, rep.consensusAction)

        // Consensus Banner
        val actionColor =
            if (rep.consensusAction.contains("BUY")) {
                ProfitGreen
            } else if (rep.consensusAction.contains("SELL")) {
                LossRed
            } else {
                GoldYellow
            }

        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(containerColor = DarkBlue.copy(alpha = 0.5f)),
            border = BorderStroke(1.dp, actionColor),
        ) {
            Column(modifier = Modifier.padding(12.dp)) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Box(
                            modifier =
                                Modifier
                                    .background(actionColor, RoundedCornerShape(4.dp))
                                    .padding(horizontal = 8.dp, vertical = 3.dp),
                        ) {
                            Text(
                                actionLabel,
                                color = Color.White,
                                style = MaterialTheme.typography.labelMedium
                                    .copy(fontWeight = FontWeight.ExtraBold, fontFamily = FontFamily.Monospace),
                            )
                        }
                        Text(
                            String.format(strings.agentTargetAsset, rep.asset),
                            style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Bold),
                            color = Color.White
                        )
                    }

                    Text(
                        String.format(strings.agentConfidence, rep.confidencePct),
                        style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                        color = actionColor
                    )
                }

                Spacer(modifier = Modifier.height(8.dp))

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    Text(
                        String.format(strings.agentEntry, rep.suggestedEntry),
                        style = MaterialTheme.typography.labelSmall,
                        color = LightGrayText,
                    )
                    Text(
                        String.format(strings.agentStopLoss, rep.suggestedStopLoss),
                        style = MaterialTheme.typography.labelSmall,
                        color = LossRed,
                    )
                    Text(
                        String.format(strings.agentTakeProfit, rep.suggestedTakeProfit),
                        style = MaterialTheme.typography.labelSmall,
                        color = ProfitGreen,
                    )
                }
            }
        }

        Spacer(modifier = Modifier.height(12.dp))

        // 4 Specialized Agent Cards Grid
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            AgentReportRow(rep.macroReport, Icons.Default.Public, GoldYellow)
            AgentReportRow(rep.quantReport, Icons.Default.ShowChart, CyberBlue)
            AgentReportRow(rep.sentimentReport, Icons.Default.Newspaper, ProfitGreen)
            AgentReportRow(rep.riskReport, Icons.Default.Shield, LossRed)
        }

        Spacer(modifier = Modifier.height(12.dp))

        // Synthesized Reasoning Box
        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(containerColor = CharcoalBackground),
            border = BorderStroke(1.dp, BorderColor),
        ) {
            Column(modifier = Modifier.padding(12.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    Icon(Icons.Default.AutoAwesome, contentDescription = null, tint = CyberBlue, modifier = Modifier.size(16.dp))
                    Text(
                        strings.agentCioSynthesis,
                        style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                        color = CyberBlue
                    )
                }
                Spacer(modifier = Modifier.height(6.dp))
                Text(
                    rep.synthesizedReasoning,
                    style = MaterialTheme.typography.labelSmall,
                    color = Color.White
                )
            }
        }

        Spacer(modifier = Modifier.height(12.dp))

        // Inject consensus signal button
        OutlinedButton(
            onClick = { onInjectSignal(rep) },
            border = BorderStroke(1.dp, ProfitGreen),
            colors = ButtonDefaults.outlinedButtonColors(containerColor = ProfitGreen.copy(alpha = 0.15f)),
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(8.dp),
        ) {
            Row(
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Icon(Icons.Default.FlashOn, contentDescription = null, tint = ProfitGreen, modifier = Modifier.size(18.dp))
                Text(
                    strings.agentInjectSignal,
                    color = ProfitGreen,
                    style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                )
            }
        }
    }
}
