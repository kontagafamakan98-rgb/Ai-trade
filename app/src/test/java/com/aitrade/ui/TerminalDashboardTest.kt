package com.aitrade.ui

import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.performClick
import com.aitrade.ui.theme.AiTradeTheme
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Coquille applicative : barre supérieure, bannière simulateur, onglets.
 *
 * `TerminalDashboard` fournit lui-même les chaînes via `prefs.language` (français
 * par défaut), donc les assertions portent sur les libellés **français**.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], qualifiers = [TEST_QUALIFIERS])
class TerminalDashboardTest {
    @get:Rule
    val compose = createComposeRule()

    private lateinit var viewModel: TradingViewModel

    @Before
    fun setUp() {
        viewModel = newTestViewModel()
    }

    private fun render() {
        compose.setContent {
            AiTradeTheme { TerminalDashboard(viewModel) }
        }
    }

    @Test
    fun rendersAllFiveTabs() {
        render()
        listOf(
            "Terminal",
            "Analyses IA",
            "Gestion Risque",
            "Chat IA & Scan",
            "Synchro Cloud & VIP",
        ).forEach { tab -> compose.assertTextExists(tab) }
    }

    @Test
    fun alwaysShowsSimulatorBanner() {
        render()
        compose.assertTextExists(
            "SIMULATEUR LOCAL : prix, actualités et backtest générés. " +
                "Aucun ordre réel n'est exécuté.",
        )
    }

    @Test
    fun opensTerminalTabByDefault() {
        render()
        compose.assertTextExists("MOTEUR ARRÊTÉ")
    }

    @Test
    fun switchingTabShowsRiskScreen() {
        render()
        compose.onNodeWithContentDescription("Gestion Risque").performClick()
        compose.assertTextExists("Paramètres de Gestion du Risque")
    }
}
