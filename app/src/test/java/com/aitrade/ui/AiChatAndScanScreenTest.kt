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

/** Écran 4 (Chat IA & Scan) : paramètres Gemini et fil de discussion. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], qualifiers = [TEST_QUALIFIERS])
class AiChatAndScanScreenTest {
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
                WithEnglishStrings { AiChatAndScanScreen(viewModel) }
            }
        }
    }

    @Test
    fun rendersGeminiParametersSection() {
        render()
        compose.assertTextExists("Gemini AI Parameters")
        compose.onNodeWithTag("chat_list").assertExists()
    }

    @Test
    fun rendersWelcomeMessageInChatFeed() {
        render()
        compose.scrollToListSubstring("chat_list", "Welcome to AI Trade Terminal!")
        compose.assertSubstringExists("Welcome to AI Trade Terminal!")
    }
}
