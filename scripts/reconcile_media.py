#!/usr/bin/env python
"""Réconciliation du bucket `telegram-media` avec la table `knowledge_media`.

Un **orphelin** est un objet présent dans le bucket mais absent de
`knowledge_media` : upload interrompu avant l'insertion, échec du nettoyage
automatique, ou ligne supprimée à la main. Il occupe du stockage sans être
référencé nulle part — invisible dans l'application, retrouvable seulement ici.

Le sens inverse (ligne sans objet) n'est pas traité par ce script : il demande de
re-télécharger le média, pas de supprimer quoi que ce soit.

Par défaut le script **ne supprime rien** : il liste. `--delete` applique la
suppression des objets listés.

Codes de sortie :

* ``0`` : réconciliation terminée (orphelins trouvés ou non) ;
* ``1`` : base ou Storage inaccessible — rien de fiable n'a été lu/écrit ;
* ``2`` : mauvaise utilisation (arguments refusés par ``argparse``).

Exemples
--------

::

    # Liste les objets sans ligne knowledge_media
    python scripts/reconcile_media.py

    # Les supprime réellement (après les avoir listés)
    python scripts/reconcile_media.py --delete

    # Se limite à un canal
    python scripts/reconcile_media.py --prefix telegram/thehalalwinningteam
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import console  # noqa: E402
from database import media_store  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Liste (et sur demande supprime) les objets du bucket sans ligne knowledge_media.",
    )
    parser.add_argument(
        "--prefix",
        default="",
        help="Ne considère que ce dossier du bucket (défaut : tout le bucket).",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Supprime les orphelins listés (sans ce drapeau, simple liste).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=media_store.REMOVE_BATCH_SIZE,
        help=f"Objets supprimés par appel (défaut : {media_store.REMOVE_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Borne le nombre d'orphelins traités par cette exécution.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    console.make_streams_utf8()
    args = _build_parser().parse_args(argv)

    try:
        orphans = media_store.list_orphan_objects(args.prefix)
    except Exception as exc:  # base ou Storage injoignable
        print(f"Réconciliation impossible : {type(exc).__name__}: {exc}")
        return 1

    if args.limit is not None:
        orphans = orphans[: max(0, args.limit)]

    scope = f"préfixe « {args.prefix} »" if args.prefix else "tout le bucket"
    print(f"Bucket « {media_store.BUCKET} » — {scope}")

    if not orphans:
        print("Aucun objet orphelin : le bucket et knowledge_media sont cohérents.")
        return 0

    print(f"{len(orphans)} objet(s) orphelin(s) sans ligne knowledge_media :")
    for path in orphans:
        print(f"   - {path}")

    if not args.delete:
        print("\nAucune suppression (relance avec --delete pour les retirer).")
        return 0

    try:
        removed = media_store.delete_objects(orphans, batch_size=args.batch_size)
    except Exception as exc:
        print(f"Suppression interrompue : {type(exc).__name__}: {exc}")
        return 1

    print(f"\n{removed} objet(s) supprimé(s) du bucket.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
