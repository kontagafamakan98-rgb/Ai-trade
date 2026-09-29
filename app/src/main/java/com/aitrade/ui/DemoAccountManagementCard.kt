package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
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
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun DemoAccountManagementCard(
    strings: AppStrings,
    currentEquity: Double,
    viewModel: TradingViewModel,
) {
    var demoCapitalInput by remember(currentEquity) { mutableStateOf(String.format("%.0f", currentEquity)) }
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = DarkCard),
        border = BorderStroke(1.dp, CyberBlue.copy(alpha = 0.5f)),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Icon(Icons.Default.AccountBalanceWallet, contentDescription = null, tint = ProfitGreen)
                    Text(
                        strings.manageDemoAccount,
                        style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                        color = Color.White
                    )
                }
                Surface(
                    shape = RoundedCornerShape(4.dp),
                    color = ProfitGreen.copy(alpha = 0.2f),
                    border = BorderStroke(1.dp, ProfitGreen),
                ) {
                    Text(
                        strings.demoAccount,
                        color = ProfitGreen,
                        style = LabelExtraSmall.copy(fontWeight = FontWeight.Bold),
                        modifier = Modifier.padding(horizontal = 8.dp, vertical = 2.dp)
                    )
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            Text(
                "${strings.demoBalance}: \$${String.format("%,.2f", currentEquity)}",
                style = MaterialTheme.typography.titleLarge.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                color = ProfitGreen
            )

            Spacer(modifier = Modifier.height(12.dp))

            // Preset quick selection chips
            Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                modifier = Modifier.fillMaxWidth(),
            ) {
                listOf(10000.0, 50000.0, 100000.0, 250000.0).forEach { amount ->
                    OutlinedButton(
                        shape = ButtonShape,
                        onClick = {
                            demoCapitalInput = amount.toInt().toString()
                            viewModel.updateDemoCapital(amount)
                        },
                        colors = ButtonDefaults.outlinedButtonColors(contentColor = CyberBlue),
                        border = BorderStroke(1.dp, if (currentEquity == amount) CyberBlue else BorderColor),
                        contentPadding = PaddingValues(horizontal = 8.dp, vertical = 4.dp),
                        modifier = Modifier.weight(1f),
                    ) {
                        Text("\$${amount.toInt() / 1000}k", style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold))
                    }
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                OutlinedTextField(
                    value = demoCapitalInput,
                    onValueChange = { demoCapitalInput = it },
                    label = { Text(strings.setInitialCapital) },
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    colors =
                        OutlinedTextFieldDefaults.colors(
                            focusedTextColor = Color.White,
                            unfocusedTextColor = Color.White,
                            focusedBorderColor = CyberBlue,
                            unfocusedBorderColor = BorderColor,
                        ),
                    modifier = Modifier.weight(1f),
                )

                Button(
                    shape = ButtonShape,
                    onClick = {
                        focusManager.clearFocus()
                        val amount = demoCapitalInput.toDoubleOrNull()
                        if (amount != null && amount > 0) {
                            viewModel.updateDemoCapital(amount)
                        }
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = ProfitGreen),
                ) {
                    Text(strings.updateDemoCapitalBtn, style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold))
                }
            }

            Spacer(modifier = Modifier.height(8.dp))

            OutlinedButton(
                shape = ButtonShape,
                onClick = { viewModel.resetSimulation() },
                colors = ButtonDefaults.outlinedButtonColors(contentColor = LossRed),
                border = BorderStroke(1.dp, LossRed.copy(alpha = 0.5f)),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Icon(Icons.Default.Refresh, contentDescription = null, tint = LossRed, modifier = Modifier.size(16.dp))
                Spacer(modifier = Modifier.width(6.dp))
                Text(strings.resetDemoCapital, style = MaterialTheme.typography.labelMedium)
            }
        }
    }
}
