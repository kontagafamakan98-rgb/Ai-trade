package com.aitrade.ui

import androidx.compose.ui.test.junit4.createComposeRule
import com.aitrade.ui.theme.AiTradeTheme
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** Écran 1 (Terminal) : contrôles du moteur, surveillance, journal et cartes analytiques. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], qualifiers = [TEST_QUALIFIERS])
class TerminalScreenTest {
    @get:Rule
    val compose = createComposeRule()

    private lateinit var viewModel: TradingViewModel

    @Before
    fun setUp() {
        viewModel = newTestViewModel()
    }

    private fun render() {
        compose.setContent {
            AiTradeTheme {
                WithEnglishStrings {
                    TerminalScreen(
                        viewModel = viewModel,
                        prefs = viewModel.preferences.value,
                        signals = emptyList(),
                        logs = emptyList(),
                    )
                }
            }
        }
    }

    @Test
    fun rendersEngineControlsAndWatchlist() {
        render()
        compose.assertTextExists("ENGINE STOPPED")
        compose.assertTextExists("START AI BOT")
        compose.assertTextExists("Watchlist Tickers (Select to View Chart)")
    }

    @Test
    fun rendersEmptySignalsState() {
        render()
        compose.assertTextExists("Detected Trading Signals")
        compose.assertTextExists("No signals generated yet.")
    }

    @Test
    fun rendersEveryAnalyticalCard() {
        render()
        listOf(
            "execution_mode_control_card",
            "portfolio_performance_card",
            "quant_backtest_card",
            "multi_agent_research_card",
            "system_health_card",
            "adaptive_learning_card",
        ).forEach { cardTag ->
            compose.scrollToCard("terminal_list", cardTag)
        }
    }

    @Test
    fun rendersLogSectionTitle() {
        render()
        compose.scrollToText("terminal_list", "Engine Execution Logs")
        compose.assertTextExists("Engine Execution Logs")
    }
}
