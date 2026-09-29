package com.aitrade.engine

import com.aitrade.data.Signal
import kotlin.math.abs
import kotlin.math.pow
import kotlin.math.sqrt
import kotlin.random.Random

enum class TradingStrategy(
    val code: String,
    val displayName: String,
    val description: String,
) {
    MULTI_FACTOR_COMPOSITE("MULTI_FACTOR", "Multi-Factor Quant Engine", "TA 35% + Geo 25% + Sentiment 25% + Vol 15%"),
    EMA_CROSS_TREND("EMA_CROSS", "EMA 20/50 Trend Following", "Golden & Death Cross momentum with regime filtering"),
    RSI_MEAN_REVERSION("RSI_REVERSION", "RSI 14 Mean Reversion", "Oversold <30 Buy / Overbought >70 Sell reversals"),
    VOLATILITY_BREAKOUT("VOL_BREAKOUT", "ATR Keltner Volatility Breakout", "ATR channel expansion and momentum breakout"),
}

data class BacktestTradeLog(
    val id: String,
    val asset: String,
    val direction: String,
    val entryPrice: Double,
    val exitPrice: Double,
    val pnl: Double,
    val pnlPct: Double,
    val regime: String,
    val strategy: String,
    val step: Int,
)

data class MonteCarloResult(
    val iterations: Int = 500,
    val var95Pct: Double,
    val var99Pct: Double,
    val expectedShortfall95Pct: Double,
    val medianFinalEquity: Double,
    val fifthPercentileEquity: Double,
    val ninetyFifthPercentileEquity: Double,
    val maxDrawdownDistribution: List<Double>,
)

data class BacktestResult(
    val timeframeLabel: String,
    val strategy: TradingStrategy = TradingStrategy.MULTI_FACTOR_COMPOSITE,
    val initialCapital: Double,
    val finalCapital: Double,
    val totalReturnPct: Double,
    val benchmarkReturnPct: Double, // Buy & Hold BTC Benchmark
    val alphaPct: Double,
    val beta: Double,
    val totalTrades: Int,
    val winningTrades: Int,
    val losingTrades: Int,
    val winRatePct: Double,
    val profitFactor: Double,
    val sharpeRatio: Double,
    val sortinoRatio: Double,
    val calmarRatio: Double,
    val maxDrawdownPct: Double,
    val avgTradeReturnPct: Double,
    val equityCurve: List<Double>,
    val tradeLogs: List<BacktestTradeLog> = emptyList(),
    val regimeAttribution: Map<String, Double>,
    val monteCarlo: MonteCarloResult? = null,
    // Marqueur explicite : les séries utilisées sont GÉNÉRÉES localement
    // (marche aléatoire + probabilités de gain fixes), donc NON
    // représentatives d'un vrai backtest sur données historiques.
    val isSynthetic: Boolean = true,
)

object BacktestEngine {
    /**
     * Runs historical walk-forward backtest over generated historical multi-asset candles
     * with strategy selection, Monte Carlo stress testing, and quantitative risk metrics.
     */
    fun runBacktest(
        initialCapital: Double = 100000.0,
        timeframeDays: Int = 365,
        minConfidence: Double = 0.55,
        strategy: TradingStrategy = TradingStrategy.MULTI_FACTOR_COMPOSITE,
        runMonteCarlo: Boolean = true,
    ): BacktestResult {
        val days = timeframeDays.coerceIn(30, 1095)
        val equityCurve = mutableListOf<Double>()
        equityCurve.add(initialCapital)

        var currentEquity = initialCapital
        var peakEquity = initialCapital
        var maxDrawdown = 0.0

        val grossProfits = mutableListOf<Double>()
        val grossLosses = mutableListOf<Double>()
        val returns = mutableListOf<Double>()
        val downsideReturns = mutableListOf<Double>()
        val tradeLogs = mutableListOf<BacktestTradeLog>()

        val assets = listOf("BTC", "ETH", "AAPL", "NVDA", "TSLA", "MSFT", "GOOGL")
        var winningCount = 0
        var losingCount = 0

        val regimePnl = mutableMapOf<String, Double>()
        MarketRegime.entries.forEach { regimePnl[it.label] = 0.0 }

        // Generate synthetic walk-forward candle sequence for simulation
        val stepCount = (days / 2).coerceAtLeast(30)
        var benchmarkEquity = initialCapital
        val benchmarkReturns = mutableListOf<Double>()

        for (step in 1..stepCount) {
            val asset = assets[step % assets.size]
            val basePrice =
                when (asset) {
                    "BTC" -> 60000.0
                    "ETH" -> 3200.0
                    "NVDA" -> 120.0
                    "AAPL" -> 220.0
                    "TSLA" -> 210.0
                    "MSFT" -> 440.0
                    else -> 180.0
                }

            val closes = mutableListOf<Double>()
            var p = basePrice
            val vol = if (asset == "BTC" || asset == "ETH") 0.025 else 0.015
            val seed = step * 1000
            val rng = java.util.Random(seed.toLong())

            for (i in 0 until 50) {
                val rnd = (rng.nextDouble() - 0.48) * 2 * vol
                p *= (1.0 + rnd)
                closes.add(p)
            }

            // Benchmark simulation (BTC Buy & Hold)
            val benchmarkChange = (rng.nextDouble() - 0.47) * 0.02
            val prevBench = benchmarkEquity
            benchmarkEquity *= (1.0 + benchmarkChange)
            benchmarkReturns.add((benchmarkEquity - prevBench) / prevBench)

            // Execute Strategy Analysis
            val signal = evaluateStrategy(strategy, asset, closes, minConfidence)

            if (signal != null) {
                val regime = DecisionEngine.detectMarketRegime(closes)

                // Strategy Win Probabilities & Expectancy
                val winProb =
                    when (strategy) {
                        TradingStrategy.MULTI_FACTOR_COMPOSITE -> 0.59
                        TradingStrategy.EMA_CROSS_TREND -> 0.55
                        TradingStrategy.RSI_MEAN_REVERSION -> 0.57
                        TradingStrategy.VOLATILITY_BREAKOUT -> 0.53
                    }

                val isWin = rng.nextDouble() < winProb
                val tradeReturnPct =
                    if (isWin) {
                        0.015 + rng.nextDouble() * 0.028
                    } else {
                        -(0.010 + rng.nextDouble() * 0.018)
                    }

                val pnl = currentEquity * 0.02 * (tradeReturnPct / 0.015) // Risk 2% per trade
                val entryPrice = closes.last()
                val exitPrice = entryPrice * (1.0 + (if (signal.direction == "BUY") tradeReturnPct else -tradeReturnPct))

                currentEquity += pnl
                val ret = pnl / (currentEquity - pnl)
                returns.add(ret)
                if (ret < 0) downsideReturns.add(ret)

                equityCurve.add(currentEquity)

                if (currentEquity > peakEquity) {
                    peakEquity = currentEquity
                } else {
                    val dd = ((peakEquity - currentEquity) / peakEquity) * 100.0
                    if (dd > maxDrawdown) maxDrawdown = dd
                }

                if (pnl >= 0) {
                    winningCount++
                    grossProfits.add(pnl)
                } else {
                    losingCount++
                    grossLosses.add(-pnl)
                }

                regimePnl[regime.label] = (regimePnl[regime.label] ?: 0.0) + pnl

                tradeLogs.add(
                    BacktestTradeLog(
                        id = "BT-$step-$asset",
                        asset = asset,
                        direction = signal.direction,
                        entryPrice = Math.round(entryPrice * 100.0) / 100.0,
                        exitPrice = Math.round(exitPrice * 100.0) / 100.0,
                        pnl = Math.round(pnl * 100.0) / 100.0,
                        pnlPct = Math.round(tradeReturnPct * 10000.0) / 100.0,
                        regime = regime.label,
                        strategy = strategy.displayName,
                        step = step,
                    ),
                )
            }
        }

        val totalTrades = winningCount + losingCount
        val winRatePct = if (totalTrades > 0) (winningCount.toDouble() / totalTrades) * 100.0 else 0.0
        val sumProfits = grossProfits.sum()
        val sumLosses = grossLosses.sum()
        val profitFactor =
            if (sumLosses > 0) {
                sumProfits / sumLosses
            } else if (sumProfits > 0) {
                9.99
            } else {
                1.0
            }

        val totalReturnPct = ((currentEquity - initialCapital) / initialCapital) * 100.0
        val benchmarkReturnPct = ((benchmarkEquity - initialCapital) / initialCapital) * 100.0

        // Sharpe, Sortino & Calmar Ratios
        val meanReturn = if (returns.isNotEmpty()) returns.average() else 0.0
        val variance = if (returns.size > 1) returns.map { (it - meanReturn).pow(2) }.average() else 0.0001
        val stdDev = sqrt(variance)
        val sharpeRatio = if (stdDev > 0) (meanReturn / stdDev) * sqrt(252.0) else 0.0

        val downsideVariance = if (downsideReturns.size > 1) downsideReturns.map { it.pow(2) }.average() else 0.0001
        val downsideStdDev = sqrt(downsideVariance)
        val sortinoRatio = if (downsideStdDev > 0) (meanReturn / downsideStdDev) * sqrt(252.0) else 0.0

        val calmarRatio = if (maxDrawdown > 0) (totalReturnPct / maxDrawdown) else 0.0

        // Alpha & Beta calculation vs Benchmark
        val benchMean = if (benchmarkReturns.isNotEmpty()) benchmarkReturns.average() else 0.0
        val covariance =
            if (returns.size == benchmarkReturns.size && returns.size > 1) {
                returns.indices.sumOf { i -> (returns[i] - meanReturn) * (benchmarkReturns[i] - benchMean) } / (returns.size - 1)
            } else {
                0.0001
            }
        val benchVariance = if (benchmarkReturns.size > 1) benchmarkReturns.map { (it - benchMean).pow(2) }.average() else 0.0001
        val beta = if (benchVariance > 0) covariance / benchVariance else 1.0
        val alphaPct = totalReturnPct - (beta * benchmarkReturnPct)

        // Monte Carlo Stress Testing
        val monteCarloResult =
            if (runMonteCarlo && returns.isNotEmpty()) {
                simulateMonteCarlo(returns, initialCapital, steps = stepCount)
            } else {
                null
            }

        return BacktestResult(
            timeframeLabel = "$days Days Walk-Forward",
            strategy = strategy,
            initialCapital = initialCapital,
            finalCapital = currentEquity,
            totalReturnPct = totalReturnPct,
            benchmarkReturnPct = benchmarkReturnPct,
            alphaPct = alphaPct,
            beta = beta,
            totalTrades = totalTrades,
            winningTrades = winningCount,
            losingTrades = losingCount,
            winRatePct = winRatePct,
            profitFactor = profitFactor,
            sharpeRatio = sharpeRatio,
            sortinoRatio = sortinoRatio,
            calmarRatio = calmarRatio,
            maxDrawdownPct = maxDrawdown,
            avgTradeReturnPct = if (returns.isNotEmpty()) returns.average() * 100 else 0.0,
            equityCurve = equityCurve,
            tradeLogs = tradeLogs,
            regimeAttribution = regimePnl,
            monteCarlo = monteCarloResult,
        )
    }

    private fun evaluateStrategy(
        strategy: TradingStrategy,
        asset: String,
        closes: List<Double>,
        minConfidence: Double,
    ): Signal? {
        if (closes.size < 30) return null
        val rsi = DecisionEngine.calculateRsi(closes)
        val ema20List = DecisionEngine.calculateEma(closes, 20)
        val ema50List = DecisionEngine.calculateEma(closes, 50)
        val entry = closes.last()

        return when (strategy) {
            TradingStrategy.MULTI_FACTOR_COMPOSITE -> {
                DecisionEngine.analyze(
                    asset = asset,
                    closes = closes,
                    insights = emptyList(),
                    minConfidence = minConfidence,
                    forceDemoMode = false,
                )
            }
            TradingStrategy.EMA_CROSS_TREND -> {
                if (ema20List.isEmpty() || ema50List.isEmpty()) return null
                val ema20 = ema20List.last()
                val ema50 = ema50List.last()
                val prev20 = ema20List[ema20List.size - 2]
                val prev50 = ema50List[ema50List.size - 2]

                val dir =
                    if (prev20 <= prev50 && ema20 > ema50) {
                        "BUY"
                    } else if (prev20 >= prev50 && ema20 < ema50) {
                        "SELL"
                    } else {
                        null
                    }

                if (dir != null) {
                    Signal(
                        asset = asset,
                        direction = dir,
                        entry = entry,
                        stopLoss = if (dir == "BUY") entry * 0.98 else entry * 1.02,
                        takeProfit = if (dir == "BUY") entry * 1.04 else entry * 0.96,
                        confidence = 0.68,
                        taSummary = "EMA20/50 Cross Strategy",
                        geoSummary = "Strategy: EMA Cross Trend",
                        sentimentSummary = "Confidence: 68%",
                        reasoning = "Trend Following EMA20 ($ema20) vs EMA50 ($ema50) Cross",
                        status = "PENDING",
                    )
                } else {
                    null
                }
            }
            TradingStrategy.RSI_MEAN_REVERSION -> {
                val dir =
                    if (rsi < 30.0) {
                        "BUY"
                    } else if (rsi > 70.0) {
                        "SELL"
                    } else {
                        null
                    }
                if (dir != null) {
                    Signal(
                        asset = asset,
                        direction = dir,
                        entry = entry,
                        stopLoss = if (dir == "BUY") entry * 0.975 else entry * 1.025,
                        takeProfit = if (dir == "BUY") entry * 1.035 else entry * 0.965,
                        confidence = 0.64,
                        taSummary = "RSI Mean Reversion (RSI=${String.format("%.1f", rsi)})",
                        geoSummary = "Strategy: RSI Reversion",
                        sentimentSummary = "Confidence: 64%",
                        reasoning = "Reversal signal triggered at extreme RSI boundary",
                        status = "PENDING",
                    )
                } else {
                    null
                }
            }
            TradingStrategy.VOLATILITY_BREAKOUT -> {
                val diffs = mutableListOf<Double>()
                for (i in 1 until closes.size) diffs.add(abs(closes[i] - closes[i - 1]))
                val atr = diffs.takeLast(14).sum() / 14.0
                val upperKeltner = ema20List.last() + (1.5 * atr)
                val lowerKeltner = ema20List.last() - (1.5 * atr)

                val dir =
                    if (entry > upperKeltner) {
                        "BUY"
                    } else if (entry < lowerKeltner) {
                        "SELL"
                    } else {
                        null
                    }
                if (dir != null) {
                    Signal(
                        asset = asset,
                        direction = dir,
                        entry = entry,
                        stopLoss = if (dir == "BUY") entry - (1.5 * atr) else entry + (1.5 * atr),
                        takeProfit = if (dir == "BUY") entry + (3.0 * atr) else entry - (3.0 * atr),
                        confidence = 0.62,
                        taSummary = "ATR Keltner Breakout (ATR=${String.format("%.2f", atr)})",
                        geoSummary = "Strategy: Volatility Breakout",
                        sentimentSummary = "Confidence: 62%",
                        reasoning = "Volatility Expansion Breakout beyond Keltner Channel",
                        status = "PENDING",
                    )
                } else {
                    null
                }
            }
        }
    }

    private fun simulateMonteCarlo(
        tradeReturns: List<Double>,
        initialCapital: Double,
        iterations: Int = 500,
        steps: Int = 100,
    ): MonteCarloResult {
        val finalEquities = mutableListOf<Double>()
        val maxDrawdowns = mutableListOf<Double>()

        val rng = Random(42)

        for (sim in 0 until iterations) {
            var simEquity = initialCapital
            var peak = initialCapital
            var simMaxDd = 0.0

            for (s in 0 until steps) {
                val randomReturn = tradeReturns[rng.nextInt(tradeReturns.size)]
                simEquity *= (1.0 + randomReturn)
                if (simEquity > peak) {
                    peak = simEquity
                } else {
                    val dd = ((peak - simEquity) / peak) * 100.0
                    if (dd > simMaxDd) simMaxDd = dd
                }
            }

            finalEquities.add(simEquity)
            maxDrawdowns.add(simMaxDd)
        }

        finalEquities.sort()
        maxDrawdowns.sort()

        val fifthIdx = (iterations * 0.05).toInt().coerceIn(0, iterations - 1)
        val firstIdx = (iterations * 0.01).toInt().coerceIn(0, iterations - 1)
        val medianIdx = (iterations * 0.50).toInt().coerceIn(0, iterations - 1)
        val ninetyFifthIdx = (iterations * 0.95).toInt().coerceIn(0, iterations - 1)

        val fifthEq = finalEquities[fifthIdx]
        val medianEq = finalEquities[medianIdx]
        val ninetyFifthEq = finalEquities[ninetyFifthIdx]

        val var95Pct = ((initialCapital - fifthEq) / initialCapital * 100.0).coerceAtLeast(0.0)
        val var99Pct = ((initialCapital - finalEquities[firstIdx]) / initialCapital * 100.0).coerceAtLeast(0.0)

        // Expected Shortfall (CVaR 95%) = average loss beyond 95th percentile
        val tailEquities = finalEquities.take(fifthIdx + 1)
        val avgTailEquity = if (tailEquities.isNotEmpty()) tailEquities.average() else fifthEq
        val es95Pct = ((initialCapital - avgTailEquity) / initialCapital * 100.0).coerceAtLeast(0.0)

        return MonteCarloResult(
            iterations = iterations,
            var95Pct = var95Pct,
            var99Pct = var99Pct,
            expectedShortfall95Pct = es95Pct,
            medianFinalEquity = medianEq,
            fifthPercentileEquity = fifthEq,
            ninetyFifthPercentileEquity = ninetyFifthEq,
            maxDrawdownDistribution = maxDrawdowns,
        )
    }
}
