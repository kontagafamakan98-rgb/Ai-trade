from config import SUPABASE_URL, SUPABASE_KEY
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

#: Les **clés opaques** de la nouvelle génération (2025) : `sb_secret_…` côté
#: serveur, `sb_publishable_…` côté navigateur. Ce ne sont pas des JWT — c'est
#: tout leur intérêt — et c'est aussi ce qui les faisait refuser ici.
OPAQUE_KEY_PREFIXES = ("sb_secret_", "sb_publishable_")

#: Ce que le contrôle de forme de `create_client` exige : `a.b` (ou `a.b.c`). Une
#: clé opaque lui est présentée suivie de ce suffixe, puis remise en place : la
#: bibliothèque ne la voit sous cette forme que le temps de **son** contrôle.
JWT_SHAPED_SUFFIX = "sig"

#: L'en-tête où supabase-py dépose la clé (l'orthographe de la bibliothèque ;
#: HTTP ne distingue pas la casse, la passerelle lit bien `apikey`).
API_KEY_HEADER = "apiKey"


def is_opaque_key(key: Optional[str]) -> bool:
    """La clé est-elle de la nouvelle génération (donc pas un JWT) ?

    La question est posée au **préfixe**, comme `supabase_key_role`
    (`core/config_runtime.py`) : une clé opaque est faite pour être lisible, pas
    décodable, et sa forme est tout ce qu'on en sache sans le réseau.
    """
    return bool(key) and str(key).startswith(OPAQUE_KEY_PREFIXES)


def build_supabase_client(url: str, key: str) -> Any:
    """Le client Supabase, y compris pour une clé **opaque** (`sb_secret_…`).

    `create_client` refuse toute clé qui n'a pas la forme d'un JWT
    (`SupabaseException: Invalid API key`, dans `supabase/_sync/client.py`) : les
    clés de la nouvelle génération ne sont pas des JWT, donc le client ne se
    construisait **pas du tout** — et comme ce module avale l'exception, la base
    paraissait vide. Deux corrections, dans cet ordre :

    1. **la construction** : la clé est présentée sous une forme que le contrôle
       accepte, puis remplacée par la vraie valeur juste après ;
    2. **l'en-tête** : supabase-py dépose la clé **deux fois**, en `apiKey` et en
       `Authorization: Bearer <clé>`. C'est ce second en-tête que la passerelle
       refuse pour une clé opaque — elle y attend un JWT d'**utilisateur** —, donc
       il est retiré : une clé opaque voyage en `apikey`, et rien d'autre. Le
       remettre ferait échouer chaque requête avec « Invalid API key », ce que la
       base vide rendrait invisible.

    `options.headers` est le **même dictionnaire** que celui des sous-clients
    (`postgrest`, `storage`, `functions` le lisent à leur construction paresseuse,
    `auth` y lit ses en-têtes à chaque appel) : le corriger ici les corrige tous,
    et aucun n'est construit avant ce retour.

    Une clé **JWT** (l'ancienne génération) garde le chemin et les en-têtes
    d'origine : sa place dans `Authorization` est justement ce qui la fait
    fonctionner aujourd'hui.
    """
    if not is_opaque_key(key):
        return create_client(url, key)

    client = create_client(url, f"{key}.{JWT_SHAPED_SUFFIX}")
    client.supabase_key = key
    headers = client.options.headers
    for name in [name for name in headers if name.lower() == API_KEY_HEADER.lower()]:
        headers[name] = key
    headers.setdefault(API_KEY_HEADER, key)
    for name in [name for name in headers if name.lower() == "authorization"]:
        headers.pop(name)
    return client


try:
    from supabase import create_client, Client
    supabase: Optional[Any] = build_supabase_client(SUPABASE_URL, SUPABASE_KEY)
    #: Pourquoi le client n'a pas pu être construit (`None` s'il l'a été).
    #: Sans ce motif, une dépendance absente ou une URL malformée se traduisent
    #: par le **même** symptôme que « la base est vide » : toutes les fonctions de
    #: ce module rendent `{}` / `[]`, sans jamais lever.
    SUPABASE_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover - dépend de l'environnement
    supabase = None
    SUPABASE_ERROR = f"{type(exc).__name__}: {exc}"

#: Tables dont l'absence casse une fonctionnalité. Liste tenue ici, à côté du
#: client : c'est elle que vérifie `scripts/check_supabase.py`, et elle couvre
#: aussi bien celles créées par les migrations que les tables historiques
#: (`users`, `insights`, `pending_signals` — voir la migration 011).
REQUIRED_TABLES = (
    "users",                  # /start, notifications de tous les utilisateurs
    "user_preferences",       # watchlist et seuils par utilisateur
    "user_risk_state",        # garde-fous de risque (migration 004)
    "user_broker_credentials",   # clés broker chiffrées (migration 002)
    "pending_signals",        # signaux proposés, validés, réglés
    "insights",               # fil collectif (news, sentiment, canaux)
    "economic_events",        # calendrier macro (Forex Factory)
    "macro_bias_logs",        # journal des décisions macro
    "knowledge_base",         # notes et règles (migration 003)
    "knowledge_chunks",       # texte indexé et vecteurs (migrations 008/009)
    "knowledge_media",        # description des médias stockés (migration 008)
    "trade_post_mortems",     # post-mortems de l'apprentissage
    "adaptive_model_weights",  # poids appris (migration 006)
)

#: Tables **facultatives** : leur absence dégrade une fonctionnalité sans la
#: casser, par construction (voir `database/settings.py`).
OPTIONAL_TABLES = ("bot_settings",)


def client_status() -> Dict[str, Any]:
    """État du client Supabase : construit, ou la raison de son absence.

    Rend `{"ready": bool, "error": str | None}`. Sert à distinguer « la base ne
    répond rien » de « le client n'a jamais été construit » : les fonctions de ce
    module rendent la même chose dans les deux cas.
    """
    return {"ready": supabase is not None, "error": SUPABASE_ERROR}

def insert_insight(insight: Dict[str, Any]) -> Dict:
    if not supabase: return {}
    insight.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    res = supabase.table("insights").insert(insight).execute()
    # Garde défensive : ne jamais indexer une réponse sans données.
    if not res or not res.data or not isinstance(res.data, list):
        return {}
    return res.data[0] or {}


def prune_insights(keep: int = 1000) -> int:
    """Supprime les insights les plus anciens au-delà de `keep` lignes.

    La table `insights` reçoit de nouvelles lignes à chaque cycle de collecte
    (toutes les 15 min) sans jamais être purgée. Sans cette rétention, elle
    grossit indéfiniment.
    Retourne le nombre estimé de lignes supprimées (0 si indisponible).
    """
    if not supabase:
        return 0
    keep = max(int(keep), 100)
    try:
        total = (
            supabase.table("insights").select("id", count="exact").execute().count
            or 0
        )
        if total <= keep:
            return 0

        # Récupère l'horodatage de coupure (la `keep`-ième ligne la plus récente).
        res = (
            supabase.table("insights")
            .select("created_at")
            .order("created_at", desc=True)
            .range(keep, keep)
            .execute()
        )
        rows = res.data or []
        cutoff = rows[0].get("created_at") if rows else None
        if not cutoff:
            return 0
        supabase.table("insights").delete().lt("created_at", cutoff).execute()
        return max(0, total - keep)
    except Exception as e:
        print(f"⚠️ prune_insights warning: {e}")
        return 0

def get_recent_insights(asset: Optional[str] = None, limit: int = 30) -> List[Dict]:
    if not supabase: return []
    try:
        q = supabase.table("insights").select("*").order("created_at", desc=True).limit(limit)
        if asset:
            q = q.eq("asset", asset)
        return q.execute().data or []
    except Exception as e:
        print(f"⚠️ get_recent_insights fallback: {e}")
        return []

def create_pending_signal(user_id: str, signal: Dict) -> str:
    res = supabase.table("pending_signals").insert({
        "user_id": user_id,
        "signal": signal,
        "status": "pending"
    }).execute()
    if not res.data or not isinstance(res.data, list) or not res.data[0]:
        raise RuntimeError("Failed to create pending signal: Supabase returned no data")
    return str(res.data[0].get("id"))

def update_signal_status(signal_id: str, status: str, execution_result: Optional[Dict] = None):
    payload = {
        "status": status,
        "validated_at": datetime.now(timezone.utc).isoformat()
    }
    if execution_result:
        payload["execution_result"] = execution_result
    supabase.table("pending_signals").update(payload).eq("id", signal_id).execute()

def upsert_economic_events(events: List[Dict[str, Any]]) -> int:
    """Upsert list of economic events into economic_events table in Supabase."""
    if not events:
        return 0
    try:
        now_str = datetime.now(timezone.utc).isoformat()
        for e in events:
            e["updated_at"] = now_str
        res = supabase.table("economic_events").upsert(events, on_conflict="id").execute()
        return len(res.data) if res.data else len(events)
    except Exception as err:
        print(f"⚠️ Supabase upsert_economic_events warning: {err}")
        return len(events)

def get_economic_events(currency: Optional[str] = None, impact: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieve economic events from Supabase."""
    try:
        q = supabase.table("economic_events").select("*").order("event_date", desc=True).limit(limit)
        if currency:
            q = q.eq("currency", currency.upper())
        if impact:
            q = q.eq("impact", impact)
        res = q.execute()
        return res.data or []
    except Exception as err:
        print(f"⚠️ Supabase get_economic_events warning: {err}")
        return []

def log_macro_decision(symbol: str, currency: str, macro_score: float, news_risk_level: str, decision: str, reasoning: str):
    """Log macro decision and risk filter details in macro_bias_logs."""
    try:
        payload = {
            "symbol": symbol,
            "currency": currency,
            "macro_score": round(macro_score, 4),
            "news_risk_level": news_risk_level,
            "decision": decision,
            "reasoning": reasoning,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        supabase.table("macro_bias_logs").insert(payload).execute()
    except Exception as err:
        print(f"⚠️ Supabase log_macro_decision warning: {err}")
