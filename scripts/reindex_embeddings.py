#!/usr/bin/env python
"""Ré-vectorisation des `knowledge_chunks` avec les embeddings Gemini.

Changer de modèle ou de fournisseur d'embeddings change l'**espace vectoriel** :
des vecteurs e5 et Gemini dans la même colonne produiraient des similarités
dénuées de sens. Ce script recalcule les vecteurs des morceaux **déjà en base** —
sans re-télécharger les médias, puisque leur texte est stocké — et inscrit dans
`embedding_model` le modèle qui a produit chaque vecteur.

Codes de sortie :

* ``0`` : ré-indexation terminée (ou index déjà à jour) ;
* ``1`` : clé Gemini absente ou base inaccessible — rien de fiable n'a été écrit ;
* ``2`` : mauvaise utilisation (arguments refusés par ``argparse``).

Exemples
--------

::

    # Combien de morceaux sont périmés ? (aucune écriture, aucune clé requise)
    python scripts/reindex_embeddings.py --dry-run

    # Ré-indexe les morceaux périmés ou jamais vectorisés
    python scripts/reindex_embeddings.py

    # Force le recalcul de TOUT l'index, par lots de 50
    python scripts/reindex_embeddings.py --all --batch-size 50
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ai import embeddings  # noqa: E402
from config import GEMINI_API_KEY  # noqa: E402
from core import console  # noqa: E402
from database import knowledge_index  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ré-vectorise knowledge_chunks avec les embeddings Gemini.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Modèle d'embeddings à utiliser (défaut : EMBEDDING_MODEL).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Recalcule aussi les vecteurs déjà produits par le modèle courant.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compte les morceaux concernés sans rien écrire.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Morceaux traités par lot (défaut : 50).",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Borne le nombre de morceaux traités par cette exécution.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    console.make_streams_utf8()
    args = _build_parser().parse_args(argv)
    model = args.model or embeddings.MODEL

    if not args.dry_run and not GEMINI_API_KEY:
        print(
            "GEMINI_API_KEY absente : les embeddings sont désactivés, "
            "aucun vecteur ne peut être calculé."
        )
        return 1

    scope = "tout l'index" if args.all else "morceaux périmés ou jamais vectorisés"
    print(f"Embeddings Gemini — modèle cible : {model}")
    print(f"Portée : {scope}")

    try:
        processed = knowledge_index.reembed_chunks(
            model=model,
            only_stale=not args.all,
            page_size=max(1, args.batch_size),
            max_rows=args.max_rows,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        print(f"Base inaccessible : {exc}")
        return 1

    if args.dry_run:
        print(f"{processed} morceau(x) à (re)vectoriser — aucune écriture (--dry-run).")
    elif processed:
        print(f"{processed} morceau(x) ré-vectorisé(s) avec {model}.")
        if args.max_rows is not None and processed >= args.max_rows:
            print("Borne --max-rows atteinte : relance le script pour continuer.")
    else:
        print(f"Aucun morceau à traiter : l'index est déjà à jour pour {model}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
