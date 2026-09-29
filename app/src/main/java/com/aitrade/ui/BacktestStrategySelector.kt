package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun BacktestStrategySelector(
    strings: AppStrings,
    onRunBacktest: (Int, TradingStrategy) -> Unit,
    selectedDaysState: MutableState<Int>,
    selectedStrategyState: MutableState<TradingStrategy>,
) {
    var selectedDays by selectedDaysState
    var selectedStrategy by selectedStrategyState
    // Strategy Selector Chips
    Text(
        strings.backtestStrategyMode,
        color = LightGrayText,
        style = MaterialTheme.typography.labelSmall.copy(fontFamily = FontFamily.Monospace),
    )
    Spacer(modifier = Modifier.height(6.dp))

    LazyRow(
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        modifier = Modifier.fillMaxWidth(),
    ) {
        items(TradingStrategy.entries.size) { index ->
            val strat = TradingStrategy.entries[index]
            val isSelected = selectedStrategy == strat
            Card(
                modifier =
                    Modifier.clickable {
                        selectedStrategy = strat
                        onRunBacktest(selectedDays, strat)
                    },
                colors =
                    CardDefaults.cardColors(
                        containerColor = if (isSelected) CyberBlue.copy(alpha = 0.2f) else DarkBlue,
                    ),
                border = BorderStroke(1.dp, if (isSelected) CyberBlue else BorderColor),
            ) {
                Column(modifier = Modifier.padding(horizontal = 10.dp, vertical = 6.dp)) {
                    Text(
                        strat.displayName,
                        style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                        color = if (isSelected) CyberBlue else Color.White
                    )
                    Text(
                        strat.description,
                        style = LabelTiny,
                        color = MutedText
                    )
                }
            }
        }
    }
}
