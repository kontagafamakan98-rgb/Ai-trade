try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from core.config_runtime import get_env_config
_cfg = get_env_config()

SUPABASE_URL = _cfg.supabase_url
SUPABASE_KEY = _cfg.supabase_service_key
TELEGRAM_BOT_TOKEN = _cfg.telegram_bot_token
#: Chat privé où partent les comptes-rendus d'ingestion venus d'un canal
#: (`channel_post`) — le canal ne doit rien recevoir de tout ça.
TELEGRAM_ADMIN_CHAT_ID = _cfg.telegram_admin_chat_id
ENCRYPTION_KEY = _cfg.encryption_key
#: Clés **retirées** de l'anneau de chiffrement (voir `utils/encryption.py`) :
#: elles ne servent qu'à rouvrir le chiffré historique, jamais à chiffrer.
ENCRYPTION_KEYS_PREVIOUS = _cfg.encryption_keys_previous

PAPER_TRADING = _cfg.paper_trading
MIN_CONFIDENCE = _cfg.min_confidence
HEADLESS = _cfg.headless

ALPACA_API_KEY = _cfg.alpaca_api_key
ALPACA_SECRET_KEY = _cfg.alpaca_secret_key
ALPACA_BASE_URL = _cfg.alpaca_base_url

WEBHOOK_SECRET = _cfg.webhook_secret
DEFAULT_RISK_PCT = _cfg.default_risk_pct
DEFAULT_PAPER_EQUITY = _cfg.default_paper_equity
MIN_RISK_REWARD_RATIO = _cfg.min_risk_reward_ratio
MAX_POSITION_QTY = _cfg.max_position_qty

FINNHUB_API_KEY = _cfg.finnhub_api_key
GROQ_API_KEY = _cfg.groq_api_key
GEMINI_API_KEY = _cfg.gemini_api_key

# Embeddings Gemini (recherche vectorielle de la base de connaissances).
# Réutilise GEMINI_API_KEY : aucun secret supplémentaire à gérer.
EMBEDDING_MODEL = _cfg.embedding_model

# IDs de modèles LLM centralisés (voir core/config_runtime.py)
GEMINI_MODEL = _cfg.gemini_model
GEMINI_LITE_MODEL = _cfg.gemini_lite_model
GEMINI_PRO_MODEL = _cfg.gemini_pro_model
GROQ_PRIMARY_MODEL = _cfg.groq_primary_model
GROQ_SECONDARY_MODEL = _cfg.groq_secondary_model

# Apprentissage adaptatif : la descente de gradient est désactivée par défaut.
ADAPTIVE_GD_ENABLED = _cfg.adaptive_gd_enabled
ADAPTIVE_GD_MIN_SAMPLES = _cfg.adaptive_gd_min_samples

# Synchronisation Obsidian (via plugin "Obsidian Git" -> dépôt GitHub)
GITHUB_TOKEN = _cfg.github_token
GITHUB_REPO = _cfg.github_repo  # format "utilisateur/nom-du-repo"
GITHUB_BRANCH = _cfg.github_branch
