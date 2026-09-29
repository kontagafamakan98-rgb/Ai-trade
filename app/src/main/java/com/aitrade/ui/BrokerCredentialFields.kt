package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun BrokerCredentialFields(
    strings: AppStrings,
    selectedBroker: String,
    accountNumberState: MutableState<String>,
    apiKeyState: MutableState<String>,
    secretKeyState: MutableState<String>,
    customEndpointState: MutableState<String>,
) {
    var accountNumber by accountNumberState
    var apiKey by apiKeyState
    var secretKey by secretKeyState
    var customEndpoint by customEndpointState
    if (selectedBroker.contains(strings.brokerNameIbkr) ||
        selectedBroker.contains(strings.brokerNameMetatrader) ||
        selectedBroker.contains(strings.brokerNameOanda)
    ) {
        OutlinedTextField(
            value = accountNumber,
            onValueChange = { accountNumber = it },
            label = { Text(String.format(strings.brokerAccountIdOf, selectedBroker)) },
            colors =
                OutlinedTextFieldDefaults.colors(
                    focusedTextColor = Color.White,
                    unfocusedTextColor = Color.White,
                    focusedBorderColor = CyberBlue,
                    unfocusedBorderColor = BorderColor,
                ),
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(modifier = Modifier.height(12.dp))
    }

    OutlinedTextField(
        value = apiKey,
        onValueChange = { apiKey = it },
        label = {
            Text(
                if (selectedBroker.contains(strings.brokerNameCustomShort)) {
                    strings.brokerWebhookToken
                } else {
                    String.format(strings.brokerApiKeyOf, selectedBroker)
                },
            )
        },
        colors =
            OutlinedTextFieldDefaults.colors(
                focusedTextColor = Color.White,
                unfocusedTextColor = Color.White,
                focusedBorderColor = CyberBlue,
                unfocusedBorderColor = BorderColor,
            ),
        modifier = Modifier.fillMaxWidth(),
    )
    Spacer(modifier = Modifier.height(12.dp))

    OutlinedTextField(
        value = secretKey,
        onValueChange = { secretKey = it },
        label = {
            Text(
                if (selectedBroker.contains(strings.brokerNameMetatrader)) {
                    strings.brokerPasswordSecret
                } else {
                    String.format(strings.brokerApiSecretOf, selectedBroker)
                },
            )
        },
        colors =
            OutlinedTextFieldDefaults.colors(
                focusedTextColor = Color.White,
                unfocusedTextColor = Color.White,
                focusedBorderColor = CyberBlue,
                unfocusedBorderColor = BorderColor,
            ),
        modifier = Modifier.fillMaxWidth(),
    )
    Spacer(modifier = Modifier.height(12.dp))

    if (selectedBroker.contains(strings.brokerNameCustomShort) ||
        selectedBroker.contains(strings.brokerNameIbkr) ||
        selectedBroker.contains(strings.brokerNameMetatrader)
    ) {
        OutlinedTextField(
            value = customEndpoint,
            onValueChange = { customEndpoint = it },
            label = { Text(strings.serverGatewayUrlLabel) },
            colors =
                OutlinedTextFieldDefaults.colors(
                    focusedTextColor = Color.White,
                    unfocusedTextColor = Color.White,
                    focusedBorderColor = CyberBlue,
                    unfocusedBorderColor = BorderColor,
                ),
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(modifier = Modifier.height(12.dp))
    }
}
