package com.aitrade.api

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import retrofit2.HttpException
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Header
import retrofit2.http.POST
import retrofit2.http.Query
import java.io.IOException
import java.util.concurrent.TimeUnit

// --- Administration : la sonde Supabase du backend, vue depuis l'application ---
//
// Le backend expose déjà les deux appels (`api/admin_router.py`) :
//
//   GET  /admin/supabase/check      les trois niveaux, SANS rien écrire
//   POST /admin/supabase/roundtrip  écrit, relit puis supprime sur les tables nommées
//
// Les deux sont protégés par la clé interne, en en-tête `X-API-Key`. C'est la
// seule clé jamais lue depuis `BuildConfig` : elle se saisit dans l'écran
// d'administration et se conserve **chiffrée** sur l'appareil, comme les
// identifiants courtier. La compiler dans l'APK la mettrait entre les mains de
// quiconque décompile l'application, pour un endpoint qui sait écrire dans la
// base de production.
//
// Le rapport est celui du script, tel quel : l'application l'affiche, elle ne le
// reformule pas. Ses noms de vérification et ses détails sont les mots du
// backend, pas des libellés d'interface — les traduire ici les ferait diverger
// de ce que dit `python scripts/check_supabase.py`.

@Serializable
data class AdminProbeCheck(
    @SerialName("name") val name: String = "",
    @SerialName("ok") val ok: Boolean = false,
    @SerialName("detail") val detail: String = "",
)

@Serializable
data class AdminProbeSection(
    @SerialName("title") val title: String = "",
    @SerialName("checks") val checks: List<AdminProbeCheck> = emptyList(),
    @SerialName("ok") val ok: Boolean = false,
)

@Serializable
data class AdminProbeSelection(
    @SerialName("requested") val requested: List<String> = emptyList(),
    @SerialName("tables") val tables: List<String> = emptyList(),
)

/**
 * Le rapport de la sonde, tel que le backend le rend.
 *
 * `leftovers` est toujours présent (vide en temps normal) ; `writes` est `null`
 * pour une consultation et la liste des tables réellement écrites pour un
 * aller-retour — c'est ce qui distingue « je regarde » de « j'écris ».
 */
@Serializable
data class AdminProbeReport(
    @SerialName("sections") val sections: List<AdminProbeSection> = emptyList(),
    @SerialName("ok") val ok: Boolean = false,
    @SerialName("leftovers") val leftovers: List<String> = emptyList(),
    @SerialName("selection") val selection: AdminProbeSelection? = null,
    @SerialName("writes") val writes: List<String>? = null,
    @SerialName("command") val command: String = "",
)

@Serializable
data class AdminRoundtripRequest(
    @SerialName("tables") val tables: List<String>,
)

/** Les deux appels d'administration, avec la clé en en-tête — jamais en `Query`. */
interface AdminProbeService {
    @GET("admin/supabase/check")
    suspend fun check(
        @Header("X-API-Key") apiKey: String,
        @Query("only") only: List<String>? = null,
    ): AdminProbeReport

    @POST("admin/supabase/roundtrip")
    suspend fun roundtrip(
        @Header("X-API-Key") apiKey: String,
        @Body request: AdminRoundtripRequest,
    ): AdminProbeReport
}

/** Pourquoi la sonde n'a pas rendu de rapport — trois cas, trois phrases. */
enum class AdminProbeFailure {
    /** URL ou clé absente : rien n'a été envoyé, et ça se dit. */
    NOT_CONFIGURED,

    /** L'URL n'est pas exploitable : Retrofit refuse de construire le service. */
    BAD_URL,

    /** Le backend a répondu, mais en refusant (401, 400 sur un nom inconnu). */
    REFUSED,

    /** Rien n'a répondu : réseau coupé, serveur éteint, adresse injoignable. */
    UNREACHABLE,
}

/** Ce que l'écran affiche : un rapport, ou l'explication de son absence. */
sealed interface AdminProbeOutcome {
    data class Report(
        val report: AdminProbeReport,
    ) : AdminProbeOutcome

    data class Failed(
        val kind: AdminProbeFailure,
        val detail: String = "",
        val code: Int = 0,
    ) : AdminProbeOutcome
}

/**
 * L'appel réseau, et la traduction de chaque échec en une cause nommée.
 *
 * Un `catch (Exception)` fourre-tout rendrait « injoignable » et « refusé »
 * indiscernables — or ce sont deux conduites opposées : l'un se réessaie, l'autre
 * dit qu'un nom de table est inconnu ou que la clé est fausse. Les exceptions de
 * Retrofit et d'OkHttp sont donc nommées une par une, et le message du serveur
 * (qui **liste les choix** quand une table est refusée) est repris tel quel.
 */
object AdminProbeRunner {
    //: L'aller-retour fait une trentaine de requêtes réseau côté serveur : le
    //: délai est plus large que celui de Gemini, sans quoi un aller-retour long
    //: se lirait comme une panne de réseau.
    private const val TIMEOUT_SECONDS = 120L
    private const val MAX_DETAIL = 300

    @Volatile
    private var cached: Pair<String, AdminProbeService>? = null

    /**
     * Le service pour cette URL, mémorisé par URL.
     *
     * `Retrofit.Builder().baseUrl(...)` **lève** si l'URL n'est pas valide (et
     * exige un `/` final) : c'est pour ça que la construction est ici, sous
     * `try`, et non au moment de la saisie.
     */
    fun serviceFor(baseUrl: String): AdminProbeService {
        val normalized = if (baseUrl.endsWith("/")) baseUrl else "$baseUrl/"
        cached?.let { (url, service) ->
            if (url == normalized) return service
        }
        val json = Json {
            ignoreUnknownKeys = true
            coerceInputValues = true
        }
        val okHttp =
            OkHttpClient
                .Builder()
                .connectTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
                .readTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
                .writeTimeout(TIMEOUT_SECONDS, TimeUnit.SECONDS)
                .build()
        val service =
            Retrofit
                .Builder()
                .baseUrl(normalized)
                .client(okHttp)
                .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
                .build()
                .create(AdminProbeService::class.java)
        cached = normalized to service
        return service
    }

    suspend fun check(
        baseUrl: String,
        apiKey: String,
        only: List<String> = emptyList(),
    ): AdminProbeOutcome = guarded { serviceFor(baseUrl).check(apiKey, only.ifEmpty { null }) }

    suspend fun roundtrip(
        baseUrl: String,
        apiKey: String,
        tables: List<String>,
    ): AdminProbeOutcome = guarded { serviceFor(baseUrl).roundtrip(apiKey, AdminRoundtripRequest(tables)) }

    private suspend fun guarded(block: suspend () -> AdminProbeReport): AdminProbeOutcome =
        try {
            AdminProbeOutcome.Report(block())
        } catch (e: IllegalArgumentException) {
            AdminProbeOutcome.Failed(AdminProbeFailure.BAD_URL, detail(e.message))
        } catch (e: HttpException) {
            AdminProbeOutcome.Failed(
                AdminProbeFailure.REFUSED,
                detail(refusalDetail(e) ?: e.message()),
                e.code(),
            )
        } catch (e: IOException) {
            AdminProbeOutcome.Failed(AdminProbeFailure.UNREACHABLE, detail(e.message))
        } catch (e: Exception) {
            AdminProbeOutcome.Failed(AdminProbeFailure.UNREACHABLE, detail(e.message))
        }

    /** Le corps d'erreur du serveur : c'est lui qui nomme les tables acceptées. */
    private fun refusalDetail(e: HttpException): String? =
        try {
            e.response()?.errorBody()?.string()?.take(MAX_DETAIL)
        } catch (ignored: Exception) {
            null
        }

    private fun detail(message: String?): String = (message ?: "").take(MAX_DETAIL)
}
