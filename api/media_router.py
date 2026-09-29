"""Les médias et leur texte indexé, relus depuis le tableau de bord (surface protégée).

Un média vit en trois endroits : les octets dans le bucket Storage, sa description
dans `knowledge_media`, son texte dans `knowledge_chunks`. Le tableau de bord
montrait les deux premiers — le fichier, son actif, son verdict de revue — mais
jamais ce qui a réellement été **indexé**, c'est-à-dire ce que lisent les analyses,
les prompts et `/search`. Seul Telegram permettait de le relire, par la pièce
jointe `.txt` du compte-rendu (`telegram_media.attachment_for`) : au moment de
l'ingestion, et seulement dans le chat.

Deux routes, parce qu'une réponse de texte a besoin d'un identifiant et qu'aucune
ligne de média n'en portait jusque-là :

* `GET /media` **liste** les médias (`media_store.list_media`, du plus récent au
  plus ancien, filtrable et paginé) avec, pour chacun, son verdict de revue et son
  **nombre de morceaux** (`knowledge_index.chunk_counts`) — de quoi choisir une
  ligne avant de demander quoi que ce soit ;
* `GET /media/{id}/text` rend les morceaux de ce média, dans l'ordre du document,
  par la délégation média (`media_store.list_media_chunks`) — la logique de
  lecture reste dans `database/knowledge_index.py`, comme pour l'écriture.

Deux choix qui ne se voient pas dans la réponse :

* **`embedding` n'est jamais lu.** Chaque morceau porte le vecteur de 768
  flottants qui sert à la recherche (`ai/embeddings.py`) : un `select("*")`
  enverrait des dizaines de kilo-octets de nombres par morceau, de Postgres
  jusqu'au navigateur, pour un affichage qui n'en montre rien. La projection est
  donc restreinte **dans la requête** (`READING_COLUMNS`) — filtrer après coup
  laisserait tout traverser, et ne réglerait que l'apparence ;
* **un média introuvable est un 404, mais un média sans texte est un 200.** Un
  média rejeté en revue (`❌`) a **zéro** morceau et reste parfaitement valide :
  c'est son verdict qui l'explique, et le confondre avec une erreur rendrait les
  deux indiscernables — précisément ce que la revue cherchait à distinguer.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.security import require_api_key
from database.knowledge_index import chunk_counts
from database.media_store import (
    asset_tag,
    content_was_extracted,
    get_media,
    list_media,
    list_media_chunks,
    review_status,
)

router = APIRouter(
    prefix="/media",
    tags=["Médias"],
    dependencies=[Depends(require_api_key)],
)

#: Colonnes lues pour relire un texte. `embedding` en est absent **exprès** : la
#: projection est demandée à la base, donc le vecteur ne quitte pas Postgres.
READING_COLUMNS = ("chunk_index", "content", "token_count")

#: Médias rendus quand l'appelant ne demande rien : un écran de tableau de bord,
#: où l'on choisit une ligne — pas un export.
DEFAULT_PAGE = 25
#: Plafond : au-delà, ce n'est plus une liste à parcourir à l'œil. Il garde aussi
#: la lecture groupée des morceaux proportionnée à la page.
MAX_PAGE = 100


def _listing_row(row: Dict[str, Any], chunks: int) -> Dict[str, Any]:
    """Un média réduit à ce qu'une liste doit montrer — et à rien d'autre.

    La charge est **reconstruite** champ par champ : la légende (le texte de la
    publication) et les colonnes non nommées ici ne sortent pas de la base, alors
    qu'un `**row` les publierait toutes.

    Le verdict de revue et le résultat d'extraction viennent de `metadata` : ce
    sont eux qui expliquent une ligne à **zéro** morceau — rejetée en revue, ou
    jamais extraite —, les deux cas qu'un compteur seul confondrait.
    """
    asset, asset_source = asset_tag(row)
    return {
        "media_id": row.get("id"),
        "file_name": row.get("file_name"),
        "media_type": row.get("media_type"),
        "file_size": row.get("file_size"),
        "created_at": row.get("created_at"),
        "source": row.get("source"),
        "chat_id": row.get("chat_id"),
        "message_id": row.get("message_id"),
        "asset": asset,
        "asset_source": asset_source,
        "review_status": review_status(row),
        "extracted": content_was_extracted(row),
        "chunks": chunks,
    }


@router.get("")
def media_listing(
    limit: int = Query(
        DEFAULT_PAGE,
        ge=1,
        le=MAX_PAGE,
        description=f"Médias renvoyés au plus (défaut : {DEFAULT_PAGE}).",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Rang du premier média de la page (0 = le plus récent).",
    ),
    source: Optional[str] = Query(
        None,
        max_length=40,
        description="Ne garde qu'une provenance (`telegram`, `channel_scraper`…).",
    ),
    media_type: Optional[str] = Query(
        None,
        max_length=40,
        description="Ne garde qu'un type (`photo`, `video`, `document`…).",
    ),
) -> Dict[str, Any]:
    """Les médias stockés, du plus récent au plus ancien — de quoi choisir un identifiant.

    Chaque ligne porte ce qu'il faut pour en **choisir** une sans deviner : de quoi
    la reconnaître (nom, type, taille, date, origine), son actif, son verdict de
    revue, si son contenu a été extrait, et combien de morceaux son texte compte
    (`chunks`). Un identifiant choisi ici se lit ensuite par
    `GET /media/{media_id}/text`.

    **La table n'est pas comptée.** Lire une ligne de plus que demandé dit s'il en
    reste (`truncated`, `next_offset`) ; un total exact demanderait un second
    parcours de la table pour un nombre dont personne ne se sert pour lire la page
    suivante. L'ordre de `list_media` est total, donc `offset` ne saute ni ne
    répète une ligne.

    Un média **sans texte** reste dans la liste avec `chunks: 0` : c'est le cas
    d'une extraction rejetée en revue, et c'est `review_status`/`extracted` qui
    l'explique. Ce sont les **comptes** qui font échouer l'appel : sans eux, la
    réponse ne dit plus ce pour quoi elle existe, et un `null` par ligne laisserait
    le premier client qui affiche `0` faire croire à un média vide.
    """
    try:
        rows = list_media(
            source=source, media_type=media_type, limit=limit + 1, offset=offset
        )
    except Exception as exc:
        # Une panne doit être bruyante : une liste vide dirait « aucun média ».
        raise HTTPException(
            status_code=502,
            detail=f"Lecture des médias impossible : {type(exc).__name__}: {exc}",
        ) from exc

    truncated = len(rows) > limit
    page = rows[:limit]
    media_ids = [str(row.get("id")) for row in page if row.get("id")]
    try:
        counts = chunk_counts(media_ids)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Index des morceaux illisible : {type(exc).__name__}: {exc}",
        ) from exc

    return {
        "returned": len(page),
        "offset": offset,
        "limit": limit,
        "truncated": truncated,
        "next_offset": offset + len(page) if truncated else None,
        "text": "GET /media/{media_id}/text",
        "media": [
            _listing_row(row, counts.get(str(row.get("id")), 0)) for row in page
        ],
    }


@router.get("/{media_id}/text")
def media_text(media_id: str) -> Dict[str, Any]:
    """Morceaux indexés d'un média, dans l'ordre du document.

    `chars` est le total des contenus : le découpage **chevauche** ses morceaux
    (`knowledge_index.replace_chunks`), donc ce total dépasse la longueur du
    document d'origine. C'est une mesure de l'index, pas du fichier — et
    `count` seul dit combien de morceaux les analyses ont réellement à lire.
    """
    row = get_media(media_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Média introuvable : {media_id}")

    chunks = list_media_chunks(media_id, columns=READING_COLUMNS)
    asset, asset_source = asset_tag(row)
    return {
        "media_id": media_id,
        "count": len(chunks),
        "chars": sum(len(str(chunk.get("content") or "")) for chunk in chunks),
        "asset": asset,
        "asset_source": asset_source,
        "review_status": review_status(row),
        "chunks": chunks,
    }


__all__ = ["router"]
