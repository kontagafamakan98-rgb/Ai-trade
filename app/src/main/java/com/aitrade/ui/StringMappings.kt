package com.aitrade.ui

import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalConfiguration
import androidx.core.os.ConfigurationCompat
import java.util.Locale
import kotlin.math.roundToInt

/*
 * Codes stables -> libellés localisés.
 *
 * Les valeurs « EXECUTED », « STRONG_BUY », « monthly »… viennent de la base,
 * du moteur ou du pipeline d'exécution : les traduire à la source casserait le
 * filtrage et les comparaisons. La traduction se fait donc au dernier moment,
 * ici, et jamais en comparant deux textes traduits entre eux.
 */

/** Statut d'un signal (`Signal.status`). */
fun statusLabel(strings: AppStrings, code: String): String = when (code) {
    "EXECUTED" -> strings.statusExecuted
    "PENDING" -> strings.statusPending
    "BLOCKED_RISK" -> strings.statusBlockedRisk
    "CLOSED" -> strings.statusClosed
    else -> code
}

/** Sens d'un ordre (`Signal.direction`, `BacktestTradeLog.direction`). */
fun directionLabel(strings: AppStrings, code: String): String = if (code == "BUY") strings.buy else strings.sell

/** Action de consensus du comité multi-agents (`MultiAgentResearchReport`). */
fun actionLabel(strings: AppStrings, code: String): String = when {
    code.contains("STRONG_BUY") -> strings.actionStrongBuy
    code.contains("BUY") -> strings.buy
    code.contains("STRONG_SELL") -> strings.actionStrongSell
    code.contains("SELL") -> strings.sell
    else -> strings.actionNeutralHold
}

/** Niveau de conviction d'un rapport d'agent (`AgentReport.conviction`). */
fun convictionLabel(strings: AppStrings, code: String): String = when (code) {
    "HIGH" -> strings.convictionHigh
    "LOW" -> strings.convictionLow
    else -> strings.convictionMedium
}

/**
 * Plans VIP payants.
 *
 * [amountUsd] est le montant réellement facturé : il reste en ASCII à point
 * décimal, indépendamment de la langue, car il part dans l'URL PayPal.
 */
enum class VipPlan(val amountUsd: String) {
    MONTHLY("19.99"),
    ANNUAL("99.99"),
    LIFETIME("199.99"),
}

/**
 * Écart réel entre l'abonnement mensuel et l'annuel, en pourcentage.
 *
 * Calculé depuis [VipPlan.amountUsd], jamais écrit en dur : la fiche tarifaire
 * annonçait « 60 % » alors que l'écart vaut `(19,99 x 12 - 99,99) / (19,99 x 12)`
 * = 58,3 %, et le chiffre serait devenu franchement faux au premier changement de
 * prix. Une remise se calcule, elle ne se déclare pas.
 */
fun annualSavingPercent(): Int {
    val monthly = VipPlan.MONTHLY.amountUsd.toDouble()
    val annual = VipPlan.ANNUAL.amountUsd.toDouble()
    val fullYear = monthly * 12
    return ((fullYear - annual) / fullYear * 100).roundToInt()
}

/** Nom commercial du plan (sert aussi d'intitulé PayPal). */
fun vipPlanTitle(strings: AppStrings, plan: VipPlan): String = when (plan) {
    VipPlan.MONTHLY -> strings.vipPlanMonthly
    VipPlan.ANNUAL -> strings.vipPlanAnnual
    VipPlan.LIFETIME -> strings.vipPlanLifetime
}

/** Prix affiché avec sa période (« 19,99 $ / mois »). */
fun vipPlanRecurringPrice(strings: AppStrings, plan: VipPlan): String = when (plan) {
    VipPlan.MONTHLY -> strings.vipPeriodMonthly
    VipPlan.ANNUAL -> strings.vipPeriodAnnual
    VipPlan.LIFETIME -> strings.vipPriceLifetime
}

/**
 * Locale de la langue affichée.
 *
 * `ProvideAppStrings` localise le contexte sans toucher à la locale par défaut
 * du processus : sans cette aide, `SimpleDateFormat` formaterait les dates dans
 * la langue du téléphone et non dans celle choisie dans l'application.
 */
@Composable
fun rememberAppLocale(): Locale {
    val configuration = LocalConfiguration.current
    return remember(configuration) {
        ConfigurationCompat.getLocales(configuration)[0] ?: Locale.getDefault()
    }
}
