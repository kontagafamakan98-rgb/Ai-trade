#!/usr/bin/env python
"""Publie l'arbre local sur une branche distante, et vérifie chaque fichier poussé.

L'arbre de travail n'est pas un dépôt git : le publier se faisait jusqu'ici à la
main — recopier, deviner, pousser — et deux accidents ont montré ce que valait
cette méthode : un fichier `git` vide de 0 octet, né d'une redirection, publié
sans que personne ne le voie, et dix modules hérités que l'arbre local avait
remplacés, restés sur la branche faute d'élagage.

Ce script fait le geste entier, en un appel :

* **export** — l'arbre local est recopié dans un dossier de travail (un clone),
  selon le `.gitignore` **du dépôt** : `.env` et le registre de rotation ne
  partent pas, et une règle ajoutée là-bas vaut aussitôt ici ;
* **élagage** — tout fichier suivi par la branche et absent du poste disparaît ;
* **commit** — en un seul, hooks du dépôt actifs (`--no-verify` n'est jamais
  passé) ;
* **push** — refspec **explicite** `branche:branche`, jamais un `git push` nu ;
* **vérification** — le distant est relu, et **chaque** fichier de la branche est
  comparé au fichier du poste par son empreinte de contenu (l'identifiant d'objet
  git, recalculé sur les octets d'ici). Seule exception déclarée et nommée : les
  fins de ligne, que `.gitattributes` normalise.

Rien n'est publié au hasard : `--dry-run` montre ce qui serait copié, élagué et
écarté sans rien écrire, et un fichier **nouveau** à la racine qui a la forme d'un
débris (vide, nom d'essai `_…`, sauvegarde d'éditeur) est **écarté et nommé** —
`--include-strays` est là pour l'assumer explicitement.

Codes de sortie : ``0`` publié et vérifié ; ``1`` refus ou défaillance (commit,
push, dérive constatée entre la branche et le poste) ; ``2`` mauvaise utilisation.

Exemples
--------

::

    # Ce qui partirait, sans rien toucher
    python scripts/publish_tree.py --dry-run

    # Publier, et vérifier fichier par fichier
    python scripts/publish_tree.py --url https://github.com/<compte>/<dépôt>.git

    # Une branche donnée, un message écrit à la main
    python scripts/publish_tree.py --branch import/arbre-local -m "Import du 30/09"

    # Sortie machine
    python scripts/publish_tree.py --json
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
from core import tree_publish  # noqa: E402

OK_MARK = "\u2705"
KO_MARK = "\u274c"
WARN_MARK = "\u26a0\ufe0f"

#: Combien de noms on montre avant de compter. Le rapport doit rester lisible ;
#: la liste intégrale est dans `--json`, qui est là pour ça.
MAX_NAMES = 12


def _some(names: Sequence[str]) -> str:
    """Les noms, plafonnés — et le reste compté, jamais tu."""
    shown = ", ".join(names[:MAX_NAMES])
    if len(names) > MAX_NAMES:
        shown += f" … (+{len(names) - MAX_NAMES} autre(s))"
    return shown


def report_lines(report: Dict[str, Any]) -> List[str]:
    """Le rapport lisible du geste — ce qui est parti, et la preuve que c'est parti."""
    built = report.get("plan")
    branch = report["branch"]
    lines = [
        f"Publication de l'arbre local sur {report['remote']}/{branch}",
        f"   poste       : {report['root']}",
        f"   dossier     : {report['work_tree']}",
    ]
    state = report.get("work_tree_state") or {}
    if state:
        origin = "cloné" if state.get("cloned") else "réutilisé"
        hooks = state.get("hooks") or []
        lines.append(
            f"   état        : {origin}, crochets actifs : "
            + (", ".join(hooks) if hooks else "aucun (pas de `.githooks` dans la branche)")
        )
        if state.get("resets"):
            lines.append(
                f"   ℹ️ {state['resets']} commit(s) local(aux) que le distant n'a pas : "
                "la branche est repositionnée sur le distant, et l'arbre du poste repart "
                "de là (aucun contenu n'est perdu — il vient du poste)"
            )

    if built is None:
        lines.append("")
        lines.append(f"{KO_MARK} aucun plan : rien n'a été lu, donc rien n'a été publié.")
        return _problems(lines, report)

    lines.append("")
    lines.append(
        f"1. Export : {len(built['published'])} fichier(s) retenu(s) — "
        f"{len(built['copies'])} à écrire, {len(built['identical'])} déjà identique(s)."
    )
    if built["excluded"]:
        lines.append(
            f"   • hors périmètre : {len(built['excluded'])} élément(s) — "
            f"{_some([item['path'] for item in built['excluded']])}"
        )
        by_reason: Dict[str, List[str]] = {}
        for item in built["excluded"]:
            by_reason.setdefault(item["reason"], []).append(item["path"])
        for reason, names in sorted(by_reason.items()):
            lines.append(f"       – {reason} : {_some(names)}")
    if built["strays"]:
        named = ", ".join(f"{item['path']} ({item['reason']})" for item in built["strays"])
        lines.append(f"   {WARN_MARK} écartés (suspects) : {named}")
        lines.append(
            "       à publier tout de même : --include-strays (un fichier vide ou un nom "
            "d'essai à la racine est un débris, pas un contenu)"
        )

    lines.append(
        f"2. Élagage : {len(built['prunes'])} fichier(s) suivi(s) par la branche et "
        "absent(s) du poste."
    )
    if built["prunes"]:
        lines.append(f"   • {_some(built['prunes'])}")

    if report["dry_run"]:
        lines.append("")
        lines.append(f"{WARN_MARK} essai à blanc : rien n'a été copié, committé ni poussé.")
        return _problems(lines, report)

    committed = report.get("commit") or {}
    if committed.get("sha"):
        lines.append(f"3. Commit : {committed['sha'][:12]} — {committed['files']} fichier(s).")
    else:
        lines.append("3. Commit : rien à committer, la branche est déjà le miroir du poste.")

    pushed = report.get("push") or {}
    if pushed:
        lines.append(f"4. Push : {' '.join(pushed['argv'])}")
        if pushed.get("output"):
            for line in pushed["output"].strip().splitlines():
                lines.append(f"   │ {line}")

    verification = report.get("verification")
    if verification:
        lines.append(f"5. Vérification : le distant porte {(report['remote_sha'] or '')[:12]}.")
        lines.append(
            f"   • {verification['files']} fichier(s) comparé(s) : "
            f"{len(verification['identical'])} identique(s), "
            f"{len(verification['normalized'])} normalisé(s), "
            f"{len(verification['drift'])} en dérive."
        )
        if verification["normalized"]:
            lines.append(
                "   • normalisé(s) par `.gitattributes` (fin de ligne) : "
                f"{_some(verification['normalized'])}"
            )
        for item in verification["drift"][:MAX_NAMES]:
            lines.append(
                f"   {KO_MARK} {item['path']} — poste {item['poste'][:12]}, "
                f"branche {item['branche'][:12]}"
            )
        if verification["missing_local"]:
            lines.append(
                f"   {KO_MARK} publié(s) sans équivalent au poste : "
                f"{_some(verification['missing_local'])}"
            )

    return _problems(lines, report)


def _problems(lines: List[str], report: Dict[str, Any]) -> List[str]:
    """Le verdict, en dernier — y compris quand il est mauvais."""
    lines.append("")
    if report["ok"]:
        lines.append(
            f"{OK_MARK} Publié et vérifié : chaque fichier de la branche a l'empreinte du poste."
        )
        return lines
    if report["problems"]:
        for problem in report["problems"]:
            lines.append(f"{KO_MARK} {problem}")
    elif report["dry_run"]:
        lines.append(f"{OK_MARK} Rien à signaler (essai à blanc).")
    else:
        lines.append(f"{KO_MARK} Le geste n'a pas abouti.")
    return lines


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="publish_tree.py",
        description=(
            "Recopie l'arbre local dans un clone, commite, pousse par refspec "
            "explicite (hooks actifs) puis vérifie l'empreinte de chaque fichier publié."
        ),
    )
    parser.add_argument(
        "--root",
        default=str(REPO_ROOT),
        help="Arbre local à publier (défaut : la racine du dépôt).",
    )
    parser.add_argument(
        "--work-tree",
        default=None,
        help="Clone de travail, réutilisé d'une publication à l'autre "
        "(défaut : un dossier du répertoire temporaire).",
    )
    parser.add_argument("--remote", default="origin", help="Nom du distant (défaut : origin).")
    parser.add_argument(
        "--url",
        default=None,
        help="URL du distant, utile seulement au premier clonage (dossier de travail absent).",
    )
    parser.add_argument(
        "--branch",
        default=tree_publish.DEFAULT_BRANCH,
        help=f"Branche publiée (défaut : {tree_publish.DEFAULT_BRANCH}).",
    )
    parser.add_argument(
        "-m",
        "--message",
        default=None,
        help="Message du commit. Sans lui, il est écrit à partir de ce que git a indexé.",
    )
    parser.add_argument(
        "--include-strays",
        action="store_true",
        help="Publier aussi les fichiers suspects de la racine (vide, `_…`, sauvegarde).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Montrer ce qui serait copié, élagué et écarté, sans rien écrire.",
    )
    parser.add_argument("--json", action="store_true", help="Sortie JSON uniquement.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    console.make_streams_utf8()
    args = _build_parser().parse_args(argv)

    root = Path(args.root).resolve()
    work_tree = (
        Path(args.work_tree).resolve() if args.work_tree else tree_publish.default_work_tree()
    )

    report = tree_publish.publish(
        root,
        work_tree,
        remote=args.remote,
        branch=args.branch,
        url=args.url,
        message=args.message,
        include_strays=args.include_strays,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        for line in report_lines(report):
            print(line)

    if args.dry_run:
        return 0 if report.get("plan") else 1
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
