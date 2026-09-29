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
fun AutoLoopControlCard(
    strings: AppStrings,
    isLoopRunning: Boolean,
    viewModel: TradingViewModel,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        Row(
            modifier =
                Modifier
                    .fillMaxWidth()
                    .padding(16.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column {
                Text(
                    strings.appTitle,
                    style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                    color = Color.White
                )
                Text(
                    if (isLoopRunning) strings.engineActive else strings.engineStopped,
                    color = if (isLoopRunning) ProfitGreen else MutedText,
                    style = MaterialTheme.typography.labelMedium,
                )
            }
            Button(
                shape = ButtonShape,
                onClick = { viewModel.toggleAutoLoop() },
                colors =
                    ButtonDefaults.buttonColors(
                        containerColor = if (isLoopRunning) LossRed else CyberBlue,
                    ),
            ) {
                Row(
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Icon(
                        if (isLoopRunning) Icons.Default.Pause else Icons.Default.PlayArrow,
                        contentDescription = null,
                    )
                    Text(if (isLoopRunning) strings.stopBot else strings.startBot, fontWeight = FontWeight.Bold)
                }
            }
        }
    }
}
