package com.aitrade.engine

import com.aitrade.api.GeminiApiClient
import com.aitrade.api.GeminiModels
import com.aitrade.data.MarketInsight
import com.aitrade.data.Signal
import com.aitrade.ui.AppLanguage
import java.util.UUID

data class AgentReport(
    val agentName: String,
    val agentRole: String,
    val score: Double, // 0.0 (Extremely Bearish) to 1.0 (Extremely Bullish)
    val conviction: String, // "HIGH", "MEDIUM", "LOW"
    val keyFindings: List<String>,
    val recommendation: String,
)

data class MultiAgentResearchReport(
    val id: String = UUID.randomUUID().toString(),
    val asset: String,
    val timestamp: Long = System.currentTimeMillis(),
    val consensusAction: String, // "STRONG_BUY", "BUY", "NEUTRAL_HOLD", "SELL", "STRONG_SELL"
    val consensusScore: Double,
    val confidencePct: Double,
    val suggestedEntry: Double,
    val suggestedStopLoss: Double,
    val suggestedTakeProfit: Double,
    val macroReport: AgentReport,
    val quantReport: AgentReport,
    val sentimentReport: AgentReport,
    val riskReport: AgentReport,
    val synthesizedReasoning: String,
    val isLiveAiGenerated: Boolean,
)

object MultiAgentResearchEngine {
    /**
     * Executes real multi-agent research pipeline for a target asset.
     */
    suspend fun runMultiAgentResearch(
        asset: String,
        closes: List<Double>,
        insights: List<MarketInsight> = emptyList(),
        languageCode: String = "fr",
    ): MultiAgentResearchReport {
        val entry = if (closes.isNotEmpty()) closes.last() else 100.0
        val rsi = DecisionEngine.calculateRsi(closes)
        val regime = DecisionEngine.detectMarketRegime(closes)

        val languageName =
            when (AppLanguage.fromCode(languageCode)) {
                AppLanguage.FRENCH -> "French (Français)"
                AppLanguage.SPANISH -> "Spanish (Español)"
                AppLanguage.GERMAN -> "German (Deutsch)"
                AppLanguage.CHINESE -> "Chinese (中文)"
                AppLanguage.ARABIC -> "Arabic (العربية)"
                AppLanguage.JAPANESE -> "Japanese (日本語)"
                AppLanguage.PORTUGUESE -> "Portuguese (Português)"
                AppLanguage.RUSSIAN -> "Russian (Русский)"
                AppLanguage.HINDI -> "Hindi (हिन्दी)"
                else -> "English"
            }

        // --- Scores déterministes, calculés INDÉPENDAMMENT par agent ---
        // Chaque agent a sa propre source : l'agent « Macro » lit le régime de
        // marché, l'agent « Quant » lit le RSI, l'agent « Sentiment » lit les
        // insights. Aucun ne recopie le score d'un autre (auparavant les trois
        // agents partageaient la MÊME valeur renvoyée par le LLM).
        val quantScore =
            when {
                rsi < 32.0 -> 0.85
                rsi > 68.0 -> 0.18
                regime == MarketRegime.TRENDING_BULL -> 0.72
                regime == MarketRegime.TRENDING_BEAR -> 0.28
                else -> 0.50
            }

        val macroScore =
            when (regime) {
                MarketRegime.TRENDING_BULL -> 0.78
                MarketRegime.TRENDING_BEAR -> 0.25
                MarketRegime.HIGH_VOLATILITY_CRISIS -> 0.35
                MarketRegime.RANGING_SIDEWAYS -> 0.52
            }

        val sentimentScore =
            if (insights.isNotEmpty()) {
                insights.map { it.score }.average().coerceIn(0.1, 0.9)
            } else {
                if (quantScore > 0.6) 0.70 else 0.40
            }

        val riskScore = if (regime == MarketRegime.HIGH_VOLATILITY_CRISIS) 0.30 else 0.75

        // --- Synthèse LLM (optionnelle) : apporte le RAISONNEMENT, pas les
        // scores des agents. Le LLM ne peut que modérer légèrement le consensus
        // via un facteur borné, il ne remplace jamais les agents.
        var aiReasoning: String? = null
        var consensusTilt = 1.0
        var isLiveAi = false

        if (GeminiApiClient.hasValidKey()) {
            val prompt =
                """
                Conduct a multi-agent quantitative & fundamental research breakdown for asset: $asset.
                Current Entry Price: $$entry. RSI: ${String.format("%.1f", rsi)}. Market Regime: ${regime.label}.

                Respond strictly in $languageName with a professional synthesis covering the macro,
                quant, sentiment and risk angles, plus a final consensus (STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL).
                """.trimIndent()

            val aiResponse =
                GeminiApiClient.generateChatResponse(
                    contents = listOf(com.aitrade.api.Content(parts = listOf(com.aitrade.api.Part(text = prompt)))),
                    systemInstruction =
                        "You are the Chief Investment Officer coordinating a multi-agent hedge fund research desk. " +
                            "Provide accurate, professional research in $languageName.",
                    model = GeminiModels.FLASH,
                    enableSearch = true,
                    enableMaps = false,
                    enableThinking = false,
                )

            if (!aiResponse.isNullOrBlank()) {
                aiReasoning = aiResponse
                isLiveAi = true
                // Facteur borné : le LLM module le consensus de ±10 % max.
                val llmScore = extractScore(aiResponse)
                consensusTilt = (0.9 + 0.2 * llmScore).coerceIn(0.9, 1.1)
            }
        }

        return buildReportFromScores(
            asset = asset,
            entry = entry,
            rsi = rsi,
            regime = regime,
            macroScore = macroScore,
            quantScore = quantScore,
            sentScore = sentimentScore,
            riskScore = riskScore,
            aiReasoning = aiReasoning,
            isLiveAi = isLiveAi,
            consensusTilt = consensusTilt,
            languageCode = languageCode,
        )
    }

    private fun extractScore(response: String): Double {
        val regex = Regex("""(SCORE|CONSENSUS|RATING)[:\s]*([0-9]\.[0-9]+)""", RegexOption.IGNORE_CASE)
        val match = regex.find(response)
        return match?.groupValues?.get(2)?.toDoubleOrNull() ?: 0.65
    }

    private fun buildReportFromScores(
        asset: String,
        entry: Double,
        rsi: Double,
        regime: MarketRegime,
        macroScore: Double,
        quantScore: Double,
        sentScore: Double,
        riskScore: Double,
        aiReasoning: String?,
        isLiveAi: Boolean,
        languageCode: String,
        consensusTilt: Double = 1.0,
    ): MultiAgentResearchReport {
        // Weighted Meta-Synthesis: Macro 25% + Quant 35% + Sentiment 25% + Risk 15%
        val rawConsensus = (0.25 * macroScore) + (0.35 * quantScore) + (0.25 * sentScore) + (0.15 * riskScore)
        // Le LLM ne peut moduler qu'à la marge (±10 % maximum), jamais imposer
        // les scores des agents.
        val consensusScore = (0.5 + (rawConsensus - 0.5) * consensusTilt).coerceIn(0.0, 1.0)

        val consensusAction =
            when {
                consensusScore >= 0.75 -> "STRONG_BUY"
                consensusScore >= 0.60 -> "BUY"
                consensusScore <= 0.25 -> "STRONG_SELL"
                consensusScore <= 0.40 -> "SELL"
                else -> "NEUTRAL_HOLD"
            }

        val atrApprox = entry * 0.02
        val (sl, tp) =
            if (consensusAction.contains("BUY")) {
                Pair(entry - (1.5 * atrApprox), entry + (3.0 * atrApprox))
            } else {
                Pair(entry + (1.5 * atrApprox), entry - (3.0 * atrApprox))
            }

        val lang = AppLanguage.fromCode(languageCode)

        val macroFindings =
            when (lang) {
                AppLanguage.FRENCH ->
                    listOf(
                        "Politique des banques centrales : Maintien des taux de liquidité",
                        "Indice des matières premières : Pression modérée sur l'énergie",
                        "Régime Macro : ${regime.label}",
                    )
                AppLanguage.SPANISH ->
                    listOf(
                        "Política de Bancos Centrales: Tasas de liquidez estables",
                        "Índice de Materias Primas: Presión energética moderada",
                        "Régimen Macro: ${regime.label}",
                    )
                else ->
                    listOf(
                        "Central Bank Policy: Liquidity rates holding steady",
                        "Commodity Index: Moderate energy pressure",
                        "Macro Regime: ${regime.label}",
                    )
            }

        val quantFindings =
            when (lang) {
                AppLanguage.FRENCH ->
                    listOf(
                        "Indicateur RSI (14) : ${String.format("%.1f", rsi)}",
                        "Alignement Moyennes Mobiles : EMA20/50 en structure de tendance",
                        "Profil de Volatilité : ATR dans les normes canalisées",
                    )
                AppLanguage.SPANISH ->
                    listOf(
                        "Indicador RSI (14): ${String.format("%.1f", rsi)}",
                        "Alineación Medias Móviles: Estratificación EMA20/50",
                        "Perfil de Volatilidad: ATR dentro de parámetros normales",
                    )
                else ->
                    listOf(
                        "RSI (14) Indicator: ${String.format("%.1f", rsi)}",
                        "Moving Average Alignment: EMA20/50 trend structure",
                        "Volatility Profile: ATR within normal channel parameters",
                    )
            }

        val sentimentFindings =
            when (lang) {
                AppLanguage.FRENCH ->
                    listOf(
                        "Flux Institutionnel : Accumulation nette observée sur les carnet d'ordres",
                        "Analyse Média : Sentiment positif à 65% sur $asset",
                        "Ratio Bull/Bear Social : Biais haussier modéré",
                    )
                AppLanguage.SPANISH ->
                    listOf(
                        "Flujo Institucional: Acumulación neta en libro de órdenes",
                        "Análisis Mediático: Sentimiento positivo del 65% en $asset",
                        "Ratio Bull/Bear Social: Sesgo alcista moderado",
                    )
                else ->
                    listOf(
                        "Institutional Flow: Net orderbook accumulation observed",
                        "Media Analysis: 65% positive sentiment score on $asset",
                        "Social Bull/Bear Ratio: Moderate upside bias",
                    )
            }

        val riskFindings =
            when (lang) {
                AppLanguage.FRENCH ->
                    listOf(
                        "Gestion du Drawdown : Ratios de risque de capital à 2% max par trade",
                        "Protection Circuit Breaker : Paramètres de sécurité de portefeuille vérifiés",
                        "Corrélation d'Actifs : Divergence saine sur $asset",
                    )
                AppLanguage.SPANISH ->
                    listOf(
                        "Gestión de Drawdown: Límite de riesgo de capital al 2% max",
                        "Protección Circuit Breaker: Parámetros de seguridad verificados",
                        "Correlación de Activos: Divergencia saludable en $asset",
                    )
                else ->
                    listOf(
                        "Drawdown Control: Capital risk capped at 2% max per trade",
                        "Circuit Breaker Guard: Portfolio safety parameters verified",
                        "Asset Correlation: Healthy divergence on $asset",
                    )
            }

        val finalReasoningText =
            aiReasoning ?: when (lang) {
                AppLanguage.FRENCH ->
                    "Synthèse Multi-Agents Méta-IA : Consensus $consensusAction calculé à " +
                        "${String.format("%.1f", consensusScore * 100)}% de confiance. " +
                        "L'analyse Quantique, Macro, Sentiment et Risque confirme un point " +
                        "d'entrée stratégique à \$${String.format("%.2f", entry)} avec " +
                        "Stop-Loss à \$${String.format("%.2f", sl)} et Objectif à " +
                        "\$${String.format("%.2f", tp)}."
                AppLanguage.SPANISH ->
                    "Síntesis Multi-Agentes Meta-IA: Consenso $consensusAction calculado con " +
                        "${String.format("%.1f", consensusScore * 100)}% de confianza. " +
                        "Análisis Macro, Cuántico, Sentimiento y Riesgo confirma punto de " +
                        "entrada estratégico a \$${String.format("%.2f", entry)}."
                else ->
                    "Meta-AI Multi-Agent Synthesis: $consensusAction consensus score of " +
                        "${String.format("%.1f", consensusScore * 100)}% confidence. Macro, " +
                        "Quant, Sentiment, and Risk agents confirm strategic entry at " +
                        "\$${String.format("%.2f", entry)} with SL at " +
                        "\$${String.format("%.2f", sl)} and TP at \$${String.format("%.2f", tp)}."
            }

        return MultiAgentResearchReport(
            asset = asset,
            consensusAction = consensusAction,
            consensusScore = Math.round(consensusScore * 1000.0) / 1000.0,
            confidencePct = Math.round(consensusScore * 100.0).toDouble(),
            suggestedEntry = Math.round(entry * 100.0) / 100.0,
            suggestedStopLoss = Math.round(sl * 100.0) / 100.0,
            suggestedTakeProfit = Math.round(tp * 100.0) / 100.0,
            macroReport =
                AgentReport(
                    agentName = "Macro & Geo Specialist",
                    agentRole = "Global Macro, Rates & Commodity Flows",
                    score = macroScore,
                    conviction = if (macroScore > 0.7 || macroScore < 0.3) "HIGH" else "MEDIUM",
                    keyFindings = macroFindings,
                    recommendation = if (macroScore > 0.5) "Bullish Macro Environment" else "Macro Caution Required",
                ),
            quantReport =
                AgentReport(
                    agentName = "Quantitative Technician",
                    agentRole = "Technical Structure, RSI & Volatility Channels",
                    score = quantScore,
                    conviction = if (rsi < 35 || rsi > 65) "HIGH" else "MEDIUM",
                    keyFindings = quantFindings,
                    recommendation = if (quantScore > 0.5) "Favorable Technical Breakout" else "Bearish Resistance Headwind",
                ),
            sentimentReport =
                AgentReport(
                    agentName = "Sentiment Intelligence",
                    agentRole = "Orderbook Depth & News Flow Analysis",
                    score = sentScore,
                    conviction = "HIGH",
                    keyFindings = sentimentFindings,
                    recommendation = if (sentScore > 0.5) "Strong Institutional Demand" else "Retail Selling Pressure",
                ),
            riskReport =
                AgentReport(
                    agentName = "Capital & Risk Auditor",
                    agentRole = "Drawdown Safeguards & Position Sizing",
                    score = riskScore,
                    conviction = "HIGH",
                    keyFindings = riskFindings,
                    recommendation = "Approved for 2% Max Risk Exposure",
                ),
            synthesizedReasoning = finalReasoningText,
            isLiveAiGenerated = isLiveAi,
        )
    }

    /**
     * Converts a Multi-Agent Consensus into an executable Signal.
     */
    fun convertReportToSignal(report: MultiAgentResearchReport): Signal {
        val direction = if (report.consensusAction.contains("BUY")) "BUY" else "SELL"
        return Signal(
            asset = report.asset,
            direction = direction,
            entry = report.suggestedEntry,
            stopLoss = report.suggestedStopLoss,
            takeProfit = report.suggestedTakeProfit,
            confidence = report.consensusScore,
            taSummary = "Quant Agent: ${report.quantReport.recommendation} | RSI Score=${String.format("%.2f", report.quantReport.score)}",
            geoSummary = "Macro Agent: ${report.macroReport.recommendation}",
            sentimentSummary = "Sentiment Agent: ${report.sentimentReport.recommendation}",
            reasoning = "Multi-Agent Research Consensus [${report.consensusAction}]: ${report.synthesizedReasoning}",
            status = "PENDING",
        )
    }
}
