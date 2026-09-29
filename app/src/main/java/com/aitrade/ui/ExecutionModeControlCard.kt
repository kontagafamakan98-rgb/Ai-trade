package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
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
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

// --- EXECUTION MODE CONTROL CARD ---

@Composable
fun ExecutionModeControlCard(
    credentials: com.aitrade.data.BrokerCredentials,
    onToggleLive: (Boolean) -> Unit,
) {
    val strings = LocalAppStrings.current
    val isLive = !credentials.paperMode && credentials.apiKey.isNotEmpty()

    Card(
        modifier =
            Modifier
                .fillMaxWidth()
                .testTag("execution_mode_control_card"),
        colors =
            CardDefaults.cardColors(
                containerColor = if (isLive) LossRed.copy(alpha = 0.12f) else CyberBlue.copy(alpha = 0.08f),
            ),
        border = BorderStroke(1.dp, if (isLive) LossRed else CyberBlue),
    ) {
        Column(modifier = Modifier.padding(14.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    modifier = Modifier.weight(1f),
                ) {
                    Box(
                        modifier =
                            Modifier
                                .size(10.dp)
                                .background(if (isLive) LossRed else ProfitGreen, CircleShape),
                    )
                    Column {
                        Text(
                            text = if (isLive) strings.execModeLiveTitle else strings.execModePaperTitle,
                            style = MaterialTheme.typography.labelSmall
                                .copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                            color = if (isLive) LossRed else ProfitGreen
                        )
                        Text(
                            text =
                                if (isLive) {
                                    String.format(strings.execLiveNote, credentials.brokerName)
                                } else {
                                    strings.execPaperNote
                                },
                            style = LabelExtraSmall,
                            color = LightGrayText
                        )
                    }
                }

                Button(
                    onClick = { onToggleLive(!isLive) },
                    colors =
                        ButtonDefaults.buttonColors(
                            containerColor = if (isLive) CyberBlue else LossRed,
                        ),
                    shape = RoundedCornerShape(6.dp),
                ) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(4.dp),
                    ) {
                        Icon(
                            imageVector = if (isLive) Icons.Default.Shield else Icons.Default.FlashOn,
                            contentDescription = null,
                            modifier = Modifier.size(14.dp),
                            tint = Color.White,
                        )
                        Text(
                            text = if (isLive) strings.execSwitchPaper else strings.execSwitchLive,
                            style = LabelExtraSmall.copy(fontWeight = FontWeight.Bold),
                            color = Color.White
                        )
                    }
                }
            }
        }
    }
}
