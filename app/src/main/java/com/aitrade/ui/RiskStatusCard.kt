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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun RiskStatusCard(
    strings: AppStrings,
    prefs: com.aitrade.data.UserPreferences,
    currentEquity: Double,
    currentDrawdown: Double,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(
                strings.riskSettingsTitle,
                style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                color = Color.White
            )
            Spacer(modifier = Modifier.height(12.dp))
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Column {
                    Text(strings.currentEquityLabel, color = MutedText, style = MaterialTheme.typography.labelSmall)
                    Text(
                        "\$${String.format("%,.2f", currentEquity)}",
                        color = Color.White,
                        style = MaterialTheme.typography.bodyLarge.copy(fontWeight = FontWeight.Bold),
                    )
                }
                Column {
                    Text(strings.maxDrawdownLimitLabel, color = MutedText, style = MaterialTheme.typography.labelSmall)
                    Text(
                        "${prefs.maxTotalDrawdownPct}%",
                        color = LossRed,
                        style = MaterialTheme.typography.bodyLarge.copy(fontWeight = FontWeight.Bold),
                    )
                }
                Column {
                    Text(strings.currentDrawdownLabel, color = MutedText, style = MaterialTheme.typography.labelSmall)
                    Text(
                        "${String.format("%.2f", Math.max(0.0, currentDrawdown))}%",
                        color =
                            if (currentDrawdown >
                                5
                            ) {
                                LossRed
                            } else {
                                ProfitGreen
                            },
                        style = MaterialTheme.typography.bodyLarge.copy(fontWeight = FontWeight.Bold),
                    )
                }
            }
        }
    }
}
