#!/usr/bin/env python
"""Contrôles structurels des écrans Compose — sans compilateur Kotlin.

Ce dépôt ne peut pas être compilé sur toutes les machines (le SDK Android n'y
est pas forcément installé, et Gradle télécharge la distribution). Ce script
couvre la part vérifiable autrement, celle qui se voit **tard** en CI :

1. **Aucun import nommé inutilisé.** C'est la règle `no-unused-imports` de ktlint
   (l'unique gate bloquant) reproduite en Python : elle signale la même erreur,
   mais en quelques millisecondes, sans JDK ni Gradle. Les imports « étoile » sont
   ignorés : leurs symboles ne sont pas déterminables sans classpath (et la règle
   `no-wildcard-imports` correspondante est désactivée côté ktlint).
2. **Aucune déclaration de premier niveau en double** dans le paquet : deux
   `fun Foo` dans deux fichiers ne compilent pas, et un découpage de fichier est
   précisément la situation où cela arrive.
3. **Accolades équilibrées** dans chaque fichier : première signature d'une
   extraction de bloc ratée (une lambda coupée en deux).
4. **Aucune taille de police en dur** dans un écran (`fontSize = 10.sp`).
   L'échelle typographique vit dans `ui/theme/Theme.kt` : un écran qui fixe une
   taille contourne l'échelle, et la retoucher devient une chasse dans 39
   fichiers. `theme/` est donc exclu — c'est là que les tailles sont la
   **source**, pas la fuite.
5. **Aucun libellé anglais en dur** dans un écran. Tout texte affiché vient de
   `res/values*/strings.xml` : c'est la seule façon qu'il soit traduit dans les
   dix langues. Ce contrôle est le garde-fou de cette règle — sans lui, un
   `Text("Save changes")` ajouté plus tard repasserait en douce. Les codes
   (`BUY`, `EXECUTED`, `sentiment`), types MIME, URL, `testTag` et autres
   valeurs de protocole sont admis via `ALLOWED_LITERAL_TEXT`.
6. **Cohérence du modèle `AppStrings`** : chaque champ déclaré est projeté depuis
   une ressource, et chaque `strings.<champ>` du paquet existe vraiment. C'est le
   compilateur qui vérifierait la seconde moitié ; sans lui, une faute de frappe
   dans une migration de 350 chaînes ne se voit qu'à l'exécution.
7. **Forme des boutons** : chaque bouton passe explicitement une forme. Material
   3 donne par défaut aux boutons la forme `corner full`, c'est-à-dire une
   **pilule** : rien dans le code ne le dit, et c'est pourtant ce que l'écran
   montre. La forme commune vit dans `theme/Theme.kt` (`ButtonShape`), et ce
   contrôle est le seul endroit où l'oublier se voit autrement qu'à l'œil.
8. **Taille des fichiers** : signalée (avertissement) car au-delà d'environ 300
   lignes un écran devient difficile à relire. Ce n'est pas une erreur : le
   seuil est un repère, pas une règle de compilation.
9. **Mise en forme** : la part de ktlint qui se vérifie sans parseur — la
   longueur de ligne (`max_line_length`, 140 ici) et l'indentation (espaces
   uniquement, multiples de `indent_size`). Les deux seuils sont **lus dans
   `.editorconfig`**, jamais recopiés : le contrôle ne peut donc pas dériver du
   gate qu'il reproduit. Les exceptions de `max-line-length` sont celles de la
   règle : ligne qui n'est qu'un commentaire, intérieur d'un commentaire de bloc
   ou d'un KDoc, contenu d'une chaîne brute (entre triples guillemets), ligne
   qui ne porte
   qu'un littéral, déclaration `package`/`import`. Ce contrôle **ne** modélise
   pas la profondeur des blocs : voir [scan_formatting] pour la raison mesurée.

Les commentaires et le *texte* des chaînes sont ignorés par les contrôles 1 à 4,
mais les interpolations (`"$x"`, `"${strings.y}"`) sont analysées : ce sont de
vraies références, et les ignorer ferait passer un import réellement utilisé pour
inutilisé. Le contrôle 5 fait l'inverse : il ne regarde **que** le texte des
littéraux.

Les contrôles 1 à 8 portent sur `--root` (le paquet `ui`) ; la mise en forme
porte sur `--sources`, c'est-à-dire tout le Kotlin du module, comme ktlint.

Usage : `python scripts/kotlin_ui_check.py [--root DOSSIER] [--max-lines N]
[--sources DOSSIER]`. Code de sortie : 0 si tout passe, 1 sinon.
"""

from __future__ import annotations

import argparse
import fnmatch
import pathlib
import re
import sys
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

DEFAULT_ROOT = "app/src/main/java/com/aitrade/ui"
DEFAULT_MAX_LINES = 300

#: Tout le Kotlin du module : ce que `:app:ktlintCheck` passe au crible.
DEFAULT_SOURCES = "app/src"

#: Limite de longueur de ligne du style `ktlint_official`, appliquée quand
#: `.editorconfig` ne dit rien. La valeur de référence reste celle du fichier :
#: elle est lue, pas recopiée.
DEFAULT_MAX_LINE_LENGTH = 140

#: Racine du dépôt, pour retrouver `.editorconfig` quel que soit le répertoire
#: depuis lequel le script est lancé.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # exécuté comme script : `core` doit être joignable
    sys.path.insert(0, str(REPO_ROOT))

from core import console  # noqa: E402  (après l'ajustement de `sys.path`)

EDITORCONFIG_PATH = REPO_ROOT / ".editorconfig"

#: Déclaration de premier niveau : en colonne 0 (aucune indentation).
DECLARATION = re.compile(
    r"^(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:internal\s+|private\s+|public\s+|protected\s+|open\s+|abstract\s+|sealed\s+|"
    r"data\s+|enum\s+|annotation\s+|value\s+)*"
    r"(?:fun|val|var|class|object|interface|typealias)\s+"
    #: Récepteur éventuel d'une fonction ou propriété d'extension : le nom de la
    #: déclaration est **après** le point. L'ignorer faisait lire `fun
    #: Context.localizedTo(…)` comme une déclaration nommée `Context` : le nom
    #: réel était invisible pour la détection de doublons, et aucun import ne
    #: pouvait le résoudre.
    r"(?:[A-Za-z_][A-Za-z0-9_.]*(?:<[^<>\n]*>)?\.)?"
    r"([A-Za-z_]\w*)"
)

#: Le `as` est capturé séparément : un import renommé n'introduit pas le
#: dernier segment du chemin, mais l'alias.
IMPORT = re.compile(
    r"^import\s+([A-Za-z_][\w.]*)(?:\s+as\s+([A-Za-z_]\w*))?\s*$"
)

#: Boutons Material 3 : leur forme par défaut est `corner full`, donc une pilule.
#: `IconButton` n'en fait pas partie (il est circulaire par construction), et les
#: `Chip` non plus : une puce est arrondie par définition.
BUTTON_CALL = re.compile(
    r"(?<![\w.])(?:FilledTonalButton|OutlinedButton|ElevatedButton|TextButton|Button)\("
)
#: Argument de forme, écrit dans l'appel (`shape = ButtonShape`).
SHAPE_ARGUMENT = re.compile(r"\bshape\s*=")

#: Taille de police écrite en dur.
FONT_SIZE = re.compile(r"fontSize\s*=\s*\d+(?:\.\d+)?\.sp")

#: Le motif d'une phrase : au moins un espace **et** un mot de quatre lettres ou
#: plus. Un identifiant (`BTC`, `image/jpeg`, `chat_list`, `BLOCKED_RISK`) n'a pas
#: d'espace ; un libellé paramétré (`"Score: %s"`) en a un et retombe dessus.
PROSE = re.compile(r"[A-Za-z]{4,}")

#: Texte légitimement gardé en dur dans le paquet `ui`. Chaque entrée est une
#: décision assumée, pas un oubli : les dix noms de langue servent à dire au
#: modèle Gemini dans quelle langue répondre (ils ne sont jamais affichés), et le
#: dernier est le message d'assertion de `LocalAppStrings`. Les noms natifs de
#: `AppLanguage` (`Français`, `English`…) sont dans le même cas : la convention
#: veut qu'un sélecteur de langue affiche chaque langue dans sa propre langue.
ALLOWED_LITERAL_TEXT = {
    "English",
    "Deutsch",
    "Français",
    "Español",
    "Português",
    "French (Français)",
    "Spanish (Español)",
    "German (Deutsch)",
    "Chinese (中文)",
    "Arabic (العربية)",
    "Japanese (日本語)",
    "Portuguese (Português)",
    "Russian (Русский)",
    "Hindi (हिन्दी)",
    "LocalAppStrings absent : entourer l'écran avec ProvideAppStrings(languageCode) { ... }",
}


def code_only(text: str) -> str:
    """Retire commentaires et texte littéral des chaînes, garde le code.

    Les `${...}` d'un template sont conservés tels quels : ils contiennent du
    code réellement exécuté. Les accolades de ces templates s'équilibrent
    (`${` … `}`), donc le comptage d'accolades reste juste.
    """
    out: List[str] = []
    index, size = 0, len(text)
    while index < size:
        char = text[index]
        if char == "/" and text.startswith("//", index):
            while index < size and text[index] != "\n":
                index += 1
            continue
        if char == "/" and text.startswith("/*", index):
            index += 2
            while index < size and not text.startswith("*/", index):
                index += 1
            index += 2
            continue
        if char == '"':
            index += 1
            depth = 0
            #: Accolades ouvertes DANS chaque expression de template (`${ … }`).
            #: Sans ce compteur, la première `}` d'un bloc `if (…) { … }`
            #: serait prise pour la fin du template : le reste du bloc serait
            #: compté comme du texte de chaîne, et le fichier paraîtrait avoir
            #: une accolade en trop.
            braces: List[int] = []
            while index < size:
                current = text[index]
                if current == "\\" and depth == 0:
                    index += 2
                    continue
                if depth == 0:
                    if current == '"':
                        index += 1
                        break
                    if text.startswith("${", index):
                        out.append("${")
                        index += 2
                        depth += 1
                        braces.append(0)
                        continue
                    if current == "$":
                        # Forme courte `"$x"` : c'est une référence, au même
                        # titre que `"${x}"`. L'oublier ferait passer pour
                        # inutilisé un import dont le seul usage est un template
                        # court.
                        following = text[index + 1:index + 2]
                        if following.isalpha() or following == "_":
                            out.append("$")
                            index += 1
                            while index < size and (text[index].isalnum() or text[index] == "_"):
                                out.append(text[index])
                                index += 1
                            continue
                        index += 1
                        continue
                    index += 1
                    continue
                # Dans une expression de template : on conserve tout (les
                # identifiants servent à la détection d'usage), mais on suit
                # les accolades pour savoir laquelle `}` referme le template.
                if text.startswith("${", index):
                    out.append("${")
                    index += 2
                    depth += 1
                    braces.append(0)
                    continue
                if current == "{":
                    out.append("{")
                    braces[-1] += 1
                    index += 1
                    continue
                if current == "}":
                    out.append("}")
                    index += 1
                    if braces[-1] == 0:
                        braces.pop()
                        depth -= 1
                    else:
                        braces[-1] -= 1
                    continue
                out.append(current)
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def identifiers(text: str) -> Set[str]:
    """Identifiants présents dans du code (donc hors texte des chaînes)."""
    cleaned = code_only(text)
    found: Set[str] = set()
    current: List[str] = []
    for char in cleaned + " ":
        if char.isalnum() or char == "_":
            current.append(char)
        elif current:
            found.add("".join(current))
            current = []
    return found


def named_imports(path: pathlib.Path) -> List[Tuple[str, str]]:
    """Imports nommés : (ligne complète, symbole introduit).

    Les imports « étoile » sont écartés : sans classpath il est impossible de
    savoir quels symboles ils apportent, et ktlint ne les contrôle pas non plus
    (`no-wildcard-imports` désactivée dans `.editorconfig`).
    """
    found: List[Tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = IMPORT.match(line)
        if not match:
            continue
        target = match.group(1)
        if target.endswith(".*"):
            continue
        symbol = match.group(2) or target.rsplit(".", 1)[-1]
        found.append((line, symbol))
    return found


def body_without_imports(path: pathlib.Path) -> str:
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("import ")
    )


def unused_imports(path: pathlib.Path) -> List[str]:
    used = identifiers(body_without_imports(path))
    return [line for line, symbol in named_imports(path) if symbol not in used]


def code_lines(path: pathlib.Path) -> List[Tuple[int, str]]:
    """Lignes du fichier qui portent du code, numérotées, commentaires écartés.

    `code_only` blanchit aussi les **retours à la ligne** d'un commentaire de
    bloc : les numéros de ligne qui suivent un KDoc se décaleraient. Ici la
    ligne est l'unité, donc la numérotation reste celle de l'éditeur. Une
    chaîne contenant `fontSize = 10.sp` serait un faux positif, mais elle
    n'existe pas : le seuil est un repère de mise en forme, pas un secret.
    """
    inside_block = False
    found: List[Tuple[int, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if inside_block:
            if "*/" in stripped:
                inside_block = False
            continue
        if stripped.startswith("/*"):
            inside_block = "*/" not in stripped
            continue
        if stripped.startswith("//"):
            continue
        found.append((number, line))
    return found


def balanced_paren_span(clean: str, open_index: int) -> Optional[int]:
    """Index de la parenthèse fermante de celle ouverte à `open_index`.

    `clean` est le texte déjà passé par [code_only] : les parenthèses d'un
    commentaire ou d'un libellé ne sont donc pas comptées. Les `${…}` d'un
    template, eux, restent du code et s'équilibrent.
    """
    depth = 0
    for index in range(open_index, len(clean)):
        char = clean[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def pill_buttons(path: pathlib.Path) -> List[str]:
    """Boutons qui laissent à Material sa forme par défaut, donc en pilule."""
    text = path.read_text(encoding="utf-8")
    clean = code_only(text)
    found: List[str] = []
    for match in BUTTON_CALL.finditer(clean):
        open_index = match.end() - 1
        close_index = balanced_paren_span(clean, open_index)
        arguments = clean[open_index + 1 : close_index if close_index is not None else len(clean)]
        if SHAPE_ARGUMENT.search(arguments):
            continue
        line = clean.count("\n", 0, open_index) + 1
        call = match.group(0)[:-1]
        found.append(f"{path.name}:{line} {call}(")
    return found


def hardcoded_font_sizes(path: pathlib.Path) -> List[str]:
    """Tailles de police fixées dans un écran (vide pour le paquet `theme`)."""
    if path.parent.name == "theme":
        return []
    found: List[str] = []
    for number, line in code_lines(path):
        match = FONT_SIZE.search(line)
        if match:
            found.append(f"{path.name}:{number} -> {match.group(0)}")
    return found


def literal_texts(path: pathlib.Path) -> List[Tuple[int, str]]:
    """Texte de chaque littéral, avec son numéro de ligne, commentaires écartés.

    Chaque expression de gabarit (`${…}`) est ramenée à `%s` : c'est exactement
    la partie qui deviendrait un `%1$s` de ressource, et sans cette
    normalisation un libellé paramétré paraîtrait toujours « en dur ».
    """
    text = path.read_text(encoding="utf-8")
    found: List[Tuple[int, str]] = []
    index, size, line = 0, len(text), 1
    while index < size:
        char = text[index]
        if char == "\n":
            line += 1
            index += 1
            continue
        if text.startswith("//", index):
            while index < size and text[index] != "\n":
                index += 1
            continue
        if text.startswith("/*", index):
            stop = text.find("*/", index + 2)
            stop = size if stop < 0 else stop + 2
            line += text[index:stop].count("\n")
            index = stop
            continue
        if char != '"':
            index += 1
            continue
        start_line, index, parts = line, index + 1, []
        while index < size:
            current = text[index]
            if current == "\\":
                parts.append(text[index : index + 2])
                index += 2
                continue
            if current == "\n" or current == '"':
                index += 1
                break
            if text.startswith("${", index):
                depth, index = 0, index + 2
                while index < size:
                    inner = text[index]
                    if inner == '"':
                        index += 1
                        while index < size and text[index] != '"':
                            index += 2 if text[index] == "\\" else 1
                        index += 1
                        continue
                    if inner == "{":
                        depth += 1
                    elif inner == "}" and depth == 0:
                        index += 1
                        break
                    elif inner == "}":
                        depth -= 1
                    line += inner == "\n"
                    index += 1
                parts.append("%s")
                continue
            if current == "$":
                index += 1
                while index < size and (text[index].isalnum() or text[index] == "_"):
                    index += 1
                parts.append("%s")
                continue
            parts.append(current)
            index += 1
        found.append((start_line, "".join(parts)))
    return found


def hardcoded_text(path: pathlib.Path) -> List[str]:
    """Libellés anglais écrits en dur dans un écran (vide pour `theme/`)."""
    found: List[str] = []
    for number, value in literal_texts(path):
        stripped = value.strip()
        if not stripped or stripped in ALLOWED_LITERAL_TEXT:
            continue
        if " " not in stripped or not PROSE.search(stripped):
            continue
        found.append(f"{path.name}:{number} -> {stripped[:60]!r}")
    return found


FIELD_DECLARATION = re.compile(r"^    val (\w+): String,$", re.M)
FIELD_PROJECTION = re.compile(r"^        (\w+) = context\.getString\(R\.string\.(\w+)\),$", re.M)
#: Récepteurs conventionnels des chaînes : `strings.x`, `ui.x`, `strings(lang).x`.
RECEIVER_NAMES = "strings|ui|localized"
FIELD_ACCESS = re.compile(rf"\b(?:{RECEIVER_NAMES})\s*(?:\([^)]*\))?\s*\.\s*(\w+)")
#: Nom du destinataire seul, pour vérifier qu'il est bien déclaré.
RECEIVER_ACCESS = re.compile(rf"\b(?:{RECEIVER_NAMES})\b")
#: Membres qui ne viennent pas du modèle (API Kotlin/Compose).
NOT_A_FIELD = {"current", "value", "copy", "equals", "hashCode", "toString", "let", "run", "also"}


def app_strings_model(root: pathlib.Path) -> Optional[Tuple[Set[str], Set[str]]]:
    """(champs déclarés, champs projetés) de `AppStrings`, ou `None` si absent.

    Deux fautes sont autrement invisibles sans compilateur : un champ jamais
    projeté dans `StringsResources.kt` (donc `null` à l'exécution) et un accès à
    un champ qui n'existe pas (`strings.chatWelcomMessage`) — cette dernière
    étant précisément le genre de faute de frappe qu'une migration en masse
    produit.
    """
    localization, resources = root / "Localization.kt", root / "StringsResources.kt"
    if not localization.exists() or not resources.exists():
        return None
    text = localization.read_text(encoding="utf-8")
    if "data class AppStrings(" not in text:
        return None
    body = text.split("data class AppStrings(", 1)[1].split("\n)", 1)[0]
    declared = set(FIELD_DECLARATION.findall(body))
    projected = {name for name, _key in FIELD_PROJECTION.findall(resources.read_text(encoding="utf-8"))}
    return declared, projected


def undeclared_field_accesses(root: pathlib.Path, declared: Set[str]) -> List[str]:
    """Accès `strings.<champ>` dont le champ n'existe pas dans le modèle."""
    found: List[str] = []
    for path in sorted(root.glob("*.kt")):
        if path.name in {"Localization.kt", "StringsResources.kt"}:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            #: Les directives contiennent `ui.theme`, `ui.graphics`… : des paquets.
            if line.startswith(("import ", "package ")):
                continue
            #: Même chose pour un chemin qualifié en ligne
            #: (`androidx.compose.ui.geometry.Offset`).
            for match in FIELD_ACCESS.finditer(re.sub(r"[\w.]*ui\.", " ", line)):
                name = match.group(1)
                if name in declared or name in NOT_A_FIELD:
                    continue
                found.append(f"{path.name}:{number} -> {name}")
    return found


def unbound_field_receivers(root: pathlib.Path) -> List[str]:
    """Accès à une chaîne dont le recepteur n'est defini nulle part dans le fichier.

    `ui.logRiskUpdated` ne compile pas si aucune `val ui` n'est en portee en
    amont. C'est la faute que produit une reecriture en masse : le champ existe,
    le recepteur non. On exige donc que le recepteur soit declare dans le meme
    fichier, comme `val`/`var`, comme parametre de type `AppStrings`, ou comme
    la fonction `strings(language)` du ViewModel.
    """
    found: List[str] = []
    for path in sorted(root.glob("*.kt")):
        if path.name in {"Localization.kt", "StringsResources.kt"}:
            continue
        #: La déclaration `package com.aitrade.ui` et un chemin qualifié
        #: (`androidx.compose.ui.geometry.Offset`) contiennent le nom du récepteur
        #: sans en être un.
        kept = (
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(("import ", "package "))
        )
        text = re.sub(r"\w+(?:\.\w+)*\.ui\.", " ", "\n".join(kept))
        for receiver in sorted({match.group(0) for match in RECEIVER_ACCESS.finditer(text)}):
            declared = any(
                re.search(pat, text)
                for pat in (
                    rf"\b(?:val|var)\s+{receiver}\b",
                    rf"\b{receiver}\s*:\s*AppStrings",
                    rf"\bfun\s+{receiver}\s*\(\s*(?:language\s*:)?",
                    rf"\b{receiver}\s*=\s*[^=]",
                )
            )
            if not declared:
                found.append(f"{path.name}: récepteur non déclaré -> {receiver}.<champ>")
    return found


def declarations(path: pathlib.Path) -> Dict[str, int]:
    found: Dict[str, int] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        match = DECLARATION.match(line)
        if match:
            found.setdefault(match.group(1), number)
    return found


def brace_delta(path: pathlib.Path) -> int:
    cleaned = code_only(path.read_text(encoding="utf-8"))
    return cleaned.count("{") - cleaned.count("}")


def line_count(path: pathlib.Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


# ---------------------------------------------------------------------------
# Mise en forme : `max-line-length` et la part décidable d'`indent`, sans JVM.
# ---------------------------------------------------------------------------

#: Motif d'en-tête de section `.editorconfig`.
SECTION = re.compile(r"^\[(.*)\]\s*$")

#: Accolades d'un motif `.editorconfig` (`*.{kt,kts}`).
ALTERNATIVES = re.compile(r"\{([^{}]*)\}")

#: Indentation d'une ligne.
INDENT = re.compile(r"^[ \t]*")

#: Genres de ligne, du point de vue de ktlint : ce que la ligne **est**, et donc
#: ce à quoi les règles s'appliquent.
CODE, COMMENT, KDOC, STRING, BLANK = "code", "comment", "kdoc", "string", "blank"


class LineFacts(NamedTuple):
    """Ce qu'une ligne porte, tel que la mise en forme le regarde."""

    number: int
    indent: str
    body: str
    kind: str
    length: int


def _brace_alternatives(pattern: str) -> List[str]:
    """Développe les accolades d'un motif : `*.{kt,kts}` donne deux motifs."""
    match = ALTERNATIVES.search(pattern)
    if not match:
        return [pattern]
    expanded: List[str] = []
    for item in match.group(1).split(","):
        alternative = pattern[: match.start()] + item.strip() + pattern[match.end() :]
        expanded.extend(_brace_alternatives(alternative))
    return expanded


def _section_matches(section: str, relative: str) -> bool:
    """Un motif sans barre oblique porte sur le **nom** du fichier.

    C'est la spécification `.editorconfig` : `[*.{kt,kts}]` s'applique à
    `Foo.kt` où qu'il soit, alors qu'un motif contenant une barre oblique se
    compare au chemin.
    """
    target = relative if "/" in section else relative.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(target, item) for item in _brace_alternatives(section))


def editorconfig_properties(
    names: Sequence[str], relative: str = "Sample.kt"
) -> Dict[str, str]:
    """Valeurs effectives de `names` pour un fichier Kotlin du module.

    Les sections sont appliquées dans l'ordre du fichier — la dernière qui
    correspond gagne —, comme le fait `.editorconfig` lui-même. Lire les seuils
    plutôt que de les recopier garantit que ce contrôle ne pourra pas dériver
    silencieusement du gate ktlint qu'il reproduit.
    """
    if not EDITORCONFIG_PATH.is_file():
        return {}
    found: Dict[str, str] = {}
    section: Optional[str] = None
    for raw in EDITORCONFIG_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        header = SECTION.match(line)
        if header:
            section = header.group(1).strip()
            continue
        if section is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key in names and _section_matches(section, relative):
            found[key] = value.strip()
    return found


def formatting_limits() -> Tuple[Optional[int], int, str]:
    """(longueur de ligne, taille d'indentation, style) lus dans `.editorconfig`.

    La longueur vaut `None` quand ktlint ne mesurerait rien non plus : propriété
    `off`, ou valeur illisible. Le contrôle n'invente alors pas de seuil, il le
    dit. `indent_size` et `indent_style` retombent sur les valeurs du style
    `ktlint_official` (4 espaces), celles que ktlint appliquerait de toute façon.
    """
    values = editorconfig_properties(("max_line_length", "indent_size", "indent_style"))
    declared = values.get("max_line_length", "").strip().lower()
    if declared.isdigit():
        max_line_length: Optional[int] = int(declared)
    elif not declared:
        max_line_length = DEFAULT_MAX_LINE_LENGTH
    else:
        max_line_length = None
    size = values.get("indent_size", "")
    indent_size = int(size) if size.isdigit() else 4
    return max_line_length, indent_size, values.get("indent_style", "space").lower()


def _string_end(line: str, start: int) -> Optional[int]:
    """Index juste après le littéral ouvert à `start`, `None` s'il ne se ferme
    pas sur cette ligne.

    Les gabarits `${…}` sont suivis pour de bon : sans cela, le `"` d'un
    `"%.2f"` imbriqué dans un `${String.format(…)}` passerait pour la fin du
    littéral englobant, et le reste de la ligne serait lu comme du code.
    """
    if line.startswith('"""', start):
        end = line.find('"""', start + 3)
        return None if end < 0 else end + 3
    index, size = start + 1, len(line)
    while index < size:
        char = line[index]
        if char == "\\":
            index += 2
            continue
        if char == '"':
            return index + 1
        if line.startswith("${", index):
            close = _template_end(line, index + 2)
            if close is None:
                return None
            index = close
            continue
        index += 1
    return None


def _template_end(line: str, index: int) -> Optional[int]:
    """Index juste après la `}` qui referme le `${` ouvert avant `index`."""
    depth, size = 0, len(line)
    while index < size:
        char = line[index]
        if char == '"':
            stop = _string_end(line, index)
            if stop is None:
                return None
            index = stop
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                return index + 1
            depth -= 1
        index += 1
    return None


def _line_genre(body: str) -> Tuple[str, bool, bool, bool]:
    """Genre de la ligne d'après sa **première** structure.

    Rend `(genre, ouvre un commentaire, ouvre un KDoc, ouvre une chaîne brute)`.
    Un `/* … */` fermé dans la ligne n'ouvre rien : c'est du commentaire, mais
    la ligne reste du code s'il y en a autour.
    """
    index, size = 0, len(body)
    code_seen, literal_seen = False, False
    while index < size:
        char = body[index]
        if char in " \t,":
            index += 1
            continue
        if body.startswith("//", index):
            break
        if body.startswith("/*", index):
            stop = body.find("*/", index + 2)
            kdoc = body.startswith("/**", index)
            if stop < 0:
                return (CODE if code_seen else (KDOC if kdoc else COMMENT), True, kdoc, False)
            index = stop + 2
            continue
        if char == '"':
            stop = _string_end(body, index)
            if stop is None:
                #: Littéral qui déborde sur la ligne suivante : la ligne est du
                #: code, et la suite sera classée ligne par ligne (le contenu
                #: d'un gabarit multi-ligne est du code, comme pour ktlint).
                return (CODE if code_seen else STRING, False, False, body.startswith('"""', index))
            literal_seen = True
            index = stop
            continue
        if char == "'":
            stop = index + 1
            while stop < size and body[stop] != "'":
                stop += 2 if body[stop] == "\\" else 1
            index = min(stop + 1, size)
            continue
        code_seen = True
        index += 1
    if code_seen:
        return (CODE, False, False, False)
    if literal_seen:
        return (STRING, False, False, False)
    return (COMMENT if body.startswith("//") else BLANK, False, False, False)


def scan_formatting(text: str) -> List[LineFacts]:
    """Décompose un fichier ligne à ligne : indentation, genre, longueur.

    Le genre dit ce que la ligne **est** pour ktlint, et donc ce à quoi les
    règles s'appliquent :

    * `comment` / `kdoc` — un commentaire seul, ou l'intérieur d'un commentaire
      de bloc. ktlint ne mesure pas leur longueur, et leur indentation est
      celle du commentaire, pas celle du code : les astérisques d'un KDoc sont
      alignées sous le `/**`, donc à `4n + 1` ou `1`.
    * `string` — le contenu d'une chaîne brute, ou une ligne qui ne porte qu'un
      littéral (éventuellement suivi d'une virgule). Ni longueur ni indentation
      de code n'y ont de sens.
    * `code` — tout le reste, y compris ce qui suit un commentaire fermé dans la
      même ligne.

    Ce qui n'est **pas** modélisé : la profondeur des blocs. Mesuré sur ce
    dépôt, un modèle « l'indentation vaut 4 × le nombre d'accolades ouvertes »
    produit 792 désalignements légitimes (signatures étalées sur plusieurs
    lignes, lambdas ouvertes en fin de continuation, `when`, expressions à
    cheval sur plusieurs lignes) : il rendrait la CI rouge pour de mauvaises
    raisons. Une désindentation franche d'un bloc entier — 4 espaces — passe
    donc ici sans être vue ; seul le vrai ktlint, qui a l'arbre, la voit.
    """
    facts: List[LineFacts] = []
    in_block, kdoc, in_raw = False, False, False
    for number, line in enumerate(text.split("\n"), start=1):
        indent = INDENT.match(line).group(0)
        body = line[len(indent) :]
        length = len(line.rstrip())
        if in_raw:
            kind = STRING
            stop = body.find('"""')
            if stop >= 0:
                in_raw = False
                if body[stop + 3 :].strip():
                    kind = CODE
        elif in_block:
            kind = KDOC if kdoc else COMMENT
            stop = body.find("*/")
            if stop >= 0:
                in_block, kdoc = False, False
                if body[stop + 2 :].strip():
                    kind = CODE
        elif not body.strip():
            kind = BLANK
        else:
            kind, opens_block, opens_kdoc, opens_raw = _line_genre(body)
            if opens_block:
                in_block, kdoc = True, opens_kdoc
            if opens_raw:
                in_raw = True
        facts.append(LineFacts(number, indent, body, kind, length))
    return facts


def formatting_failures(
    facts: Sequence[LineFacts],
    name: str,
    max_line_length: Optional[int],
    indent_size: int,
    indent_style: str,
) -> Tuple[List[str], List[str]]:
    """(lignes trop longues, indentation fautive) pour un fichier.

    Les deux règles de ktlint sont reproduites séparément pour être rapportées
    séparément : une ligne trop longue et une indentation fautive n'appellent
    pas la même correction.
    """
    long_lines: List[str] = []
    indents: List[str] = []
    for fact in facts:
        if fact.kind != CODE:
            continue
        if "\t" in fact.indent:
            if indent_style == "space":
                indents.append(f"{name}:{fact.number} -> tabulation dans l'indentation")
        elif indent_size and len(fact.indent) % indent_size:
            indents.append(
                f"{name}:{fact.number} -> indentation de {len(fact.indent)} espaces "
                f"(multiple de {indent_size} attendu)"
            )
        if not max_line_length or fact.length <= max_line_length:
            continue
        #: ktlint exempte les directives : un import long ne se coupe pas.
        if fact.body.startswith(("package", "import")):
            continue
        long_lines.append(
            f"{name}:{fact.number} -> {fact.length} caractères (limite {max_line_length})"
        )
    return long_lines, indents


def check(
    root: pathlib.Path,
    max_lines: int,
    stream: Optional[object] = None,
    sources: Optional[pathlib.Path] = None,
) -> int:
    out = stream or sys.stdout
    files = sorted(root.glob("*.kt"))
    if not files:
        print(f"aucun fichier Kotlin dans {root}", file=out)
        return 1

    failures: List[str] = []
    print(f"== {len(files)} fichiers Kotlin dans {root}", file=out)

    owners: Dict[str, List[str]] = {}
    for path in files:
        for name in declarations(path):
            owners.setdefault(name, []).append(path.name)
    duplicates = {name: where for name, where in owners.items() if len(where) > 1}
    if duplicates:
        for name, where in sorted(duplicates.items()):
            failures.append(f"déclaration en double : {name} dans {', '.join(sorted(where))}")
    else:
        print(f"  [OK] {len(owners)} déclarations de premier niveau, toutes uniques", file=out)

    unbalanced = [(path.name, brace_delta(path)) for path in files if brace_delta(path)]
    if unbalanced:
        for name, delta in unbalanced:
            failures.append(f"accolades déséquilibrées ({delta:+d}) : {name}")
    else:
        print("  [OK] accolades équilibrées dans tous les fichiers", file=out)

    stale: List[Tuple[str, List[str]]] = []
    for path in files:
        leftovers = unused_imports(path)
        if leftovers:
            stale.append((path.name, leftovers))
    if stale:
        for name, leftovers in stale:
            for line in leftovers:
                failures.append(f"import inutilisé dans {name} : {line}")
    else:
        print("  [OK] aucun import nommé inutilisé", file=out)

    hardcoded: List[str] = []
    for path in files:
        hardcoded.extend(hardcoded_font_sizes(path))
    if hardcoded:
        for item in hardcoded:
            failures.append(
                f"taille de police en dur (l'échelle vit dans theme/Theme.kt) : {item}"
            )
    else:
        print(
            "  [OK] aucune taille de police en dur : toute la typographie vient du thème",
            file=out,
        )

    model = app_strings_model(root)
    if model is None:
        print("  [--] pas de modèle AppStrings dans ce dossier : contrôle ignoré", file=out)
    else:
        declared, projected = model
        missing = sorted(declared - projected)
        extra = sorted(projected - declared)
        unknown = undeclared_field_accesses(root, declared)
        unbound = unbound_field_receivers(root)
        if missing:
            failures.append(f"champs déclarés sans projection (null à l'exécution) : {missing}")
        if extra:
            failures.append(f"projections sans champ déclaré : {extra}")
        if unknown:
            failures.append(f"accès à un champ inexistant : {unknown}")
        if unbound:
            failures.append(f"accès hors portée : {unbound}")
        if not (missing or extra or unknown or unbound):
            print(
                f"  [OK] {len(declared)} champs AppStrings : déclarés, projetés et accédés de façon cohérente",
                file=out,
            )

    hardcoded_texts: List[str] = []
    for path in files:
        hardcoded_texts.extend(hardcoded_text(path))
    if hardcoded_texts:
        for item in hardcoded_texts:
            failures.append(f"libellé en dur (à déplacer dans res/values*/strings.xml) : {item}")
    else:
        print(
            "  [OK] aucun libellé en dur : tout texte affiché vient des ressources",
            file=out,
        )

    pills: List[str] = []
    for path in files:
        pills.extend(pill_buttons(path))
    if pills:
        for item in pills:
            failures.append(
                f"bouton à la forme par défaut (pilule) : passer `shape = ButtonShape` : {item}"
            )
    else:
        print("  [OK] tous les boutons passent une forme explicite", file=out)

    oversized = sorted(
        ((line_count(path), path.name) for path in files if line_count(path) > max_lines),
        reverse=True,
    )
    if oversized:
        print(f"  [avertissement] fichiers de plus de {max_lines} lignes :", file=out)
        for count, name in oversized:
            print(f"    {count:4d}  {name}", file=out)
    else:
        print(f"  [OK] aucun fichier de plus de {max_lines} lignes", file=out)

    source_root = sources if sources is not None else root
    scanned = sorted(source_root.rglob("*.kt")) if source_root.is_dir() else []
    if not scanned:
        failures.append(f"mise en forme : aucun fichier Kotlin sous {source_root}")
    else:
        max_line_length, indent_size, indent_style = formatting_limits()
        long_lines: List[str] = []
        bad_indents: List[str] = []
        for path in scanned:
            facts = scan_formatting(path.read_text(encoding="utf-8"))
            found_long, found_indent = formatting_failures(
                facts, path.name, max_line_length, indent_size, indent_style
            )
            long_lines.extend(found_long)
            bad_indents.extend(found_indent)
        where = f"{len(scanned)} fichiers sous {source_root}"
        if long_lines:
            failures.extend(f"mise en forme (max-line-length) : {item}" for item in long_lines)
        elif max_line_length is None:
            print(
                "  [--] max_line_length absent ou désactivé dans .editorconfig : "
                "longueur de ligne non contrôlée",
                file=out,
            )
        else:
            print(
                f"  [OK] mise en forme : aucune ligne de plus de {max_line_length} caractères "
                f"({where})",
                file=out,
            )
        if bad_indents:
            failures.extend(f"mise en forme (indent) : {item}" for item in bad_indents)
        else:
            print(
                f"  [OK] mise en forme : indentation en espaces, multiple de {indent_size} "
                f"sur chaque ligne de code ({where})",
                file=out,
            )

    if failures:
        for item in failures:
            print(f"  [ÉCHEC] {item}", file=out)
        return 1
    return 0


def main(argv: Sequence[str]) -> int:
    console.make_streams_utf8()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=DEFAULT_ROOT, help=f"dossier analysé (défaut : {DEFAULT_ROOT})")
    parser.add_argument(
        "--max-lines",
        type=int,
        default=DEFAULT_MAX_LINES,
        help=f"seuil d'avertissement de taille (défaut : {DEFAULT_MAX_LINES})",
    )
    parser.add_argument(
        "--sources",
        default=DEFAULT_SOURCES,
        help=(
            "Kotlin soumis au contrôle de mise en forme, récursivement "
            f"(défaut : {DEFAULT_SOURCES}, tout le module)"
        ),
    )
    args = parser.parse_args(argv)
    return check(
        pathlib.Path(args.root), args.max_lines, sources=pathlib.Path(args.sources)
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
