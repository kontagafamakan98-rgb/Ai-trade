package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.aitrade.data.LogEvent
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.text.SimpleDateFormat
import java.util.*

@Composable
fun LiveLogFeedCard(
    strings: AppStrings,
    logs: List<LogEvent>,
) {
    Card(
        modifier =
            Modifier
                .fillMaxWidth()
                .height(180.dp),
        colors = CardDefaults.cardColors(containerColor = CharcoalBackground),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        if (logs.isEmpty()) {
            Box(modifier = Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                Text(strings.noLogsYet, color = MutedText, style = MaterialTheme.typography.labelMedium)
            }
        } else {
            LazyColumn(
                modifier =
                    Modifier
                        .fillMaxSize()
                        .padding(8.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                items(logs) { log ->
                    val color =
                        when (log.type) {
                            "TRADE" -> ProfitGreen
                            "RISK" -> GoldYellow
                            "ERROR" -> LossRed
                            else -> LightGrayText
                        }
                    val dateStr = SimpleDateFormat("HH:mm:ss", Locale.getDefault()).format(Date(log.timestamp))
                    Text(
                        "[$dateStr] ${log.message}",
                        style = MaterialTheme.typography.labelSmall.copy(fontFamily = FontFamily.Monospace),
                        color = color
                    )
                }
            }
        }
    }
}
