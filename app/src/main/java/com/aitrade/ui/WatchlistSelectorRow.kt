package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
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
fun WatchlistSelectorRow(
    prefs: com.aitrade.data.UserPreferences,
    selectedAssetState: MutableState<String>,
) {
    val strings = LocalAppStrings.current
    var selectedAsset by selectedAssetState
    Row(
        modifier =
            Modifier
                .fillMaxWidth()
                .horizontalScroll(rememberScrollState()),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        val watchlist = prefs.getWatchlistList()
        watchlist.forEach { asset ->
            // Get current price of asset
            val lastPrice = MarketService.getLastPrice(asset)
            val closes = MarketService.getCloses(asset)
            val ema20List = DecisionEngine.calculateEma(closes, 20)
            val ema50List = DecisionEngine.calculateEma(closes, 50)
            val bullish =
                if (ema20List.isNotEmpty() && ema50List.isNotEmpty()) {
                    ema20List.last() > ema50List.last()
                } else {
                    true
                }

            val isSelected = selectedAsset == asset
            Card(
                modifier =
                    Modifier
                        .width(130.dp)
                        .clickable { selectedAsset = asset },
                colors =
                    CardDefaults.cardColors(
                        containerColor = if (isSelected) CyberBlue.copy(alpha = 0.2f) else DarkCard,
                    ),
                border =
                    BorderStroke(
                        1.dp,
                        if (isSelected) CyberBlue else BorderColor,
                    ),
            ) {
                Column(modifier = Modifier.padding(12.dp)) {
                    Text(
                        asset,
                        color = Color.White,
                        style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Bold),
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        "\$${String.format("%,.2f", lastPrice)}",
                        color = if (bullish) ProfitGreen else LossRed,
                        style = MaterialTheme.typography.bodyLarge.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(4.dp),
                    ) {
                        Icon(
                            imageVector = if (bullish) Icons.Default.ArrowUpward else Icons.Default.ArrowDownward,
                            contentDescription = null,
                            tint = if (bullish) ProfitGreen else LossRed,
                            modifier = Modifier.size(12.dp),
                        )
                        Text(
                            if (bullish) strings.bullishLabel else strings.bearishLabel,
                            color = if (bullish) ProfitGreen else LossRed,
                            style = LabelExtraSmall,
                        )
                    }
                }
            }
        }
    }
}
