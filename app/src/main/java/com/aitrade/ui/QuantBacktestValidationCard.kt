package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

// --- QUANT BACKTEST & WALK-FORWARD VALIDATION CARD ---
@Composable
fun QuantBacktestValidationCard(
    backtestResult: BacktestResult?,
    onRunBacktest: (Int, TradingStrategy) -> Unit,
) {
    val strings = LocalAppStrings.current
    val selectedDaysState = remember { mutableStateOf(365) }
    val selectedStrategyState = remember { mutableStateOf(TradingStrategy.MULTI_FACTOR_COMPOSITE) }
    val showTradeLogsState = remember { mutableStateOf(false) }
    var showTradeLogs by showTradeLogsState
    val showMonteCarloState = remember { mutableStateOf(false) }
    var showMonteCarlo by showMonteCarloState

    Card(
        modifier =
            Modifier
                .fillMaxWidth()
                .testTag("quant_backtest_card"),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, CyberBlue.copy(alpha = 0.5f)),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            BacktestHeaderRow(
                strings = strings,
                onRunBacktest = onRunBacktest,
                selectedDaysState = selectedDaysState,
                selectedStrategyState = selectedStrategyState,
            )

            Spacer(modifier = Modifier.height(12.dp))

            BacktestStrategySelector(
                strings = strings,
                onRunBacktest = onRunBacktest,
                selectedDaysState = selectedDaysState,
                selectedStrategyState = selectedStrategyState,
            )

            Spacer(modifier = Modifier.height(14.dp))

            if (backtestResult != null) {
                BacktestMetricsGrid(strings = strings, backtestResult = backtestResult)

                BacktestTogglesRow(
                    strings = strings,
                    backtestResult = backtestResult,
                    showMonteCarloState = showMonteCarloState,
                    showTradeLogsState = showTradeLogsState,
                )

                MonteCarloPanel(strings = strings, backtestResult = backtestResult, showMonteCarlo = showMonteCarlo)

                TradeLogPanel(strings = strings, backtestResult = backtestResult, showTradeLogs = showTradeLogs)
            }
        }
    }
}
