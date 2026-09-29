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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun BacktestHeaderRow(
    strings: AppStrings,
    onRunBacktest: (Int, TradingStrategy) -> Unit,
    selectedDaysState: MutableState<Int>,
    selectedStrategyState: MutableState<TradingStrategy>,
) {
    var selectedDays by selectedDaysState
    var selectedStrategy by selectedStrategyState
    // Header with title
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(modifier = Modifier.weight(1f)) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                Icon(Icons.Default.Analytics, contentDescription = null, tint = CyberBlue, modifier = Modifier.size(20.dp))
                Text(
                    strings.quantBacktestTitle,
                    style = MaterialTheme.typography.bodyLarge.copy(fontWeight = FontWeight.Bold),
                    color = Color.White
                )
            }
            Text(
                strings.quantBacktestSubtitle,
                color = MutedText,
                style = MaterialTheme.typography.labelSmall,
            )
        }

        // Timeframe Chips
        Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            listOf(30, 90, 365, 1095).forEach { days ->
                val isSel = selectedDays == days
                val label = if (days == 1095) "3y" else "${days}d"
                Box(
                    modifier =
                        Modifier
                            .clip(RoundedCornerShape(6.dp))
                            .background(if (isSel) CyberBlue else DarkBlue)
                            .clickable {
                                selectedDays = days
                                onRunBacktest(days, selectedStrategy)
                            }.padding(horizontal = 8.dp, vertical = 4.dp),
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
}
