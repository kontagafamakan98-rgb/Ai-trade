#!/usr/bin/env python
"""Seuil de similarité des médias, calibré sur la base (au lieu de la constante 0,5).

Une similarité cosinus n'a pas d'échelle absolue : deux documents **sans rapport**
d'un corpus homogène (quelques dizaines de morceaux traitant tous de trading)
peuvent atteindre 0,65, là où un corpus vaste et hétérogène descend à 0,3 pour deux
documents réellement liés. Un seuil fixe se trompe donc dans les deux cas : il
laisse passer du bruit sur le premier et jette les bons extraits du second.

Ce script montre la mesure : la similarité entre des questions **délibérément hors
sujet** et les documents de la base, puis le seuil qui en découle (percentile élevé
du bruit + marge). C'est le chiffre que `get_media_context` applique réellement —
la constante de `knowledge_base` ne servant plus qu'au repli.

Codes de sortie :

* ``0`` : mesure effectuée (la valeur est affichée) ;
* ``1`` : mesure impossible (base inaccessible, aucun vecteur, clé Gemini absente) ;
* ``2`` : mauvaise utilisation (arguments refusés par ``argparse``).

Exemples
--------

::

    # Le seuil de la base, tel qu'il sera appliqué
    python scripts/calibrate_similarity.py

    # Échantillon plus large, percentile plus exigeant
    python scripts/calibrate_similarity.py --docs 80 --percentile 99

    # Sortie machine (intégration, comparaison de deux bases)
    python scripts/calibrate_similarity.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import console  # noqa: E402
from database import knowledge_base, knowledge_index  # noqa: E402

#: Champs de la distribution affichés, dans l'ordre de lecture.
_STAT_ORDER = ("min", "median", "p75", "p90", "p95", "max")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Mesure le seuil de similarité des médias sur la base.",
    )
    parser.add_argument(
        "--docs",
        type=int,
        default=knowledge_index.CALIBRATION_DOCS,
        help=f"documents échantillonnés (défaut : {knowledge_index.CALIBRATION_DOCS})",
    )
    parser.add_argument(
        "--percentile",
        type=float,
        default=knowledge_index.NOISE_PERCENTILE,
        help=f"percentile du bruit retenu (défaut : {knowledge_index.NOISE_PERCENTILE:g})",
    )
    parser.add_argument(
        "--margin",
        type=float,
        default=knowledge_index.NOISE_MARGIN,
        help=f"marge ajoutée au percentile (défaut : {knowledge_index.NOISE_MARGIN:g})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="sortie JSON (pour comparer deux bases ou surveiller l'évolution)",
    )
    return parser


def measure(docs: int, percentile: float, margin: float) -> dict:
    """Mesure **hors cache** : le script décrit la base maintenant, pas il y a 15 min."""
    measured = knowledge_index.calibration(force=True, limit=docs)
    score = knowledge_index.similarity_floor(
        measured, percentile_value=percentile, margin=margin
    )
    return {**measured, "floor": score, "percentile": percentile, "margin": margin}


def _distribution_line(measured: dict) -> str:
    parts = [
        f"{name} {float(measured[name]):.3f}"
        for name in _STAT_ORDER
        if measured.get(name) is not None
    ]
    return " · ".join(parts)


def main(argv: Optional[Sequence[str]] = None) -> int:
    #: La sortie est réglée **avant** tout le reste : ce script imprime des seuils
    #: calibrés (`≈`, `→`) et un verdict, et un `UnicodeEncodeError` les remplacerait.
    console.make_streams_utf8()
    args = _build_parser().parse_args(argv)

    if args.docs < knowledge_index.CALIBRATION_MIN_DOCS:
        print(
            f"--docs doit valoir au moins {knowledge_index.CALIBRATION_MIN_DOCS} : "
            "en dessous, l'échantillon ne représente pas la base.",
            file=sys.stderr,
        )
        return 2

    measured = measure(args.docs, args.percentile, args.margin)

    if args.json:
        print(json.dumps(measured, ensure_ascii=False, sort_keys=True))
        return 0 if measured.get("floor") is not None else 1

    print("Seuil de similarité des médias — mesure sur la base")
    print(
        f"Repli codé en dur : {knowledge_base.MIN_MEDIA_SIMILARITY:.3f} "
        "(utilisé seulement si la mesure est impossible)"
    )
    print()

    if measured.get("floor") is None:
        print(f"Mesure impossible : {measured.get('reason')}")
        print("Le seuil de repli ci-dessus reste appliqué — rien n'est cassé.")
        return 1

    print(
        f"Échantillon : {measured['documents']} document(s) (un morceau par média) · "
        f"{measured['probes']} sonde(s) hors sujet"
    )
    print("Distribution du bruit (question sans rapport x document) :")
    print(f"  {_distribution_line(measured)}   ({measured['pairs']} paires)")
    print(
        f"Seuil retenu : {measured['floor']:.3f}  = "
        f"p{args.percentile:g} du bruit + {args.margin:g}"
    )
    print()
    print(
        "Un média dont la similarité n'atteint pas ce seuil n'est pas injecté dans\n"
        "l'analyse ; si aucun ne l'atteint, le bloc média reste vide — c'est le\n"
        "comportement voulu (un extrait hors sujet fait dériver l'analyse)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
