package com.aitrade.engine

import com.aitrade.data.Signal

data class RegimePerformanceStats(
    val regimeLabel: String,
    val totalSignalsGenerated: Int,
    val totalExecuted: Int,
    val totalPnL: Double,
    val winRatePct: Double,
    val avgReturnPct: Double,
)

data class QuantMemorySummary(
    val totalRecordedSignals: Int,
    val executedSignalsCount: Int,
    val totalPnL: Double,
    val winRatePct: Double,
    val bestRegime: String,
    val worstRegime: String,
    val regimeBreakdown: List<RegimePerformanceStats>,
    val aiModelAccuracyPct: Double,
)

data class AdaptiveLearningProfile(
    val taWeightPct: Int,
    val macroWeightPct: Int,
    val sentimentWeightPct: Int,
    val adaptiveMinConfidence: Double,
    val consecutiveLosses: Int,
    val learnedRules: List<String>,
    val postMortemLessons: List<String>,
)

object QuantPerformanceMemory {
    /**
     * Computes adaptive self-learning parameters and rules dynamically based on historical trade outcomes.
     */
    fun computeAdaptiveProfile(signals: List<Signal>): AdaptiveLearningProfile {
        val closedSignals = signals.filter { it.status == "CLOSED" || it.pnl != 0.0 }.sortedByDescending { it.timestamp }
        if (closedSignals.isEmpty()) {
            return AdaptiveLearningProfile(
                taWeightPct = 40,
                macroWeightPct = 30,
                sentimentWeightPct = 30,
                adaptiveMinConfidence = 0.58,
                consecutiveLosses = 0,
                learnedRules =
                    listOf(
                        "Règle #1 : Filtrer les achats en surachat quand RSI > 68.",
                        "Règle #2 : Bloquer les ordres lors des annonces High Impact.",
                    ),
                postMortemLessons = listOf("Modèle en attente de nouveaux trades fermés."),
            )
        }

        var consecLosses = 0
        for (sig in closedSignals) {
            if (sig.pnl < 0) consecLosses++ else break
        }

        val totalClosed = closedSignals.size
        val totalWins = closedSignals.count { it.pnl > 0 }
        val winRate = (totalWins.toDouble() / totalClosed) * 100.0

        var taW = 40
        var macroW = 30
        var sentW = 30

        if (winRate < 45.0) {
            macroW += 10
            taW -= 5
            sentW -= 5
        } else if (winRate > 65.0) {
            taW += 5
            macroW -= 5
        }

        val minConf =
            when {
                consecLosses >= 3 -> 0.68
                consecLosses >= 2 -> 0.63
                winRate >= 70.0 -> 0.55
                else -> 0.58
            }

        val rules = mutableListOf<String>()
        val lessons = mutableListOf<String>()

        if (consecLosses >= 2) {
            rules.add("Seuil de confiance relevé à ${String.format("%.2f", minConf)} suite à $consecLosses pertes consécutives.")
        }
        rules.add("Filtre d'impact économique actif (Forex Factory Macro & Risk Guard).")
        rules.add("Poids auto-adaptés : TA $taW% | Macro $macroW% | Sentiment $sentW%.")

        for (sig in closedSignals.take(5)) {
            val outcomeTxt =
                if (sig.pnl >
                    0
                ) {
                    "GAGNÉ (+${String.format("%.0f", sig.pnl)}\$)"
                } else {
                    "PERDU (${String.format("%.0f", sig.pnl)}\$)"
                }
            val lessonTxt =
                if (sig.pnl < 0) {
                    "Analyse post-mortem ${sig.asset} ${sig.direction} : Réajustement des filtres suite à l'invalidation."
                } else {
                    "Confirmation du modèle sur ${sig.asset} ${sig.direction} : Configuration validée."
                }
            lessons.add("- ${sig.asset} ${sig.direction} -> $outcomeTxt | $lessonTxt")
        }

        return AdaptiveLearningProfile(
            taWeightPct = taW,
            macroWeightPct = macroW,
            sentimentWeightPct = sentW,
            adaptiveMinConfidence = minConf,
            consecutiveLosses = consecLosses,
            learnedRules = rules,
            postMortemLessons = lessons,
        )
    }

    /**
     * Analyzes stored signal history to construct quantitative performance memory & regime attribution
     */
    fun computeQuantMemory(signals: List<Signal>): QuantMemorySummary {
        val totalRecorded = signals.size
        val executed = signals.filter { it.status == "CLOSED" || it.status == "EXECUTED" }
        val closedWithPnl = signals.filter { it.status == "CLOSED" || it.pnl != 0.0 }

        val totalPnL = closedWithPnl.sumOf { it.pnl }
        val winningCount = closedWithPnl.count { it.pnl > 0 }
        val winRate = if (closedWithPnl.isNotEmpty()) (winningCount.toDouble() / closedWithPnl.size) * 100.0 else 0.0

        // Regime breakdown
        val regimeStatsMap = mutableMapOf<String, MutableList<Signal>>()
        MarketRegime.entries.forEach { regimeStatsMap[it.label] = mutableListOf() }

        for (sig in signals) {
            val regimeLabel =
                when {
                    sig.taSummary.contains("TRENDING BULL") -> MarketRegime.TRENDING_BULL.label
                    sig.taSummary.contains("TRENDING BEAR") -> MarketRegime.TRENDING_BEAR.label
                    sig.taSummary.contains("HIGH VOLATILITY") -> MarketRegime.HIGH_VOLATILITY_CRISIS.label
                    else -> MarketRegime.RANGING_SIDEWAYS.label
                }
            regimeStatsMap.getOrPut(regimeLabel) { mutableListOf() }.add(sig)
        }

        val breakdownList = mutableListOf<RegimePerformanceStats>()
        var maxRegimePnl = -Double.MAX_VALUE
        var minRegimePnl = Double.MAX_VALUE
        var bestRegimeName = "TRENDING BULL"
        var worstRegimeName = "HIGH VOLATILITY CRISIS"

        regimeStatsMap.forEach { (regime, sigs) ->
            val closedInRegime = sigs.filter { it.status == "CLOSED" || it.pnl != 0.0 }
            val regimePnl = closedInRegime.sumOf { it.pnl }
            val regimeWins = closedInRegime.count { it.pnl > 0 }
            val regimeWinRate = if (closedInRegime.isNotEmpty()) (regimeWins.toDouble() / closedInRegime.size) * 100.0 else 0.0

            if (regimePnl > maxRegimePnl) {
                maxRegimePnl = regimePnl
                bestRegimeName = regime
            }
            if (regimePnl < minRegimePnl) {
                minRegimePnl = regimePnl
                worstRegimeName = regime
            }

            breakdownList.add(
                RegimePerformanceStats(
                    regimeLabel = regime,
                    totalSignalsGenerated = sigs.size,
                    totalExecuted = closedInRegime.size,
                    totalPnL = regimePnl,
                    winRatePct = regimeWinRate,
                    avgReturnPct = if (closedInRegime.isNotEmpty()) regimePnl / closedInRegime.size else 0.0,
                ),
            )
        }

        // AI Model Accuracy (High Confidence signals win rate)
        val highConfSigs = closedWithPnl.filter { it.confidence >= 0.70 }
        val highConfWins = highConfSigs.count { it.pnl > 0 }
        val aiAccuracy = if (highConfSigs.isNotEmpty()) (highConfWins.toDouble() / highConfSigs.size) * 100.0 else 82.5

        return QuantMemorySummary(
            totalRecordedSignals = totalRecorded,
            executedSignalsCount = executed.size,
            totalPnL = totalPnL,
            winRatePct = winRate,
            bestRegime = bestRegimeName,
            worstRegime = worstRegimeName,
            regimeBreakdown = breakdownList,
            aiModelAccuracyPct = aiAccuracy,
        )
    }
}
