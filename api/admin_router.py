"""La réconciliation média, consultable sans exécuter de script sur la machine.

`scripts/reconcile_media.py` répond à « le bucket et `knowledge_media` sont-ils
cohérents ? » — mais il faut un shell sur la machine, les secrets du projet et un
`python` installé pour poser la question. Cet endpoint pose la **même** question,
à la même source (`media_store.list_orphan_objects`), depuis un navigateur ou un
appel `curl` : c'est le script qui garde la logique, le routeur ne fait que
l'exposer.

Trois choix qui ne se voient pas dans une réponse heureuse :

* **la lecture est seule.** Le script a un `--delete` ; l'endpoint n'en a pas et
  n'en aura pas. Un `GET` qui supprime des objets du Storage serait rejoué par un
  navigateur, un cache ou une sonde de disponibilité, et effacerait des octets
  sans trace — la suppression reste une décision humaine, prise depuis un shell
  ou la console Supabase, où elle laisse une commande dans l'historique ;
* **une panne n'est pas une liste vide.** Un bucket injoignable répond **502**,
  jamais `{"orphans": []}` : une liste vide veut dire « bucket et base
  cohérents », et confondre les deux ferait passer une panne Storage pour une
  bonne nouvelle — exactement ce que le code de sortie `1` du script distingue ;
* **`limit` borne la réponse, pas le travail.** `list_orphan_objects` parcourt le
  bucket en entier de toute façon (deux listings complets : Storage puis
  `knowledge_media`) : la borne évite de renvoyer des milliers de chemins, elle
  n'accélère rien. `count` reste le total **exact**, donc une réponse tronquée le
  dit (`truncated`) au lieu de laisser croire qu'il n'y en a pas plus — et comme
  le parcours est complet de toute façon, la suite se **demande** (`offset`,
  `next_offset`) au lieu de s'obtenir en relevant la borne : un listing ne doit
  pas obliger à choisir entre « la première tranche » et « tout d'un coup ».

Le **sens inverse** est là aussi (`GET /admin/media/missing`) : des lignes
décrites dont l'objet a disparu du bucket. La réparation, elle, est un **`POST`**
(`/admin/media/missing/repair`) : elle n'efface rien mais elle re-télécharge des
fichiers, donc elle se demande explicitement et sur des identifiants nommés — un
`GET` la déclencherait depuis un cache, un préchargement de lien ou une sonde.
Deux garde-fous y sont appliqués : un objet **présent** n'est jamais réécrit, et
la ligne n'est jamais touchée (`core/media_repair`).

Le **dernier venu** est d'une autre nature, et il est traité comme tel :
`POST /admin/supabase/roundtrip` éprouve la base en **écrivant** (puis en
supprimant) sur les tables nommées. C'est `scripts/check_supabase.py --roundtrip`
sans terminal — utile quand la question se pose depuis un téléphone, et c'est
précisément pour ça qu'il est un `POST` à sélection **explicite** : une route qui
écrit ne déduit pas son périmètre. `GET /admin/supabase/check` est sa moitié
lecture seule : les mêmes vérifications, sans une seule écriture.

Comme les autres routeurs internes, il exige `X-API-Key: <INTERNAL_API_KEY>` et
refuse en 503 sans clé configurée (`api.security`, fail-closed).
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.security import require_api_key
from core import media_repair
from database.media_store import (
    BUCKET,
    asset_tag,
    content_fingerprint,
    get_media,
    list_missing_objects,
    list_orphan_objects,
    review_status,
)

router = APIRouter(
    prefix="/admin",
    tags=["Administration"],
    dependencies=[Depends(require_api_key)],
)

#: Objets rendus quand l'appelant ne demande rien (les plus longs listings n'ont
#: pas à traverser le réseau pour un diagnostic qui se lit à l'œil).
DEFAULT_LIMIT = 100
#: Plafond : un orphelin est un chemin, mais mille chemins suffisent largement à
#: décider quoi faire — et `count` dit le reste.
MAX_LIMIT = 1000

#: Médias réparés au plus en un appel : chaque réparation télécharge un fichier
#: entier et le redépose. Au-delà, l'appelant découpe — un lot trop long finirait
#: en timeout au milieu, sans dire lesquels sont passés.
MAX_REPAIR_IDS = 20

#: Tables nommées au plus pour un aller-retour. La sonde en éprouve sept au
#: maximum : au-delà, c'est une liste fabriquée, et la refuser vaut mieux que
#: l'accepter pour ne rien en faire.
MAX_PROBE_TABLES = 20


class RepairRequest(BaseModel):
    """Les médias à réparer, nommés un par un (aucun « tout » implicite)."""

    media_ids: List[str] = Field(
        ...,
        min_length=1,
        max_length=MAX_REPAIR_IDS,
        description=f"Identifiants des médias (1 à {MAX_REPAIR_IDS}).",
    )


async def download_by_file_id(file_id: str) -> bytes:
    """Octets d'un `file_id` Telegram, via le bot partagé.

    L'import est **local**, comme dans `api/webhook` : la surface protégée doit
    s'importer sans `python-telegram-bot`, et un token absent doit dégrader cette
    seule voie (le scraper public reste tenté), pas empêcher le serveur de
    démarrer.
    """
    from notifications.notify import get_bot

    tg_file = await get_bot().get_file(file_id)
    return bytes(await tg_file.download_as_bytearray())


@router.get("/media/orphans")
def media_orphans(
    prefix: str = Query(
        "",
        max_length=200,
        description="Ne considère que ce dossier du bucket (défaut : tout le bucket).",
    ),
    limit: int = Query(
        DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description=f"Chemins renvoyés au plus (défaut : {DEFAULT_LIMIT}).",
    ),
    offset: int = Query(
        0,
        ge=0,
        description="Rang du premier orphelin de la page (0 = le premier chemin).",
    ),
) -> Dict[str, Any]:
    """Objets du bucket sans ligne `knowledge_media` — les orphelins.

    Un orphelin vient d'un upload interrompu avant l'insertion, d'un échec du
    nettoyage automatique, ou d'une ligne supprimée à la main : il occupe du
    stockage sans être référencé nulle part, donc invisible dans l'application.
    Le sens inverse (ligne sans objet) n'est **pas** traité ici : il demande de
    re-télécharger le média, pas de supprimer quoi que ce soit.

    La liste se **parcourt** : `offset` avance dans les orphelins et
    `next_offset` donne la page suivante (`null` à la fin). Consulter au-delà de
    la première tranche ne demande donc pas de relever `limit`, qui n'est qu'une
    taille de réponse — sans chemin pour lire la suite, un listing obligeait à
    demander les mille d'un coup, ou à ne jamais voir le reste.

    L'ordre est celui du parcours (`storage_path` trié, donc total et unique) :
    une page ne saute ni ne répète un chemin. **Aucun instantané n'est gardé**
    d'une page à l'autre : le bucket et la table sont relus à chaque appel, donc
    un nettoyage ou une ingestion survenu entre-temps déplace les rangs, et
    `count` est relu avec eux au lieu d'être promis.
    """
    scope = prefix.strip("/")
    try:
        orphans = list_orphan_objects(scope)
    except Exception as exc:
        # Une panne doit être bruyante : une liste vide dirait « tout va bien ».
        raise HTTPException(
            status_code=502,
            detail=f"Réconciliation impossible : {type(exc).__name__}: {exc}",
        ) from exc

    shown = orphans[offset : offset + limit]
    following = offset + len(shown)
    return {
        "bucket": BUCKET,
        "prefix": scope,
        "count": len(orphans),
        "returned": len(shown),
        "offset": offset,
        "limit": limit,
        "truncated": following < len(orphans),
        "next_offset": following if following < len(orphans) else None,
        "orphans": shown,
        "deletion": "scripts/reconcile_media.py --delete",
    }


@router.get("/media/missing")
def media_missing(
    prefix: str = Query(
        "",
        max_length=200,
        description="Ne considère que ce dossier du bucket (défaut : tout le bucket).",
    ),
    limit: int = Query(
        DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description=f"Lignes renvoyées au plus (défaut : {DEFAULT_LIMIT}).",
    ),
) -> Dict[str, Any]:
    """Lignes `knowledge_media` dont l'**objet a disparu** du bucket.

    L'inverse de `media_orphans`, et le plus gênant des deux : l'application croit
    le média disponible (ligne complète, texte indexé, verdict de revue), mais les
    octets ne sont plus là — les liens signés répondent 404 et le tableau de bord
    pointe dans le vide. Aucune autre lecture ne le voit.

    Chaque ligne annonce la voie de retour possible (`route`) avant qu'on essaie
    quoi que ce soit : `telegram_file_id` quand le bot a l'identifiant, sinon
    `channel_scraper` quand le `chat_id` est un canal **public** avec un
    `message_id`, sinon `null` — une ligne irréparable par l'endpoint.

    `fingerprint` dit si une empreinte a été enregistrée à l'ingestion, donc si
    la restauration pourra être **prouvée** (`POST .../repair` compare les octets
    re-téléchargés). `false` ne condamne pas la ligne — elle reste réparable —
    mais annonce qu'on ne saura dire que la taille.
    """
    scope = prefix.strip("/")
    try:
        rows = list_missing_objects(scope)
    except Exception as exc:
        # Une panne doit être bruyante : une liste vide dirait « rien ne manque ».
        raise HTTPException(
            status_code=502,
            detail=f"Réconciliation impossible : {type(exc).__name__}: {exc}",
        ) from exc

    missing = [
        {
            "media_id": row.get("id"),
            "storage_path": row.get("storage_path"),
            "media_type": row.get("media_type"),
            "source": row.get("source"),
            "chat_id": row.get("chat_id"),
            "message_id": row.get("message_id"),
            "file_size": row.get("file_size"),
            "created_at": row.get("created_at"),
            "route": media_repair.repair_route(row),
            "fingerprint": bool(content_fingerprint(row)),
            "asset": asset_tag(row)[0],
            "review_status": review_status(row),
        }
        for row in rows[:limit]
    ]
    return {
        "bucket": BUCKET,
        "prefix": scope,
        "count": len(rows),
        "returned": len(missing),
        "truncated": len(missing) < len(rows),
        "repairable": sum(1 for entry in missing if entry["route"]),
        "missing": missing,
        "repair": "POST /admin/media/missing/repair",
    }


@router.post("/media/missing/repair")
async def repair_missing_media(
    payload: RepairRequest = Body(...),
) -> Dict[str, Any]:
    """Re-télécharge les médias nommés et remet leurs octets dans le bucket.

    Deux refus qui protègent des données : un objet **déjà présent** n'est pas
    réécrit — la voie du scraper rend la copie de l'aperçu, possiblement réduite,
    donc restaurer par-dessus un original intact le dégraderait pour rien ; et la
    **ligne n'est jamais modifiée**, sinon `metadata` (verdict de revue, actif,
    note d'extraction) serait écrasé et l'extraction repayée alors que le texte
    est toujours indexé.

    Ordre : `telegram_file_id` (le fichier exact), puis l'aperçu public du canal
    (la seule voie pour un média ingéré par le scraper). Un identifiant inconnu
    est rapporté à part (`unknown`) sans interrompre les autres.

    Chaque résultat restauré dit si les octets sont **ceux de l'ingestion**
    (`fingerprint_matches`) : `true` prouvé par l'empreinte, `false` un autre
    fichier (l'aperçu public en sert une copie réduite), `null` aucune
    empreinte enregistrée. Le lot résume les deux premiers (`verified`,
    `diverged`).
    """
    unknown: List[str] = []
    rows: List[Dict[str, Any]] = []
    for media_id in payload.media_ids:
        row = await asyncio.to_thread(get_media, media_id)
        if row:
            rows.append(row)
        else:
            unknown.append(media_id)

    report = await media_repair.repair_missing(
        rows, download_file_id=download_by_file_id
    )
    report["unknown"] = unknown
    return report


class SupabaseProbeRequest(BaseModel):
    """Les tables à éprouver, nommées — aucun « tout » implicite.

    Même règle que `RepairRequest` : un `POST` qui **écrit** ne déduit pas son
    périmètre. Les noms passent par `resolve_only()` du script, donc une table ou
    un **groupe** (`core`, `engine`, `knowledge`) est accepté — exactement comme
    après `--only` —, et un nom inconnu est refusé en **400**, avant la moindre
    écriture.
    """

    tables: List[str] = Field(
        ...,
        min_length=1,
        max_length=MAX_PROBE_TABLES,
        description=(
            "Noms de tables ou de groupes visés, comme l'option `--only` du "
            "script. Un nom inconnu est refusé, et le refus liste les choix."
        ),
    )


def supabase_probe():
    """Le script de diagnostic, importé **à l'appel**.

    Deux raisons, et la seconde est la vraie : la logique — résolution de la
    sélection, écriture, relecture, **nettoyage** — doit rester dans le script,
    pour que le terminal et l'application éprouvent exactement la même chose ; et
    l'import est paresseux, comme celui de `notifications.notify`, pour que la
    surface protégée s'importe sans le paquet `supabase`.
    """
    from scripts import check_supabase

    return check_supabase


def supabase_command(tables: Optional[Iterable[str]], *, roundtrip: bool) -> str:
    """La ligne de terminal équivalente — ce qu'un opérateur taperait."""
    line = "python scripts/check_supabase.py"
    if roundtrip:
        line += " --roundtrip"
    if tables:
        line += " --only " + ",".join(str(table) for table in tables)
    return line


async def supabase_report(
    *, tables: Optional[Iterable[str]], roundtrip: bool
) -> Dict[str, Any]:
    """Le rapport du script, enrichi de ce qu'une surface doit dire.

    Trois choix qui ne se voient pas dans une réponse heureuse :

    * **la sélection est résolue avant tout appel** — la même fonction que le
      script, donc le même message d'erreur : un nom inconnu est un **400**, jamais
      un repli sur « tout ». Avec l'aller-retour, un repli silencieux écrirait
      précisément là où on a demandé de ne pas aller ;
    * **le verdict est dans la réponse, pas dans le code HTTP.** Le rapport porte
      `ok` et une ligne par vérification : une base en échec est donc un 200 qui le
      dit, parce que la forme de la réponse sait l'exprimer — c'est le contraire
      d'une liste vide, indiscernable d'une bonne nouvelle. Un **502** est réservé
      à la sonde qui **tombe** : l'appel n'a alors rien rendu de lisible ;
    * **le `.env` de la machine n'est pas lu.** `run()` ne le charge pas, et c'est
      délibéré : le serveur a déjà sa configuration, et une requête ne doit pas
      pouvoir désigner une autre base que la sienne.

    `writes` est `None` pour une consultation et la liste des tables réellement
    éprouvées pour un aller-retour : c'est ce qui distingue « je regarde » de
    « j'écris », y compris quand la sélection ne contient **aucune** table que
    l'aller-retour sache éprouver — le script le dit alors, et la liste est vide.

    **Un nettoyage incomplet prévient aussi l'opérateur.** Le rapport porte
    `leftovers` (les lignes de sonde restées en base) ; la route les relaie *et*
    déclenche l'alerte du script, parce qu'un reste en base ne doit pas dépendre de
    quelqu'un qui aurait encore la page ouverte. Dans un thread : l'alerte fait
    `asyncio.run` (pas de boucle en cours là-bas) et l'envoi Telegram bloque.
    """
    check_supabase = supabase_probe()
    selection = check_supabase.resolve_only(tables)
    if not selection["ok"]:
        raise HTTPException(
            status_code=400, detail=check_supabase.selection_problem(selection)
        )

    chosen = [*selection["required"], *selection["optional"]]
    try:
        report = await asyncio.to_thread(
            check_supabase.run, roundtrip=roundtrip, as_json=False, only=tables
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"Sonde impossible : {type(exc).__name__}: {exc}"
        ) from exc

    leftovers = list(report.get("leftovers") or [])
    alert = None
    if leftovers:
        alert = await asyncio.to_thread(check_supabase.alert_leftovers, leftovers)

    return {
        **report,
        "leftovers": leftovers,
        "alert": alert,
        "selection": {"requested": selection["requested"], "tables": chosen},
        "writes": (
            [table for table in check_supabase.ROUNDTRIP_TABLES if table in set(chosen)]
            if roundtrip
            else None
        ),
        "command": supabase_command(tables, roundtrip=roundtrip),
    }


@router.get("/supabase/check")
async def supabase_check(
    only: Optional[List[str]] = Query(
        None,
        description="Tables ou groupes visés (répétable, ou séparés par des virgules).",
    ),
) -> Dict[str, Any]:
    """Les trois niveaux du script **sans rien écrire** : configuration, tables, sélection.

    C'est le contrôle qu'une application peut rappeler sans conséquence — au
    démarrage, après un déploiement, depuis un écran d'administration : aucune
    ligne n'est créée, modifiée ni supprimée. Une base injoignable n'est pas un 502
    pour autant : le rapport du script le dit (`ok: false` et la ligne fautive).
    """
    return await supabase_report(tables=only, roundtrip=False)


@router.post("/supabase/roundtrip")
async def supabase_roundtrip(
    payload: SupabaseProbeRequest = Body(...),
) -> Dict[str, Any]:
    """Écrit, relit puis **supprime** sur les tables nommées (nettoyage compris).

    C'est `scripts/check_supabase.py --roundtrip`, déclenché depuis l'application
    plutôt que depuis un terminal. **Cet appel écrit dans la base** que
    l'application utilise — production comprise — puis nettoie derrière lui (les
    lignes portent un suffixe `probe-…`), et un nettoyage qui échoue est
    **annoncé** dans le rapport, avec le détail à supprimer à la main.

    D'où un `POST` — jamais un `GET`, qu'un cache ou un préchargement de lien
    rejouerait — et une sélection **explicite** : les tables sont nommées une par
    une, comme pour la réparation média. C'est le même verrou que le reste de
    l'administration (la clé interne), avec en plus un périmètre que l'appelant
    doit écrire.

    Deux sondes simultanées ne se gênent pas : chaque ligne créée porte un suffixe
    aléatoire, et chaque appel nettoie les siennes. L'appel prend en revanche
    **plusieurs secondes** (une trentaine de requêtes réseau) : il se lance depuis
    un écran d'administration, pas depuis un rafraîchissement automatique.
    """
    return await supabase_report(tables=payload.tables, roundtrip=True)


__all__ = ["router"]
