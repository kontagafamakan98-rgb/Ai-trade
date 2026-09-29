package com.aitrade.ui

import android.content.Intent
import android.net.Uri
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.util.*

@Composable
fun GooglePayUpgradeDialog(
    strings: AppStrings,
    viewModel: TradingViewModel,
    showGPayPopupState: MutableState<Boolean>,
    selectedPlanState: MutableState<VipPlan>,
    isUpgradingProgressState: MutableState<Boolean>,
) {
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    var showGPayPopup by showGPayPopupState
    var selectedPlan by selectedPlanState
    var isUpgradingProgress by isUpgradingProgressState
    AlertDialog(
        onDismissRequest = { showGPayPopup = false },
        title = {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Icon(Icons.Default.Diamond, contentDescription = null, tint = CyberBlue)
                Text(strings.gpayTitle, fontWeight = FontWeight.Bold, color = Color.White)
            }
        },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text(
                    String.format(strings.gpayUpgradePlan, vipPlanTitle(strings, selectedPlan)),
                    color = Color.White,
                    fontWeight = FontWeight.Bold,
                )
                Text(
                    String.format(strings.gpayTotalCharge, vipPlanRecurringPrice(strings, selectedPlan)),
                    color = ProfitGreen,
                    style = MaterialTheme.typography.titleLarge.copy(fontWeight = FontWeight.Bold),
                )
                Spacer(modifier = Modifier.height(4.dp))
                Text(
                    strings.gpayMerchantNote,
                    color = LightGrayText,
                    style = MaterialTheme.typography.labelMedium,
                )

                if (isUpgradingProgress) {
                    Spacer(modifier = Modifier.height(8.dp))
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        CircularProgressIndicator(modifier = Modifier.size(20.dp), color = CyberBlue)
                        Text(
                            strings.gpayAuthorizing,
                            color = CyberBlue,
                            style = MaterialTheme.typography.labelMedium.copy(fontFamily = FontFamily.Monospace),
                        )
                    }
                } else {
                    Spacer(modifier = Modifier.height(8.dp))
                    // PayPal Option
                    Button(
                        onClick = {
                            // Montant facturé : valeur machine (USD), jamais traduite.
                            val amountValue = selectedPlan.amountUsd
                            val paypalUrl =
                                "https://www.paypal.com/cgi-bin/webscr?cmd=_xclick" +
                                    "&business=makemoney0598@gmail.com" +
                                    "&item_name=AI+Trade+Terminal+VIP+" +
                                    "${vipPlanTitle(strings, selectedPlan).replace(" ", "+")}" +
                                    "&amount=$amountValue&currency_code=USD"
                            try {
                                val intent = Intent(Intent.ACTION_VIEW, Uri.parse(paypalUrl))
                                context.startActivity(intent)
                            } catch (e: Exception) {
                                e.printStackTrace()
                            }

                            isUpgradingProgress = true
                            scope.launch {
                                delay(2000)
                                viewModel.upgradeToVip()
                                isUpgradingProgress = false
                                showGPayPopup = false
                            }
                        },
                        colors = ButtonDefaults.buttonColors(containerColor = Color(0xFFFFC439)),
                        modifier = Modifier.fillMaxWidth(),
                        shape = RoundedCornerShape(8.dp),
                    ) {
                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Icon(Icons.Default.Payment, contentDescription = strings.gpayPaypalLogo, tint = Color(0xFF003087))
                            Text(strings.payWithPaypal, color = Color(0xFF003087), fontWeight = FontWeight.Bold)
                        }
                    }

                    // Google Pay Option
                    Button(
                        onClick = {
                            isUpgradingProgress = true
                            scope.launch {
                                delay(1800)
                                viewModel.upgradeToVip()
                                isUpgradingProgress = false
                                showGPayPopup = false
                            }
                        },
                        colors = ButtonDefaults.buttonColors(containerColor = Color.White),
                        modifier = Modifier.fillMaxWidth(),
                        shape = RoundedCornerShape(8.dp),
                    ) {
                        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Icon(Icons.Default.CreditCard, contentDescription = strings.gpayCreditCard, tint = Color.Black)
                            Text(strings.googlePaySandbox, color = Color.Black, fontWeight = FontWeight.Bold)
                        }
                    }
                }
            }
        },
        confirmButton = {},
        dismissButton = {
            TextButton(
                shape = ButtonShape,
                onClick = { showGPayPopup = false },
                enabled = !isUpgradingProgress,
            ) {
                Text(strings.cancelBtn, color = LossRed)
            }
        },
        containerColor = DarkCard,
    )
}
