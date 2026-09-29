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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.data.Signal
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.text.SimpleDateFormat
import java.util.*

// --- PORTFOLIO PERFORMANCE & EQUITY CURVE VISUALIZER ---

data class EquityPoint(
    val index: Int,
    val equity: Double,
    val timestamp: Long,
    val tradeInfo: String? = null,
    val pnl: Double = 0.0,
)

@Composable
fun PortfolioPerformanceChartCard(
    signals: List<Signal>,
    currentEquity: Double,
    startingBalance: Double = 100000.0,
) {
    val strings = LocalAppStrings.current
    val appLocale = rememberAppLocale()
    var selectedTimeframe by remember { mutableStateOf("ALL") }
    // Codes de période : valeurs stables pour le filtrage, libellés localisés.
    val timeframes =
        listOf(
            "ALL" to strings.portfolioFilterAll,
            "1W" to strings.portfolioFilter1w,
            "1M" to strings.portfolioFilter1m,
            "YTD" to strings.portfolioFilterYtd,
        )

    // Process equity points history from signals
    val points =
        remember(signals, currentEquity, startingBalance, selectedTimeframe, strings) {
            val sortedSignals =
                signals
                    .filter { it.status == "CLOSED" || it.status == "EXECUTED" }
                    .sortedBy { it.timestamp }

            val now = System.currentTimeMillis()
            val dayMs = 86400000L
            val filteredSignals =
                when (selectedTimeframe) {
                    "1W" -> sortedSignals.filter { (now - it.timestamp) <= 7 * dayMs }
                    "1M" -> sortedSignals.filter { (now - it.timestamp) <= 30 * dayMs }
                    "YTD" -> sortedSignals.filter { (now - it.timestamp) <= 365 * dayMs }
                    else -> sortedSignals
                }

            val list = mutableListOf<EquityPoint>()
            var runEquity = startingBalance
            val baseTime = if (filteredSignals.isNotEmpty()) filteredSignals.first().timestamp - dayMs else now - (7 * dayMs)

            list.add(EquityPoint(0, startingBalance, baseTime, strings.portfolioStartingCapital, 0.0))

            filteredSignals.forEachIndexed { idx, sig ->
                runEquity += sig.pnl
                // Sens localisé, puis libellé composé pour rester sous la
                // limite de 140 colonnes.
                val direction = directionLabel(strings, sig.direction)
                val signedPnl = if (sig.pnl >= 0) "+" else ""
                val tradeLabel = "$direction ${sig.asset} ($signedPnl\$${String.format("%.2f", sig.pnl)})"
                list.add(EquityPoint(idx + 1, runEquity, sig.timestamp, tradeLabel, sig.pnl))
            }

            if (list.size == 1 || (list.last().equity != currentEquity && filteredSignals.size == sortedSignals.size)) {
                list.add(EquityPoint(list.size, currentEquity, now, strings.portfolioCurrentEquity, currentEquity - runEquity))
            }

            list
        }

    val totalPnL = currentEquity - startingBalance
    val pctReturn = if (startingBalance > 0) (totalPnL / startingBalance) * 100 else 0.0
    val closedTrades = signals.filter { it.status == "CLOSED" || it.pnl != 0.0 }
    val winTrades = closedTrades.filter { it.pnl > 0 }
    val winRateVal = if (closedTrades.isNotEmpty()) (winTrades.size.toDouble() / closedTrades.size) * 100 else 0.0

    var touchedPointIndex by remember { mutableStateOf<Int?>(null) }

    Card(
        modifier =
            Modifier
                .fillMaxWidth()
                .testTag("portfolio_performance_card"),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            // Header & Timeframe Chips
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(modifier = Modifier.weight(1f)) {
                    Text(
                        strings.portfolioPerformanceTitle,
                        style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                        color = Color.White
                    )
                    Text(
                        strings.portfolioPerformanceSubtitle,
                        color = MutedText,
                        style = MaterialTheme.typography.labelSmall,
                    )
                }

                // Timeframe Chips
                Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    timeframes.forEach { (tf, label) ->
                        val isSel = selectedTimeframe == tf
                        Box(
                            modifier =
                                Modifier
                                    .clip(RoundedCornerShape(6.dp))
                                    .background(if (isSel) CyberBlue else DarkBlue)
                                    .clickable { selectedTimeframe = tf }
                                    .padding(horizontal = 8.dp, vertical = 4.dp),
                        ) {
                            Text(
                                label,
                                style = LabelExtraSmall,
                                fontWeight = if (isSel) FontWeight.Bold else FontWeight.Normal,
                                color = if (isSel) Color.Black else MutedText
                            )
                        }
                    }
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            // Metrics Row
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                // Total Equity
                Card(
                    modifier = Modifier.weight(1f),
                    colors = CardDefaults.cardColors(containerColor = DarkBlue),
                    border = BorderStroke(1.dp, BorderColor),
                ) {
                    Column(modifier = Modifier.padding(10.dp)) {
                        Text(strings.totalEquity, style = LabelExtraSmall, color = MutedText)
                        Spacer(modifier = Modifier.height(2.dp))
                        Text(
                            "\$${String.format("%,.2f", currentEquity)}",
                            style = MaterialTheme.typography.labelLarge
                                .copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                            color = Color.White
                        )
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Icon(
                                if (pctReturn >= 0) Icons.Default.TrendingUp else Icons.Default.TrendingDown,
                                contentDescription = null,
                                tint = if (pctReturn >= 0) ProfitGreen else LossRed,
                                modifier = Modifier.size(12.dp),
                            )
                            Spacer(modifier = Modifier.width(2.dp))
                            Text(
                                "${if (pctReturn >= 0) "+" else ""}${String.format("%.2f", pctReturn)}%",
                                style = LabelExtraSmall.copy(fontWeight = FontWeight.Bold),
                                color = if (pctReturn >= 0) ProfitGreen else LossRed
                            )
                        }
                    }
                }

                // Realized PnL
                Card(
                    modifier = Modifier.weight(1f),
                    colors = CardDefaults.cardColors(containerColor = DarkBlue),
                    border = BorderStroke(1.dp, BorderColor),
                ) {
                    Column(modifier = Modifier.padding(10.dp)) {
                        Text(strings.realizedPnl, style = LabelExtraSmall, color = MutedText)
                        Spacer(modifier = Modifier.height(2.dp))
                        Text(
                            "${if (totalPnL >= 0) "+" else ""}\$${String.format("%,.2f", totalPnL)}",
                            style = MaterialTheme.typography.labelLarge
                                .copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                            color = if (totalPnL >= 0) ProfitGreen else LossRed
                        )
                        Text(
                            String.format(strings.portfolioVsBaseline, String.format("%,.0f", startingBalance)),
                            style = LabelTiny,
                            color = MutedText
                        )
                    }
                }

                // Win Rate
                Card(
                    modifier = Modifier.weight(1f),
                    colors = CardDefaults.cardColors(containerColor = DarkBlue),
                    border = BorderStroke(1.dp, BorderColor),
                ) {
                    Column(modifier = Modifier.padding(10.dp)) {
                        Text(strings.winRate, style = LabelExtraSmall, color = MutedText)
                        Spacer(modifier = Modifier.height(2.dp))
                        Text(
                            "${String.format("%.1f", winRateVal)}%",
                            style = MaterialTheme.typography.labelLarge
                                .copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                            color = GoldYellow
                        )
                        Text(
                            "${winTrades.size}/${closedTrades.size} ${strings.totalTrades}",
                            style = LabelTiny,
                            color = MutedText
                        )
                    }
                }
            }

            Spacer(modifier = Modifier.height(16.dp))

            // Interactive Recharts-Style Chart Canvas
            Box(
                modifier =
                    Modifier
                        .fillMaxWidth()
                        .height(210.dp),
            ) {
                PortfolioEquityCanvas(
                    points = points,
                    startingBalance = startingBalance,
                    touchedIndex = touchedPointIndex,
                    onPointTouched = { idx -> touchedPointIndex = idx },
                )
            }

            // Interactive Tooltip Info Panel
            touchedPointIndex?.let { idx ->
                val p = points.getOrNull(idx)
                if (p != null) {
                    Spacer(modifier = Modifier.height(10.dp))
                    Card(
                        modifier = Modifier.fillMaxWidth(),
                        colors = CardDefaults.cardColors(containerColor = CharcoalBackground),
                        border = BorderStroke(1.dp, CyberBlue),
                    ) {
                        Row(
                            modifier =
                                Modifier
                                    .fillMaxWidth()
                                    .padding(10.dp),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Column {
                                Text(
                                    p.tradeInfo ?: String.format(strings.portfolioPoint, p.index),
                                    style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
                                    color = Color.White
                                )
                                val dateStr = SimpleDateFormat(strings.dateTimePatternShort, appLocale).format(Date(p.timestamp))
                                Text(dateStr, style = LabelExtraSmall, color = MutedText)
                            }
                            Column(horizontalAlignment = Alignment.End) {
                                Text(
                                    "\$${String.format("%,.2f", p.equity)}",
                                    style = MaterialTheme.typography.labelLarge
                                        .copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                                    color = ProfitGreen
                                )
                                if (p.pnl != 0.0) {
                                    Text(
                                        String.format(
                                            strings.portfolioPnl,
                                            if (p.pnl >= 0) "+" else "",
                                            String.format("%.2f", p.pnl),
                                        ),
                                        style = LabelExtraSmall.copy(fontWeight = FontWeight.Bold),
                                        color = if (p.pnl >= 0) ProfitGreen else LossRed
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
