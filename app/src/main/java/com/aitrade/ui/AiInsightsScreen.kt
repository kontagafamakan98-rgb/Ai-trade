package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aitrade.data.MarketInsight
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.text.SimpleDateFormat
import java.util.*

@Composable
fun AiInsightsScreen(
    viewModel: TradingViewModel,
    prefs: com.aitrade.data.UserPreferences,
    insights: List<MarketInsight>,
) {
    val strings = LocalAppStrings.current
    val appLocale = rememberAppLocale()
    val aiState by viewModel.aiAnalysisState.collectAsStateWithLifecycle()
    var selectedAsset by remember { mutableStateOf("BTC") }
    val watchlist = prefs.getWatchlistList()

    LazyColumn(
        modifier =
            Modifier
                .fillMaxSize()
                .padding(horizontal = 16.dp)
                .testTag("insights_list"),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        // 1. Interactive Gemini Analysis Card
        item {
            Card(
                modifier = Modifier.fillMaxWidth(),
                colors = CardDefaults.cardColors(containerColor = DarkCard),
                border = BorderStroke(1.dp, BorderColor),
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text(
                        strings.insightsTitle,
                        style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                        color = Color.White
                    )
                    Text(
                        strings.aiChatSubtitle,
                        color = MutedText,
                        style = MaterialTheme.typography.labelMedium,
                    )
                    Spacer(modifier = Modifier.height(16.dp))

                    // Dropdown or horizontal selector for asset
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        watchlist.forEach { asset ->
                            OutlinedButton(
                                shape = ButtonShape,
                                onClick = { selectedAsset = asset },
                                colors =
                                    ButtonDefaults.outlinedButtonColors(
                                        containerColor = if (selectedAsset == asset) CyberBlue.copy(alpha = 0.1f) else Color.Transparent,
                                    ),
                                border =
                                    BorderStroke(
                                        1.dp,
                                        if (selectedAsset == asset) CyberBlue else BorderColor,
                                    ),
                                modifier = Modifier.weight(1f),
                            ) {
                                Text(asset, style = MaterialTheme.typography.labelSmall, color = Color.White)
                            }
                        }
                    }
                    Spacer(modifier = Modifier.height(16.dp))

                    Button(
                        shape = ButtonShape,
                        onClick = { viewModel.runAiAnalysis(selectedAsset) },
                        colors = ButtonDefaults.buttonColors(containerColor = CyberBlue),
                        modifier = Modifier.fillMaxWidth(),
                        enabled = aiState !is TradingViewModel.AiState.Loading,
                    ) {
                        Row(
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Icon(Icons.Default.AutoAwesome, contentDescription = null)
                            Text(strings.runGeminiAnalysis, fontWeight = FontWeight.Bold)
                        }
                    }

                    Spacer(modifier = Modifier.height(16.dp))

                    // Display AI State
                    AnimatedContent(targetState = aiState, label = "AI_STATE") { state ->
                        when (state) {
                            is TradingViewModel.AiState.Idle -> {
                                Text(
                                    strings.noInsightsYet,
                                    color = MutedText,
                                    style = MaterialTheme.typography.labelMedium,
                                    modifier = Modifier.fillMaxWidth()
                                )
                            }
                            is TradingViewModel.AiState.Loading -> {
                                Column(
                                    modifier = Modifier.fillMaxWidth(),
                                    horizontalAlignment = Alignment.CenterHorizontally,
                                ) {
                                    CircularProgressIndicator(color = CyberBlue, modifier = Modifier.size(24.dp))
                                    Spacer(modifier = Modifier.height(8.dp))
                                    Text(strings.scanningNews, color = LightGrayText, style = MaterialTheme.typography.labelMedium)
                                }
                            }
                            is TradingViewModel.AiState.Success -> {
                                Column(
                                    modifier =
                                        Modifier
                                            .fillMaxWidth()
                                            .background(CharcoalBackground, RoundedCornerShape(8.dp))
                                            .border(BorderStroke(1.dp, BorderColor), RoundedCornerShape(8.dp))
                                            .padding(12.dp),
                                ) {
                                    Row(
                                        modifier = Modifier.fillMaxWidth(),
                                        horizontalArrangement = Arrangement.SpaceBetween,
                                        verticalAlignment = Alignment.CenterVertically,
                                    ) {
                                        Text(
                                            "${strings.geminiBias}: ${if (state.score >= 0.58) {
                                                strings.bullishLabel
                                            } else if (state.score <= 0.42) {
                                                strings.bearishLabel
                                            } else {
                                                strings.neutralLabel
                                            }}",
                                            color =
                                                if (state.score >=
                                                    0.58
                                                ) {
                                                    ProfitGreen
                                                } else if (state.score <= 0.42) {
                                                    LossRed
                                                } else {
                                                    GoldYellow
                                                },
                                            style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Bold),
                                        )
                                        Text(
                                            "${strings.scoreLabel}: ${String.format("%.2f", state.score)}",
                                            style = MaterialTheme.typography.labelLarge
                                                .copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                                            color = CyberBlue
                                        )
                                    }
                                    Spacer(modifier = Modifier.height(8.dp))
                                    Text(
                                        state.reasoning,
                                        style = MaterialTheme.typography.labelMedium,
                                        color = Color.White
                                    )
                                }
                            }
                            is TradingViewModel.AiState.Error -> {
                                Text(
                                    String.format(strings.insightsErrorAnalysis, state.message),
                                    color = LossRed,
                                    style = MaterialTheme.typography.labelMedium,
                                )
                            }
                        }
                    }
                }
            }
        }

        // 2. Geopolitical Insights Stream
        item {
            Text(
                strings.insightsTitle,
                style = NumericTextStyle,
                color = MutedText
            )
        }

        if (insights.isEmpty()) {
            item {
                Card(
                    modifier = Modifier.fillMaxWidth(),
                    colors = CardDefaults.cardColors(containerColor = DarkCard),
                    border = BorderStroke(1.dp, BorderColor),
                ) {
                    Box(modifier = Modifier.padding(24.dp), contentAlignment = Alignment.Center) {
                        Text(strings.noInsightsYet, color = MutedText, style = MaterialTheme.typography.labelMedium)
                    }
                }
            }
        } else {
            items(insights) { insight ->
                Card(
                    modifier =
                        Modifier
                            .fillMaxWidth()
                            .padding(bottom = 8.dp),
                    colors = CardDefaults.cardColors(containerColor = DarkCard),
                    border = BorderStroke(1.dp, BorderColor),
                ) {
                    Column(modifier = Modifier.padding(14.dp)) {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Text(
                                insight.title,
                                color = Color.White,
                                style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Bold),
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                                modifier = Modifier.weight(0.7f)
                            )
                            Text(
                                if (insight.type == "geopolitical") strings.geopoliticalLabel else strings.sentimentLabel,
                                color = if (insight.type == "geopolitical") GoldYellow else CyberBlue,
                                style = LabelExtraSmall.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                                modifier = Modifier.weight(0.3f),
                                textAlign = TextAlign.End
                            )
                        }
                        Spacer(modifier = Modifier.height(6.dp))
                        Text(
                            insight.content,
                            style = MaterialTheme.typography.labelMedium,
                            color = LightGrayText
                        )
                        Spacer(modifier = Modifier.height(8.dp))
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Text(
                                "${strings.biasImpactLabel}: ${String.format("%.2f", insight.score)}",
                                style = MaterialTheme.typography.labelSmall.copy(fontFamily = FontFamily.Monospace),
                                color = MutedText
                            )
                            val date = SimpleDateFormat(strings.dateTimePatternShort, appLocale).format(Date(insight.timestamp))
                            Text(
                                date,
                                style = LabelExtraSmall,
                                color = MutedText
                            )
                        }
                    }
                }
            }
        }
    }
}
