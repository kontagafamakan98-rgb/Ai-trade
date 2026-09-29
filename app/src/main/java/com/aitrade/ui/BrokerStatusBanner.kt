package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.R
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun BrokerStatusBanner(
    strings: AppStrings,
    credentials: com.aitrade.data.BrokerCredentials,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DarkBlue.copy(alpha = 0.4f)),
        border = BorderStroke(1.dp, CyberBlue.copy(alpha = 0.5f)),
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Box(
                    modifier =
                        Modifier
                            .size(8.dp)
                            .background(
                                if (credentials.apiKey.isNotEmpty() ||
                                    credentials.accountNumber.isNotEmpty()
                                ) {
                                    ProfitGreen
                                } else {
                                    Color.Yellow
                                },
                                CircleShape,
                            ),
                )
                Text(
                    "${strings.activeBroker}: ${credentials.brokerName}",
                    style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                    color = Color.White
                )
                Spacer(modifier = Modifier.weight(1f))

                val badgeText =
                    when {
                        !credentials.paperMode -> stringResource(R.string.badge_live_account)
                        credentials.apiKey.isNotEmpty() -> stringResource(R.string.badge_real_demo)
                        else -> stringResource(R.string.badge_paper_sim)
                    }

                val badgeBgColor =
                    when {
                        !credentials.paperMode -> LossRed
                        credentials.apiKey.isNotEmpty() -> ProfitGreen
                        else -> CyberBlue
                    }

                Box(
                    modifier =
                        Modifier
                            .background(badgeBgColor, RoundedCornerShape(4.dp))
                            .padding(horizontal = 6.dp, vertical = 2.dp),
                ) {
                    Text(
                        badgeText,
                        color = Color.White,
                        style = LabelTiny.copy(fontWeight = FontWeight.Bold),
                    )
                }
            }
            if (credentials.apiKey.isNotEmpty() || credentials.accountNumber.isNotEmpty()) {
                Spacer(modifier = Modifier.height(4.dp))
                val keyPreview = if (credentials.apiKey.length > 4) "••••" + credentials.apiKey.takeLast(4) else credentials.accountNumber
                Text(
                    "${strings.keysConfiguredLabel}: $keyPreview",
                    color = LightGrayText,
                    style = MaterialTheme.typography.labelSmall.copy(fontFamily = FontFamily.Monospace),
                )
            }
        }
    }
}
