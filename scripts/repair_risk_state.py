#!/usr/bin/env python
"""Diagnostique et répare les états de risque inutilisables (`user_risk_state`).

Après le durcissement du garde-fou de risque, une ligne dont une référence de solde
est nulle ou négative **refuse** le trade : c'est voulu — « 5 % de 0 » n'est pas un
plafond. Ce script existe pour que ce refus soit **réparable** plutôt que subi, et
pour qu'il le soit **sans effacer une perte déjà subie**.

Deux temps, et le premier ne touche à rien :

* **par défaut** — lit chaque ligne, dit lesquelles sont inutilisables, **quoi**
  corriger, et ce qu'une reprise écrirait ;
* **``--apply``** — écrit les seules valeurs proposées.

Le point de reprise est le **capital configuré** de l'utilisateur
(`user_preferences.paper_equity`) : c'est la valeur que l'initialisation aurait
écrite, donc une reprise ne fait que *restaurer* ce qui manque. **Jamais le solde
courant** — s'en servir remettrait le drawdown à zéro, c'est-à-dire effacerait la
perte. Quand ce capital n'est pas connu, le script **ne fabrique rien** : il dit quoi
fournir.

Pour ces cas-là, `--set <utilisateur>:<champ>=<valeur>` fournit la valeur
explicitement (une valeur ≤ 0 est refusée : c'est le défaut à réparer). Une journée
close est signalée **sans écriture** : le garde repart d'un compteur journalier frais
au prochain solde lu.

Le script ne **supprime jamais** une ligne : la supprimer la ferait réinitialiser au
prochain solde lu, donc effacerait la perte en silence.

Codes de sortie : ``0`` rien à réparer ; ``1`` des lignes restent à réparer (ou à
renseigner) ; ``2`` mauvaise utilisation, ou base indisponible.

Exemples
--------

::

    # Ce qui est cassé, et ce qu'une reprise écrirait (lecture seule)
    python scripts/repair_risk_state.py

    # Réparer ce que le capital configuré permet de reprendre
    python scripts/repair_risk_state.py --apply

    # Fournir une référence quand le capital n'est pas connu
    python scripts/repair_risk_state.py --set 123456:starting_balance=5000 --apply

    # Sortie machine
    python scripts/repair_risk_state.py --json
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

OK_MARK = "\u2705"
KO_MARK = "\u274c"
WARN_MARK = "\u26a0\ufe0f"


def report_lines(report: Dict[str, Any]) -> List[str]:
    """Le rapport lisible : ce qui est cassé, quoi corriger, ce qui serait écrit."""
    lines = [
        f"Table {report['table']} : {report['rows']} ligne(s) d'état de risque.",
        f"   {OK_MARK} utilisables : {report['ok']}",
    ]
    if report["repairable"]:
        if report["applied"]:
            lines.append(
                f"   {OK_MARK} réparables : {report['repairable']} — "
                f"{report['repaired']} ligne(s) réparée(s)."
            )
        else:
            lines.append(
                f"   {WARN_MARK} réparables : {report['repairable']} — "
                "relance avec --apply pour écrire (aucune valeur ne passe par l'écran)."
            )
    if report["needs_value"]:
        lines.append(
            f"   {KO_MARK} sans valeur connue : {report['needs_value']} — "
            "à renseigner avec --set <utilisateur>:<champ>=<valeur>."
        )

    broken = [d for d in report["diagnoses"] if d["status"] != "ok"]
    if broken:
        lines.append("")
        lines.append("Lignes à corriger :")
        for diagnosis in broken:
            lines.append(f"   • utilisateur {diagnosis['user_id']} — {diagnosis['status']}")
            for problem in diagnosis["problems"]:
                lines.append(f"       – {problem}")
            if diagnosis["proposal"]:
                proposed = ", ".join(
                    f"{name}={value:g}" for name, value in diagnosis["proposal"].items()
                )
                marker = OK_MARK if report["applied"] else "→"
                lines.append(f"       {marker} écrit : {proposed}")
            for note in diagnosis["notes"]:
                lines.append(f"       ℹ️ {note}")

    lines.append("")
    lines.append(
        "Reprise : capital configuré (`paper_equity`), jamais le solde courant — "
        "aucune perte déjà subie n'est effacée, et aucune ligne n'est supprimée."
    )
    if report["needs_value"] and not report["applied"]:
        lines.append(
            "   Les lignes « sans valeur connue » ne seront pas écrites tant qu'un "
            "--set ne les renseigne pas : le script n'invente pas."
        )
    return lines


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repair_risk_state.py",
        description=(
            "Liste et répare les états de risque (user_risk_state) dont une référence "
            "de solde est nulle ou négative, sans jamais effacer une perte déjà subie."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Écrit réellement les valeurs proposées. Sans ce drapeau, le script "
        "ne fait que diagnostiquer (lecture seule).",
    )
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="UTILISATEUR:CHAMP=VALEUR",
        help="Référence explicite, quand le capital n'est pas connu "
        "(champ : starting_balance ou daily_start_balance ; une valeur ≤ 0 est refusée). "
        "Répétable.",
    )
    parser.add_argument("--json", action="store_true", help="Sortie JSON uniquement.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    console.make_streams_utf8()
    args = _build_parser().parse_args(argv)

    from database import risk_state_repair as repair

    try:
        overrides = repair.parse_overrides(args.overrides)
    except ValueError as exc:
        print(f"{KO_MARK} {exc}", file=sys.stderr)
        return 2

    try:
        report = repair.run(apply=args.apply, overrides=overrides)
    except RuntimeError as exc:
        print(f"{KO_MARK} {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        for line in report_lines(report):
            print(line)

    # Ce qui **reste** décide du code de sortie : une ligne réparée ne compte plus,
    # une ligne sans valeur connue compte toujours (elle est encore refusée).
    remaining = (report["repairable"] - report["repaired"]) + report["needs_value"]
    return 1 if remaining > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
