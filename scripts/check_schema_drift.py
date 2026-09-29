"""Compare les colonnes déclarées par les migrations avec celles de la base.

Pourquoi cet outil existe : les migrations et la base peuvent diverger, **dans les
deux sens**, et aucune des deux divergences ne se voit toute seule.

* une migration **non appliquée** laisse la base en arrière. Le code interroge
  alors une colonne qui n'existe pas, et le symptôme n'arrive qu'à la requête —
  jamais au démarrage ;
* une colonne ajoutée **à la main** en base met les migrations en retard. Cette
  base-là fonctionne, mais la reconstruire depuis les fichiers n'en redonne pas
  une pareille : c'est exactement ce que la migration 011 a réparé pour les trois
  tables historiques, qui n'existaient que dans la base.

Les contrats de migration (`tests/test_core_tables_migration.py`,
`tests/test_engine_tables_migration.py`) relisent le SQL et les sources Python :
ils garantissent que le DDL **décrit** ce que le code utilise, mais ils ne
regardent jamais une base. `tests/test_migrations_apply_to_postgres.py` compare
un schéma réel, mais seulement derrière `MIGRATION_DATABASE_URL` et sous forme
d'assertions. Cet outil est la version qu'on lance sur n'importe quelle base, avec
un rapport qui nomme ce qui manque et **quel fichier** le déclare.

```bash
MIGRATION_DATABASE_URL="postgresql://postgres:motdepasse@db.<projet>.supabase.co:5432/postgres" \\
    python scripts/check_schema_drift.py
python scripts/check_schema_drift.py --database-url "…" --json

# la migration de rattrapage : elle n'est pas appliquée, elle est écrite
python scripts/check_schema_drift.py --repair-sql > database/migrations/012_rattrapage.sql
```

Il est **en lecture seule** : des `select` sur `information_schema`, puis sur
`pg_catalog` pour les objets que `information_schema` ne décrit pas (index,
contraintes, déclencheurs, RLS, privilèges) — rien d'autre, et c'est vérifié sur
les requêtes elles-mêmes, pas seulement promis. Il peut donc viser une production
sans la modifier, ce qui est son intérêt premier : c'est là que la dérive
s'installe.

Quand une colonne est **en base sans être déclarée**, le rapport ne suffit pas :
il faut une migration. `--repair-sql` l'écrit, et il choisit le sens par colonne —
celles que le code utilise sont **déclarées**, celles que rien n'utilise sont
**retirées** (voir `repair_plan()`). Ce qu'il ne sait pas écrire, il le nomme au
lieu de l'inventer.

L'URL vient de `MIGRATION_DATABASE_URL`, la même variable que
`scripts/apply_migrations.py` et aucune autre : viser une base est un geste
explicite, jamais un héritage de l'environnement. Le mot de passe n'est jamais
affiché. La comparaison porte sur le schéma `public` — celui que les migrations
décrivent ; `extensions` et `storage` appartiennent à la plateforme.

Les cinq écarts cherchés sur les **colonnes**, et ce qu'ils veulent dire :

| constat | ce que ça veut dire |
| --- | --- |
| table absente | la migration qui la crée n'est pas passée |
| colonne absente | idem, une migration plus récente que la base. Le fichier qui la déclare est nommé, et le fichier de code qui l'utilise s'il y en a un |
| colonne en base, jamais déclarée | ajoutée à la main, ou migration oubliée |
| table en base, jamais déclarée | idem, au niveau de la table |
| colonne utilisée par le code, déclarée nulle part | ni la base ni les migrations ne la connaissent : l'appel échouera à l'exécution |

Les colonnes ne sont pourtant pas tout ce qu'une base peut perdre. Un index
manquant ne casse rien **tout de suite** : il se manifeste en temps de réponse, et
parfois par un doublon que l'index unique n'a pas empêché. Une RLS désactivée ne
casse rien du tout : elle **ouvre** la table. Un déclencheur perdu ne se voit qu'au
premier `updated_at` périmé. Et un privilège rendu à `anon` ne se voit jamais.
L'outil compare donc aussi les **index**, les **contraintes**, les
**déclencheurs**, la **RLS** (et ses policies) et les **privilèges** — dans les
deux sens, « ce que la base a perdu » et « ce qu'elle a gagné » :

| famille | ce qui est comparé | ce qui compte |
| --- | --- | --- |
| index | nom, table, unicié, colonnes clés | un index absent ne fait pas échouer une requête, il la ralentit ; un index unique absent laisse passer des doublons |
| contraintes | la **forme** (clé primaire, unique, clé étrangère avec sa table visée, ses actions et sa validation), jamais le nom : Postgres nomme lui-même `users_pkey` | une clé étrangère perdue ne se voit qu'au moment où une ligne orpheline arrive |
| déclencheurs | nom, table, fonction appelée | le trigger `updated_at` perdu fige silencieusement les dates |
| RLS | la RLS est-elle **active** sur chaque table que les migrations l'activent | le « deny by default » du dépôt n'est plus qu'un commentaire |
| policies | la base en porte-t-elle, et lesquelles | la RLS du dépôt n'en déclare **aucune** : une policy trouvée est une ouverture |
| privilèges | les `grant` explicites à `anon` / `authenticated` (et à `public` sur les tables) | les `revoke` des migrations 005 à 011 sont défaits |

Une famille n'est comparée que pour les tables **présentes des deux côtés** : une
table entière qui manque est déjà un constat, et lister ses douze objets par-dessus
noierait le rapport (même règle que pour les colonnes d'une table absente).

Codes de sortie : `0` aucune dérive, `1` au moins un écart (ou base
injoignable), `2` usage — les mêmes que `scripts/apply_migrations.py`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(PROJECT_ROOT))

from core import console  # noqa: E402
from scripts import apply_migrations  # noqa: E402
from tests import sql_columns  # noqa: E402

#: Le schéma comparé. `public` est celui que les migrations remplissent ; les
#: schémas `extensions` et `storage` sont ceux de la plateforme, et le comparer
#: ferait crier l'outil sur des objets qui ne lui appartiennent pas.
SCHEMA = "public"

#: Nom porté par la connexion dans `pg_stat_activity`. Volontairement différent
#: de celui de l'applicateur (`APPLICATION_NAME` de `apply_migrations`) : une
#: lecture ne doit pas se faire passer pour une écriture quand les deux outils
#: tournent en même temps.
APPLICATION_NAME = "ai-trade-schema-drift"

#: Les deux seules requêtes de l'outil, et les deux seules qu'il a le droit
#: d'envoyer. `table_type = 'BASE TABLE'` écarte les vues : elles n'ont pas de
#: RLS, aucune migration n'en déclare, et les compter ferait des « tables en
#: trop » qui ne sont pas des tables.
TABLES_QUERY = (
    "select table_name from information_schema.tables "
    "where table_schema = %s and table_type = 'BASE TABLE'"
)
COLUMNS_QUERY = (
    "select table_name, column_name from information_schema.columns where table_schema = %s"
)
#: Le DDL des colonnes, lu **seulement** quand il faut en écrire une : c'est le
#: seul moment où un nom ne suffit pas. `format_type` rend le type réel
#: (`vector(768)`, `numeric(6,2)`, `text[]` — et non `USER-DEFINED`), et le reste
#: dit ce qu'un `add column` ne recopie pas tel quel. Le schéma du type est lu à
#: part : c'est lui qui décide de la qualification.
DEFINITIONS_QUERY = (
    "select c.relname, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), "
    "a.attnotnull, pg_catalog.pg_get_expr(d.adbin, d.adrelid), a.attidentity, "
    "a.attgenerated, tn.nspname "
    "from pg_catalog.pg_attribute a "
    "join pg_catalog.pg_class c on c.oid = a.attrelid "
    "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
    "join pg_catalog.pg_type t on t.oid = a.atttypid "
    "join pg_catalog.pg_namespace tn on tn.oid = t.typnamespace "
    "left join pg_catalog.pg_attrdef d on d.adrelid = a.attrelid and d.adnum = a.attnum "
    "where n.nspname = %s and c.relkind in ('r', 'p') "
    "and a.attnum > 0 and not a.attisdropped"
)

#: Vocabulaire des constats. Nommés ici pour que le rapport, le JSON et les
#: tests parlent du même mot — un `kind` recopié à la main finirait par diverger.
MISSING_TABLE = "table absente"
MISSING_COLUMN = "colonne absente"
UNDECLARED_COLUMN = "colonne en base, jamais déclarée"
UNDECLARED_TABLE = "table en base, jamais déclarée"
CODE_WITHOUT_SCHEMA = "colonne utilisée par le code, déclarée nulle part"

OK = "✅"
FAIL = "❌"

#: Le DDL que l'outil écrit, et **rien d'autre** : déclarer une colonne que la base
#: porte, en retirer une. Les garde-fous ne sont pas décoratifs — le fichier
#: produit doit pouvoir être rejoué, comme n'importe quelle migration du dépôt, et
#: `sql_columns.unguarded_ddl()` le vérifie sur ce fichier.
ADD_COLUMN_SQL = "alter table if exists {table} add column if not exists {column} {definition};"
DROP_COLUMN_SQL = "alter table if exists {table} drop column if exists {column};"

#: Schémas dont un type s'écrit sans qualification. Tout le reste est préfixé :
#: `vector` vit dans `extensions`, et un fichier qui écrit `vector(768)` ne
#: s'applique que là où `extensions` est dans le `search_path` — la leçon des
#: migrations 008 et 009.
UNQUALIFIED_TYPE_SCHEMAS = ("public", "pg_catalog")


def declared_schema(files: Optional[Sequence[Path]] = None) -> Dict[str, Dict[str, List[str]]]:
    """Ce que les migrations déclarent : table → colonne → fichiers qui la déclarent.

    Passe par `tests/sql_columns.py` et non par une lecture écrite ici : c'est la
    même extraction que celle des contrats de migration. Deux extractions
    finiraient par décrire deux schémas, et l'outil validerait une base que les
    tests refusent.
    """
    return sql_columns.declared_schema_by_file(
        files if files is not None else apply_migrations.migration_files()
    )


def read_schema(connection: Any) -> Tuple[Set[str], Dict[str, Set[str]]]:
    """Le schéma réel : les tables de `public`, et leurs colonnes.

    Lecture seule, et rien que de la lecture : c'est ce qui autorise à viser une
    base de production avec cet outil.
    """
    tables: Set[str] = set()
    columns: Dict[str, Set[str]] = {}
    with connection.cursor() as cursor:
        cursor.execute(TABLES_QUERY, (SCHEMA,))
        for row in cursor.fetchall():
            tables.add(row[0])
        cursor.execute(COLUMNS_QUERY, (SCHEMA,))
        for table, column in cursor.fetchall():
            columns.setdefault(table, set()).add(column)
    return tables, columns


def _finding(kind: str, table: str, obj: str, detail: str) -> Dict[str, str]:
    """Un écart. `obj` est ce que vise le constat : une colonne, un index, un
    déclencheur, une contrainte, une policy, un rôle — ou rien, quand le constat
    porte sur la table elle-même (la RLS s'active sur une table, pas sur un objet).
    """
    return {"kind": kind, "table": table, "object": obj, "detail": detail}


#: Les index de `public`, **sauf** ceux qu'une contrainte porte. L'index d'une clé
#: primaire ou d'une unicité n'est pas un `create index` : il appartient à sa
#: contrainte, et le compter ici ferait crier l'outil sur toutes les bases justes
#: (chaque `primary key` en porte un).
INDEXES_QUERY = (
    "select i.relname, t.relname, x.indisunique, pg_catalog.pg_get_indexdef(x.indexrelid) "
    "from pg_catalog.pg_index x "
    "join pg_catalog.pg_class i on i.oid = x.indexrelid "
    "join pg_catalog.pg_class t on t.oid = x.indrelid "
    "join pg_catalog.pg_namespace n on n.oid = t.relnamespace "
    "where n.nspname = %s and t.relkind in ('r', 'p') and not exists ("
    "  select 1 from pg_catalog.pg_constraint c where c.conindid = x.indexrelid)"
)
#: Les contraintes, avec leurs colonnes **en clair** (les noms de colonnes vivent
#: dans `pg_attribute`, indexés par des numéros d'attribut) et leur définition
#: complète. `contype` : `p` clé primaire, `u` unicité, `f` clé étrangère, `c`
#: `check`. Les contraintes `not null` (`n`) ne sont volontairement pas lues :
#: PostgreSQL n'en fait des contraintes qu'à partir de la 17, et une base Supabase
#: peut aussi bien être en 15, 16 ou 17 — la nullabilité des colonnes, elle, est
#: déjà comparée par `COLUMNS_QUERY`.
CONSTRAINTS_QUERY = (
    "select t.relname, c.conname, c.contype, c.convalidated, c.confdeltype, c.confupdtype, "
    "rt.relname, pg_catalog.pg_get_constraintdef(c.oid), "
    "coalesce(k.names, array[]::text[]), coalesce(f.names, array[]::text[]) "
    "from pg_catalog.pg_constraint c "
    "join pg_catalog.pg_class t on t.oid = c.conrelid "
    "join pg_catalog.pg_namespace n on n.oid = t.relnamespace "
    "left join pg_catalog.pg_class rt on rt.oid = c.confrelid "
    "left join lateral (select array_agg(a.attname order by key.ord) as names "
    "  from unnest(c.conkey) with ordinality as key(attnum, ord) "
    "  join pg_catalog.pg_attribute a on a.attrelid = c.conrelid and a.attnum = key.attnum) k "
    "  on true "
    "left join lateral (select array_agg(a.attname order by key.ord) as names "
    "  from unnest(c.confkey) with ordinality as key(attnum, ord) "
    "  join pg_catalog.pg_attribute a on a.attrelid = c.confrelid and a.attnum = key.attnum) f "
    "  on true "
    "where n.nspname = %s and c.contype in ('p', 'u', 'f', 'c')"
)
#: Les déclencheurs, avec la fonction qu'ils appellent : `tgisinternal` écarte ceux
#: que Postgres crée lui-même pour les clés étrangères, qui ne sont pas des
#: déclencheurs du projet.
TRIGGERS_QUERY = (
    "select g.tgname, t.relname, coalesce(p.proname, ''), pg_catalog.pg_get_triggerdef(g.oid) "
    "from pg_catalog.pg_trigger g "
    "join pg_catalog.pg_class t on t.oid = g.tgrelid "
    "join pg_catalog.pg_namespace n on n.oid = t.relnamespace "
    "left join pg_catalog.pg_proc p on p.oid = g.tgfoid "
    "where n.nspname = %s and not g.tgisinternal"
)
#: La RLS, table par table. C'est la seule comparaison dont l'absence **ouvre** la
#: base au lieu de la ralentir.
ROW_SECURITY_QUERY = (
    "select c.relname, c.relrowsecurity, c.relforcerowsecurity "
    "from pg_catalog.pg_class c "
    "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
    "where n.nspname = %s and c.relkind in ('r', 'p')"
)
#: Les policies existantes. Le dépôt n'en déclare aucune (« deny by default ») :
#: chaque ligne trouvée est une ouverture de plus que les migrations.
POLICIES_QUERY = (
    "select policyname, tablename, cmd, roles::text[] "
    "from pg_catalog.pg_policies where schemaname = %s"
)
#: Les privilèges **explicites** sur les tables, tels que `grant` les écrit.
#: `aclexplode` ne rend rien quand la colonne de droits est `null` — le cas d'une
#: table dont personne n'a modifié les droits, et précisément l'état attendu après
#: les `revoke` des migrations. Un `grantee` à 0 est `PUBLIC` (tout le monde), et
#: `pg_roles` ne connaît pas cet oid : d'où le `left join` et le repli.
TABLE_PRIVILEGES_QUERY = (
    "select c.relname, coalesce(r.rolname, 'public'), a.privilege_type "
    "from pg_catalog.pg_class c "
    "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
    "cross join lateral pg_catalog.aclexplode(c.relacl) a "
    "left join pg_catalog.pg_roles r on r.oid = a.grantee "
    "where n.nspname = %s and c.relkind in ('r', 'p')"
)
#: Les mêmes droits, sur les fonctions. `PUBLIC` y a `execute` **par défaut**, et
#: aucune migration ne le révoque : c'est la comparaison qui l'écarte, pas la
#: requête (voir `compare_objects`).
FUNCTION_PRIVILEGES_QUERY = (
    "select p.proname, coalesce(r.rolname, 'public'), a.privilege_type "
    "from pg_catalog.pg_proc p "
    "join pg_catalog.pg_namespace n on n.oid = p.pronamespace "
    "cross join lateral pg_catalog.aclexplode(p.proacl) a "
    "left join pg_catalog.pg_roles r on r.oid = a.grantee "
    "where n.nspname = %s"
)
#: `pg_constraint.contype` → le mot qu'emploient les migrations.
CONSTRAINT_KINDS = {
    "p": "primary key",
    "u": "unique",
    "f": "foreign key",
    "c": "check",
}

#: Les actions référentielles, dans les mots des migrations. Postgres les rend en
#: une lettre (`confdeltype`) : sans cette traduction, la comparaison verrait une
#: différence partout, ou nulle part — selon celle des deux langues qu'on écrirait.
REFERENTIAL_ACTIONS = {
    "a": "no action",
    "r": "restrict",
    "c": "cascade",
    "n": "set null",
    "d": "set default",
}


def read_definitions(connection: Any) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Le DDL de chaque colonne de `public`, indexé par `(table, colonne)`.

    Lecture seule, comme le reste : `pg_catalog` décrit ce que Postgres a
    réellement, là où `information_schema` ne rend que des noms.
    """
    with connection.cursor() as cursor:
        cursor.execute(DEFINITIONS_QUERY, (SCHEMA,))
        rows = cursor.fetchall()
    return {
        (table, column): {
            "type": type_name,
            "type_schema": type_schema,
            "not_null": bool(not_null),
            "default": default,
            "identity": identity or "",
            "generated": generated or "",
        }
        for table, column, type_name, not_null, default, identity, generated, type_schema in rows
    }


def _qualified_type(definition: Dict[str, Any]) -> str:
    """Le type, qualifié s'il ne vit pas dans `public`.

    `format_type` ne qualifie que ce qui n'est pas visible depuis le
    `search_path` de **la session qui interroge** — c'est-à-dire une réponse qui
    dépend de celui qui pose la question. La règle est donc écrite ici : un type
    hors `public` porte son schéma, et le fichier produit s'applique partout.
    """
    type_name = str(definition.get("type") or "")
    schema = str(definition.get("type_schema") or "")
    if not schema or schema in UNQUALIFIED_TYPE_SCHEMAS or "." in type_name:
        return type_name
    return f"{schema}.{type_name}"


def _balanced(text: str, start: int) -> str:
    r"""Le contenu de la parenthèse ouverte à `start`, fermeture comprise.

    Un simple `\((.*?)\)` s'arrête à la première fermante : sur
    `(lower(email))` ou `(asset, (regime))` il rendrait la mauvaise chose, et une
    comparaison qui lit mal est pire qu'une comparaison qui ne lit pas.
    """
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index]
    return text[start + 1 :]


#: Le début de la liste des colonnes clés, dans la définition que Postgres rend :
#: `CREATE INDEX <nom> ON public.<table> USING <méthode> (<colonnes>) [WHERE …]`.
INDEXDEF_KEYS = re.compile(r"\busing\s+[a-z_][a-z0-9_]*\s*\(", re.IGNORECASE)


def indexdef_columns(definition: str) -> List[str]:
    """Les colonnes clés d'un index, lues dans `pg_get_indexdef`."""
    match = INDEXDEF_KEYS.search(definition or "")
    if not match:
        return []
    return sql_columns.key_columns(_balanced(definition, match.end() - 1))


def read_objects(connection: Any) -> Dict[str, Any]:
    """Tout ce que la base porte **en dehors des colonnes**. Lecture seule.

    `pg_catalog` et non `information_schema` : les index, les contraintes, les
    déclencheurs, la RLS et les droits explicites n'y sont décrits nulle part
    ailleurs. Aucune de ces requêtes n'écrit, et un test le vérifie sur le
    curseur — c'est ce qui autorise l'outil à viser une production.
    """
    objects: Dict[str, Any] = {
        "indexes": {},
        "constraints": {},
        "triggers": {},
        "row_security": {},
        "policies": {},
        "table_privileges": [],
        "function_privileges": [],
    }
    with connection.cursor() as cursor:
        cursor.execute(INDEXES_QUERY, (SCHEMA,))
        for name, table, unique, definition in cursor.fetchall():
            objects["indexes"][name] = {
                "table": table,
                "unique": bool(unique),
                "columns": indexdef_columns(definition),
                "definition": definition,
            }
        cursor.execute(CONSTRAINTS_QUERY, (SCHEMA,))
        for row in cursor.fetchall():
            table, name, kind, validated, on_delete, on_update, referenced, definition = row[:8]
            columns, reference_columns = row[8], row[9]
            objects["constraints"].setdefault(table, []).append(
                {
                    "name": name,
                    "kind": CONSTRAINT_KINDS.get(kind, kind),
                    "columns": list(columns),
                    "references": referenced,
                    "reference_columns": list(reference_columns),
                    "on_delete": REFERENTIAL_ACTIONS.get(on_delete, on_delete) if referenced else None,
                    "on_update": REFERENTIAL_ACTIONS.get(on_update, on_update) if referenced else None,
                    "validated": bool(validated),
                    "definition": definition,
                }
            )
        cursor.execute(TRIGGERS_QUERY, (SCHEMA,))
        for name, table, function, definition in cursor.fetchall():
            objects["triggers"][name] = {
                "table": table,
                "function": function,
                "definition": definition,
            }
        cursor.execute(ROW_SECURITY_QUERY, (SCHEMA,))
        for table, enabled, forced in cursor.fetchall():
            objects["row_security"][table] = {
                "enabled": bool(enabled),
                "forced": bool(forced),
            }
        cursor.execute(POLICIES_QUERY, (SCHEMA,))
        for name, table, command, roles in cursor.fetchall():
            objects["policies"][name] = {
                "table": table,
                "command": command,
                "roles": list(roles or []),
            }
        cursor.execute(TABLE_PRIVILEGES_QUERY, (SCHEMA,))
        for table, role, privilege in cursor.fetchall():
            objects["table_privileges"].append(
                {"table": table, "role": role, "privilege": privilege}
            )
        cursor.execute(FUNCTION_PRIVILEGES_QUERY, (SCHEMA,))
        for function, role, privilege in cursor.fetchall():
            objects["function_privileges"].append(
                {"function": function, "role": role, "privilege": privilege}
            )
    return objects


def render_definition(definition: Dict[str, Any]) -> str:
    """Ce qu'on écrit après le nom de la colonne : type, `not null`, défaut."""
    pieces = [_qualified_type(definition)]
    if definition.get("not_null"):
        pieces.append("not null")
    if definition.get("default"):
        pieces.append(f"default {definition['default']}")
    return " ".join(piece for piece in pieces if piece)


def _manual_reason(definition: Optional[Dict[str, Any]]) -> str:
    """Pourquoi cette colonne ne peut pas être déclarée ici — `""` si elle peut.

    Trois cas qu'on refuse d'écrire, parce que les écrire mal serait pire que ne
    rien écrire : une colonne `identity` et une colonne calculée portent une
    définition qui ne se recopie pas hors de la table (séquence, expression), et
    un défaut qui appelle `nextval` référence une séquence qui n'existe pas sur
    une base neuve. Le fichier de rattrapage s'applique aussi ailleurs : il ne
    doit pas y échouer.
    """
    if not definition:
        return "son DDL n'a pas pu être lu en base"
    if definition.get("identity"):
        return "colonne `identity` : la séquence qui la porte ne se recopie pas ici"
    if definition.get("generated"):
        return "colonne calculée : l'expression de génération doit être relue"
    if "nextval(" in str(definition.get("default") or ""):
        return "son défaut appelle une séquence (`nextval`) : elle n'existe pas ailleurs"
    return ""


def _sources(by_file: Dict[str, Iterable[str]], column: str) -> List[str]:
    """Les fichiers qui touchent une colonne, dans l'ordre de lecture."""
    return sorted(path for path, columns in by_file.items() if column in set(columns))


def _used_here(used: Dict[str, Dict[str, Iterable[str]]], table: str) -> Set[str]:
    """Les colonnes de cette table que le code touche, tous fichiers confondus."""
    found: Set[str] = set()
    for columns in used.get(table, {}).values():
        found |= set(columns)
    return found


def _declarers(sources: Iterable[str]) -> str:
    """« 005_forex_factory.sql, 009_knowledge_vectors.sql », sans doublon ni ordre au hasard."""
    seen: List[str] = []
    for name in sources:
        if name not in seen:
            seen.append(name)
    return ", ".join(seen) or "(aucun fichier)"


def compare(
    declared: Dict[str, Dict[str, List[str]]],
    real_tables: Iterable[str],
    real_columns: Dict[str, Iterable[str]],
    used_by_code: Optional[Dict[str, Dict[str, Iterable[str]]]] = None,
) -> List[Dict[str, str]]:
    """Les écarts entre ce que les migrations déclarent et ce que la base porte.

    Fonction pure : elle ne connaît ni connexion ni fichier, donc elle se teste
    sur des schémas écrits à la main — y compris ceux qu'aucune base ne porte.

    Les colonnes d'une table absente ne sont pas listées une à une : la table
    manque, cela dit déjà tout, et une table de vingt colonnes noierait le
    rapport sous vingt lignes qui n'apprennent rien de plus.
    """
    real = {table: set(columns) for table, columns in real_columns.items()}
    present = set(real_tables)
    used = used_by_code or {}
    findings: List[Dict[str, str]] = []

    for table in sorted(declared):
        sources = declared[table]
        if table not in present:
            findings.append(
                _finding(
                    MISSING_TABLE,
                    table,
                    "",
                    f"absente de {SCHEMA}, déclarée dans "
                    f"{_declarers(name for names in sources.values() for name in names)} "
                    "— migration non appliquée ?",
                )
            )
            continue

        real_here = real.get(table, set())
        used_here = _used_here(used, table)

        for column in sorted(set(sources) - real_here):
            note = ""
            if column in used_here:
                note = f", utilisée par le code dans {_declarers(_sources(used[table], column))}"
            findings.append(
                _finding(
                    MISSING_COLUMN,
                    table,
                    column,
                    f"déclarée dans {_declarers(sources[column])}{note} "
                    "— migration non appliquée ?",
                )
            )

        for column in sorted(real_here - set(sources)):
            findings.append(
                _finding(
                    UNDECLARED_COLUMN,
                    table,
                    column,
                    "présente en base, déclarée par aucune migration — ajoutée à la main, "
                    "ou migration oubliée : à déclarer, ou à retirer",
                )
            )

        for column in sorted(used_here - set(sources) - real_here):
            findings.append(
                _finding(
                    CODE_WITHOUT_SCHEMA,
                    table,
                    column,
                    f"utilisée par le code dans {_declarers(_sources(used[table], column))}, "
                    "et absente des migrations comme de la base",
                )
            )

    for table in sorted(present - set(declared)):
        findings.append(
            _finding(
                UNDECLARED_TABLE,
                table,
                "",
                f"présente dans {SCHEMA}, déclarée par aucune migration — table oubliée, "
                "ou objet de plateforme à ne pas compter",
            )
        )
    return findings


#: Les constats sur les objets autres que les colonnes. Le sens est nommé dans le
#: constat lui-même (« absent » = la base a perdu, « en base, jamais déclaré » =
#: elle a gagné) : un rapport qui dit seulement « différent » oblige à ouvrir la
#: base pour savoir de quel côté est l'écart.
LOST_INDEX = "index absent"
GAINED_INDEX = "index en base, jamais déclaré"
DIFFERENT_INDEX = "index différent"
LOST_CONSTRAINT = "contrainte absente"
GAINED_CONSTRAINT = "contrainte en base, jamais déclarée"
DIFFERENT_CONSTRAINT = "contrainte différente"
LOST_TRIGGER = "déclencheur absent"
GAINED_TRIGGER = "déclencheur en base, jamais déclaré"
DIFFERENT_TRIGGER = "déclencheur différent"
RLS_OFF = "RLS désactivée"
RLS_UNDECLARED = "RLS activée, jamais déclarée"
GAINED_POLICY = "policy en base, aucune n'est déclarée"
LOST_POLICY = "policy déclarée, absente"
GRANTED_PRIVILEGE = "privilège accordé"


def privilege_expectations(
    declared_objects: Dict[str, Any], declared: Dict[str, Dict[str, List[str]]]
) -> Dict[str, Any]:
    """Ce que les `revoke` des migrations exigent : quel rôle n'a **rien** où.

    Le modèle vient des fichiers, jamais d'une liste écrite ici : `007` révoque
    pour toutes les tables de `public`, `009` révoque l'`execute` d'une fonction,
    et les autres révoquent table par table. Une migration qui disparaîtrait du
    dépôt emporterait son exigence — l'outil compare une base à **ses**
    migrations, il n'a pas d'opinion à leur place.
    """
    roles: Set[str] = set()
    tables: Dict[str, str] = {}
    functions: Dict[str, str] = {}
    for revoke in declared_objects.get("revokes", []):
        roles |= set(revoke["roles"])
        target = revoke["target"].lower()
        if "all tables in schema" in target:
            for table in declared:
                tables.setdefault(table, revoke["file"])
            continue
        name = target.replace("function", "", 1).strip().split("(")[0].strip()
        if target.startswith("function") or "(" in target:
            functions.setdefault(name, revoke["file"])
        else:
            tables.setdefault(name, revoke["file"])
    return {"roles": sorted(roles), "tables": tables, "functions": functions}


def granted_privileges(
    real: Dict[str, Any], expectations: Dict[str, Any], tables: Iterable[str]
) -> List[Dict[str, str]]:
    """Les droits explicites que les migrations révoquent, constat par constat.

    Une seule fonction pour les **lire** et pour les **compter** : le rapport
    annonce combien la base en accorde, et ce nombre doit être celui des constats.
    Deux filtres écrits à deux endroits finiraient par diverger, et le rapport
    annoncerait alors un compte que son propre détail ne montre pas.

    `public` sur une table, c'est tout le monde, `anon` compris : les `revoke` du
    dépôt ne le nomment pas, mais l'intention est la même. Sur une **fonction**,
    au contraire, `public` a `execute` par défaut et aucune migration ne le
    révoque — l'exiger ferait crier l'outil sur toutes les bases justes.
    """
    roles = set(expectations.get("roles", []))
    perimeter = set(tables)
    found: List[Dict[str, str]] = []
    for entry in real.get("table_privileges", []):
        table = entry["table"]
        if table not in expectations.get("tables", {}) or table not in perimeter:
            continue
        if entry["role"] not in roles | {"public"}:
            continue
        found.append(
            _finding(
                GRANTED_PRIVILEGE,
                table,
                entry["role"],
                f"`{entry['privilege'].lower()}` accordé à {entry['role']} en base, alors que "
                f"{expectations['tables'][table]} le révoque",
            )
        )
    for entry in real.get("function_privileges", []):
        function = entry["function"]
        if function not in expectations.get("functions", {}):
            continue
        if entry["role"] not in roles:
            continue
        found.append(
            _finding(
                GRANTED_PRIVILEGE,
                function,
                entry["role"],
                f"`{entry['privilege'].lower()}` accordé à {entry['role']} sur la fonction, "
                f"alors que {expectations['functions'][function]} le révoque",
            )
        )
    return found


def _shape_key(shape: Dict[str, Any]) -> Tuple[Any, ...]:
    """Ce qui **définit** une contrainte — et surtout pas son nom."""
    return (
        shape["kind"],
        tuple(shape["columns"]),
        shape["references"],
        tuple(shape["reference_columns"]),
        shape["on_delete"],
        shape["on_update"],
        shape["validated"],
    )


def _shape_core(shape: Dict[str, Any]) -> Tuple[Any, ...]:
    """La contrainte sans ses détails : de quoi la **reconnaître**.

    Reconnaître, puis comparer : une clé étrangère dont les actions ou la
    validation ont changé est la **même** contrainte modifiée, pas une contrainte
    perdue accompagnée d'une contrainte apparue. Sans cette distinction, un
    `validate constraint` produirait deux constats pour un seul écart.
    """
    return (
        shape["kind"],
        tuple(shape["columns"]),
        shape["references"],
        tuple(shape["reference_columns"]),
    )


def _shape_details_differences(
    declared: Dict[str, Any], real: Dict[str, Any]
) -> List[str]:
    """Ce qui diffère entre deux contraintes reconnues comme la même."""
    differences = []
    if declared["on_delete"] != real["on_delete"]:
        differences.append(f"on delete {real['on_delete']} en base")
    if declared["on_update"] != real["on_update"]:
        differences.append(f"on update {real['on_update']} en base")
    if declared["validated"] != real["validated"]:
        differences.append("validée en base" if real["validated"] else "`not valid` en base")
    return differences


def shape_label(shape: Dict[str, Any]) -> str:
    """Une contrainte dite en mots : `clé étrangère (user_id → users.id)`.

    Postgres nomme lui-même `users_pkey` ou `pending_signals_user_id_fkey` : le
    nom ne se compare pas, il se lit.
    """
    columns = ", ".join(shape["columns"])
    if shape["kind"] != "foreign key":
        return f"{shape['kind']} ({columns})"
    label = (
        f"{shape['kind']} ({columns} → "
        f"{shape['references']}.{', '.join(shape['reference_columns'])})"
    )
    details = []
    if shape["on_delete"] and shape["on_delete"] != "no action":
        details.append(f"on delete {shape['on_delete']}")
    if shape["on_update"] and shape["on_update"] != "no action":
        details.append(f"on update {shape['on_update']}")
    if not shape["validated"]:
        details.append("not valid")
    return f"{label} ({', '.join(details)})" if details else label


def _index_differences(declared: Dict[str, Any], real: Dict[str, Any]) -> List[str]:
    """Ce qui diffère entre l'index déclaré et celui qui porte le même nom."""
    differences = []
    if declared["table"] != real["table"]:
        differences.append(f"il porte sur {real['table']} en base")
    if declared["unique"] != real["unique"]:
        differences.append("unique en base" if real["unique"] else "non unique en base")
    if declared["columns"] != real["columns"]:
        differences.append(
            f"colonnes clés ({', '.join(real['columns']) or 'aucune'}) "
            f"au lieu de ({', '.join(declared['columns'])})"
        )
    return differences


def compare_objects(
    declared: Dict[str, Any],
    real: Dict[str, Any],
    declared_tables: Iterable[str],
    real_tables: Iterable[str],
    privileges: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, str]]:
    """Les écarts sur les index, contraintes, déclencheurs, RLS, policies et droits.

    Fonction pure, comme `compare()` : elle se teste sur des catalogues écrits à
    la main. Les deux côtés subissent la même règle — une famille n'est comparée
    que sur une table **présente des deux côtés** : une table absente est déjà un
    constat, et ses douze objets par-dessus noieraient le rapport.

    Ce que la comparaison ne voit pas, et ne prétend pas voir : la définition
    exacte d'un index (méthode d'accès, opclasse, prédicat d'un index partiel) et
    le corps d'un déclencheur. Un `pg_get_indexdef` recopié à la main diffère de
    son origine par des parenthèses et des qualifications de schéma ; crier sur
    ça ferait désactiver l'outil, or c'est justement ce qu'il faut éviter. Les
    définitions sont portées dans le rapport JSON, pour qui veut les relire.
    """
    both = set(declared_tables) & set(real_tables)
    findings: List[Dict[str, str]] = []

    for name in sorted(declared.get("indexes", {})):
        spec = declared["indexes"][name]
        if spec["table"] not in both:
            continue
        current = real.get("indexes", {}).get(name)
        if current is None:
            findings.append(
                _finding(
                    LOST_INDEX,
                    spec["table"],
                    name,
                    f"déclaré dans {spec['file']} sur ({', '.join(spec['columns'])}) — la base "
                    "ne l'a pas",
                )
            )
            continue
        differences = _index_differences(spec, current)
        if differences:
            findings.append(
                _finding(
                    DIFFERENT_INDEX,
                    current["table"],
                    name,
                    f"{' ; '.join(differences)} (déclaré dans {spec['file']})",
                )
            )
    for name in sorted(real.get("indexes", {})):
        spec = real["indexes"][name]
        if name in declared.get("indexes", {}) or spec["table"] not in both:
            continue
        findings.append(
            _finding(
                GAINED_INDEX,
                spec["table"],
                name,
                f"({', '.join(spec['columns']) or 'sans colonne clé'}) créé par aucune migration",
            )
        )

    for table in sorted(declared.get("constraints", {})):
        if table not in both:
            continue
        wanted = list(declared["constraints"][table])
        present = list(real.get("constraints", {}).get(table, []))
        for shape in sorted(wanted, key=shape_label):
            exact = [other for other in present if _shape_key(other) == _shape_key(shape)]
            if exact:
                present.remove(exact[0])
                continue
            same = [
                other for other in present if _shape_core(other) == _shape_core(shape)
            ]
            if same:
                present.remove(same[0])
                findings.append(
                    _finding(
                        DIFFERENT_CONSTRAINT,
                        table,
                        shape_label(shape),
                        f"{' ; '.join(_shape_details_differences(shape, same[0]))} "
                        f"(déclarée dans {shape['file']})",
                    )
                )
                continue
            findings.append(
                _finding(
                    LOST_CONSTRAINT,
                    table,
                    shape_label(shape),
                    f"déclarée dans {shape['file']} — la base ne la porte pas",
                )
            )
        for shape in sorted(present, key=shape_label):
            findings.append(
                _finding(
                    GAINED_CONSTRAINT,
                    table,
                    shape["name"] or shape_label(shape),
                    f"{shape_label(shape)} en base, déclarée par aucune migration "
                    f"— « {shape['definition']} »",
                )
            )

    for name in sorted(declared.get("triggers", {})):
        spec = declared["triggers"][name]
        if spec["table"] not in both:
            continue
        current = real.get("triggers", {}).get(name)
        if current is None:
            findings.append(
                _finding(
                    LOST_TRIGGER,
                    spec["table"],
                    name,
                    f"déclaré dans {spec['file']} et absent de la base — les `updated_at` "
                    "cessent d'être tenus sans que rien ne le dise",
                )
            )
            continue
        differences = []
        if current["table"] != spec["table"]:
            differences.append(f"il garde {current['table']} en base")
        if current["function"] != spec["function"]:
            differences.append(f"il appelle {current['function']}() en base")
        if differences:
            findings.append(
                _finding(
                    DIFFERENT_TRIGGER,
                    spec["table"],
                    name,
                    f"{' ; '.join(differences)} (déclaré dans {spec['file']})",
                )
            )
    for name in sorted(real.get("triggers", {})):
        spec = real["triggers"][name]
        if name in declared.get("triggers", {}) or spec["table"] not in both:
            continue
        findings.append(
            _finding(
                GAINED_TRIGGER,
                spec["table"],
                name,
                f"il appelle {spec['function']}() — créé par aucune migration",
            )
        )

    for table in sorted(declared.get("row_security", {})):
        if table not in both:
            continue
        if not real.get("row_security", {}).get(table, {}).get("enabled"):
            findings.append(
                _finding(
                    RLS_OFF,
                    table,
                    "",
                    f"{declared['row_security'][table]} l'active, la base ne l'a pas : la table "
                    "n'est plus protégée par la RLS (« deny by default »)",
                )
            )
    for table in sorted(real.get("row_security", {})):
        if table not in both or table in declared.get("row_security", {}):
            continue
        if real["row_security"][table].get("enabled"):
            findings.append(
                _finding(
                    RLS_UNDECLARED,
                    table,
                    "",
                    "RLS active en base, qu'aucune migration n'active — quelqu'un l'a posée à la main",
                )
            )

    for name in sorted(real.get("policies", {})):
        spec = real["policies"][name]
        if spec["table"] not in both:
            continue
        roles = ", ".join(spec["roles"]) or "tout le monde"
        findings.append(
            _finding(
                GAINED_POLICY,
                spec["table"],
                name,
                f"`{spec['command'].lower()}` pour {roles} — les migrations n'en déclarent "
                "aucune : c'est une ouverture de plus que la RLS « deny by default »",
            )
        )
    for name in sorted(declared.get("policies", {})):
        spec = declared["policies"][name]
        if spec["table"] not in both or name in real.get("policies", {}):
            continue
        findings.append(
            _finding(
                LOST_POLICY,
                spec["table"],
                name,
                f"déclarée dans {spec['file']} et absente de la base",
            )
        )

    findings.extend(granted_privileges(real, privileges or {}, both))
    return findings


def repair_plan(
    declared: Dict[str, Dict[str, List[str]]],
    real_tables: Iterable[str],
    real_columns: Dict[str, Iterable[str]],
    used_by_code: Optional[Dict[str, Dict[str, Iterable[str]]]] = None,
    definitions: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    objects: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Ce qu'il faudrait écrire pour que cette base et les migrations se rejoignent.

    Fonction pure, comme `compare()` : elle se teste sur des schémas écrits à la
    main. Un écart de colonne se répare dans **un** sens, et lequel ne se devine
    pas — il se lit dans l'usage :

    * une colonne que la base porte, que rien ne déclare et que **le code
      utilise** est déclarée. C'est le cas que la migration 011 a réparé : trois
      tables n'existaient que dans la base, et le code s'en servait ;
    * une colonne que la base porte, que rien ne déclare et que **personne
      n'utilise** est retirée : c'est la seule façon de supprimer l'écart sans
      inscrire dans les migrations une colonne dont personne ne veut. Le fichier
      le dit à voix haute — un retrait détruit des données ;
    * une colonne dont le DDL **ne se recopie pas** (identity, colonne calculée,
      défaut sur une séquence) n'est ni déclarée ni retirée : elle est nommée
      dans `manual`. On ne devine pas un type, et on ne retire pas ce qu'on n'a
      pas su décrire.

    Les autres écarts ne se réparent pas ici : `missing` (déclaré, absent de la
    base) est du ressort de `scripts/apply_migrations.py`, `undeclared_tables`
    demande une table entière écrite à la main, et `code_only` (le code utilise
    une colonne que la base et les migrations ignorent) ne se déduit pas d'un
    appel : rien n'en donne le type.
    """
    real = {table: set(columns) for table, columns in real_columns.items()}
    present = set(real_tables)
    used = used_by_code or {}
    known = definitions or {}
    plan: Dict[str, List[Dict[str, Any]]] = {
        "declare": [],
        "drop": [],
        "manual": [],
        "undeclared_tables": [],
        "code_only": [],
        "missing": [],
        # Les écarts d'index, de contraintes, de déclencheurs, de RLS et de droits
        # sont **portés** par le plan sans être réparés par le fichier : les taire
        # ferait produire une migration qui a l'air complète et ne l'est pas.
        "objects": list(objects or []),
    }

    for table in sorted(declared):
        declared_here = set(declared[table])
        used_here = _used_here(used, table)
        if table not in present:
            plan["missing"].append({"table": table, "column": ""})
            continue
        real_here = real.get(table, set())
        for column in sorted(real_here - declared_here):
            reason = _manual_reason(known.get((table, column)))
            if reason:
                plan["manual"].append({"table": table, "column": column, "reason": reason})
            elif column in used_here:
                plan["declare"].append(
                    {
                        "table": table,
                        "column": column,
                        "definition": render_definition(known[(table, column)]),
                        "used_by": _sources(used[table], column),
                    }
                )
            else:
                plan["drop"].append({"table": table, "column": column})
        for column in sorted(declared_here - real_here):
            plan["missing"].append({"table": table, "column": column})
        for column in sorted(used_here - declared_here - real_here):
            plan["code_only"].append(
                {"table": table, "column": column, "used_by": _sources(used[table], column)}
            )

    plan["undeclared_tables"] = [
        {"table": table} for table in sorted(present - set(declared))
    ]
    return plan


def _undone(plan: Dict[str, List[Dict[str, Any]]], width: int = 88) -> List[str]:
    """Ce que le rattrapage laisse de côté, nommé précisément, en commentaires."""
    bullets: List[str] = []
    if plan["undeclared_tables"]:
        tables = ", ".join(entry["table"] for entry in plan["undeclared_tables"])
        bullets.append(
            f"une table en base, jamais déclarée : {tables} — une table entière ne se "
            "devine pas (la migration 011 a été écrite à la main) ;"
        )
    if plan["manual"]:
        columns = ", ".join(
            f"{entry['table']}.{entry['column']} ({entry['reason']})" for entry in plan["manual"]
        )
        bullets.append(f"une colonne dont le DDL ne se recopie pas : {columns} ;")
    if plan["code_only"]:
        columns = ", ".join(
            f"{entry['table']}.{entry['column']} ({', '.join(entry['used_by']) or 'code'})"
            for entry in plan["code_only"]
        )
        bullets.append(
            f"une colonne que le code utilise et que rien ne déclare, absente de la base "
            f"comme des migrations : {columns} — le type ne se déduit pas d'un appel ;"
        )
    if plan["missing"]:
        where = ", ".join(
            entry["table"] + (f".{entry['column']}" if entry["column"] else "")
            for entry in plan["missing"]
        )
        bullets.append(
            f"une colonne (ou une table) déclarée par les migrations et absente de la base : "
            f"{where} — ce n'est pas un rattrapage, c'est une migration non appliquée "
            "(`python scripts/apply_migrations.py`) ;"
        )
    if plan.get("objects"):
        summary = ", ".join(
            f"{count} {kind}" for kind, count in sorted(counts(plan["objects"]).items())
        )
        bullets.append(
            f"des objets que la comparaison voit et que ce fichier ne répare pas : {summary} "
            "— un index se recrée (`create index if not exists`), une contrainte s'ajoute "
            "(`alter table … add constraint`), la RLS s'active "
            "(`alter table … enable row level security`), un droit accordé se révoque "
            "(`revoke … from …`) ;"
        )
    return [
        line
        for bullet in bullets
        for line in textwrap.wrap(
            bullet, width=width, initial_indent="--   * ", subsequent_indent="--     "
        )
    ]


def repair_sql(
    plan: Dict[str, List[Dict[str, Any]]], target: str, when: Optional[str] = None
) -> str:
    """Le fichier de migration de rattrapage, en texte.

    Il est **écrit**, pas appliqué : l'outil ne modifie pas la base. Le fichier se
    relit, puis s'enregistre dans `database/migrations/` et s'applique par
    `scripts/apply_migrations.py`, comme n'importe quelle migration du dépôt.
    """
    generated = when or datetime.now(timezone.utc).isoformat()
    if not plan["declare"] and not plan["drop"]:
        # Le titre ne ment pas : « rien à rattraper » se dit quand il n'y a rien du
        # tout, sinon la base a gagné des objets que ce fichier ne touche pas.
        title = "Rien à rattraper" if not plan.get("objects") else "Rien à déclarer ni à retirer"
        lines = [
            f"-- {title} — écrit par scripts/check_schema_drift.py.",
            f"-- Cible : {target}",
            f"-- Généré le : {generated}",
            "--",
            "-- Aucune colonne de la base n'est en trop : la base et les migrations",
            "-- déclarent les mêmes colonnes. Les autres écarts ne se réparent pas ici :",
            "-- une table absente ou une colonne déclarée sans exister se réparent en",
            "-- appliquant les migrations (`python scripts/apply_migrations.py`).",
        ]
        done = _undone(plan)
        if done:
            lines += ["--", "-- Ce qui reste écarté, et qui se décide à la main :"] + done
        return "\n".join(lines + [""])

    lines = [
        "-- Migration de rattrapage — écrite par scripts/check_schema_drift.py.",
        f"-- Cible : {target}",
        f"-- Généré le : {generated}",
        "--",
        "-- La base visée porte des colonnes qu'aucune migration ne déclare. Ce fichier",
        "-- répare les deux sens séparément :",
        "--",
        "--   1. une colonne que le **code utilise** est **déclarée** — sans quoi",
        "--      reconstruire la base depuis les migrations n'en redonnerait pas une",
        "--      pareille. C'est ce que la migration 011 a réparé pour trois tables qui",
        "--      n'existaient que dans la base ;",
        "--   2. une colonne que **rien n'utilise** est **retirée** : une colonne sans",
        "--      lecteur n'a pas de raison de rester. Le retrait **détruit ses données** ;",
        "--      relis la section 2 avant d'appliquer.",
        "--",
        "-- Ce que ce fichier ne fait pas, et qui reste à décider :",
    ]
    done = _undone(plan)
    lines += done or [
        "--   * rien : les deux sections ci-dessous suffisent à rejoindre les migrations."
    ]
    lines += [
        "--",
        "-- Une colonne déclarée ici arrive **sans** son index, ses contraintes et ses",
        "-- déclencheurs : la dérive comparée est celle des colonnes (voir README,",
        "-- « Comparer la base aux migrations »).",
        "",
        "-- 1. Déclarer les colonnes dont le code se sert.",
    ]
    if _not_null_without_default(plan):
        # Le piège que le dépôt a payé une fois : sur une table qui a déjà des
        # lignes, Postgres refuse `add column … not null` sans défaut.
        lines += [
            "--    Attention : un `not null` **sans défaut** est refusé par Postgres sur une",
            "--    table qui contient déjà des lignes (voir README, « Une base **déjà en",
            "--    service** »). La base visée les a peut-être : à relire avant d'appliquer.",
        ]
    lines += [
        ADD_COLUMN_SQL.format(
            table=entry["table"], column=entry["column"], definition=entry["definition"]
        )
        for entry in plan["declare"]
    ] or ["-- (aucune)"]
    lines += [
        "",
        "-- 2. Retirer les colonnes que rien n'utilise. **Ce retrait détruit les",
        "--    données de ces colonnes** : si l'une d'elles sert quelque part hors du",
        "--    code (un tableau de bord, une requête écrite à la main), déclare-la dans",
        "--    la section 1 à la place.",
    ]
    lines += [
        DROP_COLUMN_SQL.format(table=entry["table"], column=entry["column"])
        for entry in plan["drop"]
    ] or ["-- (aucune)"]
    if plan["manual"]:
        lines += [
            "",
            "-- 3. Colonnes en base dont le DDL ne se recopie pas : **nommées, jamais",
            "--    devinées**. Le type ci-dessous est un point de départ, pas une réponse.",
        ]
        for entry in plan["manual"]:
            definition = render_definition(
                {"type": "<type>", "not_null": False, "default": None}
            )
            lines.append(f"-- {entry['table']}.{entry['column']} — {entry['reason']}")
            lines.append(
                "-- "
                + ADD_COLUMN_SQL.format(
                    table=entry["table"], column=entry["column"], definition=definition
                )
            )
    lines += [
        "",
        "-- Après application : `python scripts/check_schema_drift.py` doit rendre",
        "-- « Aucun écart ».",
        "",
    ]
    return "\n".join(lines)


def _not_null_without_default(plan: Dict[str, List[Dict[str, Any]]]) -> bool:
    """Une des déclarations pose-t-elle un `not null` sans défaut ?"""
    return any(
        "not null" in entry["definition"] and " default " not in entry["definition"]
        for entry in plan["declare"]
    )


def next_migration_name(directory: Optional[Path] = None) -> str:
    """Le nom libre suivant de `database/migrations/` (`012_rattrapage.sql`).

    Le numéro ne se devine pas quand on écrit un fichier à la main : le nommer
    évite un doublon que `apply_migrations` appliquerait dans un ordre arbitraire.
    """
    found = directory if directory is not None else apply_migrations.MIGRATIONS_DIR
    numbers = [
        int(path.name[:3]) for path in found.glob("[0-9][0-9][0-9]_*.sql") if path.name[:3].isdigit()
    ]
    return f"{max(numbers, default=0) + 1:03d}_rattrapage.sql"


def repair_hint(plan: Dict[str, List[Dict[str, Any]]], suggestion: str) -> str:
    """Combien il y a à rattraper, et où écrire le fichier. `""` si rien."""
    parts = []
    if plan["declare"]:
        parts.append(f"{len(plan['declare'])} colonne(s) à déclarer")
    if plan["drop"]:
        parts.append(f"{len(plan['drop'])} à retirer (un retrait détruit des données)")
    if plan["manual"]:
        parts.append(f"{len(plan['manual'])} à écrire à la main")
    if not parts:
        return ""
    return (
        f"Rattrapage : {', '.join(parts)}.\n"
        f"  → python scripts/check_schema_drift.py --repair-sql > "
        f"database/migrations/{suggestion}"
    )


def audit(
    connection: Any, files: Optional[Sequence[Path]] = None, with_plan: bool = False
) -> Tuple[List[Dict[str, str]], Dict[str, Any], Optional[Dict[str, List[Dict[str, Any]]]]]:
    """Les écarts, leur contexte, et — si on le demande — le plan de rattrapage.

    Une **seule** lecture de la base pour les trois : bâtir le plan en relisant le
    schéma referait le même travail, et deux lectures d'une base qui bouge (une
    migration qui passe à côté) ne décrivent pas le même schéma. Le DDL des
    colonnes (`pg_catalog`) n'est lu que quand le plan est demandé — c'est la
    seule raison d'avoir plus de deux requêtes.

    Les colonnes **utilisées par le code** ne sont cherchées que pour les tables
    que les migrations déclarent : c'est le périmètre de la comparaison, et
    élargir la lecture des sources à tout le dépôt n'apprendrait rien sur une
    table dont aucune migration ne parle (le `table en base, jamais déclarée` dit
    déjà ce qu'il faut).
    """
    paths = apply_migrations.migration_files() if files is None else list(files)
    declared = declared_schema(paths)
    declared_objects = sql_columns.declared_objects(paths)
    real_tables, real_columns = read_schema(connection)
    objects = read_objects(connection)
    used = sql_columns.columns_used_by_code(tuple(declared))
    # Les écarts sur les objets sont gardés à part : le plan de rattrapage les
    # porte, mais c'est `compare()` qui décide de ce qui se répare dans un fichier.
    expectations = privilege_expectations(declared_objects, declared)
    over_objects = compare_objects(
        declared_objects,
        objects,
        declared,
        real_tables,
        expectations,
    )
    findings = compare(declared, real_tables, real_columns, used) + over_objects
    context = {
        "schema": SCHEMA,
        "migrations": len(paths),
        "declared_tables": len(declared),
        "declared_columns": sum(len(columns) for columns in declared.values()),
        "real_tables": len(real_tables),
        "real_columns": sum(len(columns) for columns in real_columns.values()),
        "declared_objects": {
            "indexes": len(declared_objects["indexes"]),
            "constraints": sum(len(shapes) for shapes in declared_objects["constraints"].values()),
            "triggers": len(declared_objects["triggers"]),
            "row_security": len(declared_objects["row_security"]),
            "policies": len(declared_objects["policies"]),
        },
        "real_objects": {
            "indexes": len(objects["indexes"]),
            "constraints": sum(len(shapes) for shapes in objects["constraints"].values()),
            "triggers": len(objects["triggers"]),
            "row_security": sum(
                1 for state in objects["row_security"].values() if state["enabled"]
            ),
            "policies": len(objects["policies"]),
        },
        # Les droits ne sont pas des objets qu'on compte : ce qui se dit, c'est
        # **qui** les migrations en privent, et combien la base en a rendu.
        "declared_privileges": {
            "roles": expectations["roles"],
            "tables": len(expectations["tables"]),
            "functions": len(expectations["functions"]),
        },
        "real_privileges": {
            "grants": len(
                granted_privileges(objects, expectations, set(declared) & real_tables)
            )
        },
    }
    plan = None
    if with_plan:
        plan = repair_plan(
            declared,
            real_tables,
            real_columns,
            used,
            read_definitions(connection),
            objects=over_objects,
        )
    return findings, context, plan


def findings_for(
    connection: Any, files: Optional[Sequence[Path]] = None
) -> Tuple[List[Dict[str, str]], Dict[str, Any]]:
    """Les écarts et leur contexte, sans le plan : la lecture d'un simple verdict."""
    findings, context, _ = audit(connection, files)
    return findings, context


def counts(findings: Iterable[Dict[str, str]]) -> Dict[str, int]:
    """Combien d'écarts de chaque sorte — c'est ce qu'on regarde en premier."""
    counted: Dict[str, int] = {}
    for finding in findings:
        counted[finding["kind"]] = counted.get(finding["kind"], 0) + 1
    return counted


def _counted(count: int, singular: str, plural: str) -> str:
    """« 0 droit », « 1 fonction », « 3 fonctions » : un rapport s'accorde.

    En français, zéro prend le singulier — et « 0 droits accordés » serait le
    genre de détail qui fait douter du reste du rapport.
    """
    return f"{count} {singular if count < 2 else plural}"


def render_objects(counts_by_family: Dict[str, int]) -> str:
    """« 17 index, 18 contraintes, 11 déclencheurs, 14 tables en RLS, 0 policy ».

    Dit ce qui a été **réellement comparé** : un rapport qui ne le dit pas laisse
    croire que tout a été regardé, et c'est ainsi qu'on se repose sur un outil qui
    ne regarde rien.
    """
    labels = (
        ("indexes", "index", "index"),
        ("constraints", "contrainte", "contraintes"),
        ("triggers", "déclencheur", "déclencheurs"),
        ("row_security", "table en RLS", "tables en RLS"),
        ("policies", "policy", "policies"),
    )
    return ", ".join(
        _counted(counts_by_family.get(key, 0), singular, plural)
        for key, singular, plural in labels
    )


def render(findings: List[Dict[str, str]], context: Dict[str, Any], target: str) -> str:
    """Le rapport, en clair : une ligne par écart, avec le fichier en cause."""
    lines = [
        f"Dérive de schéma — {datetime.now(timezone.utc).isoformat()}",
        f"Cible : {target}",
        f"Schéma comparé : {SCHEMA} — {context['declared_tables']} tables et "
        f"{context['declared_columns']} colonnes déclarées par {context['migrations']} migrations, "
        f"{context['real_tables']} tables et {context['real_columns']} colonnes en base",
    ]
    if "declared_objects" in context and "real_objects" in context:
        lines.append(
            "Objets — déclarés : "
            f"{render_objects(context['declared_objects'])} ; en base : "
            f"{render_objects(context['real_objects'])}"
        )
    if "declared_privileges" in context:
        privileges = context["declared_privileges"]
        roles = ", ".join(privileges["roles"]) or "personne"
        lines.append(
            f"Droits — les migrations révoquent pour {roles} sur "
            f"{_counted(privileges['tables'], 'table', 'tables')} et "
            f"{_counted(privileges['functions'], 'fonction', 'fonctions')} ; la base en "
            f"accorde {_counted(context['real_privileges']['grants'], 'droit', 'droits')} "
            "à ces rôles"
        )
    lines.append("")
    if not findings:
        lines.append(f"{OK} Aucun écart : la base porte ce que les migrations déclarent.")
        return "\n".join(lines)
    for finding in findings:
        where = finding["table"] + (f".{finding['object']}" if finding["object"] else "")
        lines.append(f"  {FAIL} {finding['kind']} — {where} : {finding['detail']}")
    lines.append("")
    summary = ", ".join(f"{count} {kind}" for kind, count in sorted(counts(findings).items()))
    lines.append(f"{FAIL} {len(findings)} écart(s) : {summary}.")
    if counts(findings).get(UNDECLARED_COLUMN):
        lines.append(
            "  `--repair-sql` écrit la migration de rattrapage : elle déclare les colonnes "
            "dont le code se sert et retire les autres."
        )
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    console.make_streams_utf8()
    parser = argparse.ArgumentParser(
        description=(
            "Compare les colonnes déclarées par database/migrations/*.sql avec celles de la base "
            "(lecture seule)."
        ),
        epilog=(
            "Lecture seule : deux `select` sur information_schema, plus un sur pg_catalog quand "
            "il faut écrire le DDL du rattrapage. L'outil peut donc viser une production sans la "
            "modifier."
        ),
    )
    parser.add_argument(
        "--database-url",
        help=f"URL PostgreSQL cible (sinon la variable {apply_migrations.ENV_VAR})",
    )
    # Les deux écrivent sur la sortie standard : les laisser cohabiter
    # produirait un fichier de migration avec du JSON dedans. argparse refuse la
    # combinaison et sort en 2, comme tout mauvais usage.
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="sortie exploitable par un script")
    output.add_argument(
        "--repair-sql",
        action="store_true",
        help=(
            "écrit sur la sortie standard la migration de rattrapage (le rapport passe sur la "
            "sortie d'erreur) ; l'outil ne l'applique pas"
        ),
    )
    args = parser.parse_args(argv)

    try:
        url = apply_migrations.database_url(args.database_url)
    except RuntimeError as exc:
        print(f"{exc}", file=sys.stderr)
        print(
            f'  exemple : {apply_migrations.ENV_VAR}="postgresql://user:motdepasse@'
            'db.<projet>.supabase.co:5432/postgres"',
            file=sys.stderr,
        )
        return 2

    target = apply_migrations.redacted(url)
    with_plan = args.repair_sql or args.json
    try:
        # Une connexion impossible est un **échec**, pas un silence : si une URL
        # est donnée, c'est que quelqu'un attend une réponse sur cette base.
        with apply_migrations.connect(url, application_name=APPLICATION_NAME) as connection:
            findings, context, plan = audit(connection, with_plan=with_plan)
    except Exception as exc:
        print(f"{FAIL} {target} : {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    report = {
        "ok": not findings,
        "target": target,
        "findings": findings,
        "counts": counts(findings),
        **context,
    }
    if plan is not None:
        report["repair"] = plan
    if args.repair_sql:
        # Le SQL sur la sortie standard (`> 012_rattrapage.sql` doit donner un
        # fichier propre), le verdict sur la sortie d'erreur : celui qui
        # redirige continue de voir ce qu'il vient d'écrire.
        print(repair_sql(plan or {}, target))
        print(render(findings, context, target), file=sys.stderr)
        hint = repair_hint(plan or {}, next_migration_name())
        if hint:
            print(hint, file=sys.stderr)
    elif args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(findings, context, target))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
