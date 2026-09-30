#!/usr/bin/env python
"""Hook **pre-commit** : bloque une valeur de secret avant qu'elle soit commitée.

Les valeurs des secrets configurés (`.env` puis environnement) sont recherchées
**verbatim** dans le contenu **indexé** par git. On lit l'index et non la copie
de travail : après un `git add -p`, ce qui part dans le commit n'est pas
forcément ce qui est affiché dans l'éditeur.

Différences assumées avec `scripts/verify_secrets.py` (audit pre-deploy) :

* **pre-deploy** = présence + robustesse + rotation + scan du dépôt ; c'est un
  contrôle d'environnement, il a besoin du registre de rotation et échoue
  légitimement sur une machine qui n'a rien configuré.
* **pre-commit** = uniquement le scan anti-fuite, sur ce qui va être commité.
  Il est rapide, ne dépend d'aucun secret configuré côté serveur, et ne bloque
  que sur une fuite réelle — sinon chaque `git commit` d'une machine non
  configurée deviendrait impossible, et le hook serait contourné en masse.

Le contrôle de rotation peut être ajouté ici avec ``--strict-rotation``.

Codes de sortie :

* ``0`` : rien à signaler, le commit peut continuer.
* ``1`` : fuite détectée (ou échec de rotation en ``--strict-rotation``) —
  **commit refusé**.
* ``2`` : mauvaise invocation (``--env-file`` introuvable, git indisponible
  pour ``--install``...).

Exemples
--------

::

    # Installation (une fois par clone)
    python scripts/pre_commit_secrets.py --install

    # Vérification manuelle de ce qui est indexé
    python scripts/pre_commit_secrets.py

    # Contrôle de rotation en plus du scan anti-fuite
    python scripts/pre_commit_secrets.py --strict-rotation
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import console  # noqa: E402
from core.secrets_audit import (  # noqa: E402
    DEFAULT_LEDGER_PATH,
    ERROR,
    git_staged_files,
    ledger_issues,
    load_effective_env,
    load_ledger,
    max_age_issues,
    resolve_max_age_days,
    run_audit,
    scannable_targets,
    scan_staged_for_secrets,
)

OK_MARK = "\u2705"
KO_MARK = "\u274c"
WARN_MARK = "\u26a0\ufe0f"

HOOKS_DIR = ".githooks"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pre_commit_secrets.py",
        description="Refuse un commit qui introduit une valeur de secret connue.",
    )
    parser.add_argument(
        "--root",
        default=str(REPO_ROOT),
        help="Racine du dépôt git à inspecter (défaut : racine du projet).",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Fichier de référence contenant les valeurs à rechercher "
        "(défaut : <root>/.env). L'environnement réel gagne sur ce fichier.",
    )
    parser.add_argument(
        "--no-env-file",
        action="store_true",
        help="N'utiliser que l'environnement du processus comme référence.",
    )
    parser.add_argument(
        "--strict-rotation",
        action="store_true",
        help="Exécute aussi les contrôles de présence/robustesse/rotation "
        "(bloquants). Par défaut ces contrôles restent au pre-deploy : le "
        "registre de rotation est propre à chaque environnement.",
    )
    parser.add_argument(
        "--ledger",
        default=None,
        help=f"Chemin du registre de rotation (défaut : {DEFAULT_LEDGER_PATH}).",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=None,
        help="Durée de vie maximale d'un secret avant rotation (défaut : 90).",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help=f"Configure `git config core.hooksPath {HOOKS_DIR}` dans --root puis sort.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Affiche les fichiers indexés et les valeurs de référence utilisées.",
    )
    parser.add_argument("--json", action="store_true", help="Sortie JSON uniquement.")
    return parser


class UsageError(Exception):
    """Erreur d'invocation (code de sortie 2)."""


def _install_hook(root: str) -> int:
    hooks_path = Path(root) / HOOKS_DIR
    missing = [
        name for name in ("pre-commit", "pre-push")
        if not (hooks_path / name).is_file()
    ]
    if missing:
        raise UsageError(
            "hook(s) introuvable(s) : "
            + ", ".join(str(hooks_path / name) for name in missing)
        )
    try:
        proc = subprocess.run(
            ["git", "-C", root, "config", "core.hooksPath", HOOKS_DIR],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UsageError(f"git indisponible : {exc}") from exc
    if proc.returncode != 0:
        raise UsageError(
            f"git config a échoué : {proc.stderr.decode('utf-8', errors='replace').strip()}"
        )
    print(f"{OK_MARK} hooks installés : core.hooksPath={HOOKS_DIR} (racine {root})")
    print("   • pre-commit : aucune valeur de secret dans ce qui est commité.")
    print("   • pre-push   : audit complet (présence, robustesse, rotation) avant diffusion.")
    return 0


def _resolve_env_file(args: argparse.Namespace) -> "str | None":
    if args.no_env_file:
        return None
    path = Path(args.env_file) if args.env_file else Path(args.root) / ".env"
    if not path.is_file():
        if args.env_file is not None:
            raise UsageError(f"fichier de référence introuvable : {path}")
        return None
    return str(path)


def main(argv: "list[str] | None" = None) -> int:
    console.make_streams_utf8()
    args = _build_parser().parse_args(argv)

    try:
        if args.install:
            return _install_hook(args.root)
        env_file = _resolve_env_file(args)
    except UsageError as exc:
        print(f"{KO_MARK} {exc}", file=sys.stderr)
        return 2

    staged = git_staged_files(args.root)
    if staged is None:
        # Pas de dépôt git (ou git absent) : rien à protéger à ce niveau.
        if args.json:
            print(json.dumps({"ok": True, "skipped": "no_git"}))
        elif args.verbose:
            print(f"{WARN_MARK} dépôt git introuvable sous {args.root} : rien à vérifier.")
        return 0

    values = load_effective_env(env_file)
    targets = scannable_targets(values)
    issues = scan_staged_for_secrets(values, root=args.root, staged_files=staged)

    strict_issues = []
    if args.strict_rotation:
        ledger_path = args.ledger or DEFAULT_LEDGER_PATH
        result = run_audit(
            values,
            ledger=load_ledger(ledger_path),
            max_age_days=resolve_max_age_days(args.max_age_days),
            scan_repo=False,
            environment_problems=[*ledger_issues(ledger_path), *max_age_issues()],
        )
        strict_issues = list(result.issues)

    blocking = [i for i in issues + strict_issues if i.severity == ERROR]

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not blocking,
                    "staged_files": staged,
                    "reference_secrets": [name for name, _ in targets],
                    "issues": [i.as_dict() for i in issues + strict_issues],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0 if not blocking else 1

    if not staged:
        if args.verbose:
            print(f"{OK_MARK} aucun fichier indexé.")
        return 0

    if not targets:
        # Aucun secret configuré : le scan n'a rien à chercher. Le dire
        # explicitement, sinon « aucune fuite détectée » ferait croire à un
        # contrôle effectif.
        print(
            f"{WARN_MARK} aucun secret configuré ({env_file or 'environnement'}) : "
            "le scan n'a aucune valeur de référence, aucun contrôle effectif n'a eu lieu."
        )
        print("   Renseigne .env pour que le hook puisse détecter une fuite.")
        return 0

    if args.verbose:
        print(
            f"Fichiers indexés ({len(staged)}) : {', '.join(staged[:20])}"
            + (" …" if len(staged) > 20 else "")
        )
        print(f"Valeurs de référence ({len(targets)}) : {', '.join(n for n, _ in targets)}")
        print("")

    for issue in issues + strict_issues:
        mark = KO_MARK if issue.severity == ERROR else WARN_MARK
        print(f"{mark} [{issue.name}] {issue.message}")

    if blocking:
        print("")
        print(
            f"{KO_MARK} COMMIT REFUSÉ : {len(blocking)} fuite(s) de secret dans les "
            f"{len(staged)} fichier(s) indexé(s)."
        )
        print(
            "   Retire la valeur du fichier (ou charge-la depuis l'environnement), "
            "puis réindexe : le commit repassera."
        )
        return 1

    if args.verbose and not issues:
        print(f"{OK_MARK} Aucune valeur de secret dans les {len(staged)} fichier(s) indexé(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
