package com.aitrade.ui

import android.app.Application
import androidx.compose.runtime.Composable
import androidx.compose.ui.test.ComposeContentTestRule
import androidx.compose.ui.test.assertExists
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onFirst
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performScrollToNode
import androidx.test.core.app.ApplicationProvider

/**
 * Support commun aux tests d'interface.
 *
 * Les écrans sont rendus **tels quels**, branchés sur un `TradingViewModel` réel
 * adossé à l'`Application` Robolectric et à une base Room locale : aucun
 * doublure ni refactor du code de production n'est nécessaire pour les tester.
 *
 * `TEST_QUALIFIERS` est une fenêtre volontairement haute : davantage d'items
 * sont composés d'emblée dans les `LazyColumn` (les cartes sont hautes, un
 * écran de téléphone n'en montre qu'une ou deux).
 */
const val TEST_QUALIFIERS = "w400dp-h2000dp"

/** Vue modèle réelle, sur une base Room locale en environnement Robolectric. */
fun newTestViewModel(): TradingViewModel = TradingViewModel(ApplicationProvider.getApplicationContext<Application>())

/**
 * Force l'anglais : les écrans lisent leurs libellés depuis les ressources
 * Android, on fige donc la langue pour des assertions de texte déterministes.
 */
@Composable
fun WithEnglishStrings(content: @Composable () -> Unit) {
    ProvideAppStrings(languageCode = "en", content = content)
}

/** Un texte est présent **au moins une fois** (plusieurs occurrences possibles : barre + carte). */
fun ComposeContentTestRule.assertTextExists(text: String) {
    onAllNodesWithText(text).onFirst().assertExists()
}

/**
 * Fait défiler la liste racine jusqu'à la carte portant [cardTag], puis vérifie
 * qu'elle existe. Sans cela, seules les cartes déjà composées seraient vues.
 */
fun ComposeContentTestRule.scrollToCard(
    listTag: String,
    cardTag: String,
) {
    onNodeWithTag(listTag).performScrollToNode(hasTestTag(cardTag))
    onNodeWithTag(cardTag).assertExists()
}

/** Fait défiler la liste racine jusqu'à un texte donné (cartes sans testTag). */
fun ComposeContentTestRule.scrollToText(
    listTag: String,
    text: String,
) {
    onNodeWithTag(listTag).performScrollToNode(hasText(text))
}

/** Même chose, mais sur un fragment de texte (message de chat, libellés tronqués). */
fun ComposeContentTestRule.scrollToListSubstring(
    listTag: String,
    substring: String,
) {
    onNodeWithTag(listTag).performScrollToNode(hasText(substring, substring = true))
}

/** Un fragment de texte est présent au moins une fois. */
fun ComposeContentTestRule.assertSubstringExists(substring: String) {
    onAllNodesWithText(substring, substring = true).onFirst().assertExists()
}
