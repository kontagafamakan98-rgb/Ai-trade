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
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun AgentReportRow(
    agent: AgentReport,
    icon: ImageVector,
    themeColor: Color,
) {
    val strings = LocalAppStrings.current
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DarkBlue.copy(alpha = 0.4f)),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        Column(modifier = Modifier.padding(10.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    Icon(icon, contentDescription = null, tint = themeColor, modifier = Modifier.size(16.dp))
                    Text(
                        agent.agentName,
                        style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
                        color = Color.White,
                    )
                }

                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    Text(
                        "${strings.scoreLabel}: ${String.format("%.2f", agent.score)}",
                        style = LabelExtraSmall.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                        color = themeColor
                    )
                    Box(
                        modifier =
                            Modifier
                                .background(themeColor.copy(alpha = 0.2f), RoundedCornerShape(3.dp))
                                .border(0.5.dp, themeColor, RoundedCornerShape(3.dp))
                                .padding(horizontal = 4.dp, vertical = 1.dp),
                    ) {
                        Text(
                            convictionLabel(strings, agent.conviction),
                            style = LabelMicro.copy(fontWeight = FontWeight.Bold),
                            color = themeColor,
                        )
                    }
                }
            }

            Spacer(modifier = Modifier.height(4.dp))
            Text(agent.recommendation, style = LabelExtraSmall.copy(fontWeight = FontWeight.SemiBold), color = LightGrayText)

            Spacer(modifier = Modifier.height(4.dp))
            agent.keyFindings.forEach { finding ->
                Text("• $finding", style = LabelTiny, color = MutedText)
            }
        }
    }
}
