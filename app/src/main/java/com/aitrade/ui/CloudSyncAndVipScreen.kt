package com.aitrade.ui

import androidx.compose.animation.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

@Composable
fun CloudSyncAndVipScreen(viewModel: TradingViewModel) {
    val strings = LocalAppStrings.current
    val email by viewModel.userEmail.collectAsStateWithLifecycle()
    val isConnected by viewModel.isFirebaseConnected.collectAsStateWithLifecycle()
    val isSynced by viewModel.isFirestoreSynced.collectAsStateWithLifecycle()
    val isVip by viewModel.isVipUser.collectAsStateWithLifecycle()
    val signals by viewModel.signals.collectAsStateWithLifecycle()

    val showGPayPopupState = remember { mutableStateOf(false) }
    var showGPayPopup by showGPayPopupState
    val selectedPlanState = remember { mutableStateOf(VipPlan.MONTHLY) }
    val isUpgradingProgressState = remember { mutableStateOf(false) }
    var legalDocument by remember { mutableStateOf<LegalDocument?>(null) }

    if (showGPayPopup) {
        GooglePayUpgradeDialog(
            strings = strings,
            viewModel = viewModel,
            showGPayPopupState = showGPayPopupState,
            selectedPlanState = selectedPlanState,
            isUpgradingProgressState = isUpgradingProgressState,
        )
    }

    legalDocument?.let { document ->
        LegalDocumentDialog(document = document, onDismiss = { legalDocument = null })
    }

    LazyColumn(
        modifier =
            Modifier
                .fillMaxSize()
                .padding(16.dp)
                .testTag("cloud_list"),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        // --- 1. GOOGLE SIGN IN & FIREBASE AUTH ---
        item {
            FirebaseAuthCard(strings = strings, email = email, isConnected = isConnected, viewModel = viewModel)
        }

        // --- 2. FIRESTORE DATABASE CLOUD SYNC ---
        item {
            FirestoreSyncCard(
                strings = strings,
                signals = signals,
                isSynced = isSynced,
                isConnected = isConnected,
                viewModel = viewModel,
            )
        }

        // --- 3. HIGH-CONVERSION VIP MONETIZATION PAYWALL ---
        item {
            VipPaywallCard(
                strings = strings,
                isVip = isVip,
                showGPayPopupState = showGPayPopupState,
                selectedPlanState = selectedPlanState,
            )
        }

        // --- 4. LEGAL NOTICES (RGPD / CGU) ---
        item {
            LegalLinksCard(onOpen = { legalDocument = it })
        }
    }
}
