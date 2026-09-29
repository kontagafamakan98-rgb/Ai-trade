"""Remettre les octets d'un média dont l'objet a disparu du bucket.

Le sens inverse de la réconciliation (`list_orphan_objects`) : ici la ligne
`knowledge_media` existe, la description est complète — légende, canal, message,
`telegram_file_id`, actif, verdict de revue, texte indexé — mais **l'objet n'est
plus dans le bucket**. L'application affiche alors un fichier fantôme : les liens
signés répondent 404, le tableau de bord pointe dans le vide, et seule une
lecture du Storage peut s'en apercevoir (`media_store.list_missing_objects`).

Deux voies de retour, essayées dans cet ordre :

1. **`telegram_file_id`** — l'identifiant que le bot a reçu à l'ingestion. C'est
   la voie exacte : Telegram sert le fichier d'origine, tel qu'il était. Elle
   exige que le bot soit configuré (`TELEGRAM_BOT_TOKEN`), et l'appelant fournit
   le téléchargement (`download_file_id`) : ce module n'importe ni
   `python-telegram-bot` ni l'application Telegram — comme le reste de la surface
   web, il doit s'importer sans la pile du bot ;
2. **l'aperçu public du canal** (`scrapers.telegram_channel`) — la publication est
   retrouvée par son `message_id` (`?before=<id>`) et le fichier re-téléchargé.
   C'est la seule voie pour un média ingéré par le **scraper**, qui n'a jamais eu
   de `telegram_file_id` ; elle ne marche que pour un canal **public** et pour une
   publication que l'aperçu accepte encore de rendre.

Trois choix qui ne se voient pas dans un rapport heureux :

* **on n'écrase pas un objet présent.** La restauration est refusée si le fichier
  est là, parce que la voie du scraper rend le fichier de **l'aperçu** — une
  copie possiblement réduite. Restaurer par-dessus un original intact
  l'abaisserait de qualité, silencieusement, pour réparer une panne imaginaire ;
* **la ligne n'est jamais réécrite** (`media_store.restore_object` ne touche qu'à
  Storage). Rejouer l'ingestion écraserait `metadata` — verdict de revue, actif,
  note d'extraction — et repaierait une extraction (vision, Whisper) dont le
  texte est déjà indexé dans `knowledge_chunks`;
* **les octets qui reviennent sont comparés à ceux qui étaient partis.**
  `media_store` enregistre à l'ingestion l'empreinte SHA-256 du fichier
  (`FINGERPRINT_KEY`); ici elle est recalculée sur ce qui vient d'être
  re-téléchargé et comparée. C'est ce qui sépare « le fichier d'origine est
  revenu » de « un fichier a été retrouvé » : la taille, seule, ne le prouve pas
  — l'aperçu public sert volontiers une copie réduite qui peut tomber juste.
  Rapporté, jamais opposé : l'empreinte dit ce qui est revenu, elle ne refuse pas
  une restauration utile (voir `fingerprint_matches`).
* **un échec nomme les deux voies essayées.** « Le scraper n'a rien trouvé après
  un `file_id` refusé » et « aucune voie possible » ne disent pas la même chose :
  la première invite à réessayer plus tard, la seconde à re-télécharger le canal
  par le bot ou à admettre que l'objet est perdu.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, List, Optional

from database import media_store
from scrapers import telegram_channel

#: Voie « bot » : l'identifiant de fichier Telegram de la ligne.
ROUTE_FILE_ID = "telegram_file_id"
#: Voie « aperçu public » : le canal + l'id du message.
ROUTE_CHANNEL = "channel_scraper"

#: Télécharge des octets à partir d'un `file_id` Telegram (injecté).
FileDownloader = Callable[[str], Awaitable[bytes]]
#: Retrouve les octets d'un message de canal public (injecté).
MessageFetcher = Callable[..., Awaitable[Optional[Dict[str, Any]]]]
#: Dépose des octets dans le bucket (injecté) — Storage seul, jamais la ligne.
Restorer = Callable[..., int]
#: Dit si l'objet est déjà là (injecté).
Existence = Callable[[str], bool]

#: Raison rapportée quand aucune voie n'existe pour cette ligne.
NO_ROUTE = (
    "aucune voie de re-téléchargement : ni `telegram_file_id`, ni "
    "`chat_id` public avec `message_id`"
)

#: Issues possibles d'une ligne. Distinguées parce qu'elles n'appellent pas la
#: même suite : un échec se rejoue, un « ignoré » non.
OUTCOME_RESTORED = "restored"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"


def channel_of(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """Nom de canal **exploitable par l'aperçu public**, ou `None`.

    L'aperçu vit à `t.me/s/<nom>` : il lui faut un nom d'utilisateur public. Un
    `chat_id` numérique (canal privé, ou chat dont le bot ne connaît pas le
    `username`) ne donne aucune URL à interroger — le déclarer « réparable par le
    scraper » ferait échouer la réparation avec un motif trompeur.
    """
    raw = str((row or {}).get("chat_id") or "").strip().lstrip("@")
    if not raw or raw.lstrip("-").isdigit():
        return None
    return raw


def repair_route(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """Par où les octets peuvent revenir, ou `None` si aucune voie n'existe.

    Sert aussi à **annoncer** ce qui est réparable avant d'essayer : la liste des
    objets manquants peut ainsi dire, ligne par ligne, si un re-téléchargement a
    une chance — au lieu de laisser l'appelant tout tenter pour le découvrir.
    """
    if not row:
        return None
    if str(row.get("telegram_file_id") or "").strip():
        return ROUTE_FILE_ID
    if channel_of(row) and row.get("message_id") is not None:
        return ROUTE_CHANNEL
    return None


def _fingerprint_matches(row: Dict[str, Any], data: bytes) -> Optional[bool]:
    """Les octets re-téléchargés sont-ils **les mêmes** que ceux ingérés ?

    La taille ne le prouve pas ; l'empreinte si. Trois réponses, et le `None`
    n'est pas un succès : `True` prouvé, `False` un **autre** fichier, `None`
    aucune référence enregistrée (média antérieur à `FINGERPRINT_KEY`, ou ligne
    sans `metadata`).

    L'empreinte est calculée sur les octets **re-téléchargés**, ceux qu'on remet
    dans le bucket : c'est eux que la question juge. Ce que le Storage en fait
    ensuite n'est attesté que par la taille (`size_matches`) — vérifier l'objet
    déposé demanderait de le relire en entier, un second transfert pour un
    contrôle que `object_exists` fait déjà par listing.
    """
    expected = media_store.content_fingerprint(row)
    if not expected:
        return None
    return media_store.fingerprint(data) == expected


def _size_matches(row: Dict[str, Any], size: int) -> Optional[bool]:
    """La taille restaurée correspond-elle à celle enregistrée ?

    Rapporté, jamais opposé : l'aperçu public peut servir une copie réduite, et
    refuser pour autant une restauration utile serait pire que de la signaler.
    """
    expected = row.get("file_size")
    try:
        return int(expected) == int(size)
    except (TypeError, ValueError):
        return None


async def repair_media(
    row: Dict[str, Any],
    *,
    download_file_id: Optional[FileDownloader] = None,
    fetch_message: Optional[MessageFetcher] = None,
    restore: Optional[Restorer] = None,
    exists: Optional[Existence] = None,
    max_bytes: int = telegram_channel.MAX_MEDIA_BYTES,
) -> Dict[str, Any]:
    """Re-télécharge **un** média et le remet dans le bucket.

    Ne lève jamais : chaque issue (réparé, échec, non réparable, objet déjà là)
    est un résultat, parce qu'un lot de réparations doit continuer après une
    ligne perdue. Un objet **présent** n'est pas réécrit (voir le module).
    """
    fetch_message = fetch_message or telegram_channel.fetch_message_media
    restore = restore or media_store.restore_object
    exists = exists or media_store.object_exists

    media_id = row.get("id")
    path = row.get("storage_path")
    result: Dict[str, Any] = {
        "media_id": media_id,
        "storage_path": path,
        "ok": False,
        "outcome": OUTCOME_SKIPPED,
        "route": None,
        "bytes": 0,
        "size_matches": None,
        "fingerprint_matches": None,
        "reason": None,
        "notes": [],
    }

    if not path:
        result["reason"] = "ligne sans `storage_path` : rien à restaurer"
        return result
    try:
        present = await asyncio.to_thread(exists, path)
    except Exception as exc:
        # Sans réponse du Storage, on ne peut pas savoir si l'on écraserait un
        # original : on s'abstient. Redéposer « au cas où » dégraderait
        # silencieusement un fichier intact (la copie d'aperçu est plus petite).
        result["outcome"] = OUTCOME_FAILED
        result["reason"] = f"présence indéterminée : {type(exc).__name__}: {exc}"
        return result
    if present:
        result["reason"] = "objet déjà présent dans le bucket : non écrasé"
        return result

    route = repair_route(row)
    if route is None:
        result["reason"] = NO_ROUTE
        return result

    attempts: List[str] = []
    data: Optional[bytes] = None
    mime_type = row.get("mime_type")

    if route == ROUTE_FILE_ID:
        file_id = str(row.get("telegram_file_id") or "").strip()
        if download_file_id is None:
            attempts.append(f"{ROUTE_FILE_ID} : aucun bot configuré")
        else:
            try:
                data = await download_file_id(file_id)
            except Exception as exc:
                attempts.append(f"{ROUTE_FILE_ID} : {type(exc).__name__}: {exc}")

    if data is None and channel_of(row) and row.get("message_id") is not None:
        channel = channel_of(row)
        try:
            fetched = await fetch_message(channel, row.get("message_id"), max_bytes=max_bytes)
        except Exception as exc:
            attempts.append(f"{ROUTE_CHANNEL} : {type(exc).__name__}: {exc}")
        else:
            if fetched:
                data = fetched.get("data")
                mime_type = fetched.get("mime_type") or mime_type
                route = ROUTE_CHANNEL
            else:
                attempts.append(f"{ROUTE_CHANNEL} : publication ou média introuvable dans l'aperçu")

    # Les tentatives sont conservées même en cas de succès : savoir que le
    # `file_id` a été refusé et que c'est l'aperçu qui a sauvé le média se relit
    # des semaines plus tard, quand le même média manquera à nouveau.
    result["notes"] = list(attempts)
    if not data:
        result["outcome"] = OUTCOME_FAILED
        result["reason"] = " ; ".join(attempts) or "aucune source n'a rendu le fichier"
        return result

    try:
        # `restore_object` est bloquant (réseau) : hors de l'event loop.
        size = await asyncio.to_thread(restore, path, data, mime_type=mime_type, upsert=True)
    except Exception as exc:
        result["outcome"] = OUTCOME_FAILED
        result["reason"] = f"dépôt impossible : {type(exc).__name__}: {exc}"
        return result

    result.update(
        {
            "ok": True,
            "outcome": OUTCOME_RESTORED,
            "route": route,
            "bytes": size,
            "size_matches": _size_matches(row, size),
            "fingerprint_matches": _fingerprint_matches(row, data),
            "reason": None,
        }
    )
    return result


async def repair_missing(
    rows: List[Dict[str, Any]],
    *,
    download_file_id: Optional[FileDownloader] = None,
    fetch_message: Optional[MessageFetcher] = None,
    restore: Optional[Restorer] = None,
    exists: Optional[Existence] = None,
    max_bytes: int = telegram_channel.MAX_MEDIA_BYTES,
) -> Dict[str, Any]:
    """Répare un lot de lignes, **une par une**, et compte les trois issues.

    Séquentiel à dessein : chaque réparation télécharge un fichier entier et le
    redépose ; les mener en parallèle multiplierait la mémoire par le nombre de
    lignes sans rien apprendre de plus. `restored`, `failed` et `skipped` sont
    distingués parce qu'ils n'appellent pas la même suite : un échec se rejoue,
    un « non réparable » ne se rejouera jamais sans nouvelle information (un
    `chat_id` public, un `file_id` à jour).

    `verified` et `diverged` comptent, parmi les lignes réparées, celles dont
    l'empreinte **prouve** que les octets sont ceux de l'ingestion et celles
    dont l'empreinte dit le contraire. Les deux ne s'additionnent pas à
    `restored` : la différence est faite des médias sans référence enregistrée,
    où l'on ne sait pas — un compte qui les inclurait dans `verified`
    présenterait une ignorance comme une preuve.
    """
    results: List[Dict[str, Any]] = []
    for row in rows:
        results.append(
            await repair_media(
                row,
                download_file_id=download_file_id,
                fetch_message=fetch_message,
                restore=restore,
                exists=exists,
                max_bytes=max_bytes,
            )
        )
    counts = {outcome: 0 for outcome in (OUTCOME_RESTORED, OUTCOME_FAILED, OUTCOME_SKIPPED)}
    for result in results:
        counts[result["outcome"]] += 1
    return {
        "requested": len(rows),
        "restored": counts[OUTCOME_RESTORED],
        "failed": counts[OUTCOME_FAILED],
        "skipped": counts[OUTCOME_SKIPPED],
        "verified": sum(1 for r in results if r.get("fingerprint_matches") is True),
        "diverged": sum(1 for r in results if r.get("fingerprint_matches") is False),
        "results": results,
    }
