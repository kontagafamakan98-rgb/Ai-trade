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
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

// --- MULTI-AGENT RESEARCH DESK CARD ---
@Composable
fun MultiAgentResearchHubCard(
    report: MultiAgentResearchReport?,
    isResearching: Boolean,
    onRunResearch: (String) -> Unit,
    onInjectSignal: (MultiAgentResearchReport) -> Unit,
) {
    val watchlist = listOf("BTC", "ETH", "NVDA", "AAPL", "TSLA", "MSFT", "GOOGL")
    val selectedAssetState = remember { mutableStateOf("BTC") }

    Card(
        modifier =
            Modifier
                .fillMaxWidth()
                .testTag("multi_agent_research_card"),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, CyberBlue),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            MultiAgentControlPanel(
                selectedAssetState = selectedAssetState,
                watchlist = watchlist,
                isResearching = isResearching,
                onRunResearch = onRunResearch,
            )

            MultiAgentReportPanel(report = report, onInjectSignal = onInjectSignal)
        }
    }
}
