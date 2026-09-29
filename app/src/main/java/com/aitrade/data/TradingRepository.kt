package com.aitrade.data

import android.content.Context
import com.aitrade.R
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

class TradingRepository(
    private val dao: TradingDao,
    private val context: Context,
) {
    val preferencesFlow: Flow<UserPreferences> =
        dao.getPreferencesFlow().map { prefs ->
            prefs ?: UserPreferences()
        }

    // Les identifiants broker sont chiffrés (AES-GCM / Android Keystore) au
    // repos : on déchiffre à la lecture et on chiffre à l'écriture, de façon
    // centralisée ici pour que la base locale ne contienne jamais de secret
    // en clair.
    val credentialsFlow: Flow<BrokerCredentials> =
        dao.getCredentialsFlow().map { creds ->
            decryptCredentials(creds ?: BrokerCredentials())
        }

    val signalsFlow: Flow<List<Signal>> = dao.getAllSignalsFlow()
    val logsFlow: Flow<List<LogEvent>> = dao.getLogsFlow()
    val insightsFlow: Flow<List<MarketInsight>> = dao.getInsightsFlow()
    val postMortemsFlow: Flow<List<TradePostMortemEntity>> = dao.getPostMortemsFlow()

    suspend fun insertPostMortem(postMortem: TradePostMortemEntity) {
        dao.insertPostMortem(postMortem)
    }

    suspend fun getPreferences(): UserPreferences = dao.getPreferences() ?: UserPreferences().also { dao.savePreferences(it) }

    suspend fun savePreferences(prefs: UserPreferences) {
        dao.savePreferences(prefs)
    }

    suspend fun getCredentials(): BrokerCredentials {
        val stored = dao.getCredentials() ?: BrokerCredentials().also { dao.saveCredentials(it) }
        return decryptCredentials(stored)
    }

    suspend fun saveCredentials(creds: BrokerCredentials) {
        dao.saveCredentials(encryptCredentials(creds))
    }

    private fun encryptCredentials(creds: BrokerCredentials): BrokerCredentials =
        creds.copy(
            apiKey = Crypto.encrypt(creds.apiKey),
            apiSecret = Crypto.encrypt(creds.apiSecret),
        )

    private fun decryptCredentials(creds: BrokerCredentials): BrokerCredentials =
        creds.copy(
            apiKey = Crypto.decrypt(creds.apiKey),
            apiSecret = Crypto.decrypt(creds.apiSecret),
        )

    // Le point d'accès de l'écran d'administration est traité **comme** les
    // identifiants courtier, et pour la même raison : sa clé interne est un
    // secret, et la base locale ne doit jamais en contenir en clair. Ce n'est pas
    // la clé d'un courtier mais c'est celle qui autorise un appel capable
    // d'écrire dans la base de production.
    val adminEndpointFlow: Flow<AdminEndpoint> =
        dao.getAdminEndpointFlow().map { endpoint ->
            decryptEndpoint(endpoint ?: AdminEndpoint())
        }

    suspend fun getAdminEndpoint(): AdminEndpoint =
        decryptEndpoint(dao.getAdminEndpoint() ?: AdminEndpoint())

    suspend fun saveAdminEndpoint(endpoint: AdminEndpoint) {
        dao.saveAdminEndpoint(encryptEndpoint(endpoint))
    }

    private fun encryptEndpoint(endpoint: AdminEndpoint): AdminEndpoint =
        endpoint.copy(apiKey = Crypto.encrypt(endpoint.apiKey))

    private fun decryptEndpoint(endpoint: AdminEndpoint): AdminEndpoint =
        endpoint.copy(apiKey = Crypto.decrypt(endpoint.apiKey))

    suspend fun insertSignal(signal: Signal): Long = dao.insertSignal(signal)

    suspend fun updateSignal(signal: Signal) {
        dao.updateSignal(signal)
    }

    suspend fun getActiveSignals(): List<Signal> = dao.getActiveSignals()

    suspend fun clearSignals() {
        dao.clearSignals()
    }

    suspend fun logInfo(message: String) {
        dao.insertLog(LogEvent(message = message, type = "INFO"))
    }

    suspend fun logWarning(message: String) {
        dao.insertLog(LogEvent(message = message, type = "WARNING"))
    }

    suspend fun logError(message: String) {
        dao.insertLog(LogEvent(message = message, type = "ERROR"))
    }

    suspend fun logTrade(message: String) {
        dao.insertLog(LogEvent(message = message, type = "TRADE"))
    }

    suspend fun logRisk(message: String) {
        dao.insertLog(LogEvent(message = message, type = "RISK"))
    }

    suspend fun clearLogs() {
        dao.clearLogs()
    }

    suspend fun insertInsight(insight: MarketInsight) {
        dao.insertInsight(insight)
    }

    suspend fun clearInsights() {
        dao.clearInsights()
    }

    /**
     * Analyses de démonstration, dans la langue demandée.
     *
     * Les textes viennent de `res/values-<code>/strings.xml` : les mêmes dix langues
     * que le reste de l'interface, sans `when` sur le code de langue. Les
     * champs qui ne sont pas du texte affiché (identifiant, type, score,
     * horodatage) restent des données : le type est une valeur de protocole
     * comparée telle quelle par les écrans.
     */
    fun getLocalizedInsights(languageCode: String): List<MarketInsight> {
        val now = System.currentTimeMillis()
        val res = context.localizedTo(languageCode)
        return listOf(
            MarketInsight(
                id = 1,
                title = res.getString(R.string.demo_insight_cpi_title),
                type = "sentiment",
                score = 0.65,
                content = res.getString(R.string.demo_insight_cpi_content),
                timestamp = now - 500000,
            ),
            MarketInsight(
                id = 2,
                title = res.getString(R.string.demo_insight_geopolitical_title),
                type = "geopolitical",
                score = 0.40,
                content = res.getString(R.string.demo_insight_geopolitical_content),
                timestamp = now - 1000000,
            ),
            MarketInsight(
                id = 3,
                title = res.getString(R.string.demo_insight_fed_title),
                type = "sentiment",
                score = 0.70,
                content = res.getString(R.string.demo_insight_fed_content),
                timestamp = now - 1500000,
            ),
        )
    }

    /**
     * Signaux de démonstration, dans la langue demandée.
     *
     * Même contrat que [getLocalizedInsights] : le libellé est une ressource,
     * la valeur de marché reste une donnée. `status` et `direction` sont des
     * codes de protocole (`BUY`, `EXECUTED`…) traduits au dernier moment par
     * `ui/StringMappings.kt`, jamais stockés traduits.
     */
    fun getLocalizedSignals(languageCode: String): List<Signal> {
        val now = System.currentTimeMillis()
        val dayMs = 86400000L
        val res = context.localizedTo(languageCode)
        return listOf(
            Signal(
                id = 1,
                asset = "BTC",
                direction = "BUY",
                entry = 62400.0,
                stopLoss = 60500.0,
                takeProfit = 65800.0,
                confidence = 0.82,
                taSummary = res.getString(R.string.demo_signal_btc_ema_ta),
                geoSummary = res.getString(R.string.demo_signal_btc_ema_geo),
                sentimentSummary = res.getString(R.string.demo_signal_btc_ema_sentiment),
                reasoning = res.getString(R.string.demo_signal_btc_ema_reasoning),
                status = "CLOSED",
                pnl = 1250.0,
                exitPrice = 65800.0,
                timestamp = now - (6 * dayMs),
            ),
            Signal(
                id = 2,
                asset = "NVDA",
                direction = "BUY",
                entry = 118.5,
                stopLoss = 114.0,
                takeProfit = 126.0,
                confidence = 0.88,
                taSummary = res.getString(R.string.demo_signal_nvda_bull_flag_ta),
                geoSummary = res.getString(R.string.demo_signal_nvda_bull_flag_geo),
                sentimentSummary = res.getString(R.string.demo_signal_nvda_bull_flag_sentiment),
                reasoning = res.getString(R.string.demo_signal_nvda_bull_flag_reasoning),
                status = "CLOSED",
                pnl = 1800.0,
                exitPrice = 126.0,
                timestamp = now - (5 * dayMs),
            ),
            Signal(
                id = 3,
                asset = "AAPL",
                direction = "SELL",
                entry = 228.0,
                stopLoss = 232.0,
                takeProfit = 220.0,
                confidence = 0.62,
                taSummary = res.getString(R.string.demo_signal_aapl_rsi_ta),
                geoSummary = res.getString(R.string.demo_signal_aapl_rsi_geo),
                sentimentSummary = res.getString(R.string.demo_signal_aapl_rsi_sentiment),
                reasoning = res.getString(R.string.demo_signal_aapl_rsi_reasoning),
                status = "CLOSED",
                pnl = -450.0,
                exitPrice = 232.0,
                timestamp = now - (4 * dayMs),
            ),
            Signal(
                id = 4,
                asset = "ETH",
                direction = "BUY",
                entry = 3120.0,
                stopLoss = 3000.0,
                takeProfit = 3380.0,
                confidence = 0.79,
                taSummary = res.getString(R.string.demo_signal_eth_macd_ta),
                geoSummary = res.getString(R.string.demo_signal_eth_macd_geo),
                sentimentSummary = res.getString(R.string.demo_signal_eth_macd_sentiment),
                reasoning = res.getString(R.string.demo_signal_eth_macd_reasoning),
                status = "CLOSED",
                pnl = 950.0,
                exitPrice = 3380.0,
                timestamp = now - (3 * dayMs),
            ),
            Signal(
                id = 5,
                asset = "TSLA",
                direction = "BUY",
                entry = 215.0,
                stopLoss = 208.0,
                takeProfit = 228.0,
                confidence = 0.74,
                taSummary = res.getString(R.string.demo_signal_tsla_support_ta),
                geoSummary = res.getString(R.string.demo_signal_tsla_support_geo),
                sentimentSummary = res.getString(R.string.demo_signal_tsla_support_sentiment),
                reasoning = res.getString(R.string.demo_signal_tsla_support_reasoning),
                status = "CLOSED",
                pnl = 1100.0,
                exitPrice = 228.0,
                timestamp = now - (2 * dayMs),
            ),
            Signal(
                id = 6,
                asset = "GOOGL",
                direction = "SELL",
                entry = 178.0,
                stopLoss = 182.0,
                takeProfit = 171.0,
                confidence = 0.58,
                taSummary = res.getString(R.string.demo_signal_googl_double_top_ta),
                geoSummary = res.getString(R.string.demo_signal_googl_double_top_geo),
                sentimentSummary = res.getString(R.string.demo_signal_googl_double_top_sentiment),
                reasoning = res.getString(R.string.demo_signal_googl_double_top_reasoning),
                status = "CLOSED",
                pnl = -380.0,
                exitPrice = 182.0,
                timestamp = now - (1 * dayMs),
            ),
            Signal(
                id = 7,
                asset = "BTC",
                direction = "BUY",
                entry = 64200.0,
                stopLoss = 62500.0,
                takeProfit = 68000.0,
                confidence = 0.85,
                taSummary = res.getString(R.string.demo_signal_btc_golden_cross_ta),
                geoSummary = res.getString(R.string.demo_signal_btc_golden_cross_geo),
                sentimentSummary = res.getString(R.string.demo_signal_btc_golden_cross_sentiment),
                reasoning = res.getString(R.string.demo_signal_btc_golden_cross_reasoning),
                status = "EXECUTED",
                pnl = 1180.0,
                exitPrice = 67200.0,
                timestamp = now - (4 * 3600000L),
            ),
        )
    }

    suspend fun populateInitialDataIfEmpty() {
        val prefs = dao.getPreferences() ?: UserPreferences().also { dao.savePreferences(it) }
        val lang = prefs.language

        if (dao.getCredentials() == null) {
            dao.saveCredentials(BrokerCredentials())
        }

        // Add localized default insights
        getLocalizedInsights(lang).forEach { dao.insertInsight(it) }

        logInfo("AI Trading Terminal & Risk Guard initialized successfully.")

        if (dao.getAllSignalsList().isEmpty()) {
            getLocalizedSignals(lang).forEach { dao.insertSignal(it) }
        }
    }

    suspend fun localizeDefaultData(languageCode: String) {
        // Update default insights (IDs 1, 2, 3)
        getLocalizedInsights(languageCode).forEach { dao.insertInsight(it) }

        // Update default signals (IDs 1 to 7)
        getLocalizedSignals(languageCode).forEach { dao.insertSignal(it) }
    }
}
