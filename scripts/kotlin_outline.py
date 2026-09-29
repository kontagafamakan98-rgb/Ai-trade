#!/usr/bin/env python
"""Outil d'analyse structurelle pour le découpage des fichiers Compose.

Neutralise commentaires et chaînes (en préservant les positions), puis apparie
les accolades pour produire un plan des blocs `{ ... }` d'un fichier Kotlin.

Utilisation :
    python scripts/kotlin_outline.py app/src/main/java/com/aitrade/ui/Foo.kt
    python scripts/kotlin_outline.py Foo.kt --depth 2 --contains item
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # exécuté comme script : `core` doit être joignable
    sys.path.insert(0, str(REPO_ROOT))

from core import console  # noqa: E402  (après l'ajustement de `sys.path`)


def neutralize(text: str) -> str:
    """Remplace le contenu des commentaires et des chaînes par des espaces.

    La longueur totale et le nombre de lignes sont préservés : les positions
    restent donc alignées sur le fichier d'origine.
    """
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            out.append("  ")
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
            continue
        if ch == '"':
            # Chaîne, éventuellement triple-quotée. Les `${...}` d'un template
            # contiennent du code réel : on le neutralise aussi, car on ne
            # cherche ici que la structure des accolades.
            if text.startswith('"""', i):
                out.append("   ")
                i += 3
                while i < n and not text.startswith('"""', i):
                    out.append("\n" if text[i] == "\n" else " ")
                    i += 1
                if i < n:
                    out.append("   ")
                    i += 3
                continue
            out.append(" ")
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def line_offsets(text: str) -> List[int]:
    offsets = [0]
    for index, ch in enumerate(text):
        if ch == "\n":
            offsets.append(index + 1)
    return offsets


def line_of(offsets: List[int], position: int) -> int:
    lo, hi = 0, len(offsets) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if offsets[mid] <= position:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def brace_pairs(clean: str, offsets: List[int]) -> List[Tuple[int, int]]:
    stack: List[int] = []
    pairs: List[Tuple[int, int]] = []
    for index, ch in enumerate(clean):
        if ch == "{":
            stack.append(index)
        elif ch == "}":
            if stack:
                pairs.append((stack.pop(), index))
    return pairs


def strip_line_comments(text: str) -> str:
    """Retire les commentaires de fin de ligne, en préservant les chaînes.

    Les chaînes sont conservées à dessein : un template `"${strings.x}"`
    contient du code réellement exécuté.
    """
    out: List[str] = []
    for raw in text.splitlines(keepends=True):
        # Le saut de ligne est retiré avant la découpe puis rajouté : sans cela,
        # une ligne terminée par un commentaire perdait son `\n` et fusionnait
        # avec la suivante (les numéros de ligne se décalaient — bug vécu).
        if raw.endswith("\r\n"):
            line, ending = raw[:-2], "\r\n"
        elif raw.endswith("\n"):
            line, ending = raw[:-1], "\n"
        else:
            line, ending = raw, ""
        in_string = False
        index = 0
        cut = len(line)
        while index < len(line) - 1:
            ch = line[index]
            if in_string:
                if ch == "\\":
                    index += 2
                    continue
                if ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "/" and line[index + 1] == "/":
                cut = index
                break
            index += 1
        out.append(line[:cut] + ending)
    return "".join(out)


def identifiers(text: str) -> List[str]:
    """Découpe en identifiants, sans expression régulière."""
    found: List[str] = []
    current: List[str] = []
    for ch in text:
        if ch.isalnum() or ch == "_":
            current.append(ch)
        else:
            if current:
                found.append("".join(current))
                current = []
    if current:
        found.append("".join(current))
    return found


def usage(path: Path, names: List[str], ranges: List[Tuple[int, int]]) -> int:
    """Pour chaque plage, compte les noms de `names` réellement utilisés."""
    text = strip_line_comments(path.read_text(encoding="utf-8"))
    lines = text.splitlines()
    print(f"{path} — utilisation des variables")
    for start, end in ranges:
        counter: dict = {}
        for number in range(start, min(end, len(lines)) + 1):
            present = set(identifiers(lines[number - 1]))
            for name in names:
                if name in present:
                    counter[name] = counter.get(name, 0) + 1
        detail = ", ".join(f"{k}×{v}" for k, v in counter.items())
        print(f"  L{start}-L{end}: {detail or '(aucun)'}")
    return 0


def outline(path: Path, depth: Optional[int], contains: Optional[str]) -> int:
    text = path.read_text(encoding="utf-8")
    clean = neutralize(text)
    lines = text.splitlines()
    clean_lines = clean.splitlines()
    offsets = line_offsets(clean)
    pairs = brace_pairs(clean, offsets)

    rows = []
    for open_pos, close_pos in pairs:
        open_line = line_of(offsets, open_pos)
        close_line = line_of(offsets, close_pos)
        indent = len(clean_lines[open_line - 1]) - len(clean_lines[open_line - 1].lstrip())
        rows.append((open_line, close_line, indent, lines[open_line - 1].strip()))

    rows.sort()
    print(f"{path} — {len(lines)} lignes, {len(rows)} blocs")
    for open_line, close_line, indent, snippet in rows:
        level = indent // 4
        if depth is not None and level != depth:
            continue
        if contains and contains not in snippet:
            continue
        print(
            f"  L{open_line:>4}-L{close_line:<4} ({close_line - open_line + 1:>4} lignes) "
            f"niv.{level}  {snippet[:90]}"
        )
    return 0


def main(argv: "list[str] | None" = None) -> int:
    console.make_streams_utf8()
    parser = argparse.ArgumentParser(description="Plan des blocs d'un fichier Kotlin.")
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--depth", type=int, default=None, help="Niveau d'indentation du bloc ouvrant.")
    parser.add_argument("--contains", default=None, help="Filtre sur le texte de la ligne ouvrante.")
    parser.add_argument("--usage", default=None, help="Noms à rechercher, séparés par des virgules.")
    parser.add_argument(
        "--range",
        action="append",
        default=[],
        metavar="A-B",
        help="Plage de lignes à analyser (répétable) — pour le mode --usage.",
    )
    args = parser.parse_args(argv)

    if args.usage:
        names = [n.strip() for n in args.usage.split(",") if n.strip()]
        ranges = []
        for raw_range in args.range:
            head, _, tail = raw_range.partition("-")
            ranges.append((int(head), int(tail)))
        for raw in args.paths:
            usage(Path(raw), names, ranges)
        return 0

    for raw in args.paths:
        outline(Path(raw), args.depth, args.contains)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
