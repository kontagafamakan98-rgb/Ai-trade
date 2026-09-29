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
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

// --- ADAPTIVE AI SELF-LEARNING & AUTO-CORRECTION CARD ---

@Composable
fun AdaptiveLearningCard(
    profile: AdaptiveLearningProfile,
    onTriggerReview: () -> Unit,
) {
    val strings = LocalAppStrings.current
    Card(
        modifier =
            Modifier
                .fillMaxWidth()
                .testTag("adaptive_learning_card"),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, CyberBlue.copy(alpha = 0.6f)),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Icon(
                        imageVector = Icons.Default.AutoAwesome,
                        contentDescription = null,
                        tint = CyberBlue,
                        modifier = Modifier.size(20.dp),
                    )
                    Column {
                        Text(
                            strings.learnTitle,
                            style = MaterialTheme.typography.labelLarge
                                .copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                            color = Color.White
                        )
                        Text(
                            strings.learnSubtitle,
                            style = LabelExtraSmall,
                            color = MutedText
                        )
                    }
                }

                IconButton(onClick = onTriggerReview) {
                    Icon(
                        imageVector = Icons.Default.Refresh,
                        contentDescription = strings.learnReevaluate,
                        tint = CyberBlue,
                        modifier = Modifier.size(18.dp),
                    )
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            // Dynamic Weights distribution bar
            Text(
                strings.learnWeightsAdjusted,
                style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                color = LightGrayText
            )
            Spacer(modifier = Modifier.height(6.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Box(
                    modifier =
                        Modifier
                            .weight(profile.taWeightPct.toFloat().coerceAtLeast(1f))
                            .height(8.dp)
                            .background(CyberBlue, RoundedCornerShape(topStart = 4.dp, bottomStart = 4.dp)),
                )
                Spacer(modifier = Modifier.width(2.dp))
                Box(
                    modifier =
                        Modifier
                            .weight(profile.macroWeightPct.toFloat().coerceAtLeast(1f))
                            .height(8.dp)
                            .background(GoldYellow),
                )
                Spacer(modifier = Modifier.width(2.dp))
                Box(
                    modifier =
                        Modifier
                            .weight(profile.sentimentWeightPct.toFloat().coerceAtLeast(1f))
                            .height(8.dp)
                            .background(ProfitGreen, RoundedCornerShape(topEnd = 4.dp, bottomEnd = 4.dp)),
                )
            }

            Spacer(modifier = Modifier.height(6.dp))
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Text(
                    String.format(strings.learnWeightTa, profile.taWeightPct),
                    style = LabelExtraSmall.copy(fontFamily = FontFamily.Monospace),
                    color = CyberBlue,
                )
                Text(
                    String.format(strings.learnWeightMacro, profile.macroWeightPct),
                    style = LabelExtraSmall.copy(fontFamily = FontFamily.Monospace),
                    color = GoldYellow,
                )
                Text(
                    String.format(strings.learnWeightSentiment, profile.sentimentWeightPct),
                    style = LabelExtraSmall.copy(fontFamily = FontFamily.Monospace),
                    color = ProfitGreen
                )
            }

            Spacer(modifier = Modifier.height(12.dp))
            HorizontalDivider(color = BorderColor, thickness = 0.5.dp)
            Spacer(modifier = Modifier.height(10.dp))

            // Auto-learned rules
            Text(
                strings.learnRulesGenerated,
                style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                color = LightGrayText
            )
            Spacer(modifier = Modifier.height(4.dp))

            profile.learnedRules.forEach { rule ->
                Text(
                    rule,
                    style = MaterialTheme.typography.labelSmall,
                    color = Color.White,
                    modifier = Modifier.padding(vertical = 2.dp)
                )
            }

            if (profile.postMortemLessons.isNotEmpty()) {
                Spacer(modifier = Modifier.height(8.dp))
                Text(
                    strings.learnPostMortemLessons,
                    style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                    color = MutedText
                )
                Spacer(modifier = Modifier.height(2.dp))
                profile.postMortemLessons.forEach { lesson ->
                    Text(
                        lesson,
                        style = LabelExtraSmall.copy(fontFamily = FontFamily.Monospace),
                        color = LightGrayText
                    )
                }
            }
        }
    }
}
