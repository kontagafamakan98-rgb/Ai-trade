package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun AiChatAndScanScreen(viewModel: TradingViewModel) {
    val strings = LocalAppStrings.current
    val chatMessages by viewModel.chatMessages.collectAsStateWithLifecycle()
    val isVip by viewModel.isVipUser.collectAsStateWithLifecycle()
    val searchEnabled by viewModel.searchGroundingEnabled.collectAsStateWithLifecycle()
    val mapsEnabled by viewModel.mapsGroundingEnabled.collectAsStateWithLifecycle()
    val highThinkingEnabled by viewModel.highThinkingEnabled.collectAsStateWithLifecycle()
    val lowLatencyEnabled by viewModel.lowLatencyEnabled.collectAsStateWithLifecycle()
    val isChatLoading by viewModel.isChatLoading.collectAsStateWithLifecycle()

    val inputMessageState = remember { mutableStateOf("") }
    val selectedPatternNameState = remember { mutableStateOf<String?>(null) }
    var selectedPatternName by selectedPatternNameState
    val selectedPatternBase64State = remember { mutableStateOf<String?>(null) }
    var selectedPatternBase64 by selectedPatternBase64State
    val showLockedDialogState = remember { mutableStateOf(false) }
    var showLockedDialog by showLockedDialogState
    val lockedFeatureNameState = remember { mutableStateOf("") }
    var lockedFeatureName by lockedFeatureNameState

    val scrollState = rememberLazyListState()

    // Auto-scroll chat to bottom
    LaunchedEffect(chatMessages.size, isChatLoading) {
        if (chatMessages.isNotEmpty()) {
            scrollState.animateScrollToItem(chatMessages.size - 1)
        }
    }

    if (showLockedDialog) {
        AlertDialog(
            onDismissRequest = { showLockedDialog = false },
            title = { Text(strings.vipFeatureLockedTitle, fontWeight = FontWeight.Bold, color = CyberBlue) },
            text = {
                Text(
                    String.format(strings.chatLockedBody, lockedFeatureName),
                    color = LightGrayText,
                )
            },
            confirmButton = {
                Button(
                    shape = ButtonShape,
                    onClick = { showLockedDialog = false },
                    colors = ButtonDefaults.buttonColors(containerColor = CyberBlue),
                ) {
                    Text("OK")
                }
            },
            containerColor = DarkCard,
        )
    }

    Column(
        modifier =
            Modifier
                .fillMaxSize()
                .padding(16.dp),
    ) {
        AiChatControlsCard(
            strings = strings,
            isVip = isVip,
            searchEnabled = searchEnabled,
            mapsEnabled = mapsEnabled,
            highThinkingEnabled = highThinkingEnabled,
            lowLatencyEnabled = lowLatencyEnabled,
            viewModel = viewModel,
            showLockedDialogState = showLockedDialogState,
            lockedFeatureNameState = lockedFeatureNameState,
        )

        Spacer(modifier = Modifier.height(12.dp))

        ChatFeedList(
            chatMessages = chatMessages,
            isChatLoading = isChatLoading,
            scrollState = scrollState,
            strings = strings,
        )

        Spacer(modifier = Modifier.height(8.dp))

        ChartPatternScannerRow(
            strings = strings,
            isVip = isVip,
            selectedPatternNameState = selectedPatternNameState,
            selectedPatternBase64State = selectedPatternBase64State,
            showLockedDialogState = showLockedDialogState,
            lockedFeatureNameState = lockedFeatureNameState,
        )

        Spacer(modifier = Modifier.height(10.dp))

        // Attached image label
        selectedPatternName?.let { name ->
            Row(
                modifier =
                    Modifier
                        .fillMaxWidth()
                        .background(DarkBlue.copy(alpha = 0.3f), RoundedCornerShape(4.dp))
                        .padding(8.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    String.format(strings.chatAttachedPattern, name),
                    color = CyberBlue,
                    style = MaterialTheme.typography.labelSmall.copy(fontFamily = FontFamily.Monospace),
                )
                Text(
                    strings.chatClear,
                    color = LossRed,
                    style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                    modifier =
                        Modifier.clickable {
                            selectedPatternName = null
                            selectedPatternBase64 = null
                        }
                )
            }
            Spacer(modifier = Modifier.height(6.dp))
        }

        AiChatSendRow(
            strings = strings,
            viewModel = viewModel,
            inputMessageState = inputMessageState,
            selectedPatternNameState = selectedPatternNameState,
            selectedPatternBase64State = selectedPatternBase64State,
        )
    }
}
