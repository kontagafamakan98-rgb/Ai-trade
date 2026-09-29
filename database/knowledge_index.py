"""Index texte + vectoriel de la base de connaissances (`knowledge_chunks`).

La migration `008_telegram_media.sql` a créé `knowledge_chunks` pour les médias ;
la migration `009_knowledge_vectors.sql` l'a ouverte aux notes de
`knowledge_base` (`media_id` nullable, colonnes `source`/`note_source`) et a
ajouté la fonction de recherche `match_knowledge_chunks`.

Ce module gère :

* l'**indexation** d'un document (média ou note) : découpe en morceaux, appel des
  embeddings Gemini (`ai/embeddings.py`), écriture des lignes ;
* la **recherche** top-k filtrée par source, actif et régime ;
* la **recherche de médias** pertinents pour un actif
  (`search_media_for_asset`) : les morceaux remontés sont regroupés par média ;
* la **ré-vectorisation** des morceaux déjà en base (`reembed_chunks`) : changer
  de modèle ou de fournisseur d'embeddings change l'espace vectoriel, donc les
  anciens vecteurs doivent être recalculés — jamais mélangés à des vecteurs
  d'un autre modèle, dont la comparaison n'aurait aucun sens.

L'embedding est **best-effort** : sans clé Gemini (ou en cas de panne), les
morceaux sont écrits avec un `embedding` NULL — le texte reste cherchable plus
tard, et l'appelant retombe sur l'extrait fixe. Une écriture muette serait pire
qu'une absence d'embedding : on n'échoue donc jamais à cause du vecteur.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from database.supabase_client import supabase

#: Table créée par la migration 008, étendue par la 009.
TABLE = "knowledge_chunks"

#: Fonction SQL de recherche (migration 009).
MATCH_FUNCTION = "match_knowledge_chunks"

#: Table des médias (migration 008), jointe pour décrire chaque résultat.
MEDIA_TABLE = "knowledge_media"

#: Requête par défaut de `search_media_for_asset` quand aucune question n'est
#: fournie. On ne cherche pas un mot-clé mais le **thème** d'un actif : c'est le
#: sens même d'une recherche sémantique. Le corpus pouvant être multilingue,
#: Gemini fait le rapprochement entre les langues.
DEFAULT_ASSET_QUERY = (
    "{asset} analyse graphique, tendance, niveaux de support et résistance, "
    "indicateurs techniques"
)

#: Provenance d'un morceau : un média Telegram, ou une note de `knowledge_base`.
SOURCE_MEDIA = "telegram"
SOURCE_NOTE = "note"

#: Taille de morceau par défaut, en caractères. ~1500 caractères ≈ 375 tokens :
#: assez de contexte pour une recherche pertinente, assez petit pour que
#: l'embedding reste dans les limites du modèle.
DEFAULT_CHUNK_CHARS = 1500

#: Chevauchement entre deux morceaux : évite de couper une idée à la jointure.
DEFAULT_OVERLAP_CHARS = 200

#: Nombre de morceaux renvoyés par défaut pour une recherche.
DEFAULT_TOP_K = 5

#: Taille de page des lectures groupées de morceaux (`chunk_counts`) : la réponse
#: de PostgREST est bornée, et les morceaux d'une page de médias peuvent la
#: dépasser — une lecture tronquée sous-compterait, sans rien dire.
COUNT_PAGE = 1000


def _require_client() -> Any:
    """Refuse d'agir sans client configuré (même règle que `media_store`)."""
    if supabase is None:
        raise RuntimeError(
            "Supabase n'est pas configuré : renseigne SUPABASE_URL et "
            "SUPABASE_SERVICE_KEY (voir .env.example)."
        )
    return supabase


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_OVERLAP_CHARS,
) -> List[str]:
    """Découpe un texte en morceaux qui ne coupent pas les mots.

    Le texte est d'abord normalisé (espaces multiples réduits) puis découpé en
    fenêtres de `max_chars` se chevauchant de `overlap`. Les frontières — début
    comme fin — sont alignées sur des espaces pour ne jamais trancher un mot.

    Retourne une liste vide pour un texte vide — jamais `[""]`, qui créerait une
    ligne sans contenu.
    """
    if max_chars <= 0:
        raise ValueError("max_chars doit être strictement positif")
    normalized = " ".join((text or "").split())
    if not normalized:
        return []

    overlap = max(0, min(overlap, max_chars - 1))
    chunks: List[str] = []
    start = 0
    length = len(normalized)

    while start < length:
        end = min(start + max_chars, length)
        if end < length:
            space = normalized.rfind(" ", start, end)
            if space > start:
                end = space
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break
        next_start = max(end - overlap, start + 1)
        # Avance aussi jusqu'au prochain mot : un chevauchement ne doit pas
        # laisser un morceau commencer au milieu d'un mot.
        if next_start < length and normalized[next_start - 1] != " ":
            space = normalized.find(" ", next_start)
            if space != -1:
                next_start = space + 1
        start = next_start

    return chunks


def replace_chunks(
    media_id: Optional[str],
    text: str,
    *,
    note_source: Optional[str] = None,
    source: Optional[str] = None,
    asset: Optional[str] = None,
    regime: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_OVERLAP_CHARS,
    embed: bool = True,
) -> int:
    """Remplace les morceaux d'un document et retourne le nombre inséré.

    Le document est identifié par `media_id` (média Telegram) **ou** par
    `note_source` (note de `knowledge_base`) — jamais les deux. Idempotent : les
    morceaux existants de ce document sont supprimés avant insertion, donc
    rejouer une ingestion ou une note ne duplique rien.

    `embed=False` saute l'appel d'embeddings (utile pour les tests et pour une
    réindexation purement textuelle).
    """
    client = _require_client()
    if not media_id and not note_source:
        raise ValueError("replace_chunks exige media_id ou note_source")
    resolved_source = source or (SOURCE_NOTE if note_source else SOURCE_MEDIA)

    delete = client.table(TABLE).delete()
    delete = delete.eq("media_id", media_id) if media_id else delete.eq("note_source", note_source)
    delete.execute()

    chunks = chunk_text(text, max_chars=max_chars, overlap=overlap)
    if not chunks:
        return 0

    embeddings, model_name = _embed_passages(chunks) if embed else (None, None)

    rows: List[Dict[str, Any]] = []
    for index, content in enumerate(chunks):
        row: Dict[str, Any] = {
            "media_id": media_id,
            "note_source": note_source,
            "chunk_index": index,
            "content": content,
            "source": resolved_source,
            "asset": asset,
            "regime": regime,
            "token_count": _approx_tokens(content),
            "metadata": metadata or {},
        }
        if embeddings is not None:
            row["embedding"] = embeddings[index]
            row["embedding_model"] = model_name
        rows.append(row)

    client.table(TABLE).insert(rows).execute()
    return len(rows)


def search_chunks(
    query: str,
    *,
    asset: Optional[str] = None,
    regime: Optional[str] = None,
    source: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
) -> List[Dict[str, Any]]:
    """Recherche vectorielle top-k, filtrée par source, actif et régime.

    Les filtres `asset`/`regime` sont **préférentiels** : un morceau non étiqueté
    reste candidat (voir `match_knowledge_chunks`), mais les correspondances
    explicites remontent en tête. Retourne `[]` si les embeddings sont
    indisponibles — l'appelant décide alors du repli.
    """
    text = (query or "").strip()
    if not text:
        return []
    vector = _embed_query(text)
    if vector is None:
        return []

    response = (
        _require_client()
        .rpc(
            MATCH_FUNCTION,
            {
                "query_embedding": vector,
                "match_count": max(1, int(top_k)),
                "filter_source": source,
                "filter_asset": asset,
                "filter_regime": regime,
            },
        )
        .execute()
    )
    return response.data or []


def search_media_for_asset(
    asset: str,
    *,
    query: Optional[str] = None,
    regime: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
    chunk_top_k: Optional[int] = None,
    min_similarity: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Médias les plus pertinents pour un actif, par similarité sémantique.

    `query` permet de préciser la recherche (« cassure des 100k »), mais reste
    facultatif : sans elle, on cherche le *thème* de l'actif
    (`DEFAULT_ASSET_QUERY`).

    On remonte plus de morceaux que de médias demandés — un même média peut en
    fournir plusieurs — puis chaque média est représenté par son **meilleur**
    morceau. `chunk_count` indique combien de ses morceaux figurent dans le
    résultat (et non combien il en possède en base).

    `min_similarity` écarte les correspondances faibles **avant** de couper au
    `top_k` : mieux vaut moins de résultats que du bruit, notamment quand cette
    recherche alimente un prompt (voir `knowledge_base.get_media_context`).

    Une **étiquette d'actif explicite** (`set_chunks_asset`, commande `/tag`)
    n'est pas traitée comme une similarité de plus : un document que l'utilisateur
    a désigné comme parlant de cet actif est pertinent par construction, donc il
    passe le seuil mesuré même si son texte — court, ou sans le vocabulaire de
    l'actif — le place sous le bruit du corpus. C'est le seul cas où le seuil est
    ignoré, et il reste borné par `TAGGED_HARD_FLOOR` : une étiquette posée par
    erreur sur un contenu sans aucun rapport ne doit pas pour autant injecter
    n'importe quoi dans un prompt. Les médias étiquetés sont aussi **classés en
    tête**, étiquette d'abord puis similarité.

    Retourne `[]` si les embeddings sont indisponibles ou si `asset` est vide :
    aucune exception, comme `search_chunks`.
    """
    target = (asset or "").strip()
    if not target:
        return []

    text = (query or "").strip() or DEFAULT_ASSET_QUERY.format(asset=target)
    wanted = max(1, int(top_k))
    chunks = search_chunks(
        text,
        asset=target,
        regime=regime,
        source=SOURCE_MEDIA,
        top_k=max(wanted, int(chunk_top_k or wanted * 3)),
    )
    if not chunks:
        return []

    ranked = select_media_candidates(
        chunks, target=target, top_k=wanted, min_similarity=min_similarity
    )
    if not ranked:
        return []
    media = _fetch_media([item["media_id"] for item in ranked])
    return [{**media.get(item["media_id"], {}), **item} for item in ranked]


def select_media_candidates(
    chunks: Sequence[Dict[str, Any]],
    *,
    target: str,
    top_k: int = DEFAULT_TOP_K,
    min_similarity: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Règle de sélection des extraits de médias, à partir des morceaux trouvés.

    Fonction **pure** (aucun accès base) : c'est ici que vit la politique — un
    média = son meilleur morceau, une étiquette d'actif explicite passe le seuil,
    l'étiquette prime sur la similarité au classement. L'isoler de l'entrée/sortie
    permet de la tester et de la verrouiller dans un golden sans base ni doublure.

    Un média est représenté par son **meilleur** morceau ; `chunk_count` dit
    combien de ses morceaux figurent dans les candidats. `tagged` est vrai dès
    qu'**un** de ses morceaux porte l'actif demandé : l'étiquette s'applique au
    média entier (`set_chunks_asset`), un morceau resté sans actif ne doit pas
    annuler ceux qui en portent un.

    `min_similarity` écarte les correspondances faibles **avant** de couper au
    `top_k` — sauf pour un média étiqueté, qui reste borné par `TAGGED_HARD_FLOOR`
    (voir `search_media_for_asset`).
    """
    best: Dict[str, Dict[str, Any]] = {}
    for chunk in chunks or []:
        media_id = chunk.get("media_id")
        if not media_id:
            continue
        similarity = float(chunk.get("similarity") or 0.0)
        tagged = str(chunk.get("asset") or "").strip() == target
        entry = best.get(str(media_id))
        if entry is None:
            best[str(media_id)] = {
                "media_id": str(media_id),
                "similarity": similarity,
                "matched_chunk": chunk.get("content"),
                "chunk_count": 1,
                "tagged": tagged,
            }
        else:
            entry["chunk_count"] += 1
            entry["tagged"] = entry["tagged"] or tagged
            if similarity > entry["similarity"]:
                entry["similarity"] = similarity
                entry["matched_chunk"] = chunk.get("content")

    # L'étiquette passe devant la similarité : la fonction SQL remontait déjà les
    # correspondances explicites en tête (`order by kc.asset = filter_asset desc`),
    # mais un tri sur la seule similarité annulait ce classement ici — l'étiquette
    # ne servait donc qu'au filtrage, jamais au choix des extraits retenus.
    ranked = sorted(
        best.values(), key=lambda item: (item["tagged"], item["similarity"]), reverse=True
    )
    if min_similarity is not None:
        floor = float(min_similarity)
        ranked = [
            item
            for item in ranked
            if item["similarity"] >= floor
            or (item["tagged"] and item["similarity"] >= TAGGED_HARD_FLOOR)
        ]
    return ranked[: max(1, int(top_k))]


def _fetch_media(media_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """Descriptions des médias indexées par identifiant.

    Métadonnées d'agrément : si la table est illisible, on rend quand même les
    morceaux trouvés (l'appelant perd le nom du fichier, pas le résultat).
    """
    if not media_ids:
        return {}
    try:
        response = (
            _require_client().table(MEDIA_TABLE).select("*").in_("id", list(media_ids)).execute()
        )
    except Exception as exc:
        print(f"   [knowledge] medias illisibles : {type(exc).__name__}: {exc}")
        return {}
    return {str(row.get("id")): row for row in (response.data or [])}


def reembed_chunks(
    *,
    model: Optional[str] = None,
    only_stale: bool = True,
    page_size: int = 100,
    max_rows: Optional[int] = None,
    dry_run: bool = False,
) -> int:
    """Recalcule les vecteurs des morceaux déjà stockés, sans re-téléchargement.

    Le `content` est en base : ré-indexer ne demande donc aucun média d'origine.
    C'est la seule façon correcte de changer de modèle ou de fournisseur
    d'embeddings — des vecteurs de deux modèles dans la même colonne produiraient
    des similarités dénuées de sens.

    `only_stale=False` retraite tout, y compris les vecteurs déjà au bon modèle.
    `max_rows` borne le travail (un lot à la fois pour un rattrapage progressif),
    et `dry_run` se contente de compter. Retourne le nombre de morceaux traités.
    """
    client = _require_client()
    model_name = model or _current_model()
    rows = _rows_to_reembed(
        client,
        model_name,
        only_stale=only_stale,
        page_size=page_size,
        max_rows=max_rows,
    )
    if dry_run or not rows:
        return len(rows)

    updated = 0
    for start in range(0, len(rows), page_size):
        batch = rows[start : start + page_size]
        vectors, embedded_with = _embed_passages([row.get("content") or "" for row in batch])
        if not vectors:
            print("   [embeddings] indisponibles : re-vectorisation interrompue")
            break
        for row, vector in zip(batch, vectors):
            (
                client.table(TABLE)
                .update({"embedding": vector, "embedding_model": embedded_with})
                .eq("id", row.get("id"))
                .execute()
            )
            updated += 1
    return updated


def _rows_to_reembed(
    client: Any,
    model_name: str,
    *,
    only_stale: bool,
    page_size: int,
    max_rows: Optional[int],
) -> List[Dict[str, Any]]:
    """Morceaux à (re)vectoriser — lecture paginée, sans écriture.

    On lit tout **avant** d'écrire : mettre à jour au fil des pages ferait
    rétrécir le filtre, et l'offset sauterait des lignes.
    """
    page_size = max(1, int(page_size))
    rows: List[Dict[str, Any]] = []
    offset = 0
    while True:
        query = client.table(TABLE).select("id, content, embedding_model")
        if only_stale:
            # `embedding.is.null` couvre les morceaux jamais vectorisés (ou dont
            # l'appel d'embedding a échoué) ; `neq` ceux d'un autre modèle.
            query = query.or_(f"embedding.is.null,embedding_model.neq.{model_name}")
        page = (
            query.order("id").range(offset, offset + page_size - 1).execute().data or []
        )
        rows.extend(page)
        if max_rows is not None and len(rows) >= max_rows:
            return rows[: max(1, int(max_rows))]
        if len(page) < page_size:
            return rows
        offset += page_size


def delete_chunks(media_id: str) -> int:
    """Supprime les morceaux indexés d'un média, et retourne combien ont disparu.

    Sert au **rejet d'une extraction** (bouton Telegram) : les morceaux d'un
    média mal lu (une vision qui a décrit autre chose que le graphique, une
    transcription dans la mauvaise langue) ne doivent plus remonter dans les
    prompts ni dans `/search`. Reconnaître une extraction ratée sans pouvoir la
    retirer laisserait le bruit en place, avec seulement un commentaire.

    Le compte exige une lecture préalable : PostgREST ne renvoie pas le nombre de
    lignes supprimées, et « 3 morceaux retirés » est justement ce que l'utilisateur
    doit voir pour savoir que le clic a fait quelque chose. `replace_chunks`
    n'utilise volontairement pas cette fonction : réindexer n'a pas besoin du
    compte, et une lecture de plus par document coûterait cher sur un lot.
    """
    client = _require_client()
    existing = client.table(TABLE).select("id").eq("media_id", media_id).execute()
    count = len(existing.data or [])
    if count:
        client.table(TABLE).delete().eq("media_id", media_id).execute()
    return count


def set_chunks_asset(media_id: str, asset: Optional[str]) -> int:
    """Étiquette (ou dé-étiquette) les morceaux d'un média ; retourne combien.

    C'est **le** champ que lit `match_knowledge_chunks` : étiqueter des morceaux
    déjà indexés suffit donc à faire jouer le filtre préférentiel sur un média,
    sans réextraction ni re-vectorisation — `asset` ne participe pas au vecteur,
    le `content` ne change pas, seules les égalités de filtre changent.

    `asset=None` restaure le **joker** : un morceau non étiqueté reste candidat
    pour n'importe quel actif (sans bonus de classement). Indispensable pour
    corriger une étiquette fausse — un actif faux, lui, *exclut* le média des
    recherches de l'actif réel.

    Le compte exige une lecture préalable (comme `delete_chunks`) : PostgREST ne
    renvoie pas les lignes touchées, et « 3 morceaux étiquetés » est ce que
    l'utilisateur doit voir pour savoir que son `/tag` a fait quelque chose.
    """
    client = _require_client()
    existing = client.table(TABLE).select("id").eq("media_id", media_id).execute()
    count = len(existing.data or [])
    if count:
        client.table(TABLE).update({"asset": asset}).eq("media_id", media_id).execute()
    return count


# --------------------------------------------------------------------------- #
# Seuil de similarité calibré sur la base
# --------------------------------------------------------------------------- #
# Pourquoi mesurer au lieu de fixer une constante : la similarité cosinus n'a pas
# d'échelle absolue. Un corpus de quelques dizaines de documents traitant tous de
# trading obtient 0,65 de similarité entre deux morceaux **sans rapport** (même
# vocabulaire, même modèle, même dimension 768 tronquée) ; un corpus vaste et
# hétérogène tombe à 0,3. Le seuil « 0,5 » qui allait bien à l'un laisse donc
# passer du bruit sur le premier et jette les bons extraits du second. Le seul
# chiffre défendable est celui mesuré **sur la base** : à quelle hauteur se
# situent les similarités entre une question qui ne la concerne **pas** et ses
# documents.
#
# La mesure est faite en tâche `RETRIEVAL_QUERY`, la même que les vraies
# recherches : une estimation faite document-à-document ne serait pas dans le même
# espace (le modèle distingue les deux tâches, et leurs échelles diffèrent).

#: Requêtes **délibérément hors sujet** pour un corpus de trading : elles mesurent
#: le plancher de bruit de cette base — taille, langue, homogénéité du domaine et
#: modèle d'embeddings compris. Volontairement sans rapport entre elles : si elles
#: se ressemblaient, elles mesureraient leur propre voisinage.
NOISE_PROBE_QUERIES = (
    "recette de tarte aux pommes pour six personnes",
    "itinéraire de randonnée en montagne avec dénivelé",
    "résumé du match de football d'hier soir",
    "liste de courses pour la semaine",
)

#: Percentile du bruit retenu : une correspondance doit dépasser presque toutes les
#: paires **hors sujet** de la base, pas seulement la moyenne (deux documents sans
#: rapport peuvent se ressembler par hasard, une seule paire ne doit pas fixer le
#: seuil).
NOISE_PERCENTILE = 95.0

#: Marge ajoutée à ce percentile : dépasser le bruit de justesse n'est pas un
#: signe de pertinence.
NOISE_MARGIN = 0.05

#: Bornes du seuil. En dessous, on injecterait du bruit ; au-dessus, plus rien ne
#: passerait jamais — et une base si homogène que son bruit dépasse 0,9 n'a
#: effectivement **rien** à apporter, ce que le bloc vide traduit correctement.
FLOOR_BOUNDS = (0.2, 0.9)

#: Plancher opposé à une **étiquette d'actif explicite** : le seuil mesuré ne
#: s'applique pas à un média que l'utilisateur a désigné comme parlant de cet actif
#: (voir `search_media_for_asset`), mais aucun seuil ne descend sous
#: `FLOOR_BOUNDS[0]` — c'est la limite en dessous de laquelle la calibration
#: elle-même refuse de descendre, donc le seul chiffre qui exprime « sans aucun
#: rapport » quelle que soit la base. Une étiquette posée par erreur sur un contenu
#: étranger à l'actif reste ainsi écartée.
TAGGED_HARD_FLOOR = FLOOR_BOUNDS[0]

#: Documents échantillonnés (un morceau par média : des morceaux d'un **même**
#: document se chevauchent, donc leurs similarités mesureraient la redondance
#: interne et non le bruit entre documents).
CALIBRATION_DOCS = 40

#: En dessous, l'échantillon ne couvre pas le vocabulaire de la base : son bruit
#: est sous-estimé, donc le seuil serait trop bas et laisserait passer du bruit —
#: on préfère alors le repli conservateur.
CALIBRATION_MIN_DOCS = 5

#: Durée de validité d'une mesure, en secondes. La base change à chaque ingestion,
#: mais la mesure coûte un appel d'embeddings par sonde : un quart d'heure borne
#: les deux.
CALIBRATION_TTL_SECONDS = 900

_CALIBRATION_LOCK = threading.Lock()
_CALIBRATION_CACHE: Optional[Dict[str, Any]] = None
_CALIBRATION_AT = 0.0


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Percentile par interpolation linéaire (méthode inclusive, comme numpy).

    `None` sur une série vide : il n'y a pas de "milieu" d'une distribution vide,
    et rendre 0 ferait croire à une mesure.
    """
    numbers = sorted(float(value) for value in values)
    if not numbers:
        return None
    ratio = min(max(float(q), 0.0), 100.0) / 100.0
    rank = ratio * (len(numbers) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return numbers[int(rank)]
    weight = rank - lower
    return numbers[lower] * (1 - weight) + numbers[upper] * weight


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Similarité cosinus de deux vecteurs, ou `0.0` si l'un est nul.

    C'est exactement ce que calcule l'opérateur `<=>` de pgvector côté SQL : la
    mesure doit être dans la même unité que le seuil qu'elle fixe. Les vecteurs
    stockés sont normalisés (MRL), mais la fonction ne le suppose pas.
    """
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))


def noise_distribution(
    document_vectors: Sequence[Sequence[float]],
    probe_vectors: Sequence[Sequence[float]],
) -> Optional[Dict[str, Any]]:
    """Distribution des similarités « question hors sujet × document » de la base.

    Fonction **pure** : elle ne lit ni la base ni le réseau, ce qui permet de la
    vérifier sur des vecteurs construits à la main (et de figer son résultat dans
    un golden). `None` si l'un des deux côtés est vide — une distribution sans
    paire n'existe pas.
    """
    if not document_vectors or not probe_vectors:
        return None
    scores = [
        cosine_similarity(probe, document)
        for probe in probe_vectors
        for document in document_vectors
    ]
    return {
        "pairs": len(scores),
        "documents": len(document_vectors),
        "probes": len(probe_vectors),
        # Les scores bruts sont conservés : le seuil est un **percentile choisi**
        # de cette distribution, donc le changer doit changer le résultat, pas
        # seulement le chiffre affiché à côté.
        "scores": scores,
        "min": min(scores),
        "median": percentile(scores, 50),
        "p75": percentile(scores, 75),
        "p90": percentile(scores, 90),
        "p95": percentile(scores, 95),
        "max": max(scores),
    }


def similarity_floor(
    distribution: Optional[Dict[str, Any]],
    *,
    percentile_value: float = NOISE_PERCENTILE,
    margin: float = NOISE_MARGIN,
    bounds: Tuple[float, float] = FLOOR_BOUNDS,
) -> Optional[float]:
    """Seuil déduit de la distribution du bruit, borné, ou `None`.

    Le percentile visé est recalculé depuis les scores bruts afin que changer
    `NOISE_PERCENTILE` change effectivement le seuil (et pas seulement le chiffre
    affiché) : `similarity_floor(distribution, percentile_value=99)` doit rendre
    une valeur plus haute.
    """
    if not distribution or not distribution.get("pairs"):
        return None
    scores = distribution.get("scores") or []
    base = percentile(scores, percentile_value)
    if base is None:
        return None
    low, high = bounds
    return min(max(float(base) + float(margin), float(low)), float(high))


def sample_document_vectors(
    *,
    source: str = SOURCE_MEDIA,
    limit: int = CALIBRATION_DOCS,
    client: Any = None,
) -> List[List[float]]:
    """Vecteurs d'au plus `limit` documents de la base, **un par média**.

    Lecture paginée et bornée : la table peut contenir des morceaux sans vecteur
    (embeddings indisponibles au moment de l'indexation), qui sont simplement
    ignorés — le tri par `chunk_index` fait remonter le premier morceau de chaque
    média, celui qui porte la légende ou le titre.
    """
    wanted = max(1, int(limit))
    reader = client if client is not None else _require_client()
    page_size = max(wanted, 20)
    vectors: List[List[float]] = []
    seen: set = set()
    offset = 0
    while len(vectors) < wanted and offset < page_size * 4:
        page = (
            reader.table(TABLE)
            .select("media_id,embedding")
            .eq("source", source)
            .order("chunk_index")
            .order("media_id")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
            or []
        )
        for row in page:
            media_id = str(row.get("media_id") or "")
            embedding = row.get("embedding")
            if not media_id or media_id in seen or not embedding:
                continue
            seen.add(media_id)
            vectors.append(list(embedding))
            if len(vectors) >= wanted:
                break
        if len(page) < page_size:
            break
        offset += page_size
    return vectors


def calibration(
    *,
    force: bool = False,
    ttl: float = CALIBRATION_TTL_SECONDS,
    limit: int = CALIBRATION_DOCS,
    probes: Sequence[str] = NOISE_PROBE_QUERIES,
    sample: Optional[Callable[..., List[List[float]]]] = None,
    embed: Optional[Callable[..., Optional[List[List[float]]]]] = None,
    now: Optional[Callable[[], float]] = None,
) -> Dict[str, Any]:
    """Seuil de similarité mesuré sur la base, mémorisé `ttl` secondes.

    Ne lève jamais : chaque étape peut rendre la mesure impossible (base non
    configurée, aucun vecteur, embeddings indisponibles), et l'appelant a un repli
    — la calibration est une amélioration, pas une dépendance.

    Le cache est volontairement **court et global** : la mesure coûte un appel
    d'embeddings par sonde, mais la base change à chaque ingestion, et un seuil
    figé pour la session finirait par décrire un corpus qui n'existe plus.
    """
    global _CALIBRATION_CACHE, _CALIBRATION_AT
    clock = now or time.monotonic
    with _CALIBRATION_LOCK:
        stamp = clock()
        if (
            not force
            and _CALIBRATION_CACHE is not None
            and stamp - _CALIBRATION_AT < ttl
        ):
            return {**_CALIBRATION_CACHE, "cached": True, "age": stamp - _CALIBRATION_AT}

    # Les échecs sont mémorisés comme les succès : le coût est le même (des appels
    # d'embeddings), et sans ça un embedding manquant se paierait à **chaque**
    # analyse. Le repli étant le comportement sûr, attendre la fin du TTL pour
    # réessayer ne coûte rien.
    outcome = {
        **_measure(documents_sample=sample, embed_probes=embed, limit=limit, probes=probes),
        "cached": False,
        "age": 0.0,
    }
    with _CALIBRATION_LOCK:
        _CALIBRATION_CACHE = outcome
        _CALIBRATION_AT = clock()
    return outcome


def _measure(
    *,
    documents_sample: Optional[Callable[..., List[List[float]]]],
    embed_probes: Optional[Callable[..., Optional[List[List[float]]]]],
    limit: int,
    probes: Sequence[str],
) -> Dict[str, Any]:
    """Une mesure complète (sans cache) : échantillon, sondes, statistiques."""
    try:
        vectors = (
            documents_sample(source=SOURCE_MEDIA, limit=limit)
            if documents_sample is not None
            else sample_document_vectors(limit=limit)
        )
    except Exception as exc:
        return _failed("base inaccessible", error=f"{type(exc).__name__}: {exc}")

    if len(vectors) < CALIBRATION_MIN_DOCS:
        return _failed(
            "trop peu de documents vectorisés",
            documents=len(vectors),
            minimum=CALIBRATION_MIN_DOCS,
        )

    try:
        probe_vectors = (
            embed_probes(list(probes)) if embed_probes is not None else _embed_probes(probes)
        )
    except Exception as exc:
        return _failed("embedding des sondes impossible", error=f"{type(exc).__name__}: {exc}")
    if not probe_vectors:
        return _failed("embeddings indisponibles", documents=len(vectors))

    distribution = noise_distribution(vectors, probe_vectors) or {}
    floor = similarity_floor(distribution)
    if floor is None:
        return _failed("distribution vide", documents=len(vectors))

    noise = percentile(distribution["scores"], NOISE_PERCENTILE)
    print(
        f"   [similarite] seuil calibre sur la base : {floor:.3f} "
        f"(bruit p{NOISE_PERCENTILE:g} {noise:.3f} + marge {NOISE_MARGIN:g}, "
        f"{distribution['documents']} documents / {distribution['pairs']} paires)"
    )
    return {
        "ok": True,
        "reason": None,
        "floor": floor,
        "noise": noise,
        **distribution,
    }


def _failed(reason: str, **extra: Any) -> Dict[str, Any]:
    print(f"   [similarite] seuil non calibre ({reason}) : repli sur la constante")
    return {"ok": False, "reason": reason, "floor": None, **extra}


def _embed_probes(probes: Sequence[str]) -> Optional[List[List[float]]]:
    """Vecteurs des sondes, en tâche **requête** (celle des vraies recherches)."""
    try:
        from ai import embeddings

        return embeddings.embed_texts(list(probes), kind="query")
    except Exception as exc:
        print(f"   [similarite] sondes non vectorisables : {type(exc).__name__}: {exc}")
        return None


def reset_calibration_cache() -> None:
    """Oublie la mesure mémorisée (tests, ou après une ingestion massive)."""
    global _CALIBRATION_CACHE, _CALIBRATION_AT
    with _CALIBRATION_LOCK:
        _CALIBRATION_CACHE = None
        _CALIBRATION_AT = 0.0


def _current_model() -> str:
    """Modèle d'embeddings configuré (`EMBEDDING_MODEL`)."""
    from ai import embeddings

    return embeddings.MODEL


def has_chunks(media_id: str) -> bool:
    """Vrai si ce média a déjà au moins un morceau indexé.

    Sert aux **balayages répétés** : un scraper revoit les mêmes publications à
    chaque passage, et rejouer l'extraction (vision Gemini, Whisper) par média et
    par balayage coûterait le quota pour un texte identique. Le média est donc
    reconnu comme déjà indexé **avant** d'être retéléchargé — c'est l'équivalent,
    pour une route sans `telegram_file_id`, de la déduplication des doublons.

    Une lecture impossible lève : à l'appelant de décider, sachant que le pire cas
    d'un « je ne sais pas » traité comme « pas indexé » est une extraction refaite.
    """
    if not media_id:
        return False
    response = (
        _require_client()
        .table(TABLE)
        .select("id")
        .eq("media_id", media_id)
        .limit(1)
        .execute()
    )
    return bool(response.data or [])


def chunk_counts(
    media_ids: Sequence[str], *, page_size: Optional[int] = None
) -> Dict[str, int]:
    """Combien de morceaux indexés possède chacun de ces médias.

    Même lecture groupée que `media_with_chunks` — une requête par page, et non
    une par média —, mais les morceaux sont **comptés** au lieu d'être réduits à
    un booléen : une liste de médias dit ce qu'il y a à relire derrière chacun, et
    « douze morceaux » n'est pas « du texte ».

    Un média **absent** du dictionnaire n'a aucun morceau : le zéro n'est pas
    écrit, comme `media_with_chunks` ne rend que les médias trouvés. Un
    dictionnaire peuplé de zéros ferait passer une lecture vide pour une lecture
    complète.

    La lecture est **paginée** (`order by media_id`, puis `.range()`), pour la
    même raison que `media_store._read_media` : l'API ne rend qu'une partie des
    lignes d'une réponse, et une lecture tronquée **sous-compterait** — un média
    annoncé à un morceau alors qu'il en a cinquante se relirait à moitié, et rien
    ne le dirait. Une lecture impossible lève : l'appelant décide.
    """
    ids = sorted({str(media_id) for media_id in media_ids if media_id})
    if not ids:
        return {}
    # La taille de page est résolue **à l'appel** : le défaut de `COUNT_PAGE` est
    # le réglage de production, et une lecture groupée se règle comme les autres.
    size = max(1, int(page_size or COUNT_PAGE))
    counts: Dict[str, int] = {}
    offset = 0
    while True:
        page = (
            _require_client()
            .table(TABLE)
            .select("media_id")
            .in_("media_id", ids)
            .order("media_id")
            .range(offset, offset + size - 1)
            .execute()
            .data
            or []
        )
        for row in page:
            found = str(row.get("media_id") or "")
            if found:
                counts[found] = counts.get(found, 0) + 1
        if len(page) < size:
            return counts
        offset += size


def media_with_chunks(media_ids: Sequence[str]) -> Set[str]:
    """Parmi ces identifiants, ceux qui ont **au moins un** morceau indexé.

    Le cas booléen de `chunk_counts`, dont il est la projection : `/media` doit
    dire, ligne par ligne, s'il y a du texte à relire, et vingt-cinq lectures
    ponctuelles seraient vingt-cinq allers-retours pour une seule information.

    Une lecture impossible lève (comme `has_chunks`) : l'appelant décide, sachant
    qu'un « je ne sais pas » traité comme « pas indexé » propose de refaire
    l'extraction — un coût, pas une perte.
    """
    return set(chunk_counts(media_ids))


def list_chunks(
    media_id: str, *, columns: Optional[Sequence[str]] = None
) -> List[Dict[str, Any]]:
    """Morceaux d'un média, dans l'ordre du document.

    `columns` restreint la **projection** : `select("*")` ramène aussi
    `embedding`, soit 768 flottants par morceau (quelques dizaines de kilo-octets
    pour un document de vingt morceaux) — indispensable à `reembed_chunks`, inutile
    à qui relit le texte. Le défaut (`None`) garde toutes les colonnes, donc le
    comportement d'origine pour les appelants qui n'en demandent pas moins.
    """
    selection = ",".join(columns) if columns else "*"
    response = (
        _require_client()
        .table(TABLE)
        .select(selection)
        .eq("media_id", media_id)
        .order("chunk_index")
        .execute()
    )
    return response.data or []


def _embed_passages(chunks: Sequence[str]) -> Tuple[Optional[List[List[float]]], Optional[str]]:
    """Vecteurs des morceaux (`None, None` si les embeddings sont indisponibles).

    Le second élément est le modèle réellement utilisé : il est stocké dans
    `embedding_model`, ce qui permet ensuite de détecter les vecteurs périmés.
    """
    try:
        from ai import embeddings

        vectors = embeddings.embed_texts(list(chunks), kind="passage")
    except Exception as exc:  # dépendance manquante, panne réseau…
        print(f"   [embeddings] indisponibles : {type(exc).__name__}: {exc}")
        return None, None
    if not vectors:
        return None, None
    return vectors, embeddings.MODEL


def embeddings_available() -> bool:
    """Vrai si un vecteur de requête peut être produit.

    Test **local** (clé + dépendance), sans appel réseau : il sert uniquement à
    distinguer, pour l'utilisateur, « pas de recherche vectorielle » de « rien
    trouvé » — deux situations qui produisent le même `[]` dans `search_chunks`.
    """
    try:
        from ai import embeddings
    except Exception:
        return False
    if not embeddings.GEMINI_API_KEY:
        return False
    try:
        import httpx  # noqa: F401  — dépendance d'appel des embeddings
    except ImportError:
        return False
    return True


def _embed_query(query: str) -> Optional[List[float]]:
    """Vecteur de la question (`None` si les embeddings sont indisponibles)."""
    try:
        from ai import embeddings

        return embeddings.embed_text(query, kind="query")
    except Exception as exc:
        print(f"   [embeddings] embedding de requete indisponible : {type(exc).__name__}: {exc}")
        return None


def _approx_tokens(text: str) -> int:
    """Estimation grossière (~4 caractères/token) — le vrai compte dépend du modèle."""
    return max(0, len(text) // 4)


__all__ = [
    "DEFAULT_ASSET_QUERY",
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_OVERLAP_CHARS",
    "DEFAULT_TOP_K",
    "MATCH_FUNCTION",
    "MEDIA_TABLE",
    "SOURCE_MEDIA",
    "SOURCE_NOTE",
    "CALIBRATION_DOCS",
    "CALIBRATION_MIN_DOCS",
    "CALIBRATION_TTL_SECONDS",
    "COUNT_PAGE",
    "FLOOR_BOUNDS",
    "NOISE_MARGIN",
    "NOISE_PERCENTILE",
    "NOISE_PROBE_QUERIES",
    "TAGGED_HARD_FLOOR",
    "calibration",
    "chunk_text",
    "cosine_similarity",
    "delete_chunks",
    "embeddings_available",
    "chunk_counts",
    "has_chunks",
    "media_with_chunks",
    "noise_distribution",
    "percentile",
    "reset_calibration_cache",
    "select_media_candidates",
    "sample_document_vectors",
    "similarity_floor",
    "list_chunks",
    "reembed_chunks",
    "replace_chunks",
    "search_chunks",
    "search_media_for_asset",
    "set_chunks_asset",
]
