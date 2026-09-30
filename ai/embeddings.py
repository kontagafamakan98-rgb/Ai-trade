"""Embeddings Gemini pour la recherche vectorielle de la base de connaissances.

Modèle : `gemini-embedding-001`, appelé via l'**API Gemini** (`:embedContent`) —
offerte sur le palier gratuit, avec la même clé `GEMINI_API_KEY` que l'analyse de
news et la vision de `ai/media_extractor.py`. Aucun service ni compte en plus, et
`httpx` reste la seule dépendance (importée à l'usage).

Quatre points de conception :

* la dimension est **imposée à 768** par la colonne `knowledge_chunks.embedding`
  (`extensions.vector(768)`). Les modèles d'embedding Gemini produisent 3072
  dimensions par défaut et sont entraînés en MRL (*Matryoshka Representation
  Learning*) : tronquer via `outputDimensionality` conserve l'essentiel de la
  qualité sans migration de schéma ;
* `gemini-embedding-001` distingue `RETRIEVAL_DOCUMENT` (les morceaux indexés) et
  `RETRIEVAL_QUERY` (la question). C'est l'équivalent Gemini des préfixes
  `passage:`/`query:` d'e5 : l'omettre dégrade nettement la similarité, donc on ne
  le laisse pas à l'appelant ;
* `gemini-embedding-2` est **inadapté** ici : il agrège plusieurs entrées en *un
  seul* vecteur, alors que l'indexation a besoin d'un vecteur par morceau ;
* un vecteur tronqué par MRL n'est plus unitaire : les vecteurs sont donc
  systématiquement **normalisés (L2)**, ce que suppose la similarité cosinus de
  pgvector (`<=>`).

Les textes partent **un par un** : `embedContent` accepte plusieurs parties de
texte, mais c'est exactement le chemin qui déclenche l'agrégation sur les modèles
multimodaux. Un appel par texte reste prévisible quel que soit le modèle.

Sans `GEMINI_API_KEY` ou sans `httpx`, `embed_texts` retourne `None` : la
recherche vectorielle est une amélioration, son absence ne doit jamais casser un
appel au moteur de décision. Une dimension inattendue est aussi refusée — stocker
un vecteur de mauvaise taille casserait la recherche côté Postgres.
"""
from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence

from config import EMBEDDING_MODEL, GEMINI_API_KEY

#: Dimension imposée par `knowledge_chunks.embedding` (migration 008).
EMBEDDING_DIM = 768

#: Modèle par défaut, surchargeable par la variable d'env `EMBEDDING_MODEL`.
MODEL = EMBEDDING_MODEL

#: Base de l'API Gemini (même hôte que `ai/media_extractor.py`).
API_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

#: Tâche déclarée à Gemini, selon le côté de la recherche.
TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_QUERY = "RETRIEVAL_QUERY"

_REQUEST_TIMEOUT = 60


def task_type(kind: str) -> str:
    """Traduit `kind` (`"passage"`/`"query"`) en `taskType` Gemini.

    Lève `ValueError` pour toute autre valeur : une faute de frappe doit se voir
    immédiatement, pas produire des vecteurs silencieusement dégradés.
    """
    if kind == "query":
        return TASK_QUERY
    if kind == "passage":
        return TASK_DOCUMENT
    raise ValueError(f"kind inconnu : {kind!r} (attendu 'query' ou 'passage')")


def httpx_available() -> bool:
    """Vrai si l'appel HTTP est possible (`httpx` installé).

    Sonde nommée, et non un `try/except ImportError` autour de la fonction : le
    `None` de `embed_texts` est la valeur de repli *documentée*, et il valait pour
    trois causes différentes — pas de clé, bibliothèque absente, appel qui échoue.
    Seule la bibliothèque manquante était indiscernable par l'appelant (les deux
    autres se voient : `GEMINI_API_KEY` est lisible, un appel raté lève). Publier
    la question permet à `embeddings_available()` de la poser au lieu de recopier
    une sonde — et à un rapport de distinguer « pas d'index » de « rien trouvé ».

    L'import est **essayé** plutôt que sondé (`importlib.util.find_spec`) : la
    question est « puis-je importer », et une doublure posée dans `sys.modules`
    (tests) y répond différemment — la sonde et l'appel seraient alors en
    désaccord, ce qui est pire que le silence qu'on corrige.
    """
    try:
        import httpx  # noqa: F401  — on veut seulement savoir si l'import aboutit
    except ImportError:
        return False
    return True


def embed_texts(
    texts: Sequence[str],
    *,
    kind: str = "passage",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[List[List[float]]]:
    """Retourne un vecteur par texte, ou `None` si les embeddings sont indisponibles.

    `None` (et non une exception) est la valeur de repli : la recherche
    vectorielle est une amélioration, son absence ne doit jamais casser un appel
    au moteur de décision. `kind` est en revanche validé tout de suite.
    """
    if not texts:
        return []
    task = task_type(kind)
    key = api_key if api_key is not None else GEMINI_API_KEY
    if not key:
        return None

    if not httpx_available():
        return None
    import httpx

    model_name = model or MODEL
    vectors: List[List[float]] = []
    with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
        for text in texts:
            vector = _embed_one(client, text, task=task, model=model_name, api_key=key)
            if vector is None:
                return None
            vectors.append(vector)
    return vectors


def embed_text(
    text: str,
    *,
    kind: str = "query",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[List[float]]:
    """Vecteur d'un seul texte (`None` si les embeddings sont indisponibles)."""
    vectors = embed_texts([text], kind=kind, model=model, api_key=api_key)
    if not vectors:
        return None
    return vectors[0]


def _embed_one(
    client: Any,
    text: str,
    *,
    task: str,
    model: str,
    api_key: str,
) -> Optional[List[float]]:
    """Un appel `:embedContent`, un vecteur normalisé — ou `None`."""
    payload = {
        "model": f"models/{model}",
        "content": {"parts": [{"text": text}]},
        "taskType": task,
        "outputDimensionality": EMBEDDING_DIM,
    }
    try:
        response = client.post(
            f"{API_BASE_URL}/{model}:embedContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json=payload,
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        print(f"   [embeddings] erreur Gemini : {type(exc).__name__}: {exc}")
        return None

    values = _extract_values(body)
    if values is None:
        print("   [embeddings] reponse Gemini inattendue : aucun vecteur lisible")
        return None
    if len(values) != EMBEDDING_DIM:
        print(
            f"   [embeddings] dimension inattendue (attendu {EMBEDDING_DIM}, "
            f"recu {len(values)}) - modele {model} incompatible"
        )
        return None
    return _normalize([float(value) for value in values])


def _extract_values(body: Any) -> Optional[List[float]]:
    """Vecteur d'une réponse `embedContent`.

    Forme documentée : ``{"embedding": {"values": [...]}}``. On accepte aussi
    ``{"embeddings": [{"values": [...]}]}`` (un seul élément), que renvoie le
    même modèle selon la version de l'API.
    """
    if not isinstance(body, dict):
        return None

    single = body.get("embedding")
    if isinstance(single, dict) and isinstance(single.get("values"), list):
        return single["values"]

    many = body.get("embeddings")
    if isinstance(many, list) and len(many) == 1 and isinstance(many[0], dict):
        values = many[0].get("values")
        if isinstance(values, list):
            return values
    return None


def _normalize(vector: Sequence[float]) -> List[float]:
    """Normalisation L2 (les vecteurs sont ensuite comparés en cosinus)."""
    norm = math.sqrt(sum(float(x) * float(x) for x in vector))
    if norm == 0:
        return [0.0] * len(vector)
    return [float(x) / norm for x in vector]


__all__ = [
    "API_BASE_URL",
    "EMBEDDING_DIM",
    "MODEL",
    "TASK_DOCUMENT",
    "TASK_QUERY",
    "embed_text",
    "embed_texts",
    "httpx_available",
    "task_type",
]
