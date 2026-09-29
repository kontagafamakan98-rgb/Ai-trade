package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.items
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
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun ChatFeedList(
    chatMessages: List<ChatMessage>,
    isChatLoading: Boolean,
    scrollState: LazyListState,
    strings: AppStrings,
) {
    // 2. Chat Feed lazy list
    LazyColumn(
        modifier =
            Modifier
                .weight(1f)
                .fillMaxWidth()
                .testTag("chat_list"),
        state = scrollState,
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        items(chatMessages.size) { index ->
            val msg = chatMessages[index]
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = if (msg.isUser) Arrangement.End else Arrangement.Start,
            ) {
                Card(
                    modifier = Modifier.fillMaxWidth(0.85f),
                    colors =
                        CardDefaults.cardColors(
                            containerColor = if (msg.isUser) DarkBlue else DarkCard,
                        ),
                    border =
                        BorderStroke(
                            1.dp,
                            if (msg.isUser) CyberBlue.copy(alpha = 0.5f) else BorderColor,
                        ),
                ) {
                    Column(modifier = Modifier.padding(12.dp)) {
                        Text(
                            text = if (msg.isUser) strings.chatSenderUser else strings.chatSenderGemini,
                            color = if (msg.isUser) CyberBlue else ProfitGreen,
                            style = LabelExtraSmall.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                        )
                        Spacer(modifier = Modifier.height(4.dp))
                        Text(
                            text = msg.content,
                            color = Color.White,
                            style = MaterialTheme.typography.labelMedium,
                        )
                    }
                }
            }
        }

        if (isChatLoading) {
            item {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.Start,
                ) {
                    Card(
                        modifier = Modifier.fillMaxWidth(0.85f),
                        colors = CardDefaults.cardColors(containerColor = DarkCard),
                        border = BorderStroke(1.dp, BorderColor),
                    ) {
                        Row(
                            modifier = Modifier.padding(12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            CircularProgressIndicator(
                                modifier = Modifier.size(16.dp),
                                color = CyberBlue,
                                strokeWidth = 2.dp,
                            )
                            Text(
                                strings.scanningMetrics,
                                color = LightGrayText,
                                style = MaterialTheme.typography.labelSmall.copy(fontFamily = FontFamily.Monospace),
                            )
                        }
                    }
                }
            }
        }
    }
}
