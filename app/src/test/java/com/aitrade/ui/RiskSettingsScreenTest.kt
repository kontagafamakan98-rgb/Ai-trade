package com.aitrade.ui

import androidx.compose.ui.test.assertExists
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import com.aitrade.ui.theme.AiTradeTheme
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/** Écran 3 (Gestion Risque) : langue, compte démo, risque, multi-broker, zone de danger. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], qualifiers = [TEST_QUALIFIERS])
class RiskSettingsScreenTest {
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
                    RiskSettingsScreen(viewModel = viewModel, prefs = viewModel.preferences.value)
                }
            }
        }
    }

    @Test
    fun rendersHeadSections() {
        render()
        compose.assertTextExists("Risk Management Settings")
        compose.assertTextExists("Application Language")
        compose.onNodeWithTag("risk_list").assertExists()
    }

    @Test
    fun rendersDangerZoneAtTheBottom() {
        render()
        compose.scrollToText("risk_list", "Danger Zone")
        compose.assertTextExists("Danger Zone")
        compose.assertTextExists("RESET ENTIRE TRADING DATA")
    }
}
