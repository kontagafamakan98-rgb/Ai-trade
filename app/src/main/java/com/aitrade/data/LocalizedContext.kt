package com.aitrade.data

import android.content.Context
import android.content.res.Configuration
import java.util.Locale

/**
 * Contexte dont les ressources pointent vers [code] (`fr`, `en`, `zh`…).
 *
 * Android résout alors `res/values-<code>/strings.xml`, avec repli automatique
 * sur `res/values/strings.xml` (anglais) si la traduction n'existe pas. La
 * locale par défaut du processus n'est **pas** modifiée : seule la lecture des
 * ressources suit la langue choisie, les formats implicites restent inchangés.
 *
 * Vit ici, et non dans `ui/`, parce que les deux couches en ont besoin : les
 * données de démonstration de [TradingRepository] sont des ressources, donc
 * lues dans la langue choisie, exactement comme les libellés des écrans. Le
 * sens de la dépendance reste celui du projet : `ui/` s'appuie sur `data/`.
 */
internal fun Context.localizedTo(code: String): Context {
    val locale = Locale.forLanguageTag(code)
    val configuration = Configuration(resources.configuration)
    configuration.setLocale(locale)
    return createConfigurationContext(configuration)
}
