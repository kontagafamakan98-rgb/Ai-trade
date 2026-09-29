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
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.R
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun BrokerQuickConnectCard(
    viewModel: TradingViewModel,
    selectedBrokerState: MutableState<String>,
    apiKeyState: MutableState<String>,
    secretKeyState: MutableState<String>,
    paperModeState: MutableState<Boolean>,
    accountNumberState: MutableState<String>,
    customEndpointState: MutableState<String>,
) {
    val strings = LocalAppStrings.current
    var selectedBroker by selectedBrokerState
    var apiKey by apiKeyState
    var secretKey by secretKeyState
    var paperMode by paperModeState
    var accountNumber by accountNumberState
    var customEndpoint by customEndpointState
    val quickConnectDemoLabel = stringResource(R.string.quick_connect_demo_label)
    val quickConnectDemoSubtitle = stringResource(R.string.quick_connect_demo_subtitle)

    Card(
        modifier =
            Modifier
                .fillMaxWidth()
                .padding(vertical = 12.dp),
        colors = CardDefaults.cardColors(containerColor = CyberBlue.copy(alpha = 0.08f)),
        border = BorderStroke(1.dp, CyberBlue.copy(alpha = 0.4f)),
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            Text(
                quickConnectDemoLabel,
                color = CyberBlue,
                style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
            )
            Spacer(modifier = Modifier.height(2.dp))
            Text(
                quickConnectDemoSubtitle,
                color = LightGrayText,
                style = MaterialTheme.typography.labelSmall,
            )
            Spacer(modifier = Modifier.height(10.dp))

            OutlinedButton(
                onClick = {
                    selectedBroker = strings.brokerNameAlpaca
                    apiKey = ""
                    secretKey = ""
                    paperMode = true
                    accountNumber = "PA-DEMO-8942"
                    customEndpoint = ""

                    viewModel.saveBrokerCredentials(
                        brokerName = strings.brokerNameAlpaca,
                        apiKey = "",
                        secretKey = "",
                        paperMode = true,
                        accountNumber = "PA-DEMO-8942",
                        customEndpoint = "",
                    )
                },
                border = BorderStroke(1.dp, CyberBlue),
                colors = ButtonDefaults.outlinedButtonColors(contentColor = CyberBlue),
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(8.dp),
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    Icon(
                        imageVector = Icons.Default.CheckCircle,
                        contentDescription = strings.brokerQuickConnect,
                        modifier = Modifier.size(16.dp),
                        tint = CyberBlue,
                    )
                    Text(
                        quickConnectDemoLabel,
                        style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
                    )
                }
            }
        }
    }
}
