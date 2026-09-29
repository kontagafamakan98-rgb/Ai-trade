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
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

// --- QUANT PERFORMANCE MEMORY & LEARNING REGISTRY CARD ---

@Composable
fun QuantMemoryLearningCard(quantMemory: QuantMemorySummary) {
    val strings = LocalAppStrings.current

    Card(
        modifier =
            Modifier
                .fillMaxWidth()
                .testTag("quant_memory_card"),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(
                strings.quantMemoryTitle,
                style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Bold),
                color = Color.White
            )
            Text(
                strings.quantMemorySubtitle,
                color = MutedText,
                style = MaterialTheme.typography.labelSmall,
            )

            Spacer(modifier = Modifier.height(10.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Text(
                    "${strings.bestRegime}: ${quantMemory.bestRegime}",
                    style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                    color = ProfitGreen
                )
                Text(
                    "${strings.aiAccuracy}: ${String.format("%.1f", quantMemory.aiModelAccuracyPct)}%",
                    style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                    color = GoldYellow
                )
            }

            Spacer(modifier = Modifier.height(8.dp))

            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                quantMemory.regimeBreakdown.forEach { reg ->
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                    ) {
                        Text(reg.regimeLabel, style = LabelExtraSmall, color = MutedText)
                        Text(
                            String.format(
                                strings.quantMemoryStats,
                                reg.totalExecuted,
                                String.format("%.0f", reg.winRatePct),
                                if (reg.totalPnL >= 0) "+" else "",
                                String.format("%.0f", reg.totalPnL),
                            ),
                            style = LabelExtraSmall.copy(fontFamily = FontFamily.Monospace),
                            color = if (reg.totalPnL >= 0) ProfitGreen else LossRed
                        )
                    }
                }
            }
        }
    }
}
