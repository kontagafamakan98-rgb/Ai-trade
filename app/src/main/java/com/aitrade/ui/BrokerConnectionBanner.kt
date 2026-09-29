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
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.aitrade.R
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun BrokerConnectionBanner(viewModel: TradingViewModel) {
    val strings = LocalAppStrings.current

    Card(
        modifier =
            Modifier
                .fillMaxWidth()
                .padding(top = 8.dp),
        colors = CardDefaults.cardColors(containerColor = CyberBlue.copy(alpha = 0.08f)),
        border = BorderStroke(1.dp, CyberBlue.copy(alpha = 0.4f)),
    ) {
        Column(modifier = Modifier.padding(14.dp)) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Icon(
                    imageVector = Icons.Default.Info,
                    contentDescription = null,
                    tint = CyberBlue,
                    modifier = Modifier.size(18.dp),
                )
                Text(
                    stringResource(R.string.broker_demo_available_title),
                    color = Color.White,
                    style = MaterialTheme.typography.labelLarge.copy(fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace),
                )
            }
            Spacer(modifier = Modifier.height(4.dp))
            Text(
                stringResource(R.string.broker_demo_available_body),
                color = LightGrayText,
                style = MaterialTheme.typography.labelSmall,
            )
            Spacer(modifier = Modifier.height(12.dp))
            Button(
                onClick = {
                    viewModel.saveBrokerCredentials(
                        brokerName = strings.brokerNameAlpaca,
                        apiKey = "",
                        secretKey = "",
                        paperMode = true,
                        accountNumber = "PA-DEMO-8942",
                        customEndpoint = "",
                    )
                },
                colors = ButtonDefaults.buttonColors(containerColor = CyberBlue),
                shape = RoundedCornerShape(6.dp),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Icon(
                    imageVector = Icons.Default.CloudSync,
                    contentDescription = null,
                    modifier = Modifier.size(16.dp),
                    tint = Color.White,
                )
                Spacer(modifier = Modifier.width(6.dp))
                Text(
                    stringResource(R.string.broker_activate_demo_btn),
                    style = MaterialTheme.typography.labelSmall.copy(fontWeight = FontWeight.Bold),
                    color = Color.White
                )
            }
        }
    }
}
