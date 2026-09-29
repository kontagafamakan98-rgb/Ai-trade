package com.aitrade.ui

import androidx.activity.compose.BackHandler
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
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.aitrade.R
import com.aitrade.engine.*
import com.aitrade.ui.theme.*
import java.util.*

val DarkBlue = Color(0xFF0C2340)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TerminalDashboard(viewModel: TradingViewModel) {
    val prefs by viewModel.preferences.collectAsStateWithLifecycle()
    val signals by viewModel.signals.collectAsStateWithLifecycle()
    val logs by viewModel.logs.collectAsStateWithLifecycle()
    val insights by viewModel.insights.collectAsStateWithLifecycle()
    val isLoopRunning by viewModel.isLoopRunning.collectAsStateWithLifecycle()
    val currentEquity by viewModel.currentEquity.collectAsStateWithLifecycle()

    ProvideAppStrings(prefs.language) {
        val strings = LocalAppStrings.current
        val credentials by viewModel.credentials.collectAsStateWithLifecycle()
        var selectedTab by remember { mutableStateOf(0) }
        var showLangMenu by remember { mutableStateOf(false) }
        var adminOpen by remember { mutableStateOf(false) }
        val adminEndpoint by viewModel.adminEndpoint.collectAsStateWithLifecycle()
        val adminProbe by viewModel.adminProbe.collectAsStateWithLifecycle()

        if (adminOpen) {
            // Route plein écran : l'écran d'administration remplace le tableau de
            // bord au lieu de s'ajouter à la barre de navigation, qui porte déjà
            // cinq destinations — ce que Material recommande au maximum.
            BackHandler { adminOpen = false }
            AdminSupabaseScreen(
                endpoint = adminEndpoint,
                state = adminProbe,
                onSaveEndpoint = { baseUrl, apiKey -> viewModel.saveAdminEndpoint(baseUrl, apiKey) },
                onRunCheck = { viewModel.runAdminCheck() },
                onRunRoundtrip = { tables -> viewModel.runAdminRoundtrip(tables) },
                onClose = { adminOpen = false },
            )
            return@ProvideAppStrings
        }

        val tabs =
            listOf(
                strings.tabTerminal,
                strings.tabInsights,
                strings.tabRisk,
                strings.tabChat,
                strings.tabCloud,
            )
        val icons =
            listOf(
                Icons.Default.TrendingUp,
                Icons.Default.Hub,
                Icons.Default.Shield,
                Icons.Default.AutoAwesome,
                Icons.Default.CloudSync,
            )

        Scaffold(
            topBar = {
                TopAppBar(
                    title = {
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            Text(
                                strings.appTitle,
                                style = MaterialTheme.typography.titleLarge,
                                color = LightGrayText,
                            )
                            // Live indicator badge
                            Box(
                                modifier =
                                    Modifier
                                        .size(8.dp)
                                        .clip(CircleShape)
                                        .background(if (isLoopRunning) ProfitGreen else DisabledText),
                            )
                        }
                    },
                    actions = {
                        val currentLang = AppLanguage.fromCode(prefs.language)
                        Box {
                            TextButton(shape = ButtonShape, onClick = { showLangMenu = true }) {
                                Row(
                                    verticalAlignment = Alignment.CenterVertically,
                                    horizontalArrangement = Arrangement.spacedBy(4.dp),
                                ) {
                                    Text(currentLang.code.uppercase(), color = CyberBlue, fontWeight = FontWeight.Bold)
                                    Icon(
                                        Icons.Default.ArrowDropDown,
                                        contentDescription = null,
                                        tint = CyberBlue,
                                        modifier = Modifier.size(16.dp),
                                    )
                                }
                            }
                            DropdownMenu(
                                expanded = showLangMenu,
                                onDismissRequest = { showLangMenu = false },
                                modifier = Modifier.background(DarkCard),
                            ) {
                                AppLanguage.entries.forEach { lang ->
                                    DropdownMenuItem(
                                        text = {
                                            Row(
                                                horizontalArrangement = Arrangement.spacedBy(8.dp),
                                                verticalAlignment = Alignment.CenterVertically,
                                            ) {
                                                Text(
                                                    lang.code.uppercase(),
                                                    color = CyberBlue,
                                                    style = MaterialTheme.typography.labelMedium.copy(fontWeight = FontWeight.Bold),
                                                )
                                                Text(lang.displayName, color = LightGrayText)
                                            }
                                        },
                                        onClick = {
                                            viewModel.updateLanguage(lang.code)
                                            showLangMenu = false
                                        },
                                    )
                                }
                            }
                        }
                        val badgeBg =
                            when {
                                !credentials.paperMode -> LossRed
                                credentials.apiKey.isNotEmpty() -> ProfitGreen
                                else -> CyberBlue
                            }
                        val badgeLabel =
                            when {
                                !credentials.paperMode -> stringResource(R.string.badge_live_account)
                                credentials.apiKey.isNotEmpty() -> stringResource(R.string.badge_real_demo)
                                else -> stringResource(R.string.badge_paper_sim)
                            }

                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(6.dp),
                            modifier = Modifier.padding(end = 12.dp),
                        ) {
                            Box(
                                modifier =
                                    Modifier
                                        .background(badgeBg, RoundedCornerShape(4.dp))
                                        .padding(horizontal = 6.dp, vertical = 2.dp),
                            ) {
                                Text(
                                    badgeLabel,
                                    color = OnAccent,
                                    style = MaterialTheme.typography.labelSmall,
                                    fontWeight = FontWeight.Bold,
                                )
                            }
                            Text(
                                "${strings.paperBalance}: \$${String.format("%,.2f", currentEquity)}",
                                color = ProfitGreen,
                                style = NumericTextStyle,
                            )
                        }
                    },
                    colors =
                        TopAppBarDefaults.topAppBarColors(
                            containerColor = CharcoalBackground,
                        ),
                )
            },
            bottomBar = {
                NavigationBar(
                    containerColor = CharcoalBackground,
                    tonalElevation = 8.dp,
                    modifier = Modifier.navigationBarsPadding(),
                ) {
                    tabs.forEachIndexed { index, label ->
                        NavigationBarItem(
                            selected = selectedTab == index,
                            onClick = { selectedTab = index },
                            label = { Text(label, style = MaterialTheme.typography.labelSmall) },
                            icon = { Icon(icons[index], contentDescription = label) },
                            colors =
                                NavigationBarItemDefaults.colors(
                                    selectedIconColor = CyberBlue,
                                    unselectedIconColor = MutedText,
                                    selectedTextColor = CyberBlue,
                                    unselectedTextColor = MutedText,
                                    indicatorColor = CharcoalBackground,
                                ),
                        )
                    }
                }
            },
            containerColor = CharcoalBackground,
        ) { padding ->
            Column(
                modifier =
                    Modifier
                        .padding(padding)
                        .fillMaxSize(),
            ) {
                // Bannière permanente : cette application est un SIMULATEUR
                // local (prix/actualités/backtest générés), pas un terminal
                // connecté à de vrais flux ni un exécuteur d'ordres réels.
                Box(
                    modifier =
                        Modifier
                            .fillMaxWidth()
                            .background(WarningAmber.copy(alpha = 0.12f))
                            .padding(horizontal = Spacing.lg, vertical = Spacing.sm),
                ) {
                    Text(
                        text = stringResource(R.string.simulator_notice),
                        color = WarningAmber,
                        style = MaterialTheme.typography.labelSmall,
                    )
                }
                when (selectedTab) {
                    0 -> TerminalScreen(viewModel, prefs, signals, logs, onOpenAdmin = { adminOpen = true })
                    1 -> AiInsightsScreen(viewModel, prefs, insights)
                    2 -> RiskSettingsScreen(viewModel, prefs)
                    3 -> AiChatAndScanScreen(viewModel)
                    4 -> CloudSyncAndVipScreen(viewModel)
                }
            }
        }
    }
}
