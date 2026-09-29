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
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun VipPaywallCard(
    strings: AppStrings,
    isVip: Boolean,
    showGPayPopupState: MutableState<Boolean>,
    selectedPlanState: MutableState<VipPlan>,
) {
    var showGPayPopup by showGPayPopupState
    var selectedPlan by selectedPlanState
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = if (isVip) DarkBlue else DarkCard),
        border = BorderStroke(1.dp, if (isVip) ProfitGreen else CyberBlue),
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Icon(
                        Icons.Default.Diamond,
                        contentDescription = null,
                        tint = if (isVip) ProfitGreen else CyberBlue,
                        modifier = Modifier.size(32.dp),
                    )
                    Text(
                        strings.eliteVipLicenseTitle,
                        color = Color.White,
                        style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold),
                    )
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            if (!isVip) {
                Text(
                    strings.institutionalGradeTitle,
                    color = LightGrayText,
                    style = MaterialTheme.typography.labelMedium,
                )

                Spacer(modifier = Modifier.height(12.dp))

                // Features checklist
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Icon(Icons.Default.Check, contentDescription = null, tint = ProfitGreen, modifier = Modifier.size(16.dp))
                        Text(strings.vipFeature1, color = Color.White, style = MaterialTheme.typography.labelSmall)
                    }
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Icon(Icons.Default.Check, contentDescription = null, tint = ProfitGreen, modifier = Modifier.size(16.dp))
                        Text(strings.vipFeature2, color = Color.White, style = MaterialTheme.typography.labelSmall)
                    }
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Icon(Icons.Default.Check, contentDescription = null, tint = ProfitGreen, modifier = Modifier.size(16.dp))
                        Text(strings.vipFeature3, color = Color.White, style = MaterialTheme.typography.labelSmall)
                    }
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Icon(Icons.Default.Check, contentDescription = null, tint = ProfitGreen, modifier = Modifier.size(16.dp))
                        Text(strings.vipFeature4, color = Color.White, style = MaterialTheme.typography.labelSmall)
                    }
                }

                Spacer(modifier = Modifier.height(12.dp))

                // PayPal Active configuration badge
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    modifier =
                        Modifier
                            .fillMaxWidth()
                            .background(Color(0xFF003087).copy(alpha = 0.2f), RoundedCornerShape(6.dp))
                            .border(1.dp, Color(0xFF003087).copy(alpha = 0.5f), RoundedCornerShape(6.dp))
                            .padding(10.dp),
                ) {
                    Icon(
                        Icons.Default.Payment,
                        contentDescription = strings.vipPaypalActive,
                        tint = Color(0xFFFFC439),
                        modifier = Modifier.size(18.dp),
                    )
                    Text(strings.payPalSupported, color = Color.White, style = LabelExtraSmall.copy(fontFamily = FontFamily.Monospace))
                }

                Spacer(modifier = Modifier.height(16.dp))

                // Plan pricing layout
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    // Monthly plan
                    Card(
                        modifier =
                            Modifier
                                .weight(1f)
                                .clickable {
                                    selectedPlan = VipPlan.MONTHLY
                                    showGPayPopup = true
                                },
                        colors = CardDefaults.cardColors(containerColor = CharcoalBackground),
                        border = BorderStroke(1.dp, BorderColor),
                    ) {
                        Column(
                            modifier = Modifier.padding(10.dp),
                            horizontalAlignment = Alignment.CenterHorizontally,
                        ) {
                            Text(
                                strings.monthlyVip,
                                style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                                color = Color.White,
                            )
                            Spacer(modifier = Modifier.height(4.dp))
                            Text(
                                strings.vipPriceMonthly,
                                style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Bold),
                                color = CyberBlue,
                            )
                            Text(strings.vipStandardAccess, style = LabelMicro, color = Color.Gray)
                        }
                    }

                    // Annual Plan
                    Card(
                        modifier =
                            Modifier
                                .weight(1.2f)
                                .clickable {
                                    selectedPlan = VipPlan.ANNUAL
                                    showGPayPopup = true
                                },
                        colors = CardDefaults.cardColors(containerColor = DarkBlue.copy(alpha = 0.5f)),
                        border = BorderStroke(1.dp, CyberBlue),
                    ) {
                        Column(
                            modifier = Modifier.padding(10.dp),
                            horizontalAlignment = Alignment.CenterHorizontally,
                        ) {
                            Text(
                                strings.annualVip,
                                style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                                color = Color.White,
                            )
                            Text(strings.bestValue, style = LabelMicro.copy(fontWeight = FontWeight.Bold), color = ProfitGreen)
                            Spacer(modifier = Modifier.height(4.dp))
                            Text(
                                strings.vipPriceAnnual,
                                style = MaterialTheme.typography.bodyLarge.copy(fontWeight = FontWeight.Bold),
                                color = CyberBlue,
                            )
                            Text(String.format(strings.vipSaveAnnual, annualSavingPercent()), style = LabelMicro, color = Color.Gray)
                        }
                    }

                    // Lifetime plan
                    Card(
                        modifier =
                            Modifier
                                .weight(1f)
                                .clickable {
                                    selectedPlan = VipPlan.LIFETIME
                                    showGPayPopup = true
                                },
                        colors = CardDefaults.cardColors(containerColor = CharcoalBackground),
                        border = BorderStroke(1.dp, BorderColor),
                    ) {
                        Column(
                            modifier = Modifier.padding(10.dp),
                            horizontalAlignment = Alignment.CenterHorizontally,
                        ) {
                            Text(
                                strings.lifetimeElite,
                                style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                                color = Color.White,
                            )
                            Spacer(modifier = Modifier.height(4.dp))
                            Text(
                                strings.vipPriceLifetime,
                                style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Bold),
                                color = CyberBlue,
                            )
                            Text(strings.vipPayOnce, style = LabelMicro, color = Color.Gray)
                        }
                    }
                }

                Spacer(modifier = Modifier.height(16.dp))

                Button(
                    shape = ButtonShape,
                    onClick = {
                        selectedPlan = VipPlan.ANNUAL
                        showGPayPopup = true
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = CyberBlue),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text(strings.activateVipSub, fontWeight = FontWeight.Bold)
                }
            } else {
                // VIP IS ACTIVE GORGEOUS CONTAINER
                Column(
                    modifier =
                        Modifier
                            .fillMaxWidth()
                            .background(ProfitGreen.copy(alpha = 0.1f), RoundedCornerShape(8.dp))
                            .padding(16.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    Icon(Icons.Default.CheckCircle, contentDescription = null, tint = ProfitGreen, modifier = Modifier.size(48.dp))
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        strings.vipInstitutionalUnlocked,
                        color = ProfitGreen,
                        style = MaterialTheme.typography.bodyLarge.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        strings.vipLicenseActive,
                        color = Color.White,
                        style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        strings.vipAllFeaturesActive,
                        color = LightGrayText,
                        style = MaterialTheme.typography.labelSmall,
                        textAlign = TextAlign.Center
                    )
                }
            }
        }
    }
}
