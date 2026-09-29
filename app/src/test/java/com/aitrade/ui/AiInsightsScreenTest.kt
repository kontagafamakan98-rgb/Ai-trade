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

/** Écran 2 (AI Insights) : analyse Gemini et insights. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], qualifiers = [TEST_QUALIFIERS])
class AiInsightsScreenTest {
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
                    AiInsightsScreen(
                        viewModel = viewModel,
                        prefs = viewModel.preferences.value,
                        insights = emptyList(),
                    )
                }
            }
        }
    }

    @Test
    fun rendersAnalysisCardAndRunButton() {
        render()
        compose.assertTextExists("Geopolitical & Sentiment AI Insights")
        compose.assertTextExists("RUN GEMINI ANALYSIS")
    }

    @Test
    fun rendersIdleStateMessage() {
        render()
        compose.assertTextExists("No insights generated yet.")
    }

    @Test
    fun rendersScrollableRoot() {
        render()
        compose.onNodeWithTag("insights_list").assertExists()
    }
}
