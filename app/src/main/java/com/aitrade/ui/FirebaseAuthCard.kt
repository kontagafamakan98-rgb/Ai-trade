package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
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
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun FirebaseAuthCard(
    strings: AppStrings,
    email: String?,
    isConnected: Boolean,
    viewModel: TradingViewModel,
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
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    Icon(Icons.Default.AccountCircle, contentDescription = null, tint = CyberBlue, modifier = Modifier.size(32.dp))
                    Text(
                        strings.firebaseAuthTitle,
                        color = Color.White,
                        style = MaterialTheme.typography.bodyLarge.copy(fontWeight = FontWeight.Bold),
                    )
                }
                Box(
                    modifier =
                        Modifier
                            .background(if (isConnected) ProfitGreen else Color.Gray, RoundedCornerShape(4.dp))
                            .padding(horizontal = 6.dp, vertical = 2.dp),
                ) {
                    Text(
                        if (isConnected) strings.connectedStatusLabel else strings.disconnectedStatusLabel,
                        color = Color.White,
                        style = LabelTiny.copy(fontWeight = FontWeight.Bold),
                    )
                }
            }

            Spacer(modifier = Modifier.height(12.dp))

            if (email == null) {
                Text(
                    strings.firebaseAuthNote,
                    color = LightGrayText,
                    style = MaterialTheme.typography.labelMedium,
                )
                Spacer(modifier = Modifier.height(16.dp))
                Button(
                    shape = ButtonShape,
                    onClick = {
                        viewModel.signInWithGoogle("Kontagafamakan98@gmail.com")
                    },
                    colors = ButtonDefaults.buttonColors(containerColor = Color.White),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text("G", color = Color.Black, style = MaterialTheme.typography.titleMedium.copy(fontWeight = FontWeight.Bold))
                        Text(strings.signInWithGoogle, color = Color.Black, fontWeight = FontWeight.Bold)
                    }
                }
            } else {
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                    modifier =
                        Modifier
                            .fillMaxWidth()
                            .background(DarkBlue.copy(alpha = 0.2f), RoundedCornerShape(8.dp))
                            .padding(12.dp),
                ) {
                    Box(
                        modifier =
                            Modifier
                                .size(40.dp)
                                .clip(CircleShape)
                                .background(CyberBlue),
                        contentAlignment = Alignment.Center,
                    ) {
                        Text("K", color = Color.White, style = MaterialTheme.typography.titleLarge.copy(fontWeight = FontWeight.Bold))
                    }
                    Column {
                        Text(
                            "Kontagafamakan",
                            color = Color.White,
                            style = MaterialTheme.typography.titleSmall.copy(fontWeight = FontWeight.Bold),
                        )
                        Text(email ?: "", color = LightGrayText, style = MaterialTheme.typography.labelMedium)
                    }
                }

                Spacer(modifier = Modifier.height(12.dp))
                OutlinedButton(
                    shape = ButtonShape,
                    onClick = { viewModel.signOut() },
                    border = BorderStroke(1.dp, LossRed),
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = LossRed),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text(strings.signOutFromFirebase)
                }
            }
        }
    }
}
