package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
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
fun MultiAgentControlPanel(
    selectedAssetState: MutableState<String>,
    watchlist: List<String>,
    isResearching: Boolean,
    onRunResearch: (String) -> Unit,
) {
    val strings = LocalAppStrings.current
    var selectedAsset by selectedAssetState
    // Title Header
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Icon(Icons.Default.Groups, contentDescription = null, tint = CyberBlue, modifier = Modifier.size(24.dp))
            Column {
                Text(
                    strings.agentDeskTitle,
                    style = MaterialTheme.typography.bodyLarge.copy(fontWeight = FontWeight.Bold),
                    color = Color.White
                )
                Text(
                    strings.agentDeskSubtitle,
                    color = MutedText,
                    style = LabelExtraSmall,
                )
            }
        }

        Box(
            modifier =
                Modifier
                    .background(CyberBlue.copy(alpha = 0.2f), RoundedCornerShape(4.dp))
                    .border(1.dp, CyberBlue, RoundedCornerShape(4.dp))
                    .padding(horizontal = 6.dp, vertical = 2.dp),
        ) {
            Text(
                strings.agentConsensus,
                color = CyberBlue,
                style = LabelTiny.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
            )
        }
    }

    Spacer(modifier = Modifier.height(12.dp))

    // Asset selector row
    LazyRow(
        horizontalArrangement = Arrangement.spacedBy(6.dp),
        modifier = Modifier.fillMaxWidth(),
    ) {
        items(watchlist.size) { idx ->
            val asset = watchlist[idx]
            val isSelected = selectedAsset == asset
            Box(
                modifier =
                    Modifier
                        .clip(RoundedCornerShape(6.dp))
                        .background(if (isSelected) CyberBlue else DarkBlue)
                        .clickable {
                            selectedAsset = asset
                            onRunResearch(asset)
                        }.padding(horizontal = 10.dp, vertical = 6.dp),
            ) {
                Text(
                    asset,
                    style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                    color = if (isSelected) Color.Black else Color.White
                )
            }
        }
    }

    Spacer(modifier = Modifier.height(12.dp))

    Button(
        onClick = { onRunResearch(selectedAsset) },
        colors = ButtonDefaults.buttonColors(containerColor = CyberBlue),
        modifier = Modifier.fillMaxWidth(),
        enabled = !isResearching,
        shape = RoundedCornerShape(8.dp),
    ) {
        if (isResearching) {
            Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                CircularProgressIndicator(modifier = Modifier.size(16.dp), color = Color.White, strokeWidth = 2.dp)
                Text(
                    strings.agentCoordinating,
                    style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
                )
            }
        } else {
            Row(
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Icon(Icons.Default.Psychology, contentDescription = null, modifier = Modifier.size(18.dp))
                Text(
                    String.format(strings.agentRunResearch, selectedAsset),
                    style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
                )
            }
        }
    }

    Spacer(modifier = Modifier.height(14.dp))
}
