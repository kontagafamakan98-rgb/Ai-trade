package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun BacktestTogglesRow(
    strings: AppStrings,
    backtestResult: BacktestResult,
    showMonteCarloState: MutableState<Boolean>,
    showTradeLogsState: MutableState<Boolean>,
) {
    var showMonteCarlo by showMonteCarloState
    var showTradeLogs by showTradeLogsState
    // Toggle Monte Carlo & Trade Log Buttons
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        OutlinedButton(
            shape = ButtonShape,
            onClick = { showMonteCarlo = !showMonteCarlo },
            modifier = Modifier.weight(1f),
            border = BorderStroke(1.dp, if (showMonteCarlo) CyberBlue else BorderColor),
            colors =
                ButtonDefaults.outlinedButtonColors(
                    containerColor = if (showMonteCarlo) CyberBlue.copy(alpha = 0.15f) else Color.Transparent,
                ),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                Icon(Icons.Default.Casino, contentDescription = null, modifier = Modifier.size(14.dp), tint = CyberBlue)
                Text(strings.backtestMonteCarloStress, style = LabelExtraSmall.copy(fontWeight = FontWeight.Bold), color = Color.White)
            }
        }

        OutlinedButton(
            shape = ButtonShape,
            onClick = { showTradeLogs = !showTradeLogs },
            modifier = Modifier.weight(1f),
            border = BorderStroke(1.dp, if (showTradeLogs) ProfitGreen else BorderColor),
            colors =
                ButtonDefaults.outlinedButtonColors(
                    containerColor = if (showTradeLogs) ProfitGreen.copy(alpha = 0.15f) else Color.Transparent,
                ),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                Icon(Icons.Default.ReceiptLong, contentDescription = null, modifier = Modifier.size(14.dp), tint = ProfitGreen)
                Text(
                    String.format(strings.backtestTradeLogs, backtestResult.tradeLogs.size),
                    style = LabelExtraSmall.copy(fontWeight = FontWeight.Bold),
                    color = Color.White,
                )
            }
        }
    }
}
