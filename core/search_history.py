"""Historique des recherches `/search` — de quoi paginer sans relancer la recherche.

`/search` interroge la base **une seule fois** et ramène un *lot* de passages
(``search_knowledge(..., top_k=SEARCH_POOL_SIZE)``). Les premiers sont affichés,
les suivants sont conservés ici : `/search_more` déroule le lot, sans nouvel
appel d'embedding ni requête SQL. C'est la différence entre « voir la suite d'une
recherche » et « relancer une recherche » — la seconde coûte un appel réseau et
peut renvoyer un classement différent, ce qui rendrait la pagination incohérente.

L'historique est **en mémoire** et volontairement minuscule :

* **une** entrée par utilisateur : `/search_more` continue la dernière recherche,
  une nouvelle recherche remplace la précédente. Garder davantage d'entrées sans
  interface pour les choisir ne serait pas un historique, seulement de la
  mémoire occupée ;
* au plus ``MAX_USERS`` utilisateurs (éviction du plus ancien) : le bot tourne en
  continu, la borne doit exister. Un lot complet pèse au plus
  ``SEARCH_POOL_SIZE`` extraits (~30 ko) — soit ~1,5 Mo au pire pour l'ensemble ;
* les passages sont gardés **entiers** (contenu compris) : c'est le prix à payer
  pour ne rien redemander au réseau au moment d'afficher la suite.

Aucune dépendance à Supabase : ce n'est pas une donnée à conserver entre deux
redémarrages, c'est l'état d'une conversation.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Dict, List, NamedTuple, Optional

#: Utilisateurs dont la dernière recherche est conservée (LRU).
MAX_USERS = 50


class SearchSession(NamedTuple):
    """Une recherche et ce qu'il en reste à montrer."""

    query: str
    options: Dict[str, Any]
    #: Tous les passages du lot, dans l'ordre de pertinence.
    pool: List[Dict[str, Any]]
    #: Nombre de passages déjà affichés (les `shown` premiers).
    shown: int

    @property
    def remaining(self) -> int:
        return max(0, len(self.pool) - self.shown)


_LOCK = threading.Lock()
_SESSIONS: "OrderedDict[str, SearchSession]" = OrderedDict()


def _page_size(options: Dict[str, Any]) -> int:
    """Taille de page : `-n` de la recherche, plancher à 1.

    Validée par `parse_search_args`, donc entière et dans les bornes ; le
    plancher n'est là que pour qu'un appel direct (test, script) ne puisse pas
    produire une page vide.
    """
    try:
        size = int(options.get("top_k") or 0)
    except (TypeError, ValueError):
        return 1
    return max(1, size)


def _store(user_id: str, session: SearchSession) -> None:
    """Écrit la session et fait respecter la borne d'utilisateurs (LRU)."""
    _SESSIONS[user_id] = session
    _SESSIONS.move_to_end(user_id)
    while len(_SESSIONS) > MAX_USERS:
        _SESSIONS.popitem(last=False)


def start(user_id: str, options: Dict[str, Any], pool: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Enregistre le lot d'une recherche et retourne sa **première page**.

    Retourne le contrat habituel de la commande (`ok`/`reason`/`hits`) enrichi de
    `start` et `total` : le rendu peut alors annoncer « passages 1 à 5 sur 17 » et
    proposer `/search_more`.
    """
    hits = list(pool or [])
    size = _page_size(options)
    page = hits[:size]
    with _LOCK:
        _store(
            str(user_id),
            SearchSession(
                query=str(options.get("query") or ""),
                options=dict(options),
                pool=hits,
                shown=len(page),
            ),
        )
    return _page_result(
        query=str(options.get("query") or ""),
        options=options,
        hits=page,
        start=1,
        total=len(hits),
    )


def next_page(user_id: str) -> Dict[str, Any]:
    """Page suivante de la dernière recherche de cet utilisateur.

    `ok=False` avec `reason="no_history"` (rien à continuer) ou
    `"exhausted"` (le lot est épuisé) : les deux cas n'ont pas le même remède,
    donc pas le même message.
    """
    with _LOCK:
        session = _SESSIONS.get(str(user_id))
        if session is None:
            return _rejected("no_history")
        if session.remaining <= 0:
            return {
                "ok": False,
                "reason": "exhausted",
                "query": session.query,
                "options": dict(session.options),
                "total": len(session.pool),
                "shown": session.shown,
                "hits": [],
            }
        size = _page_size(session.options)
        hits = session.pool[session.shown : session.shown + size]
        start = session.shown + 1
        _store(str(user_id), session._replace(shown=session.shown + len(hits)))

    return _page_result(
        query=session.query,
        options=session.options,
        hits=hits,
        start=start,
        total=len(session.pool),
    )


def latest(user_id: str) -> Optional[SearchSession]:
    """La session courante d'un utilisateur, ou `None`. Lecture seule (tests)."""
    with _LOCK:
        session = _SESSIONS.get(str(user_id))
        return None if session is None else session._replace(pool=list(session.pool))


def forget(user_id: str) -> None:
    """Oublie la recherche d'un utilisateur (utile aux tests, ou à un `/reset`)."""
    with _LOCK:
        _SESSIONS.pop(str(user_id), None)


def reset() -> None:
    """Vide l'historique entier. Réservé aux tests : l'état est global."""
    with _LOCK:
        _SESSIONS.clear()


def _page_result(
    *,
    query: str,
    options: Dict[str, Any],
    hits: List[Dict[str, Any]],
    start: int,
    total: int,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "reason": None,
        "query": query,
        "options": dict(options),
        "hits": list(hits),
        "start": start,
        "total": total,
    }


def _rejected(reason: str) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "error": None, "hits": []}


__all__ = [
    "MAX_USERS",
    "SearchSession",
    "forget",
    "latest",
    "next_page",
    "reset",
    "start",
]
