"""
Stockage chiffré des identifiants de courtier (API key/secret) par
utilisateur, pour que chaque client exécute ses ordres sur SON PROPRE
compte Alpaca plutôt que sur une clé partagée.

Les clés ne sont JAMAIS stockées ni loguées en clair — chiffrées avec
Fernet (utils/encryption.py) avant tout passage en base.
"""
from typing import Optional, Dict, Any
from database.supabase_client import supabase
from utils.encryption import encrypt, decrypt

TABLE = "user_broker_credentials"


def set_broker_credentials(user_id: str, api_key: str, api_secret: str, paper: bool = True) -> None:
    supabase.table(TABLE).upsert({
        "user_id": str(user_id),
        "broker": "alpaca",
        "api_key_enc": encrypt(api_key),
        "api_secret_enc": encrypt(api_secret),
        "paper": paper,
    }).execute()


def get_broker_credentials(user_id: str) -> Optional[Dict[str, Any]]:
    """Retourne les identifiants déchiffrés, ou None si absents/invalides."""
    res = (
        supabase.table(TABLE)
        .select("*")
        .eq("user_id", str(user_id))
        .maybe_single()
        .execute()
    )
    if not res.data:
        return None
    row = res.data
    try:
        return {
            "api_key": decrypt(row["api_key_enc"]),
            "api_secret": decrypt(row["api_secret_enc"]),
            "paper": row.get("paper", True),
            "broker": row.get("broker", "alpaca"),
        }
    except Exception as e:
        print(f"decrypt broker credentials error (user {user_id}): {type(e).__name__}: {e}")
        return None


def has_broker_credentials(user_id: str) -> bool:
    res = (
        supabase.table(TABLE)
        .select("user_id")
        .eq("user_id", str(user_id))
        .maybe_single()
        .execute()
    )
    return bool(res.data)


def delete_broker_credentials(user_id: str) -> None:
    supabase.table(TABLE).delete().eq("user_id", str(user_id)).execute()
