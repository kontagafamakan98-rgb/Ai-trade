package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun PriceHistoryChartCard(
    strings: AppStrings,
    selectedAsset: String,
    priceTickTrigger: Int,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    String.format(strings.priceHistoryPairTitle, selectedAsset, strings.chartTitle),
                    fontWeight = FontWeight.Bold,
                    color = Color.White,
                    fontFamily = FontFamily.Monospace,
                )
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(modifier = Modifier.size(8.dp).background(CyberBlue).clip(CircleShape))
                        Spacer(modifier = Modifier.width(4.dp))
                        Text(strings.priceLabel, color = MutedText, style = LabelExtraSmall)
                    }
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(modifier = Modifier.size(8.dp).background(ProfitGreen).clip(CircleShape))
                        Spacer(modifier = Modifier.width(4.dp))
                        Text("EMA20", color = MutedText, style = LabelExtraSmall)
                    }
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(modifier = Modifier.size(8.dp).background(LossRed).clip(CircleShape))
                        Spacer(modifier = Modifier.width(4.dp))
                        Text("EMA50", color = MutedText, style = LabelExtraSmall)
                    }
                }
            }
            Spacer(modifier = Modifier.height(16.dp))

            // Live custom vector chart drawn on Compose Canvas!
            @Suppress("UNUSED_EXPRESSION")
            priceTickTrigger // Read state to force recompose on ticks
            val closes = MarketService.getCloses(selectedAsset)
            PriceChartCanvas(closes = closes)
        }
    }
}
