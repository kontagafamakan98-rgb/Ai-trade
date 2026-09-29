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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.data.Signal
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun FirestoreSyncCard(
    strings: AppStrings,
    signals: List<Signal>,
    isSynced: Boolean,
    isConnected: Boolean,
    viewModel: TradingViewModel,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Icon(Icons.Default.CloudQueue, contentDescription = null, tint = CyberBlue, modifier = Modifier.size(28.dp))
                    Text(
                        strings.firestoreDbSyncTitle,
                        color = Color.White,
                        style = MaterialTheme.typography.bodyLarge.copy(fontWeight = FontWeight.Bold),
                    )
                }
                Box(
                    modifier =
                        Modifier
                            .background(if (isSynced) ProfitGreen else Color.DarkGray, RoundedCornerShape(4.dp))
                            .padding(horizontal = 6.dp, vertical = 2.dp),
                ) {
                    Text(
                        if (isSynced) strings.firestoreSyncedLabel else strings.firestoreOutOfSyncLabel,
                        color = Color.White,
                        style = LabelTiny.copy(fontWeight = FontWeight.Bold),
                    )
                }
            }

            Spacer(modifier = Modifier.height(12.dp))
            Text(
                String.format(strings.firestoreSyncNote, signals.size),
                color = LightGrayText,
                style = MaterialTheme.typography.labelMedium,
            )

            Spacer(modifier = Modifier.height(16.dp))
            Button(
                shape = ButtonShape,
                onClick = { viewModel.triggerFirestoreSync() },
                colors = ButtonDefaults.buttonColors(containerColor = CyberBlue),
                enabled = isConnected,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(if (isSynced) strings.syncNowBtn else strings.firestoreConnectBtn, fontWeight = FontWeight.Bold)
            }
        }
    }
}
