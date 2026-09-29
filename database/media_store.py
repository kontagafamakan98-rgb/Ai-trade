"""Médias Telegram : octets dans Storage, métadonnées dans Postgres.

Ce module est l'interface Python de la migration `008_telegram_media.sql` :

* le bucket Storage privé `telegram-media` contient les fichiers ;
* la table `knowledge_media` décrit chaque fichier (canal, message, type MIME,
  taille, légende…).

La table `knowledge_chunks` (pgvector) n'est **pas implémentée** ici :
`database/knowledge_index.py` en est propriétaire (découpage, embeddings Gemini,
RPC `match_knowledge_chunks`). Ce module n'en expose que des **délégations**
(`replace_media_chunks`, `list_media_chunks`, `search_chunks`) pour offrir un
point d'entrée orienté média — sans créer une seconde source de vérité.

Le module sait aussi **réconcilier** le bucket et la table, dans les deux sens :
`list_orphan_objects` rend les objets présents dans le bucket sans ligne
correspondante (upload interrompu, suppression manquée) et `delete_objects`
permet de les retirer ; à l'inverse, `list_missing_objects` rend les lignes dont
l'objet a disparu du bucket, et `restore_object` remet les octets en place sans
réécrire la ligne. Le script `scripts/reconcile_media.py` expose le premier sens
en ligne de commande.

Le backend utilise la clé `service_role` (voir `database/supabase_client.py`),
qui contourne la RLS. Le bucket étant privé, il n'existe aucune URL publique :
pour exposer un fichier, passe par `create_signed_url()` (lien temporaire).
"""
from __future__ import annotations

import hashlib
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from uuid import uuid4

from database import knowledge_index
from database.supabase_client import supabase

#: Bucket Storage créé par la migration 008 — privé.
BUCKET = "telegram-media"

#: Table de description des médias (migration 008).
TABLE = "knowledge_media"

#: Types de médias acceptés, alignés sur ce que produit Telegram. Volontairement
#: fermé : une faute de frappe ici ne doit pas créer silencieusement une valeur
#: que plus aucun filtre ne retrouverait.
MEDIA_TYPES = ("photo", "video", "document", "audio", "voice", "other")

#: Taille des lots de suppression : l'API Storage refuse les listes trop longues,
#: et un bucket réconcilié peut contenir des centaines d'orphelins.
REMOVE_BATCH_SIZE = 100

#: Plafond de lignes ramenées par page sur `knowledge_media` (l'API PostgREST
#: est bornée, par défaut à 1000) : au-delà, on pagine.
PAGE_SIZE = 1000


def _require_client() -> Any:
    """Refuse d'agir sans client configuré.

    Une écriture muette serait le pire des comportements : le média semblerait
    stocké alors que rien n'a été envoyé. On échoue donc franchement.
    """
    if supabase is None:
        raise RuntimeError(
            "Supabase n'est pas configuré : renseigne SUPABASE_URL et "
            "SUPABASE_SERVICE_KEY (voir .env.example)."
        )
    return supabase


def guess_mime_type(file_name: str) -> str:
    """Type MIME déduit de l'extension, avec repli générique."""
    guessed, _ = mimetypes.guess_type(file_name or "")
    return guessed or "application/octet-stream"


def media_object_path(
    *,
    source: str = "telegram",
    chat_id: Optional[str] = None,
    message_id: Optional[int] = None,
    file_name: Optional[str] = None,
) -> str:
    """Construit une clé d'objet stable dans le bucket.

    Déterministe dès que `chat_id` et `message_id` sont fournis
    (`telegram/<chat>/<message>-<fichier>`) : rejouer le même message RÉÉCRIT le
    même objet au lieu d'accumuler des copies. À défaut d'identifiants Telegram,
    on retombe sur un nom unique, pour ne jamais écraser un fichier existant.
    """
    safe_name = Path(file_name).name if file_name else f"{uuid4().hex}.bin"
    folder = f"{source or 'telegram'}/{chat_id or 'inconnu'}"
    prefix = f"{message_id}-" if message_id is not None else ""
    return f"{folder}/{prefix}{safe_name}"


#: Clé de `metadata` où vit l'**empreinte des octets** déposés à l'ingestion.
#: Une annotation, comme le verdict de revue (`REVIEW_KEY`) et l'actif
#: (`ASSET_KEY`) : `metadata` est déjà là pour ce genre de champ, aucune migration
#: à jouer. Comme eux, elle **survit à une restauration** — `restore_object` ne
#: touche qu'à Storage, jamais à la ligne.
FINGERPRINT_KEY = "content_sha256"


def fingerprint(data: bytes) -> str:
    """Empreinte SHA-256 des octets, en hexadécimal minuscule.

    SHA-256, et pas une somme de contrôle courte : il s'agit de dire « c'est le
    **même fichier** », et une empreinte faible laisserait passer une copie
    d'aperçu retaillée — précisément le cas que la réparation doit démasquer.
    """
    return hashlib.sha256(bytes(data)).hexdigest()


def content_fingerprint(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """Empreinte enregistrée à l'ingestion, ou `None` (média sans référence).

    Le `None` compte : il veut dire « on ne peut pas prouver », jamais « ça
    correspond ». Un appelant qui le lit comme un succès transformerait une
    ignorance en attestation — c'est pourquoi il n'est pas rendu en chaîne vide,
    qui se confondrait avec une empreinte absente dans un test de vérité.
    """
    if not row:
        return None
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None
    value = str(metadata.get(FINGERPRINT_KEY) or "").strip()
    return value or None


def upload_media(
    data: bytes,
    *,
    storage_path: Optional[str] = None,
    media_type: str = "other",
    mime_type: Optional[str] = None,
    file_name: Optional[str] = None,
    source: str = "telegram",
    chat_id: Optional[str] = None,
    message_id: Optional[int] = None,
    caption: Optional[str] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    duration_seconds: Optional[float] = None,
    telegram_file_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    upsert: bool = False,
) -> Dict[str, Any]:
    """Dépose les octets dans le bucket, puis décrit le fichier en base.

    L'ordre compte : si l'insertion en base échoue après un upload réussi, on
    supprime l'objet tout juste déposé (au mieux) pour ne pas laisser un
    orphelin dans le bucket — un fichier que plus aucune ligne ne référence.

    `upsert=True` remplace la ligne de même `storage_path` au lieu d'échouer
    dessus ; c'est ce qu'il faut pour rejouer un message Telegram déjà ingéré.

    L'empreinte SHA-256 des octets part avec la ligne (`FINGERPRINT_KEY`) : c'est
    la seule référence qui dira plus tard si un objet restauré est bien le fichier
    d'origine — une taille identique ne le prouve pas (`core/media_repair`).
    """
    client = _require_client()
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data doit être des octets (bytes)")
    if media_type not in MEDIA_TYPES:
        raise ValueError(
            f"media_type inconnu : {media_type!r} (attendu : {', '.join(MEDIA_TYPES)})"
        )

    path = storage_path or media_object_path(
        source=source,
        chat_id=chat_id,
        message_id=message_id,
        file_name=file_name,
    )
    resolved_mime = mime_type or guess_mime_type(file_name or path)
    # L'empreinte est **calculée**, jamais reçue de l'appelant : laisser annoncer
    # une valeur permettrait d'attester d'octets qu'on n'a pas déposés.
    recorded = dict(metadata or {})
    recorded[FINGERPRINT_KEY] = fingerprint(data)

    client.storage.from_(BUCKET).upload(
        path,
        bytes(data),
        {"content-type": resolved_mime, "upsert": "true" if upsert else "false"},
    )

    payload: Dict[str, Any] = {
        "source": source,
        "chat_id": chat_id,
        "message_id": message_id,
        "media_type": media_type,
        "mime_type": resolved_mime,
        "file_name": file_name or Path(path).name,
        "storage_path": path,
        "file_size": len(data),
        "caption": caption,
        "width": width,
        "height": height,
        "duration_seconds": duration_seconds,
        "telegram_file_id": telegram_file_id,
        "metadata": recorded,
    }

    try:
        if upsert:
            response = (
                client.table(TABLE).upsert(payload, on_conflict="storage_path").execute()
            )
        else:
            response = client.table(TABLE).insert(payload).execute()
        rows = response.data or []
        if not rows:
            raise RuntimeError("Supabase n'a renvoyé aucune ligne après l'insertion")
        return rows[0]
    except Exception:
        try:
            client.storage.from_(BUCKET).remove([path])
        except Exception as cleanup_error:  # pragma: no cover - réseau/API externe
            print(f"⚠️ média orphelin non supprimé ({path}) : {cleanup_error}")
        raise


def list_media(
    *,
    source: Optional[str] = None,
    chat_id: Optional[str] = None,
    media_type: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Liste les médias, du plus récent au plus ancien.

    `limit` est borné : une requête qui rapporte toute la table est un piège à
    mémoire, et ce module ne sert pas à exporter la base.

    L'ordre est **total** (`created_at`, puis `id`) : deux médias d'un même album
    partagent leur date à la seconde, et un tri non unique ne dit pas lequel des
    deux vient en premier — deux pages lues par `offset` exposeraient alors la
    même ligne et en cacheraient une autre. L'`id` est unique, donc l'ordre l'est
    aussi, et la seconde page commence exactement où la première s'arrête.
    """
    client = _require_client()
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))

    query = client.table(TABLE).select("*")
    if source:
        query = query.eq("source", source)
    if chat_id:
        query = query.eq("chat_id", chat_id)
    if media_type:
        query = query.eq("media_type", media_type)

    response = (
        query.order("created_at", desc=True)
        # Le second critère n'est pas décoratif : c'est lui qui rend l'ordre total
        # (voir la docstring), donc la lecture par `offset` honnête.
        .order("id", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return response.data or []


def find_media_by_telegram_file_id(telegram_file_id: str) -> Optional[Dict[str, Any]]:
    """Ligne `knowledge_media` correspondant à ce `telegram_file_id`, ou None.

    Sert à la **déduplication** : Telegram réutilise le même identifiant pour un
    fichier identique, donc le retrouver ici signifie que ce média est déjà
    stocké — inutile de le re-télécharger puis de l'écrire une seconde fois.
    """
    if not telegram_file_id:
        return None
    response = (
        _require_client()
        .table(TABLE)
        .select("*")
        .eq("telegram_file_id", telegram_file_id)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    return rows[0] if rows else None


def find_media_by_message(chat_id: str, message_id: int) -> Optional[Dict[str, Any]]:
    """Ligne `knowledge_media` d'un **message** précis, ou None.

    Sert à la déduplication **entre les deux routes** d'un même canal : le scraper
    public lit `t.me/s/<canal>` et n'a aucun `telegram_file_id` (donc invisible
    pour `find_media_by_telegram_file_id`), tandis que le bot reçoit le message
    lui-même. Le scraper stocke le **nom** du canal comme `chat_id` ; le bot le
    connaît par `chat.username` et pose la même clé — cette lecture retrouve donc
    l'un comme l'autre, sans colonne ni migration supplémentaire (l'index
    `(chat_id, message_id)` de la migration 008 couvre la requête).

    `message_id` vide ou `chat_id` vide : `None`, il n'y a rien à comparer.
    """
    if not chat_id or message_id in (None, ""):
        return None
    response = (
        _require_client()
        .table(TABLE)
        .select("*")
        .eq("chat_id", str(chat_id))
        .eq("message_id", message_id)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    return rows[0] if rows else None


def get_media(media_id: str) -> Optional[Dict[str, Any]]:
    """Retourne la description d'un média, ou None s'il n'existe pas."""
    response = (
        _require_client()
        .table(TABLE)
        .select("*")
        .eq("id", media_id)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    return rows[0] if rows else None


def delete_media(media_id: str, *, remove_object: bool = True) -> bool:
    """Supprime la description, puis l'objet du bucket (l'inverse de l'upload).

    Les `knowledge_chunks` attachés partent avec la ligne (`on delete cascade`).
    Retourne False si le média n'existe pas — l'appel est donc idempotent.
    """
    client = _require_client()
    row = get_media(media_id)
    if not row:
        return False

    client.table(TABLE).delete().eq("id", media_id).execute()

    path = row.get("storage_path")
    if remove_object and path:
        try:
            client.storage.from_(BUCKET).remove([path])
        except Exception as cleanup_error:  # pragma: no cover - réseau/API externe
            print(f"⚠️ objet non supprimé ({path}) : {cleanup_error}")
    return True


def download_media(storage_path: str) -> bytes:
    """Récupère les octets d'un objet (le bucket est privé, donc pas d'URL)."""
    return _require_client().storage.from_(BUCKET).download(storage_path)


def list_storage(path: str = "") -> List[Dict[str, Any]]:
    """Liste les objets du bucket, indépendamment de la table.

    Listing brut, **non récursif** : pour la réconciliation, préférer
    `list_orphan_objects()` (parcours complet + croisement avec la table) et
    `iter_storage_paths()` pour la liste exhaustive des chemins.
    """
    objects = _require_client().storage.from_(BUCKET).list(path)
    normalized: List[Dict[str, Any]] = []
    for obj in objects or []:
        if isinstance(obj, dict):
            normalized.append(obj)
        else:
            normalized.append(vars(obj))
    return normalized


def iter_storage_paths(prefix: str = "") -> List[str]:
    """Chemins de **tous** les objets du bucket, récursivement.

    `list()` n'est pas récursif : il rend les fichiers d'un dossier et, pour
    chaque sous-dossier, une entrée sans `id`. On descend donc explicitement, à
    l'aide d'une pile — un bucket profond ne doit pas faire récurser Python.
    """
    bucket = _require_client().storage.from_(BUCKET)
    found: List[str] = []
    stack = [prefix.strip("/")]
    visited: set = set()
    while stack:
        folder = stack.pop()
        if folder in visited:
            continue
        visited.add(folder)
        for entry in bucket.list(folder) or []:
            name = entry.get("name") if isinstance(entry, dict) else getattr(entry, "name", None)
            if not name:
                continue
            path = f"{folder}/{name}" if folder else name
            entry_id = entry.get("id") if isinstance(entry, dict) else getattr(entry, "id", None)
            if entry_id:
                found.append(path)
            else:
                # Entrée sans `id` = sous-dossier : on le parcourra plus tard.
                stack.append(path)
    return sorted(found)


#: Colonnes lues pour réconcilier les deux sens en **un seul** parcours. Ni
#: `caption`, ni la légende : une anomalie se nomme par son chemin et de quoi la
#: réparer. La projection reste explicite par principe — `select("*")` ramènerait
#: tout, y compris ce que personne ne lira.
#:
#: `metadata` y est **délibérément**, comme dans `REVIEW_LIST_COLUMNS` : c'est de
#: là que viennent l'**empreinte** enregistrée à l'ingestion (`FINGERPRINT_KEY`,
#: sans laquelle une restauration ne peut pas être prouvée) et ce que la liste des
#: manquants annonce à l'opérateur (actif, verdict de revue). L'omettre rendait
#: ces trois champs silencieusement `null` — une projection qui cache ce qu'on
#: vient lui demander.
RECONCILE_COLUMNS = (
    "id",
    "storage_path",
    "chat_id",
    "message_id",
    "telegram_file_id",
    "media_type",
    "source",
    "file_size",
    "created_at",
    "metadata",
)


def _read_media(
    columns: Sequence[str],
    *,
    page_size: int = PAGE_SIZE,
    order: str = "storage_path",
    desc: bool = False,
) -> List[Dict[str, Any]]:
    """Lecture **paginée** de `knowledge_media`, avec une projection nommée.

    La table peut contenir bien plus de lignes que ce que l'API accepte de
    renvoyer en une réponse : on pagine, et une page courte signale la fin.

    `order`/`desc` ne servent qu'à la **lecture** : un tri qui n'est pas unique
    (`created_at`, que deux éléments d'un même album peuvent partager) exposerait
    une ligne à la fois sur deux pages lors d'une comparaison de deux instantanés
    — c'est pourquoi la réconciliation, elle, garde l'ordre total de
    `storage_path` (unique).
    """
    client = _require_client()
    selection = ",".join(columns)
    page_size = max(1, min(int(page_size), PAGE_SIZE))
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        page = (
            client.table(TABLE)
            .select(selection)
            .order(order, desc=desc)
            .range(offset, offset + page_size - 1)
            .execute()
            .data
            or []
        )
        rows.extend(page)
        if len(page) < page_size:
            return rows
        offset += page_size


def all_media_paths(page_size: int = PAGE_SIZE) -> List[str]:
    """Tous les `storage_path` enregistrés dans `knowledge_media`.

    Lecture paginée par `_read_media` : la table peut contenir bien plus de lignes
    que ce que l'API accepte de renvoyer en une seule réponse.
    """
    return [
        row["storage_path"]
        for row in _read_media(("storage_path",), page_size=page_size)
        if row.get("storage_path")
    ]


#: Colonnes lues par les **listes de revue** (`/pending`, `/transcribe`). Le
#: **verdict** (`REVIEW_KEY`) et le **résultat d'extraction** (`OUTCOME_KEY`)
#: vivent dans `metadata` : il en fait donc partie, et c'est lui — pas une colonne
#: — qui décide de l'appartenance à ces listes. La projection reste explicite par
#: principe : la légende et le `telegram_file_id` ne sont pas lus ici, et elle est
#: **partagée** par les deux listes parce qu'elles montrent la même chose ; seule
#: la décision qu'elles en tirent diffère.
REVIEW_LIST_COLUMNS = (
    "id",
    "storage_path",
    "media_type",
    "file_name",
    "file_size",
    "created_at",
    "source",
    "chat_id",
    "message_id",
    "metadata",
)


def list_all_media(page_size: int = PAGE_SIZE) -> List[Dict[str, Any]]:
    """**Toute** la table `knowledge_media`, du plus récent au plus ancien.

    C'est la lecture que les deux listes qui ne peuvent pas se contenter des
    derniers médias partagent : `/pending` (verdict manquant) et `/transcribe`
    (texte à rattraper) y appliquent ensuite **leur** prédicat. Un seul parcours
    complet et une seule projection : deux scans séparés finiraient par ne plus
    voir les mêmes lignes, et une extraction introuvable dans l'un serait
    annoncée comme réparée par l'autre.

    Ce n'est pas `list_media` : celui-ci borne sa lecture (`limit`), exactement le
    défaut qu'un rattrapage ancien ne pardonne pas.
    """
    return _read_media(
        REVIEW_LIST_COLUMNS, page_size=page_size, order="created_at", desc=True
    )


def list_pending_review(page_size: int = PAGE_SIZE) -> List[Dict[str, Any]]:
    """Médias **sans verdict de revue**, du plus récent au plus ancien.

    C'est la liste que `/media` ne peut pas donner : `/media` borne sa lecture aux
    derniers médias (`list_media`, une page), donc une extraction ancienne jamais
    relue y est invisible dès qu'il y a eu dix ingestions depuis. Ici la table est
    parcourue **en entier** (voir `list_all_media`), sans borne d'âge.

    Le filtre passe par `review_status` — la **même** fonction qui affiche le
    verdict dans `/media` — plutôt que par un filtre JSONB côté Postgres. Un
    verdict présent mais illisible (clé absente, statut que `REVIEW_STATUSES` ne
    connaît pas) doit retomber dans « sans verdict » des deux côtés : deux
    prédicats différents finiraient par se contredire, et une ligne affichée sans
    verdict mais absente de la liste serait introuvable.
    """
    return [
        row for row in list_all_media(page_size=page_size) if review_status(row) is None
    ]


def reconcile_bucket(prefix: str = "") -> Dict[str, Any]:
    """Les **deux sens** de la réconciliation, en **un seul parcours**.

    Un objet sans ligne (orphelin : stockage occupé sans référence) et une ligne
    sans objet (média décrit mais absent : l'application pointe dans le vide) sont
    les deux faces du même écart. Les chercher séparément ferait deux parcours
    complets du bucket et deux lectures de la table — et surtout, deux
    implémentations qui *peuvent* diverger : un objet serait alors déclaré
    référencé d'un côté et orphelin de l'autre. Ici les deux listes sortent du
    même instantané, donc elles ne peuvent pas se contredire.

    Coût assumé : la table est lue avec `RECONCILE_COLUMNS` (et non le seul
    `storage_path`) parce que réparer une ligne manquante demande de savoir d'où
    re-télécharger, et — `metadata` compris — à quoi reconnaître que la
    restauration a rendu le **même** fichier. Quelques petites colonnes de plus
    valent mieux qu'un second parcours de bucket.

    `prefix` restreint le parcours à un dossier **et** filtre les lignes à ce
    dossier : sans ce filtre, les lignes des autres dossiers seraient déclarées
    « manquantes » simplement parce qu'on n'est pas allé voir chez elles.

    Ne supprime rien, ne répare rien : c'est un constat, l'appelant décide
    (`delete_objects`, `core/media_repair`, `scripts/reconcile_media.py`).
    """
    scope = prefix.strip("/")
    objects = iter_storage_paths(scope)
    rows = _read_media(RECONCILE_COLUMNS)
    known = {row.get("storage_path") for row in rows if row.get("storage_path")}
    present = set(objects)
    return {
        "bucket": BUCKET,
        "prefix": scope,
        "objects": len(objects),
        "rows": len(rows),
        "orphans": [path for path in objects if path not in known],
        "missing": [
            row
            for row in rows
            if row.get("storage_path")
            and row["storage_path"] not in present
            and _in_scope(row["storage_path"], scope)
        ],
    }


def list_orphan_objects(prefix: str = "") -> List[str]:
    """Objets du bucket **sans ligne** `knowledge_media` (orphelins).

    Un orphelin vient d'un upload interrompu avant l'insertion, d'un échec de
    nettoyage, ou d'une ligne supprimée côté base sans que l'objet ait pu
    l'être. Aucune suppression ici : on se contente de nommer, l'appelant décide
    (voir `delete_objects` et `scripts/reconcile_media.py`).

    Délègue à `reconcile_bucket`, qui rend les deux sens du **même** parcours :
    ceux qui veulent les deux d'un coup (la veille de l'auto-loop) l'appellent
    directement au lieu de payer deux balayages du bucket.
    """
    return reconcile_bucket(prefix)["orphans"]


def list_missing_objects(prefix: str = "") -> List[Dict[str, Any]]:
    """Lignes `knowledge_media` dont l'objet a disparu du bucket.

    Le **sens inverse** de `list_orphan_objects` : ici la description existe mais
    les octets manquent. Un média dans cet état casse tout ce qui le lit —
    `create_signed_url` rend un lien qui répond 404, et le tableau de bord
    affiche un fichier absent — sans qu'aucune autre lecture ne s'en aperçoive.

    On rend les **lignes entières** (et non leurs chemins comme pour les
    orphelins) : réparer demande de savoir *d'où* re-télécharger
    (`telegram_file_id`, `chat_id` + `message_id`), et cette information n'existe
    que dans la ligne. Les colonnes lues sont celles de `RECONCILE_COLUMNS` —
    de quoi nommer la ligne, la réparer et dire ce qui la rendra vérifiable
    (l'empreinte enregistrée à l'ingestion). Un bucket vide rend toutes les
    lignes : ce n'est pas une erreur, c'est le pire cas de la réconciliation.

    Délègue à `reconcile_bucket` (un seul parcours pour les deux sens).
    """
    return reconcile_bucket(prefix)["missing"]


def _in_scope(path: str, prefix: str) -> bool:
    """Vrai si `path` appartient au dossier demandé (`prefix` vide : tout).

    `iter_storage_paths(prefix)` ne parcourt que ce dossier : sans ce filtre, les
    lignes d'un autre dossier seraient déclarées « manquantes » simplement parce
    qu'on n'est pas allé voir chez elles.
    """
    folder = prefix.strip("/")
    return not folder or path.startswith(f"{folder}/")


def object_exists(storage_path: str) -> bool:
    """Vrai si l'objet est **déjà** dans le bucket (listing du dossier parent).

    `download_media` répondrait aussi, mais en ramenant tout le fichier : pour
    savoir s'il y a lieu de re-télécharger, un listing suffit — et c'est ce qui
    empêche une réparation d'écraser un original intact par une copie d'aperçu.
    """
    if not storage_path:
        return False
    folder, _, name = storage_path.rpartition("/")
    for entry in list_storage(folder):
        # Une entrée sans `id` est un sous-dossier, pas le fichier cherché.
        if entry.get("name") == name and entry.get("id"):
            return True
    return False


def restore_object(
    storage_path: str,
    data: bytes,
    *,
    mime_type: Optional[str] = None,
    upsert: bool = True,
) -> int:
    """Remet des octets dans le bucket, **sans toucher à la ligne**.

    Écrit uniquement dans Storage : `knowledge_media` n'est pas réécrite, et
    c'est tout l'intérêt. Rejouer `upload_media(upsert=True)` à la place
    remplacerait la ligne par la charge utile de l'upload — dont
    `metadata: {}` —, donc effacerait le verdict de revue, l'étiquette d'actif
    et la note d'extraction : réparer le fichier détruirait ce qu'on a mis des
    tours à enregistrer. Les morceaux indexés (`knowledge_chunks`) et leur texte
    restent eux aussi intacts : il n'y a rien à réextraire, seul l'objet avait
    disparu.

    Retourne le nombre d'octets déposés.
    """
    if not storage_path:
        raise ValueError("storage_path vide : aucun objet à restaurer")
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("data doit être des octets (bytes)")
    payload = bytes(data)
    _require_client().storage.from_(BUCKET).upload(
        storage_path,
        payload,
        {
            "content-type": mime_type or guess_mime_type(storage_path),
            "upsert": "true" if upsert else "false",
        },
    )
    return len(payload)


def delete_objects(paths: Sequence[str], *, batch_size: int = REMOVE_BATCH_SIZE) -> int:
    """Supprime des objets du bucket, par lots. Retourne le nombre visé.

    N'touche **pas** `knowledge_media` : par construction un orphelin n'a aucune
    ligne. Pour un média référencé, passer par `delete_media` (qui supprime la
    ligne et l'objet ensemble).
    """
    client = _require_client()
    targets = [path for path in paths if path]
    step = max(1, int(batch_size))
    for start in range(0, len(targets), step):
        client.storage.from_(BUCKET).remove(targets[start : start + step])
    return len(targets)


# --------------------------------------------------------------------------- #
# Morceaux de texte (`knowledge_chunks`) — délégations vers `knowledge_index`
# --------------------------------------------------------------------------- #
# La logique vit dans `database/knowledge_index.py` : ces trois fonctions ne
# font que déléguer, pour donner un point d'entrée ORIENTÉ MÉDIA (la durée de vie
# d'un média inclut son texte) sans dupliquer le découpage, les embeddings ou le
# RPC. Toute correction de la logique reste donc à un seul endroit.


def replace_media_chunks(media_id: str, text: str, **kwargs: Any) -> int:
    """Remplace les morceaux de texte d'un média. Retourne le nombre inséré.

    Idempotent : les morceaux existants du média sont supprimés avant insertion,
    donc rejouer une ingestion ne duplique rien. `**kwargs` est transmis tel quel
    à `knowledge_index.replace_chunks` (découpage, chevauchement, métadonnées,
    `embed=False` pour un test sans embeddings…).
    """
    return knowledge_index.replace_chunks(media_id, text, **kwargs)


def list_media_chunks(
    media_id: str, *, columns: Optional[Sequence[str]] = None
) -> List[Dict[str, Any]]:
    """Morceaux de texte d'un média, dans l'ordre du document.

    `columns` restreint la projection demandée à la base (voir
    `knowledge_index.list_chunks`) : relire un texte n'a pas besoin du vecteur de
    768 flottants qui accompagne chaque morceau. `None` les ramène tous.
    """
    return knowledge_index.list_chunks(media_id, columns=columns)


def search_chunks(query: str, **kwargs: Any) -> List[Dict[str, Any]]:
    """Recherche sémantique top-k par similarité cosinus (RPC Postgres).

    La requête est vectorisée (embeddings Gemini) puis comparée par
    `match_knowledge_chunks` : « support du bitcoin » retrouve « zone d'achat sous
    64k » sans aucun mot commun. Porte sur **tous** les morceaux — passer
    `source=knowledge_index.SOURCE_MEDIA` pour ne viser que les médias. Retourne
    `[]` si les embeddings sont indisponibles (aucune exception).
    """
    return knowledge_index.search_chunks(query, **kwargs)


#: Clé de `metadata` où vit le verdict de revue d'une extraction. Le verdict est
#: une **annotation**, pas une colonne : l'état qui compte pour le pipeline est
#: déjà porté par les morceaux (`knowledge_chunks`) — rejeté veut dire « il n'y a
#: plus de morceaux indexés ». Une colonne aurait imposé une migration pour un
#: champ que rien d'autre ne lit, et `metadata` est déjà prévue pour ça (elle
#: porte l'origine Telegram et l'album).
REVIEW_KEY = "extraction_review"

#: Verdicts acceptés. Volontairement fermés : une faute de frappe ne doit pas
#: créer un état que plus aucun filtre ne retrouverait.
REVIEW_STATUSES = ("validated", "rejected")


#: Clé de `metadata` où vit l'**actif** associé au média (voir `core.asset_tags`).
#: Une annotation, comme le verdict de revue : ce qui compte pour la recherche est
#: `knowledge_chunks.asset`, et `metadata` est déjà là pour ce genre de champ (elle
#: porte l'origine Telegram et l'album). Aucune migration à jouer.
ASSET_KEY = "asset"


def _merge_metadata(client: Any, media_id: str, **fields: Any) -> Dict[str, Any]:
    """Fusionne des clés dans `metadata` d'un média et retourne la ligne écrite.

    Lecture-modification-écriture : écrire `metadata` en entier écraserait les
    clés des autres modules (origine Telegram, album, verdict de revue). Une
    valeur `None` **retire** la clé — c'est ainsi qu'on efface un étiquetage.
    """
    response = (
        client.table(TABLE).select("*").eq("id", media_id).limit(1).execute()
    )
    rows = response.data or []
    if not rows:
        raise ValueError(f"média introuvable : {media_id}")
    row = rows[0]
    metadata = dict(row.get("metadata") or {})
    for key, value in fields.items():
        if value is None:
            metadata.pop(key, None)
        else:
            metadata[key] = value
    written = client.table(TABLE).update({"metadata": metadata}).eq("id", media_id).execute()
    written_rows = written.data or []
    return written_rows[0] if written_rows else {**row, "metadata": metadata}


#: Clé de `metadata` où vit le **résultat** de l'extraction (`ok`, `method`,
#: `chars`, `chunks`, `reason`). Distincte de `REVIEW_KEY`, et pour une raison de
#: fond : `extraction_review` est un **verdict humain**, ceci est ce que la
#: machine a produit. Les confondre effacerait la seule trace qui dit qu'une
#: extraction a échoué sans qu'aucun humain ne l'ait relue.
OUTCOME_KEY = "extraction"

#: Champs du compte-rendu d'extraction conservés sur la ligne. Le **texte** n'en
#: fait pas partie : il est déjà indexé, et `metadata` n'est pas un endroit où
#: garder des dizaines de kilo-octets.
OUTCOME_FIELDS = ("ok", "method", "chars", "chunks", "reason")


def set_extraction_outcome(media_id: str, summary: Dict[str, Any]) -> Dict[str, Any]:
    """Enregistre le **résultat** de l'extraction sur la ligne média.

    Pourquoi le garder : sans lui, un média dont l'extraction a échoué (clef
    absente, PDF scanné, modèle sans poids) est **indiscernable** d'un média
    correctement lu — dès qu'il y a une légende, les deux ont des morceaux
    indexés. Le compte-rendu d'ingestion disait lequel, mais il a défilé, et
    plus rien ne permet ensuite de savoir quoi rattraper, ni pourquoi.

    Le motif est conservé **tel quel** (« GEMINI_API_KEY absente : vision
    indisponible ») : c'est lui qui dit quoi réparer avant de relancer.
    """
    outcome: Dict[str, Any] = {
        field: summary.get(field) for field in OUTCOME_FIELDS
    }
    outcome["ok"] = bool(summary.get("ok"))
    outcome["at"] = datetime.now(timezone.utc).isoformat()
    return _merge_metadata(_require_client(), media_id, **{OUTCOME_KEY: outcome})


def extraction_outcome(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Résultat de la dernière extraction d'un média, ou `None` (jamais enregistré)."""
    if not row:
        return None
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None
    outcome = metadata.get(OUTCOME_KEY)
    return outcome if isinstance(outcome, dict) else None


def content_was_extracted(row: Optional[Dict[str, Any]]) -> Optional[bool]:
    """Le **contenu** du média a-t-il été extrait ? `None` si on ne peut pas le dire.

    Trois réponses, et le `None` compte : une extraction à laquelle il manquait sa
    clef laisse quand même la légende indexée, donc « des morceaux existent » ne
    répond pas à la question. Sans résultat enregistré (média antérieur à ce
    champ), on ne devine pas — c'est à l'appelant de décider quoi faire d'une
    ignorance, pas à cette fonction de la maquiller.
    """
    outcome = extraction_outcome(row)
    if outcome is None:
        return None
    return bool(outcome.get("ok"))


def review_status(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """Verdict de revue d'une ligne média, ou `None` (jamais relu)."""
    if not row:
        return None
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None
    review = metadata.get(REVIEW_KEY) or {}
    if not isinstance(review, dict):
        return None
    status = review.get("status")
    return status if status in REVIEW_STATUSES else None


def asset_tag(row: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """`(actif, provenance)` d'un média — provenance `caption`, `extraction` ou `manual`.

    La provenance est conservée telle quelle : elle distingue une détection
    automatique d'un choix de l'utilisateur, seule information qui dit si une
    nouvelle détection a le droit d'écraser l'étiquette.
    """
    if not row:
        return None, None
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        return None, None
    value = metadata.get(ASSET_KEY)
    source = None
    if isinstance(value, dict):  # forme tracée : {"value": ..., "source": ...}
        source = value.get("source")
        value = value.get("value")
    text = str(value or "").strip()
    return (text or None), (str(source) if source else None)


def media_asset(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """Actif associé à un média, ou `None` (média non étiqueté — le joker du filtre)."""
    return asset_tag(row)[0]


def set_media_asset(
    media_id: str,
    asset: Optional[str],
    *,
    source: Optional[str] = None,
    reviewer: Optional[str] = None,
) -> Dict[str, Any]:
    """Associe un actif à un média (ou l'efface avec `asset=None`).

    `source` trace **d'où** vient l'étiquette (`caption`, `extraction`, `manual`) :
    sans elle, impossible de distinguer plus tard une détection automatique d'un
    choix de l'utilisateur, et donc de savoir ce qu'on peut corriger en cas de
    nouvelle détection. `reviewer` (compte Telegram) n'est enregistré que si
    quelqu'un a étiqueté à la main — comme le `by` d'un verdict de revue.
    Retourne la ligne mise à jour (`updated_at` est posé par le trigger).
    """
    clean = str(asset or "").strip() or None
    payload: Optional[Dict[str, Any]] = None
    if clean:
        payload = {"value": clean, "source": source}
        if reviewer:
            payload["by"] = str(reviewer)
    return _merge_metadata(_require_client(), media_id, **{ASSET_KEY: payload})


def set_review_status(
    media_id: str,
    status: str,
    *,
    reviewer: Optional[str] = None,
    chunks: Optional[int] = None,
) -> Dict[str, Any]:
    """Enregistre le verdict humain sur l'extraction d'un média.

    Retourne la ligne mise à jour ; `updated_at` est posé par le trigger.
    """
    if status not in REVIEW_STATUSES:
        raise ValueError(
            f"verdict inconnu : {status!r} (attendu : {', '.join(REVIEW_STATUSES)})"
        )
    review: Dict[str, Any] = {
        "status": status,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if reviewer:
        review["by"] = str(reviewer)
    if chunks is not None:
        review["chunks"] = int(chunks)
    return _merge_metadata(_require_client(), media_id, **{REVIEW_KEY: review})


def create_signed_url(storage_path: str, expires_in: int = 3600) -> Optional[str]:
    """Lien temporaire vers un média privé.

    `expires_in` est en secondes. Le résultat n'expose rien publiquement : le
    lien expire, et le bucket reste inaccessible sans lui.
    """
    response = _require_client().storage.from_(BUCKET).create_signed_url(
        storage_path, int(expires_in)
    )
    if isinstance(response, dict):
        return (
            response.get("signedURL")
            or response.get("signedUrl")
            or response.get("signed_url")
        )
    return getattr(response, "signedURL", None) or getattr(response, "signed_url", None)
