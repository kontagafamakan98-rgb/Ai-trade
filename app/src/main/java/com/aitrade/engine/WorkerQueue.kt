package com.aitrade.engine

import com.aitrade.data.BrokerCredentials
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.launch
import java.util.concurrent.atomic.AtomicInteger

enum class TaskPriority { HIGH, STANDARD, LOW }

data class TradingJob(
    val id: String,
    val type: String, // "SCAN_WATCHLIST", "EVALUATE_SIGNAL", "EXECUTE_ORDER", "RECONCILE_POSITIONS", "HEALTH_CHECK"
    val asset: String? = null,
    val priority: TaskPriority = TaskPriority.STANDARD,
    val timestamp: Long = System.currentTimeMillis(),
)

/**
 * Codes internes de l'état courtier exposé par [SystemHealthState].
 *
 * Ils sont volontairement indépendants de la langue : l'UI les traduit via
 * `strings.health_broker_*`. Aucune comparaison de texte localisé ne doit être
 * nécessaire pour connaître la couleur ou le libellé affiché.
 */
object BrokerStateCode {
    const val PAPER_SANDBOX = "paper_sandbox"
    const val ALPACA_PAPER = "alpaca_paper"
    const val ALPACA_LIVE = "alpaca_live"
}

data class SystemHealthState(
    val dbConnected: Boolean,
    val brokerStatus: String,
    val workerQueueThroughput: Int,
    val activeCircuitBreakers: List<String>,
    val memoryUsageMb: Double,
    val lastSyncTime: Long,
)

object WorkerQueue {
    /**
     * File non bloquante : capacité illimitée + `trySend` (jamais de
     * suspension). L'ancienne implémentation utilisait un channel borné à 100
     * sans consommateur : au 101e job, `send()` se suspendait indéfiniment et
     * gelait la boucle de trading.
     */
    private val channel = Channel<TradingJob>(capacity = Channel.UNLIMITED)
    val jobFlow: Flow<TradingJob> = channel.receiveAsFlow()

    private val processedJobs = AtomicInteger(0)
    private var consumerStarted = false

    /**
     * Démarre (une seule fois) le consommateur qui draine la file. Les jobs
     * sont des notifications de cycle déjà appliquées de façon synchrone par
     * le ViewModel : on les consomme pour éviter toute saturation.
     */
    @Synchronized
    fun startConsumer(scope: CoroutineScope) {
        if (consumerStarted) return
        consumerStarted = true
        scope.launch {
            for (job in channel) {
                processedJobs.incrementAndGet()
                // Hook d'observabilité : le traitement métier est effectué
                // en amont ; ici on garantit seulement que la file se vide.
                _ = job
            }
        }
    }

    /** Enfile un job sans jamais bloquer. Retourne `true` si mis en file. */
    fun enqueueJob(job: TradingJob): Boolean = channel.trySend(job).isSuccess

    fun getProcessedJobsCount(): Int = processedJobs.get()

    fun checkHealth(credentials: BrokerCredentials?): SystemHealthState {
        val runtime = Runtime.getRuntime()
        val usedMemMb = (runtime.totalMemory() - runtime.freeMemory()) / (1024.0 * 1024.0)

        // Codes internes stables : la couche UI les projette sur les chaînes
        // localisées (`strings.health_broker_*`). Ne jamais y mettre de texte
        // affichable, sinon la traduction exigerait de comparer des libellés.
        val brokerState =
            when {
                credentials == null || credentials.apiKey.isBlank() -> BrokerStateCode.PAPER_SANDBOX
                credentials.paperMode -> BrokerStateCode.ALPACA_PAPER
                else -> BrokerStateCode.ALPACA_LIVE
            }

        return SystemHealthState(
            dbConnected = true,
            brokerStatus = brokerState,
            workerQueueThroughput = processedJobs.get(),
            activeCircuitBreakers = emptyList(),
            memoryUsageMb = usedMemMb,
            lastSyncTime = System.currentTimeMillis(),
        )
    }
}

object UserIsolationGuard {
    data class MultiTenantAudit(
        val isAuthorized: Boolean,
        val reason: String,
        val mode: String,
    )

    /**
     * Strict User-Tenant Isolation Check (No shared Alpaca credentials fallback in live production)
     */
    fun validateTenantAuthorization(
        credentials: BrokerCredentials?,
        isLiveRequested: Boolean,
    ): MultiTenantAudit {
        if (isLiveRequested) {
            if (credentials == null || credentials.apiKey.isBlank() || credentials.apiSecret.isBlank()) {
                return MultiTenantAudit(
                    isAuthorized = false,
                    reason = "STRICT MULTI-TENANT ISOLATION: Production live order blocked. User must provide personal Alpaca API Keys.",
                    mode = "STRICT_LIVE_BLOCKED",
                )
            }
            if (credentials.paperMode) {
                return MultiTenantAudit(
                    isAuthorized = false,
                    reason = "STRICT MULTI-TENANT ISOLATION: Broker account is set to Paper mode. Cannot execute live real-money trades.",
                    mode = "PAPER_MODE_MISMATCH",
                )
            }
        }

        return MultiTenantAudit(
            isAuthorized = true,
            reason = "Tenant Credentials Validated: User Isolated Environment OK.",
            mode = if (credentials?.paperMode == false) "ISOLATED_LIVE" else "ISOLATED_PAPER",
        )
    }
}
