"""Chiffrement des identifiants broker — anneau de clés **versionnées**.

Le chiffré des identifiants courtier n'était lisible que par **une** clé : une
seule `ENCRYPTION_KEY`, un seul `Fernet`, aucune colonne de version. La conséquence
n'était pas « on ne peut pas tourner la clé » mais pire : *on ne peut pas la
tourner sans perdre le chiffré existant*, et la perte ne se voyait qu'au moment de
passer un ordre — `get_broker_credentials` avalait l'`InvalidToken`, rendait `None`,
et `execution/order_executor.py` retombait alors sur le compte Alpaca **partagé**.
Un identifiant devenu illisible ressemblait donc exactement à un identifiant qui
n'a jamais existé, et l'ordre du client partait sur le compte du propriétaire.

Trois décisions réparent cela, et aucune ne suffit seule :

1. **Un anneau**, pas une clé : `ENCRYPTION_KEY` chiffre, `ENCRYPTION_KEYS_PREVIOUS`
   (les clés retirées) rouvre. On peut donc poser une clé neuve sans rendre
   l'ancien chiffré illisible.
2. **Une version dans le jeton** : `encrypt()` écrit `v2:<jeton Fernet>`. Sans elle,
   « cette ligne est-elle tournée ? » se devine en essayant les clés ; avec elle,
   cela se lit — c'est ce qui rend une rotation **observable** (combien de lignes
   restent sur l'ancienne) et **terminable** (on retire l'ancienne clé quand il n'en
   reste aucune, ce que fait `scripts/rotate_encryption_key.py`).
3. **Jamais de repli silencieux** : un jeton que rien dans l'anneau ne rouvre lève
   `EncryptionError`, et `database/broker_credentials.py` la traduit en
   `BrokerCredentialsUnreadable` — l'appelant **refuse** l'ordre au lieu de le
   router ailleurs.

Le préfixe de version n'a pas demandé de migration : `api_key_enc` est un `text`,
et les jetons déjà en base, écrits sans préfixe, restent lisibles — ils sont
« sans version », donc essayés contre chaque clé de l'anneau (la plus récente
d'abord) et signalés comme *à tourner*.

La clé active s'écrit `ENCRYPTION_KEY="v2:<clé>"`, ou **nue** pour la version 1 —
c'est ce qu'ont tous les `.env` d'avant, donc rien n'est à réécrire. Une clé nue
reste une clé nue : ce module ne décide jamais d'une version à la place de
l'opérateur, il la lit.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from cryptography.fernet import Fernet, InvalidToken

from config import ENCRYPTION_KEY, ENCRYPTION_KEYS_PREVIOUS
from core.secrets_audit import (
    DEFAULT_KEY_VERSION,
    is_valid_fernet_key,
    parse_key_entry,
    ring_entries,
)

#: Le séparateur du jeton : `vN:<jeton Fernet>`. Un jeton Fernet commence par
#: `gAAAAA…` (base64 de l'octet de version 0x80), donc un `vN:` en tête ne peut
#: jamais être confondu avec un jeton.
_TOKEN_VERSION_RE_LENGTH = 4  # au plus « v999 » avant les deux-points


class EncryptionError(RuntimeError):
    """Tout échec de chiffrement — la racine des deux cas qui suivent.

    Sous-classe de `RuntimeError` : les appelants qui attrapent déjà large pour
    un problème de chiffrement continuent de fonctionner, mais ceux qui veulent
    **distinguer** « ce chiffré ne se rouvre pas » de « aucun compte connecté »
    peuvent le faire — c'est ce que fait `database/broker_credentials.py`.
    """


class EncryptionKeyMissing(EncryptionError):
    """`ENCRYPTION_KEY` absente : rien ne peut être chiffré ni rouvert."""


class EncryptionKeyInvalid(EncryptionError):
    """Une clé de l'anneau est illisible (format, version, ou version répétée).

    La faute est dans la **configuration**, pas dans la donnée — mais du point de
    vue de qui veut lire un identifiant broker, c'est le même verdict : on ne peut
    pas le rouvrir, donc on refuse au lieu de se rabattre ailleurs.
    """


def token_version(payload: str) -> Optional[int]:
    """La version annoncée par un jeton, ou `None` s'il n'en porte pas (« legacy »).

    On lit le préfixe, on ne déchiffre pas : c'est une lecture d'en-tête, donc
    utilisable pour un rapport avant toute décision.
    """
    text = str(payload or "")
    head, sep, _ = text.partition(":")
    if not sep or not head.startswith("v") or len(head) > _TOKEN_VERSION_RE_LENGTH:
        return None
    digits = head[1:]
    return int(digits) if digits.isdigit() else None


@dataclass(frozen=True)
class RingKey:
    """Une clé de l'anneau et sa version. La clé ne se journalise jamais."""

    version: int
    key: str

    def __repr__(self) -> str:  # pragma: no cover - garde-fou, jamais un secret
        return f"RingKey(version={self.version}, key=<masquée>)"


class KeyRing:
    """Les clés utilisables, la plus récente d'abord, chacune avec sa version.

    `encrypt` n'utilise que la clé **active** (la première) ; `decrypt` accepte
    n'importe laquelle, en commençant par celle que le jeton annonce. Aucune
    méthode ne journalise une clé ni un texte clair.
    """

    def __init__(self, primary: RingKey, previous: Sequence[RingKey] = ()):
        self._keys = [primary, *previous]
        versions = [item.version for item in self._keys]
        if len(set(versions)) != len(versions):
            raise EncryptionKeyInvalid(
                "anneau de clés incohérent : deux clés portent la même version "
                f"({', '.join('v' + str(v) for v in sorted(versions))}) — une "
                "version désigne une seule clé"
            )
        for item in self._keys:
            if not is_valid_fernet_key(item.key):
                raise EncryptionKeyInvalid(
                    f"la clé v{item.version} de l'anneau n'est pas une clé Fernet valide "
                    "(attendu : base64 urlsafe de 32 octets)"
                )
        self._by_version: Dict[int, Fernet] = {
            item.version: Fernet(item.key.encode("ascii")) for item in self._keys
        }
        self._primary = primary

    @property
    def primary_version(self) -> int:
        """La version avec laquelle on **chiffre** aujourd'hui."""
        return self._primary.version

    @property
    def versions(self) -> List[int]:
        """Toutes les versions de l'anneau, la plus récente d'abord."""
        return [item.version for item in self._keys]

    @property
    def size(self) -> int:
        return len(self._keys)

    def encrypt(self, data: str) -> str:
        """Chiffre avec la clé active et **préfixe la version** dans le résultat."""
        token = self._by_version[self._primary.version].encrypt(str(data).encode()).decode()
        return f"v{self._primary.version}:{token}"

    def decrypt_with_version(self, payload: str) -> Tuple[int, str]:
        """`(version qui a ouvert, texte clair)` — ou `EncryptionError`.

        Le texte clair ne quitte jamais l'appelant : c'est lui qui sait s'il doit
        l'écrire quelque part (une colonne broker), jamais ce module.
        """
        text = str(payload or "")
        announced = token_version(text)
        if announced is not None:
            fernet = self._by_version.get(announced)
            if fernet is None:
                raise EncryptionError(
                    f"ce chiffré annonce la version v{announced}, absente de l'anneau : "
                    "remets la clé retirée dans ENCRYPTION_KEYS_PREVIOUS "
                    "(docs/SECRETS.md, « ENCRYPTION_KEY »)"
                )
            token = text.split(":", 1)[1]
            try:
                return announced, fernet.decrypt(token.encode()).decode()
            except InvalidToken as exc:
                raise EncryptionError(
                    f"la clé v{announced} n'ouvre pas ce chiffré : la version annoncée "
                    "et la clé de l'anneau ne correspondent pas"
                ) from exc
        # Jeton écrit avant l'anneau : aucune version annoncée, donc on essaie
        # chaque clé — la plus récente d'abord, c'est le cas le plus probable.
        for item in self._keys:
            try:
                return item.version, self._by_version[item.version].decrypt(text.encode()).decode()
            except InvalidToken:
                continue
        raise EncryptionError(
            "aucune clé de l'anneau ne rouvre ce chiffré (jeton sans version "
            f"d'origine) ; l'anneau porte : {', '.join('v' + str(v) for v in self.versions)}"
        )

    def decrypt(self, payload: str) -> str:
        """Le texte clair d'un jeton, quelle que soit la version qui l'ouvre."""
        return self.decrypt_with_version(payload)[1]

    def needs_rotation(self, payload: str) -> bool:
        """Ce chiffré doit-il être réécrit avec la clé active ?

        Vrai pour tout ce qui n'est pas déjà à la version active — y compris les
        jetons sans version, trop anciens pour l'annoncer.
        """
        announced = token_version(payload)
        return announced != self._primary.version


def build_ring(primary_value: str, previous_value: str = "") -> KeyRing:
    """L'anneau lu dans la configuration : la clé active, puis les clés retirées.

    `primary_value` accepte `vN:<clé>` ou une clé **nue** (version 1, la forme
    historique). `previous_value` est une liste de ces mêmes entrées, séparées par
    des virgules, des points-virgules ou des espaces.

    Lève `EncryptionKeyMissing` si la clé active manque, `EncryptionKeyInvalid` si
    une entrée est illisible ou si deux entrées revendiquent la même version. Les
    messages nomment une **position** et une **version**, jamais une clé.
    """
    if not str(primary_value or "").strip():
        raise EncryptionKeyMissing(
            "ENCRYPTION_KEY manquante : les identifiants broker ne peuvent être ni "
            "chiffrés ni rouverts. Génère-la avec `python scripts/generate_secrets.py` "
            "(docs/SECRETS.md)."
        )
    try:
        primary_version, primary_key = parse_key_entry(str(primary_value))
    except ValueError as exc:
        raise EncryptionKeyInvalid(f"ENCRYPTION_KEY : {exc}") from exc

    previous: List[RingKey] = []
    for index, entry in enumerate(ring_entries(previous_value), start=1):
        try:
            version, key = parse_key_entry(entry, default_version=DEFAULT_KEY_VERSION)
        except ValueError as exc:
            raise EncryptionKeyInvalid(
                f"ENCRYPTION_KEYS_PREVIOUS, entrée {index} : {exc}"
            ) from exc
        previous.append(RingKey(version=version, key=key))

    return KeyRing(RingKey(version=primary_version, key=primary_key), previous)


_ring: Optional[KeyRing] = None


def get_ring() -> KeyRing:
    """L'anneau de la configuration, construit une fois (il ne change qu'au
    redémarrage : une variable d'environnement ne se modifie pas à chaud)."""
    global _ring
    if _ring is None:
        _ring = build_ring(ENCRYPTION_KEY, ENCRYPTION_KEYS_PREVIOUS)
    return _ring


def reset_ring() -> None:
    """Oublie l'anneau mis en cache — les tests rejouent une configuration de clés."""
    global _ring
    _ring = None


def encrypt(data: str) -> str:
    """Chiffre une chaîne, retourne une chaîne stockable telle quelle en base."""
    return get_ring().encrypt(data)


def decrypt(token: str) -> str:
    """Déchiffre une chaîne préalablement chiffrée avec `encrypt()`."""
    return get_ring().decrypt(token)


def decrypt_with_version(token: str) -> Tuple[int, str]:
    """`(version, texte clair)` d'un chiffré — voir `KeyRing.decrypt_with_version`."""
    return get_ring().decrypt_with_version(token)


def needs_reencryption(token: str) -> bool:
    """Ce chiffré est-il encore à une version ancienne ? (voir `needs_rotation`)"""
    return get_ring().needs_rotation(token)


__all__ = [
    "EncryptionError",
    "EncryptionKeyInvalid",
    "EncryptionKeyMissing",
    "KeyRing",
    "RingKey",
    "build_ring",
    "decrypt",
    "decrypt_with_version",
    "encrypt",
    "get_ring",
    "needs_reencryption",
    "reset_ring",
    "token_version",
]
