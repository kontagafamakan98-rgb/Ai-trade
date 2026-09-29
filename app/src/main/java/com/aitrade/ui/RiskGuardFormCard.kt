package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun RiskGuardFormCard(
    strings: AppStrings,
    prefs: com.aitrade.data.UserPreferences,
    viewModel: TradingViewModel,
) {
    var riskPctInput by remember(prefs) { mutableStateOf(prefs.riskPct.toString()) }
    var minConfidenceInput by remember(prefs) { mutableStateOf(prefs.minConfidence.toString()) }
    var maxDailyLossInput by remember(prefs) { mutableStateOf(prefs.maxDailyLossPct.toString()) }
    var maxDrawdownInput by remember(prefs) { mutableStateOf(prefs.maxTotalDrawdownPct.toString()) }
    var maxOpenTradesInput by remember(prefs) { mutableStateOf(prefs.maxOpenTrades.toString()) }

    val focusManager = LocalFocusManager.current

    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, BorderColor),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(
                strings.riskSettingsTitle,
                style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                color = Color.White
            )
            Spacer(modifier = Modifier.height(16.dp))

            // Form Fields
            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = riskPctInput,
                    onValueChange = { riskPctInput = it },
                    label = { Text(strings.riskPctLabel) },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                    colors =
                        OutlinedTextFieldDefaults.colors(
                            focusedTextColor = Color.White,
                            unfocusedTextColor = Color.White,
                            focusedBorderColor = CyberBlue,
                            unfocusedBorderColor = BorderColor,
                        ),
                    modifier = Modifier.weight(1f),
                )
                OutlinedTextField(
                    value = minConfidenceInput,
                    onValueChange = { minConfidenceInput = it },
                    label = { Text(strings.minConfidenceLabel) },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                    colors =
                        OutlinedTextFieldDefaults.colors(
                            focusedTextColor = Color.White,
                            unfocusedTextColor = Color.White,
                            focusedBorderColor = CyberBlue,
                            unfocusedBorderColor = BorderColor,
                        ),
                    modifier = Modifier.weight(1f),
                )
            }
            Spacer(modifier = Modifier.height(12.dp))

            Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = maxDailyLossInput,
                    onValueChange = { maxDailyLossInput = it },
                    label = { Text(strings.maxDailyLossLabel) },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                    colors =
                        OutlinedTextFieldDefaults.colors(
                            focusedTextColor = Color.White,
                            unfocusedTextColor = Color.White,
                            focusedBorderColor = CyberBlue,
                            unfocusedBorderColor = BorderColor,
                        ),
                    modifier = Modifier.weight(1f),
                )
                OutlinedTextField(
                    value = maxDrawdownInput,
                    onValueChange = { maxDrawdownInput = it },
                    label = { Text(strings.maxDrawdownLabel) },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                    colors =
                        OutlinedTextFieldDefaults.colors(
                            focusedTextColor = Color.White,
                            unfocusedTextColor = Color.White,
                            focusedBorderColor = CyberBlue,
                            unfocusedBorderColor = BorderColor,
                        ),
                    modifier = Modifier.weight(1f),
                )
            }
            Spacer(modifier = Modifier.height(12.dp))

            OutlinedTextField(
                value = maxOpenTradesInput,
                onValueChange = { maxOpenTradesInput = it },
                label = { Text(strings.maxOpenTradesLabel) },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                colors =
                    OutlinedTextFieldDefaults.colors(
                        focusedTextColor = Color.White,
                        unfocusedTextColor = Color.White,
                        focusedBorderColor = CyberBlue,
                        unfocusedBorderColor = BorderColor,
                    ),
                modifier = Modifier.fillMaxWidth(),
            )

            Spacer(modifier = Modifier.height(16.dp))

            Button(
                shape = ButtonShape,
                onClick = {
                    focusManager.clearFocus()
                    val risk = riskPctInput.toDoubleOrNull() ?: prefs.riskPct
                    val minConf = minConfidenceInput.toDoubleOrNull() ?: prefs.minConfidence
                    val dailyLoss = maxDailyLossInput.toDoubleOrNull() ?: prefs.maxDailyLossPct
                    val drawdown = maxDrawdownInput.toDoubleOrNull() ?: prefs.maxTotalDrawdownPct
                    val maxTrades = maxOpenTradesInput.toIntOrNull() ?: prefs.maxOpenTrades

                    viewModel.saveRiskPreferences(
                        riskPct = risk,
                        minConfidence = minConf,
                        maxDailyLossPct = dailyLoss,
                        maxTotalDrawdownPct = drawdown,
                        maxOpenTrades = maxTrades,
                    )
                },
                colors = ButtonDefaults.buttonColors(containerColor = CyberBlue),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(strings.saveRiskParameters, fontWeight = FontWeight.Bold)
            }
        }
    }
}
