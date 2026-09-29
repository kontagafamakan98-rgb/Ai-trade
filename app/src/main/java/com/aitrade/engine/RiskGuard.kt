package com.aitrade.engine

import com.aitrade.data.UserPreferences
import kotlin.math.abs

object RiskGuard {
    data class RiskStatus(
        val allowed: Boolean,
        val reason: String,
        val dailyLossPct: Double,
        val totalDrawdownPct: Double,
    )

    fun checkRisk(
        prefs: UserPreferences,
        currentBalance: Double,
        activeTradesCount: Int,
        startingBalance: Double,
        dailyStartBalance: Double,
    ): RiskStatus {
        val maxDailyLossPct = prefs.maxDailyLossPct
        val maxDrawdownPct = prefs.maxTotalDrawdownPct
        val maxOpenTrades = prefs.maxOpenTrades

        if (activeTradesCount >= maxOpenTrades) {
            return RiskStatus(
                allowed = false,
                reason = "Maximum open positions reached ($maxOpenTrades).",
                dailyLossPct = 0.0,
                totalDrawdownPct = 0.0,
            )
        }

        var dailyLossPct = 0.0
        if (dailyStartBalance > 0.0) {
            dailyLossPct = ((dailyStartBalance - currentBalance) / dailyStartBalance) * 100.0
            if (dailyLossPct >= maxDailyLossPct) {
                return RiskStatus(
                    allowed = false,
                    reason = "Daily loss limit reached (${String.format("%.2f", dailyLossPct)}% ≥ $maxDailyLossPct%).",
                    dailyLossPct = dailyLossPct,
                    totalDrawdownPct = 0.0,
                )
            }
        }

        var totalDrawdownPct = 0.0
        if (startingBalance > 0.0) {
            totalDrawdownPct = ((startingBalance - currentBalance) / startingBalance) * 100.0
            if (totalDrawdownPct >= maxDrawdownPct) {
                return RiskStatus(
                    allowed = false,
                    reason = "Max drawdown limit reached (${String.format("%.2f", totalDrawdownPct)}% ≥ $maxDrawdownPct%).",
                    dailyLossPct = dailyLossPct,
                    totalDrawdownPct = totalDrawdownPct,
                )
            }
        }

        return RiskStatus(
            allowed = true,
            reason = "Passes all risk parameters.",
            dailyLossPct = dailyLossPct,
            totalDrawdownPct = totalDrawdownPct,
        )
    }

    /**
     * Compute quantity for the trade order based on risk percentage of equity.
     * In Python:
     * risk_amount = equity * (risk_pct / 100.0)
     * stop_distance = abs(entry - sl)
     * qty = risk_amount / stop_distance
     */
    fun computeQty(
        entry: Double,
        stopLoss: Double,
        equity: Double,
        riskPct: Double,
        asset: String,
    ): Double {
        if (entry <= 0.0) return 1.0
        val riskAmount = equity * (riskPct / 100.0)
        val stopDistance = if (stopLoss > 0.0) abs(entry - stopLoss) else entry * 0.01
        val validatedDistance = if (stopDistance <= 0.0) entry * 0.01 else stopDistance

        var qty = riskAmount / validatedDistance
        qty = Math.max(0.001, Math.min(qty, 1000.0))

        // Stocks are usually integers, crypto can be fractional
        val isCrypto = asset.contains("BTC") || asset.contains("ETH") || asset.contains("SOL") || asset.contains("USD")
        return if (!isCrypto) {
            Math.max(1.0, Math.round(qty).toDouble())
        } else {
            Math.round(qty * 10000.0) / 10000.0
        }
    }
}
