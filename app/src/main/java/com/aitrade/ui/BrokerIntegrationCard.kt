package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun BrokerIntegrationCard(
    strings: AppStrings,
    prefs: com.aitrade.data.UserPreferences,
    viewModel: TradingViewModel,
    credentials: com.aitrade.data.BrokerCredentials,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        val selectedBrokerState =
            remember(credentials, strings) {
                mutableStateOf(credentials.brokerName.ifEmpty { strings.brokerNameAlpaca })
            }
        var selectedBroker by selectedBrokerState
        val apiKeyState = remember(credentials) { mutableStateOf(credentials.apiKey) }
        var apiKey by apiKeyState
        val secretKeyState = remember(credentials) { mutableStateOf(credentials.apiSecret) }
        var secretKey by secretKeyState
        val paperModeState = remember(credentials) { mutableStateOf(credentials.paperMode) }
        var paperMode by paperModeState
        val accountNumberState = remember(credentials) { mutableStateOf(credentials.accountNumber) }
        var accountNumber by accountNumberState
        val customEndpointState = remember(credentials) { mutableStateOf(credentials.customEndpoint) }
        var customEndpoint by customEndpointState

        val supportedBrokers =
            listOf(
                strings.brokerNameAlpaca to strings.brokerDescStocksCrypto,
                strings.brokerNameIbkr to strings.brokerDescGlobalEquities,
                strings.brokerNameFtmo to strings.brokerDescPropFirm,
                strings.brokerNameIcmarkets to strings.brokerDescRawSpreads,
                strings.brokerNamePepperstone to strings.brokerDescStp,
                strings.brokerNameRoboforex to strings.brokerDescEcn,
                strings.brokerNameXm to strings.brokerDescMicro,
                strings.brokerNameExness to strings.brokerDescLeverage,
                strings.brokerNameFpmarkets to strings.brokerDescDma,
                strings.brokerNameAvatrade to strings.brokerDescFixedFloating,
                strings.brokerNameAdmirals to strings.brokerDescMultiAsset,
                strings.brokerNameOanda to strings.brokerDescForexCommodities,
                strings.brokerNameBinance to strings.brokerDescCryptoSpotDerivatives,
                strings.brokerNameCoinbase to strings.brokerDescCryptoSpotFutures,
                strings.brokerNameKraken to strings.brokerDescCryptoSpotFutures,
                strings.brokerNameCustom to strings.brokerDescProprietary,
            )

        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column {
                    Text(
                        strings.multiBrokerTitle,
                        style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                        color = Color.White
                    )
                    Text(
                        strings.multiBrokerSubtitle,
                        color = MutedText,
                        style = MaterialTheme.typography.labelMedium,
                    )
                }
            }

            Spacer(modifier = Modifier.height(14.dp))

            // Active Connected Status Banner
            BrokerStatusBanner(strings = strings, credentials = credentials)

            Spacer(modifier = Modifier.height(16.dp))

            Text(
                strings.selectBroker,
                color = LightGrayText,
                style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
            )
            Spacer(modifier = Modifier.height(8.dp))

            // Broker Chips Selector
            BrokerChannelsRow(supportedBrokers = supportedBrokers, selectedBroker = selectedBrokerState)

            Spacer(modifier = Modifier.height(16.dp))

            // Dynamic Fields according to selected broker
            Text(
                "${strings.configuringBroker} $selectedBroker",
                color = CyberBlue,
                style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
            )
            Spacer(modifier = Modifier.height(8.dp))

            BrokerCredentialFields(
                strings = strings,
                selectedBroker = selectedBroker,
                accountNumber = accountNumberState,
                apiKey = apiKeyState,
                secretKey = secretKeyState,
                customEndpoint = customEndpointState,
            )

            // Quick Demo Real Account connection section
            BrokerQuickConnectCard(
                viewModel = viewModel,
                selectedBrokerState = selectedBrokerState,
                apiKeyState = apiKeyState,
                secretKeyState = secretKeyState,
                paperModeState = paperModeState,
                accountNumberState = accountNumberState,
                customEndpointState = customEndpointState,
            )

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column {
                    Text(strings.simulationMode, color = LightGrayText, style = MaterialTheme.typography.labelLarge)
                    Text(
                        if (paperMode) strings.brokerExecutesSimulated else strings.liveModeWarning,
                        color = if (paperMode) MutedText else LossRed,
                        style = LabelExtraSmall,
                    )
                }
                Switch(
                    checked = paperMode,
                    onCheckedChange = { paperMode = it },
                    colors = SwitchDefaults.colors(checkedThumbColor = CyberBlue),
                )
            }
            Spacer(modifier = Modifier.height(16.dp))

            Button(
                shape = ButtonShape,
                onClick = {
                    viewModel.saveBrokerCredentials(
                        brokerName = selectedBroker,
                        apiKey = apiKey,
                        secretKey = secretKey,
                        paperMode = paperMode,
                        accountNumber = accountNumber,
                        customEndpoint = customEndpoint,
                    )
                },
                colors = ButtonDefaults.buttonColors(containerColor = CyberBlue),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(strings.saveBrokerConfiguration, fontWeight = FontWeight.Bold)
            }
        }
    }
}
