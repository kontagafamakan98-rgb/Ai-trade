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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun ResetTradingDataCard(
    strings: AppStrings,
    viewModel: TradingViewModel,
) {
    Card(
        modifier =
            Modifier
                .fillMaxWidth()
                .padding(bottom = 16.dp),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        Column(
            modifier =
                Modifier
                    .fillMaxWidth()
                    .padding(16.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                strings.dangerZone,
                color = LossRed,
                style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Bold),
            )
            Spacer(modifier = Modifier.height(8.dp))
            OutlinedButton(
                shape = ButtonShape,
                onClick = { viewModel.resetSimulation() },
                colors =
                    ButtonDefaults.outlinedButtonColors(
                        contentColor = LossRed,
                    ),
                border = BorderStroke(1.dp, LossRed),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Row(
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Icon(Icons.Default.Delete, contentDescription = null)
                    Text(strings.resetEntireTradingData, fontWeight = FontWeight.Bold)
                }
            }
        }
    }
}
