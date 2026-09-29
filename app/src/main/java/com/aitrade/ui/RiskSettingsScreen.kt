package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun RiskSettingsScreen(
    viewModel: TradingViewModel,
    prefs: com.aitrade.data.UserPreferences,
) {
    val strings = LocalAppStrings.current
    val currentEquity by viewModel.currentEquity.collectAsStateWithLifecycle()
    val startingBalance by viewModel.startingBalance.collectAsStateWithLifecycle()
    val credentials by viewModel.credentials.collectAsStateWithLifecycle()

    val currentDrawdown =
        if (startingBalance > 0) {
            ((startingBalance - currentEquity) / startingBalance) * 100.0
        } else {
            0.0
        }

    LazyColumn(
        modifier =
            Modifier
                .fillMaxSize()
                .padding(horizontal = 16.dp)
                .testTag("risk_list"),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        // 0. Language Selector Card
        item {
            LanguageSelectorCard(strings = strings, prefs = prefs, viewModel = viewModel)
        }

        // 0.5 Real Demo Account Management Card
        item {
            DemoAccountManagementCard(strings = strings, currentEquity = currentEquity, viewModel = viewModel)
        }

        // 1. Current Risk Status Card
        item {
            RiskStatusCard(
                strings = strings,
                prefs = prefs,
                currentEquity = currentEquity,
                currentDrawdown = currentDrawdown,
            )
        }

        // 2. Risk Guard Form Card
        item {
            RiskGuardFormCard(strings = strings, prefs = prefs, viewModel = viewModel)
        }

        // 3. Multi-Broker Integration Card
        item {
            BrokerIntegrationCard(strings = strings, prefs = prefs, viewModel = viewModel, credentials = credentials)
        }

        // 4. Reset Button Card
        item {
            ResetTradingDataCard(strings = strings, viewModel = viewModel)
        }
    }
}
