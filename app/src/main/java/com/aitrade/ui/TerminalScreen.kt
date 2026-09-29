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
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aitrade.data.LogEvent
import com.aitrade.data.Signal
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import kotlinx.coroutines.delay
import java.util.*

@Composable
fun TerminalScreen(
    viewModel: TradingViewModel,
    prefs: com.aitrade.data.UserPreferences,
    signals: List<Signal>,
    logs: List<LogEvent>,
    onOpenAdmin: () -> Unit = {},
) {
    val strings = LocalAppStrings.current
    val isLoopRunning by viewModel.isLoopRunning.collectAsStateWithLifecycle()
    val currentEquity by viewModel.currentEquity.collectAsStateWithLifecycle()
    val startingBalance by viewModel.startingBalance.collectAsStateWithLifecycle()
    val backtestResult by viewModel.backtestResult.collectAsStateWithLifecycle()
    val systemHealth by viewModel.systemHealth.collectAsStateWithLifecycle()
    val quantMemory by viewModel.quantMemory.collectAsStateWithLifecycle()
    val credentials by viewModel.credentials.collectAsStateWithLifecycle()
    val adaptiveProfile by viewModel.adaptiveLearningProfile.collectAsStateWithLifecycle()
    val selectedAssetState = remember { mutableStateOf("BTC") }

    // Auto-update price charts periodically
    var priceTickTrigger by remember { mutableStateOf(0) }
    LaunchedEffect(isLoopRunning) {
        if (isLoopRunning) {
            while (true) {
                delay(3000)
                priceTickTrigger++
            }
        }
    }

    LazyColumn(
        modifier =
            Modifier
                .fillMaxSize()
                .padding(horizontal = 16.dp)
                .testTag("terminal_list"),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        if (credentials.apiKey.isEmpty()) {
            item {
                BrokerConnectionBanner(viewModel = viewModel)
            }
        }

        // 0-a. Execution Mode Control Banner (Sandbox vs Live Real Broker)
        item {
            ExecutionModeControlCard(
                credentials = credentials,
                onToggleLive = { isLive -> viewModel.toggleLiveRealTrading(isLive) },
            )
        }

        // 0. Portfolio Performance & Equity Curve Visualizer
        item {
            PortfolioPerformanceChartCard(
                signals = signals,
                currentEquity = currentEquity,
                startingBalance = startingBalance,
            )
        }

        // 0b. Historical Backtest & Walk-Forward Validation Engine
        item {
            QuantBacktestValidationCard(
                backtestResult = backtestResult,
                onRunBacktest = { days, strat -> viewModel.runBacktest(days, strat) },
            )
        }

        // 0b2. Multi-Agent Research Desk
        item {
            val multiAgentReport by viewModel.multiAgentReport.collectAsStateWithLifecycle()
            val isMultiAgentResearching by viewModel.isMultiAgentResearching.collectAsStateWithLifecycle()
            MultiAgentResearchHubCard(
                report = multiAgentReport,
                isResearching = isMultiAgentResearching,
                onRunResearch = { asset -> viewModel.runMultiAgentResearch(asset) },
                onInjectSignal = { report -> viewModel.injectMultiAgentSignalIntoEngine(report) },
            )
        }

        // 0c. System Health, Observability & Circuit Breaker Guard
        item {
            SystemHealthObservabilityCard(
                healthState = systemHealth,
                onResetCircuitBreaker = { viewModel.resetCircuitBreaker() },
                onOpenAdmin = onOpenAdmin,
            )
        }

        // 0d. Quantitative Memory & Learning Registry
        quantMemory?.let { mem ->
            item {
                QuantMemoryLearningCard(quantMemory = mem)
            }
        }

        // 0e. Adaptive AI Self-Learning & Auto-Correction Panel
        item {
            AdaptiveLearningCard(
                profile = adaptiveProfile,
                onTriggerReview = { viewModel.triggerSelfLearningReview() },
            )
        }

        // 1. Loop Controls Card
        item {
            AutoLoopControlCard(strings = strings, isLoopRunning = isLoopRunning, viewModel = viewModel)
        }

        // 2. Watchlist Grid
        item {
            SectionHeader(strings.watchlistTitle)
        }

        item {
            WatchlistSelectorRow(prefs = prefs, selectedAsset = selectedAssetState)
        }

        // 3. Price History Chart
        item {
            PriceHistoryChartCard(
                strings = strings,
                selectedAsset = selectedAssetState.value,
                priceTickTrigger = priceTickTrigger,
            )
        }

        // 4. Live Log Feed
        item {
            SectionHeader(strings.logTerminalTitle)
        }

        item {
            LiveLogFeedCard(strings = strings, logs = logs)
        }

        // 5. Signals Table Header
        item {
            SectionHeader(strings.signalsTitle)
        }

        // 6. Signals List
        if (signals.isEmpty()) {
            item {
                AppCard(modifier = Modifier.padding(bottom = Spacing.lg)) {
                    Box(
                        modifier =
                            Modifier
                                .fillMaxWidth()
                                .padding(vertical = Spacing.lg),
                        contentAlignment = Alignment.Center,
                    ) {
                        Text(
                            strings.noSignalsYet,
                            style = MaterialTheme.typography.bodySmall,
                            color = MutedText,
                        )
                    }
                }
            }
        } else {
            items(signals) { signal ->
                SignalItemCard(signal)
            }
        }
    }
}
