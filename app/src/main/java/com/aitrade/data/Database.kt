package com.aitrade.data

import android.content.Context
import androidx.room.*
import kotlinx.coroutines.flow.Flow

// --- Entities ---

/**
 * Courtier de démonstration utilisé tant que l'utilisateur n'en a pas choisi un.
 *
 * C'est une marque : elle reste identique dans toutes les langues et ne doit
 * jamais être traduite. Le libellé affiché vient de la ressource
 * `broker_name_alpaca` (`translatable="false"`), pas de cette constante qui
 * sert de valeur de repli en base.
 */
const val DEFAULT_BROKER_NAME = "Alpaca Trading"

@Entity(tableName = "user_preferences")
data class UserPreferences(
    @PrimaryKey val id: Int = 1,
    val watchlist: String = "AAPL,MSFT,GOOGL,BTC,ETH,TSLA,NVDA",
    val riskPct: Double = 1.0,
    val paperEquity: Double = 100000.0,
    val minConfidence: Double = 0.55,
    val maxDailyLossPct: Double = 5.0,
    val maxTotalDrawdownPct: Double = 10.0,
    val maxOpenTrades: Int = 3,
    val language: String = "fr",
) {
    fun getWatchlistList(): List<String> = watchlist.split(",").map { it.trim() }.filter { it.isNotEmpty() }
}

@Entity(tableName = "signals")
data class Signal(
    @PrimaryKey(autoGenerate = true) val id: Int = 0,
    val asset: String,
    val direction: String, // "BUY", "SELL"
    val entry: Double,
    val stopLoss: Double,
    val takeProfit: Double,
    val confidence: Double,
    val taSummary: String,
    val geoSummary: String,
    val sentimentSummary: String,
    val reasoning: String,
    val status: String, // "PENDING", "EXECUTED", "BLOCKED_RISK", "CLOSED"
    val pnl: Double = 0.0,
    val exitPrice: Double = 0.0,
    val timestamp: Long = System.currentTimeMillis(),
)

@Entity(tableName = "broker_credentials")
data class BrokerCredentials(
    @PrimaryKey val id: Int = 1,
    val brokerName: String = DEFAULT_BROKER_NAME,
    val apiKey: String = "",
    val apiSecret: String = "",
    val paperMode: Boolean = true,
    val accountNumber: String = "",
    val customEndpoint: String = "",
)

/**
 * Point d'accès de l'écran d'administration : l'URL du backend et la clé interne.
 *
 * La clé est **chiffrée au repos** (voir `TradingRepository`), exactement comme
 * les identifiants courtier, et elle vit ici plutôt que dans `BuildConfig` : une
 * clé compilée dans l'APK est une clé que quiconque décompile l'application
 * possède, et celle-ci ouvre un endpoint qui sait **écrire** dans la base de
 * production.
 *
 * Un identifiant fixe (`1`), comme `user_preferences` : il n'y a qu'un point
 * d'accès, remplacé à chaque enregistrement.
 */
@Entity(tableName = "admin_endpoint")
data class AdminEndpoint(
    @PrimaryKey val id: Int = 1,
    val baseUrl: String = "",
    val apiKey: String = "",
) {
    /**
     * Utilisable seulement si les deux sont renseignés.
     *
     * La moitié d'un point d'accès ne sert à rien : une URL sans clé se ferait
     * refuser par le backend (`401`), une clé sans URL ne désigne rien. Le dire
     * ici évite que l'écran ait à décider de son côté.
     */
    val configured: Boolean get() = baseUrl.isNotBlank() && apiKey.isNotBlank()
}

@Entity(tableName = "log_events")
data class LogEvent(
    @PrimaryKey(autoGenerate = true) val id: Int = 0,
    val message: String,
    val type: String, // "INFO", "WARNING", "ERROR", "TRADE", "RISK"
    val timestamp: Long = System.currentTimeMillis(),
)

@Entity(tableName = "market_insights")
data class MarketInsight(
    @PrimaryKey(autoGenerate = true) val id: Int = 0,
    val title: String,
    val type: String, // "geopolitical", "sentiment"
    val score: Double,
    val content: String,
    val timestamp: Long = System.currentTimeMillis(),
)

@Entity(tableName = "economic_events")
data class EconomicEventEntity(
    @PrimaryKey val id: String,
    val eventId: String,
    val title: String,
    val country: String,
    val currency: String,
    val eventDateIso: String,
    val impact: String, // "High", "Medium", "Low", "Non-Economic"
    val forecast: String? = null,
    val previous: String? = null,
    val actual: String? = null,
    val forecastNum: Double? = null,
    val previousNum: Double? = null,
    val actualNum: Double? = null,
    val timestamp: Long = System.currentTimeMillis(),
)

@Entity(tableName = "trade_post_mortems")
data class TradePostMortemEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val signalId: Long,
    val asset: String,
    val direction: String,
    val outcome: String, // "WON" or "LOST"
    val entryPrice: Double,
    val exitPrice: Double,
    val pnl: Double,
    val errorType: String,
    val learnedLesson: String,
    val timestamp: Long = System.currentTimeMillis(),
)

// --- DAOs ---

@Dao
interface TradingDao {
    @Query("SELECT * FROM user_preferences WHERE id = 1 LIMIT 1")
    fun getPreferencesFlow(): Flow<UserPreferences?>

    @Query("SELECT * FROM user_preferences WHERE id = 1 LIMIT 1")
    suspend fun getPreferences(): UserPreferences?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun savePreferences(preferences: UserPreferences)

    @Query("SELECT * FROM signals ORDER BY timestamp DESC")
    fun getAllSignalsFlow(): Flow<List<Signal>>

    @Query("SELECT * FROM signals ORDER BY timestamp ASC")
    suspend fun getAllSignalsList(): List<Signal>

    @Query("SELECT * FROM signals WHERE status = 'EXECUTED'")
    suspend fun getActiveSignals(): List<Signal>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertSignal(signal: Signal): Long

    @Update
    suspend fun updateSignal(signal: Signal)

    @Query("DELETE FROM signals")
    suspend fun clearSignals()

    @Query("SELECT * FROM broker_credentials WHERE id = 1 LIMIT 1")
    fun getCredentialsFlow(): Flow<BrokerCredentials?>

    @Query("SELECT * FROM broker_credentials WHERE id = 1 LIMIT 1")
    suspend fun getCredentials(): BrokerCredentials?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun saveCredentials(credentials: BrokerCredentials)

    @Query("SELECT * FROM admin_endpoint WHERE id = 1 LIMIT 1")
    fun getAdminEndpointFlow(): Flow<AdminEndpoint?>

    @Query("SELECT * FROM admin_endpoint WHERE id = 1 LIMIT 1")
    suspend fun getAdminEndpoint(): AdminEndpoint?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun saveAdminEndpoint(endpoint: AdminEndpoint)

    @Query("SELECT * FROM log_events ORDER BY timestamp DESC LIMIT 100")
    fun getLogsFlow(): Flow<List<LogEvent>>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertLog(log: LogEvent)

    @Query("DELETE FROM log_events")
    suspend fun clearLogs()

    @Query("SELECT * FROM market_insights ORDER BY timestamp DESC LIMIT 50")
    fun getInsightsFlow(): Flow<List<MarketInsight>>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertInsight(insight: MarketInsight)

    @Query("DELETE FROM market_insights")
    suspend fun clearInsights()

    @Query("SELECT * FROM economic_events ORDER BY timestamp DESC LIMIT 100")
    fun getEconomicEventsFlow(): Flow<List<EconomicEventEntity>>

    @Query("SELECT * FROM economic_events ORDER BY timestamp DESC LIMIT 100")
    suspend fun getEconomicEventsList(): List<EconomicEventEntity>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertEconomicEvents(events: List<EconomicEventEntity>)

    @Query("DELETE FROM economic_events")
    suspend fun clearEconomicEvents()

    @Query("SELECT * FROM trade_post_mortems ORDER BY timestamp DESC LIMIT 50")
    fun getPostMortemsFlow(): Flow<List<TradePostMortemEntity>>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertPostMortem(postMortem: TradePostMortemEntity)
}

// --- Database ---

@Database(
    entities = [
        UserPreferences::class,
        Signal::class,
        BrokerCredentials::class,
        LogEvent::class,
        MarketInsight::class,
        EconomicEventEntity::class,
        TradePostMortemEntity::class,
        AdminEndpoint::class,
    ],
    // 6 : `admin_endpoint` (le point d'accès de l'écran d'administration).
    // La base est recréée dans ce cas (`fallbackToDestructiveMigration`) : elle
    // ne contient que des données de démonstration, et perdre un historique
    // simulé est préférable à une migration écrite pour rien.
    version = 6,
    exportSchema = false,
)
abstract class AppDatabase : RoomDatabase() {
    abstract fun tradingDao(): TradingDao

    companion object {
        @Volatile
        private var instance: AppDatabase? = null

        fun getDatabase(context: Context): AppDatabase =
            instance ?: synchronized(this) {
                val database =
                    Room
                        .databaseBuilder(
                            context.applicationContext,
                            AppDatabase::class.java,
                            "ai_trade_database",
                        ).fallbackToDestructiveMigration()
                        .build()
                instance = database
                database
            }
    }
}
