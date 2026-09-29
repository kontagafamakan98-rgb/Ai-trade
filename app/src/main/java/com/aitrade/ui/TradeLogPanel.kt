package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
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
fun TradeLogPanel(
    strings: AppStrings,
    backtestResult: BacktestResult,
    showTradeLogs: Boolean,
) {
    // Trade History Log Panel
    if (showTradeLogs && backtestResult.tradeLogs.isNotEmpty()) {
        Spacer(modifier = Modifier.height(10.dp))
        Text(
            strings.tradeLogHistory,
            style = MaterialTheme.typography.labelSmall.copy(fontFamily = FontFamily.Monospace),
            color = LightGrayText
        )
        Spacer(modifier = Modifier.height(6.dp))

        Column(
            modifier =
                Modifier
                    .fillMaxWidth()
                    .heightIn(max = 200.dp)
                    .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            backtestResult.tradeLogs.take(30).forEach { log ->
                Row(
                    modifier =
                        Modifier
                            .fillMaxWidth()
                            .background(DarkBlue.copy(alpha = 0.4f), RoundedCornerShape(4.dp))
                            .padding(horizontal = 8.dp, vertical = 6.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Box(
                            modifier =
                                Modifier
                                    .background(if (log.direction == "BUY") ProfitGreen else LossRed, RoundedCornerShape(2.dp))
                                    .padding(horizontal = 4.dp, vertical = 1.dp),
                        ) {
                            Text(
                                directionLabel(strings, log.direction),
                                style = LabelMicro.copy(fontWeight = FontWeight.Bold),
                                color = Color.White,
                            )
                        }
                        Text(log.asset, style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold), color = Color.White)
                        Text(String.format(strings.tradeLogEntry, log.entryPrice), style = LabelTiny, color = MutedText)
                    }

                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        Text(log.regime, style = LabelTiny, color = LightGrayText)
                        Text(
                            "${if (log.pnl >= 0) "+" else ""}\$${log.pnl}",
                            style = MaterialTheme.typography.labelSmall
                                .copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                            color = if (log.pnl >= 0) ProfitGreen else LossRed
                        )
                    }
                }
            }
        }
    }
}
