"""Refuse un fichier de workflow GitHub mal formé (`scripts/check_github_workflows.py`).

Un workflow YAML cassé ne produit **aucune erreur visible** : GitHub ignore le
fichier. Tous les jobs disparaissent d'un coup, et comme il n'y a plus rien à
exécuter, il n'y a plus rien à échouer — une branche verte avec zéro CI. Le
fichier le plus dangereux est donc `ci.yml` lui-même, celui dont on attendrait
qu'il prévienne : le seul endroit d'où la vérification peut encore parler est la
suite de tests, qui tourne localement **et** dans le job `python`.

Ce que l'outil refuse, dans l'ordre :

1. **YAML illisible** — indentation en tabulation, guillemet ou crochet non
   fermé, ligne plus indentée que ce que la structure attend ;
2. **clé définie deux fois** dans le même mapping. C'est le piège par excellence
   du format : un second `jobs:` (ou deux fois le job `python:`), **écrase** le
   premier en silence, et la moitié du fichier disparaît sans un mot ;
3. **structure GitHub invalide** — `on` ou `jobs` absent, `jobs` vide, clé
   étrangère en tête de fichier (un `one:` à la place de `on:` dispense de CI sans
   le dire), job sans `runs-on` ni `uses`, `steps` vide, étape qui porte à la fois
   `run` et `uses` (ou ni l'un ni l'autre), `needs:` qui nomme un job inexistant,
   service sans `image`.

Ce qu'il **n'est pas** : un parseur YAML. Il lit le sous-ensemble qu'emploient
les workflows — mappings et séquences en blocs, scalaires simples ou repliés,
scalaires blocs `|` / `>`, collections en flux sur une seule ligne — et il refuse
explicitement ce qu'il ne sait pas lire (ancres, alias, clés complexes, collection
en flux sur plusieurs lignes) au lieu de l'approuver à l'aveugle. Un refus visible
vaut mieux qu'un accord sans preuve, et c'est la règle du dépôt.

Bibliothèque standard uniquement : cette vérification doit pouvoir tourner dans
un job qui n'installe **aucune** dépendance.

Usage :

    python scripts/check_github_workflows.py              # tous les workflows
    python scripts/check_github_workflows.py --json       # sortie exploitable
    python scripts/check_github_workflows.py --root autre/depot

Codes de sortie, comme les autres outils du dépôt : `0` tout est valide, `1` au
moins un refus, `2` usage (racine sans `.github/workflows`).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # exécuté comme script : `core` doit être joignable
    sys.path.insert(0, str(PROJECT_ROOT))

from core import console  # noqa: E402  (après l'ajustement de `sys.path`)

WORKFLOWS = pathlib.Path(".github") / "workflows"
SUFFIXES = (".yml", ".yaml")

#: Ce qu'un workflow peut porter **en tête de fichier**. La liste est courte et
#: stable : une clé étrangère est presque toujours une faute de frappe, et
#: `one:` au lieu de `on:` supprime tout déclenchement sans le dire.
TOP_LEVEL_KEYS = frozenset(
    {"name", "run-name", "on", "permissions", "env", "defaults", "concurrency", "jobs"}
)
#: Les clés d'un job (documentation GitHub Actions, section `jobs.<job_id>`).
JOB_KEYS = frozenset(
    {
        "name",
        "permissions",
        "needs",
        "if",
        "runs-on",
        "environment",
        "concurrency",
        "outputs",
        "env",
        "defaults",
        "steps",
        "timeout-minutes",
        "strategy",
        "continue-on-error",
        "container",
        "services",
        "uses",
        "with",
        "secrets",
    }
)
#: Les clés d'une étape.
STEP_KEYS = frozenset(
    {
        "name",
        "id",
        "if",
        "uses",
        "run",
        "working-directory",
        "shell",
        "with",
        "env",
        "continue-on-error",
        "timeout-minutes",
    }
)
#: Les clés d'un service (`jobs.<job_id>.services`).
SERVICE_KEYS = frozenset({"image", "env", "ports", "options", "credentials", "volumes"})

#: Un identifiant de job : GitHub refuse le reste, et un `id` refusé emporte le
#: job entier.
JOB_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

#: Un scalaire bloc : `|`, `>-`, `|+2`, `>2-`…
BLOCK_SCALAR = re.compile(r"^[|>][0-9+-]*$")

#: Les constats, nommés dans le rapport (comme `check_schema_drift.py`).
BROKEN_YAML = "YAML illisible"
DUPLICATE_KEY = "clé définie deux fois"
UNKNOWN_KEY = "clé inconnue"
MISSING_KEY = "clé obligatoire absente"
EMPTY = "section vide"
BAD_JOB = "job invalide"
BAD_STEP = "étape invalide"
BAD_SERVICE = "service invalide"
UNKNOWN_NEEDS = "référence inconnue"
UNSUPPORTED = "non supporté"

OK = "✅"
FAIL = "❌"


@dataclass
class Line:
    """Une ligne logique : son numéro, son indentation, son contenu sans commentaire."""

    number: int
    indent: int
    text: str

    @property
    def dash(self) -> bool:
        """La ligne ouvre-t-elle un élément de séquence ?"""
        return self.text == "-" or self.text.startswith("- ")


@dataclass
class Node:
    """Un nœud du sous-ensemble YAML lu : mapping, séquence, scalaire ou rien."""

    kind: str
    line: int
    value: Any = None
    text: str = ""
    #: Pour un mapping : la ligne de chaque clé (c'est ce qui permet de nommer la
    #: première définition d'une clé écrite deux fois).
    lines: Dict[str, int] = field(default_factory=dict)
    #: Le nom du fichier, pour que chaque refus dise **où** il a été vu.
    file: str = ""

    def keys(self) -> Sequence[str]:
        return list(self.value) if self.kind == "map" else []

    def get(self, key: str) -> Optional["Node"]:
        if self.kind != "map":
            return None
        return self.value.get(key)

    def entries(self) -> Sequence["Node"]:
        return list(self.value) if self.kind == "seq" else []

    def as_scalar(self) -> Optional[str]:
        """La valeur, si c'est un scalaire — `None` pour une structure vide."""
        if self.kind == "scalar":
            return self.text
        if self.kind == "null":
            return ""
        return None


def findings(kind: str, line: int, detail: str) -> Dict[str, Any]:
    """Un refus : sa sorte, sa ligne, et ce qui se passerait si on laissait passer."""
    return {"kind": kind, "line": line, "detail": detail}


# --------------------------------------------------------------------------- #
# Lecture du sous-ensemble YAML employé par les workflows
# --------------------------------------------------------------------------- #


def _unbalanced(text: str) -> Optional[str]:
    """Ce qui n'est pas refermé sur cette ligne, ou `None`.

    Un guillemet n'**ouvre** que là où YAML l'attend (début d'un scalaire, après
    `:` , après `-`, `[`, `{` ou `,`) : sans cette règle, `name: l'app` serait
    refusé à cause de l'apostrophe française.
    """
    quote: Optional[str] = None
    depth = {"[": 0, "{": 0}
    pairs = {"]": "[", "}": "{"}
    previous = ""
    index = 0
    while index < len(text):
        character = text[index]
        if quote is not None:
            if character == "\\" and quote == '"' and index + 1 < len(text):
                index += 2
                continue
            if character == quote:
                quote = None
            index += 1
            continue
        if character in "\"'" and (previous == "" or previous in ":-,([{"):
            quote = character
        elif character in depth and not quote:
            depth[character] += 1
        elif character in pairs:
            if depth[pairs[character]] == 0:
                return f"« {character} » fermé alors qu'aucun « {pairs[character]} » n'est ouvert"
            depth[pairs[character]] -= 1
        if character != " ":
            previous = character
        index += 1
    if quote is not None:
        return f"guillemet {quote} non fermé"
    for opener, count in depth.items():
        if count:
            return f"« {opener} » non fermé"
    return None


def _strip_comment(text: str) -> str:
    """Le contenu sans son commentaire : un `#` hors guillemets, après un espace."""
    quote: Optional[str] = None
    previous = ""
    index = 0
    while index < len(text):
        character = text[index]
        if quote is not None:
            if character == "\\" and quote == '"' and index + 1 < len(text):
                index += 2
                previous = character
                continue
            if character == quote:
                quote = None
        elif character in "\"'" and (previous == "" or previous in ":-,([{"):
            quote = character
        elif character == "#" and (previous == "" or previous in " \t"):
            return text[:index].rstrip()
        if character != " ":
            previous = character
        index += 1
    return text.rstrip()


def _logical_lines(text: str, findings_: List[Dict[str, Any]]) -> List[Line]:
    """Les lignes qui portent du contenu : indentation mesurée, commentaires retirés."""
    lines: List[Line] = []
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.rstrip()
        if not stripped.strip() or stripped.strip() in ("---", "..."):
            continue
        leading = stripped[: len(stripped) - len(stripped.lstrip(" \t"))]
        if "\t" in leading:
            findings_.append(
                findings(
                    BROKEN_YAML,
                    number,
                    "indentation en tabulation : YAML l'interdit, le fichier serait ignoré "
                    "en entier",
                )
            )
            return lines
        content = _strip_comment(stripped[len(leading) :])
        if not content:
            continue
        unbalanced = _unbalanced(content)
        if unbalanced:
            findings_.append(findings(BROKEN_YAML, number, f"{unbalanced} sur cette ligne"))
            return lines
        lines.append(Line(number=number, indent=len(leading), text=content))
    return lines


def _split_entry(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """`(clé, valeur)` d'une entrée de mapping, ou `None` si ce n'en est pas une.

    Le `:` séparateur doit être **suivi d'une espace** (règle YAML en bloc) : sans
    elle, `- 5432:5432` est un scalaire, pas une clé, et le confondre ferait
    refuser un fichier juste.
    """
    quote: Optional[str] = None
    depth = 0
    previous = ""
    index = 0
    while index < len(text):
        character = text[index]
        if quote is not None:
            if character == "\\" and quote == '"' and index + 1 < len(text):
                index += 2
                continue
            if character == quote:
                quote = None
        elif character in "\"'" and (previous == "" or previous in ":-,([{"):
            quote = character
        elif character in "[{":
            depth += 1
        elif character in "]}":
            depth -= 1
        elif character == ":" and depth == 0:
            if index + 1 == len(text) or text[index + 1] in " \t":
                key = text[:index].strip()
                if not key:
                    return None
                value = text[index + 1 :].strip()
                return _unquote(key), (value or None)
        if character != " ":
            previous = character
        index += 1
    return None


def _unquote(text: str) -> str:
    """Retire les guillemets d'encadrement, s'il y en a."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _block(lines: List[Line], index: int, indent: int, findings_: List[Dict[str, Any]]) -> Tuple[Node, int]:
    """Le bloc qui commence à `index`, à l'indentation `indent`."""
    line = lines[index]
    if line.dash:
        return _sequence(lines, index, indent, findings_)
    entry = _split_entry(line.text)
    if entry is None:
        findings_.append(
            findings(
                UNSUPPORTED,
                line.number,
                f"ligne inattendue dans un bloc : « {line.text} » — un scalaire replié "
                "sur plusieurs lignes doit s'écrire en scalaire bloc (`|` ou `>`)",
            )
        )
        return Node("null", line.number), index + 1
    return _mapping(lines, index, indent, findings_)


def _sequence(
    lines: List[Line], index: int, indent: int, findings_: List[Dict[str, Any]]
) -> Tuple[Node, int]:
    """Une séquence d'éléments, à l'indentation `indent`."""
    node = Node("seq", lines[index].number, value=[])
    while index < len(lines) and lines[index].indent == indent and lines[index].dash:
        line = lines[index]
        rest = line.text[1:]
        offset = indent + 1 + (len(rest) - len(rest.lstrip(" ")))
        rest = rest.strip()
        index += 1
        if not rest:
            if index < len(lines) and lines[index].indent > indent:
                item, index = _block(lines, index, lines[index].indent, findings_)
            else:
                item = Node("null", line.number)
            node.value.append(item)
            continue
        if _split_entry(rest) is None:
            # Un élément **scalaire** : `- 5432:5432` en est un (YAML n'y voit un
            # mapping que si le `:` est suivi d'une espace), et le refuser ferait
            # refuser un fichier que GitHub exécute très bien.
            if index < len(lines) and lines[index].indent > indent:
                findings_.append(
                    findings(
                        UNSUPPORTED,
                        lines[index].number,
                        f"la valeur de « {rest} » continue sur les lignes suivantes : un "
                        "scalaire replié doit s'écrire en scalaire bloc (`|` ou `>`)",
                    )
                )
                while index < len(lines) and lines[index].indent > indent:
                    index += 1
            node.value.append(Node("scalar", line.number, text=rest))
            continue
        # Sinon, le contenu de l'élément est rejoué comme une ligne à sa propre
        # colonne : `- name: x` devient `  name: x`, et le mapping continue avec
        # les lignes suivantes à la même colonne. C'est la même information, sans
        # grammaire supplémentaire.
        synthetic = Line(number=line.number, indent=offset, text=rest)
        lines.insert(index, synthetic)
        item, index = _block(lines, index, offset, findings_)
        node.value.append(item)
    return node, index


def _mapping(
    lines: List[Line], index: int, indent: int, findings_: List[Dict[str, Any]]
) -> Tuple[Node, int]:
    """Un mapping, à l'indentation `indent`."""
    node = Node("map", lines[index].number, value={}, lines={})
    while index < len(lines) and lines[index].indent == indent and not lines[index].dash:
        line = lines[index]
        entry = _split_entry(line.text)
        if entry is None:
            findings_.append(
                findings(
                    UNSUPPORTED,
                    line.number,
                    f"ligne inattendue dans un mapping : « {line.text} » — un scalaire "
                    "replié sur plusieurs lignes doit s'écrire en scalaire bloc",
                )
            )
            index += 1
            continue
        key, value = entry
        if key in node.value:
            findings_.append(
                findings(
                    DUPLICATE_KEY,
                    line.number,
                    f"« {key} » est déjà défini ligne {node.lines[key]} : la seconde "
                    "définition écrase la première en silence",
                )
            )
        node.lines[key] = node.lines.get(key, line.number)
        index += 1
        if value is None:
            if index < len(lines) and (
                lines[index].indent > indent
                or (lines[index].indent == indent and lines[index].dash)
            ):
                child, index = (
                    _sequence(lines, index, indent, findings_)
                    if lines[index].indent == indent
                    else _block(lines, index, lines[index].indent, findings_)
                )
            else:
                child = Node("null", line.number)
            node.value[key] = child
        elif BLOCK_SCALAR.match(value):
            while index < len(lines) and lines[index].indent > indent:
                index += 1
            node.value[key] = Node("scalar", line.number, text=value)
        else:
            node.value[key] = Node("scalar", line.number, text=value)
    return node, index


def parse(text: str, path: pathlib.Path, findings_: List[Dict[str, Any]]) -> Optional[Node]:
    """Le document lu, ou `None` si le fichier est illisible."""
    lines = _logical_lines(text, findings_)
    if not lines:
        if not findings_:
            findings_.append(
                findings(BROKEN_YAML, 1, "aucun contenu : un workflow vide ne définit aucun job")
            )
        return None
    if lines[0].indent:
        findings_.append(
            findings(
                BROKEN_YAML,
                lines[0].number,
                "le fichier commence par une ligne indentée : YAML n'accepte pas "
                "d'indentation à la racine",
            )
        )
        return None
    node, index = _block(lines, 0, 0, findings_)
    if index < len(lines):
        # Des lignes que la structure n'a pas absorbées : plus indéntées que ce que
        # l'on attendait, donc rattachées à rien. Les ignorer reviendrait à
        # approuver un fichier qu'on n'a pas compris — le défaut exact que cet
        # outil existe pour interdire.
        findings_.append(
            findings(
                UNSUPPORTED,
                lines[index].number,
                f"ligne non rattachée à la structure : « {lines[index].text} » — son "
                "indentation ne correspond à aucune section, et elle serait perdue",
            )
        )
    return node


# --------------------------------------------------------------------------- #
# Ce que GitHub exige, une fois le document lu
# --------------------------------------------------------------------------- #


def _check_maps_against(
    node: Node,
    allowed: frozenset,
    what: str,
    findings_: List[Dict[str, Any]],
) -> None:
    """Refuse les clés hors de `allowed`, en disant ce que la faute coûterait."""
    for key in node.keys():
        if key not in allowed:
            findings_.append(
                findings(
                    UNKNOWN_KEY,
                    node.lines[key],
                    f"clé inconnue `{key}` dans {what} — GitHub refuserait {what}, "
                    f"et un fichier refusé ne lance aucun job",
                )
            )


def _check_job(
    job_id: str,
    job: Node,
    identifiers: Sequence[str],
    findings_: List[Dict[str, Any]],
) -> None:
    if job.kind != "map":
        findings_.append(
            findings(BAD_JOB, job.line, f"le job `{job_id}` n'est pas un mapping de clés")
        )
        return
    if not JOB_ID.match(job_id):
        findings_.append(
            findings(
                BAD_JOB,
                job.line,
                f"l'identifiant de job `{job_id}` n'est pas valide (lettre ou `_` pour "
                "commencer, puis lettres, chiffres, `-` et `_`)",
            )
        )
    _check_maps_against(job, JOB_KEYS, "un job", findings_)
    _check_services(job_id, job, findings_)
    _check_needs(job_id, job, identifiers, findings_)

    uses = job.get("uses")
    steps = job.get("steps")
    if uses is not None:
        if steps is not None:
            findings_.append(
                findings(
                    BAD_JOB,
                    steps.line,
                    f"le job `{job_id}` porte à la fois `uses` et `steps` : GitHub refuse "
                    "les deux ensemble",
                )
            )
        return

    if job.get("runs-on") is None:
        findings_.append(
            findings(
                MISSING_KEY,
                job.line,
                f"le job `{job_id}` n'a ni `runs-on` ni `uses` : il n'a aucune machine "
                "où s'exécuter",
            )
        )
    if steps is None:
        findings_.append(
            findings(
                MISSING_KEY,
                job.line,
                f"le job `{job_id}` n'a pas de `steps` : il ne ferait rien",
            )
        )
        return
    if steps.kind != "seq" or not steps.value:
        findings_.append(
            findings(
                EMPTY,
                steps.line,
                f"`steps` du job `{job_id}` est vide : le job ne ferait rien",
            )
        )
        return

    runs = 0
    for step in steps.value:
        if step.kind != "map":
            findings_.append(
                findings(BAD_STEP, step.line, f"une étape du job `{job_id}` n'est pas un mapping")
            )
            continue
        _check_maps_against(step, STEP_KEYS, "une étape", findings_)
        has_run = step.get("run") is not None
        has_uses = step.get("uses") is not None
        runs += 1
        name = _step_name(step)
        if has_run == has_uses:
            findings_.append(
                findings(
                    BAD_STEP,
                    step.line,
                    f"l'étape {name} du job `{job_id}` porte "
                    + ("`run` **et** `uses`" if has_run else "ni `run` ni `uses`")
                    + " : GitHub demande exactement l'un des deux",
                )
            )


def _check_services(job_id: str, job: Node, findings_: List[Dict[str, Any]]) -> None:
    services = job.get("services")
    if services is None or services.kind != "map":
        return
    for name in services.keys():
        service = services.get(name)
        if service is None or service.kind != "map":
            findings_.append(
                findings(
                    BAD_SERVICE,
                    services.lines[name],
                    f"le service `{name}` du job `{job_id}` n'est pas un mapping",
                )
            )
            continue
        _check_maps_against(service, SERVICE_KEYS, "un service", findings_)
        if service.get("image") is None:
            findings_.append(
                findings(
                    BAD_SERVICE,
                    service.line,
                    f"le service `{name}` du job `{job_id}` n'a pas d'`image` : GitHub ne "
                    "saurait pas quoi démarrer",
                )
            )


def _flow_items(text: str) -> List[str]:
    """Les éléments d'une collection en flux tenue sur une ligne : `[a, b]`.

    `needs: [python]` est la façon la plus courante d'écrire une dépendance, et
    la lire comme le nom d'un job `[python]` ferait refuser un fichier juste.
    """
    body = text.strip()
    if not (body.startswith("[") and body.endswith("]")):
        return []
    return [_unquote(piece.strip()) for piece in body[1:-1].split(",") if piece.strip()]


def _check_needs(
    job_id: str, job: Node, identifiers: Sequence[str], findings_: List[Dict[str, Any]]
) -> None:
    needs = job.get("needs")
    if needs is None:
        return
    if needs.kind == "scalar":
        wanted = _flow_items(needs.text) or [needs.text]
    elif needs.kind == "seq":
        wanted = [item.as_scalar() or "" for item in needs.value]
    else:
        findings_.append(
            findings(UNKNOWN_NEEDS, needs.line, f"`needs` du job `{job_id}` n'est pas lisible")
        )
        return
    for name in wanted:
        if not name:
            continue
        if name not in identifiers:
            findings_.append(
                findings(
                    UNKNOWN_NEEDS,
                    needs.line,
                    f"le job `{job_id}` dépend de `{name}`, qui n'existe pas : GitHub "
                    "refuserait le workflow entier",
                )
            )


def _step_name(step: Node) -> str:
    """Une étape dite en mots : son `name`, à défaut son `id`, à défaut sa ligne."""
    for key in ("name", "id"):
        value = step.get(key)
        if value is not None and value.as_scalar():
            return f"« {value.as_scalar()} »"
    return f"ligne {step.line}"


def check_document(node: Node, findings_: List[Dict[str, Any]]) -> Dict[str, int]:
    """Refuse ce que GitHub refuserait, et compte ce qui a été vérifié."""
    counted = {"jobs": 0, "steps": 0}
    if node.kind != "map":
        findings_.append(
            findings(BROKEN_YAML, node.line, "la racine du fichier n'est pas un mapping de clés")
        )
        return counted
    _check_maps_against(node, TOP_LEVEL_KEYS, "un workflow", findings_)

    if node.get("on") is None:
        findings_.append(
            findings(
                MISSING_KEY,
                node.line,
                "aucun déclencheur `on:` : le workflow existerait sans jamais se lancer "
                "(une faute de frappe comme `one:` a exactement cet effet)",
            )
        )
    jobs = node.get("jobs")
    if jobs is None:
        findings_.append(
            findings(
                MISSING_KEY,
                node.line,
                "aucune section `jobs:` : le workflow ne définirait aucun job",
            )
        )
        return counted
    if jobs.kind != "map" or not jobs.value:
        findings_.append(
            findings(EMPTY, jobs.line, "`jobs:` est vide : le workflow ne lancerait rien")
        )
        return counted

    # `needs:` ne se vérifie pas job par job, mais contre l'ensemble des
    # identifiants : la liste est donc calculée une fois et passée à chacun.
    identifiers = tuple(jobs.keys())
    for job_id in jobs.keys():
        job = jobs.get(job_id)
        _check_job(job_id, job, identifiers, findings_)
        counted["jobs"] += 1
        steps = job.get("steps") if job.kind == "map" else None
        if steps is not None and steps.kind == "seq":
            counted["steps"] += len(steps.value)
    return counted


def check_file(path: pathlib.Path) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Les refus d'un fichier, et ce qui a été vérifié dedans."""
    findings_: List[Dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    node = parse(text, path, findings_)
    counted = {"jobs": 0, "steps": 0}
    if node is not None and not any(
        finding["kind"] in (BROKEN_YAML, UNSUPPORTED) for finding in findings_
    ):
        counted = check_document(node, findings_)
    for finding in findings_:
        finding["file"] = str(path)
    return findings_, counted


def workflow_files(root: pathlib.Path) -> List[pathlib.Path]:
    """Les workflows du dépôt, dans l'ordre — `.yml` et `.yaml`."""
    directory = root / WORKFLOWS
    if not directory.is_dir():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix in SUFFIXES
    )


def render(findings_: List[Dict[str, Any]], counted: Dict[str, int], root: pathlib.Path) -> str:
    """Le rapport, en clair : une ligne par refus, et ce qui a été lu."""
    files = ", ".join(sorted(_relative(path, root) for path in counted["files"])) or "aucun"
    lines = [
        f"Workflows GitHub — {len(findings_)} refus",
        f"Racine : {root}",
        f"Lu : {len(counted['files'])} fichier(s) ({files}), "
        f"{counted['jobs']} job(s), {counted['steps']} étape(s)",
        "",
    ]
    if not findings_:
        lines.append(f"{OK} Tous les workflows sont valides : GitHub les lancerait.")
        return "\n".join(lines)
    for finding in findings_:
        relative = _relative(finding["file"], root)
        lines.append(f"  {FAIL} {relative}:{finding['line']} — {finding['kind']} : {finding['detail']}")
    lines.append("")
    counted_kinds = ", ".join(
        f"{count} {kind}" for kind, count in sorted(_kinds(findings_).items())
    )
    lines.append(f"{FAIL} {len(findings_)} refus : {counted_kinds}.")
    if _kinds(findings_).get(BROKEN_YAML):
        lines.append(
            "  Un fichier illisible est **ignoré** par GitHub : aucun job ne tourne, "
            "et rien n'échoue — c'est ce silence que cette vérification remplace."
        )
    return "\n".join(lines)


def _kinds(findings_: List[Dict[str, Any]]) -> Dict[str, int]:
    counted: Dict[str, int] = {}
    for finding in findings_:
        counted[finding["kind"]] = counted.get(finding["kind"], 0) + 1
    return counted


def _relative(path: str, root: pathlib.Path) -> str:
    try:
        return str(pathlib.Path(path).resolve().relative_to(root.resolve()))
    except ValueError:
        return path


def audit(root: pathlib.Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Les refus de tous les workflows, et le contexte de la lecture."""
    files = workflow_files(root)
    findings_: List[Dict[str, Any]] = []
    counted = {"files": [str(path) for path in files], "jobs": 0, "steps": 0}
    for path in files:
        file_findings, file_counted = check_file(path)
        findings_.extend(file_findings)
        counted["jobs"] += file_counted["jobs"]
        counted["steps"] += file_counted["steps"]
    return findings_, counted


def main(argv: Optional[List[str]] = None) -> int:
    console.make_streams_utf8()
    parser = argparse.ArgumentParser(
        description=(
            "Refuse un fichier de workflow GitHub mal formé : un YAML cassé n'échoue "
            "nulle part, GitHub l'ignore et tous ses jobs disparaissent."
        )
    )
    parser.add_argument(
        "--root",
        default=str(PROJECT_ROOT),
        help="racine du dépôt (défaut : celle de ce script)",
    )
    parser.add_argument("--json", action="store_true", help="sortie exploitable par un script")
    arguments = parser.parse_args(argv)
    root = pathlib.Path(arguments.root).resolve()

    if not (root / WORKFLOWS).is_dir():
        print(
            f"aucun répertoire {WORKFLOWS.as_posix()}/ sous {root} : rien à vérifier",
            file=sys.stderr,
        )
        return 2
    files = workflow_files(root)
    if not files:
        print(
            f"aucun workflow (*.yml, *.yaml) dans {root / WORKFLOWS} : rien à vérifier",
            file=sys.stderr,
        )
        return 2

    findings_, counted = audit(root)
    if arguments.json:
        print(
            json.dumps(
                {
                    "ok": not findings_,
                    "root": str(root),
                    "files": counted["files"],
                    "jobs": counted["jobs"],
                    "steps": counted["steps"],
                    "counts": _kinds(findings_),
                    "findings": findings_,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render(findings_, counted, root), file=sys.stdout if not findings_ else sys.stderr)
    return 1 if findings_ else 0


if __name__ == "__main__":
    raise SystemExit(main())
