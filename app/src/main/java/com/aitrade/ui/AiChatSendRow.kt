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
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun AiChatSendRow(
    strings: AppStrings,
    viewModel: TradingViewModel,
    inputMessageState: MutableState<String>,
    selectedPatternNameState: MutableState<String?>,
    selectedPatternBase64State: MutableState<String?>,
) {
    var inputMessage by inputMessageState
    var selectedPatternName by selectedPatternNameState
    var selectedPatternBase64 by selectedPatternBase64State
    // 4. Send Field Row
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        OutlinedTextField(
            value = inputMessage,
            onValueChange = { inputMessage = it },
            placeholder = { Text(strings.chatInputPlaceholder) },
            colors =
                OutlinedTextFieldDefaults.colors(
                    focusedTextColor = Color.White,
                    unfocusedTextColor = Color.White,
                    focusedBorderColor = CyberBlue,
                    unfocusedBorderColor = BorderColor,
                ),
            modifier = Modifier.weight(1f),
        )

        IconButton(
            onClick = {
                if (inputMessage.isNotBlank() || selectedPatternBase64 != null) {
                    viewModel.sendChatMessage(
                        messageText = inputMessage,
                        attachedImageBase64 = selectedPatternBase64,
                        mimeType = if (selectedPatternBase64 != null) "image/jpeg" else null,
                    )
                    inputMessage = ""
                    selectedPatternName = null
                    selectedPatternBase64 = null
                }
            },
            modifier =
                Modifier
                    .size(48.dp)
                    .background(CyberBlue, RoundedCornerShape(8.dp)),
        ) {
            Icon(
                Icons.Default.Send,
                contentDescription = strings.chatSend,
                tint = Color.White,
            )
        }
    }
}
