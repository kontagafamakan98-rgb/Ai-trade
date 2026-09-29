package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun LanguageSelectorCard(
    strings: AppStrings,
    prefs: com.aitrade.data.UserPreferences,
    viewModel: TradingViewModel,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(
                strings.languageSettingsTitle,
                style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                color = Color.White
            )
            Text(
                strings.languageSettingsSubtitle,
                color = MutedText,
                style = MaterialTheme.typography.labelMedium,
            )
            Spacer(modifier = Modifier.height(12.dp))
            LazyRow(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                modifier = Modifier.fillMaxWidth(),
            ) {
                items(AppLanguage.entries.size) { index ->
                    val lang = AppLanguage.entries[index]
                    val isSelected = prefs.language == lang.code
                    Card(
                        modifier =
                            Modifier.clickable {
                                viewModel.updateLanguage(lang.code)
                            },
                        colors =
                            CardDefaults.cardColors(
                                containerColor = if (isSelected) CyberBlue.copy(alpha = 0.25f) else DarkBlue,
                            ),
                        border = BorderStroke(1.dp, if (isSelected) CyberBlue else BorderColor),
                    ) {
                        Row(
                            modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(6.dp),
                        ) {
                            Text(
                                lang.code.uppercase(),
                                style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
                                color = if (isSelected) CyberBlue else MutedText
                            )
                            Text(
                                lang.displayName,
                                style = MaterialTheme.typography.labelLarge,
                                fontWeight = if (isSelected) FontWeight.Bold else FontWeight.Normal,
                                color = if (isSelected) CyberBlue else Color.White
                            )
                        }
                    }
                }
            }
        }
    }
}
