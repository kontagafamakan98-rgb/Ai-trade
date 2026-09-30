"""Réglages d'exécution (table `bot_settings`) — **surcharge** de l'environnement.

L'environnement reste la source par défaut ; cette table existe pour changer un
réglage sans redéployer un worker qui tourne en continu. C'est ce qui a fait
sortir la liste des canaux balayés et la période de balayage de
`workers/auto_loop.py`, où elles étaient des constantes de code.

Elle est traitée comme un **confort**, jamais comme un point de rupture : table
absente (migration 010 non appliquée), Supabase non configuré ou lecture
impossible rendent `None`, et l'appelant garde alors sa valeur d'environnement.
Un réglage absent ne veut donc pas dire « vide » : c'est `None` qui les
distingue, sinon on ne pourrait plus éteindre un balayage.

La **validation** n'est pas ici : elle est dans `core.config_runtime`, avec les
mêmes fonctions pour les deux sources. Une valeur de base acceptée et la même
valeur refusée dans l'environnement (ou l'inverse) seraient la pire des
configurations — deux vérités pour un seul réglage.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from core import config_runtime

try:
    from database.supabase_client import supabase
except Exception:  # pragma: no cover - `supabase_client` est déjà tolérant
    # sans signal : client optionnel, `supabase_client` a déjà nommé la cause
    supabase = None

TABLE = "bot_settings"

#: Canaux publics balayés par l'auto-loop (`["canal", …]`).
SETTING_TELEGRAM_CHANNELS = "telegram_channels"

#: Période de balayage de ces canaux (entier, en minutes).
SETTING_TELEGRAM_SCAN_MINUTES = "telegram_scan_minutes"

#: Plafond d'extractions par canal et par balayage (entier ; `0` = sans plafond).
#: Le surplus d'un canal chargé est repris au cycle suivant.
SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS = "telegram_scan_max_extractions"

#: Période de la réconciliation média (entier, en minutes ; `0` = éteinte).
SETTING_MEDIA_RECONCILE_MINUTES = "media_reconcile_minutes"

#: Seuil d'alerte des orphelins (entier ; `0` = dès le premier).
SETTING_MEDIA_ORPHAN_ALERT_THRESHOLD = "media_orphan_alert_threshold"

#: Période de la veille Supabase en lecture seule (entier, en minutes ; `0` = éteinte).
#: Le passage ne fait que lire : ce réglage ne peut pas dégrader la base, seulement
#: la fréquence à laquelle on apprend qu'elle va mal.
SETTING_SUPABASE_WATCH_MINUTES = "supabase_watch_minutes"


def _rows(response: Any) -> List[Dict[str, Any]]:
    """Lignes d'une réponse PostgREST, qu'elle ait échoué ou non.

    `supabase-py` rend selon la version une exception **ou** un objet portant son
    erreur : les deux mènent au même `None` chez l'appelant (retour à
    l'environnement), donc on ne laisse pas une forme passer pour un succès vide.
    """
    error = getattr(response, "error", None)
    if error:
        raise RuntimeError(str(error))
    data = getattr(response, "data", None) or []
    return data if isinstance(data, list) else []


def get_setting(key: str, *, client: Any = None) -> Optional[Any]:
    """Valeur brute d'un réglage, ou `None` s'il est absent ou illisible.

    `None` couvre trois cas qu'on ne distingue **pas** volontairement — table
    absente, clé absente, lecture en échec — parce que la conduite à tenir est la
    même dans les trois : reprendre la valeur d'environnement. Le journal, lui,
    dit lequel des trois a eu lieu.
    """
    client = supabase if client is None else client
    if client is None or not key:
        return None
    try:
        rows = _rows(
            client.table(TABLE).select("value").eq("key", key).limit(1).execute()
        )
    except Exception as exc:
        print(f"   [reglages] lecture de {key!r} impossible : {type(exc).__name__}: {exc}")
        return None
    if not rows:
        return None
    return rows[0].get("value")


def set_setting(key: str, value: Any, *, client: Any = None) -> Dict[str, Any]:
    """Écrit un réglage ; `value=None` le **supprime** (retour à l'environnement).

    Supprimer plutôt qu'écrire `null` : deux façons de dire « pas de surcharge »
    finiraient par diverger, alors que c'est la ligne absente que `get_setting()`
    sait relire. Ne lève jamais (même contrat que `media_store`).
    """
    client = supabase if client is None else client
    if client is None or not key:
        return {"ok": False, "reason": "no_client", "key": key}
    try:
        if value is None:
            client.table(TABLE).delete().eq("key", key).execute()
        else:
            # La cible du conflit est **nommée** : sans elle, PostgREST devine la
            # clé primaire de la table. Le résultat est le même ici (`key text
            # primary key`, migration 010), mais une cible écrite en toutes
            # lettres ne dépend pas de ce que la base devine — et c'est la seule
            # forme dont on puisse dire, depuis un test, sur quoi la ligne a été
            # remplacée.
            client.table(TABLE).upsert(
                {"key": key, "value": value}, on_conflict="key"
            ).execute()
    except Exception as exc:
        return {"ok": False, "reason": "write_failed", "key": key, "error": str(exc)}
    return {"ok": True, "key": key, "value": value}


def telegram_scan_settings(*, client: Any = None) -> Dict[str, Any]:
    """Réglages **effectifs** du balayage des canaux Telegram.

    Une seule fonction pour les trois réglages : le worker ne doit pas payer trois
    `asyncio.to_thread` séparés pour configurer un cycle, et les valeurs doivent
    être lues au même instant — une liste lue juste avant un changement de période
    décrirait un état qui n'a jamais existé.

    Précédence : une valeur de base **utilisable** l'emporte, sinon
    l'environnement (`core.config_runtime`), sinon le défaut du projet. Une
    surcharge illisible retombe sur l'environnement **et** est signalée — elle ne
    peut pas arrêter la collecte en silence.

    Retourne `{"channels": [...], "rejected": [...], "scan_minutes": int,
    "max_extractions": int}`, où `rejected` porte les valeurs écartées pour que
    l'appelant les affiche, et `max_extractions` le plafond d'extractions par
    canal et par balayage (`0` = sans plafond).
    """
    env = config_runtime.get_env_config()
    channels, rejected = config_runtime.choose_telegram_channels(
        override=get_setting(SETTING_TELEGRAM_CHANNELS, client=client),
        fallback=env.telegram_channels,
    )
    minutes = config_runtime.parse_scan_minutes(
        get_setting(SETTING_TELEGRAM_SCAN_MINUTES, client=client),
        default=env.telegram_scan_minutes,
    )
    max_extractions = config_runtime.parse_scan_max_extractions(
        get_setting(SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS, client=client),
        default=env.telegram_scan_max_extractions,
    )
    return {
        "channels": channels,
        "rejected": rejected,
        "scan_minutes": minutes,
        "max_extractions": max_extractions,
    }


def media_reconcile_settings(*, client: Any = None) -> Dict[str, Any]:
    """Réglages **effectifs** de la veille média (période et seuil d'alerte).

    Même précédence que le balayage des canaux : une valeur de base utilisable
    l'emporte, sinon l'environnement, sinon le défaut du projet. Une surcharge
    illisible retombe sur l'environnement — elle ne peut pas éteindre la veille
    par accident.

    Les deux valeurs sont lues en deux appels seulement quand la table répond :
    c'est le prix à payer pour pouvoir éteindre la veille (`0`) sans redéployer,
    ce qui est justement la raison d'être de `bot_settings`.

    Retourne `{"minutes": int, "orphan_threshold": int}`.
    """
    env = config_runtime.get_env_config()
    return {
        "minutes": config_runtime.parse_reconcile_minutes(
            get_setting(SETTING_MEDIA_RECONCILE_MINUTES, client=client),
            default=env.media_reconcile_minutes,
        ),
        "orphan_threshold": config_runtime.parse_orphan_threshold(
            get_setting(SETTING_MEDIA_ORPHAN_ALERT_THRESHOLD, client=client),
            default=env.media_orphan_alert_threshold,
        ),
    }


def supabase_watch_settings(*, client: Any = None) -> Dict[str, Any]:
    """Réglage **effectif** de la veille Supabase : sa période, en minutes.

    Même précédence que les autres veilles : une valeur de base utilisable
    l'emporte, sinon l'environnement, sinon le défaut du projet. Une surcharge
    illisible retombe sur l'environnement — elle ne peut pas éteindre la veille
    par accident, ce qui serait la façon la plus discrète de ne plus être prévenu
    de rien.

    Retourne `{"minutes": int}`, `0` signifiant **éteinte**.
    """
    env = config_runtime.get_env_config()
    return {
        "minutes": config_runtime.parse_supabase_watch_minutes(
            get_setting(SETTING_SUPABASE_WATCH_MINUTES, client=client),
            default=env.supabase_watch_minutes,
        )
    }


__all__ = [
    "SETTING_MEDIA_ORPHAN_ALERT_THRESHOLD",
    "SETTING_MEDIA_RECONCILE_MINUTES",
    "SETTING_SUPABASE_WATCH_MINUTES",
    "SETTING_TELEGRAM_CHANNELS",
    "SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS",
    "SETTING_TELEGRAM_SCAN_MINUTES",
    "TABLE",
    "get_setting",
    "media_reconcile_settings",
    "set_setting",
    "supabase_watch_settings",
    "telegram_scan_settings",
]
