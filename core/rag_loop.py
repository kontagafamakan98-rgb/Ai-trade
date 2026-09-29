"""Boucle RAG : des passages proposés, puis injectés dans une analyse **validée**.

Le cycle automatique (`workers/auto_loop.py`) construit son contexte tout seul :
recherche vectorielle sur l'actif et le régime, médias rattachés, puis analyse LLM
et signal persisté. Cette boucle-ci fait l'inverse — c'est l'utilisateur qui
**choisit** ce que le modèle lira :

1. `/search` interroge la base et affiche des passages paginés (`/search_more`) ;
2. `/use <ACTIF> [rangs]` transforme une sélection de passages en **proposition**
   chiffrée (« 2 extraits, 780 caractères, pour BTC-USD ») — rien n'est exécuté ;
3. l'utilisateur **valide** ; l'analyse de l'actif part alors immédiatement, avec
   ces extraits injectés dans le prompt, sans attendre le prochain cycle.

Trois propriétés sont tenues par ce module, et ce sont elles qui justifient qu'il
existe plutôt qu'un simple appel dans le handler :

* **la sélection est bornée par ce qui a été affiché** — un rang inexistant est
  refusé avec la plage réelle, jamais ignoré en silence (sinon l'analyse
  partirait avec moins d'extraits que ce que l'utilisateur croit avoir validé) ;
* **une validation s'use** — `take()` consomme la proposition : un double clic
  sur « Lancer » ne déclenche pas deux analyses ;
* **ce qui est montré est ce qui sera injecté** — le bloc construit ici
  (`MAX_CONTEXT_CHARS`, `EXCERPT_CHARS`) est exactement celui transmis au
  prompt, aux plafonds de `ai/news_analyzer.py` près, qui s'appliquent en dernier.

La proposition en attente vit en mémoire, comme l'historique de `/search` : c'est
l'état d'une conversation, pas une donnée à conserver.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

from core import search_history
from database.knowledge_base import hit_label

#: Utilisateurs dont la proposition en attente est conservée (LRU).
MAX_USERS = 50

#: Nombre de passages injectés quand l'utilisateur n'en désigne aucun.
DEFAULT_RANKS = 3

#: Plafond du bloc injecté. Doit rester **inférieur ou égal** à
#: `ai.news_analyzer.MAX_RAG_CONTEXT_CHARS`, qui rogne en dernier ressort : mieux
#: vaut rogner ici, où l'utilisateur voit ce qui sera lu.
MAX_CONTEXT_CHARS = 1500

#: Plafond par extrait : un passage entier (jusqu'à 1500 caractères) noierait les
#: autres dans un bloc plafonné.
EXCERPT_CHARS = 500


class RagProposal(NamedTuple):
    """Une sélection en attente de validation."""

    asset: str
    ranks: List[int]
    query: str
    #: Bloc de texte exact qui sera injecté dans le prompt.
    context: str
    excerpts: List[str]

    @property
    def chars(self) -> int:
        return len(self.context)


_LOCK = threading.Lock()
_PENDING: "OrderedDict[str, RagProposal]" = OrderedDict()


def parse_ranks(spec: Optional[str], *, available: int) -> Dict[str, Any]:
    """Interprète `1,3` / `2-4` / `1 3` (les rangs affichés par `/search`).

    Accepte les rangs **tels qu'ils sont affichés** (numérotation 1-based sur tout
    le lot, qui continue d'une page à l'autre). Un rang hors du lot est refusé
    avec la plage réelle : le corriger soi-même serait deviner à la place de
    l'utilisateur — et valider une analyse sur d'autres extraits que ceux
    annoncés.

    Sans `spec`, renvoie les ``min(DEFAULT_RANKS, available)`` premiers passages.
    """
    if available <= 0:
        return {"ok": False, "reason": "empty_pool", "ranks": []}

    raw = (spec or "").strip()
    if not raw:
        head = min(DEFAULT_RANKS, available)
        return {"ok": True, "reason": None, "ranks": list(range(1, head + 1))}

    ranks: List[int] = []
    for chunk in raw.replace(",", " ").split():
        start, _, end = chunk.partition("-") if "-" in chunk[1:] else (chunk, "", "")
        try:
            first = int(start)
            last = int(end) if end else first
        except ValueError:
            return {"ok": False, "reason": "bad_rank", "ranks": [], "value": chunk}
        if first < 1 or last < 1 or first > last:
            return {"ok": False, "reason": "bad_rank", "ranks": [], "value": chunk}
        if last > available:
            return {
                "ok": False,
                "reason": "out_of_range",
                "ranks": [],
                "value": chunk,
                "available": available,
            }
        for rank in range(first, last + 1):
            if rank not in ranks:
                ranks.append(rank)

    if not ranks:
        return {"ok": False, "reason": "empty_selection", "ranks": []}
    return {"ok": True, "reason": None, "ranks": ranks}


def _excerpt(hit: Dict[str, Any], rank: int) -> str:
    """Un extrait du bloc injecté : rang affiché, provenance et contenu tronqué.

    Le rang est exactement celui de `/search` (`1. [note · BTC-USD] — …`) : ce que
    l'utilisateur a validé doit pouvoir être relu ligne à ligne dans le prompt.
    """
    content = " ".join(str(hit.get("content") or hit.get("matched_chunk") or "").split())
    if len(content) > EXCERPT_CHARS:
        content = content[:EXCERPT_CHARS].rstrip() + "…"
    score = hit.get("similarity")
    try:
        score_txt = f"{float(score):.3f}"
    except (TypeError, ValueError):
        score_txt = "n/a"
    return f"{rank}. {hit_label(hit)} — similarité {score_txt}\n{content}"


def build_context(
    hits: Sequence[Dict[str, Any]], ranks: Sequence[int]
) -> tuple[str, List[int]]:
    """Bloc injecté dans le prompt **et** la liste des rangs réellement retenus.

    Le plafond est appliqué **par extrait entier** : couper au milieu d'un extrait
    donnerait un texte tronqué sans que l'utilisateur sache lequel a été rogné.

    Renvoyer les rangs retenus (et pas seulement le texte) est indispensable :
    afficher « 20 extraits » alors que le plafond n'en laisse passer que deux
    ferait valider une analyse sur un contexte qui n'est pas celui annoncé.
    """
    blocks: List[str] = []
    kept: List[int] = []
    used = 0
    for rank in ranks:
        if rank < 1 or rank > len(hits):
            continue
        block = _excerpt(hits[rank - 1], rank)
        if used + len(block) > MAX_CONTEXT_CHARS:
            break
        blocks.append(block)
        kept.append(rank)
        used += len(block)
    return "\n\n".join(blocks), kept


def propose(user_id: str, asset: str, spec: Optional[str] = None) -> Dict[str, Any]:
    """Prépare une analyse : passages choisis dans la **dernière** recherche.

    Ne lance rien : la proposition attend une validation (`take`). Les échecs sont
    nommés (`no_history`, `bad_asset`, `bad_rank`, `out_of_range`, `empty_pool`) —
    chacun a un remède différent, donc un message différent.
    """
    target = (asset or "").strip().upper()
    if not target:
        return {"ok": False, "reason": "bad_asset", "asset": ""}

    session = search_history.latest(str(user_id))
    if session is None or not session.pool:
        return {"ok": False, "reason": "no_history", "asset": target}

    parsed = parse_ranks(spec, available=len(session.pool))
    if not parsed["ok"]:
        return {**parsed, "asset": target, "available": parsed.get("available", len(session.pool))}

    ranks = parsed["ranks"]
    context, kept = build_context(session.pool, ranks)
    if not context.strip() or not kept:
        return {"ok": False, "reason": "empty_selection", "asset": target}

    proposal = RagProposal(
        asset=target,
        ranks=kept,
        query=session.query,
        context=context,
        excerpts=[_excerpt(session.pool[rank - 1], rank) for rank in kept],
    )
    with _LOCK:
        _PENDING[str(user_id)] = proposal
        _PENDING.move_to_end(str(user_id))
        while len(_PENDING) > MAX_USERS:
            _PENDING.popitem(last=False)
    return {
        "ok": True,
        "reason": None,
        "asset": target,
        "ranks": kept,
        "requested": len(ranks),
        "excerpts": proposal.excerpts,
        "chars": proposal.chars,
        # Le plafond est transmis avec la proposition : le message d'affichage vit
        # dans un module qui ne peut pas importer celui-ci (il en utilise déjà
        # `hit_label`), et deux constantes homonymes se sont déjà confondues.
        "budget": MAX_CONTEXT_CHARS,
        "query": session.query,
        "context": context,
    }


def take(user_id: str) -> Optional[RagProposal]:
    """Consomme la proposition en attente : valider **use** la validation.

    Un double clic sur « Lancer » ne doit pas produire deux analyses (et donc
    deux propositions de trade) : la seconde ne trouverait plus rien.
    """
    with _LOCK:
        return _PENDING.pop(str(user_id), None)


def pending(user_id: str) -> Optional[RagProposal]:
    """La proposition en attente, sans la consommer (affichage, tests)."""
    with _LOCK:
        return _PENDING.get(str(user_id))


def cancel(user_id: str) -> bool:
    """Abandonne la proposition. `True` si quelque chose a bien été annulé."""
    with _LOCK:
        return _PENDING.pop(str(user_id), None) is not None


def reset() -> None:
    """Vide les propositions en attente. Réservé aux tests : l'état est global."""
    with _LOCK:
        _PENDING.clear()


__all__ = [
    "DEFAULT_RANKS",
    "EXCERPT_CHARS",
    "MAX_CONTEXT_CHARS",
    "MAX_USERS",
    "RagProposal",
    "build_context",
    "cancel",
    "parse_ranks",
    "pending",
    "propose",
    "reset",
    "take",
]
