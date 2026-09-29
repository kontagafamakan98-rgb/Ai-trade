package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun ChartPatternScannerRow(
    strings: AppStrings,
    isVip: Boolean,
    selectedPatternNameState: MutableState<String?>,
    selectedPatternBase64State: MutableState<String?>,
    showLockedDialogState: MutableState<Boolean>,
    lockedFeatureNameState: MutableState<String>,
) {
    var selectedPatternName by selectedPatternNameState
    var selectedPatternBase64 by selectedPatternBase64State
    var showLockedDialog by showLockedDialogState
    var lockedFeatureName by lockedFeatureNameState
    // 3. Image technical chart attachment row
    Text(
        strings.chartScannerSelect,
        style = MaterialTheme.typography.labelSmall.copy(fontFamily = FontFamily.Monospace),
        color = Color.Gray
    )
    Spacer(modifier = Modifier.height(4.dp))

    LazyRow(
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        modifier = Modifier.fillMaxWidth(),
    ) {
        item {
            Card(
                modifier =
                    Modifier
                        .size(100.dp)
                        .clickable {
                            if (!isVip) {
                                lockedFeatureName = strings.chartScannerTitle
                                showLockedDialog = true
                            } else {
                                selectedPatternName = strings.chartPatternDoubleBottom
                                // Mock base64 representable of image
                                selectedPatternBase64 = "MOCK_BASE64_DOUBLE_BOTTOM_PATTERN"
                            }
                        },
                border =
                    BorderStroke(
                        2.dp,
                        if (selectedPatternName == strings.chartPatternDoubleBottom) CyberBlue else BorderColor,
                    ),
                colors = CardDefaults.cardColors(containerColor = DarkCard),
            ) {
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    modifier = Modifier.padding(4.dp),
                ) {
                    DoubleBottomChartPreview()
                    Text(strings.chartPatternDoubleBottom, style = LabelExtraSmall, color = Color.White)
                }
            }
        }

        item {
            Card(
                modifier =
                    Modifier
                        .size(100.dp)
                        .clickable {
                            if (!isVip) {
                                lockedFeatureName = strings.chartScannerTitle
                                showLockedDialog = true
                            } else {
                                selectedPatternName = strings.chartPatternHeadShoulders
                                selectedPatternBase64 = "MOCK_BASE64_HEAD_SHOULDERS_PATTERN"
                            }
                        },
                border =
                    BorderStroke(
                        2.dp,
                        if (selectedPatternName == strings.chartPatternHeadShoulders) CyberBlue else BorderColor,
                    ),
                colors = CardDefaults.cardColors(containerColor = DarkCard),
            ) {
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    modifier = Modifier.padding(4.dp),
                ) {
                    HeadAndShouldersChartPreview()
                    Text(strings.chartPatternHs, style = LabelExtraSmall, color = Color.White)
                }
            }
        }

        item {
            Card(
                modifier =
                    Modifier
                        .size(100.dp)
                        .clickable {
                            if (!isVip) {
                                lockedFeatureName = strings.chartScannerTitle
                                showLockedDialog = true
                            } else {
                                selectedPatternName = strings.chartPatternRsiDivergence
                                selectedPatternBase64 = "MOCK_BASE64_RSI_DIVERGENCE_PATTERN"
                            }
                        },
                border =
                    BorderStroke(
                        2.dp,
                        if (selectedPatternName == strings.chartPatternRsiDivergence) CyberBlue else BorderColor,
                    ),
                colors = CardDefaults.cardColors(containerColor = DarkCard),
            ) {
                Column(
                    horizontalAlignment = Alignment.CenterHorizontally,
                    modifier = Modifier.padding(4.dp),
                ) {
                    RsiDivergenceChartPreview()
                    Text(strings.chartPatternRsiDivergence, style = LabelExtraSmall, color = Color.White)
                }
            }
        }
    }
}
