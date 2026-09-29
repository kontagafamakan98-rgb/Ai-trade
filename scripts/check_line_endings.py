"""Fins de ligne : ce que `.editorconfig` déclare, vérifié sur tout le dépôt.

Le dépôt a déjà été converti **à l'envers**, et deux fois par la même cause :

* sous Windows, `core.autocrlf=true` réécrit les fichiers au checkout — c'est ce
  qui a laissé 71 fichiers (tout le Kotlin de l'interface, les `strings.xml`, six
  gros modules Python) en CRLF alors que `.editorconfig` déclare `end_of_line =
  lf` depuis le premier jour ;
* un script qui lit ou écrit en **mode texte** (`newline=None`) retraduit chaque
  `\\n` en `\\r\\n`. Un script de preuve de ce dépôt l'a fait sur un fichier
  Kotlin, et seules les empreintes SHA-256 s'en sont aperçues — la mise en forme,
  elle, était intacte.

D'où ce contrôle, qui lit les **octets** et jamais le texte : `\r` est un
caractère comme un autre, et aucun outil ne peut « voir » une fin de ligne pour
nous. Ce qu'il vérifie est **lu dans `.editorconfig`** (`end_of_line`,
`insert_final_newline`, `charset`) au lieu d'être recopié : changer la règle
là-bas change le contrôle, et réciproquement on ne durcit pas ici ce que la
configuration n'exige pas. Les sections sont appliquées **par fichier**, dans
l'ordre du fichier de configuration : `[*]` fournit le défaut, et une section plus
précise peut le remplacer pour ce qu'elle vise — `[*.{bat,cmd}] end_of_line =
crlf` est ainsi honorée plutôt que contournée, `cmd.exe` lisant mal un script de
commande dont les lignes finissent en LF. `trim_trailing_whitespace` n'est **pas**
vérifié : c'est une règle de mise en forme, tenue par les linters de chaque
langage (ruff, ktlint), pas une propriété binaire qu'un test puisse juger sans
faux positifs (les fichiers de documentation en contiennent légitimement).

    python scripts/check_line_endings.py          # vérifie (code 1 en cas de dérive)
    python scripts/check_line_endings.py --fix    # normalise, puis re-vérifie

Ce qu'est un fichier **texte** du dépôt n'est pas décidé ici : le vocabulaire
(binaires, dépendances, artefacts) vient de `core.secrets_audit`, où le scan
anti-fuite s'en sert déjà. Une exclusion nouvelle n'a donc qu'un seul endroit où
être écrite — et le bac à sable `.pgtest` s'y ajoute, puisque git l'ignore.
"""
from __future__ import annotations

import argparse
import fnmatch
import pathlib
import re
import sys
from typing import Dict, List, NamedTuple, Optional, Sequence, TextIO, Tuple

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # exécuté comme script : `core` doit être joignable
    sys.path.insert(0, str(REPO_ROOT))

from core import console, secrets_audit  # noqa: E402  (après l'ajustement de `sys.path`)

#: La section qui s'applique à **tout** ce qui n'est visé par aucune autre : ses
#: règles servent de défaut, et non de vérité unique. Une section plus spécifique
#: peut les remplacer fichier par fichier — c'est la précédence d'EditorConfig,
#: et c'est elle qui permet à un fichier déclaré à part d'être jugé selon SA
#: règle au lieu de l'être d'après le défaut du dépôt.
ROOT_SECTION = "*"

#: Les seules clés que ce contrôle interroge. Tout le reste d'une section (les
#: réglages de ktlint, ici) est lu puis ignoré : ce script n'a pas à connaître la
#: mise en forme, et une section qui n'en dit rien ne le concerne pas.
INTERESTING_KEYS = frozenset({"end_of_line", "insert_final_newline", "charset"})

#: Le bac à sable des preuves : git l'ignore, ce n'est pas du dépôt — et ses
#: fichiers sont écrits par des scripts, donc pas un modèle de propreté.
SANDBOX_DIR = ".pgtest"

EXCLUDED_DIRS = frozenset(secrets_audit.DEFAULT_SCAN_EXCLUDED_DIRS) | {SANDBOX_DIR}
EXCLUDED_SUFFIXES = frozenset(s.lower() for s in secrets_audit.DEFAULT_SCAN_EXCLUDED_SUFFIXES)

#: `end_of_line` → la séquence attendue entre deux lignes.
EOL_BYTES = {"lf": b"\n", "crlf": b"\r\n"}


class Violation(NamedTuple):
    """Un fichier et ce qui, dans ses octets, contredit la configuration."""

    path: str
    problem: str


class Rules(NamedTuple):
    """Les règles de fin de ligne déclarées, prises telles quelles dans la config.

    Un `NamedTuple` et non une `dataclass` : ce script est aussi chargé **par
    chemin** (`tests/test_line_endings.py`), cas où une `dataclass` échoue à
    s'enregistrer (elle cherche son module dans `sys.modules`).
    """

    end_of_line: str = "lf"
    insert_final_newline: bool = True
    charset: str = "utf-8"

    @property
    def separator(self) -> bytes:
        return EOL_BYTES[self.end_of_line]


def expand_braces(pattern: str) -> List[str]:
    """`*.{bat,cmd}` → `['*.bat', '*.cmd']` : `fnmatch` ne connaît pas les accolades.

    Sans cela, une section écrite avec des accolades ne correspondrait à rien —
    silencieusement, ce qui est la pire façon pour une règle de ne pas s'appliquer.
    """
    match = re.search(r"\{([^{}]*)\}", pattern)
    if match is None:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end() :]
    expanded: List[str] = []
    for option in match.group(1).split(","):
        expanded.extend(expand_braces(head + option.strip() + tail))
    return expanded


class Section(NamedTuple):
    """Une section de `.editorconfig` : des motifs, et **seulement** ce qu'elle déclare.

    `None` veut dire « cette section ne se prononce pas », ce qui n'est pas la
    même chose que « elle dit `lf` » : c'est la différence entre laisser le défaut
    du dépôt s'appliquer et le contredire.
    """

    patterns: Tuple[str, ...]
    root: bool = False
    end_of_line: Optional[str] = None
    insert_final_newline: Optional[bool] = None
    charset: Optional[str] = None

    def matches(self, relative: str) -> bool:
        """EditorConfig : un motif sans `/` se compare au **nom** du fichier.

        C'est ce qui fait que `[*.bat]` vaut à n'importe quelle profondeur, alors
        qu'un motif qui contient un `/` se compare au chemin relatif entier.
        """
        name = relative.rsplit("/", 1)[-1]
        return any(
            fnmatch.fnmatchcase(relative if "/" in pattern else name, pattern)
            for pattern in self.patterns
        )


def declared_sections(config: pathlib.Path) -> List[Section]:
    """Toutes les sections de `.editorconfig`, dans l'ordre du fichier.

    Une valeur `end_of_line` inconnue est refusée **ici**, même dans une section
    qui ne vise qu'une poignée de fichiers : deviner ce que `cr` a voulu dire
    serait pire que de le dire tout de suite — et une règle qu'on ne sait pas
    appliquer ne doit pas pouvoir passer pour appliquée.
    """
    sections: List[Section] = []
    patterns: Optional[List[str]] = None
    values: Dict[str, str] = {}

    def flush() -> None:
        if patterns is None:
            return
        end_of_line = values.get("end_of_line")
        if end_of_line is not None and end_of_line not in EOL_BYTES:
            raise ValueError(
                f"`end_of_line = {end_of_line}` n'est pas supporté "
                f"(attendu : {', '.join(sorted(EOL_BYTES))})"
            )
        sections.append(
            Section(
                patterns=tuple(patterns),
                root=list(patterns) == [ROOT_SECTION],
                end_of_line=end_of_line,
                insert_final_newline=(
                    None
                    if "insert_final_newline" not in values
                    else values["insert_final_newline"] == "true"
                ),
                charset=values.get("charset"),
            )
        )

    for raw in config.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            flush()
            patterns = expand_braces(line[1:-1].strip())
            values = {}
            continue
        if patterns is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key in INTERESTING_KEYS:
            values[key] = value.strip().lower()

    flush()
    return sections


def rules_for_file(
    relative: str,
    sections: Sequence[Section],
    defaults: Optional[Rules] = None,
) -> Rules:
    """La règle **effective** d'un fichier : les sections qui le concernent, dans l'ordre.

    La dernière section qui déclare une clé l'emporte, comme le veut
    EditorConfig. Un fichier visé par aucune section suit le défaut du dépôt —
    et c'est ce défaut, `[*]`, que `declared_rules` rapporte.
    """
    resolved = defaults if defaults is not None else Rules()
    for section in sections:
        if not section.matches(relative):
            continue
        resolved = Rules(
            end_of_line=section.end_of_line or resolved.end_of_line,
            insert_final_newline=(
                resolved.insert_final_newline
                if section.insert_final_newline is None
                else section.insert_final_newline
            ),
            charset=section.charset or resolved.charset,
        )
    return resolved


def declared_rules(config: pathlib.Path) -> Rules:
    """Les règles qui s'appliquent à un fichier qu'aucune section ne vise : `[*]`."""
    root_sections = [section for section in declared_sections(config) if section.root]
    return rules_for_file(ROOT_SECTION, root_sections, Rules())


def sections_for(root: pathlib.Path) -> List[Section]:
    """Les sections de `root/.editorconfig`, ou aucune : sans config, pas de règle."""
    config = root / ".editorconfig"
    return declared_sections(config) if config.is_file() else []


def is_binary(data: bytes) -> bool:
    """Un octet NUL : ce n'est pas du texte, on ne juge pas ses fins de ligne."""
    return b"\0" in data[:8000]


def text_files(root: pathlib.Path) -> List[pathlib.Path]:
    """Les fichiers texte du dépôt, dans l'ordre des chemins."""
    found: List[pathlib.Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        if is_binary(path.read_bytes()):
            continue
        found.append(path)
    return found


def ending_kinds(data: bytes) -> tuple[int, int, int]:
    """Compte les trois fins de ligne possibles : CRLF, LF seul, CR seul."""
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    lone_cr = data.count(b"\r") - crlf
    return crlf, lf, lone_cr


def inspect(path: pathlib.Path, rules: Rules) -> List[str]:
    """Ce qui, dans les octets de ce fichier, contredit les règles. Vide = conforme."""
    data = path.read_bytes()
    problems: List[str] = []
    crlf, lf, lone_cr = ending_kinds(data)

    if lone_cr:
        # Un `\r` sans `\n` : ce n'est aucune des deux conventions.
        problems.append(
            f"{lone_cr} retour(s) chariot isolé(s) : fins de ligne hétérogènes, "
            "à reprendre à la main"
        )
    elif crlf and lf:
        # Mélanger les deux styles n'est pas une conversion oubliée, c'est un
        # fichier qu'un outil a tronqué ou recollé : le réécrire mécaniquement
        # effacerait la trace de ce qui l'a produit.
        problems.append(
            f"{crlf} fin(s) de ligne CRLF et {lf} fin(s) de ligne LF : fins de "
            "ligne hétérogènes, à reprendre à la main"
        )
    elif rules.separator == b"\n" and crlf:
        problems.append(
            f"{crlf} fin(s) de ligne CRLF là où `.editorconfig` demande "
            f"`end_of_line = {rules.end_of_line}`"
        )
    elif rules.separator == b"\r\n" and lf:
        problems.append(
            f"{lf} fin(s) de ligne LF là où `.editorconfig` demande "
            f"`end_of_line = {rules.end_of_line}`"
        )

    if rules.insert_final_newline and data and not data.endswith(b"\n"):
        # Jugée sur la présence d'un **quelconque** terminateur, pas sur le bon :
        # un fichier en CRLF face à une règle `lf` est déjà signalé comme tel, et
        # le compter deux fois ferait deux reproches pour un seul défaut.
        problems.append("pas de fin de ligne finale (`insert_final_newline = true`)")

    if rules.charset == "utf-8":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            problems.append(f"octets qui ne sont pas de l'UTF-8 (`charset = utf-8`) : {exc}")

    return problems


def relative_name(path: pathlib.Path, root: pathlib.Path) -> str:
    """Le chemin du dépôt, en séparateurs POSIX.

    Sous Windows `str(path.relative_to(root))` rend des `\\`, et deux rapports
    d'un même dépôt ne se compareraient pas : un nom de fichier est une donnée
    du dépôt, pas une propriété de la machine qui l'examine.
    """
    return path.relative_to(root).as_posix()


def find_violations(
    root: pathlib.Path,
    rules: Rules,
    sections: Optional[Sequence[Section]] = None,
) -> List[Violation]:
    """Tout ce qui dérive, fichier par fichier — jamais un « environ N fichiers ».

    `rules` est le défaut du dépôt ; chaque fichier peut être jugé selon une règle
    plus précise si `.editorconfig` en déclare une pour lui.
    """
    resolved_sections = sections_for(root) if sections is None else sections
    violations: List[Violation] = []
    for path in text_files(root):
        name = relative_name(path, root)
        for problem in inspect(path, rules_for_file(name, resolved_sections, rules)):
            violations.append(Violation(name, problem))
    return violations


def _report(violations: Sequence[Violation], rules: Rules, stream: TextIO, limit: int = 20) -> None:
    print(
        f"Fins de ligne — `.editorconfig` (défaut `end_of_line = {rules.end_of_line}`) : "
        f"{len(violations)} problème(s) dans {len({v.path for v in violations})} fichier(s).",
        file=stream,
    )
    for violation in violations[:limit]:
        print(f"  ✗ {violation.path} : {violation.problem}", file=stream)
    if len(violations) > limit:
        print(f"  … et {len(violations) - limit} autre(s)", file=stream)
    print(
        "\nPour normaliser (l'opération écrit des **octets**, jamais du texte) :\n"
        "  python scripts/check_line_endings.py --fix",
        file=stream,
    )


def check(root: pathlib.Path = REPO_ROOT, rules: Optional[Rules] = None, stream: Optional[TextIO] = None) -> int:
    """Vérifie le dépôt : 0 si tout est conforme, 1 sinon. Ne touche à rien."""
    out = stream if stream is not None else sys.stdout
    resolved = rules if rules is not None else declared_rules(root / ".editorconfig")
    violations = find_violations(root, resolved)
    files = len(text_files(root))
    if violations:
        _report(violations, resolved, out)
        return 1
    print(
        f"Fins de ligne : {files} fichier(s) texte conforme(s) à `.editorconfig` "
        f"(défaut `{resolved.end_of_line}`, fin de ligne finale incluse).",
        file=out,
    )
    return 0


def normalize_file(path: pathlib.Path, rules: Rules) -> Optional[str]:
    """Réécrit un fichier en octets. Rend ce qui a été fait, ou `None` si rien."""
    data = path.read_bytes()
    crlf, lf, lone_cr = ending_kinds(data)
    if lone_cr or (crlf and lf):
        return None  # hétérogène : ce n'est pas à ce script de trancher

    # On convertit d'abord, on juge la fin de ligne finale ensuite : c'est le
    # fichier **tel qu'il sera écrit** qui doit se terminer par le bon séparateur.
    converted = data.replace(b"\r\n", b"\n")
    if rules.separator != b"\n":
        converted = converted.replace(b"\n", rules.separator)
    needs_final = (
        rules.insert_final_newline and bool(converted) and not converted.endswith(b"\n")
    )
    # La règle dit `crlf` : ce sont alors les `\n` seuls qu'il faut convertir.
    wrong = crlf if rules.separator == b"\n" else lf
    if not wrong and not needs_final:
        return None

    normalized = converted
    if needs_final:
        normalized += rules.separator
    # Le contenu **hors fins de ligne** doit être identique : c'est la garantie
    # qu'une normalisation n'est pas une réécriture. On compare les octets sans
    # `\r` et sans `\n`, pas seulement la taille.
    before = data.replace(b"\r\n", b"").replace(b"\n", b"").replace(b"\r", b"")
    after = normalized.replace(rules.separator, b"").replace(b"\n", b"")
    if before != after:  # pragma: no cover - garde-fou, jamais atteint
        raise AssertionError(f"{path} : la normalisation changerait autre chose")

    path.write_bytes(normalized)
    parts: List[str] = []
    if wrong:
        source = "CRLF" if rules.separator == b"\n" else "LF"
        parts.append(f"{wrong} fin(s) {source} → {rules.end_of_line.upper()}")
    if needs_final:
        parts.append("fin de ligne finale ajoutée")
    return ", ".join(parts)


def fix(root: pathlib.Path = REPO_ROOT, rules: Optional[Rules] = None, stream: Optional[TextIO] = None) -> int:
    """Normalise, puis **re-vérifie** : 0 si le dépôt est conforme après coup.

    Un fichier à fins de ligne hétérogènes est laissé tel quel et rapporté : le
    script ne devine pas ce qu'un outil a voulu faire.
    """
    out = stream if stream is not None else sys.stdout
    resolved = rules if rules is not None else declared_rules(root / ".editorconfig")
    changed: List[str] = []
    refused: List[Violation] = []
    sections = sections_for(root)

    for path in text_files(root):
        name = relative_name(path, root)
        try:
            done = normalize_file(path, rules_for_file(name, sections, resolved))
        except AssertionError as exc:  # pragma: no cover - garde-fou
            print(f"  ✗ {exc}", file=out)
            return 1
        if done:
            changed.append(f"  ✓ {name} : {done}")

    for violation in find_violations(root, resolved, sections):
        if "hétérogènes" in violation.problem:
            refused.append(violation)

    print(f"Fins de ligne : {len(changed)} fichier(s) normalisé(s).", file=out)
    for line in changed:
        print(line, file=out)
    if refused:
        print("\nNon touchés (à reprendre à la main) :", file=out)
        for violation in refused:
            print(f"  ✗ {violation.path} : {violation.problem}", file=out)
        return 1

    remaining = find_violations(root, resolved, sections)
    if remaining:
        _report(remaining, resolved, out)
        return 1
    print(
        f"Vérification après coup : conforme (`{resolved.end_of_line}`, "
        "sections de `.editorconfig` comprises).",
        file=out,
    )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    console.make_streams_utf8()
    parser = argparse.ArgumentParser(
        description="Vérifie (ou normalise) les fins de ligne face à `.editorconfig`.",
    )
    parser.add_argument("--root", default=str(REPO_ROOT), help="Racine examinée.")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Normalise au lieu de seulement vérifier.",
    )
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root)
    config = root / ".editorconfig"
    if not config.is_file():
        print(f"`.editorconfig` absent de {root} : rien à défendre.", file=sys.stderr)
        return 2
    try:
        rules = declared_rules(config)
    except ValueError as exc:
        print(f"Configuration illisible : {exc}", file=sys.stderr)
        return 2

    if args.fix:
        return fix(root, rules)
    return check(root, rules)


if __name__ == "__main__":
    sys.exit(main())
