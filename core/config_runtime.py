from __future__ import annotations

import base64
import json
import os
import re
import threading
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.secrets_audit import (
    DEPLOYED_REPORT_KIND,
    DEPLOYED_REPORT_VERSION,
    FINGERPRINT_SALT,
    KeyRingInfo,
    fingerprints_for_values,
    key_ring_info,
)

try:
    from pydantic import BaseModel, Field, ValidationError, model_validator
    _HAS_PYDANTIC = True
except ImportError:
    # Fallback SANS pydantic, mais FONCTIONNEL : il lit réellement les
    # variables d'environnement (alors que l'ancien fallback ignorait
    # silencieusement l'environnement et renvoyait des valeurs par défaut, ce
    # qui désactivait de fait la sécurité configurée par env).
    _HAS_PYDANTIC = False

    class ValidationError(Exception):
        pass

    class _Field:
        __slots__ = ("default", "alias")

        def __init__(self, default=..., alias=None):
            self.default = default
            self.alias = alias

    def Field(default=..., alias=None, **kwargs):
        return _Field(default=default, alias=alias)

    def model_validator(**kwargs):
        def decorator(f):
            return f
        return decorator

    _BOOL_FIELDS = {"paper_trading", "headless", "adaptive_gd_enabled"}

    class BaseModel:
        def __init__(self, **data):
            for name, fld in self.__class__.__dict__.items():
                if not isinstance(fld, _Field):
                    continue
                alias = fld.alias or name
                value = data.get(name, data.get(alias))
                if value is None and alias in os.environ:
                    value = os.environ.get(alias)
                if value is None:
                    value = fld.default
                if name in _BOOL_FIELDS and not isinstance(value, bool):
                    value = parse_env_bool(value, default=bool(fld.default))
                setattr(self, name, value)

            normalize = getattr(self, "_normalize", None)
            if callable(normalize):
                normalize()


TRUTHY = {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# Balayage des canaux Telegram (auto-loop)
# --------------------------------------------------------------------------- #
# La liste des canaux et la période de balayage sont des **réglages**, pas des
# constantes de code : elles vivaient dans `workers/auto_loop.py`, donc les
# changer demandait de modifier le worker et de le redéployer.
#
# Elles sont validées **ici** parce que c'est ici que se lit la configuration,
# quelle qu'en soit la source : l'environnement (`TELEGRAM_CHANNELS`,
# `TELEGRAM_SCAN_MINUTES`) comme la surcharge en base (`database/settings.py`)
# passent par les mêmes fonctions, donc une valeur fautive se voit pareillement.

#: Canaux publics balayés quand rien n'est configuré (comportement historique :
#: ne rien lire dans l'environnement ne doit pas éteindre la collecte sans le dire).
DEFAULT_TELEGRAM_CHANNELS = ("thehalalwinningteam",)

#: Bornes de la période de balayage, en minutes. En dessous, on martèle l'aperçu
#: public `t.me/s/<canal>` (Telegram finit par limiter) ; au-dessus d'une journée,
#: un canal n'est plus vraiment suivi. Une valeur hors bornes y est ramenée, comme
#: les autres réglages numériques (`min_confidence`, `adaptive_gd_min_samples`).
TELEGRAM_SCAN_MINUTES_BOUNDS = (5, 1440)
DEFAULT_TELEGRAM_SCAN_MINUTES = 30

#: Plafond d'extractions **par canal et par balayage**. Un canal qui publie des
#: albums expose des dizaines de photos d'un coup : les extraire toutes dans le
#: même passage consommerait le quota de vision (Gemini) ou de transcription
#: (Groq) en une seule fois. Le surplus n'est pas perdu — il est **reporté au
#: cycle suivant**, l'aperçu redonnant les mêmes publications —, donc le
#: balayage est étalé au lieu d'être amputé. `0` veut dire **sans plafond** :
#: comme pour la veille média, borner en silence rendrait un cran d'arrêt
#: impossible à retirer depuis la base comme depuis l'environnement.
TELEGRAM_SCAN_MAX_EXTRACTIONS_BOUNDS = (0, 100)
DEFAULT_TELEGRAM_SCAN_MAX_EXTRACTIONS = 10

#: Période de la réconciliation média (orphelins et objets manquants), en minutes.
#: Le parcours coûte deux listings complets du bucket : en dessous d'une demi-
#: heure on interroge Storage pour rien, au-delà d'une journée une fuite de
#: stockage vit trop longtemps sans être signalée. `0` **éteint** la veille —
#: c'est la seule période qui ne soit pas bornée, parce qu'éteindre est une
#: décision, pas une valeur à corriger.
RECONCILE_MINUTES_BOUNDS = (30, 1440)
DEFAULT_RECONCILE_MINUTES = 360

#: Nombre d'objets orphelins à partir duquel on alerte (strictement au-dessus).
#: Un orphelin isolé est banal — un upload interrompu avant l'insertion —, donc
#: `0` veut dire « alerter dès le premier » et non « ne jamais alerter ».
DEFAULT_ORPHAN_ALERT_THRESHOLD = 25

#: Période de la **veille Supabase en lecture seule** (`workers/supabase_watch.py`),
#: en minutes. Le passage ne coûte qu'une lecture : la configuration, puis une
#: requête par table. Le plancher est donc bas — cinq minutes laissent voir une
#: panne avant que l'opérateur ne la découvre par un signal muet —, et `0`
#: **éteint** la veille, comme `MEDIA_RECONCILE_MINUTES`.
SUPABASE_WATCH_MINUTES_BOUNDS = (5, 1440)
DEFAULT_SUPABASE_WATCH_MINUTES = 30

# --------------------------------------------------------------------------- #
# Connexion Supabase
# --------------------------------------------------------------------------- #
# Deux erreurs de configuration coûtent des heures de recherche parce qu'elles ne
# se voient qu'indirectement : une URL qui n'est pas celle de l'API (on récupère
# celle de la base Postgres, ou on garde le `/rest/v1` collé du navigateur) et une
# **clé publique** mise à la place de la clé service_role.
#
# La seconde est la pire des deux : la RLS est en « deny by default » (migrations
# 005 à 011) et **aucune policy** n'est créée, donc la clé `anon` ne lit ni
# n'écrit rien. L'application démarre, interroge, et se comporte comme si la base
# était vide — sans la moindre exception.

#: Hôtes d'API Supabase : `https://<ref>.supabase.co`. On refuse une URL qui porte
#: un chemin (le client y ajoute `/rest/v1`), le host `db.<ref>` (c'est Postgres,
#: pas l'API) et `http://` (la clé service_role circulerait en clair).
_SUPABASE_PLACEHOLDERS = ("xxxxx", "your-project", "your-project-ref", "example")


def supabase_url_issue(url: str) -> str:
    """Ce qui cloche dans `SUPABASE_URL`, ou `""` si elle est utilisable.

    Volontairement permissif sur le domaine : un projet auto-hébergé n'a pas à
    finir par `.supabase.co`. On ne refuse que ce qui ne peut pas marcher.
    """
    text = str(url or "").strip()
    if not text:
        return "SUPABASE_URL manquante"
    if not text.startswith("https://"):
        return "SUPABASE_URL doit commencer par https:// (la clé service_role ne doit pas circuler en clair)"
    host = text[len("https://") :].split("/", 1)[0].split("?", 1)[0]
    if not host:
        return "SUPABASE_URL sans hôte"
    if text[len("https://") + len(host) :].strip("/").split("?", 1)[0]:
        return (
            "SUPABASE_URL ne doit pas porter de chemin : le client y ajoute "
            "`/rest/v1` lui-même (colle l'URL du projet, pas `/rest/v1` ni `/auth/v1`)"
        )
    if host.startswith("db."):
        return (
            "SUPABASE_URL pointe la base Postgres (`db.…`), pas l'API : prends "
            "`https://<projet>.supabase.co` dans Settings → API"
        )
    if any(marker in host for marker in _SUPABASE_PLACEHOLDERS):
        return "SUPABASE_URL contient encore un exemple de `.env.example`"
    return ""


def supabase_key_role(key: str) -> str:
    """Rôle porté par une clé Supabase : `service_role`, `anon`, sinon `""`.

    La signature n'est **pas** vérifiée : on lit ce que la clé prétend être, pour
    attraper la confusion anon/service_role. Un jeton forgé serait de toute façon
    refusé par le serveur, et un rôle vérifié ici ne remplace pas l'authentification.

    Les deux formats coexistent : les jetons JWT (l'ancien `service_role`) et les
    clés `sb_secret_…` / `sb_publishable_…` des projets récents. Ce qui ne se lit
    pas rend `""` — une clé d'un format inconnu n'est pas signalée comme fausse.
    """
    text = str(key or "").strip()
    if not text:
        return ""
    if text.startswith("sb_secret_"):
        return "service_role"
    if text.startswith("sb_publishable_"):
        return "anon"
    parts = text.split(".")
    if len(parts) != 3:
        return ""
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return ""
    role = str((claims or {}).get("role") or "")
    if role == "service_role":
        return "service_role"
    if role in ("anon", "authenticated"):
        return "anon"
    return ""


#: Un pseudo de canal : 5 à 32 caractères, minuscules/chiffres/`_`. C'est aussi la
#: seule forme que l'aperçu public `t.me/s/<canal>` sait lire.
#: Séparateurs d'une liste écrite à la main : virgules, points-virgules, retours
#: à la ligne. L'espace n'en est **pas** un : un pseudo Telegram ne peut pas en
#: contenir, donc `Crypto Signals` est un titre, pas deux canaux — le découper
#: donnerait deux pseudo fantômes balayés (et en échec) à chaque cycle.
_TELEGRAM_CHANNEL_RE = re.compile(r"[a-z0-9_]{5,32}")
_TELEGRAM_CHANNEL_SPLIT_RE = re.compile(r"[,;\t\r\n]+")


def _normalize_channel_token(raw: str) -> str:
    """Pseudo de canal depuis ce qui a été écrit, ou `""` si ce n'en est pas un."""
    token = raw.strip().lower()
    # Ce qu'on colle naturellement dans une variable d'environnement : une URL
    # complète, l'aperçu public, ou un lien vers un message précis.
    for prefix in ("https://", "http://", "www."):
        token = token.removeprefix(prefix)
    for host in ("t.me/", "telegram.me/"):
        token = token.removeprefix(host)
    token = token.removeprefix("s/")  # aperçu public : t.me/s/<canal>
    token = token.split("/", 1)[0]  # lien de message : t.me/<canal>/42
    token = token.split("?", 1)[0]  # paramètres d'un lien
    token = token.lstrip("@")
    return token if _TELEGRAM_CHANNEL_RE.fullmatch(token) else ""


def split_telegram_channels(value: Any) -> Tuple[List[str], List[str]]:
    """`(canaux exploitables, valeurs écartées)` d'une valeur d'env ou de base.

    Accepte ce qu'on écrit naturellement : plusieurs canaux séparés par des
    virgules, des points-virgules ou des retours à la ligne, un `@canal`, une URL
    `https://t.me/<canal>`, et même un lien de message `t.me/<canal>/42` (le canal
    y est explicite). La casse est repliée — Telegram l'ignore — et les doublons
    retirés : un canal écrit deux fois serait balayé deux fois par cycle.

    Ce qui ne peut **pas** être un canal public est **écarté**, pas deviné : un
    titre, un identifiant numérique de canal privé ou une phrase ne donneraient
    qu'un balayage en erreur à chaque cycle. Les valeurs écartées sont rendues à
    part, pour être signalées à l'opérateur au lieu d'être corrigées en silence.
    """
    if value is None:
        return [], []
    if isinstance(value, (list, tuple, set)):
        tokens: Sequence[Any] = list(value)
    else:
        tokens = _TELEGRAM_CHANNEL_SPLIT_RE.split(str(value))

    channels: List[str] = []
    rejected: List[str] = []
    for token in tokens:
        raw = str(token).strip()
        if not raw:
            continue
        name = _normalize_channel_token(raw)
        if not name:
            rejected.append(raw)
        elif name not in channels:
            channels.append(name)
    return channels, rejected


def clamp_scan_minutes(value: Any) -> Optional[int]:
    """Période de balayage **bornée**, ou `None` si la valeur n'est pas un nombre.

    Les bornes sont celles du balayage (`TELEGRAM_SCAN_MINUTES_BOUNDS`) : il n'y a
    pas deux tables à tenir à jour.

    Sert aux surfaces **interactives** — la commande Telegram `/channels` —, où
    une faute de frappe mérite une réponse parce que quelqu'un attend : « bientot »
    ne peut pas y passer pour « garde la valeur actuelle ». `parse_scan_minutes`
    reste la version « jamais en échec », celle d'une lecture de configuration.
    """
    low, high = TELEGRAM_SCAN_MINUTES_BOUNDS
    try:
        minutes = int(float(value))
    except (TypeError, ValueError):
        return None
    return min(max(minutes, low), high)


def parse_scan_minutes(value: Any, *, default: int = DEFAULT_TELEGRAM_SCAN_MINUTES) -> int:
    """Période de balayage des canaux, en minutes, bornée et jamais en échec.

    `default` est le repli d'une valeur absente ou illisible : c'est à l'appelant
    de dire de quoi il est le repli (la valeur d'environnement, pour une surcharge
    de base). Ne lève **jamais** : une faute de frappe dans un intervalle ne doit
    pas empêcher le reste de la configuration de se lire.
    """
    minutes = clamp_scan_minutes(value)
    return int(default) if minutes is None else minutes


def clamp_scan_max_extractions(value: Any) -> Optional[int]:
    """Plafond d'extractions par canal et par balayage, borné — `None` si illisible.

    Même contrat que `clamp_scan_minutes`, et les mêmes bornes des deux côtés de
    la configuration (environnement et `bot_settings`) : `0` est conservé tel quel
    — c'est « sans plafond » —, une valeur au-dessus du plafond technique est
    ramenée. Rendre `None` sur une valeur illisible laisse la commande Telegram
    répondre « je n'ai pas compris », au lieu de garder l'ancien réglage en silence.
    """
    low, high = TELEGRAM_SCAN_MAX_EXTRACTIONS_BOUNDS
    try:
        count = int(float(value))
    except (TypeError, ValueError):
        return None
    return min(max(count, low), high)


def parse_scan_max_extractions(
    value: Any, *, default: int = DEFAULT_TELEGRAM_SCAN_MAX_EXTRACTIONS
) -> int:
    """Plafond d'extractions effectif, jamais en échec (lecture de configuration).

    Le pendant « qui ne lève jamais » de `clamp_scan_max_extractions`, comme
    `parse_scan_minutes` l'est de `clamp_scan_minutes` : une faute de frappe dans
    un entier ne doit pas rendre le reste de la configuration illisible.
    """
    count = clamp_scan_max_extractions(value)
    return int(default) if count is None else count


def parse_reconcile_minutes(
    value: Any, *, default: int = DEFAULT_RECONCILE_MINUTES
) -> int:
    """Période de la réconciliation média, bornée, `0` signifiant **éteinte**.

    Ne lève **jamais** (même contrat que `parse_scan_minutes`) : une faute de
    frappe dans un intervalle ne doit pas rendre le reste de la configuration
    illisible. La différence avec le balayage des canaux, c'est que `0` est
    accepté **tel quel** au lieu d'être remonté au plancher : éteindre une veille
    coûteuse est une décision légitime, et la borner en silence la rendrait
    impossible à prendre depuis la base comme depuis l'environnement.
    """
    low, high = RECONCILE_MINUTES_BOUNDS
    try:
        minutes = int(float(value))
    except (TypeError, ValueError):
        return int(default)
    if minutes <= 0:
        return 0
    return min(max(minutes, low), high)


def parse_supabase_watch_minutes(
    value: Any, *, default: int = DEFAULT_SUPABASE_WATCH_MINUTES
) -> int:
    """Période de la veille Supabase, bornée, `0` signifiant **éteinte**.

    Même contrat que `parse_reconcile_minutes` : `0` est accepté tel quel (éteindre
    une veille est une décision, la borner en silence la rendrait impossible à
    prendre), une valeur illisible retombe sur `default` sans lever — une faute de
    frappe dans un intervalle ne doit pas rendre le reste de la configuration
    illisible.
    """
    low, high = SUPABASE_WATCH_MINUTES_BOUNDS
    try:
        minutes = int(float(value))
    except (TypeError, ValueError):
        return int(default)
    if minutes <= 0:
        return 0
    return min(max(minutes, low), high)


def parse_orphan_threshold(
    value: Any, *, default: int = DEFAULT_ORPHAN_ALERT_THRESHOLD
) -> int:
    """Seuil d'alerte (nombre d'orphelins **strictement au-dessus** duquel on parle).

    `0` est une valeur, pas une absence : elle veut dire « alerter dès le premier
    orphelin ». Une valeur illisible ou négative retombe sur `default` — un seuil
    négatif n'a pas de sens (aucun compte ne peut lui être inférieur) et le
    corriger en silence reviendrait à choisir à la place de l'opérateur.
    """
    try:
        threshold = int(float(value))
    except (TypeError, ValueError):
        return int(default)
    return int(default) if threshold < 0 else threshold


def choose_telegram_channels(
    *, override: Any = None, fallback: Sequence[str] = ()
) -> Tuple[List[str], List[str]]:
    """Liste retenue : la **surcharge** si elle donne un canal, sinon le repli.

    `None` veut dire « aucune surcharge » : le repli s'applique tel quel. Une
    valeur **vide** veut dire « n'en balayer aucun » — c'est la seule façon
    d'éteindre la collecte sans toucher au code (`TELEGRAM_CHANNELS=""`), donc
    elle est respectée.

    Une valeur **illisible**, en revanche, ne coupe pas la collecte : c'est une
    faute de frappe, pas une décision. Le repli reprend et la valeur fautive est
    rendue à part pour être signalée — arrêter de suivre les canaux sans que rien
    ne le dise serait le pire des deux mondes.
    """
    if override is None:
        return list(fallback), []
    channels, rejected = split_telegram_channels(override)
    if channels or not rejected:
        return channels, rejected
    return list(fallback), rejected


def parse_env_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in TRUTHY


def local_transcription_ready() -> bool:
    """Le repli de transcription **hors ligne** est-il installé (`faster-whisper`) ?

    Sans `GROQ_API_KEY`, c'est lui — et lui seul — qui transcrit encore les
    vidéos et les notes vocales : cette réponse dit donc si la transcription
    reste possible. Elle ne dit rien des **poids** du modèle, qui peuvent manquer
    même paquet installé (premier téléchargement hors ligne impossible).

    Import **local** : `ai.media_extractor` importe `config`, qui importe ce
    module — le lire au niveau du module fermerait un cycle. Le nom du paquet
    n'est pas recopié ici : c'est l'extracteur qui sait ce dont il a besoin.
    """
    from ai.media_extractor import local_transcription_available

    return local_transcription_available()


class EnvConfig(BaseModel):
    supabase_url: str = Field(default="", alias="SUPABASE_URL")
    supabase_service_key: str = Field(default="", alias="SUPABASE_SERVICE_KEY")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")

    # --- Comptes-rendus d'ingestion venus d'un CANAL (optionnel) ---
    # Un `channel_post` ne se répond pas dans le canal (la réponse s'y publierait,
    # devant tous les abonnés) : le compte-rendu et la revue partent en chat privé.
    # Le premier candidat est l'admin qui a publié le message ; ce chat-ci est le
    # second, et le seul possible quand la publication est anonyme ("en tant que
    # canal", le cas par défaut). Absent : l'ingestion a lieu, la revue est
    # journalisée comme non envoyée.
    telegram_admin_chat_id: str = Field(default="", alias="TELEGRAM_ADMIN_CHAT_ID")
    encryption_key: str = Field(default="", alias="ENCRYPTION_KEY")
    # --- Anneau de clés (rotation d'`ENCRYPTION_KEY`) ---
    # Les clés **retirées**, gardées uniquement pour rouvrir le chiffré écrit avec
    # elles : `v1:<clé>,v2:<clé>`, la plus récente d'abord (une clé nue vaut `v1:`,
    # la forme qu'avaient tous les `.env` d'avant). La clé active est celle du
    # dessus ; c'est `utils/encryption.py` qui construit l'anneau, et
    # `core/secrets_audit.py` qui vérifie **chaque** entrée — une clé retirée mal
    # recopiée ne se plaint qu'au premier ordre passé, donc le plus tard possible.
    encryption_keys_previous: str = Field(default="", alias="ENCRYPTION_KEYS_PREVIOUS")

    paper_trading: bool = Field(default=True, alias="PAPER_TRADING")
    min_confidence: float = Field(default=0.55, alias="MIN_CONFIDENCE")
    headless: bool = Field(default=True, alias="HEADLESS")

    alpaca_api_key: str = Field(default="", alias="ALPACA_API_KEY")
    alpaca_secret_key: str = Field(default="", alias="ALPACA_SECRET_KEY")
    alpaca_base_url: str = Field(default="https://paper-api.alpaca.markets", alias="ALPACA_BASE_URL")

    webhook_secret: str = Field(default="change-me-super-secret", alias="WEBHOOK_SECRET")
    internal_api_key: str = Field(default="", alias="INTERNAL_API_KEY")
    default_risk_pct: float = Field(default=1.0, alias="DEFAULT_RISK_PCT")
    default_paper_equity: float = Field(default=100000.0, alias="DEFAULT_PAPER_EQUITY")
    min_risk_reward_ratio: float = Field(default=1.2, alias="MIN_RISK_REWARD_RATIO")
    max_position_qty: float = Field(default=1000.0, alias="MAX_POSITION_QTY")

    finnhub_api_key: str = Field(default="", alias="FINNHUB_API_KEY")
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")

    # --- Embeddings Gemini (recherche vectorielle de la base de connaissances) ---
    # C'est la clé GEMINI_API_KEY ci-dessus qui alimente `:embedContent` : aucun
    # secret supplémentaire. Sans elle, la recherche vectorielle est désactivée
    # et l'extrait fixe de `knowledge_base` reprend.
    embedding_model: str = Field(default="gemini-embedding-001", alias="EMBEDDING_MODEL")

    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_repo: str = Field(default="", alias="GITHUB_REPO")
    github_branch: str = Field(default="main", alias="GITHUB_BRANCH")

    # --- Balayage des canaux Telegram (`workers/auto_loop.py`) ---
    # Annotés `Any` à dessein : l'environnement ne sait fournir qu'un texte, et
    # c'est `split_telegram_channels` (ci-dessus) qui le découpe, l'écarte ou le
    # replie, dans `_normalize`. Annoncer `List[str]` ferait refuser par pydantic
    # la forme brute que l'environnement fournit toujours, et une faute de frappe
    # dans l'intervalle ne doit pas rendre toute la configuration illisible : les
    # deux champs arrivent bruts et sont validés par les fonctions dédiées.
    telegram_channels: Any = Field(default=None, alias="TELEGRAM_CHANNELS")
    telegram_scan_minutes: Any = Field(
        default=DEFAULT_TELEGRAM_SCAN_MINUTES, alias="TELEGRAM_SCAN_MINUTES"
    )
    #: Plafond d'extractions par canal et par balayage (`0` = sans plafond) :
    #: c'est ce qui empêche un canal chargé en photos de consommer d'un coup le
    #: quota de vision, le surplus étant repris au cycle suivant.
    telegram_scan_max_extractions: Any = Field(
        default=DEFAULT_TELEGRAM_SCAN_MAX_EXTRACTIONS,
        alias="TELEGRAM_SCAN_MAX_EXTRACTIONS",
    )
    #: Canaux écartés à la lecture (voir `split_telegram_channels`) : rempli par
    #: `_normalize`, jamais lu dans l'environnement. Il n'existe que pour être
    #: signalé (`preflight()`), pas pour être utilisé.
    telegram_channels_invalid: Any = Field(default=None)

    # --- Réconciliation média (`workers/media_reconcile.py`) ---
    # Annotés `Any` pour la même raison : validés par les fonctions dédiées, qui
    # ne lèvent jamais — une faute de frappe dans la période ne doit pas rendre
    # toute la configuration illisible.
    media_reconcile_minutes: Any = Field(
        default=DEFAULT_RECONCILE_MINUTES, alias="MEDIA_RECONCILE_MINUTES"
    )
    media_orphan_alert_threshold: Any = Field(
        default=DEFAULT_ORPHAN_ALERT_THRESHOLD, alias="MEDIA_ORPHAN_ALERT_THRESHOLD"
    )

    # --- Veille Supabase en lecture seule (`workers/supabase_watch.py`) ---
    # Annoté `Any` pour la même raison que les précédents : validé par la fonction
    # dédiée, qui ne lève jamais.
    supabase_watch_minutes: Any = Field(
        default=DEFAULT_SUPABASE_WATCH_MINUTES, alias="SUPABASE_WATCH_MINUTES"
    )

    # --- Apprentissage adaptatif par descente de gradient ---
    # Désactivé par défaut : tant que le flag n'est pas activé explicitement,
    # la recalibration heuristique historique reste le comportement nominal.
    adaptive_gd_enabled: bool = Field(default=False, alias="ADAPTIVE_GD_ENABLED")
    adaptive_gd_min_samples: int = Field(default=20, alias="ADAPTIVE_GD_MIN_SAMPLES")

    # --- IDs de modèles LLM (surchargeables par env pour rester à jour) ---
    # Valeurs par défaut alignées avec l'app Android (GeminiApi.kt). Les IDs
    # fournisseurs changent vite : on les centralise ici au lieu de les
    # disperser en dur dans le code (voir docs/LLM_MODELS.md).
    gemini_model: str = Field(default="gemini-3.8-flash", alias="GEMINI_MODEL")
    gemini_lite_model: str = Field(default="gemini-3.1-flash-lite", alias="GEMINI_LITE_MODEL")
    gemini_pro_model: str = Field(default="gemini-3.1-pro", alias="GEMINI_PRO_MODEL")
    groq_primary_model: str = Field(default="openai/gpt-oss-120b", alias="GROQ_PRIMARY_MODEL")
    groq_secondary_model: str = Field(default="qwen/qwen3.6-27b", alias="GROQ_SECONDARY_MODEL")

    @model_validator(mode="before")
    @classmethod
    def _load_from_env(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            data = {}
        result = dict(data)
        for field_name, field in cls.model_fields.items():
            alias = field.alias or field_name
            if field_name in result or alias in result:
                continue
            if alias in os.environ:
                result[alias] = os.environ.get(alias)
        bool_fields = {
            "PAPER_TRADING": True,
            "HEADLESS": True,
            "ADAPTIVE_GD_ENABLED": False,
        }
        for key, default in bool_fields.items():
            if key in result:
                result[key] = parse_env_bool(result[key], default=default)
        return result

    @model_validator(mode="after")
    def _normalize(self) -> "EnvConfig":
        # Un canal jamais configuré garde le balayage historique ; un canal
        # configuré **vide** l'éteint (voir `choose_telegram_channels`).
        if self.telegram_channels is None:
            self.telegram_channels = list(DEFAULT_TELEGRAM_CHANNELS)
            self.telegram_channels_invalid = []
        else:
            self.telegram_channels, self.telegram_channels_invalid = split_telegram_channels(
                self.telegram_channels
            )
        self.telegram_scan_minutes = parse_scan_minutes(self.telegram_scan_minutes)
        self.telegram_scan_max_extractions = parse_scan_max_extractions(
            self.telegram_scan_max_extractions
        )
        self.media_reconcile_minutes = parse_reconcile_minutes(self.media_reconcile_minutes)
        self.media_orphan_alert_threshold = parse_orphan_threshold(
            self.media_orphan_alert_threshold
        )
        self.supabase_watch_minutes = parse_supabase_watch_minutes(
            self.supabase_watch_minutes
        )
        self.min_confidence = min(max(float(self.min_confidence), 0.5), 0.95)
        self.default_risk_pct = min(max(float(self.default_risk_pct), 0.1), 5.0)
        self.default_paper_equity = max(float(self.default_paper_equity), 1000.0)
        self.min_risk_reward_ratio = min(max(float(self.min_risk_reward_ratio), 0.5), 10.0)
        self.max_position_qty = max(float(self.max_position_qty), 1.0)
        # Au moins 2 exemples pour qu'une descente de gradient ait un sens.
        self.adaptive_gd_min_samples = max(int(float(self.adaptive_gd_min_samples)), 2)
        return self

    def supabase_issues(self) -> List[str]:
        """Ce qui empêche de **parler à la base**, quand une valeur est renseignée.

        Distinct de `security_issues()` : une clé publique ne met rien en danger
        (la RLS refuse tout), elle rend l'application muette — c'est un défaut de
        configuration, pas une faille, et il se répare en changeant la clé.
        """
        issues: List[str] = []
        url_problem = supabase_url_issue(self.supabase_url)
        if url_problem:
            issues.append(url_problem)
        if not self.supabase_service_key:
            issues.append("SUPABASE_SERVICE_KEY manquante")
        elif supabase_key_role(self.supabase_service_key) == "anon":
            issues.append(
                "SUPABASE_SERVICE_KEY porte une clé publique (anon / publishable) : "
                "la RLS est en deny-by-default, donc l'application ne lira et n'écrira "
                "rien. Prends la clé service_role dans Settings → API."
            )
        return issues

    def required_issues(self) -> List[str]:
        issues: List[str] = []
        issues.extend(self.supabase_issues())
        if not self.telegram_bot_token:
            issues.append("TELEGRAM_BOT_TOKEN manquant")
        if not self.encryption_key:
            issues.append("ENCRYPTION_KEY manquante")
        if self.webhook_secret == "change-me-super-secret":
            issues.append("WEBHOOK_SECRET utilise encore la valeur par défaut")
        if not self.internal_api_key:
            issues.append("INTERNAL_API_KEY manquante (protège les endpoints /consensus, /learning, /macro, /reports)")
        elif self.internal_api_key == self.webhook_secret:
            issues.append("INTERNAL_API_KEY identique à WEBHOOK_SECRET (utilise deux secrets distincts)")
        return issues

    def security_issues(self) -> List[str]:
        """Sous-ensemble des issues qui EMPÊCHENT un démarrage sûr en production.

        Distinct de `required_issues()` (qui peut inclure des manques
        fonctionnels tolérés en local) : ici, uniquement les défauts qui
        ouvrent une faille (secret par défaut, secret absent/partagé).
        """
        issues: List[str] = []
        if self.webhook_secret == "change-me-super-secret":
            issues.append("WEBHOOK_SECRET utilise encore la valeur par défaut")
        if not self.internal_api_key:
            issues.append("INTERNAL_API_KEY manquante")
        elif self.internal_api_key == self.webhook_secret:
            issues.append("INTERNAL_API_KEY identique à WEBHOOK_SECRET")
        return issues

    def component_health(self) -> Dict[str, Dict[str, Any]]:
        return {
            "telegram": {
                "ready": bool(self.telegram_bot_token),
                "paper_mode": self.paper_trading,
            },
            "supabase": {
                "ready": bool(self.supabase_url and self.supabase_service_key),
                # Le rôle, jamais la clé : c'est ce qui distingue « configuré » de
                # « configuré avec la clé qui ne peut rien faire ».
                "key_role": supabase_key_role(self.supabase_service_key),
                "url_issue": supabase_url_issue(self.supabase_url),
            },
            "alpaca_shared": {
                "ready": bool(self.alpaca_api_key and self.alpaca_secret_key),
                "paper_only": True,
            },
            "news_llm": {
                "groq": bool(self.groq_api_key),
                "gemini": bool(self.gemini_api_key),
            },
            "transcription": {
                # Groq est la voie nominale ; le local est le repli hors ligne.
                # Les deux faux, et plus aucun média audio n'est transcrit.
                "groq": bool(self.groq_api_key),
                "local_fallback": local_transcription_ready(),
            },
            "media_reconciliation": {
                # Une veille qui tourne sans chat admin ne peut rien signaler :
                # c'est exactement ce qu'on veut voir ici, plutôt que de le
                # découvrir en cherchant pourquoi aucune alerte n'arrive.
                "every_minutes": self.media_reconcile_minutes,
                "orphan_threshold": self.media_orphan_alert_threshold,
                "admin_chat": bool(self.telegram_admin_chat_id),
            },
            "supabase_watch": {
                # Même raison que ci-dessus, et une de plus : cette veille est en
                # lecture seule, donc la seule chose qui puisse la rendre muette
                # est un chat admin absent.
                "every_minutes": self.supabase_watch_minutes,
                "admin_chat": bool(self.telegram_admin_chat_id),
            },
            "market_data": {
                "finnhub": bool(self.finnhub_api_key),
                "fallbacks": ["yahoo", "binance", "coingecko", "stooq"],
            },
        }

    def preflight(self) -> Dict[str, Any]:
        issues = self.required_issues()
        return {
            "ok": len(issues) == 0,
            "issues": issues,
            "components": self.component_health(),
            # Le balayage des canaux n'est pas un secret : il est publié ici pour
            # qu'on puisse lire la configuration **effective** (et ce qui a été
            # écarté) sans fouiller les variables d'environnement.
            "telegram_channels": {
                "channels": list(self.telegram_channels or []),
                "scan_minutes": self.telegram_scan_minutes,
                "max_extractions": self.telegram_scan_max_extractions,
                "invalid": list(self.telegram_channels_invalid or []),
            },
            # La veille média n'est pas un secret non plus : période et seuil
            # effectifs, tels qu'ils seront appliqués au prochain cycle.
            "media_reconciliation": {
                "every_minutes": self.media_reconcile_minutes,
                "orphan_threshold": self.media_orphan_alert_threshold,
            },
            # La veille Supabase est publiée pour la même raison : `0` doit se
            # lire quelque part, sinon une veille éteinte se découvre en
            # cherchant pourquoi aucune alerte n'arrive.
            "supabase_watch": {"every_minutes": self.supabase_watch_minutes},
            "limits": {
                "min_confidence": self.min_confidence,
                "default_risk_pct": self.default_risk_pct,
                "default_paper_equity": self.default_paper_equity,
                "min_risk_reward_ratio": self.min_risk_reward_ratio,
                "max_position_qty": self.max_position_qty,
            },
        }


_instance: EnvConfig | None = None
_lock = threading.Lock()


def get_env_config() -> EnvConfig:
    global _instance
    if _instance is not None:
        return _instance
    with _lock:
        if _instance is not None:
            return _instance
        _instance = EnvConfig()
        return _instance


def reset_env_config() -> None:
    global _instance
    with _lock:
        _instance = None


def enforce_secure_config() -> None:
    """Bloque le démarrage si un secret de sécurité est absent ou par défaut.

    Fail-CLOSED volontaire : mieux vaut ne pas démarrer du tout qu'exposer
    des endpoints qui exécutent des ordres ou modifient l'IA.
    """
    issues = get_env_config().security_issues()
    if issues:
        raise RuntimeError(
            "Configuration de sécurité invalide, démarrage refusé :\n- "
            + "\n- ".join(issues)
            + "\n\nDéfinis WEBHOOK_SECRET et INTERNAL_API_KEY (deux valeurs distinctes) "
            "dans les variables d'environnement."
        )


def safe_preflight() -> Dict[str, Any]:
    try:
        return get_env_config().preflight()
    except ValidationError as exc:
        return {
            "ok": False,
            "issues": [f"Validation config: {exc.errors()}"],
            "components": {},
            "limits": {},
        }


# --------------------------------------------------------------------------- #
# Rapport d'empreintes du **processus** (vérification de la production)
# --------------------------------------------------------------------------- #

#: Pont entre l'audit des secrets (`core/secrets_audit.DEFAULT_SPECS`) et la
#: configuration que ce processus utilise réellement : `(nom du secret, attribut
#: d'EnvConfig)`. Écrit à la main **et vérifié par un test** qui exige la
#: couverture des specs : un secret ajouté à l'audit mais oublié ici disparaîtrait
#: en silence du rapport, et la barrière conclurait « absent de la production »
#: pour un secret qui y est — exactement le genre de faux verdict qu'on supprime.
SECRET_CONFIG_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("WEBHOOK_SECRET", "webhook_secret"),
    ("INTERNAL_API_KEY", "internal_api_key"),
    ("ENCRYPTION_KEY", "encryption_key"),
    ("ENCRYPTION_KEYS_PREVIOUS", "encryption_keys_previous"),
    ("SUPABASE_SERVICE_KEY", "supabase_service_key"),
    ("TELEGRAM_BOT_TOKEN", "telegram_bot_token"),
    ("GEMINI_API_KEY", "gemini_api_key"),
    ("GROQ_API_KEY", "groq_api_key"),
    ("FINNHUB_API_KEY", "finnhub_api_key"),
    ("ALPACA_API_KEY", "alpaca_api_key"),
    ("ALPACA_SECRET_KEY", "alpaca_secret_key"),
)


def configured_secret_values() -> Dict[str, str]:
    """Les valeurs de secrets que **ce processus** utilise, par nom d'audit.

    Lu depuis la configuration effective (`get_env_config()`), pas depuis
    `os.environ` : c'est elle que les appelants interrogent (`api.security`,
    `config.py`, donc `utils/encryption.py`). Une variable posée après le
    démarrage ne changerait pas le comportement du processus ; publier son
    empreinte ferait croire à une rotation déjà en vigueur, alors que le service
    tourne encore l'ancienne valeur.
    """
    cfg = get_env_config()
    return {name: str(getattr(cfg, field, "") or "") for name, field in SECRET_CONFIG_FIELDS}


def deployed_secret_report(now: Optional[datetime] = None) -> Dict[str, Any]:
    """Le rapport publié par un service déployé : des **empreintes**, jamais des valeurs.

    Il répond à une question que rien d'autre ne peut trancher de l'extérieur :
    « quels secrets ce processus a-t-il réellement chargés ? » Ni le tableau de
    bord de la plateforme (il montre la configuration *stockée*, pas ce que le
    processus tourne) ni le `.env` d'une machine de développement ne le disent.

    L'empreinte est salée (`FINGERPRINT_SALT`) et tronquée à 16 hexadécimaux :
    elle ne se renverse pas, et deux services qui la calculent pareil comparent
    leurs configurations sans jamais échanger une valeur. Le sel et la version du
    schéma sont publiés avec elle — un client d'une autre révision doit pouvoir
    **refuser** de comparer plutôt que de rendre un écart inexpliqué.
    """
    values = configured_secret_values()
    try:
        ring = key_ring_info(
            values.get("ENCRYPTION_KEY", ""), values.get("ENCRYPTION_KEYS_PREVIOUS", "")
        )
    except ValueError as exc:
        # Clé active illisible : le message de `parse_key_entry` nomme la faute
        # sans jamais recopier la valeur. On publie l'anneau comme illisible au
        # lieu de faire échouer tout le rapport — les autres empreintes restent
        # comparables, et c'est bien le diagnostic qu'on veut lire.
        ring = KeyRingInfo(None, (), str(exc))
    return {
        "kind": DEPLOYED_REPORT_KIND,
        "version": DEPLOYED_REPORT_VERSION,
        "fingerprint_salt": FINGERPRINT_SALT,
        "checked_at": (now or datetime.now(timezone.utc)).isoformat(),
        "source": "configuration effective du processus (core.config_runtime.EnvConfig)",
        "fingerprints": fingerprints_for_values(values),
        "key_ring": ring.as_dict(),
    }
