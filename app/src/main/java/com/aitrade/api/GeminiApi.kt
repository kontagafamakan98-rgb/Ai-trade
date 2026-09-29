package com.aitrade.api

import com.aitrade.BuildConfig
import com.aitrade.ui.AppLanguage
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.kotlinx.serialization.asConverterFactory
import retrofit2.http.Body
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query
import java.util.concurrent.TimeUnit

// --- Gemini REST API Request & Response Models ---

@Serializable
data class GenerateContentRequest(
    val contents: List<Content>,
    val generationConfig: GenerationConfig? = null,
    val systemInstruction: Content? = null,
    val tools: List<Tool>? = null,
)

@Serializable
data class Content(
    val role: String? = null,
    val parts: List<Part>,
)

@Serializable
data class Part(
    val text: String? = null,
    val inlineData: InlineData? = null,
)

@Serializable
data class InlineData(
    val mimeType: String,
    val data: String, // Base64 encoded string
)

@Serializable
data class Tool(
    val googleSearch: GoogleSearch? = null,
    val googleMaps: GoogleMaps? = null,
)

@Serializable
class GoogleSearch

@Serializable
class GoogleMaps

@Serializable
data class GenerationConfig(
    val responseFormat: ResponseFormat? = null,
    val temperature: Float? = null,
    val thinkingConfig: ThinkingConfig? = null,
)

@Serializable
data class ResponseFormat(
    val type: String, // e.g. "application/json"
)

@Serializable
data class ThinkingConfig(
    val thinkingLevel: String, // "low", "medium", "high"
)

@Serializable
data class GenerateContentResponse(
    val candidates: List<Candidate>? = null,
)

@Serializable
data class Candidate(
    val content: Content? = null,
)

// --- Retrofit API Service ---

interface GeminiApiService {
    @POST("v1beta/models/{model}:generateContent")
    suspend fun generateContent(
        @Path("model") model: String,
        @Query("key") apiKey: String,
        @Body request: GenerateContentRequest,
    ): GenerateContentResponse
}

object GeminiApiClient {
    private const val BASE_URL = "https://generativelanguage.googleapis.com/"

    private val okHttpClient =
        OkHttpClient
            .Builder()
            .connectTimeout(60, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .build()

    val service: GeminiApiService by lazy {
        val json = Json { ignoreUnknownKeys = true }
        val retrofit =
            Retrofit
                .Builder()
                .baseUrl(BASE_URL)
                .client(okHttpClient)
                .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
                .build()
        retrofit.create(GeminiApiService::class.java)
    }

    /**
     * Determines if a valid Gemini API key is available.
     */
    fun hasValidKey(): Boolean {
        val key = BuildConfig.GEMINI_API_KEY
        return key.isNotEmpty() && key != "YOUR_GEMINI_API_KEY" && !key.startsWith("YOUR_")
    }

    /**
     * Call the Gemini API to analyze an asset's market context.
     */
    suspend fun analyzeMarketContext(
        asset: String,
        newsHeadlines: String,
        languageCode: String = "fr",
    ): String? {
        if (!hasValidKey()) return null

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

        val prompt =
            """
            Analyze the market sentiment for $asset based on these recent developments:
            $newsHeadlines
            
            CRITICAL LANGUAGE MANDATE:
            The REASONING MUST be written strictly in $languageName (Language code: $languageCode).
            
            Provide your response in raw text with two section headers (keep header names SCORE: and REASONING: exact):
            SCORE: <Double value between 0.0 and 1.0 representing bullishness where 0.0 is extremely bearish, 1.0 is extremely bullish, and 0.5 is neutral>
            REASONING: <2-3 sentence logical technical explanation written in $languageName>
            """.trimIndent()

        val request =
            GenerateContentRequest(
                contents = listOf(Content(parts = listOf(Part(text = prompt)))),
                systemInstruction =
                    Content(
                        parts =
                            listOf(
                                Part(
                                    text =
                                        "You are an emotionless AI market analyst. Output only the requested SCORE and REASONING " +
                                            "headers. Output reasoning strictly in $languageName.",
                                ),
                            ),
                    ),
            )

        return try {
            val response = service.generateContent(GeminiModels.FLASH, BuildConfig.GEMINI_API_KEY, request)
            response.candidates
                ?.firstOrNull()
                ?.content
                ?.parts
                ?.firstOrNull()
                ?.text
        } catch (e: Exception) {
            e.printStackTrace()
            null
        }
    }

    /**
     * Call the Gemini API to generate a chat response with flexible configuration parameters.
     */
    suspend fun generateChatResponse(
        contents: List<Content>,
        systemInstruction: String,
        model: String,
        enableSearch: Boolean,
        enableMaps: Boolean,
        enableThinking: Boolean,
    ): String? {
        if (!hasValidKey()) return null

        val toolsList =
            mutableListOf<Tool>()
                .apply {
                    if (enableSearch) add(Tool(googleSearch = GoogleSearch()))
                    if (enableMaps) add(Tool(googleMaps = GoogleMaps()))
                }.takeIf { it.isNotEmpty() }

        val genConfig =
            if (enableThinking && model.contains("pro")) {
                // Under instruction: Do not set maxOutputTokens for high thinking level
                GenerationConfig(
                    thinkingConfig = ThinkingConfig(thinkingLevel = "high"),
                    temperature = null,
                )
            } else {
                GenerationConfig(temperature = 0.7f)
            }

        val request =
            GenerateContentRequest(
                contents = contents,
                generationConfig = genConfig,
                systemInstruction = Content(parts = listOf(Part(text = systemInstruction))),
                tools = toolsList,
            )

        return try {
            val response = service.generateContent(model, BuildConfig.GEMINI_API_KEY, request)
            response.candidates
                ?.firstOrNull()
                ?.content
                ?.parts
                ?.firstOrNull()
                ?.text
        } catch (e: Exception) {
            e.printStackTrace()
            null
        }
    }
}
