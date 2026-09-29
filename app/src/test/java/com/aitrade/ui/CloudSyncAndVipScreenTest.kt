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

/** Écran 5 (Cloud Sync & VIP) : authentification, synchronisation et paywall. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], qualifiers = [TEST_QUALIFIERS])
class CloudSyncAndVipScreenTest {
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
                WithEnglishStrings { CloudSyncAndVipScreen(viewModel) }
            }
        }
    }

    @Test
    fun rendersAuthAndSyncSections() {
        render()
        compose.assertTextExists("Firebase Authentication")
        compose.assertTextExists("Firestore Database Sync")
        compose.onNodeWithTag("cloud_list").assertExists()
    }

    @Test
    fun rendersVipPricingPlans() {
        render()
        compose.scrollToText("cloud_list", "Elite Premium VIP License")
        compose.assertTextExists("Elite Premium VIP License")
        compose.assertTextExists("Monthly VIP")
        compose.assertTextExists("BEST VALUE")
        compose.assertTextExists("Lifetime Elite")
    }

    /**
     * Les documents légaux sont en français dans **toutes** les langues
     * (ressources déclarées non traduisibles) : les libellés assertés ici sont
     * donc les mêmes que l'écran soit rendu en anglais ou non.
     */
    @Test
    fun rendersLegalNotices() {
        render()
        compose.scrollToText("cloud_list", "Informations légales")
        compose.assertTextExists("Informations légales")
        compose.assertTextExists("Confidentialité (RGPD)")
        compose.assertTextExists("Conditions générales (CGU)")
    }
}
