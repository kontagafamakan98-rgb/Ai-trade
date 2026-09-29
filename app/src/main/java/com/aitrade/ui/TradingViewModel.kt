package com.aitrade.ui

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.aitrade.api.AdminProbeFailure
import com.aitrade.api.AdminProbeOutcome
import com.aitrade.api.AdminProbeRunner
import com.aitrade.api.GeminiApiClient
import com.aitrade.api.GeminiModels
import com.aitrade.data.*
import com.aitrade.engine.*
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch
import kotlinx.serialization.Serializable
import java.util.UUID
import kotlin.random.Random

class TradingViewModel(
    application: Application,
) : AndroidViewModel(application) {
    private val db = AppDatabase.getDatabase(application)
    val repository = TradingRepository(db.tradingDao(), application)

    // --- State Flows ---

    val preferences =
        repository.preferencesFlow.stateIn(
            scope = viewModelScope,
            started = SharingStarted.WhileSubscribed(5000),
            initialValue = UserPreferences(),
        )

    private var cachedStrings: Pair<String, AppStrings>? = null

    /**
     * Chaînes de la langue choisie, hors composition Compose.
     *
     * Le ViewModel alimente le journal, les insights et le chat : il lui faut la
     * même source de vérité que les écrans. `appStrings` projette toutes les clés
     * d'un coup, on mémorise donc la dernière langue pour que les boucles (scan
     * de la watchlist, gestion des positions) ne la reconstruisent pas à chaque
     * tour, tout en suivant immédiatement un changement de langue.
     */
    private fun strings(language: String = preferences.value.language): AppStrings {
        cachedStrings?.let { (cachedLanguage, value) ->
            if (cachedLanguage == language) return value
        }
        val built = appStrings(getApplication<Application>().localizedTo(language))
        cachedStrings = language to built
        return built
    }

    /**
     * Remet le message d'accueil dans [language], tant que l'utilisateur n'a pas
     * engagé de conversation.
     */
    private fun refreshWelcomeMessage(language: String) {
        val messages = _chatMessages.value
        if (messages.size == 1 && !messages.first().isUser) {
            _chatMessages.value = listOf(ChatMessage(strings(language).chatWelcomeMessage, false))
        }
    }

    val credentials =
        repository.credentialsFlow.stateIn(
            scope = viewModelScope,
            started = SharingStarted.WhileSubscribed(5000),
            initialValue = BrokerCredentials(),
        )

    val signals =
        repository.signalsFlow.stateIn(
            scope = viewModelScope,
            started = SharingStarted.WhileSubscribed(5000),
            initialValue = emptyList(),
        )

    val logs =
        repository.logsFlow.stateIn(
            scope = viewModelScope,
            started = SharingStarted.WhileSubscribed(5000),
            initialValue = emptyList(),
        )

    val insights =
        repository.insightsFlow.stateIn(
            scope = viewModelScope,
            started = SharingStarted.WhileSubscribed(5000),
            initialValue = emptyList(),
        )

    // --- Administration : la sonde Supabase du backend (`/admin/supabase/*`) ---
    //
    // L'écran d'administration ne fabrique rien : il envoie ce que l'opérateur a
    // saisi et affiche la réponse du backend. Sans point d'accès enregistré, la
    // conduite est de le **dire** plutôt que d'afficher un rapport inventé — un
    // rapport fabriqué sur l'appareil ressemblerait à une vérification réussie,
    // et c'est précisément le silence que cette sonde existe pour rompre.
    val adminEndpoint =
        repository.adminEndpointFlow.stateIn(
            scope = viewModelScope,
            started = SharingStarted.WhileSubscribed(5000),
            initialValue = AdminEndpoint(),
        )

    private val _adminProbe = MutableStateFlow(AdminProbeUiState())
    val adminProbe = _adminProbe.asStateFlow()

    fun saveAdminEndpoint(
        baseUrl: String,
        apiKey: String,
    ) {
        viewModelScope.launch {
            repository.saveAdminEndpoint(
                AdminEndpoint(baseUrl = baseUrl.trim(), apiKey = apiKey.trim()),
            )
            // Le rapport précédent décrivait une autre destination : le garder
            // ferait lire le verdict d'hier sur la base d'aujourd'hui.
            _adminProbe.value = AdminProbeUiState()
        }
    }

    fun runAdminCheck(only: List<String> = emptyList()) {
        runAdminProbe { endpoint ->
            AdminProbeRunner.check(endpoint.baseUrl, endpoint.apiKey, only)
        }
    }

    fun runAdminRoundtrip(tables: List<String>) {
        // Rien à écrire sans table nommée, et le backend refuse une liste vide :
        // on ne l'envoie pas pour recevoir ce qu'on sait déjà.
        if (tables.isEmpty()) return
        runAdminProbe { endpoint ->
            AdminProbeRunner.roundtrip(endpoint.baseUrl, endpoint.apiKey, tables)
        }
    }

    private fun runAdminProbe(call: suspend (AdminEndpoint) -> AdminProbeOutcome) {
        val endpoint = adminEndpoint.value
        if (!endpoint.configured) {
            _adminProbe.value =
                AdminProbeUiState(
                    outcome = AdminProbeOutcome.Failed(AdminProbeFailure.NOT_CONFIGURED),
                )
            return
        }
        _adminProbe.value = AdminProbeUiState(running = true)
        viewModelScope.launch {
            val outcome = call(endpoint)
            _adminProbe.value = AdminProbeUiState(outcome = outcome)
        }
    }

    // --- Local Simulation States ---

    private val _isLoopRunning = MutableStateFlow(false)
    val isLoopRunning = _isLoopRunning.asStateFlow()

    private val _currentEquity = MutableStateFlow(100000.0)
    val currentEquity = _currentEquity.asStateFlow()

    private val _dailyStartBalance = MutableStateFlow(100000.0)
    val dailyStartBalance = _dailyStartBalance.asStateFlow()

    private val _startingBalance = MutableStateFlow(100000.0)
    val startingBalance = _startingBalance.asStateFlow()

    private val _aiAnalysisState = MutableStateFlow<AiState>(AiState.Idle)
    val aiAnalysisState = _aiAnalysisState.asStateFlow()

    private var loopJob: Job? = null

    sealed interface AiState {
        object Idle : AiState

        object Loading : AiState

        data class Success(
            val score: Double,
            val reasoning: String,
        ) : AiState

        data class Error(
            val message: String,
        ) : AiState
    }

    // --- AI Chat, Sync, and VIP Subscription States ---

    private val _chatMessages =
        MutableStateFlow<List<ChatMessage>>(
            listOf(
                ChatMessage(
                    // Langue par défaut au démarrage : `init` la corrige dès
                    // que la préférence enregistrée est lue.
                    strings().chatWelcomeMessage,
                    false,
                ),
            ),
        )
    val chatMessages = _chatMessages.asStateFlow()

    private val _userEmail = MutableStateFlow<String?>(null)
    val userEmail = _userEmail.asStateFlow()

    private val _isFirebaseConnected = MutableStateFlow(false)
    val isFirebaseConnected = _isFirebaseConnected.asStateFlow()

    private val _isFirestoreSynced = MutableStateFlow(false)
    val isFirestoreSynced = _isFirestoreSynced.asStateFlow()

    private val _isVipUser = MutableStateFlow(false)
    val isVipUser = _isVipUser.asStateFlow()

    private val _searchGroundingEnabled = MutableStateFlow(false)
    val searchGroundingEnabled = _searchGroundingEnabled.asStateFlow()

    private val _mapsGroundingEnabled = MutableStateFlow(false)
    val mapsGroundingEnabled = _mapsGroundingEnabled.asStateFlow()

    private val _highThinkingEnabled = MutableStateFlow(false)
    val highThinkingEnabled = _highThinkingEnabled.asStateFlow()

    private val _lowLatencyEnabled = MutableStateFlow(false)
    val lowLatencyEnabled = _lowLatencyEnabled.asStateFlow()

    private val _isChatLoading = MutableStateFlow(false)
    val isChatLoading = _isChatLoading.asStateFlow()

    // --- Multi-Agent Research State ---
    private val _multiAgentReport = MutableStateFlow<MultiAgentResearchReport?>(null)
    val multiAgentReport = _multiAgentReport.asStateFlow()

    private val _isMultiAgentResearching = MutableStateFlow(false)
    val isMultiAgentResearching = _isMultiAgentResearching.asStateFlow()

    // --- Quant Backtest, System Health & Memory States ---
    private val _backtestResult = MutableStateFlow<BacktestResult?>(null)
    val backtestResult = _backtestResult.asStateFlow()

    private val _systemHealth = MutableStateFlow<SystemHealthState>(WorkerQueue.checkHealth(null))
    val systemHealth = _systemHealth.asStateFlow()

    val quantMemory: StateFlow<QuantMemorySummary?> =
        signals
            .map { sigs ->
                QuantPerformanceMemory.computeQuantMemory(sigs)
            }.stateIn(
                scope = viewModelScope,
                started = SharingStarted.WhileSubscribed(5000),
                initialValue = null,
            )

    val adaptiveLearningProfile: StateFlow<AdaptiveLearningProfile> =
        signals
            .map { sigs ->
                QuantPerformanceMemory.computeAdaptiveProfile(sigs)
            }.stateIn(
                scope = viewModelScope,
                started = SharingStarted.WhileSubscribed(5000),
                initialValue =
                    AdaptiveLearningProfile(
                        taWeightPct = 40,
                        macroWeightPct = 30,
                        sentimentWeightPct = 30,
                        adaptiveMinConfidence = 0.58,
                        consecutiveLosses = 0,
                        learnedRules =
                            listOf(
                                strings().learnRuleRsiOverbought,
                                strings().learnRuleHighImpactNews,
                            ),
                        postMortemLessons = listOf(strings().learnFeedbackActive),
                    ),
            )

    fun triggerSelfLearningReview() {
        viewModelScope.launch {
            val profile = adaptiveLearningProfile.value
            repository.logInfo(
                String.format(
                    strings().logSelfLearningReview,
                    profile.taWeightPct,
                    profile.macroWeightPct,
                    profile.sentimentWeightPct,
                    String.format("%.2f", profile.adaptiveMinConfidence),
                ),
            )
        }
    }

    init {
        // Démarre le consommateur de la file de jobs (sinon la file sature).
        WorkerQueue.startConsumer(viewModelScope)
        viewModelScope.launch {
            repository.populateInitialDataIfEmpty()
            val prefs = repository.getPreferences()
            _currentEquity.value = prefs.paperEquity
            _dailyStartBalance.value = prefs.paperEquity
            _startingBalance.value = prefs.paperEquity
            // Run initial default backtest
            _backtestResult.value =
                BacktestEngine.runBacktest(initialCapital = prefs.paperEquity, timeframeDays = 365, minConfidence = prefs.minConfidence)
            // Run initial multi-agent research for BTC
            runMultiAgentResearch("BTC")
            refreshWelcomeMessage(prefs.language)
        }
    }

    fun runBacktest(
        days: Int = 365,
        strategy: TradingStrategy = TradingStrategy.MULTI_FACTOR_COMPOSITE,
    ) {
        viewModelScope.launch(Dispatchers.Default) {
            val prefs = repository.getPreferences()
            val result =
                BacktestEngine.runBacktest(
                    initialCapital = _currentEquity.value,
                    timeframeDays = days,
                    minConfidence = prefs.minConfidence,
                    strategy = strategy,
                    runMonteCarlo = true,
                )
            _backtestResult.value = result
            repository.logInfo(
                String.format(
                    strings(prefs.language).logBacktestSummary,
                    result.strategy.displayName,
                    result.timeframeLabel,
                    String.format("%.2f", result.totalReturnPct),
                    String.format("%.2f", result.sharpeRatio),
                    String.format("%.2f", result.monteCarlo?.var95Pct ?: 0.0),
                ),
            )
            if (result.isSynthetic) {
                repository.logWarning(strings(prefs.language).logBacktestSynthetic)
            }
        }
    }

    fun runMultiAgentResearch(asset: String) {
        _isMultiAgentResearching.value = true
        viewModelScope.launch(Dispatchers.IO) {
            val closes = MarketService.getCloses(asset)
            val currentInsights = insights.value
            val prefs = repository.getPreferences()
            val report =
                MultiAgentResearchEngine.runMultiAgentResearch(
                    asset = asset,
                    closes = closes,
                    insights = currentInsights,
                    languageCode = prefs.language,
                )
            _multiAgentReport.value = report
            _isMultiAgentResearching.value = false

            // Store insight in Repository
            val ui = strings(prefs.language)
            val consensus = actionLabel(ui, report.consensusAction)
            val insightTitle = String.format(ui.insightMultiAgentTitle, asset, consensus, report.confidencePct)
            repository.insertInsight(
                MarketInsight(
                    title = insightTitle,
                    type = "sentiment",
                    score = report.consensusScore,
                    content = report.synthesizedReasoning,
                ),
            )
            repository.logInfo(
                String.format(ui.logMultiAgentResearch, asset, consensus, report.confidencePct),
            )
        }
    }

    fun injectMultiAgentSignalIntoEngine(report: MultiAgentResearchReport) {
        viewModelScope.launch(Dispatchers.IO) {
            val prefs = repository.getPreferences()
            val signal = MultiAgentResearchEngine.convertReportToSignal(report)
            val activeSignals = repository.getActiveSignals()
            val creds = repository.getCredentials()

            evaluateAndExecuteSignal(
                signal = signal,
                prefs = prefs,
                activeCount = activeSignals.size,
                credentials = creds,
            )
            repository.logInfo(String.format(strings(prefs.language).logConsensusInjected, report.asset))
        }
    }

    fun resetCircuitBreaker() {
        PortfolioRiskGuard.resetCircuitBreaker()
        viewModelScope.launch {
            repository.logRisk(strings().logCircuitBreakerReset)
        }
    }

    // --- Toggles & Setup ---

    fun toggleAutoLoop() {
        if (_isLoopRunning.value) {
            _isLoopRunning.value = false
            loopJob?.cancel()
            loopJob = null
            viewModelScope.launch {
                repository.logInfo(strings().logLoopPaused)
            }
        } else {
            _isLoopRunning.value = true
            PortfolioRiskGuard.resetCircuitBreaker()
            viewModelScope.launch {
                repository.logInfo(strings().logLoopStarted)
            }
            startLoop()
        }
    }

    private fun startLoop() {
        loopJob?.cancel()
        loopJob =
            viewModelScope.launch(Dispatchers.IO) {
                PortfolioRiskGuard.resetCircuitBreaker()
                repository.logInfo(strings().logLoopQueueStarted)
                var cycleCount = 0
                while (_isLoopRunning.value) {
                    try {
                        cycleCount++
                        val prefs = repository.getPreferences()
                        val activeSignals = repository.getActiveSignals()
                        val currentList = insights.value
                        val credentials = repository.getCredentials()

                        // Update Health State
                        _systemHealth.value = WorkerQueue.checkHealth(credentials)

                        // 1. Audit Portfolio Risk before loop processing
                        val riskAudit =
                            PortfolioRiskGuard.auditPortfolioRisk(
                                prefs = prefs,
                                activeSignals = activeSignals,
                                currentEquity = _currentEquity.value,
                                startingBalance = _startingBalance.value,
                                dailyStartBalance = _dailyStartBalance.value,
                            )

                        if (!riskAudit.allowed && riskAudit.isCircuitBreakerTripped) {
                            repository.logRisk(String.format(strings(prefs.language).logPortfolioRiskBreach, riskAudit.reason))
                            _isLoopRunning.value = false
                            break
                        }

                        // 2. Enqueue Reconcile Job & Run Broker Reconciliation
                        WorkerQueue.enqueueJob(TradingJob(id = UUID.randomUUID().toString(), type = "RECONCILE_POSITIONS"))
                        val reconReport = PortfolioRiskGuard.reconcileBrokerPositions(activeSignals, credentials)

                        // 3. Tick prices in MarketService
                        MarketService.tickPrices()

                        // 4. Manage existing active positions
                        manageActivePositions(activeSignals)

                        // 5. Scan Watchlist and generate multi-factor signals
                        if (riskAudit.allowed) {
                            val watchlist = prefs.getWatchlistList()
                            var executedInCycle = false

                            for (asset in watchlist) {
                                val closes = MarketService.getCloses(asset)
                                if (closes.size >= 30) {
                                    val signal =
                                        DecisionEngine.analyze(
                                            asset = asset,
                                            closes = closes,
                                            insights = currentList,
                                            minConfidence = prefs.minConfidence,
                                            forceDemoMode = false,
                                        )

                                    if (signal != null) {
                                        val alreadyOpen = activeSignals.any { it.asset.uppercase() == asset.uppercase() }
                                        if (!alreadyOpen) {
                                            evaluateAndExecuteSignal(signal, prefs, activeSignals.size, credentials)
                                            executedInCycle = true
                                        }
                                    }
                                }
                            }

                            // If no technical signal met the confidence threshold on flat data, trigger an initial setup on cycle #1 if no active signals are open
                            if (!executedInCycle && activeSignals.isEmpty() && cycleCount == 1) {
                                val targetAsset = watchlist.firstOrNull() ?: "BTC"
                                val closes = MarketService.getCloses(targetAsset)
                                val synthSignal =
                                    DecisionEngine.analyze(
                                        asset = targetAsset,
                                        closes = closes,
                                        insights = currentList,
                                        minConfidence = 0.50,
                                        forceDemoMode = true,
                                    )
                                if (synthSignal != null) {
                                    evaluateAndExecuteSignal(synthSignal, prefs, activeSignals.size, credentials)
                                    repository.logInfo(
                                        String.format(strings(prefs.language).logBootstrapSignal, targetAsset),
                                    )
                                }
                            } else if (!executedInCycle) {
                                if (cycleCount % 3 == 0) {
                                    repository.logInfo(
                                        String.format(
                                            strings(prefs.language).logCycleScan,
                                            cycleCount,
                                            watchlist.size,
                                            (prefs.minConfidence * 100).toInt(),
                                        ),
                                    )
                                }
                            }
                        }
                    } catch (e: Exception) {
                        repository.logError(String.format(strings().logLoopError, e.message))
                    }
                    delay(3000)
                }
            }
    }

    private suspend fun manageActivePositions(activeSignals: List<Signal>) {
        val ui = strings()
        for (signal in activeSignals) {
            val currentPrice = MarketService.getLastPrice(signal.asset)
            val qty = RiskGuard.computeQty(signal.entry, signal.stopLoss, _currentEquity.value, preferences.value.riskPct, signal.asset)

            if (signal.direction == "BUY") {
                if (currentPrice <= signal.stopLoss) {
                    val realizedPnl = (signal.stopLoss - signal.entry) * qty
                    val closedSignal =
                        signal.copy(
                            status = "CLOSED",
                            exitPrice = signal.stopLoss,
                            pnl = realizedPnl,
                        )
                    repository.updateSignal(closedSignal)
                    adjustEquity(realizedPnl)
                    repository.logTrade(
                        String.format(
                            ui.logPositionClosedSl,
                            directionLabel(ui, signal.direction),
                            String.format("%.4f", qty),
                            signal.asset,
                            String.format("%.2f", signal.stopLoss),
                            String.format("%.2f", realizedPnl),
                        ),
                    )
                } else if (currentPrice >= signal.takeProfit) {
                    val realizedPnl = (signal.takeProfit - signal.entry) * qty
                    val closedSignal =
                        signal.copy(
                            status = "CLOSED",
                            exitPrice = signal.takeProfit,
                            pnl = realizedPnl,
                        )
                    repository.updateSignal(closedSignal)
                    adjustEquity(realizedPnl)
                    repository.logTrade(
                        String.format(
                            ui.logPositionClosedTp,
                            directionLabel(ui, signal.direction),
                            String.format("%.4f", qty),
                            signal.asset,
                            String.format("%.2f", signal.takeProfit),
                            String.format("%.2f", realizedPnl),
                        ),
                    )
                }
            } else if (signal.direction == "SELL") {
                if (currentPrice >= signal.stopLoss) {
                    val realizedPnl = (signal.entry - signal.stopLoss) * qty
                    val closedSignal =
                        signal.copy(
                            status = "CLOSED",
                            exitPrice = signal.stopLoss,
                            pnl = realizedPnl,
                        )
                    repository.updateSignal(closedSignal)
                    adjustEquity(realizedPnl)
                    repository.logTrade(
                        String.format(
                            ui.logPositionClosedSl,
                            directionLabel(ui, signal.direction),
                            String.format("%.4f", qty),
                            signal.asset,
                            String.format("%.2f", signal.stopLoss),
                            String.format("%.2f", realizedPnl),
                        ),
                    )
                } else if (currentPrice <= signal.takeProfit) {
                    val realizedPnl = (signal.entry - signal.takeProfit) * qty
                    val closedSignal =
                        signal.copy(
                            status = "CLOSED",
                            exitPrice = signal.takeProfit,
                            pnl = realizedPnl,
                        )
                    repository.updateSignal(closedSignal)
                    adjustEquity(realizedPnl)
                    repository.logTrade(
                        String.format(
                            ui.logPositionClosedTp,
                            directionLabel(ui, signal.direction),
                            String.format("%.4f", qty),
                            signal.asset,
                            String.format("%.2f", signal.takeProfit),
                            String.format("%.2f", realizedPnl),
                        ),
                    )
                }
            }
        }
    }

    private suspend fun evaluateAndExecuteSignal(
        signal: Signal,
        prefs: UserPreferences,
        activeCount: Int,
        credentials: BrokerCredentials? = null,
    ) {
        val ui = strings(prefs.language)

        // Multi-Tenant Strict Isolation check
        val tenantAudit =
            UserIsolationGuard.validateTenantAuthorization(
                credentials,
                isLiveRequested =
                    credentials?.paperMode == false && credentials?.apiKey?.isNotBlank() == true,
            )
        if (!tenantAudit.isAuthorized) {
            val blockedSignal =
                signal.copy(
                    status = "BLOCKED_RISK",
                    reasoning = tenantAudit.reason,
                )
            repository.insertSignal(blockedSignal)
            repository.logRisk(String.format(ui.logTenantBreach, tenantAudit.reason))
            return
        }

        val riskCheck =
            RiskGuard.checkRisk(
                prefs = prefs,
                currentBalance = _currentEquity.value,
                activeTradesCount = activeCount,
                startingBalance = _startingBalance.value,
                dailyStartBalance = _dailyStartBalance.value,
            )

        if (riskCheck.allowed) {
            val executedSignal = signal.copy(status = "EXECUTED")
            repository.insertSignal(executedSignal)

            val qty = RiskGuard.computeQty(signal.entry, signal.stopLoss, _currentEquity.value, prefs.riskPct, signal.asset)
            WorkerQueue.enqueueJob(TradingJob(id = UUID.randomUUID().toString(), type = "EXECUTE_ORDER", asset = signal.asset))
            repository.logTrade(
                String.format(
                    ui.logOrderExecuted,
                    tenantAudit.mode,
                    directionLabel(ui, signal.direction),
                    String.format("%.4f", qty),
                    signal.asset,
                    String.format("%.2f", signal.entry),
                    String.format("%.2f", signal.stopLoss),
                    String.format("%.2f", signal.takeProfit),
                ),
            )
        } else {
            val blockedSignal =
                signal.copy(
                    status = "BLOCKED_RISK",
                    reasoning = String.format(ui.signalReasonBlockedByRisk, riskCheck.reason),
                )
            repository.insertSignal(blockedSignal)
            repository.logRisk(String.format(ui.logSignalBlocked, signal.asset, riskCheck.reason))
        }
    }

    private fun adjustEquity(pnl: Double) {
        _currentEquity.value += pnl
        viewModelScope.launch {
            val prefs = repository.getPreferences()
            repository.savePreferences(prefs.copy(paperEquity = _currentEquity.value))
        }
    }

    // --- Action Methods ---

    fun saveRiskPreferences(
        riskPct: Double,
        maxDailyLossPct: Double,
        maxTotalDrawdownPct: Double,
        maxOpenTrades: Int,
        minConfidence: Double,
    ) {
        viewModelScope.launch {
            val current = repository.getPreferences()
            val updated =
                current.copy(
                    riskPct = riskPct,
                    maxDailyLossPct = maxDailyLossPct,
                    maxTotalDrawdownPct = maxTotalDrawdownPct,
                    maxOpenTrades = maxOpenTrades,
                    minConfidence = minConfidence,
                )
            repository.savePreferences(updated)
            repository.logInfo(strings().logRiskUpdated)
        }
    }

    fun updateWatchlist(watchlistString: String) {
        viewModelScope.launch {
            val current = repository.getPreferences()
            repository.savePreferences(current.copy(watchlist = watchlistString))
            repository.logInfo(String.format(strings().logWatchlistUpdated, watchlistString))
        }
    }

    fun updateLanguage(languageCode: String) {
        viewModelScope.launch {
            val current = repository.getPreferences()
            repository.savePreferences(current.copy(language = languageCode))
            repository.logInfo(String.format(strings(languageCode).logLanguageUpdated, languageCode))

            // Localize existing default insights and signals in database
            repository.localizeDefaultData(languageCode)

            // Update welcome message if chat hasn't started custom conversation
            refreshWelcomeMessage(languageCode)
        }
    }

    fun saveBrokerCredentials(
        brokerName: String = DEFAULT_BROKER_NAME,
        apiKey: String = "",
        secretKey: String = "",
        paperMode: Boolean = true,
        accountNumber: String = "",
        customEndpoint: String = "",
    ) {
        viewModelScope.launch {
            repository.saveCredentials(
                BrokerCredentials(
                    brokerName = brokerName,
                    apiKey = apiKey,
                    apiSecret = secretKey,
                    paperMode = paperMode,
                    accountNumber = accountNumber,
                    customEndpoint = customEndpoint,
                ),
            )
            val ui = strings()
            repository.logInfo(
                String.format(ui.logBrokerSaved, brokerName, if (paperMode) ui.modePaper else ui.modeLive),
            )
        }
    }

    fun toggleLiveRealTrading(isLive: Boolean) {
        viewModelScope.launch {
            val current = repository.getCredentials()
            val updated = current.copy(paperMode = !isLive)
            repository.saveCredentials(updated)
            if (isLive) {
                repository.logWarning(strings().logSwitchedLive)
            } else {
                repository.logInfo(strings().logSwitchedPaper)
            }
        }
    }

    fun resetSimulation() {
        viewModelScope.launch {
            repository.clearSignals()
            repository.clearLogs()
            _currentEquity.value = 100000.0
            _dailyStartBalance.value = 100000.0
            _startingBalance.value = 100000.0
            val prefs = repository.getPreferences()
            repository.savePreferences(prefs.copy(paperEquity = 100000.0))
            repository.logInfo(strings().logDemoReset)
        }
    }

    fun updateDemoCapital(amount: Double) {
        viewModelScope.launch {
            _currentEquity.value = amount
            _dailyStartBalance.value = amount
            _startingBalance.value = amount
            val prefs = repository.getPreferences()
            repository.savePreferences(prefs.copy(paperEquity = amount))
            repository.logInfo(String.format(strings().logDemoBalanceSet, String.format("%,.2f", amount)))
        }
    }

    // --- Gemini AI Analysis ---

    fun runAiAnalysis(asset: String) {
        _aiAnalysisState.value = AiState.Loading
        viewModelScope.launch(Dispatchers.IO) {
            val headlines = MarketService.getNewsHeadlines(asset)
            val prefs = repository.getPreferences()
            val userLang = prefs.language
            val ui = strings(userLang)

            if (GeminiApiClient.hasValidKey()) {
                val rawResult = GeminiApiClient.analyzeMarketContext(asset, headlines, userLang)
                if (rawResult != null) {
                    try {
                        // Parse score and reasoning from raw text
                        var score = 0.5
                        var reasoning = ""
                        rawResult.lines().forEach { line ->
                            if (line.uppercase().startsWith("SCORE:")) {
                                score = line.substringAfter("SCORE:").trim().toDoubleOrNull() ?: 0.5
                            } else if (line.uppercase().startsWith("REASONING:")) {
                                reasoning = line.substringAfter("REASONING:").trim()
                            }
                        }

                        if (reasoning.isEmpty()) {
                            reasoning = rawResult.trim()
                        }

                        _aiAnalysisState.value = AiState.Success(score, reasoning)

                        val apiTitle = String.format(ui.insightSentimentApiTitle, asset)

                        // Push into DB as market insight
                        repository.insertInsight(
                            MarketInsight(
                                title = apiTitle,
                                type = "sentiment",
                                score = score,
                                content = reasoning,
                            ),
                        )
                        repository.logInfo(String.format(ui.logSentimentApiDone, asset, score))
                    } catch (e: Exception) {
                        _aiAnalysisState.value = AiState.Error(String.format(ui.errorParsing, e.message))
                    }
                } else {
                    _aiAnalysisState.value = AiState.Error(ui.errorNullResponse)
                }
            } else {
                // FALLBACK AI SIMULATOR (Localized)
                delay(1200) // Realistic loading
                val randomScore = Random.nextDouble(0.35, 0.85)
                val trendDirection =
                    when {
                        randomScore >= 0.58 -> ui.directionBullish
                        randomScore <= 0.42 -> ui.directionBearish
                        else -> ui.directionNeutral
                    }

                val simulatedReasoning = String.format(ui.insightSimulatedReasoning, trendDirection, asset)

                _aiAnalysisState.value = AiState.Success(randomScore, simulatedReasoning)

                val fallbackTitle = String.format(ui.insightSentimentSimulatorTitle, asset)

                // Push insight to DB
                repository.insertInsight(
                    MarketInsight(
                        title = fallbackTitle,
                        type = "sentiment",
                        score = randomScore,
                        content = simulatedReasoning,
                    ),
                )
                repository.logInfo(
                    String.format(ui.logSentimentFallbackDone, asset, String.format("%.2f", randomScore)),
                )
            }
        }
    }

    // --- Dynamic Chat, Firebase & VIP Premium Operations ---

    fun signInWithGoogle(email: String) {
        viewModelScope.launch {
            _userEmail.value = email
            _isFirebaseConnected.value = true
            _isFirestoreSynced.value = true
            repository.logInfo(String.format(strings().logFirebaseSignedIn, email))
        }
    }

    fun signOut() {
        viewModelScope.launch {
            _userEmail.value = null
            _isFirebaseConnected.value = false
            _isFirestoreSynced.value = false
            repository.logInfo(strings().logFirebaseSignedOut)
        }
    }

    fun triggerFirestoreSync() {
        viewModelScope.launch {
            if (!_isFirebaseConnected.value) {
                repository.logError(strings().errorFirestoreUnauthenticated)
                return@launch
            }
            _isFirestoreSynced.value = false
            delay(1200) // Simulated network sync latency
            _isFirestoreSynced.value = true
            repository.logInfo(String.format(strings().logFirestoreSynced, signals.value.size))
        }
    }

    fun upgradeToVip() {
        viewModelScope.launch {
            _isVipUser.value = true
            repository.logInfo(strings().logVipActivated)
        }
    }

    fun toggleSearchGrounding(enabled: Boolean) {
        _searchGroundingEnabled.value = enabled
    }

    fun toggleMapsGrounding(enabled: Boolean) {
        _mapsGroundingEnabled.value = enabled
    }

    fun toggleHighThinking(enabled: Boolean) {
        _highThinkingEnabled.value = enabled
        if (enabled) {
            _lowLatencyEnabled.value = false // Mutually exclusive for better reasoning
        }
    }

    fun toggleLowLatency(enabled: Boolean) {
        _lowLatencyEnabled.value = enabled
        if (enabled) {
            _highThinkingEnabled.value = false // Mutually exclusive for speed
        }
    }

    fun sendChatMessage(
        messageText: String,
        attachedImageBase64: String? = null,
        mimeType: String? = null,
    ) {
        if (messageText.isBlank() && attachedImageBase64 == null) return

        viewModelScope.launch(Dispatchers.IO) {
            // Add User Message to thread
            val userMsg =
                ChatMessage(
                    content = if (messageText.isNotBlank()) messageText else strings().chatAnalyzingImage,
                    isUser = true,
                )
            _chatMessages.update { it + userMsg }
            _isChatLoading.value = true

            val currentHistory = _chatMessages.value
            val prompt = messageText

            // Select Model based on prompt requirements & toggle states
            val model =
                when {
                    _lowLatencyEnabled.value -> GeminiModels.FLASH_LITE
                    _highThinkingEnabled.value -> GeminiModels.PRO
                    attachedImageBase64 != null -> GeminiModels.PRO
                    else -> GeminiModels.FLASH
                }

            // Build contents history
            val contents = mutableListOf<com.aitrade.api.Content>()

            // Add historical messages (limit to last 10 messages for speed & context constraints)
            val historyToUse = currentHistory.takeLast(10)
            historyToUse.forEach { msg ->
                if (msg === userMsg && attachedImageBase64 != null && mimeType != null) {
                    contents.add(
                        com.aitrade.api.Content(
                            role = "user",
                            parts =
                                listOf(
                                    com.aitrade.api.Part(text = msg.content),
                                    com.aitrade.api.Part(
                                        inlineData = com.aitrade.api.InlineData(mimeType = mimeType, data = attachedImageBase64),
                                    ),
                                ),
                        ),
                    )
                } else {
                    contents.add(
                        com.aitrade.api.Content(
                            role = if (msg.isUser) "user" else "model",
                            parts = listOf(com.aitrade.api.Part(text = msg.content)),
                        ),
                    )
                }
            }

            val prefs = repository.getPreferences()
            val userLang = prefs.language
            val ui = strings(userLang)
            val languageName =
                when (AppLanguage.fromCode(userLang)) {
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

            val systemInstruction =
                """
                You are an expert financial adviser, market technician, and automated trading terminal assistant.
                CRITICAL INSTRUCTION: You MUST answer the user strictly in $languageName (Language code: $userLang). Do NOT reply in English unless the user explicitly requests it or language code is 'en'.
                Answer all trading, charting, and technical questions professionally, clearly, and completely in $languageName.
                If the user has Search Grounding enabled, provide up-to-date information in $languageName.
                If Maps Grounding is enabled, help them locate physical desks, crypto ATMs, or financial institutions in $languageName.
                If High Thinking is active, give extremely deep technical breakdowns in $languageName.
                If they ask about premium features, kindly direct them to upgrade in $languageName.
                """.trimIndent()

            var responseText: String? = null
            if (com.aitrade.api.GeminiApiClient
                    .hasValidKey()
            ) {
                responseText =
                    com.aitrade.api.GeminiApiClient.generateChatResponse(
                        contents = contents,
                        systemInstruction = systemInstruction,
                        model = model,
                        enableSearch = _searchGroundingEnabled.value,
                        enableMaps = _mapsGroundingEnabled.value,
                        enableThinking = _highThinkingEnabled.value,
                    )
            }

            if (responseText != null) {
                _chatMessages.update { it + ChatMessage(responseText, false) }
            } else {
                // Localized Simulated Local AI response when no key is entered
                delay(1200)
                val fallbackResponse =
                    buildString {
                        append(ui.chatSimFallbackHeader)
                        if (attachedImageBase64 != null) {
                            append(ui.chatSimImageDone)
                            append(ui.chatSimImageAnalysis)
                            append(ui.chatSimImageReco)
                        } else {
                            append(String.format(ui.chatSimRegarding, prompt))
                            append(ui.chatSimIntro)
                            append(ui.chatSimBulletVolume)
                            append(ui.chatSimBulletModes)
                        }
                    }
                _chatMessages.update { it + ChatMessage(fallbackResponse, false) }
            }
            _isChatLoading.value = false
        }
    }
}

@Serializable
data class ChatMessage(
    val content: String,
    val isUser: Boolean,
    val timestamp: Long = System.currentTimeMillis(),
)
