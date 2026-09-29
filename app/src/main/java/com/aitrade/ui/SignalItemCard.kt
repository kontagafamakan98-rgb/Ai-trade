package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
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
import com.aitrade.data.Signal
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun SignalItemCard(signal: Signal) {
    val strings = LocalAppStrings.current
    val statusColor =
        when (signal.status) {
            "EXECUTED" -> ProfitGreen
            "BLOCKED_RISK" -> GoldYellow
            "CLOSED" -> Color.Gray
            else -> LightGrayText
        }

    Card(
        modifier =
            Modifier
                .fillMaxWidth()
                .padding(bottom = 8.dp),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        Column(modifier = Modifier.padding(14.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text(
                        signal.asset,
                        style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                        color = Color.White
                    )
                    Text(
                        if (signal.direction == "BUY") strings.buy else strings.sell,
                        color = if (signal.direction == "BUY") ProfitGreen else LossRed,
                        style = MaterialTheme.typography.labelLarge
                            .copy(fontWeight = FontWeight.ExtraBold, fontFamily = FontFamily.Monospace),
                    )
                }
                Text(
                    statusLabel(strings, signal.status),
                    color = statusColor,
                    style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                )
            }
            Spacer(modifier = Modifier.height(8.dp))
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Column {
                    Text(strings.entryLabel, color = MutedText, style = LabelExtraSmall)
                    Text(
                        "\$${String.format("%.2f", signal.entry)}",
                        color = Color.White,
                        style = MaterialTheme.typography.labelMedium.copy(fontFamily = FontFamily.Monospace),
                    )
                }
                Column {
                    Text(strings.stopLossLabel, color = MutedText, style = LabelExtraSmall)
                    Text(
                        "\$${String.format("%.2f", signal.stopLoss)}",
                        color = LossRed,
                        style = MaterialTheme.typography.labelMedium.copy(fontFamily = FontFamily.Monospace),
                    )
                }
                Column {
                    Text(strings.takeProfitLabel, color = MutedText, style = LabelExtraSmall)
                    Text(
                        "\$${String.format("%.2f", signal.takeProfit)}",
                        color = ProfitGreen,
                        style = MaterialTheme.typography.labelMedium.copy(fontFamily = FontFamily.Monospace),
                    )
                }
                Column {
                    Text(strings.confidenceLabel, color = MutedText, style = LabelExtraSmall)
                    Text(
                        "${String.format("%.1f", signal.confidence * 100)}%",
                        color = CyberBlue,
                        style = MaterialTheme.typography.labelMedium.copy(fontFamily = FontFamily.Monospace),
                    )
                }
            }
            Spacer(modifier = Modifier.height(8.dp))
            HorizontalDivider(color = BorderColor, thickness = 0.5.dp)
            Spacer(modifier = Modifier.height(8.dp))
            Text(
                String.format(strings.signalTaSummary, signal.taSummary),
                style = MaterialTheme.typography.labelSmall,
                color = LightGrayText
            )
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                String.format(strings.signalSentiment, signal.sentimentSummary),
                style = MaterialTheme.typography.labelSmall,
                color = LightGrayText
            )
            if (signal.pnl != 0.0) {
                Spacer(modifier = Modifier.height(6.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("${strings.realizedPnl}: ", style = MaterialTheme.typography.labelMedium, color = MutedText)
                    Text(
                        "${if (signal.pnl > 0) "+" else ""}\$${String.format("%.2f", signal.pnl)}",
                        style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                        color = if (signal.pnl > 0) ProfitGreen else LossRed
                    )
                }
            }
        }
    }
}
