package com.aitrade.engine

import com.aitrade.data.EconomicEventEntity
import com.aitrade.data.MarketInsight
import com.aitrade.data.Signal
import kotlin.math.abs

enum class MarketRegime(
    val label: String,
    val description: String,
) {
    TRENDING_BULL("TRENDING BULL", "ADX High, EMA20 > EMA50, Strong Momentum"),
    TRENDING_BEAR("TRENDING BEAR", "ADX High, EMA20 < EMA50, Downtrend Pressure"),
    RANGING_SIDEWAYS("RANGING SIDEWAYS", "Low ADX Volatility, Range Bound Consolidation"),
    HIGH_VOLATILITY_CRISIS("HIGH VOLATILITY CRISIS", "Extreme ATR Spikes, High Downside Risk"),
}

enum class SignalClassification(
    val code: String,
    val displayName: String,
) {
    REAL_HIGH_CONVICTION("HIGH_CONVICTION", "Real Signal - High Conviction (Multi-Factor Aligned)"),
    REAL_STANDARD("STANDARD", "Real Signal - Standard Confidence"),
    WEAK_SIGNAL("WEAK", "Weak Signal - Below Execution Threshold"),
    DEMO_SIMULATION("DEMO", "Explicit Demo/Sandbox Simulation"),
}

data class NewsRiskCheckResult(
    val allowed: Boolean,
    val riskLevel: String, // "BLOCK", "DEGRADE", "ALLOWED"
    val reason: String,
)

object DecisionEngine {
    fun checkNewsRisk(
        asset: String,
        economicEvents: List<EconomicEventEntity>,
    ): NewsRiskCheckResult {
        val now = System.currentTimeMillis()
        val relevantCurrencies =
            when {
                asset.contains("EUR") -> listOf("EUR", "USD")
                asset.contains("GBP") -> listOf("GBP", "USD")
                asset.contains("JPY") -> listOf("USD", "JPY")
                else -> listOf("USD")
            }

        for (event in economicEvents) {
            if (relevantCurrencies.contains(event.currency.uppercase())) {
                val timeDiffMins = (event.timestamp - now) / (1000.0 * 60.0)
                if (event.impact.equals("High", ignoreCase = true) || event.impact.equals("Red", ignoreCase = true)) {
                    if (timeDiffMins in -15.0..30.0) {
                        return NewsRiskCheckResult(
                            allowed = false,
                            riskLevel = "BLOCK",
                            reason = "Trade bloqué par filtre News High Impact: ${event.currency} - ${event.title}",
                        )
                    }
                } else if (event.impact.equals("Medium", ignoreCase = true) || event.impact.equals("Orange", ignoreCase = true)) {
                    if (timeDiffMins in -10.0..15.0) {
                        return NewsRiskCheckResult(
                            allowed = true,
                            riskLevel = "DEGRADE",
                            reason = "Confiance dégradée: News Medium Impact (${event.currency} - ${event.title})",
                        )
                    }
                }
            }
        }
        return NewsRiskCheckResult(allowed = true, riskLevel = "ALLOWED", reason = "Pas de risque news majeur détecté.")
    }

    fun calculateMacroBias(economicEvents: List<EconomicEventEntity>): Pair<Double, String> {
        if (economicEvents.isEmpty()) return Pair(0.50, "Macro Forex Factory: Neutre / Pas d'événements")
        var sumScore = 0.0
        var count = 0
        for (event in economicEvents) {
            val act = event.actualNum
            val fc = event.forecastNum ?: event.previousNum
            if (act != null && fc != null) {
                val diff = act - fc
                val isInverse = event.title.contains("Unemployment", ignoreCase = true) || event.title.contains("Claims", ignoreCase = true)
                val score =
                    if (isInverse) {
                        if (diff < 0) 0.8 else 0.2
                    } else {
                        if (diff > 0) 0.8 else 0.2
                    }
                val weight = if (event.impact.equals("High", ignoreCase = true)) 2.0 else 1.0
                sumScore += score * weight
                count += weight.toInt()
            }
        }
        if (count == 0) return Pair(0.50, "Macro Forex Factory: Neutre (données en attente)")
        val avgScore = sumScore / count
        val label =
            if (avgScore >= 0.60) {
                "BULLISH"
            } else if (avgScore <= 0.40) {
                "BEARISH"
            } else {
                "NEUTRE"
            }
        return Pair(avgScore, "Forex Factory Macro Biais: $label (score ${String.format("%.2f", avgScore)})")
    }

    fun calculateRsi(
        closes: List<Double>,
        period: Int = 14,
    ): Double {
        if (closes.size <= period) return 50.0
        val gains = mutableListOf<Double>()
        val losses = mutableListOf<Double>()
        for (i in 1 until closes.size) {
            val d = closes[i] - closes[i - 1]
            gains.add(if (d > 0) d else 0.0)
        }
        for (i in 1 until closes.size) {
            val d = closes[i] - closes[i - 1]
            losses.add(if (d < 0) -d else 0.0)
        }

        val lastGains = gains.takeLast(period)
        val lastLosses = losses.takeLast(period)

        val avgGain = lastGains.sum() / period
        val avgLoss = lastLosses.sum() / period

        if (avgLoss == 0.0) return 100.0
        val rs = avgGain / avgLoss
        return 100.0 - (100.0 / (1.0 + rs))
    }

    fun calculateEma(
        values: List<Double>,
        span: Int,
    ): List<Double> {
        if (values.isEmpty()) return emptyList()
        val k = 2.0 / (span + 1.0)
        val out = mutableListOf<Double>()
        out.add(values[0])
        for (i in 1 until values.size) {
            val v = values[i]
            out.add(v * k + out.last() * (1.0 - k))
        }
        return out
    }

    /**
     * Detect Market Regime based on ATR Volatility Ratio and EMA Slopes
     */
    fun detectMarketRegime(closes: List<Double>): MarketRegime {
        if (closes.size < 20) return MarketRegime.RANGING_SIDEWAYS

        val entry = closes.last()
        val diffs = mutableListOf<Double>()
        for (i in 1 until closes.size) {
            diffs.add(abs(closes[i] - closes[i - 1]))
        }
        val atr = if (diffs.size >= 14) diffs.takeLast(14).sum() / 14.0 else entry * 0.015
        val atrPct = (atr / entry) * 100.0

        if (atrPct > 3.2) {
            return MarketRegime.HIGH_VOLATILITY_CRISIS
        }

        val ema20 = calculateEma(closes, 20).last()
        val ema50 = calculateEma(closes, 50).last()
        val emaDiffPct = ((ema20 - ema50) / ema50) * 100.0

        return when {
            emaDiffPct > 0.8 && atrPct > 1.2 -> MarketRegime.TRENDING_BULL
            emaDiffPct < -0.8 && atrPct > 1.2 -> MarketRegime.TRENDING_BEAR
            else -> MarketRegime.RANGING_SIDEWAYS
        }
    }

    fun analyze(
        asset: String,
        closes: List<Double>,
        insights: List<MarketInsight>,
        minConfidence: Double = 0.55,
        aiSentimentScore: Double? = null,
        aiReasoning: String? = null,
        forceDemoMode: Boolean = false,
    ): Signal? {
        if (closes.size < 30) return null

        val rsi = calculateRsi(closes)
        val ema20List = calculateEma(closes, 20)
        val ema50List = calculateEma(closes, 50)

        if (ema20List.isEmpty() || ema50List.isEmpty()) return null

        val ema20 = ema20List.last()
        val ema50 = ema50List.last()
        val entry = closes.last()

        val marketRegime = detectMarketRegime(closes)

        // Compute ATR
        val diffs = mutableListOf<Double>()
        for (i in 1 until closes.size) {
            diffs.add(abs(closes[i] - closes[i - 1]))
        }
        val atr =
            if (diffs.size >= 14) {
                diffs.takeLast(14).sum() / 14.0
            } else {
                entry * 0.015
            }
        val validatedAtr = if (atr <= 0.0) entry * 0.015 else atr

        // Factor 1: Technical Analysis (35% Weight)
        var taScore = 0.50
        val reasons = mutableListOf<String>()

        reasons.add("Regime: ${marketRegime.label}")

        if (rsi < 32.0) {
            taScore += 0.25
            reasons.add("RSI Oversold (${String.format("%.1f", rsi)})")
        } else if (rsi > 68.0) {
            taScore -= 0.25
            reasons.add("RSI Overbought (${String.format("%.1f", rsi)})")
        }

        if (ema20 > ema50) {
            taScore += 0.20
            reasons.add("EMA20 > EMA50 (Bullish Cross)")
        } else {
            taScore -= 0.20
            reasons.add("EMA20 < EMA50 (Bearish Pressure)")
        }

        // Factor 2: Macro & Geopolitical (25% Weight)
        val geoScoreAndSummary = scoreGeopolitical(insights)
        val geoScore = geoScoreAndSummary.first
        reasons.add("GeoMacro Score: ${String.format("%.2f", geoScore)}")

        // Factor 3: Market Sentiment (25% Weight)
        val sentScore = if (aiSentimentScore != null) aiSentimentScore else scoreSentiment(insights).first
        reasons.add("Sentiment Score: ${String.format("%.2f", sentScore)}")

        // Factor 4: Volatility & Order Flow Profile (15% Weight)
        val volFactor =
            when (marketRegime) {
                MarketRegime.TRENDING_BULL -> 0.70
                MarketRegime.TRENDING_BEAR -> 0.30
                MarketRegime.HIGH_VOLATILITY_CRISIS -> 0.40
                MarketRegime.RANGING_SIDEWAYS -> 0.50
            }
        reasons.add("VolProfile Factor: ${String.format("%.2f", volFactor)}")

        // Composite Multi-Factor Score Calculation
        val normalizedTa = taScore.coerceIn(0.0, 1.0)
        val finalProb = (0.35 * normalizedTa) + (0.25 * geoScore) + (0.25 * sentScore) + (0.15 * volFactor)

        var direction: String? = null
        if (finalProb >= 0.58) {
            direction = "BUY"
        } else if (finalProb <= 0.42) {
            direction = "SELL"
        }

        // NO FABRICATED DEMO SIGNALS IN REAL MODE:
        // If final probability doesn't exceed confidence threshold, return null (NO TRADE)
        if (direction == null || abs(finalProb - 0.5) < (minConfidence - 0.5)) {
            if (!forceDemoMode) {
                return null // NO SIGNAL - DISCIPLINED NO-TRADE DECISION
            }
        }

        val classification =
            when {
                forceDemoMode -> SignalClassification.DEMO_SIMULATION
                finalProb >= minConfidence + 0.15 || finalProb <= (1.0 - minConfidence - 0.15) -> SignalClassification.REAL_HIGH_CONVICTION
                abs(finalProb - 0.5) >= (minConfidence - 0.5) -> SignalClassification.REAL_STANDARD
                else -> SignalClassification.WEAK_SIGNAL
            }

        val effectiveDirection = direction ?: "BUY"

        val sl: Double
        val tp: Double
        if (effectiveDirection == "BUY") {
            sl = entry - 1.5 * validatedAtr
            tp = entry + 3.0 * validatedAtr
        } else {
            sl = entry + 1.5 * validatedAtr
            tp = entry - 3.0 * validatedAtr
        }

        val geoSummaryText =
            if (aiSentimentScore != null) {
                aiReasoning ?: "Analyse Multi-Facteurs IA complétée"
            } else {
                geoScoreAndSummary.second + " [Regime: ${marketRegime.label}]"
            }

        return Signal(
            asset = asset,
            direction = effectiveDirection,
            entry = Math.round(entry * 100000.0) / 100000.0,
            stopLoss = Math.round(sl * 100000.0) / 100000.0,
            takeProfit = Math.round(tp * 100000.0) / 100000.0,
            confidence = Math.round(finalProb * 1000.0) / 1000.0,
            taSummary = reasons.joinToString(" | ") + " | RSI=${String.format("%.1f", rsi)}",
            geoSummary = geoSummaryText,
            sentimentSummary = "Classification: ${classification.displayName} | Score=${String.format("%.2f", sentScore)}",
            reasoning =
                "Multi-Factor Engine [TA 35% + Geo 25% + Sent 25% + Vol 15%]. Regime: ${marketRegime.label}. Mode: " +
                    "${classification.code}",
            status = "PENDING",
        )
    }

    private fun scoreGeopolitical(insights: List<MarketInsight>): Pair<Double, String> {
        val geoList = insights.filter { it.type == "geopolitical" }
        if (geoList.isEmpty()) return Pair(0.50, "Macro Geopolitical: Stable / Neutral")
        val avgScore = geoList.map { it.score }.average()
        val latest = geoList.first()
        return Pair(avgScore.coerceIn(0.0, 1.0), "Macro: ${latest.title}")
    }

    private fun scoreSentiment(insights: List<MarketInsight>): Pair<Double, String> {
        val sentList = insights.filter { it.type == "sentiment" }
        if (sentList.isEmpty()) return Pair(0.50, "Sentiment: Neutral 0.50")
        val avgScore = sentList.map { it.score }.average()
        val label =
            if (avgScore >=
                0.55
            ) {
                "Bullish ${String.format("%.2f", avgScore)}"
            } else if (avgScore <=
                0.45
            ) {
                "Bearish ${String.format("%.2f", avgScore)}"
            } else {
                "Neutral ${String.format("%.2f", avgScore)}"
            }
        return Pair(avgScore.coerceIn(0.0, 1.0), "Sentiment: $label")
    }
}
