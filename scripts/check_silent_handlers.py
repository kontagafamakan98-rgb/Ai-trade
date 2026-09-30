#!/usr/bin/env python
"""Aucun `except` silencieux qui ne soit déclaré, et enregistré.

Un `except` qui avale une panne sans rien en dire transforme une erreur en succès
apparent : c'est le mode de panne le plus cher du projet, parce que rien, plus
tard, ne le signale. Ce contrôle lit **tout** le code versionné — application
*et* tests — et refuse tout handler qui n'émet **aucun** signal.

Ce qu'est un signal, au sens de ce contrôle : de quoi être vu par quelqu'un
d'autre que la ligne fautive — un `raise`, un `return`, un `yield`, un `await`,
un `assert`, ou un **appel** (imprimer, journaliser, avertir…). Un repli légitime
se traite de deux façons, et de deux seulement :

* **nommer l'échec** : un handler qui capture l'exception (`as exc`) et **s'en
  sert** garde la cause ; il n'est donc pas muet même s'il n'imprime rien ;
* **déclarer le repli, et l'enregistrer** : un commentaire
  ``# sans signal : <raison>`` sur la ligne du `except` ou dans son corps nomme la
  raison, et ``--update`` la recopie dans l'inventaire **versionné**
  ``tests/silent_exceptions.json``.

Le contrôle **ne fait pas confiance au seul commentaire** : une exemption n'existe
que si elle figure dans l'inventaire, et l'inventaire doit correspondre
**exactement** au code. Déclarer un repli devient donc un acte explicite et
**relu** — le diff du JSON montre l'exemption ajoutée ou retirée, et le relecteur
la voit — au lieu d'un commentaire glissé dans un gros diff sans que personne ne
le remarque. Une exemption dont le handler a disparu (ou a cessé d'être muet) est
une **dérive** : l'inventaire ne peut pas pourrir en silence.

Ce qui est refusé, dans l'ordre :

1. un handler sans signal et sans justification — c'est le contrôle lui-même ;
2. un inventaire **absent**, illisible, ou qui ne correspond plus au code ;
3. un arbre lu à moitié : un fichier illisible, ou un parcours qui ne trouverait
   plus rien, fait **échouer** le contrôle plutôt que de le faire passer à vide.
   Un contrôle qui ne lit pas tout ne prouve rien sur ce qu'il n'a pas lu.

Usage :

    python scripts/check_silent_handlers.py            # vérifie (code 1 si refus)
    python scripts/check_silent_handlers.py --update   # enregistre les replis déclarés
    python scripts/check_silent_handlers.py --list     # montre l'inventaire enregistré
    python scripts/check_silent_handlers.py --json     # sortie exploitable par un script

Le fichier ``tests/silent_exceptions.json`` ne s'édite **jamais** à la main :
régénère-le. Il tient à la **bibliothèque standard** (comme les hooks), donc le
job CI l'exécute **avant** d'installer la moindre dépendance.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import pathlib
import sys
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Sequence, Tuple

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
# Lancé comme `python scripts/check_silent_handlers.py`, l'interpréteur met
# `scripts/` sur le chemin d'import, pas la racine : le module sortie y vit.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import console  # noqa: E402

#: Répertoires jamais lus : dépendances, caches, artefacts, et `.pgtest` (non
#: versionné, absent en CI). Le corpus doit lire le même arbre partout.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".pgtest",
        ".gradle",
        "build",
        "dist",
        "out",
        "target",
        "node_modules",
        "artifacts",
        ".freebuff",
        ".idea",
        ".vscode",
    }
)

#: Le mot qu'un handler sans signal doit porter, suivi de sa raison **sur la même
#: ligne**. Choisi pour être greppable et ne pas apparaître par accident.
MARKER = "sans signal"

#: Longueur minimale de la raison : « sans signal : ok » n'est pas une raison.
MIN_REASON = 10

#: Plancher de lecture : si le parcours se casse, le contrôle doit échouer, pas
#: passer à vide. Constaté : plus de 300 handlers sur le dépôt entier.
MIN_HANDLERS = 100

#: L'inventaire versionné des exemptions, relatif à la racine vérifiée.
DEFAULT_INVENTORY = pathlib.Path("tests") / "silent_exceptions.json"

UPDATE_COMMAND = "python scripts/check_silent_handlers.py --update"

#: Les clés d'une exemption — écrites ici, recopiées dans le message de refus.
ENTRY_KEYS = ("file", "handler", "reason", "symbol")

#: Nombre de lignes de diff affichées avant troncature.
DIFF_LIMIT = 40

_COMMENT = (
    "Inventaire versionné des `except` silencieux tolérés : ne pas éditer à la "
    f"main. Régénérer avec `{UPDATE_COMMAND}`."
)


class AuditError(Exception):
    """Le parcours n'a pas pu lire l'arbre : le contrôle refuse, il ne devine pas."""


class Harvest(NamedTuple):
    """Ce qu'un parcours de l'arbre a trouvé."""

    files: int
    handlers: int
    offenders: List[Tuple[str, int]]
    entries: List[Dict[str, str]]


class Verdict(NamedTuple):
    """Le résultat d'une vérification : ``ok``, ``refused`` ou ``error``."""

    status: str
    message: str
    report: Dict[str, Any]


# --------------------------------------------------------------------------- #
# Détecter : un handler muet, et sa raison éventuelle
# --------------------------------------------------------------------------- #


def source_files(root: pathlib.Path) -> List[pathlib.Path]:
    """Tous les `.py` de l'arbre, caches et dépendances exclus."""
    root = pathlib.Path(root)
    found: List[pathlib.Path] = []
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).parts[:-1]
        if any(part in SKIP_DIRS for part in parts):
            continue
        found.append(path)
    return found


def located_handlers(
    source: str,
) -> Iterator[Tuple[ast.ExceptHandler, str, bool]]:
    """Les handlers `except` d'une source, avec leur symbole englobant et `except*`."""
    tree = ast.parse(source)
    yield from _walk(tree, "", getattr(ast, "TryStar", None))


def _walk(
    node: ast.AST, qualname: str, try_star: Optional[type]
) -> Iterator[Tuple[ast.ExceptHandler, str, bool]]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nested = f"{qualname}.{child.name}" if qualname else child.name
            yield from _walk(child, nested, try_star)
        elif isinstance(child, ast.Try):
            for handler in child.handlers:
                yield handler, qualname or "<module>", False
            yield from _walk(child, qualname, try_star)
        elif try_star is not None and isinstance(child, try_star):
            for handler in child.handlers:
                yield handler, qualname or "<module>", True
            yield from _walk(child, qualname, try_star)
        else:
            yield from _walk(child, qualname, try_star)


def handlers(source: str):
    """Les handlers `except` d'une source, `except*` compris."""
    for handler, _symbol, _star in located_handlers(source):
        yield handler


def _observable(stmt: ast.stmt) -> bool:
    """Vrai si l'instruction est de quoi être vu par quelqu'un d'autre."""
    if isinstance(stmt, (ast.Raise, ast.Return, ast.Assert)):
        return True
    return any(
        isinstance(node, (ast.Call, ast.Yield, ast.YieldFrom, ast.Await))
        for node in ast.walk(stmt)
    )


def _uses_bound_name(handler: ast.ExceptHandler) -> bool:
    """Vrai si le handler nomme l'exception (`as exc`) et s'en sert."""
    name = handler.name
    if not name:
        return False
    return any(
        isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
        for node in ast.walk(handler)
    )


def is_silent(handler: ast.ExceptHandler) -> bool:
    """Un handler qui n'émet aucun signal et ne garde même pas la cause capturée."""
    if any(_observable(stmt) for stmt in handler.body):
        return False
    return not _uses_bound_name(handler)


def declared_reason(lines: List[str], handler: ast.ExceptHandler) -> Optional[str]:
    """La raison déclarée dans le corps du handler, ou ``None``.

    Un marqueur vide, ou plus court que ``MIN_REASON``, n'est pas une raison.
    """
    end = getattr(handler, "end_lineno", handler.lineno)
    for raw in lines[handler.lineno - 1 : end]:
        index = raw.lower().find(MARKER)
        if index == -1:
            continue
        reason = raw[index + len(MARKER) :].strip(" \t:-—")
        if len(reason) >= MIN_REASON:
            return reason
    return None


def handler_label(handler: ast.ExceptHandler, star: bool) -> str:
    """La clause d'un handler, canonique : ``except (A, B)``, ``except* X``, ``except``."""
    prefix = "except*" if star else "except"
    if handler.type is None:
        return prefix
    if isinstance(handler.type, ast.Tuple):
        body = "(" + ", ".join(ast.unparse(element) for element in handler.type.elts) + ")"
    else:
        body = ast.unparse(handler.type)
    return f"{prefix} {body}"


def undeclared_silences(source: str) -> List[int]:
    """Les lignes des handlers sans signal qui ne déclarent aucune raison."""
    lines = source.splitlines()
    return [
        handler.lineno
        for handler, _symbol, _star in located_handlers(source)
        if is_silent(handler) and declared_reason(lines, handler) is None
    ]


def exemptions(source: str, label: str) -> List[Dict[str, str]]:
    """Les exemptions déclarées dans une source : un handler muet qui se justifie."""
    lines = source.splitlines()
    found: List[Dict[str, str]] = []
    for handler, symbol, star in located_handlers(source):
        if not is_silent(handler):
            continue
        reason = declared_reason(lines, handler)
        if reason is None:
            continue
        found.append(
            {
                "file": label,
                "symbol": symbol,
                "handler": handler_label(handler, star),
                "reason": reason,
            }
        )
    return found


def collect(root: pathlib.Path) -> Harvest:
    """Parcourt l'arbre : ce qui est lu, ce qui refuse, et ce qui se justifie."""
    root = pathlib.Path(root)
    files = 0
    total = 0
    offenders: List[Tuple[str, int]] = []
    entries: List[Dict[str, str]] = []
    for path in source_files(root):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise AuditError(
                f"{_display(path)} est illisible ({exc}) : le contrôle refuse de l'ignorer, "
                "sinon un fichier non lu serait un fichier approuvé"
            ) from exc
        try:
            located = list(located_handlers(source))
        except SyntaxError as exc:
            raise AuditError(
                f"{_display(path)} ne se lit pas ({exc}) : le contrôle refuse d'ignorer "
                "un fichier qu'il n'a pas su analyser — l'approuver à l'aveugle serait pire"
            ) from exc
        files += 1
        label = path.relative_to(root).as_posix()
        lines = source.splitlines()
        for handler, symbol, star in located:
            total += 1
            if not is_silent(handler):
                continue
            reason = declared_reason(lines, handler)
            if reason is None:
                offenders.append((label, handler.lineno))
            else:
                entries.append(
                    {
                        "file": label,
                        "symbol": symbol,
                        "handler": handler_label(handler, star),
                        "reason": reason,
                    }
                )
    entries.sort(key=lambda entry: (entry["file"], entry["symbol"], entry["handler"], entry["reason"]))
    return Harvest(files, total, offenders, entries)


# --------------------------------------------------------------------------- #
# L'inventaire versionné
# --------------------------------------------------------------------------- #


def render(entries: Sequence[Dict[str, str]]) -> str:
    """Représentation canonique : JSON trié, UTF-8, deux espaces, saut final."""
    payload = {"_comment": _COMMENT, "exemptions": list(entries)}
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def read_recorded(path: pathlib.Path) -> Tuple[Optional[List[Dict[str, str]]], Optional[str]]:
    """(exemptions enregistrées, problème de lecture) — l'un des deux est ``None``."""
    path = pathlib.Path(path)
    if not path.is_file():
        return None, f"fichier absent : {_display(path)}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, f"{_display(path)} illisible ({exc})"
    except ValueError as exc:
        return None, f"{_display(path)} n'est pas du JSON valide ({exc})"
    if not isinstance(payload, dict) or not isinstance(payload.get("exemptions"), list):
        return None, f"{_display(path)} ne contient pas de liste « exemptions »"
    keys = set(ENTRY_KEYS)
    for entry in payload["exemptions"]:
        if not isinstance(entry, dict) or not keys.issubset(entry):
            return None, (
                f"{_display(path)} : une exemption ne porte pas les clés attendues "
                f"({', '.join(ENTRY_KEYS)})"
            )
    return payload["exemptions"], None


def write_inventory(root: pathlib.Path, inventory: pathlib.Path) -> Tuple[pathlib.Path, Harvest]:
    """Écrit l'inventaire depuis le code courant. Rend le chemin et le parcours."""
    harvest = collect(root)
    inventory = pathlib.Path(inventory)
    inventory.parent.mkdir(parents=True, exist_ok=True)
    # `newline="\n"` : le fichier doit être identique qu'on le régénère sous
    # Windows ou sous Linux (`.editorconfig` impose LF).
    inventory.write_text(render(harvest.entries), encoding="utf-8", newline="\n")
    return inventory, harvest


def _normalise(entries: Sequence[Dict[str, str]]):
    return sorted((tuple(sorted(entry.items())) for entry in entries), key=repr)


# --------------------------------------------------------------------------- #
# Vérifier, et dire ce qui a été refusé
# --------------------------------------------------------------------------- #


def check(
    root: pathlib.Path, inventory: pathlib.Path, min_handlers: int = MIN_HANDLERS
) -> Verdict:
    """Compare l'arbre et l'inventaire, ou explique le refus."""
    root = pathlib.Path(root)
    inventory = pathlib.Path(inventory)
    recorded, problem = read_recorded(inventory)
    try:
        harvest = collect(root)
    except AuditError as exc:
        return Verdict("error", f"[ÉCHEC] {exc}", {"ok": False, "error": str(exc)})

    report: Dict[str, Any] = {
        "ok": True,
        "inventory": _display(inventory),
        "files": harvest.files,
        "handlers": harvest.handlers,
        "computed": len(harvest.entries),
        "recorded": None if recorded is None else len(recorded),
        "drift": None,
        "offenders": [{"file": label, "line": line} for label, line in harvest.offenders],
    }

    if harvest.handlers < min_handlers:
        return Verdict(
            "error",
            _floor_message(root, harvest.handlers, min_handlers),
            {**report, "ok": False, "error": "corpus trop pauvre"},
        )

    # Le silence non déclaré se dit **en premier** : c'est le contrôle lui-même,
    #: et un inventaire absent ne doit pas masquer le fichier qui avale une panne.
    problems: List[str] = []
    if harvest.offenders:
        problems.append(_offenders_message(harvest.offenders))
    drifted = None
    if problem is not None:
        problems.append(_missing_message(inventory, problem))
    else:
        drifted = _normalise(recorded or []) != _normalise(harvest.entries)
        if drifted:
            problems.append(_drift_message(inventory, recorded or [], harvest.entries))
    report["drift"] = drifted
    if problems:
        return Verdict("refused", "\n\n".join(problems), {**report, "ok": False})
    return Verdict("ok", _ok_message(harvest), report)


def _ok_message(harvest: Harvest) -> str:
    return (
        f"[OK] aucun `except` silencieux non déclaré : {harvest.handlers} handler(s) "
        f"lu(s) dans {harvest.files} fichier(s), {len(harvest.entries)} exemption(s) "
        "déclarée(s) et enregistrée(s)."
    )


def _offenders_message(offenders: Sequence[Tuple[str, int]]) -> str:
    lines = [
        f"[ÉCHEC] {len(offenders)} handler(s) `except` sans signal et sans justification :",
        *[f"  - {label}:{line}" for label, line in offenders],
        "",
        "  Un `except` muet transforme une erreur en succès apparent. Nomme l'échec (un",
        "  appel, un `raise`, un `return`…), ou déclare le repli sur la ligne du `except`",
        "  ou dans son corps :",
        f"      # {MARKER} : <raison de {MIN_REASON} caractères au moins>",
        "  puis enregistre cette exemption dans l'inventaire versionné :",
        f"      {UPDATE_COMMAND}",
    ]
    return "\n".join(lines)


def _drift_message(
    inventory: pathlib.Path,
    recorded: Sequence[Dict[str, str]],
    computed: Sequence[Dict[str, str]],
) -> str:
    before = render(recorded).splitlines()
    after = render(computed).splitlines()
    diff = list(
        difflib.unified_diff(
            before,
            after,
            fromfile=f"{_display(inventory)} (versionné)",
            tofile=f"{_display(inventory)} (recalculé depuis le code)",
            lineterm="",
        )
    )
    shown = diff[:DIFF_LIMIT]
    if len(diff) > DIFF_LIMIT:
        shown.append(f"... {len(diff) - DIFF_LIMIT} lignes de diff en plus")
    return "\n".join(
        [
            "[ÉCHEC] l'inventaire des exemptions ne correspond plus au code.",
            f"  Fichier versionné : {_display(inventory)}",
            "",
            "  Différences :",
            *[f"    {line}" for line in shown],
            "",
            "  Si le changement est VOULU : régénère l'inventaire, relis le diff du JSON,",
            "  puis commite-le dans le MÊME commit que le code :",
            f"      {UPDATE_COMMAND}",
            "",
            "  Si le changement n'est PAS voulu, corrige le code : l'inventaire est la",
            "  référence des exemptions, et il ne s'édite jamais à la main.",
        ]
    )


def _missing_message(inventory: pathlib.Path, problem: str) -> str:
    return "\n".join(
        [
            f"[ÉCHEC] {problem}",
            "  Sans inventaire, il n'y a aucune exemption enregistrée : le contrôle refuse",
            "  de passer au vert, parce qu'un inventaire absent fait de tout repli déclaré",
            "  un silence, et d'un premier enregistrement un feu vert non vérifié.",
            "  Pour le créer à partir des replis déclarés dans le code :",
            f"      {UPDATE_COMMAND}",
            f"  Attendu : {_display(inventory)}",
        ]
    )


def _floor_message(root: pathlib.Path, handlers: int, minimum: int) -> str:
    return "\n".join(
        [
            f"[ÉCHEC] le contrôle n'a lu que {handlers} handler(s) dans {_display(root)},",
            f"  alors qu'il en attend au moins {minimum} : c'est trop peu pour être le",
            "  dépôt, donc le parcours s'est cassé — et un parcours cassé passerait au vert",
            "  sans rien lire. Vérifie la racine et les répertoires exclus, ou abaisse le",
            "  plancher explicitement (`--min-handlers N`).",
        ]
    )


# --------------------------------------------------------------------------- #
# Ligne de commande
# --------------------------------------------------------------------------- #


def _display(path: pathlib.Path) -> str:
    path = pathlib.Path(path)
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _parse(argv: Sequence[str]):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        default=str(REPO_ROOT),
        help="arbre à vérifier (défaut : la racine du dépôt)",
    )
    parser.add_argument(
        "--inventory",
        default="",
        help=f"inventaire des exemptions (défaut : <racine>/{DEFAULT_INVENTORY.as_posix()})",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="régénère l'inventaire depuis les replis déclarés dans le code",
    )
    parser.add_argument("--list", action="store_true", help="montre l'inventaire enregistré")
    parser.add_argument("--json", action="store_true", help="rapport exploitable par un script")
    parser.add_argument(
        "--min-handlers",
        type=int,
        default=MIN_HANDLERS,
        help=f"plancher de lecture, en handlers (défaut : {MIN_HANDLERS})",
    )
    return parser.parse_args(argv)


def _inventory_path(root: pathlib.Path, given: str) -> pathlib.Path:
    return pathlib.Path(given) if given else pathlib.Path(root) / DEFAULT_INVENTORY


def main(argv: Optional[Sequence[str]] = None) -> int:
    #: Sans cela, un message d'erreur non représentable en cp1252 ferait échouer
    #: l'affichage du contrôle lui-même — la panne serait donc dans le contrôle.
    console.make_streams_utf8()
    args = _parse(sys.argv[1:] if argv is None else argv)
    root = pathlib.Path(args.root)
    inventory = _inventory_path(root, args.inventory)

    if args.list:
        recorded, problem = read_recorded(inventory)
        if problem is not None:
            print(f"[ÉCHEC] {problem}", file=sys.stderr)
            return 1
        for entry in recorded or []:
            print(f"{entry['file']}:{entry['symbol']} — {entry['handler']} — {entry['reason']}")
        print(f"{len(recorded or [])} exemption(s) dans {_display(inventory)}")
        return 0

    if args.update:
        try:
            path, harvest = write_inventory(root, inventory)
        except AuditError as exc:
            print(f"[ÉCHEC] {exc}", file=sys.stderr)
            return 1
        print(f"écrit  {_display(path)} — {len(harvest.entries)} exemption(s) enregistrée(s)")
        if harvest.offenders:
            # Enregistrer ne doit pas absoudre : les silences non déclarés restent
            # des silences, et `--update` ne les invente pas — il le dit et échoue.
            print(_offenders_message(harvest.offenders), file=sys.stderr)
            return 1
        return 0

    verdict = check(root, inventory, args.min_handlers)
    if args.json:
        print(json.dumps(verdict.report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        stream = sys.stdout if verdict.status == "ok" else sys.stderr
        print(verdict.message, file=stream)
    return 0 if verdict.status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
