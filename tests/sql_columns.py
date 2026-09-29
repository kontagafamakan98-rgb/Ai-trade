"""Extracteur de colonnes partagé par les contrats de migration.

Un DDL écrit **après coup** — pour des tables qui existaient déjà en base — n'a
de valeur que s'il décrit les colonnes que le code utilise réellement. Ce module
fait les deux lectures nécessaires, et rien d'autre :

* `columns_used_by_code(tables)` relit les **sources Python** et rend, par table
  et par fichier, les colonnes touchées par une requête (`.eq("…")`,
  `.insert({…})`, `.update(payload)`, clés d'un dictionnaire construit plus
  haut, et clés du dictionnaire passé à un **écrivain délégué** — voir
  `delegated_writers()`) ;
* `declared_columns(path, tables)` relit le `create table` d'un fichier de
  migration et rend les colonnes qu'il déclare ;
* `declared_objects(paths)` relit ce que les migrations déclarent **en dehors des
  colonnes** : index, contraintes, déclencheurs, RLS, policies et `revoke`. C'est
  la même règle que pour les colonnes — une base peut avoir perdu un index ou vu
  la RLS se désactiver sans que rien ne le dise, et `scripts/check_schema_drift.py`
  ne peut comparer que ce qui est lu quelque part.

Il est partagé par `tests/test_core_tables_migration.py` (migration 011) et
`tests/test_engine_tables_migration.py` (migrations 005 et 006) : la même
extraction pour tous les contrats, et non une copie par table — une extraction
recopiée dériverait, et le contrat ne vérifierait plus rien. Voir
`tests/hook_support.py` pour l'autre aide de test partagée du dépôt.

`scripts/check_schema_drift.py` s'en sert aussi : comparer une base réelle à
« ce que les migrations déclarent » n'a de sens que si cette deuxième liste est
lue au même endroit que celle des contrats — sinon l'outil et les tests
pourraient décrire deux schémas différents, et chacun passerait au vert.
"""
from __future__ import annotations

import os
import pathlib
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Opérations de requête dont le **premier argument** nomme une colonne.
FILTER_OPS = (
    "select|eq|neq|gt|gte|lt|lte|like|ilike|is_|in_|order|contains|filter|not_|or_"
)
QUOTED_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
#: La clé d'un littéral de dictionnaire (`"colonne": …`).
KEY_OF_LITERAL = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:')
#: Une définition de fonction, avec son nom.
FUNCTION_DEF = re.compile(r"\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
#: Une écriture sur une table, avec la **charge** qu'elle reçoit ensuite.
TABLE_WRITE = re.compile(r'table\("([a-z_][a-z0-9_]*)"\)\s*\.(?:insert|update|upsert)\(')
#: Un appel de fonction libre : `f(…)`, jamais `.méthode(…)`.
FREE_CALL = re.compile(r"(?<![.\w])([A-Za-z_][A-Za-z0-9_]*)\(")
#: Un argument nommé, et non une comparaison (`x == 1` n'est pas `x = 1`).
KEYWORD_PIECE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)\s*([\s\S]+)$")


def python_sources() -> List[pathlib.Path]:
    """Sources applicatives : ni tests, ni dépendances, ni outillage jetable.

    Les dossiers **cachés** sont écartés d'office : ce dépôt n'y range que ce
    qu'il ne livre pas — `.venv` (les dépendances) et `.pgtest/`, le PostgreSQL
    jetable des vérifications locales. Ce dernier embarque à lui seul près de
    5 000 `.py` (une distribution PostgreSQL complète, pgAdmin inclus) ; les
    relire tous faisait passer une extraction de 0,4 s à 7,4 s — **sans jamais y
    trouver une colonne** — et la suite de ~40 s à ~300 s. Ces dossiers ne sont
    pas seulement filtrés, ils ne sont **pas parcourus** : marcher 24 000
    fichiers coûte à lui seul l'essentiel de ce temps, et ce module est appelé
    par chaque contrat de migration.

    Le filtre porterait sur un chemin **absolu** qu'un dépôt installé sous un
    parent caché (`.local/…`) se retrouverait vide : il porte donc sur les
    noms, relatifs par construction à la racine que l'on parcourt.
    """
    found: List[pathlib.Path] = []
    for directory, subdirectories, files in os.walk(REPO_ROOT):
        subdirectories.sort()
        subdirectories[:] = [name for name in subdirectories if not _skipped(name)]
        for name in sorted(files):
            if name.endswith(".py"):
                found.append(pathlib.Path(directory, name))
    return found


def _skipped(directory: str) -> bool:
    """Ce dossier n'est-il jamais le parent d'une source applicative ?"""
    return directory in {"tests", "__pycache__"} or directory.startswith(".")


def statement_spans(text: str) -> List[Tuple[int, str]]:
    """Les « phrases » Python du fichier : `(ligne de départ, texte)`.

    Le découpage est volontairement grossier (comptage de parenthèses, fusion des
    continuations `.methode()`) — mais il est indispensable : en lisant simplement
    les lignes qui suivent `table("…")`, on attribue à une table les colonnes de
    la requête **suivante**, et un DDL bâti là-dessus déclarerait des colonnes
    qui n'existent pas.
    """
    chunks: List[Tuple[int, str]] = []
    current: List[str] = []
    start = 0
    depth = 0
    for number, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if not current and (not stripped or stripped.startswith("#")):
            continue
        if not current:
            start = number
        current.append(line)
        depth += sum(line.count(char) for char in "([{")
        depth -= sum(line.count(char) for char in ")]}")
        if depth > 0 or stripped.endswith((".", "\\")):
            continue
        chunks.append((start, "\n".join(current)))
        current = []
    if current:
        chunks.append((start, "\n".join(current)))

    merged: List[Tuple[int, str]] = []
    for begin, chunk in chunks:
        first = next((line.strip() for line in chunk.splitlines() if line.strip()), "")
        if merged and first.startswith((".", ")")):
            merged[-1] = (merged[-1][0], f"{merged[-1][1]}\n{chunk}")
        else:
            merged.append((begin, chunk))
    return merged


def function_bodies(text: str) -> List[Tuple[int, int, str]]:
    """`(première ligne, dernière ligne, texte)` de chaque fonction du fichier.

    Sert à retrouver le dictionnaire d'une requête écrite ailleurs que dans
    l'appel : `payload = {…}` puis `.update(payload)`. Sans cette borne, on
    ramasserait les clés de **toutes** les variables nommées `payload` du
    fichier — et la table hériterait de colonnes qui ne sont pas les siennes.
    """
    lines = text.splitlines()
    bounds = [
        number for number, line in enumerate(lines) if re.match(r"\s*(async )?def ", line)
    ]
    bodies = []
    for position, start in enumerate(bounds):
        end = bounds[position + 1] - 1 if position + 1 < len(bounds) else len(lines) - 1
        bodies.append((start, end, "\n".join(lines[start : end + 1])))
    return bodies


def body_with(bodies: List[Tuple[int, int, str]], line: int) -> str:
    for start, end, body in bodies:
        if start <= line <= end:
            return body
    return ""


def dict_keys(span: str) -> Set[str]:
    """Clés des littéraux `{ … }` de la phrase — leur **premier** niveau, seul.

    Un dictionnaire imbriqué est une *valeur* (`"data": {"published": …}`) : ses
    clés ne sont pas des colonnes. Les compter faisait entrer `published` dans le
    schéma d'`insights` et `probe` dans celui d'`economic_events` — des colonnes
    fantômes, réclamées à des DDL pourtant justes. Un `{` ouvert **à l'intérieur**
    d'un littéral déjà lu est donc ignoré : ce sont les dictionnaires de la phrase
    que l'on veut, pas ceux qu'ils contiennent.
    """
    keys: Set[str] = set()
    covered = 0
    for match in re.finditer(r"\{", span):
        if match.start() < covered:
            continue
        depth = 0
        for index in range(match.start(), len(span)):
            char = span[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    covered = index + 1
                    break
            elif char == '"' and depth == 1:
                key = KEY_OF_LITERAL.match(span, index)
                if key:
                    keys.add(key.group(1))
    return keys


def call_arguments(span: str, position: int) -> str:
    """Le texte entre la parenthèse ouvrante (à `position`) et la fermante.

    Bornée à sa propre parenthèse : une phrase peut porter deux appels, et la
    charge du second n'est pas celle du premier. Elle sert aussi bien à lire les
    arguments d'un appel que la **signature** d'une fonction.
    """
    depth = 0
    for index in range(position, len(span)):
        char = span[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return span[position + 1 : index]
    return span[position + 1 :]


def top_level_pieces(text: str) -> List[str]:
    """Les morceaux d'une liste d'arguments : coupés sur les virgules **de tête**.

    Les virgules d'un dictionnaire ou d'un appel imbriqué (`f([{…}, {…}])`) ne
    séparent pas les arguments de cet appel-là.
    """
    pieces: List[str] = []
    current: List[str] = []
    depth = 0
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            pieces.append("".join(current))
            current = []
            continue
        current.append(char)
    pieces.append("".join(current))
    return [piece for piece in (piece.strip() for piece in pieces) if piece]


def bound_argument(arguments: str, parameter: str, index: int) -> str:
    """L'argument qui lie `parameter` — `f(x)`, `f(x, y=…)` ou `f(y=…)`.

    Un argument nommé gagne sur la position : il dit lui-même de quel paramètre il
    s'agit. Un `*args` la rend au contraire incalculable (il peut en porter
    plusieurs), et seul le mot-clé reste alors fiable — il est donc écarté des
    positionnels plutôt que compté à tort.
    """
    positional: List[str] = []
    for piece in top_level_pieces(arguments):
        keyword = KEYWORD_PIECE.match(piece)
        if keyword and keyword.group(1) == parameter:
            return keyword.group(2)
        if piece.startswith("*"):
            continue
        if not keyword:
            positional.append(piece)
    return positional[index] if index < len(positional) else ""


def _signature_parameters(body: str) -> Dict[str, int]:
    """Nom → position des paramètres d'une signature (`self` compris)."""
    match = FUNCTION_DEF.match(body)
    if not match:
        return {}
    parameters: Dict[str, int] = {}
    for index, piece in enumerate(top_level_pieces(call_arguments(body, match.end() - 1))):
        name = re.match(r"\*{0,2}([A-Za-z_][A-Za-z0-9_]*)", piece)
        if name:
            parameters.setdefault(name.group(1), index)
    return parameters


def _payload_binding(body: str) -> Optional[Tuple[str, str, int]]:
    """`(table, paramètre, position)` quand un paramètre est la charge écrite.

    `upsert_economic_events(events)` remet `events` tel quel à PostgREST : les clés
    du dictionnaire de l'appelant **sont** des colonnes. `create_pending_signal`
    écrit au contraire `{"user_id": …, "signal": signal}` : ce qui vient de
    l'appelant y est une *valeur*, et ses clés forment un document JSON — pas des
    colonnes. Seul le premier cas autorise à remonter une charge jusqu'au schéma,
    et c'est ce que ce test distingue.

    Une fonction qui écrit plusieurs lignes (`record_trade_settlement_and_learn`)
    ne dit pas à laquelle appartient ce qu'elle reçoit : elle n'est pas retenue.
    """
    writes = list(TABLE_WRITE.finditer(body))
    if len(writes) != 1:
        return None
    write = writes[0]
    arguments = top_level_pieces(call_arguments(body, write.end() - 1))
    payload = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)", arguments[0]) if arguments else None
    if not payload:
        return None
    parameters = _signature_parameters(body)
    if payload.group(1) not in parameters:
        return None
    return write.group(1), payload.group(1), parameters[payload.group(1)]


def delegated_writers(
    sources: Iterable[Tuple[pathlib.Path, str]],
) -> Dict[str, Tuple[str, str, int]]:
    """Nom de fonction → `(table, paramètre, position)`, pour les **écrivains
    délégués** : ceux qui remettent à une table la charge qu'on leur a passée.

    Tout ne s'écrit pas depuis l'appel : `upsert_economic_events()` construit sa
    requête **dans** `database/supabase_client.py`, et son `events` n'est qu'un
    paramètre. Les dictionnaires remplis par ses appelants — la charge de la sonde
    de configuration (`scripts/check_supabase.py`), celle du scraper — échappaient
    donc à l'extraction : le DDL déclarait `raw_data`, `title`, `country`… que
    **rien** ne reliait à une écriture, et une faute de frappe dans la charge d'un
    appelant serait passée inaperçue (`upsert_economic_events` avale ses erreurs
    et rend 0).

    Un nom porté par deux écrivains différents est écarté : deux corps, deux
    tables, et rien ne dit lequel a été appelé — deviner ferait hériter une table
    des colonnes d'une autre, ce que
    `test_the_tables_do_not_borrow_each_others_columns` interdit justement.
    """
    written: Dict[str, Optional[Tuple[str, str, int]]] = {}
    for _, text in sources:
        for _, _, body in function_bodies(text):
            name = FUNCTION_DEF.match(body)
            binding = _payload_binding(body)
            if not name or binding is None:
                continue
            known = written.setdefault(name.group(1), binding)
            if known != binding:
                written[name.group(1)] = None
    return {name: bound for name, bound in written.items() if bound is not None}


def payload_keys(text: str, name: str) -> Set[str]:
    """Clés d'un dictionnaire construit **hors** de la requête (`payload["x"] = …`).

    Le dictionnaire peut aussi être complété par une boucle sur une liste
    d'objets — `for e in events: e["updated_at"] = now_str`, dans
    `upsert_economic_events()` : l'alias de la boucle est donc traité comme le
    nom lui-même. Sans cela, une colonne écrite par le code échapperait à
    l'extraction, et le DDL ne la déclarerait pas.
    """
    names = {name}
    names.update(re.findall(rf"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+{re.escape(name)}\b", text))
    keys: Set[str] = set()
    for alias in names:
        pattern = re.escape(alias)
        keys |= set(re.findall(rf'\b{pattern}\["([A-Za-z_][A-Za-z0-9_]*)"\]', text))
        for match in re.finditer(rf"\b{pattern}\s*(?::[^=\n]+)?=\s*\{{", text):
            keys |= dict_keys(text[match.end() - 1 :])
    return keys


def columns_of_sources(
    sources: Iterable[Tuple[pathlib.Path, str]], tables: Iterable[str]
) -> Dict[str, Dict[str, Set[str]]]:
    """L'extraction elle-même, sur des sources **déjà lues** : `(chemin, texte)`.

    Deux passages, parce qu'un écrivain délégué ne se lit pas dans le même fichier
    que son appelant : les sources sont d'abord toutes lues, `delegated_writers()`
    dit quelles fonctions remettent une table à un paramètre — et lequel —, puis
    chaque dictionnaire passé à l'une d'elles est compté comme une colonne de
    **cette** table.

    Séparée du parcours du dépôt pour être éprouvable sur des sources écrites à la
    main (`tests/test_check_schema_drift.py`) : là-bas, ce sont les décisions de
    l'extraction qui sont vérifiées, pas la forme du dépôt.
    """
    names = tuple(tables)
    found: Dict[str, Dict[str, Set[str]]] = {table: {} for table in names}
    sources = list(sources)
    writers = delegated_writers(sources)
    for path, text in sources:
        relative = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        bodies = function_bodies(text)
        for start, span in statement_spans(text):
            for table in names:
                if f'table("{table}")' not in span:
                    continue
                columns = found[table].setdefault(relative, set())
                for match in re.finditer(rf"\.(?:{FILTER_OPS})\(", span):
                    argument = re.compile(r'\s*"([^"]*)"').match(span, match.end())
                    if not argument:
                        continue
                    for piece in argument.group(1).split(","):
                        piece = piece.strip()
                        if QUOTED_IDENTIFIER.match(piece):
                            columns.add(piece)
                for match in re.finditer(r"\.(?:insert|update|upsert)\(", span):
                    variable = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*[,)]").match(
                        span, match.end()
                    )
                    if variable and variable.group(1) != "execute":
                        # La requête peut écrire un dictionnaire construit plus haut
                        # dans la même fonction (`payload = {…}`).
                        columns |= payload_keys(body_with(bodies, start) or span, variable.group(1))
                    if re.compile(r"\s*\{").match(span, match.end()):
                        columns |= dict_keys(span[match.end() :])
            for match in FREE_CALL.finditer(span):
                # L'appelant qui remplit la charge d'un écrivain délégué
                # (`upsert_economic_events([{…}])`) : ses clés sont les colonnes
                # que cet écrivain écrira.
                binding = writers.get(match.group(1))
                if binding is None:
                    continue
                table, parameter, index = binding
                if table not in found:
                    continue
                argument = bound_argument(
                    call_arguments(span, match.end() - 1), parameter, index
                )
                keys = dict_keys(argument)
                if keys:
                    found[table].setdefault(relative, set()).update(keys)
    return found


def columns_used_by_code(tables: Iterable[str]) -> Dict[str, Dict[str, Set[str]]]:
    """Colonnes touchées par le **code du dépôt**, par table et par fichier.

    Un seul parcours, puis l'extraction — qui a besoin de toutes les sources avant
    de rendre quoi que ce soit, un écrivain délégué pouvant être appelé depuis un
    autre fichier que le sien.
    """
    return columns_of_sources(
        (
            (path, path.read_text(encoding="utf-8", errors="replace"))
            for path in python_sources()
        ),
        tables,
    )


#: Un bloc `create table if not exists <table> ( … );`
CREATE_TABLE = re.compile(
    r"create table if not exists ([a-z_][a-z0-9_]*)\s*\((.*?)\n\);", re.DOTALL
)
#: Une instruction `alter table [if exists] <table> … ;`
ALTER_TABLE = re.compile(
    r"alter\s+table\s+(?:if\s+exists\s+)?([a-z_][a-z0-9_]*)([^;]*);",
    re.IGNORECASE | re.DOTALL,
)
#: Une colonne ajoutée dans une telle instruction, avec son **garde-fou** capturé
#: à part. Une seule instruction peut en ajouter plusieurs (migration 009 sur
#: `knowledge_chunks`, migration 004 sur `user_preferences`) : ne lire que la
#: première laisserait des colonnes hors du schéma déclaré. Et le garde-fou est
#: ce qui rend l'instruction rejouable sur une base qui a déjà servi — voir
#: `unguarded_ddl()`.
ADD_COLUMN = re.compile(
    r"add\s+column\s+(if\s+not\s+exists\s+)?([a-z_][a-z0-9_]*)", re.IGNORECASE
)
#: Une colonne retirée. Le schéma déclaré est ce que le DDL **laisse derrière
#: lui**, pas tout ce qu'il a nommé : `006_adaptive_learning.sql` retire
#: `min_confidence_threshold`, une colonne qu'une version antérieure du fichier
#: créait. L'ignorer ferait déclarer une colonne que la base n'a pas — et une
#: comparaison « déclaré vs réel » crierait alors sur une base pourtant juste.
DROP_COLUMN = re.compile(
    r"drop\s+column\s+(if\s+exists\s+)?([a-z_][a-z0-9_]*)", re.IGNORECASE
)

#: Les DDL dont la rejouabilité dépend d'un garde-fou : sur une base **déjà en
#: service**, un `create table` sans `if not exists` échoue (« relation already
#: exists »), un `add column` sans `if not exists` aussi, et un `drop` sans
#: `if exists` échoue sur une base neuve. `add constraint` n'y figure pas :
#: Postgres n'accepte pas `if not exists` dessus, et la migration 011 le garde par
#: un bloc `do $$ … if not exists (select 1 from pg_constraint …) $$`.
GUARDED_DDL = (
    ("create table", re.compile(r"create\s+table\s+(if\s+not\s+exists\s+)?", re.IGNORECASE)),
    (
        "create index",
        re.compile(r"create\s+(?:unique\s+)?index\s+(if\s+not\s+exists\s+)?", re.IGNORECASE),
    ),
    ("drop index", re.compile(r"drop\s+index\s+(if\s+exists\s+)?", re.IGNORECASE)),
    ("drop trigger", re.compile(r"drop\s+trigger\s+(if\s+exists\s+)?", re.IGNORECASE)),
    ("add column", ADD_COLUMN),
    ("drop column", DROP_COLUMN),
)
#: Lignes d'un bloc `create table` qui ne nomment pas une colonne.
NOT_A_COLUMN = ("unique", "primary", "foreign", "constraint", "check", "like", "exclude")


def _columns_of_block(block: str) -> Set[str]:
    """Les colonnes d'un corps de `create table`, une par ligne.

    Le commentaire de fin de ligne est retiré avant lecture : dans ce dépôt, il
    porte la justification de la colonne et commence donc souvent par un mot qui
    ressemble à un type.
    """
    columns: Set[str] = set()
    for line in block.splitlines():
        line = line.split("--")[0].strip()
        name = re.match(r"([a-z_][a-z0-9_]*)", line)
        if name and not line.lower().startswith(NOT_A_COLUMN):
            columns.add(name.group(1))
    return columns


def declared_columns(path: pathlib.Path, tables: Iterable[str]) -> Dict[str, Set[str]]:
    """Colonnes déclarées dans le `create table` de chaque table du fichier.

    Le fichier peut déclarer d'autres tables que celles demandées (une migration
    en crée souvent plusieurs) : seules celles de `tables` sont relues.
    """
    text = _without_comments(path.read_text(encoding="utf-8"))
    declared: Dict[str, Set[str]] = {}
    for table in tables:
        match = re.search(
            rf"create table if not exists {table}\s*\((.*?)\n\);", text, re.DOTALL
        )
        if not match:
            raise AssertionError(f"`create table` introuvable pour {table} dans {path.name}")
        declared[table] = _columns_of_block(match.group(1))
    return declared


def _column_changes(statement: str) -> List[Tuple[int, bool, str]]:
    """`(position, ajout ?, colonne)` d'une instruction `alter table`, dans l'ordre.

    L'ordre textuel est ce qui compte : `add column a, drop column a` et
    l'inverse ne laissent pas le même schéma. Une seule instruction peut porter
    les deux (Postgres l'accepte), même si ce dépôt n'en a pas aujourd'hui.
    """
    changes = [(match.start(), True, match.group(2)) for match in ADD_COLUMN.finditer(statement)]
    changes += [(match.start(), False, match.group(2)) for match in DROP_COLUMN.finditer(statement)]
    return sorted(changes)


def _without_comments(text: str) -> str:
    """Le SQL sans ses commentaires, **aux mêmes offsets**.

    Les commentaires sont remplacés par des espaces — jamais supprimés — pour que
    les positions et les numéros de ligne restent ceux du fichier. Sans cela, un
    commentaire qui parle de DDL ferait échouer `unguarded_ddl()` : ces fichiers
    expliquent justement ce qu'ils font, et un contrat qui crie sur un
    commentaire finit par être désactivé.
    """

    def blank(match: re.Match) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return re.sub(r"/\*.*?\*/", blank, re.sub(r"--[^\n]*", blank, text))


def _remember(columns: Dict[str, List[str]], column: str, name: str) -> None:
    """Ajoute la provenance d'une colonne, **sans doublon**.

    Une même colonne est souvent déclarée deux fois dans le même fichier — dans
    le `create table` puis dans l'`alter table … add column` qui répare une table
    déjà en service. Le rapport doit nommer le fichier une fois, pas deux.
    """
    sources = columns.setdefault(column, [])
    if name not in sources:
        sources.append(name)


def declared_schema_by_file(
    paths: Iterable[pathlib.Path],
) -> Dict[str, Dict[str, List[str]]]:
    """Table → colonne → fichiers qui la déclarent, dans l'ordre des migrations.

    Les commentaires sont retirés **avant** lecture, en conservant les positions :
    ces fichiers citent leur propre DDL en prose (« `alter table … add column if
    not exists` »), et une lecture naïve y trouvait une table et une colonne qui
    n'existent pas — une colonne fantôme `if` déclarée sur `users`, que seule
    l'exécution réelle signalait.

    La provenance est la raison d'être de cette variante : quand une base n'a pas
    une colonne, la seule réponse utile est **quel fichier** la déclare — c'est
    ce qui transforme « le schéma a dérivé » en « la migration 006 n'est pas
    passée ». `declared_schema()` n'est que la même lecture sans provenance.

    Trois écritures comptent : la colonne d'un `create table if not exists`,
    celle d'un `alter table … add column if not exists` — c'est ainsi que ce dépôt
    fait évoluer une table déjà en service (migration 009 sur `knowledge_chunks`,
    migrations 005, 006 et 011 pour les colonnes de réparation) — et le retrait
    d'un `alter table … drop column`, qui **annule** une déclaration précédente.

    Les instructions sont lues **dans l'ordre du texte** : le schéma rendu est
    l'état final, celui auquel la base doit ressembler.
    """
    schema: Dict[str, Dict[str, List[str]]] = {}
    for path in paths:
        text = _without_comments(path.read_text(encoding="utf-8"))
        statements: List[Tuple[int, str, str]] = [
            (match.start(), "create", f"{match.group(1)}\n{match.group(2)}")
            for match in CREATE_TABLE.finditer(text)
        ]
        statements += [
            (match.start(), "alter", f"{match.group(1)}\n{match.group(2)}")
            for match in ALTER_TABLE.finditer(text)
        ]
        if "create table" in text.lower() and not any(
            kind == "create" for _, kind, _ in statements
        ):
            raise AssertionError(
                f"`create table` illisible dans {path.name} : le format du fichier a changé"
            )
        for _, kind, payload in sorted(statements):
            table, _, body = payload.partition("\n")
            if kind == "create":
                columns = schema.setdefault(table, {})
                for column in _columns_of_block(body):
                    _remember(columns, column, path.name)
                continue
            changes = _column_changes(body)
            if not changes:
                # Un `alter table … enable row level security` ne déclare aucune
                # colonne : il ne crée pas la table. L'inscrire au schéma ferait
                # réclamer une table que personne ne crée — et rendrait sa faute
                # à une base pourtant juste.
                continue
            columns = schema.setdefault(table, {})
            for _, added, column in changes:
                if added:
                    _remember(columns, column, path.name)
                else:
                    columns.pop(column, None)
    return schema


def added_columns_by_file(
    paths: Iterable[pathlib.Path],
) -> Dict[str, Dict[str, List[str]]]:
    """Table → colonne → fichiers qui l'**ajoutent** à une table existante.

    C'est le complément exact de `declared_schema_by_file()` : une colonne qui
    n'apparaît que dans un `create table` ne peut exister que sur une base créée
    par la migration, alors qu'une colonne posée par `alter table … add column if
    not exists` est **annoncée comme réparable**. C'est la distinction qui dit
    quelles colonnes une base déjà en service peut ne pas avoir — le test
    `tests/test_migrations_apply_to_postgres.py` retire celles-là avant de
    rejouer les migrations, plutôt que d'inventer un schéma ancien.
    """
    added: Dict[str, Dict[str, List[str]]] = {}
    for path in paths:
        text = _without_comments(path.read_text(encoding="utf-8"))
        for table, statement in ALTER_TABLE.findall(text):
            for match in ADD_COLUMN.finditer(statement):
                _remember(added.setdefault(table, {}), match.group(2), path.name)
    return added


def unguarded_ddl(paths: Iterable[pathlib.Path]) -> List[Tuple[str, int, str]]:
    """`(fichier, ligne, DDL)` des instructions **sans** leur garde-fou.

    Une migration de ce dépôt doit pouvoir être rejouée telle quelle : dans
    l'éditeur SQL d'un Supabase en service, et sur une base neuve. Les deux à la
    fois n'est pas une posture — c'est ce qui permet de relancer un fichier
    complété après coup (005, 006, 009, 011 l'ont été).
    """
    unguarded: List[Tuple[str, int, str]] = []
    for path in paths:
        text = _without_comments(path.read_text(encoding="utf-8"))
        for label, pattern in GUARDED_DDL:
            for match in pattern.finditer(text):
                if match.group(1) is None:
                    line = text.count("\n", 0, match.start()) + 1
                    unguarded.append((path.name, line, f"{label} : {match.group(0).strip()}"))
    return unguarded


def declared_schema(paths: Iterable[pathlib.Path]) -> Dict[str, Set[str]]:
    """Table → colonnes, telles que **l'ensemble des migrations** les déclare.

    Sert au test qui applique les migrations à un vrai Postgres, et à
    `scripts/check_schema_drift.py` : c'est la liste contre laquelle le schéma
    **obtenu** est comparé.
    """
    return {
        table: set(columns) for table, columns in declared_schema_by_file(paths).items()
    }


# --------------------------------------------------------------------------- #
# Les objets **autres que les colonnes** : index, contraintes, déclencheurs,
# RLS, policies et privilèges. Même lecture que pour les colonnes — les
# commentaires sont retirés en conservant les positions, parce que ces fichiers
# expliquent leur propre DDL en prose.
# --------------------------------------------------------------------------- #

#: Un `create [unique] index [if not exists] <nom>`.
CREATE_INDEX = re.compile(
    r"create\s+(unique\s+)?index\s+(?:if\s+not\s+exists\s+)?([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
#: La cible d'un index : `on <table> [using <méthode>] (<colonnes clés>)`.
INDEX_TARGET = re.compile(
    r"\bon\s+([a-z_][a-z0-9_]*)\s*(?:using\s+[a-z_][a-z0-9_]*\s*)?\(([^)]*)",
    re.IGNORECASE,
)
#: Un `create trigger <nom>` et, plus loin, `on <table> … execute function <f>`.
CREATE_TRIGGER = re.compile(r"create\s+trigger\s+([a-z_][a-z0-9_]*)", re.IGNORECASE)
TRIGGER_TARGET = re.compile(
    r"\bon\s+([a-z_][a-z0-9_]*)\b.*?execute\s+(?:function\s+)?([a-z_][a-z0-9_]*)",
    re.IGNORECASE | re.DOTALL,
)
#: `alter table [if exists] <table> enable row level security`.
ENABLE_ROW_SECURITY = re.compile(
    r"alter\s+table\s+(?:if\s+exists\s+)?([a-z_][a-z0-9_]*)\s+enable\s+row\s+level\s+security",
    re.IGNORECASE,
)
#: `create policy <nom> on <table>`.
CREATE_POLICY = re.compile(
    r"create\s+policy\s+([a-z_][a-z0-9_]*)\s+on\s+([a-z_][a-z0-9_]*)", re.IGNORECASE
)
#: `revoke <privilège> on <cible> from <rôles>;` — ce que les migrations **ôtent**.
REVOKE = re.compile(
    r"revoke\s+([a-z]+)\s+on\s+([^;]*?)\s+from\s+([^;]+?)\s*;", re.IGNORECASE
)
#: Un préfixe `constraint <nom>` optionnel, devant une contrainte de table.
_CONSTRAINT = r"(?:constraint\s+[a-z_][a-z0-9_]*\s+)?"
#: Contraintes de table, dans un corps de `create table`.
PRIMARY_KEY_TABLE = re.compile(rf"^{_CONSTRAINT}primary\s+key\s*\(([^)]*)\)", re.IGNORECASE)
UNIQUE_TABLE = re.compile(rf"^{_CONSTRAINT}unique\s*\(([^)]*)\)", re.IGNORECASE)
#: Contraintes portées par une ligne de colonne (`id uuid primary key …`).
PRIMARY_KEY_COLUMN = re.compile(
    r"^([a-z_][a-z0-9_]*)\s+.*\bprimary\s+key\b", re.IGNORECASE
)
UNIQUE_COLUMN = re.compile(r"^([a-z_][a-z0-9_]*)\s+.*(?<![a-z_])unique\b", re.IGNORECASE)
#: Une clé étrangère, dans un corps de table ou dans un `alter table`.
FOREIGN_KEY = re.compile(
    r"(?:constraint\s+([a-z_][a-z0-9_]*)\s+)?foreign\s+key\s*\(([^)]*)\)\s*"
    r"references\s+([a-z_][a-z0-9_]*)\s*\(([^)]*)\)([^,;]*)",
    re.IGNORECASE,
)
#: La référence écrite **dans** une ligne de colonne (`media_id uuid references …`).
COLUMN_REFERENCE = re.compile(
    r"^([a-z_][a-z0-9_]*)\s+.*?\breferences\s+([a-z_][a-z0-9_]*)\s*\(([^)]*)\)([^,;]*)",
    re.IGNORECASE,
)
NOT_VALID = re.compile(r"\bnot\s+valid\b", re.IGNORECASE)
#: Les actions `on delete` / `on update`, écrites en mots des deux côtés : la
#: comparaison n'a de sens que si le fichier et Postgres parlent la même langue.
ON_DELETE = re.compile(
    r"on\s+delete\s+(no\s+action|restrict|cascade|set\s+null|set\s+default)", re.IGNORECASE
)
ON_UPDATE = re.compile(
    r"on\s+update\s+(no\s+action|restrict|cascade|set\s+null|set\s+default)", re.IGNORECASE
)


def key_columns(inside: str) -> List[str]:
    """Les colonnes clés d'une liste `(a, b)` — ou d'une définition Postgres.

    Une seule lecture pour les deux côtés : un index partiel ou une opclasse
    (`using hnsw (embedding vector_cosine_ops)`) donnent des définitions écrites
    différemment de part et d'autre, alors que la **colonne** indexée, elle, est
    la même. On ne garde donc que le premier identifiant de chaque élément.
    """
    found: List[str] = []
    for piece in inside.split(","):
        match = re.search(r"[a-z_][a-z0-9_]*", piece)
        if match:
            found.append(match.group(0).lower())
    return found


def _action(pattern: re.Pattern, tail: str) -> str:
    """L'action référentielle écrite dans la suite d'une clé étrangère."""
    match = pattern.search(tail)
    return " ".join(match.group(1).lower().split()) if match else "no action"


def _statement(text: str, start: int) -> str:
    """L'instruction qui commence à `start`, jusqu'à son point-virgule."""
    end = text.find(";", start)
    return text[start : end if end != -1 else len(text)]


def _indexes_of(text: str, name: str) -> Dict[str, Dict[str, Any]]:
    """Les index d'un fichier : nom → table, unicié, colonnes clés."""
    found: Dict[str, Dict[str, Any]] = {}
    for match in CREATE_INDEX.finditer(text):
        statement = _statement(text, match.start())
        target = INDEX_TARGET.search(statement)
        if not target:
            continue
        found[match.group(2)] = {
            "table": target.group(1),
            "unique": bool(match.group(1)),
            "columns": key_columns(target.group(2)),
            "file": name,
        }
    return found


def _triggers_of(text: str, name: str) -> Dict[str, Dict[str, Any]]:
    """Les déclencheurs d'un fichier : nom → table et fonction appelée."""
    found: Dict[str, Dict[str, Any]] = {}
    for match in CREATE_TRIGGER.finditer(text):
        target = TRIGGER_TARGET.search(_statement(text, match.start()))
        if not target:
            continue
        found[match.group(1)] = {
            "table": target.group(1),
            "function": target.group(2),
            "file": name,
        }
    return found


def _constraints_of(text: str, name: str) -> Dict[str, List[Dict[str, Any]]]:
    """Les contraintes d'un fichier, par table.

    Trois écritures comptent, et les trois existent dans ce dépôt : la contrainte
    portée par une ligne de colonne (`id uuid primary key`), la contrainte de
    table (`unique (media_id, chunk_index)`), et la clé étrangère posée après
    coup par un `alter table … add constraint …` (migration 011, sur les tables
    qui existaient déjà).
    """
    found: Dict[str, List[Dict[str, Any]]] = {}

    def remember(table: str, shape: Dict[str, Any]) -> None:
        shapes = found.setdefault(table, [])
        if shape not in shapes:
            shapes.append(shape)

    for block in CREATE_TABLE.finditer(text):
        table, body = block.group(1), block.group(2)
        for line in body.splitlines():
            line = line.split("--")[0].strip()
            match = PRIMARY_KEY_TABLE.match(line)
            if match:
                remember(table, _shape("primary key", key_columns(match.group(1)), name))
                continue
            match = UNIQUE_TABLE.match(line)
            if match:
                remember(table, _shape("unique", key_columns(match.group(1)), name))
                continue
            match = PRIMARY_KEY_COLUMN.match(line)
            if match:
                remember(table, _shape("primary key", [match.group(1).lower()], name))
            match = UNIQUE_COLUMN.match(line)
            if match:
                remember(table, _shape("unique", [match.group(1).lower()], name))
            match = COLUMN_REFERENCE.match(line)
            if match:
                remember(
                    table,
                    _shape(
                        "foreign key",
                        [match.group(1).lower()],
                        name,
                        references=match.group(2),
                        reference_columns=key_columns(match.group(3)),
                        tail=match.group(4),
                    ),
                )

    for match in FOREIGN_KEY.finditer(text):
        table = _table_of(text, match.start())
        if table is None:
            continue
        remember(
            table,
            _shape(
                "foreign key",
                key_columns(match.group(2)),
                name,
                references=match.group(3),
                reference_columns=key_columns(match.group(4)),
                tail=match.group(5),
                constraint=match.group(1),
            ),
        )
    return found


def _table_of(text: str, position: int) -> Optional[str]:
    """La table que vise l'instruction `alter table` qui contient `position`."""
    for match in ALTER_TABLE.finditer(text):
        if match.start() <= position <= match.end():
            return match.group(1)
    return None


def _shape(
    kind: str,
    columns: List[str],
    file: str,
    *,
    references: Optional[str] = None,
    reference_columns: Optional[List[str]] = None,
    tail: str = "",
    constraint: Optional[str] = None,
) -> Dict[str, Any]:
    """Une contrainte décrite par ce qui la **définit**, pas par son nom.

    Le nom ne se compare pas : Postgres nomme lui-même `users_pkey` ou
    `pending_signals_user_id_fkey` quand la migration ne les nomme pas, et deux
    noms différents pour la même contrainte ne veulent rien dire. Ce qui compte
    est la forme — les colonnes, la table visée, les actions, la validation.
    """
    return {
        "kind": kind,
        "columns": columns,
        "references": references,
        "reference_columns": reference_columns or [],
        "on_delete": _action(ON_DELETE, tail) if references else None,
        "on_update": _action(ON_UPDATE, tail) if references else None,
        "validated": not (references and NOT_VALID.search(tail)),
        "constraint": constraint,
        "file": file,
    }


def _revokes_of(text: str, name: str) -> List[Dict[str, Any]]:
    """Les `revoke` d'un fichier : privilège, cible, rôles."""
    found = []
    for match in REVOKE.finditer(text):
        target = " ".join(match.group(2).split())
        roles = [role.strip().lower() for role in match.group(3).split(",") if role.strip()]
        found.append(
            {
                "privilege": match.group(1).lower(),
                "target": target,
                "roles": roles,
                "file": name,
            }
        )
    return found


def _row_security_of(text: str, name: str) -> Dict[str, str]:
    """Les tables dont ce fichier **active** la RLS."""
    return {
        match.group(1): name for match in ENABLE_ROW_SECURITY.finditer(text)
    }


def _policies_of(text: str, name: str) -> Dict[str, Dict[str, Any]]:
    """Les policies que ce fichier crée. Vide est le cas normal, pas un oubli."""
    return {
        match.group(1): {"table": match.group(2), "file": name}
        for match in CREATE_POLICY.finditer(text)
    }


def declared_indexes(paths: Iterable[pathlib.Path]) -> Dict[str, Dict[str, Any]]:
    """Nom d'index → table, unicié, colonnes clés, fichier qui le crée."""
    found: Dict[str, Dict[str, Any]] = {}
    for path in paths:
        text = _without_comments(path.read_text(encoding="utf-8"))
        found.update(_indexes_of(text, path.name))
    return found


def declared_triggers(paths: Iterable[pathlib.Path]) -> Dict[str, Dict[str, Any]]:
    """Nom de déclencheur → table, fonction appelée, fichier qui le crée."""
    found: Dict[str, Dict[str, Any]] = {}
    for path in paths:
        text = _without_comments(path.read_text(encoding="utf-8"))
        found.update(_triggers_of(text, path.name))
    return found


def declared_row_security(paths: Iterable[pathlib.Path]) -> Dict[str, str]:
    """Table → fichier qui **active** la RLS dessus."""
    found: Dict[str, str] = {}
    for path in paths:
        text = _without_comments(path.read_text(encoding="utf-8"))
        for table, source in _row_security_of(text, path.name).items():
            found.setdefault(table, source)
    return found


def declared_policies(paths: Iterable[pathlib.Path]) -> Dict[str, Dict[str, Any]]:
    """Nom de policy → table et fichier. Vide est le cas normal, pas un oubli."""
    found: Dict[str, Dict[str, Any]] = {}
    for path in paths:
        text = _without_comments(path.read_text(encoding="utf-8"))
        found.update(_policies_of(text, path.name))
    return found


def declared_constraints(
    paths: Iterable[pathlib.Path],
) -> Dict[str, List[Dict[str, Any]]]:
    """Table → formes de contraintes déclarées par l'ensemble des migrations."""
    found: Dict[str, List[Dict[str, Any]]] = {}
    for path in paths:
        text = _without_comments(path.read_text(encoding="utf-8"))
        for table, shapes in _constraints_of(text, path.name).items():
            for shape in shapes:
                if shape not in found.setdefault(table, []):
                    found[table].append(shape)
    return found


def declared_revokes(paths: Iterable[pathlib.Path]) -> List[Dict[str, Any]]:
    """Tous les `revoke` des migrations, avec leur cible et leurs rôles."""
    found: List[Dict[str, Any]] = []
    for path in paths:
        text = _without_comments(path.read_text(encoding="utf-8"))
        found.extend(_revokes_of(text, path.name))
    return found


def declared_objects(paths: Iterable[pathlib.Path]) -> Dict[str, Any]:
    """Tout ce que les migrations déclarent **en dehors des colonnes**, en un passage.

    Un seul parcours des fichiers pour les six familles : `check_schema_drift`
    compare tout d'un coup, et relire les mêmes fichiers six fois pour ça serait
    six occasions de lire deux fois la même chose différemment.
    """
    objects: Dict[str, Any] = {
        "indexes": {},
        "triggers": {},
        "row_security": {},
        "policies": {},
        "constraints": {},
        "revokes": [],
    }
    for path in paths:
        text = _without_comments(path.read_text(encoding="utf-8"))
        objects["indexes"].update(_indexes_of(text, path.name))
        objects["triggers"].update(_triggers_of(text, path.name))
        for table, source in _row_security_of(text, path.name).items():
            objects["row_security"].setdefault(table, source)
        objects["policies"].update(_policies_of(text, path.name))
        for table, shapes in _constraints_of(text, path.name).items():
            known = objects["constraints"].setdefault(table, [])
            for shape in shapes:
                if shape not in known:
                    known.append(shape)
        objects["revokes"].extend(_revokes_of(text, path.name))
    return objects
