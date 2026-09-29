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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun BrokerChannelsRow(
    supportedBrokers: List<Pair<String, String>>,
    selectedBrokerState: MutableState<String>,
) {
    var selectedBroker by selectedBrokerState
    LazyRow(
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        modifier = Modifier.fillMaxWidth(),
    ) {
        items(supportedBrokers.size) { index ->
            val (broker, assetType) = supportedBrokers[index]
            val isSelected = selectedBroker == broker
            Card(
                modifier =
                    Modifier.clickable {
                        selectedBroker = broker
                    },
                colors =
                    CardDefaults.cardColors(
                        containerColor = if (isSelected) CyberBlue.copy(alpha = 0.2f) else DarkCard,
                    ),
                border =
                    BorderStroke(
                        1.dp,
                        if (isSelected) CyberBlue else BorderColor,
                    ),
            ) {
                Column(modifier = Modifier.padding(horizontal = 10.dp, vertical = 8.dp)) {
                    Text(
                        broker,
                        style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
                        color = if (isSelected) CyberBlue else Color.White
                    )
                    Text(
                        assetType,
                        style = LabelExtraSmall,
                        color = MutedText
                    )
                }
            }
        }
    }
}
