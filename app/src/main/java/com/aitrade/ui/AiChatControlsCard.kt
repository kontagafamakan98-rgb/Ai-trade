package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
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
fun AiChatControlsCard(
    strings: AppStrings,
    isVip: Boolean,
    searchEnabled: Boolean,
    mapsEnabled: Boolean,
    highThinkingEnabled: Boolean,
    lowLatencyEnabled: Boolean,
    viewModel: TradingViewModel,
    showLockedDialogState: MutableState<Boolean>,
    lockedFeatureNameState: MutableState<String>,
) {
    var showLockedDialog by showLockedDialogState
    var lockedFeatureName by lockedFeatureNameState
    // 1. Controls Card
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            Text(
                strings.geminiParametersTitle,
                color = Color.White,
                style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
            )
            Spacer(modifier = Modifier.height(8.dp))

            // Search Grounding Toggle
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(strings.searchGroundingLabel, color = LightGrayText, style = MaterialTheme.typography.labelMedium)
                    Spacer(modifier = Modifier.width(6.dp))
                    Box(
                        modifier =
                            Modifier
                                .background(if (isVip) ProfitGreen else Color.DarkGray, RoundedCornerShape(4.dp))
                                .padding(horizontal = 4.dp, vertical = 2.dp),
                    ) {
                        Text(
                            if (isVip) strings.activeLabel else strings.vipProLabel,
                            color = Color.White,
                            style = LabelTiny.copy(fontWeight = FontWeight.Bold),
                        )
                    }
                }
                Switch(
                    checked = searchEnabled,
                    onCheckedChange = { checked ->
                        if (!isVip) {
                            lockedFeatureName = strings.chatSearchGroundingFull
                            showLockedDialog = true
                        } else {
                            viewModel.toggleSearchGrounding(checked)
                        }
                    },
                    colors = SwitchDefaults.colors(checkedThumbColor = CyberBlue),
                )
            }

            // Maps Grounding Toggle
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(strings.mapsLocatorLabel, color = LightGrayText, style = MaterialTheme.typography.labelMedium)
                    Spacer(modifier = Modifier.width(6.dp))
                    Box(
                        modifier =
                            Modifier
                                .background(if (isVip) ProfitGreen else Color.DarkGray, RoundedCornerShape(4.dp))
                                .padding(horizontal = 4.dp, vertical = 2.dp),
                    ) {
                        Text(
                            if (isVip) strings.activeLabel else strings.vipProLabel,
                            color = Color.White,
                            style = LabelTiny.copy(fontWeight = FontWeight.Bold),
                        )
                    }
                }
                Switch(
                    checked = mapsEnabled,
                    onCheckedChange = { checked ->
                        if (!isVip) {
                            lockedFeatureName = strings.chatMapsGroundingFull
                            showLockedDialog = true
                        } else {
                            viewModel.toggleMapsGrounding(checked)
                        }
                    },
                    colors = SwitchDefaults.colors(checkedThumbColor = CyberBlue),
                )
            }

            // High Thinking Toggle
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(strings.deepReasoningLabel, color = LightGrayText, style = MaterialTheme.typography.labelMedium)
                    Spacer(modifier = Modifier.width(6.dp))
                    Box(
                        modifier =
                            Modifier
                                .background(if (isVip) ProfitGreen else Color.DarkGray, RoundedCornerShape(4.dp))
                                .padding(horizontal = 4.dp, vertical = 2.dp),
                    ) {
                        Text(
                            if (isVip) strings.activeLabel else strings.vipProLabel,
                            color = Color.White,
                            style = LabelTiny.copy(fontWeight = FontWeight.Bold),
                        )
                    }
                }
                Switch(
                    checked = highThinkingEnabled,
                    onCheckedChange = { checked ->
                        if (!isVip) {
                            lockedFeatureName = strings.chatHighThinkingMode
                            showLockedDialog = true
                        } else {
                            viewModel.toggleHighThinking(checked)
                        }
                    },
                    colors = SwitchDefaults.colors(checkedThumbColor = CyberBlue),
                )
            }

            // Low Latency Toggle
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(strings.flashModeLabel, color = LightGrayText, style = MaterialTheme.typography.labelMedium)
                    Spacer(modifier = Modifier.width(6.dp))
                    Box(
                        modifier =
                            Modifier
                                .background(CyberBlue, RoundedCornerShape(4.dp))
                                .padding(horizontal = 4.dp, vertical = 2.dp),
                    ) {
                        Text(
                            strings.freeLabel,
                            color = Color.White,
                            style = LabelTiny.copy(fontWeight = FontWeight.Bold),
                        )
                    }
                }
                Switch(
                    checked = lowLatencyEnabled,
                    onCheckedChange = { viewModel.toggleLowLatency(it) },
                    colors = SwitchDefaults.colors(checkedThumbColor = CyberBlue),
                )
            }
        }
    }
}
