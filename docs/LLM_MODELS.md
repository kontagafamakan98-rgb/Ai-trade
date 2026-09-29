# IDs des modèles LLM

Les IDs de modèles fournisseurs changent régulièrement. Pour éviter de les
disperser en dur dans le code, ils sont centralisés :

| Contexte | Fichier | Variables |
|---|---|---|
| Backend Python | `core/config_runtime.py` | `GEMINI_MODEL`, `GEMINI_LITE_MODEL`, `GEMINI_PRO_MODEL`, `GROQ_PRIMARY_MODEL`, `GROQ_SECONDARY_MODEL` |
| App Android | `app/src/main/java/com/aitrade/api/GeminiModels.kt` | `GeminiModels.FLASH`, `FLASH_LITE`, `PRO` |

## Valeurs par défaut

| Rôle | Modèle |
|---|---|
| Gemini Flash (défaut) | `gemini-3.8-flash` |
| Gemini Flash-Lite (basse latence) | `gemini-3.1-flash-lite` |
| Gemini Pro (raisonnement) | `gemini-3.1-pro` |
| Groq primaire | `openai/gpt-oss-120b` |
| Groq secondaire (vérification croisée) | `qwen/qwen3.6-27b` |

## Surcharge

Côté backend, définis les variables d'environnement correspondantes
(`GEMINI_MODEL`, `GROQ_PRIMARY_MODEL`, ...) pour changer un modèle sans
toucher au code.

Côté Android, modifie les constantes de `GeminiModels.kt`.

> ⚠️ En cas de `404 model not found`, vérifie l'ID auprès de la documentation
> officielle du fournisseur (Gemini API / Groq console) et mets à jour la
> valeur. Les deux stacks doivent rester cohérentes.
