package com.aitrade.api

/**
 * IDs de modèles Gemini centralisés (source unique de vérité côté Android).
 *
 * Doivent rester cohérents avec les valeurs par défaut du backend
 * (`core/config_runtime.py`). Les IDs fournisseurs changent régulièrement :
 * mets-les à jour ici uniquement.
 */
object GeminiModels {
    const val FLASH = "gemini-3.8-flash"
    const val FLASH_LITE = "gemini-3.1-flash-lite"
    const val PRO = "gemini-3.1-pro"
}
