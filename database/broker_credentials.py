"""
Stockage chiffré des identifiants de courtier (API key/secret) par
utilisateur, pour que chaque client exécute ses ordres sur SON PROPRE
compte Alpaca plutôt que sur une clé partagée.

Les clés ne sont JAMAIS stockées ni loguées en clair — chiffrées avec
Fernet (utils/encryption.py) avant tout passage en base, et **préfixées de la
version** de la clé qui les a chiffrées (`v2:<jeton>`).

Ce que la version change, et pourquoi elle est là : une rotation d'`ENCRYPTION_KEY`
n'est terminée que quand plus aucune ligne n'est chiffrée par une clé retirée.
`reencrypt_all()` le dit **sans rien écrire** (`apply=False`), puis le fait
(`apply=True`) — c'est `scripts/rotate_encryption_key.py` qui l'expose.

Un chiffré que l'anneau ne rouvre pas lève `BrokerCredentialsUnreadable`, jamais
`None` : c'est la distinction qui empêchait le repli silencieux vers le compte
partagé (voir `execution/order_executor.py`).
"""
from typing import Any, Dict, List, Tuple

from database.supabase_client import supabase
from utils.encryption import (
    EncryptionError,
    decrypt_with_version,
    encrypt,
    get_ring,
)

TABLE = "user_broker_credentials"


class BrokerCredentialsUnreadable(RuntimeError):
    """La ligne existe, mais l'anneau de clés ne la rouvre pas.

    À ne **jamais** confondre avec « aucun compte connecté ». Rendre `None` dans
    ce cas faisait retomber `execution/order_executor.py` sur le compte Alpaca
    **partagé** : l'ordre d'un client partait sur le compte du propriétaire, en
    silence, et rien dans le résultat ne le disait.

    Le message nomme l'utilisateur et la version attendue, jamais une clé ni un
    identifiant.
    """

    def __init__(self, user_id: str, reason: str):
        super().__init__(
            f"identifiants broker de l'utilisateur {user_id} illisibles : {reason}. "
            "Le compte personnel n'est PAS remplacé par le compte partagé "
            "(docs/SECRETS.md, « ENCRYPTION_KEY »)."
        )
        self.user_id = str(user_id)
        self.reason = reason


def set_broker_credentials(
    user_id: str,
    api_key: str,
    api_secret: str,
    paper: bool = True,
    broker: str = "alpaca",
    account_number: str = "",
    custom_endpoint: str = ""
) -> None:
    data = {
        "user_id": str(user_id),
        "broker": broker,
        "api_key_enc": encrypt(api_key),
        "api_secret_enc": encrypt(api_secret),
        "paper": paper,
        "account_number": account_number,
        "custom_endpoint": custom_endpoint,
    }
    supabase.table(TABLE).upsert(data).execute()


def get_broker_credentials(user_id: str) -> Dict[str, Any] | None:
    """Retourne les identifiants déchiffrés.

    * `None` — cet utilisateur n'a jamais connecté de compte : l'appelant peut
      légitimement se rabattre sur ce qu'il veut (clé partagée comprise) ;
    * `BrokerCredentialsUnreadable` — la ligne est là, le chiffré ne se rouvre
      pas. L'appelant doit **refuser**, pas se rabattre.

    Le dictionnaire rendu porte `key_version` : la version de clé qui a rouvert
    cette ligne, pour qu'un appelant puisse dire « ce compte est encore sur v1 »
    sans avoir à déchiffrer quoi que ce soit lui-même.
    """
    res = (
        supabase.table(TABLE)
        .select("*")
        .eq("user_id", str(user_id))
        .limit(1)
        .execute()
    )
    if not res or not res.data:
        return None
    row = res.data[0]
    try:
        key_version, api_key = decrypt_with_version(row["api_key_enc"])
        _secret_version, api_secret = decrypt_with_version(row["api_secret_enc"])
    except KeyError as exc:
        raise BrokerCredentialsUnreadable(
            str(user_id), f"colonne {exc} absente de la table {TABLE}"
        ) from exc
    except EncryptionError as exc:
        raise BrokerCredentialsUnreadable(str(user_id), str(exc)) from exc
    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "paper": row.get("paper", True),
        "broker": row.get("broker", "alpaca"),
        "key_version": key_version,
    }


def delete_broker_credentials(user_id: str) -> None:
    supabase.table(TABLE).delete().eq("user_id", str(user_id)).execute()


def reencrypt_all(*, apply: bool = False) -> Dict[str, Any]:
    """Relit chaque ligne et réécrit celles qui ne sont plus sur la clé active.

    Sans `apply` (le défaut), rien n'est écrit : le rapport **compte** — combien de
    lignes par version, combien de lignes encore à tourner, combien ne se rouvrent
    pas. C'est ce qui rend une rotation observable : on voit la clé retirée devenir
    inutile au fur et à mesure, jusqu'à pouvoir la retirer de l'anneau.

    Une ligne est réécrite **entièrement** ou pas du tout : si l'une des deux
    moitiés (clé, secret) est illisible, on ne touche à rien — remplacer une moitié
    rouvrable par une autre ferait perdre la première.

    Rien de ce qui est lu ne sort d'ici : ni clé, ni identifiant, ni texte clair.
    Les compteurs, les versions et les identifiants d'utilisateur suffisent.
    """
    if not supabase:
        raise RuntimeError(
            "Supabase n'est pas configuré : renseigne SUPABASE_URL et "
            "SUPABASE_SERVICE_KEY (voir .env.example)."
        )
    ring = get_ring()
    res = supabase.table(TABLE).select("*").execute()
    rows = (res.data if res else None) or []

    by_version: Dict[int, int] = {}
    unreadable: List[Tuple[str, str]] = []
    to_rotate = 0
    rewritten = 0

    for row in rows:
        user_id = str(row.get("user_id") or "")
        try:
            key_version, api_key = decrypt_with_version(row.get("api_key_enc"))
            secret_version, api_secret = decrypt_with_version(row.get("api_secret_enc"))
        except EncryptionError as exc:
            unreadable.append((user_id, str(exc)))
            continue
        by_version[key_version] = by_version.get(key_version, 0) + 1
        stale = key_version != ring.primary_version or secret_version != ring.primary_version
        if not stale:
            continue
        to_rotate += 1
        if not apply:
            continue
        supabase.table(TABLE).update(
            {"api_key_enc": encrypt(api_key), "api_secret_enc": encrypt(api_secret)}
        ).eq("user_id", user_id).execute()
        rewritten += 1

    return {
        "table": TABLE,
        "rows": len(rows),
        "primary_version": ring.primary_version,
        "ring_versions": ring.versions,
        "by_version": by_version,
        "to_rotate": to_rotate,
        "unreadable": unreadable,
        "rewritten": rewritten,
        "applied": bool(apply),
    }
