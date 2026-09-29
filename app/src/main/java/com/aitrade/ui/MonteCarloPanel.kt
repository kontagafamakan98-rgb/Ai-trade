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
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun MonteCarloPanel(
    strings: AppStrings,
    backtestResult: BacktestResult,
    showMonteCarlo: Boolean,
) {
    // Monte Carlo Simulation Results Panel
    if (showMonteCarlo && backtestResult.monteCarlo != null) {
        val mc = backtestResult.monteCarlo
        Spacer(modifier = Modifier.height(10.dp))
        Card(
            modifier = Modifier.fillMaxWidth(),
            colors = CardDefaults.cardColors(containerColor = DarkBlue.copy(alpha = 0.6f)),
            border = BorderStroke(1.dp, CyberBlue.copy(alpha = 0.4f)),
        ) {
            Column(modifier = Modifier.padding(12.dp)) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        strings.monteCarloTitle,
                        style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                        color = CyberBlue
                    )
                    Text(
                        strings.monteCarloVarAt95,
                        style = LabelExtraSmall.copy(fontWeight = FontWeight.Bold),
                        color = LossRed
                    )
                }
                Spacer(modifier = Modifier.height(8.dp))

                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    Column {
                        Text(strings.monteCarloVar95, style = LabelTiny, color = MutedText)
                        Text(
                            "${String.format("%.2f", mc.var95Pct)}%",
                            style = MaterialTheme.typography.labelMedium
                                .copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                            color = LossRed
                        )
                    }
                    Column {
                        Text(strings.monteCarloVar99, style = LabelTiny, color = MutedText)
                        Text(
                            "${String.format("%.2f", mc.var99Pct)}%",
                            style = MaterialTheme.typography.labelMedium
                                .copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                            color = LossRed
                        )
                    }
                    Column {
                        Text(strings.monteCarloCvar, style = LabelTiny, color = MutedText)
                        Text(
                            "${String.format("%.2f", mc.expectedShortfall95Pct)}%",
                            style = MaterialTheme.typography.labelMedium
                                .copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                            color = GoldYellow
                        )
                    }
                    Column {
                        Text(strings.monteCarloMedianEquity, style = LabelTiny, color = MutedText)
                        Text(
                            "\$${String.format("%,.0f", mc.medianFinalEquity)}",
                            style = MaterialTheme.typography.labelMedium
                                .copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                            color = ProfitGreen
                        )
                    }
                }
            }
        }
    }
}
