package com.aitrade.ui

import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import com.aitrade.api.AdminProbeCheck
import com.aitrade.api.AdminProbeFailure
import com.aitrade.api.AdminProbeOutcome
import com.aitrade.api.AdminProbeReport
import com.aitrade.api.AdminProbeSection
import com.aitrade.api.AdminProbeSelection
import com.aitrade.data.AdminEndpoint
import com.aitrade.ui.theme.AiTradeTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Écran d'administration : ce qu'il faut pour qu'il ne mente pas.
 *
 * Trois propriétés sont éprouvées ici, et aucune n'est cosmétique :
 *
 * * **sans point d'accès, rien ne part** — la commande est refusée et l'écran le
 *   dit, au lieu d'afficher un rapport ;
 * * **un backend injoignable n'est pas un rapport vide** — « la sonde n'a pas
 *   répondu » et « tout va bien » ne doivent pas se ressembler ;
 * * **l'aller-retour demande confirmation** — il écrit dans la base que le
 *   serveur utilise, et l'écran **nomme** les tables visées avant de les envoyer.
 *   C'est le contrôle qui compte le plus : le reste se voit à l'écran, une
 *   écriture non confirmée ne se voit qu'en base.
 *
 * Les interactions passent par le **texte affiché** plutôt que par des étiquettes
 * de test : c'est ce que l'opérateur voit, et un bouton renommé doit faire échouer
 * le test qui prétend l'utiliser.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34], qualifiers = [TEST_QUALIFIERS])
class AdminSupabaseScreenTest {
    @get:Rule
    val compose = createComposeRule()

    private val configured = AdminEndpoint(baseUrl = "http://10.0.2.2:8000", apiKey = "cle-interne")

    private val failingReport =
        AdminProbeReport(
            sections =
                listOf(
                    AdminProbeSection(
                        title = "2. Tables",
                        ok = false,
                        checks =
                            listOf(
                                AdminProbeCheck(name = "insights", ok = false, detail = "relation absente"),
                                AdminProbeCheck(name = "users", ok = true, detail = "lisible"),
                            ),
                    ),
                ),
            ok = false,
            leftovers = listOf("insights.id=probe-1"),
            selection = AdminProbeSelection(requested = listOf("core"), tables = listOf("insights", "users")),
            writes = null,
            command = "python scripts/check_supabase.py --only core",
        )

    private fun render(
        endpoint: AdminEndpoint = configured,
        state: AdminProbeUiState = AdminProbeUiState(),
        onRunRoundtrip: (List<String>) -> Unit = {},
    ) {
        compose.setContent {
            AiTradeTheme {
                WithEnglishStrings {
                    AdminSupabaseScreen(
                        endpoint = endpoint,
                        state = state,
                        onSaveEndpoint = { _, _ -> },
                        onRunCheck = {},
                        onRunRoundtrip = onRunRoundtrip,
                        onClose = {},
                    )
                }
            }
        }
    }

    @Test
    fun rendersTheProbeForm() {
        render()
        compose.assertTextExists("Supabase probe")
        compose.onNodeWithTag("admin_list").assertExists()
        compose.assertTextExists("RUN READ-ONLY CHECK")
        compose.assertTextExists("RUN ROUND-TRIP")
    }

    @Test
    fun withoutAnEndpointItSaysSoAndRefusesToRun() {
        render(endpoint = AdminEndpoint())
        compose.assertSubstringExists("No backend saved yet")
        compose.onNodeWithText("RUN READ-ONLY CHECK").assertIsNotEnabled()
        compose.onNodeWithText("RUN ROUND-TRIP").assertIsNotEnabled()
    }

    @Test
    fun showsTheReportOfTheBackend() {
        render(state = AdminProbeUiState(outcome = AdminProbeOutcome.Report(failingReport)))
        compose.assertTextExists("At least one check failed")
        compose.assertSubstringExists("insights: relation absente")
        compose.assertSubstringExists("insights.id=probe-1")
        compose.assertSubstringExists("python scripts/check_supabase.py --only core")
    }

    @Test
    fun anUnreachableBackendIsNotAReport() {
        render(
            state =
                AdminProbeUiState(
                    outcome = AdminProbeOutcome.Failed(AdminProbeFailure.UNREACHABLE, "connection refused"),
                ),
        )
        // Le titre est un en-tête de section : l'écran l'affiche en capitales.
        compose.assertTextExists("THE PROBE DID NOT ANSWER")
        compose.assertSubstringExists("connection refused")
        compose.onAllNodesWithText("All checks passed").assertCountEquals(0)
    }

    @Test
    fun theRoundtripAsksForConfirmationAndNamesTheTables() {
        var sent: List<String>? = null
        render(onRunRoundtrip = { tables -> sent = tables })

        compose.onNodeWithTag("admin_roundtrip_tables").performTextInput("core, economy")
        compose.onNodeWithText("RUN ROUND-TRIP").performClick()

        compose.assertTextExists("Write on these tables?")
        compose.assertSubstringExists("core, economy")
        assertNull("rien ne doit partir avant la confirmation", sent)

        compose.onNodeWithText("WRITE").performClick()

        assertEquals(listOf("core", "economy"), sent)
    }

    @Test
    fun theRoundtripCannotBeRunWithoutATable() {
        var sent: List<String>? = null
        render(onRunRoundtrip = { tables -> sent = tables })

        compose.onNodeWithTag("admin_roundtrip_tables").performTextInput("  ,  ")

        compose.onNodeWithText("RUN ROUND-TRIP").assertIsNotEnabled()
        assertNull("aucune table nommée : rien à écrire", sent)
    }
}
