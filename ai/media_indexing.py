"""Du média au texte indexé — pipeline partagé par les deux routes d'ingestion.

Deux routes récupèrent des médias Telegram, et ce module existe parce qu'elles
doivent produire **exactement** le même résultat :

* celle du bot (`notifications/telegram_media.py`) : médias envoyés au bot ou
  publiés dans un canal dont il est administrateur ;
* celle du scraper public (`scrapers/telegram_channel.py`) : photos et vidéos
  exposées par l'aperçu `t.me/s/<canal>`.

L'ordre des opérations y est une règle, pas un détail d'implémentation :

1. **extraire** le texte (`ai.media_extractor` : vision Gemini pour les images,
   `pypdf` pour les PDF, et pour l'audio et la vidéo Groq `whisper-large-v3` —
   avec le repli local `faster-whisper` quand Groq ne peut pas servir) ;
2. **composer** le texte indexable — la **légende d'abord**, puis l'extrait —
   parce qu'une légende « BTC support 64k » reste cherchable et nomme souvent
   l'actif mieux qu'une description visuelle, et parce qu'elle doit exister dans
   l'index **même si l'extraction échoue** ;
3. **indexer** dans `knowledge_chunks` — par la délégation média de
   `database.media_store` (`replace_media_chunks`), donc par la même porte que
   le reste de la vie du média —, en étiquetant l'actif détecté : c'est
   `knowledge_chunks.asset` que lit le filtre préférentiel SQL, donc ce qui fait
   remonter le média dans les analyses de son actif plutôt que de le laisser
   « joker » ;
4. **enregistrer** l'étiquette sur la ligne média (`knowledge_media.metadata`),
   pour `/media` et pour qu'un réenregistrement ne la perde pas ;
5. **noter le résultat** de l'extraction sur cette même ligne — pour qu'un échec
   reste lisible après que le compte-rendu a défilé. Sans cette trace, un média
   dont la clef manquait à l'ingestion est **indiscernable** d'un média lu
   correctement : dès qu'il y a une légende, les deux ont des morceaux indexés.
   C'est cette note que lit `/transcribe` pour savoir quoi rattraper.

Une extraction impossible ne remet jamais en cause l'ingestion : le média est
déjà stocké, l'échec est **rapporté** (le compte-rendu le dit), jamais levé.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional, Tuple

from ai import media_extractor
from core.asset_tags import detect_asset
from database import media_store

#: Longueur maximale de l'**aperçu** du texte extrait dans un message. Au-delà,
#: l'aperçu serait tronqué : le texte complet part alors en pièce jointe (voir
#: `telegram_media.attachment_for`), parce qu'un fragment qui finit par « … » ne
#: permet pas de relire ce qu'on valide.
EXCERPT_CHARS = 400

#: Extrait le texte exploitable d'un média (`ai.media_extractor.extract_text`).
Extractor = Callable[..., Dict[str, Any]]
#: Indexe le texte extrait : la délégation média de `database.media_store`
#: (`replace_media_chunks`), et non `database.knowledge_index` directement — les
#: deux routes d'ingestion (bot Telegram et scraper public) écrivent ainsi par la
#: même porte.
ChunkStore = Callable[..., int]
#: Enregistre l'actif associé (`database.media_store.set_media_asset`).
Tagger = Callable[..., Dict[str, Any]]
#: Note le résultat de l'extraction sur la ligne média
#: (`database.media_store.set_extraction_outcome`).
Recorder = Callable[..., Any]


def media_id_of(row: Any) -> Optional[str]:
    """Identifiant d'un média, quel que soit le type de la ligne renvoyée.

    `media_store.upload_media` rend selon le client un `dict` ou un objet : les
    deux routes doivent en tirer le même identifiant pour pouvoir indexer, sans
    dépendre de la forme de la réponse.
    """
    if isinstance(row, dict):
        return row.get("id")
    return getattr(row, "id", None)


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """Aperçu compact du texte extrait : espaces normalisés, puis tronqué.

    L'utilisateur doit pouvoir **vérifier** l'extraction d'un coup d'œil (la
    vision a-t-elle bien lu le graphique ? Whisper a-t-il transcrit la bonne
    langue ?). Un passage à la ligne fantaisiste dans une réponse de modèle ne
    doit pas déformer l'aperçu : on normalise les espaces avant de tronquer.
    """
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "…"


def indexable_text(caption: str, extracted: str) -> str:
    """Texte à indexer : la légende d'abord (elle nomme l'actif), puis l'extrait.

    Les deux sont cherchables au même titre ; les parties vides sont ignorées,
    donc l'appelant peut toujours passer `caption` et `extracted` sans se
    demander lequel existe.
    """
    parts = [part.strip() for part in (caption, extracted) if (part or "").strip()]
    return "\n\n".join(parts)


def resolve_asset(
    asset: Optional[str],
    asset_source: Optional[str],
    caption: str,
    extracted: str,
    *,
    detect: Callable[..., Optional[str]] = detect_asset,
) -> Tuple[Optional[str], Optional[str]]:
    """Actif à étiqueter, et d'où il vient : choix imposé, légende, ou contenu.

    L'ordre porte la priorité — la légende nomme l'actif, une description visuelle
    peut le déduire de travers. Aucune correspondance rend `(None, None)`, qui
    n'est pas un échec mais une valeur : non étiqueté est le **joker** du filtre
    SQL (candidat pour tout actif, sans bonus de classement).
    """
    if asset:
        return asset, asset_source or "manual"
    from_caption = detect(caption)
    if from_caption:
        return from_caption, "caption"
    from_content = detect(extracted)
    return (from_content, "extraction") if from_content else (None, None)


async def extract_and_index(
    data: bytes,
    descriptor: Dict[str, Any],
    *,
    media_id: Optional[str],
    asset: Optional[str] = None,
    asset_source: Optional[str] = None,
    extract: Extractor = media_extractor.extract_text,
    store: ChunkStore = media_store.replace_media_chunks,
    record: Recorder = media_store.set_extraction_outcome,
) -> Dict[str, Any]:
    """Extrait le texte du média puis l'indexe dans `knowledge_chunks`.

    Ne fait jamais échouer l'ingestion : le média est **déjà** stocké, une
    extraction impossible est seulement rapportée. `extract` et `store` sont
    bloquants (réseau) : exécutés dans des threads.

    `descriptor` porte au minimum `media_type`, `mime_type`, `file_name` et
    `caption` — les deux routes construisent le même dictionnaire, c'est ce qui
    leur permet d'appeler cette fonction sans adaptation.

    L'**actif** est étiqueté sur les morceaux (`store(..., asset=)`) : c'est ce qui
    fait jouer le filtre préférentiel SQL sur les médias (`match_knowledge_chunks`),
    et non seulement le joker. `asset` force l'étiquette (choix manuel) ; sinon
    elle est détectée dans la **légende** d'abord, puis dans le texte extrait.

    Le résultat est noté sur la ligne média (`record`) **dans tous les cas**, y
    compris quand il n'y a rien à indexer : c'est cette note qui distingue plus
    tard « lu correctement » de « la clef manquait », et elle est écrite par le
    seul endroit qui connaisse les deux routes d'ingestion et les reprises.
    Elle n'est jamais bloquante — le média est déjà stocké, et un échec
    d'annotation ne doit pas faire croire que l'ingestion a échoué.
    """
    if not media_id:
        return {
            "ok": False,
            "method": None,
            "chars": 0,
            "chunks": 0,
            "text": "",
            "asset": None,
            "asset_source": None,
            "reason": "media_id absent (impossible d'indexer)",
        }

    try:
        result = await asyncio.to_thread(
            extract,
            data,
            media_type=descriptor["media_type"],
            mime_type=descriptor["mime_type"],
            file_name=descriptor["file_name"],
        )
    except Exception as exc:
        return {
            "ok": False,
            "method": None,
            "chars": 0,
            "chunks": 0,
            "text": "",
            "asset": None,
            "asset_source": None,
            "reason": f"extraction échouée : {exc}",
        }

    extracted = result.get("text") or ""
    # La LÉGENDE entre dans le texte indexé, même quand l'extraction échoue
    # (photo sans vision, PDF scanné…) : une légende « BTC support 64k » reste
    # cherchable, et elle nomme souvent l'actif mieux que la description visuelle.
    caption = str(descriptor.get("caption") or "").strip()
    text = indexable_text(caption, extracted)

    tag, tag_source = resolve_asset(asset, asset_source, caption, extracted)

    summary: Dict[str, Any] = {
        "ok": bool(result.get("ok")),
        "method": result.get("method"),
        "chars": len(text),
        "chunks": 0,
        "asset": tag,
        "asset_source": tag_source,
        "reason": result.get("reason"),
        # `excerpt` sert au message quand il suffit, `text` est le texte **entier**
        # tel qu'il sera indexé — c'est lui qui part en .txt quand l'aperçu est trop
        # court. Le garder ici ne coûte rien : il vient d'être calculé et la
        # réponse est jetée après l'envoi.
        "excerpt": excerpt(text),
        "text": text,
        "has_caption": bool(caption),
        "extracted_chars": len(extracted),
    }
    if not text:
        await _record_outcome(media_id, summary, record)
        return summary

    try:
        # `asset=tag` : l'étiquette voyage avec les morceaux, c'est elle que le
        # filtre SQL compare. `None` est une valeur légitime (= joker).
        summary["chunks"] = int(await asyncio.to_thread(store, media_id, text, asset=tag))
    except Exception as exc:
        summary["ok"] = False
        summary["reason"] = f"indexation échouée : {exc}"
    # **Après** l'indexation : le nombre de morceaux fait partie de ce qu'on note.
    await _record_outcome(media_id, summary, record)
    return summary


async def _record_outcome(
    media_id: str, summary: Dict[str, Any], record: Recorder
) -> None:
    """Note le résultat de l'extraction sur la ligne média — jamais bloquant.

    Un échec est **dit** (comme celui de l'étiquette) et non propagé : le média
    est stocké, son texte indexé, et perdre l'annotation ne justifie pas de
    faire échouer l'ingestion. Le dire évite le pire des cas — une note absente
    qu'on croirait « extraction réussie ».
    """
    try:
        await asyncio.to_thread(record, media_id, summary)
    except Exception as exc:
        print(
            f"   [medias] resultat d'extraction non enregistre ({media_id}) : "
            f"{type(exc).__name__}: {exc}"
        )


async def store_asset(
    media_id: Optional[str],
    asset: Optional[str],
    source: Optional[str],
    tag_store: Tagger = media_store.set_media_asset,
    *,
    reviewer: Optional[str] = None,
) -> bool:
    """Enregistre l'étiquette sur la **ligne** média — jamais bloquant.

    Les morceaux portent déjà l'étiquette (c'est eux que le filtre SQL lit) :
    l'écriture ici sert à la garder si l'extraction est refaite (un choix manuel
    ne doit pas être perdu par un `↩️ Réindexer`) et à l'afficher dans `/media`.
    Un échec est donc rapporté, pas propagé.
    """
    if not media_id or not asset:
        return False
    try:
        await asyncio.to_thread(tag_store, media_id, asset, source=source, reviewer=reviewer)
        return True
    except Exception as exc:
        print(
            f"   [medias] etiquette non enregistree ({media_id}) : {type(exc).__name__}: {exc}"
        )
        return False


__all__ = [
    "EXCERPT_CHARS",
    "ChunkStore",
    "Extractor",
    "Recorder",
    "Tagger",
    "excerpt",
    "extract_and_index",
    "indexable_text",
    "media_id_of",
    "resolve_asset",
    "store_asset",
]
