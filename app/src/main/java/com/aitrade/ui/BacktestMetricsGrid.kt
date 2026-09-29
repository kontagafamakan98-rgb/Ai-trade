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
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun BacktestMetricsGrid(
    strings: AppStrings,
    backtestResult: BacktestResult,
) {
    // Performance Grid (Return, Benchmark, Alpha, Beta)
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        Card(
            modifier = Modifier.weight(1f),
            colors = CardDefaults.cardColors(containerColor = DarkBlue),
            border = BorderStroke(1.dp, BorderColor),
        ) {
            Column(modifier = Modifier.padding(8.dp)) {
                Text(strings.strategyReturn, style = LabelTiny, color = MutedText)
                Text(
                    "${if (backtestResult.totalReturnPct >= 0) "+" else ""}${String.format("%.2f", backtestResult.totalReturnPct)}%",
                    style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                    color = if (backtestResult.totalReturnPct >= 0) ProfitGreen else LossRed
                )
            }
        }

        Card(
            modifier = Modifier.weight(1f),
            colors = CardDefaults.cardColors(containerColor = DarkBlue),
            border = BorderStroke(1.dp, BorderColor),
        ) {
            Column(modifier = Modifier.padding(8.dp)) {
                Text(strings.backtestBtcBenchmark, style = LabelTiny, color = MutedText)
                Text(
                    "${if (backtestResult.benchmarkReturnPct >= 0) "+" else ""}${String.format(
                        "%.2f",
                        backtestResult.benchmarkReturnPct
                    )}%",
                    style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                    color = Color.White
                )
            }
        }

        Card(
            modifier = Modifier.weight(1f),
            colors = CardDefaults.cardColors(containerColor = DarkBlue),
            border = BorderStroke(1.dp, BorderColor),
        ) {
            Column(modifier = Modifier.padding(8.dp)) {
                Text(strings.backtestAlpha, style = LabelTiny, color = MutedText)
                Text(
                    "${if (backtestResult.alphaPct >= 0) "+" else ""}${String.format("%.2f", backtestResult.alphaPct)}%",
                    style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                    color = GoldYellow
                )
            }
        }

        Card(
            modifier = Modifier.weight(1f),
            colors = CardDefaults.cardColors(containerColor = DarkBlue),
            border = BorderStroke(1.dp, BorderColor),
        ) {
            Column(modifier = Modifier.padding(8.dp)) {
                Text(strings.backtestBeta, style = LabelTiny, color = MutedText)
                Text(
                    String.format("%.2f", backtestResult.beta),
                    style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                    color = CyberBlue
                )
            }
        }
    }

    Spacer(modifier = Modifier.height(8.dp))

    // Risk Ratios Row (Sharpe, Sortino, Calmar, Win Rate, Max DD)
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column {
            Text(strings.backtestSharpe, style = LabelTiny, color = MutedText)
            Text(
                String.format("%.2f", backtestResult.sharpeRatio),
                style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
                color = GoldYellow,
            )
        }
        Column {
            Text(strings.backtestSortino, style = LabelTiny, color = MutedText)
            Text(
                String.format("%.2f", backtestResult.sortinoRatio),
                style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
                color = ProfitGreen,
            )
        }
        Column {
            Text(strings.backtestCalmar, style = LabelTiny, color = MutedText)
            Text(
                String.format("%.2f", backtestResult.calmarRatio),
                style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
                color = CyberBlue,
            )
        }
        Column {
            Text(strings.winRate, style = LabelTiny, color = MutedText)
            Text(
                "${String.format("%.1f", backtestResult.winRatePct)}%",
                style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
                color = ProfitGreen
            )
        }
        Column {
            Text(strings.backtestMaxDd, style = LabelTiny, color = MutedText)
            Text(
                "-${String.format("%.1f", backtestResult.maxDrawdownPct)}%",
                style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
                color = LossRed
            )
        }
    }

    Spacer(modifier = Modifier.height(12.dp))
}
