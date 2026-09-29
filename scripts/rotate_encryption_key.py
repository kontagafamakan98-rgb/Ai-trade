#!/usr/bin/env python
"""Termine une rotation d'`ENCRYPTION_KEY` : compte ce qui reste, puis réécrit.

Pose une clé neuve dans `ENCRYPTION_KEY` et laisse l'ancienne dans
`ENCRYPTION_KEYS_PREVIOUS` : l'anneau rouvre l'ancien chiffré, et tout ce qui
s'écrit ensuite porte la nouvelle version (`v2:<jeton>`). Mais la rotation n'est
**pas finie** pour autant — tant qu'une ligne est chiffrée avec l'ancienne clé,
elle doit rester dans l'anneau, donc dans `.env`, donc sur la plateforme. Ce
script dit combien il en reste, puis les réécrit avec la clé active.

Deux temps, et le premier ne touche à rien :

* **par défaut** — compte les lignes de `user_broker_credentials` par version de
  clé, celles qui restent à tourner, et celles qui ne se rouvrent pas ;
* **``--apply``** — réécrit les lignes concernées avec la clé active.

Rien de ce qui est lu n'est affiché : ni clé, ni identifiant broker, ni texte
clair. Le rapport ne porte que des compteurs, des versions et des identifiants
d'utilisateur — de quoi savoir **qui** doit reconnecter son compte si une clé
manque à l'anneau.

Codes de sortie : ``0`` plus rien à tourner ; ``1`` il reste des lignes à tourner
(ou illisibles) ; ``2`` mauvaise utilisation, ou base indisponible.

Exemples
--------

::

    # Ce qu'il reste à tourner (lecture seule)
    python scripts/rotate_encryption_key.py

    # Réécrire les lignes restantes avec la clé active
    python scripts/rotate_encryption_key.py --apply

    # Sortie machine, pour un script de rotation
    python scripts/rotate_encryption_key.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import console  # noqa: E402
from utils.encryption import (  # noqa: E402
    EncryptionKeyInvalid,
    EncryptionKeyMissing,
    get_ring,
)

OK_MARK = "\u2705"
KO_MARK = "\u274c"
WARN_MARK = "\u26a0\ufe0f"


def _versions_label(versions: Sequence[int]) -> str:
    return ", ".join(f"v{version}" for version in versions) or "—"


def report_lines(report: Dict[str, Any]) -> List[str]:
    """Le rapport lisible — des compteurs et des versions, jamais une valeur."""
    primary = report["primary_version"]
    previous = [version for version in report["ring_versions"] if version != primary]
    lines = [
        f"Anneau de clés : v{primary} (active)"
        + (f", retirées : {_versions_label(previous)}" if previous else ", aucune clé retirée"),
        f"Table {report['table']} : {report['rows']} ligne(s)",
    ]
    for version in sorted(report["by_version"], reverse=True):
        count = report["by_version"][version]
        mark = OK_MARK if version == primary else WARN_MARK
        lines.append(f"   {mark} v{version} : {count} ligne(s)")

    unreadable = report["unreadable"]
    if unreadable:
        lines.append(
            f"{KO_MARK} {len(unreadable)} ligne(s) illisible(s) — une clé manque "
            "probablement à l'anneau :"
        )
        for user_id, reason in unreadable:
            lines.append(f"   • utilisateur {user_id} — {reason}")

    if report["to_rotate"]:
        if report["applied"]:
            lines.append(
                f"{OK_MARK} {report['rewritten']} ligne(s) réécrite(s) avec la clé "
                f"active (v{primary})."
            )
        else:
            lines.append(
                f"{KO_MARK} {report['to_rotate']} ligne(s) encore chiffrée(s) avec une "
                "clé retirée — à réécrire."
            )
            lines.append(
                "   Relance avec --apply : les valeurs ne passent jamais par l'écran."
            )
    else:
        lines.append(
            f"{OK_MARK} Aucune ligne à réécrire : tout est chiffré avec la clé active "
            f"(v{primary})."
        )
        if previous:
            lines.append(
                "   La ou les clés retirées ne rouvrent plus rien : retire-les de "
                "`ENCRYPTION_KEYS_PREVIOUS`, puis "
                "`python scripts/verify_secrets.py --record`."
            )
    lines.append(
        f"(Clé active : v{primary}. Après toute modification de l'anneau, le "
        "registre de rotation se re-horodate : python scripts/verify_secrets.py "
        "--record.)"
    )
    return lines


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rotate_encryption_key.py",
        description=(
            "Compte et réécrit les identifiants broker encore chiffrés avec une clé "
            "retirée de l'anneau."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Réécrit réellement les lignes avec la clé active. Sans ce drapeau, le "
        "script ne fait que compter (lecture seule).",
    )
    parser.add_argument("--json", action="store_true", help="Sortie JSON uniquement.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    console.make_streams_utf8()
    args = _build_parser().parse_args(argv)

    # Construire l'anneau d'abord : une clé active absente ou une entrée retirée
    # illisible se dit ici, avant la moindre lecture de base.
    try:
        get_ring()
    except (EncryptionKeyMissing, EncryptionKeyInvalid) as exc:
        print(f"{KO_MARK} {exc}", file=sys.stderr)
        return 2

    # Import **après** l'anneau : sans clé utilisable, il n'y a rien à compter, et
    # inutile de charger le client Supabase pour le dire.
    from database.broker_credentials import reencrypt_all

    try:
        report = reencrypt_all(apply=args.apply)
    except RuntimeError as exc:
        print(f"{KO_MARK} {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        for line in report_lines(report):
            print(line)

    # Après un `--apply`, ce qui comptait encore à tourner ne compte plus : c'est
    # ce qui **reste** qui décide du code de sortie, jamais ce qui a été fait.
    remaining = report["to_rotate"] - report["rewritten"]
    return 1 if remaining > 0 or report["unreadable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
