"""Contrat de `scripts/check_schema_drift.py`.

Ce qui est testé ici ne peut pas dépendre d'une base réelle : celle du projet
n'est pas joignable depuis l'environnement de test (pas de `MIGRATION_DATABASE_URL`,
pas de PostgreSQL). On vérifie donc **ce qui décide** du verdict :

* la comparaison elle-même (`compare()`), qui est une fonction pure et se teste
  sur des schémas écrits à la main — y compris ceux qu'aucune base ne porte ;
* la lecture de la base, contre une `information_schema` simulée qui **refuse
  toute requête qui n'est pas un `select`** : c'est le contrat « lecture seule »
  de l'outil, vérifié sur les requêtes au lieu d'être promis dans une docstring ;
* le fait que la liste « déclaré » vient bien de `tests/sql_columns.py` et de
  `scripts/apply_migrations.py`, et non d'une deuxième extraction — deux lectures
  du même SQL finiraient par décrire deux schémas, et l'outil validerait une base
  que les contrats de migration refusent.

* le rattrapage (`repair_plan()`, `repair_sql()`) : quel écart se répare dans quel
  sens, et un **aller-retour** qui relit le fichier produit par l'extracteur des
  contrats de migration pour vérifier qu'appliqué, il fait disparaître la dérive.

L'exécution contre une vraie base existe, mais elle est **ignorée** sans
identifiants : `tests/test_migrations_apply_to_postgres.py`.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import tempfile
import unittest
from unittest import mock

from scripts import apply_migrations, check_schema_drift
from tests import sql_columns

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"

#: Noms de colonnes volontairement hors schéma : ils ne doivent pas être
#: confondus avec une vraie colonne si l'extraction des sources les croise.
INVENTEE = "colonne_inventee"


def plat(output: str) -> str:
    """Un fichier de migration remis à plat, commentaires compris.

    Le SQL produit est replié à 88 colonnes pour être lisible : une phrase
    entière tombe donc en travers d'un retour à la ligne et d'un `--`. Un
    `assertIn` sur une phrase doit regarder le texte, pas la mise en page.
    """
    return re.sub(r"\s+", " ", output.replace("--", " "))


#: Les mots que les migrations emploient → les lettres de Postgres. La doublure
#: parle la langue du catalogue, pas celle du dépôt : sans cette traduction, elle
#: testerait une comparaison qui n'a jamais lieu contre une vraie base.
_KINDS_AS_LETTERS = {"primary key": "p", "unique": "u", "foreign key": "f", "check": "c"}
_ACTIONS_AS_LETTERS = {
    "no action": "a",
    "restrict": "r",
    "cascade": "c",
    "set null": "n",
    "set default": "d",
}


class _FakeCursor:
    """Un curseur qui répond aux requêtes de l'outil, et refuse le reste."""

    def __init__(self, database: "_FakeDatabase") -> None:
        self.database = database
        self.result: list = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exception) -> bool:
        return False

    def execute(self, statement: str, parameters=None) -> None:
        self.database.statements.append((statement, parameters))
        self.result = []
        if not statement.lstrip().lower().startswith("select"):
            # Le point de l'outil : il peut viser une production. Une écriture
            # ici serait un incident, pas un test rouge.
            raise AssertionError(f"requête refusée (lecture seule) : {statement}")
        objects = self.database.objects
        if "information_schema.tables" in statement:
            self.result = [(table,) for table in sorted(self.database.tables)]
        elif "information_schema.columns" in statement:
            self.result = [
                (table, column)
                for table, columns in sorted(self.database.columns.items())
                for column in sorted(columns)
            ]
        elif "a.attidentity" in statement:
            # La requête de DDL des colonnes, reconnue à **son** vocabulaire :
            # `pg_constraint` joint aussi `pg_attribute`, et router sur ce nom-là
            # ferait répondre des colonnes à une question sur les contraintes.
            # Une colonne sans DDL connu ne rend **aucune** ligne, exactement
            # comme une colonne qui n'existe pas : c'est ce que le rattrapage
            # doit savoir traiter (il la nomme au lieu de l'inventer).
            self.result = [
                (
                    table,
                    column,
                    spec.get("type", "text"),
                    spec.get("not_null", False),
                    spec.get("default"),
                    spec.get("identity", ""),
                    spec.get("generated", ""),
                    spec.get("type_schema", "public"),
                )
                for (table, column), spec in sorted(self.database.definitions.items())
                if column in self.database.columns.get(table, set())
            ]
        elif "pg_catalog.pg_index" in statement:
            self.result = [
                (
                    name,
                    spec["table"],
                    spec.get("unique", False),
                    spec.get("definition", f"CREATE INDEX {name} ON public.{spec['table']}"),
                )
                for name, spec in sorted(objects["indexes"].items())
            ]
        elif "pg_catalog.pg_constraint" in statement:
            self.result = [
                (
                    table,
                    shape.get("name", f"{table}_contrainte"),
                    _KINDS_AS_LETTERS.get(shape["kind"], shape["kind"]),
                    shape.get("validated", True),
                    _ACTIONS_AS_LETTERS.get(shape.get("on_delete"), "a"),
                    _ACTIONS_AS_LETTERS.get(shape.get("on_update"), "a"),
                    shape.get("references"),
                    shape.get("definition", shape["kind"]),
                    list(shape["columns"]),
                    list(shape.get("reference_columns") or []),
                )
                for table, shapes in sorted(objects["constraints"].items())
                for shape in shapes
            ]
        elif "pg_catalog.pg_trigger" in statement:
            self.result = [
                (
                    name,
                    spec["table"],
                    spec["function"],
                    spec.get("definition", f"CREATE TRIGGER {name}"),
                )
                for name, spec in sorted(objects["triggers"].items())
            ]
        elif "pg_catalog.pg_policies" in statement:
            self.result = [
                (name, spec["table"], spec.get("command", "ALL"), list(spec.get("roles") or []))
                for name, spec in sorted(objects["policies"].items())
            ]
        elif "pg_catalog.pg_proc" in statement:
            self.result = [
                (entry["function"], entry["role"], entry["privilege"])
                for entry in objects["function_privileges"]
            ]
        elif "aclexplode(c.relacl)" in statement:
            self.result = [
                (entry["table"], entry["role"], entry["privilege"])
                for entry in objects["table_privileges"]
            ]
        elif "pg_catalog.pg_class" in statement:
            # En **dernier** : cinq des requêtes ci-dessus joignent aussi
            # `pg_class`, et ce qui les distingue est ce qu'elles demandent en
            # plus. Cette branche ne voit donc que la RLS.
            self.result = [
                (
                    table,
                    objects["row_security"].get(table, {}).get("enabled", False),
                    objects["row_security"].get(table, {}).get("forced", False),
                )
                for table in sorted(self.database.tables)
            ]
        else:
            raise AssertionError(f"requête inattendue : {statement}")

    def fetchall(self) -> list:
        return self.result


class _FakeDatabase:
    """Le catalogue simulé : tables, colonnes, et tout ce qui n'en est pas une.

    `definitions` porte les DDL (`pg_catalog`) des colonnes qu'on veut voir
    décrites, indexés par `(table, colonne)`. `objects` porte le reste, dans la
    forme que rend `check_schema_drift.read_objects()`.
    """

    def __init__(self, tables=(), columns=None, definitions=None, objects=None) -> None:
        self.tables = set(tables)
        self.columns = {table: set(cols) for table, cols in (columns or {}).items()}
        self.definitions = dict(definitions or {})
        self.objects = empty_objects()
        for family, value in (objects or {}).items():
            self.objects[family] = value
        self.statements: list = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def __enter__(self) -> "_FakeDatabase":
        return self

    def __exit__(self, *exception) -> bool:
        return False


def empty_objects() -> dict:
    """Un catalogue sans aucun objet : ni index, ni contrainte, ni droit."""
    return {
        "indexes": {},
        "constraints": {},
        "triggers": {},
        "row_security": {},
        "policies": {},
        "table_privileges": [],
        "function_privileges": [],
    }


def ideal_catalog(declared_objects) -> dict:
    """Le catalogue **idéal** : ce que la base porte quand tout est appliqué.

    Construit à partir de ce que les migrations déclarent. C'est ce qui permet
    aux tests de dire « une base juste » sans écrire à la main dix-sept index : et
    comme il est recalculé à chaque appel, un test peut le dégrader sans rien
    casser pour les autres.
    """
    objects = empty_objects()
    for name, spec in declared_objects["indexes"].items():
        objects["indexes"][name] = {
            "table": spec["table"],
            "unique": spec["unique"],
            "columns": list(spec["columns"]),
            "definition": (
                f"CREATE INDEX {name} ON public.{spec['table']} USING btree "
                f"({', '.join(spec['columns'])})"
            ),
        }
    for table, shapes in declared_objects["constraints"].items():
        objects["constraints"][table] = [
            {
                "name": shape["constraint"] or f"{table}_{shape['kind'].replace(' ', '_')}",
                "kind": shape["kind"],
                "columns": list(shape["columns"]),
                "references": shape["references"],
                "reference_columns": list(shape["reference_columns"]),
                "on_delete": shape["on_delete"],
                "on_update": shape["on_update"],
                "validated": shape["validated"],
                "definition": shape["kind"],
            }
            for shape in shapes
        ]
    for name, spec in declared_objects["triggers"].items():
        objects["triggers"][name] = {
            "table": spec["table"],
            "function": spec["function"],
            "definition": f"CREATE TRIGGER {name} ON public.{spec['table']}",
        }
    objects["row_security"] = {
        table: {"enabled": True, "forced": False}
        for table in declared_objects["row_security"]
    }
    for name, spec in declared_objects["policies"].items():
        objects["policies"][name] = {"table": spec["table"], "command": "ALL", "roles": []}
    return objects


def definition(type_name="text", *, type_schema="public", not_null=False, default=None,
               identity="", generated=""):
    """Le DDL d'une colonne, tel que `pg_catalog` le rend."""
    return {
        "type": type_name,
        "type_schema": type_schema,
        "not_null": not_null,
        "default": default,
        "identity": identity,
        "generated": generated,
    }


def database_matching(
    declared,
    *,
    without_tables=(),
    without_columns=(),
    extra=None,
    definitions=None,
    objects=None,
):
    """La base idéale : exactement ce que les migrations déclarent, moins/plus.

    `extra` est un dictionnaire table → colonnes **en plus** en base, ce qui est
    la façon dont une colonne ajoutée à la main se présente. Les colonnes en trop
    reçoivent un `text` par défaut : sans DDL lisible, le rattrapage les nomme au
    lieu de les écrire, et un test qui veut un plan doit pouvoir dire lequel.

    Sans `objects`, le catalogue ne porte **aucun** objet : c'est la vérité de
    cette doublure-là, et les tests qui visent une base juste passent le
    catalogue idéal (`_SnapshotTest.database()`).
    """
    tables = {table for table in declared if table not in set(without_tables)}
    columns = {table: set(cols) for table, cols in declared.items() if table in tables}
    for table in without_columns:
        columns[table] = columns[table] - {without_columns[table]}
    known = dict(definitions or {})
    for table, cols in (extra or {}).items():
        tables.add(table)
        columns.setdefault(table, set()).update(cols)
        for column in cols:
            known.setdefault((table, column), definition())
    return _FakeDatabase(tables=tables, columns=columns, definitions=known, objects=objects)


class _SnapshotTest(unittest.TestCase):
    """Base commune : ce que les migrations du dépôt déclarent, objets compris."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.files = apply_migrations.migration_files()
        cls.declared = check_schema_drift.declared_schema(cls.files)
        cls.declared_objects = sql_columns.declared_objects(cls.files)

    def catalog(self, **overrides) -> dict:
        """Le catalogue réel idéal, dégradable par mot-clé (`indexes=`, `row_security=`)."""
        objects = ideal_catalog(self.declared_objects)
        objects.update(overrides)
        return objects

    def database(self, **kwargs):
        """Une base qui porte le schéma **et les objets** déclarés, moins/plus."""
        kwargs.setdefault("objects", self.catalog())
        return database_matching(self.declared, **kwargs)

    def clean_database(self):
        """Une base qui porte exactement le schéma déclaré, et tous ses objets."""
        return self.database()


class DeclaredSchemaTest(_SnapshotTest):
    """Ce que l'outil entend par « déclaré » : la liste des migrations."""

    def test_the_application_tables_are_declared(self):
        for table in (
            "users",
            "insights",
            "pending_signals",
            "economic_events",
            "macro_bias_logs",
            "trade_post_mortems",
            "adaptive_model_weights",
            "knowledge_chunks",
        ):
            with self.subTest(table=table):
                self.assertIn(table, self.declared)
                self.assertTrue(self.declared[table], f"{table} sans aucune colonne déclarée")

    def test_the_provenance_names_the_file_that_declares_the_column(self):
        """Sans provenance, « le schéma a dérivé » n'est pas actionnable."""
        self.assertEqual(
            self.declared["economic_events"]["raw_data"], ["005_forex_factory.sql"]
        )
        self.assertEqual(
            self.declared["adaptive_model_weights"]["ta_weight"], ["006_adaptive_learning.sql"]
        )
        self.assertEqual(
            self.declared["knowledge_chunks"]["created_at"], ["008_telegram_media.sql"]
        )

    def test_a_file_is_named_once_even_when_it_declares_twice(self):
        """Le même fichier écrit souvent `create table` **puis** `add column`."""
        for table, columns in self.declared.items():
            for column, files in columns.items():
                with self.subTest(table=table, column=column):
                    self.assertEqual(len(files), len(set(files)), f"{table}.{column} : {files}")

    def test_a_column_removed_by_a_migration_is_not_declared(self):
        """`006` retire `min_confidence_threshold`, créée par une version antérieure.

        L'oublier ferait réclamer une colonne que la base n'a pas — l'outil
        crierait sur une base pourtant juste, c'est-à-dire sur toutes.
        """
        self.assertNotIn("min_confidence_threshold", self.declared["adaptive_model_weights"])

    def test_a_drop_after_the_declaration_wins(self):
        """L'état final est celui auquel la base doit ressembler."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "999_probe.sql"
            path.write_text(
                "create table if not exists sonde (\n"
                "    gardee text,\n"
                "    retiree text                                   -- commentaire\n"
                ");\n"
                "alter table if exists sonde drop column if exists retiree;\n",
                encoding="utf-8",
            )
            declared = sql_columns.declared_schema_by_file([path])
        self.assertEqual(set(declared["sonde"]), {"gardee"})

    def test_an_alter_without_column_change_declares_no_table(self):
        """`alter table … enable row level security` ne crée pas la table."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "999_probe.sql"
            path.write_text(
                "alter table if exists fantome enable row level security;\n"
                "alter table if exists fantome revoke all on fantome from anon;\n",
                encoding="utf-8",
            )
            declared = sql_columns.declared_schema_by_file([path])
        self.assertEqual(declared, {}, "une table que personne ne crée est réclamée pour rien")


class ColumnsUsedByCodeTest(unittest.TestCase):
    """Ce que l'extraction voit d'un **écrivain délégué**, et ce qu'elle refuse.

    Une table ne s'écrit pas toujours depuis l'appel : `upsert_economic_events()`
    construit sa requête dans `database/supabase_client.py`, et la charge n'y est
    qu'un paramètre. Sans ce suivi, les colonnes écrites par ses appelants (la
    sonde de configuration, le scraper) n'étaient reliées à **aucune** écriture :
    le DDL déclarait `raw_data`, `title`, `country`… que rien ne reliait à une
    écriture, et une faute de frappe dans la charge d'un appelant serait passée
    inaperçue (`upsert_economic_events` avale ses erreurs et rend 0).

    Les sources sont écrites ici, à la main : ce sont les décisions de l'extraction
    qui sont éprouvées, pas la forme du dépôt.
    """

    WRITER = (
        "def save_events(events):\n"
        '    supabase.table("events").upsert(events, on_conflict="id").execute()\n'
    )

    def extract(self, *sources: str, tables=("events",)):
        """Les colonnes vues, par table — sur des sources de banc d'essai."""
        files = [
            (sql_columns.REPO_ROOT / f"probe_{index}.py", text)
            for index, text in enumerate(sources)
        ]
        used = sql_columns.columns_of_sources(files, tables)
        return {
            table: set().union(*by_file.values()) if by_file else set()
            for table, by_file in used.items()
        }

    def test_a_delegated_writer_returns_the_payload_to_its_table(self):
        seen = self.extract(
            self.WRITER,
            'def probe():\n    save_events([{"raw_data": item, "title": title}])\n',
        )
        self.assertEqual(seen["events"], {"raw_data", "title"})

    def test_a_document_argument_is_not_a_column(self):
        """`signal` est une **colonne** dont la valeur est un document JSON."""
        writer = (
            "def save_signal(user_id, signal):\n"
            '    supabase.table("events").insert({\n'
            '        "user_id": user_id,\n'
            '        "signal": signal,\n'
            "    }).execute()\n"
        )
        seen = self.extract(
            writer, 'def probe():\n    save_signal("u", {"asset": "BTC"})\n'
        )
        self.assertEqual(seen["events"], {"signal", "user_id"})

    def test_a_nested_dictionary_is_a_value_not_a_column(self):
        """`{"data": {"published": …}}` ne fait pas de `published` une colonne."""
        seen = self.extract(
            self.WRITER,
            'def probe():\n    save_events([{"data": {"published": "now"}, "type": t}])\n',
        )
        self.assertEqual(seen["events"], {"data", "type"})

    def test_a_keyword_argument_binds_itself(self):
        seen = self.extract(
            self.WRITER, 'def probe():\n    save_events(events=[{"raw_data": item}])\n'
        )
        self.assertEqual(seen["events"], {"raw_data"})

    def test_a_positional_argument_is_found_by_its_position(self):
        writer = (
            "def save_events(scope, events):\n"
            '    supabase.table("events").upsert(events).execute()\n'
        )
        right = self.extract(
            writer, 'def probe():\n    save_events("global", [{"raw_data": item}])\n'
        )
        swapped = self.extract(
            writer, 'def probe():\n    save_events([{"raw_data": item}], "global")\n'
        )
        self.assertEqual(right["events"], {"raw_data"})
        self.assertEqual(swapped["events"], set())

    def test_two_writers_of_the_same_name_are_left_alone(self):
        """Deux corps, deux tables, un seul nom : rien ne dit lequel est appelé."""
        first = 'def save(events):\n    supabase.table("events").upsert(events).execute()\n'
        second = 'def save(events):\n    supabase.table("archive").upsert(events).execute()\n'
        seen = self.extract(first, second, 'def probe():\n    save([{"raw_data": item}])\n')
        self.assertEqual(seen["events"], set())

    def test_a_method_of_the_same_name_is_not_the_writer(self):
        """`scheduler.save_events(…)` n'est pas la fonction du module."""
        seen = self.extract(
            self.WRITER,
            'def probe():\n    scheduler.save_events([{"raw_data": item}])\n',
        )
        self.assertEqual(seen["events"], set())


class CompareTest(unittest.TestCase):
    """La comparaison, seule : ni connexion, ni fichier."""

    DECLARED = {"t": {"a": ["001_t.sql"], "b": ["002_u.sql"]}}

    def test_a_database_that_matches_the_migrations_is_green(self):
        findings = check_schema_drift.compare(self.DECLARED, {"t"}, {"t": {"a", "b"}})
        self.assertEqual(findings, [])

    def test_a_missing_table_is_reported_with_the_file_that_creates_it(self):
        findings = check_schema_drift.compare(self.DECLARED, set(), {"t": {"a", "b"}})
        self.assertEqual([finding["kind"] for finding in findings], ["table absente"])
        self.assertIn("001_t.sql", findings[0]["detail"])
        self.assertIn("migration non appliquée", findings[0]["detail"])

    def test_a_missing_table_does_not_list_all_of_its_columns(self):
        """La table manque : vingt lignes de colonnes n'apprendraient rien de plus."""
        declared = {"t": {f"c{index}": ["001_t.sql"] for index in range(20)}}
        findings = check_schema_drift.compare(declared, set(), {})
        self.assertEqual(len(findings), 1)

    def test_a_missing_column_names_the_migration_that_declares_it(self):
        findings = check_schema_drift.compare(self.DECLARED, {"t"}, {"t": {"a"}})
        self.assertEqual([finding["kind"] for finding in findings], ["colonne absente"])
        self.assertEqual(findings[0]["object"], "b")
        self.assertIn("002_u.sql", findings[0]["detail"])

    def test_a_missing_column_says_when_the_code_uses_it(self):
        """La colonne qui casse l'application doit être distinguable des autres."""
        used = {"t": {"core/moteur.py": {"b"}}}
        findings = check_schema_drift.compare(self.DECLARED, {"t"}, {"t": {"a"}}, used)
        self.assertIn("core/moteur.py", findings[0]["detail"])

    def test_a_column_added_by_hand_is_reported(self):
        """Le sens inverse : la base en avance sur les migrations."""
        findings = check_schema_drift.compare(
            self.DECLARED, {"t"}, {"t": {"a", "b", "ajoutee_a_la_main"}}
        )
        self.assertEqual([finding["kind"] for finding in findings], ["colonne en base, jamais déclarée"])
        self.assertEqual(findings[0]["object"], "ajoutee_a_la_main")
        self.assertIn("migration oubliée", findings[0]["detail"])

    def test_a_table_added_by_hand_is_reported(self):
        findings = check_schema_drift.compare(self.DECLARED, {"t", "oubliee"}, {"t": {"a", "b"}})
        self.assertEqual([finding["kind"] for finding in findings], ["table en base, jamais déclarée"])
        self.assertEqual(findings[0]["table"], "oubliee")

    def test_a_column_the_code_uses_and_nobody_declares_is_reported(self):
        """Ni la base ni les migrations ne la connaissent : l'appel échouera."""
        used = {"t": {"core/moteur.py": {INVENTEE}}}
        findings = check_schema_drift.compare(self.DECLARED, {"t"}, {"t": {"a", "b"}}, used)
        self.assertEqual(
            [finding["kind"] for finding in findings],
            ["colonne utilisée par le code, déclarée nulle part"],
        )
        self.assertIn("core/moteur.py", findings[0]["detail"])

    def test_every_kind_of_drift_is_found_in_one_pass(self):
        """Un rapport qui s'arrête au premier écart ne dit pas où on en est."""
        used = {"t": {"core/moteur.py": {INVENTEE}}, "disparue": {"api/routes.py": {"x"}}}
        findings = check_schema_drift.compare(
            {"t": {"a": ["001_t.sql"]}, "disparue": {"x": ["003_v.sql"]}},
            {"t"},
            {"t": {"a", "ajoutee"}},
            used,
        )
        self.assertEqual(
            sorted(finding["kind"] for finding in findings),
            sorted(
                [
                    "table absente",
                    "colonne en base, jamais déclarée",
                    "colonne utilisée par le code, déclarée nulle part",
                ]
            ),
        )


class ReadOnlyTest(_SnapshotTest):
    """L'outil lit, il n'écrit pas — le contrat qui autorise à viser une production."""

    def test_it_reads_the_tables_and_the_columns(self):
        database = self.clean_database()
        tables, columns = check_schema_drift.read_schema(database)
        self.assertEqual(tables, set(self.declared))
        self.assertEqual(columns, {table: set(cols) for table, cols in self.declared.items()})

    def test_it_sends_two_selects_and_nothing_else(self):
        database = self.clean_database()
        check_schema_drift.read_schema(database)
        self.assertEqual(len(database.statements), 2)
        for statement, parameters in database.statements:
            with self.subTest(statement=statement):
                self.assertTrue(statement.lstrip().lower().startswith("select"))
                self.assertEqual(parameters, (check_schema_drift.SCHEMA,))

    def test_reading_the_objects_is_a_select_too(self):
        """Sept requêtes de catalogue, toutes en lecture — index, contraintes,
        déclencheurs, RLS, policies, droits des tables, droits des fonctions."""
        database = self.clean_database()
        check_schema_drift.read_objects(database)
        self.assertEqual(len(database.statements), 7)
        for statement, parameters in database.statements:
            with self.subTest(statement=statement[:40]):
                self.assertTrue(statement.lstrip().lower().startswith("select"))
                self.assertEqual(parameters, (check_schema_drift.SCHEMA,))

    def test_the_queries_are_selects_by_construction(self):
        """Le vocabulaire du fichier lui-même : une requête d'écriture n'y a pas sa place."""
        for name in (
            "TABLES_QUERY",
            "COLUMNS_QUERY",
            "DEFINITIONS_QUERY",
            "INDEXES_QUERY",
            "CONSTRAINTS_QUERY",
            "TRIGGERS_QUERY",
            "ROW_SECURITY_QUERY",
            "POLICIES_QUERY",
            "TABLE_PRIVILEGES_QUERY",
            "FUNCTION_PRIVILEGES_QUERY",
        ):
            with self.subTest(name=name):
                statement = getattr(check_schema_drift, name)
                self.assertTrue(statement.lstrip().lower().startswith("select"))
                for interdit in ("insert", "update ", "delete", "create ", "alter ", "drop "):
                    self.assertNotIn(interdit, statement.lower())

    def test_it_only_looks_at_public(self):
        """`extensions` et `storage` appartiennent à la plateforme, pas aux migrations."""
        self.assertEqual(check_schema_drift.SCHEMA, "public")

    def test_reading_the_ddl_is_a_select_too(self):
        """`pg_catalog` sert à **écrire** un rattrapage, jamais à en appliquer un."""
        database = self.clean_database()
        check_schema_drift.read_definitions(database)
        self.assertEqual(len(database.statements), 1)
        statement, parameters = database.statements[0]
        self.assertTrue(statement.lstrip().lower().startswith("select"))
        self.assertEqual(parameters, (check_schema_drift.SCHEMA,))


class AgainstTheRealMigrationsTest(_SnapshotTest):
    """Les deux moitiés mises ensemble, sur le schéma du dépôt."""

    def test_a_database_built_from_the_migrations_has_no_drift(self):
        findings, context = check_schema_drift.findings_for(self.clean_database())
        self.assertEqual(findings, [], f"écarts : {findings}")
        self.assertEqual(context["declared_tables"], len(self.declared))
        self.assertEqual(context["migrations"], len(apply_migrations.migration_files()))

    def test_the_sources_do_not_use_a_column_the_migrations_ignore(self):
        """Le pire cas n'est pas la base en retard : c'est le code en avance."""
        real = {table: set(columns) for table, columns in self.declared.items()}
        used = sql_columns.columns_used_by_code(tuple(self.declared))
        findings = check_schema_drift.compare(self.declared, set(self.declared), real, used)
        self.assertEqual(findings, [], f"le code interroge des colonnes que rien ne déclare : {findings}")

    def test_a_real_drift_is_caught_end_to_end(self):
        """Une colonne que le code utilise et que la base n'a pas."""
        database = database_matching(self.declared, without_columns={"economic_events": "raw_data"})
        findings, _ = check_schema_drift.findings_for(database)
        kinds = [finding["kind"] for finding in findings]
        self.assertIn("colonne absente", kinds)
        missing = next(finding for finding in findings if finding["kind"] == "colonne absente")
        self.assertEqual((missing["table"], missing["object"]), ("economic_events", "raw_data"))
        self.assertIn("005_forex_factory.sql", missing["detail"])

    def test_the_report_points_at_the_repair_when_a_column_is_undeclared(self):
        """Un rapport qui dit « à déclarer, ou à retirer » doit dire **comment**."""
        database = self.database(extra={"users": {"colonne_ajoutee"}})
        findings, context, plan = check_schema_drift.audit(database, with_plan=True)
        self.assertEqual(
            [finding["kind"] for finding in findings], ["colonne en base, jamais déclarée"]
        )
        self.assertIn("--repair-sql", check_schema_drift.render(findings, context, "cible"))
        self.assertEqual(plan["drop"], [{"table": "users", "column": "colonne_ajoutee"}])

    def test_without_asking_there_is_no_plan_and_no_column_ddl_read(self):
        """Le DDL des colonnes n'est lu que si on le demande : c'est le seul qui
        coûte un aller-retour de plus, et il ne sert qu'à **écrire** du SQL.
        Le catalogue des objets, lui, est lu : c'est le verdict.
        """
        database = self.clean_database()
        findings, _, plan = check_schema_drift.audit(database)
        self.assertEqual(findings, [])
        self.assertIsNone(plan)
        self.assertFalse(
            [statement for statement, _ in database.statements if "a.attidentity" in statement],
            "le DDL des colonnes a été lu sans que personne ne demande de plan",
        )


class RepairPlanTest(unittest.TestCase):
    """Quel écart se répare dans quel sens : la décision, seule.

    Le sens ne se devine pas, il se lit dans l'**usage** : une colonne que le code
    interroge doit être déclarée par une migration ; celle que personne ne lit
    n'a pas de raison de rester. Et ce qu'on ne sait pas décrire, on ne le
    propose ni à la déclaration ni au retrait.
    """

    DECLARED = {"t": {"a": ["001_t.sql"]}}

    def plan(self, *, real, used=None, definitions=None, declared=None):
        return check_schema_drift.repair_plan(
            declared or self.DECLARED, {"t"}, {"t": real}, used, definitions
        )

    def test_a_column_the_code_uses_is_declared(self):
        plan = self.plan(
            real={"a", "ajoutee"},
            used={"t": {"core/moteur.py": {"ajoutee"}}},
            definitions={("t", "ajoutee"): definition("jsonb")},
        )
        self.assertEqual(
            plan["declare"],
            [
                {
                    "table": "t",
                    "column": "ajoutee",
                    "definition": "jsonb",
                    "used_by": ["core/moteur.py"],
                }
            ],
        )
        self.assertEqual(plan["drop"], [])

    def test_a_column_nothing_uses_is_removed(self):
        plan = self.plan(real={"a", "orpheline"}, definitions={("t", "orpheline"): definition()})
        self.assertEqual(plan["drop"], [{"table": "t", "column": "orpheline"}])
        self.assertEqual(plan["declare"], [])

    def test_a_type_outside_public_keeps_its_schema(self):
        """`vector` vit dans `extensions` : un type nu ne s'applique qu'ici."""
        plan = self.plan(
            real={"a", "v"},
            used={"t": {"core/moteur.py": {"v"}}},
            definitions={("t", "v"): definition("vector(768)", type_schema="extensions")},
        )
        self.assertEqual(plan["declare"][0]["definition"], "extensions.vector(768)")

    def test_a_type_in_public_is_left_unqualified(self):
        plan = self.plan(
            real={"a", "montant"},
            used={"t": {"core/moteur.py": {"montant"}}},
            definitions={("t", "montant"): definition("numeric(6,2)")},
        )
        self.assertEqual(plan["declare"][0]["definition"], "numeric(6,2)")

    def test_a_type_already_qualified_is_not_qualified_twice(self):
        """`format_type` qualifie lui-même quand le schéma n'est pas visible."""
        plan = self.plan(
            real={"a", "v"},
            used={"t": {"core/moteur.py": {"v"}}},
            definitions={("t", "v"): definition("extensions.vector(768)", type_schema="extensions")},
        )
        self.assertEqual(plan["declare"][0]["definition"], "extensions.vector(768)")

    def test_the_nullability_and_the_default_are_copied(self):
        plan = self.plan(
            real={"a", "etat"},
            used={"t": {"core/moteur.py": {"etat"}}},
            definitions={("t", "etat"): definition("text", not_null=True, default="'neuf'::text")},
        )
        self.assertEqual(plan["declare"][0]["definition"], "text not null default 'neuf'::text")

    def test_an_identity_column_is_named_and_never_removed(self):
        """On ne retire pas ce qu'on n'a pas su décrire : une `identity` est un compteur."""
        plan = self.plan(
            real={"a", "compteur"},
            definitions={("t", "compteur"): definition("integer", identity="d")},
        )
        self.assertEqual(plan["declare"], [])
        self.assertEqual(plan["drop"], [])
        self.assertEqual(plan["manual"][0]["column"], "compteur")
        self.assertIn("identity", plan["manual"][0]["reason"])

    def test_a_computed_column_is_named_too(self):
        plan = self.plan(
            real={"a", "total"},
            definitions={("t", "total"): definition("numeric", generated="s")},
        )
        self.assertEqual(plan["drop"], [])
        self.assertIn("calculée", plan["manual"][0]["reason"])

    def test_a_default_on_a_sequence_is_not_recopied(self):
        """La séquence n'existe pas sur une base neuve : le fichier y échouerait."""
        plan = self.plan(
            real={"a", "numero"},
            used={"t": {"core/moteur.py": {"numero"}}},
            definitions={
                ("t", "numero"): definition("integer", default="nextval('t_numero_seq'::regclass)")
            },
        )
        self.assertEqual(plan["declare"], [])
        self.assertIn("nextval", plan["manual"][0]["reason"])

    def test_a_column_without_a_readable_ddl_is_named_not_invented(self):
        plan = self.plan(real={"a", "inconnue"}, used={"t": {"core/moteur.py": {"inconnue"}}})
        self.assertEqual(plan["declare"], [])
        self.assertIn("pas pu être lu", plan["manual"][0]["reason"])

    def test_a_table_the_migrations_ignore_is_listed_not_rewritten(self):
        plan = check_schema_drift.repair_plan(self.DECLARED, {"t", "oubliee"}, {"t": {"a"}}, {}, {})
        self.assertEqual(plan["undeclared_tables"], [{"table": "oubliee"}])
        self.assertEqual(plan["declare"], [])

    def test_a_declared_column_absent_from_the_base_is_left_to_the_applier(self):
        """Ce n'est pas un rattrapage, c'est une migration qui n'est pas passée."""
        plan = check_schema_drift.repair_plan(
            {"t": {"a": ["001_t.sql"], "b": ["002_u.sql"]}}, {"t"}, {"t": {"a"}}, {}, {}
        )
        self.assertEqual(plan["missing"], [{"table": "t", "column": "b"}])
        self.assertEqual(plan["declare"], [])

    def test_a_column_the_code_uses_and_nobody_declares_is_named(self):
        plan = self.plan(real={"a"}, used={"t": {"core/moteur.py": {INVENTEE}}})
        self.assertEqual(
            plan["code_only"],
            [{"table": "t", "column": INVENTEE, "used_by": ["core/moteur.py"]}],
        )

    def test_a_clean_database_has_nothing_to_repair(self):
        plan = self.plan(real={"a"})
        for section, entries in plan.items():
            with self.subTest(section=section):
                self.assertEqual(entries, [])


class RepairSqlTest(unittest.TestCase):
    """Le fichier écrit : ce qu'il contient, et ce qu'il répare **pour de vrai**.

    Le contrat qui compte n'est pas la forme du texte mais son effet : relu par
    l'extracteur des contrats de migration (celui qui décide de ce que les
    migrations déclarent), il doit faire disparaître la dérive sur laquelle il a
    été bâti — et il doit pouvoir être rejoué.
    """

    TARGET = "postgresql://postgres:***@h/ai_trade_test"
    WHEN = "2026-01-01T00:00:00+00:00"
    DECLARED = {"t": {"a": ["001_t.sql"]}}

    def plan_and_text(self, *, real, used=None, definitions=None, declared=None):
        plan = check_schema_drift.repair_plan(
            declared or self.DECLARED, {"t"}, {"t": real}, used, definitions
        )
        return plan, check_schema_drift.repair_sql(plan, self.TARGET, when=self.WHEN)

    def written(self, text: str) -> pathlib.Path:
        """Écrit le SQL dans un fichier temporaire, comme le ferait une redirection."""
        directory = pathlib.Path(tempfile.mkdtemp(prefix="rattrapage-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = directory / "012_rattrapage.sql"
        path.write_text(text, encoding="utf-8")
        return path

    def test_every_statement_carries_its_guard(self):
        """Une migration se rejoue : `unguarded_ddl()` est le juge du dépôt."""
        _, text = self.plan_and_text(
            real={"a", "utilisee", "orpheline"},
            used={"t": {"core/moteur.py": {"utilisee"}}},
            definitions={
                ("t", "utilisee"): definition("jsonb"),
                ("t", "orpheline"): definition(),
            },
        )
        self.assertEqual(sql_columns.unguarded_ddl([self.written(text)]), [])

    def test_the_declared_side_gains_the_column_and_the_removed_one_disappears(self):
        """Relu par l'extracteur du dépôt : déclarée d'un côté, retirée de l'autre."""
        _, text = self.plan_and_text(
            real={"a", "utilisee", "orpheline"},
            used={"t": {"core/moteur.py": {"utilisee"}}},
            definitions={
                ("t", "utilisee"): definition("jsonb"),
                ("t", "orpheline"): definition(),
            },
        )
        declared = sql_columns.declared_schema_by_file([self.written(text)])
        self.assertEqual(declared, {"t": {"utilisee": ["012_rattrapage.sql"]}})

    def test_the_generated_file_removes_the_drift_it_was_built_for(self):
        """L'aller-retour : le fichier produit, appliqué aux deux côtés, ne laisse rien."""
        used = {"t": {"core/moteur.py": {"utilisee"}}}
        plan, text = self.plan_and_text(
            real={"a", "utilisee", "orpheline"},
            used=used,
            definitions={
                ("t", "utilisee"): definition("jsonb", not_null=False),
                ("t", "orpheline"): definition(),
            },
        )
        declared = sql_columns.declared_schema_by_file([self.written(text)])
        repaired = {table: set(columns) for table, columns in self.DECLARED.items()}
        for table, columns in declared.items():
            repaired.setdefault(table, set()).update(columns)
        after = {"t": {"a", "utilisee", "orpheline"}}
        for entry in plan["drop"]:
            after[entry["table"]].discard(entry["column"])
        findings = check_schema_drift.compare(repaired, set(after), after, used)
        self.assertEqual(findings, [], f"le rattrapage laisse encore : {findings}")

    def test_a_column_that_cannot_be_described_stays_in_drift_and_is_named(self):
        """On ne devine pas, et on ne retire pas : l'écart est **dit**, pas masqué."""
        plan, text = self.plan_and_text(
            real={"a", "compteur"},
            definitions={("t", "compteur"): definition("integer", identity="d")},
        )
        declared = sql_columns.declared_schema_by_file([self.written(text)])
        repaired = {table: set(columns) for table, columns in self.DECLARED.items()}
        after = {"t": {"a", "compteur"}}
        findings = check_schema_drift.compare(repaired, set(after), after, {})
        self.assertEqual([finding["object"] for finding in findings], ["compteur"])
        self.assertIn("t.compteur", text)
        self.assertIn("identity", text)

    def test_a_statement_left_to_a_human_is_only_a_comment(self):
        """Une ligne commentée n'est pas du DDL : l'extracteur ne doit pas la voir."""
        _, text = self.plan_and_text(
            real={"a", "compteur"},
            definitions={("t", "compteur"): definition("integer", identity="d")},
        )
        for line in text.splitlines():
            if "add column if not exists compteur" in line:
                with self.subTest(line=line):
                    self.assertTrue(line.startswith("-- "), line)

    def test_the_removal_is_announced_where_it_can_be_read(self):
        """Un `drop column` détruit des données : le fichier le dit deux fois."""
        _, text = self.plan_and_text(
            real={"a", "orpheline"}, definitions={("t", "orpheline"): definition()}
        )
        self.assertIn("drop column if exists orpheline", text)
        self.assertIn("détruit", text)
        self.assertLess(text.index("-- 1. Déclarer"), text.index("drop column if exists orpheline"))

    def test_it_recalls_that_indexes_and_constraints_do_not_follow(self):
        """La dérive comparée est celle des colonnes : le fichier ne prétend pas plus."""
        _, text = self.plan_and_text(
            real={"a", "utilisee"},
            used={"t": {"core/moteur.py": {"utilisee"}}},
            definitions={("t", "utilisee"): definition("jsonb")},
        )
        self.assertIn("index", text)
        self.assertIn("contraintes", text)

    def test_it_names_the_cases_it_leaves_to_a_human(self):
        """Table entière, colonne inconnue du code : nommées, pas silencieuses."""
        plan = check_schema_drift.repair_plan(
            {"t": {"a": ["001_t.sql"]}, "partie": {"x": ["001_t.sql"]}},
            {"t", "oubliee"},
            {"t": {"a", "utilisee"}, "oubliee": {"z"}},
            {"t": {"core/moteur.py": {"utilisee", INVENTEE}}},
            {("t", "utilisee"): definition("jsonb")},
        )
        text = check_schema_drift.repair_sql(plan, self.TARGET, when=self.WHEN)
        self.assertIn("oubliee", text)
        self.assertIn("partie", text)
        self.assertIn(INVENTEE, text)
        self.assertIn("jamais déclarée", text)

    def test_a_not_null_without_a_default_is_flagged(self):
        """Le piège que le dépôt a déjà payé : refusé sur une table qui a des lignes."""
        _, text = self.plan_and_text(
            real={"a", "obligatoire"},
            used={"t": {"core/moteur.py": {"obligatoire"}}},
            definitions={("t", "obligatoire"): definition("text", not_null=True)},
        )
        self.assertIn("text not null", text)
        self.assertIn("déjà", text)
        self.assertIn("service", text)

    def test_a_not_null_with_a_default_has_nothing_to_flag(self):
        """Avec un défaut, la ligne passe : pas d'avertissement, pas de bruit."""
        _, text = self.plan_and_text(
            real={"a", "obligatoire"},
            used={"t": {"core/moteur.py": {"obligatoire"}}},
            definitions={("t", "obligatoire"): definition("text", not_null=True, default="'x'")},
        )
        self.assertIn("text not null default 'x'", text)
        self.assertNotIn("Attention", text)

    def test_a_base_that_matches_gets_a_file_that_says_so(self):
        """Une redirection ne doit pas produire un fichier vide et muet."""
        _, text = self.plan_and_text(real={"a"})
        self.assertIn("Rien à rattraper", text)
        self.assertNotIn("add column", text)
        self.assertNotIn("drop column", text)

    def test_the_header_names_the_target_and_the_moment(self):
        _, text = self.plan_and_text(
            real={"a", "orpheline"}, definitions={("t", "orpheline"): definition()}
        )
        self.assertIn(f"-- Cible : {self.TARGET}", text)
        self.assertIn(self.WHEN, text)


class DeclaredObjectsTest(_SnapshotTest):
    """Ce que les migrations déclarent **en dehors des colonnes**.

    Extrait par `tests/sql_columns.py`, comme les colonnes : c'est la même lecture
    qui alimente l'outil de dérive et les contrats de migration, et deux lectures
    finiraient par décrire deux bases différentes.
    """

    def probe(self, sql: str) -> dict:
        """`declared_objects` sur un fichier écrit à la main, dans un dossier jetable."""
        directory = pathlib.Path(tempfile.mkdtemp(prefix="objets-"))
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        path = directory / "999_probe.sql"
        path.write_text(sql, encoding="utf-8")
        return sql_columns.declared_objects([path])

    def test_the_indexes_are_read_with_their_table_and_key_columns(self):
        objects = self.declared_objects
        self.assertEqual(len(objects["indexes"]), 17)
        self.assertEqual(
            objects["indexes"]["idx_pending_signals_user_status"],
            {
                "table": "pending_signals",
                "unique": False,
                "columns": ["user_id", "status"],
                "file": "011_core_tables.sql",
            },
        )

    def test_a_unique_index_is_flagged(self):
        objects = self.probe(
            "create unique index if not exists idx_probe on sonde (a, b);\n"
        )
        self.assertEqual(objects["indexes"]["idx_probe"]["unique"], True)
        self.assertEqual(objects["indexes"]["idx_probe"]["columns"], ["a", "b"])

    def test_a_method_and_an_opclass_do_not_hide_the_indexed_column(self):
        """`using hnsw (embedding vector_cosine_ops)` indexe `embedding`."""
        objects = self.probe(
            "create index if not exists idx_probe on sonde\n"
            "    using hnsw (embedding extensions.vector_cosine_ops);\n"
        )
        self.assertEqual(objects["indexes"]["idx_probe"]["columns"], ["embedding"])
        self.assertEqual(objects["indexes"]["idx_probe"]["table"], "sonde")

    def test_the_triggers_name_their_table_and_their_function(self):
        objects = self.declared_objects
        self.assertEqual(len(objects["triggers"]), 11)
        self.assertEqual(
            objects["triggers"]["trg_users_updated_at"],
            {
                "table": "users",
                "function": "update_updated_at",
                "file": "011_core_tables.sql",
            },
        )

    def test_every_declared_table_gets_its_row_security(self):
        """Le « deny by default » du dépôt : 14 tables, 14 RLS."""
        self.assertEqual(set(self.declared_objects["row_security"]), set(self.declared))

    def test_no_migration_creates_a_policy(self):
        """C'est **le** contrat de sécurité du dépôt, et il est lu, pas supposé."""
        self.assertEqual(self.declared_objects["policies"], {})

    def test_the_constraints_are_read_by_shape(self):
        constraints = self.declared_objects["constraints"]
        keys = [shape for shapes in constraints.values() for shape in shapes]
        self.assertEqual(sum(1 for shape in keys if shape["kind"] == "primary key"), 14)
        self.assertEqual(sum(1 for shape in keys if shape["kind"] == "unique"), 2)
        self.assertEqual(sum(1 for shape in keys if shape["kind"] == "foreign key"), 2)

    def test_the_legacy_foreign_key_is_read_as_not_valid(self):
        """`011` la pose `not valid` : c'est ce qui la rend posable sur une base pleine."""
        shapes = self.declared_objects["constraints"]["pending_signals"]
        foreign = next(shape for shape in shapes if shape["kind"] == "foreign key")
        self.assertEqual(foreign["columns"], ["user_id"])
        self.assertEqual(foreign["references"], "users")
        self.assertEqual(foreign["reference_columns"], ["id"])
        self.assertEqual(foreign["on_delete"], "cascade")
        self.assertEqual(foreign["validated"], False)
        self.assertEqual(foreign["constraint"], "pending_signals_user_id_fkey")

    def test_an_inline_foreign_key_is_read_too(self):
        """`008` l'écrit sur la ligne de colonne, pas en contrainte de table."""
        shapes = self.declared_objects["constraints"]["knowledge_chunks"]
        foreign = next(shape for shape in shapes if shape["kind"] == "foreign key")
        self.assertEqual(foreign["columns"], ["media_id"])
        self.assertEqual(foreign["references"], "knowledge_media")
        self.assertEqual(foreign["on_delete"], "cascade")

    def test_the_revokes_are_read_with_their_roles_and_targets(self):
        revokes = self.declared_objects["revokes"]
        self.assertEqual(len(revokes), 12)
        self.assertTrue(all(revoke["roles"] == ["anon", "authenticated"] for revoke in revokes))
        self.assertIn(
            {"privilege": "execute", "target": "function match_knowledge_chunks",
             "roles": ["anon", "authenticated"], "file": "009_knowledge_vectors.sql"},
            revokes,
        )

    def test_a_comment_quoting_ddl_is_not_read_as_ddl(self):
        """Ces fichiers expliquent ce qu'ils font : `_without_comments` les protège."""
        objects = self.probe(
            "-- un index se crée avec `create index if not exists idx_fantome on x (a);`\n"
            "-- et un trigger avec `create trigger trg_fantome … execute function f();`\n"
        )
        self.assertEqual(objects["indexes"], {})
        self.assertEqual(objects["triggers"], {})

    def test_the_shortcuts_of_the_aggregate_match_the_unit_functions(self):
        """Un agrégat qui divergerait des fonctions par famille serait un piège."""
        objects = self.declared_objects
        self.assertEqual(objects["indexes"], sql_columns.declared_indexes(self.files))
        self.assertEqual(objects["triggers"], sql_columns.declared_triggers(self.files))
        self.assertEqual(objects["row_security"], sql_columns.declared_row_security(self.files))
        self.assertEqual(objects["policies"], sql_columns.declared_policies(self.files))
        self.assertEqual(objects["constraints"], sql_columns.declared_constraints(self.files))
        self.assertEqual(objects["revokes"], sql_columns.declared_revokes(self.files))


class CompareObjectsTest(_SnapshotTest):
    """Ce que la base a perdu ou gagné, hors colonnes : la décision, seule."""

    TABLES = ("users", "insights")

    def compare(self, tables=None, **overrides):
        objects = self.catalog(**overrides)
        declared = self.declared_objects
        tables = tuple(tables or self.TABLES)
        expectations = check_schema_drift.privilege_expectations(declared, self.declared)
        return check_schema_drift.compare_objects(
            declared, objects, tables, tables, expectations
        )

    def kinds(self, findings):
        return sorted(finding["kind"] for finding in findings)

    def test_the_ideal_catalog_of_the_repository_raises_nothing(self):
        """Le test qui compte le plus : l'outil ne crie pas sur une base juste.

        Un outil qui signale une fausse dérive finit désactivé, et c'est ainsi
        qu'on ne voit plus les vraies.
        """
        self.assertEqual(self.compare(), [])

    def test_a_lost_index_names_the_file_that_creates_it(self):
        """Un index perdu ne casse rien tout de suite : il faut le dire fort."""
        findings = self.compare(indexes={})
        self.assertEqual(self.kinds(findings), [check_schema_drift.LOST_INDEX] * 3)
        detail = findings[0]["detail"]
        self.assertIn("011_core_tables.sql", detail)

    def test_an_index_added_by_hand_is_reported(self):
        indexes = dict(self.catalog()["indexes"])
        indexes["idx_fait_main"] = {
            "table": "users",
            "unique": False,
            "columns": ["paper_mode"],
            "definition": "CREATE INDEX idx_fait_main ON public.users USING btree (paper_mode)",
        }
        findings = self.compare(indexes=indexes)
        self.assertEqual(self.kinds(findings), [check_schema_drift.GAINED_INDEX])
        self.assertEqual(findings[0]["object"], "idx_fait_main")

    def test_an_index_of_an_undeclared_table_is_not_reported(self):
        """La table elle-même est le constat : ses objets n'y ajouteraient rien."""
        objects = self.catalog()
        objects["indexes"]["idx_ailleurs"] = {
            "table": "table_inconnue",
            "unique": False,
            "columns": ["a"],
            "definition": "CREATE INDEX idx_ailleurs ON public.table_inconnue USING btree (a)",
        }
        self.assertEqual(self.compare(indexes=objects["indexes"]), [])

    def test_an_index_that_no_longer_matches_is_reported(self):
        objects = self.catalog()
        objects["indexes"]["idx_insights_created_at"]["columns"] = ["asset"]
        findings = self.compare(indexes=objects["indexes"])
        self.assertEqual(self.kinds(findings), [check_schema_drift.DIFFERENT_INDEX])
        self.assertIn("asset", findings[0]["detail"])
        self.assertIn("011_core_tables.sql", findings[0]["detail"])

    def test_a_lost_primary_key_is_reported_by_its_shape(self):
        objects = self.catalog()
        objects["constraints"]["users"] = [
            shape for shape in objects["constraints"]["users"] if shape["kind"] != "primary key"
        ]
        findings = self.compare(constraints=objects["constraints"])
        self.assertEqual(self.kinds(findings), [check_schema_drift.LOST_CONSTRAINT])
        self.assertEqual(findings[0]["object"], "primary key (id)")
        self.assertIn("011_core_tables.sql", findings[0]["detail"])

    def test_a_constraint_that_changed_validation_is_one_finding(self):
        """Un `validate constraint` est la même contrainte modifiée, pas deux."""
        objects = self.catalog()
        for shape in objects["constraints"]["pending_signals"]:
            if shape["kind"] == "foreign key":
                shape["validated"] = True
        findings = self.compare(tables=("pending_signals",), constraints=objects["constraints"])
        self.assertEqual(self.kinds(findings), [check_schema_drift.DIFFERENT_CONSTRAINT])
        self.assertIn("validée", findings[0]["detail"])

    def test_a_check_constraint_nobody_declared_is_reported(self):
        objects = self.catalog()
        objects["constraints"]["users"].append(
            {
                "name": "users_paper_mode_check",
                "kind": "check",
                "columns": [],
                "references": None,
                "reference_columns": [],
                "on_delete": None,
                "on_update": None,
                "validated": True,
                "definition": "CHECK (paper_mode)",
            }
        )
        findings = self.compare(constraints=objects["constraints"])
        self.assertEqual(self.kinds(findings), [check_schema_drift.GAINED_CONSTRAINT])
        self.assertEqual(findings[0]["object"], "users_paper_mode_check")

    def test_a_lost_trigger_names_its_table_and_its_function(self):
        objects = self.catalog()
        del objects["triggers"]["trg_insights_updated_at"]
        findings = self.compare(triggers=objects["triggers"])
        self.assertEqual(self.kinds(findings), [check_schema_drift.LOST_TRIGGER])
        self.assertEqual(findings[0]["table"], "insights")
        self.assertEqual(findings[0]["object"], "trg_insights_updated_at")

    def test_a_trigger_that_calls_another_function_is_reported(self):
        objects = self.catalog()
        objects["triggers"]["trg_users_updated_at"]["function"] = "autre_fonction"
        findings = self.compare(triggers=objects["triggers"])
        self.assertEqual(self.kinds(findings), [check_schema_drift.DIFFERENT_TRIGGER])
        self.assertIn("autre_fonction", findings[0]["detail"])

    def test_a_disabled_row_security_is_reported(self):
        """La comparaison qui **ouvre** la base au lieu de la ralentir."""
        findings = self.compare(row_security={"insights": {"enabled": True, "forced": False}})
        self.assertEqual(self.kinds(findings), [check_schema_drift.RLS_OFF])
        self.assertEqual(findings[0]["table"], "users")
        self.assertEqual(findings[0]["object"], "")
        self.assertIn("007_row_level_security.sql", findings[0]["detail"])

    def test_row_security_nobody_declared_is_reported(self):
        """Le sens inverse : quelqu'un a activé la RLS sans le dire aux migrations."""
        declared = {**self.declared_objects, "row_security": {}}
        enabled = {table: {"enabled": True, "forced": False} for table in self.TABLES}
        findings = check_schema_drift.compare_objects(
            declared, self.catalog(row_security=enabled), self.TABLES, self.TABLES, {}
        )
        self.assertEqual(
            self.kinds(findings), [check_schema_drift.RLS_UNDECLARED] * len(self.TABLES)
        )

    def test_a_policy_is_reported_as_an_opening(self):
        objects = self.catalog()
        objects["policies"]["lecture_pour_tous"] = {
            "table": "users",
            "command": "SELECT",
            "roles": ["anon"],
        }
        findings = self.compare(policies=objects["policies"])
        self.assertEqual(self.kinds(findings), [check_schema_drift.GAINED_POLICY])
        self.assertEqual(findings[0]["object"], "lecture_pour_tous")
        self.assertIn("deny by default", findings[0]["detail"])

    def test_a_policy_declared_but_absent_is_reported(self):
        """Le sens inverse existe aussi, pour le jour où une migration en créera une."""
        declared = dict(self.declared_objects)
        declared["policies"] = {"policy_du_depot": {"table": "users", "file": "012_x.sql"}}
        expectations = check_schema_drift.privilege_expectations(declared, self.declared)
        findings = check_schema_drift.compare_objects(
            declared, self.catalog(), self.TABLES, self.TABLES, expectations
        )
        self.assertEqual(self.kinds(findings), [check_schema_drift.LOST_POLICY])
        self.assertIn("012_x.sql", findings[0]["detail"])

    def test_a_privilege_given_back_to_anon_is_reported(self):
        objects = self.catalog()
        objects["table_privileges"] = [
            {"table": "users", "role": "anon", "privilege": "SELECT"}
        ]
        findings = self.compare(table_privileges=objects["table_privileges"])
        self.assertEqual(self.kinds(findings), [check_schema_drift.GRANTED_PRIVILEGE])
        self.assertEqual(findings[0]["object"], "anon")
        self.assertIn("007_row_level_security.sql", findings[0]["detail"])
        self.assertIn("select", findings[0]["detail"])

    def test_a_privilege_given_to_the_whole_world_is_reported(self):
        """`public` sur une table, c'est `anon` compris : le dépôt ne le veut pas."""
        objects = self.catalog()
        objects["table_privileges"] = [
            {"table": "insights", "role": "public", "privilege": "SELECT"}
        ]
        self.assertEqual(
            self.kinds(self.compare(table_privileges=objects["table_privileges"])),
            [check_schema_drift.GRANTED_PRIVILEGE],
        )

    def test_execute_on_a_function_is_watched_too(self):
        objects = self.catalog()
        objects["function_privileges"] = [
            {"function": "match_knowledge_chunks", "role": "anon", "privilege": "EXECUTE"}
        ]
        findings = self.compare(function_privileges=objects["function_privileges"])
        self.assertEqual(self.kinds(findings), [check_schema_drift.GRANTED_PRIVILEGE])
        self.assertIn("009_knowledge_vectors.sql", findings[0]["detail"])

    def test_public_execute_on_a_function_is_left_alone(self):
        """`PUBLIC` a `execute` par défaut et aucune migration ne le révoque.

        L'exiger ferait crier l'outil sur **toutes** les bases justes — y compris
        celle que la CI vient de construire.
        """
        objects = self.catalog()
        objects["function_privileges"] = [
            {"function": "match_knowledge_chunks", "role": "public", "privilege": "EXECUTE"}
        ]
        self.assertEqual(self.compare(function_privileges=objects["function_privileges"]), [])

    def test_a_privilege_on_a_table_no_migration_revokes_is_left_alone(self):
        """Le modèle vient des fichiers : ce qu'ils ne révoquent pas n'est pas un écart."""
        objects = self.catalog()
        objects["table_privileges"] = [
            {"table": "insights", "role": "anon", "privilege": "SELECT"}
        ]
        declared = dict(self.declared_objects)
        declared["revokes"] = [revoke for revoke in declared["revokes"] if "insights" not in revoke["target"]]
        declared["revokes"] = [
            {**revoke, "target": "users"} if "all tables in schema" in revoke["target"] else revoke
            for revoke in declared["revokes"]
        ]
        expectations = check_schema_drift.privilege_expectations(declared, self.declared)
        findings = check_schema_drift.compare_objects(
            declared, objects, self.TABLES, self.TABLES, expectations
        )
        self.assertEqual(findings, [])

    def test_the_blanket_revoke_covers_every_declared_table(self):
        expectations = check_schema_drift.privilege_expectations(
            self.declared_objects, self.declared
        )
        self.assertEqual(set(expectations["tables"]), set(self.declared))
        self.assertEqual(expectations["roles"], ["anon", "authenticated"])
        self.assertEqual(expectations["functions"], {"match_knowledge_chunks": "009_knowledge_vectors.sql"})

    def test_nothing_is_compared_for_a_table_the_base_does_not_have(self):
        """La table absente est déjà un constat : ses objets n'y ajouteraient rien."""
        self.assertEqual(
            check_schema_drift.compare_objects(
                self.declared_objects, self.catalog(), self.TABLES, ("insights",), {}
            ),
            [],
        )


class ObjectDriftEndToEndTest(_SnapshotTest):
    """Le rapport écrit, du catalogue dégradé jusqu'aux lignes affichées."""

    def run_tool(self, database, argv=None):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(
            check_schema_drift.apply_migrations, "connect", return_value=database
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = check_schema_drift.main(
                argv or ["--database-url", "postgresql://u:p@h/ai_trade_test"]
            )
        return code, output.getvalue(), errors.getvalue()

    def test_a_lost_index_exits_one_and_names_the_migration(self):
        objects = self.catalog()
        del objects["indexes"]["idx_users_paper_mode"]
        code, output, _ = self.run_tool(self.database(objects=objects))
        self.assertEqual(code, 1)
        self.assertIn("users.idx_users_paper_mode", output)
        self.assertIn("011_core_tables.sql", output)
        self.assertIn("index absent", output)

    def test_a_disabled_row_security_is_loud(self):
        objects = self.catalog()
        objects["row_security"] = {
            table: state
            for table, state in objects["row_security"].items()
            if table != "insights"
        }
        code, output, _ = self.run_tool(self.database(objects=objects))
        self.assertEqual(code, 1)
        self.assertIn("RLS désactivée", output)
        self.assertIn("insights", output)

    def test_the_report_says_what_it_compared(self):
        """Un rapport qui ne dit pas ce qu'il a regardé laisse croire qu'il a tout vu."""
        code, output, _ = self.run_tool(self.clean_database())
        self.assertEqual(code, 0)
        self.assertIn("17 index", output)
        self.assertIn("11 déclencheurs", output)
        self.assertIn("14 tables en RLS", output)
        # Les droits aussi : ils ne se comptent pas en objets, ils se disent en
        # rôles privés et en droits rendus — sinon le lecteur croit qu'ils n'ont
        # pas été comparés.
        self.assertIn("Droits — les migrations révoquent pour anon, authenticated", output)
        self.assertIn("14 tables et 1 fonction", output)
        self.assertIn("la base en accorde 0 droit à ces rôles", output)

    def test_the_announced_privilege_count_is_the_number_of_findings(self):
        """Un compte annoncé que le détail ne montre pas est un rapport qui ment."""
        objects = self.catalog()
        objects["table_privileges"] = [
            {"table": "users", "role": "anon", "privilege": "SELECT"},
            {"table": "users", "role": "authenticated", "privilege": "INSERT"},
        ]
        objects["function_privileges"] = [
            {"function": "match_knowledge_chunks", "role": "anon", "privilege": "EXECUTE"}
        ]
        code, output, _ = self.run_tool(
            self.database(objects=objects),
            ["--database-url", "postgresql://u:p@h/ai_trade_test", "--json"],
        )
        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertEqual(report["real_privileges"], {"grants": 3})
        self.assertEqual(
            report["counts"][check_schema_drift.GRANTED_PRIVILEGE],
            report["real_privileges"]["grants"],
        )
        self.assertEqual(report["declared_privileges"]["functions"], 1)

    def test_the_json_report_carries_the_object_findings(self):
        objects = self.catalog()
        del objects["triggers"]["trg_insights_updated_at"]
        code, output, _ = self.run_tool(
            self.database(objects=objects),
            ["--database-url", "postgresql://u:p@h/ai_trade_test", "--json"],
        )
        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertEqual(report["counts"], {"déclencheur absent": 1})
        self.assertIsInstance(report["declared_objects"], dict)
        self.assertEqual(report["real_objects"]["triggers"], 10)

    def test_the_repair_file_says_what_it_does_not_repair(self):
        """Sinon le fichier produit a l'air complet alors qu'il ne l'est pas."""
        objects = self.catalog()
        del objects["indexes"]["idx_users_paper_mode"]
        code, output, _ = self.run_tool(
            self.database(objects=objects),
            ["--database-url", "postgresql://u:p@h/ai_trade_test", "--repair-sql"],
        )
        self.assertEqual(code, 1)
        texte = plat(output)
        self.assertIn("Rien à déclarer ni à retirer", texte)
        self.assertIn("ne répare pas", texte)
        self.assertIn("1 index absent", texte)


class WiringTest(unittest.TestCase):
    """L'outil ne réécrit ni la liste des migrations, ni l'extraction du SQL."""

    def test_it_reads_the_migrations_the_applier_applies(self):
        with mock.patch.object(
            apply_migrations, "migration_files", return_value=[]
        ) as liste:
            self.assertEqual(check_schema_drift.declared_schema(), {})
        self.assertTrue(liste.called, "la liste des migrations doit venir de l'applicateur")

    def test_it_shares_the_extraction_with_the_migration_contracts(self):
        with mock.patch.object(
            sql_columns, "declared_schema_by_file", wraps=sql_columns.declared_schema_by_file
        ) as extraction:
            declared = check_schema_drift.declared_schema()
        self.assertTrue(extraction.called, "une extraction parallèle finirait par diverger")
        self.assertIn("economic_events", declared)

    def test_it_does_not_look_like_the_applier_in_pg_stat_activity(self):
        """`pg_stat_activity` doit distinguer une lecture d'une écriture."""
        self.assertEqual(check_schema_drift.APPLICATION_NAME, "ai-trade-schema-drift")
        self.assertNotEqual(check_schema_drift.APPLICATION_NAME, apply_migrations.APPLICATION_NAME)

    def test_the_next_migration_number_follows_the_highest_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for name in ("001_a.sql", "007_b.sql", "011_c.sql", "notes.txt"):
                (root / name).write_text("", encoding="utf-8")
            self.assertEqual(
                check_schema_drift.next_migration_name(root), "012_rattrapage.sql"
            )

    def test_the_suggested_name_on_this_repository_is_free(self):
        files = sorted(
            (REPO_ROOT / "database" / "migrations").glob("[0-9][0-9][0-9]_*.sql")
        )
        expected = f"{int(files[-1].name[:3]) + 1:03d}_rattrapage.sql"
        self.assertEqual(check_schema_drift.next_migration_name(), expected)

    def test_it_uses_the_same_target_variable_as_the_applier_and_no_other(self):
        """Viser une base est un geste explicite, jamais un héritage d'environnement."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(apply_migrations.ENV_VAR, None)
            with self.assertRaises(RuntimeError) as caught:
                apply_migrations.database_url(None)
        self.assertIn(apply_migrations.ENV_VAR, str(caught.exception))


class RunAndExitCodeTest(_SnapshotTest):
    """Le verdict et le code de sortie, sans toucher à une base."""

    def _run(self, database, argv, *, connect_side_effect=None):
        output, errors = io.StringIO(), io.StringIO()
        target = mock.patch.object(
            check_schema_drift.apply_migrations,
            "connect",
            side_effect=connect_side_effect,
            return_value=database,
        )
        with target as connect, contextlib.redirect_stdout(output), contextlib.redirect_stderr(
            errors
        ):
            code = check_schema_drift.main(argv)
        return code, output.getvalue(), errors.getvalue(), connect

    def test_a_clean_database_exits_zero_and_says_so(self):
        code, output, _, connect = self._run(
            self.clean_database(), ["--database-url", "postgresql://u:p@h/ai_trade_test"]
        )
        self.assertEqual(code, 0)
        self.assertIn("Aucun écart", output)
        # Ce que la base verra dans `pg_stat_activity` : une lecture, pas une
        # migration en cours.
        self.assertEqual(
            connect.call_args.kwargs.get("application_name"),
            check_schema_drift.APPLICATION_NAME,
        )

    def test_a_drifted_database_exits_one_and_names_the_column(self):
        database = database_matching(self.declared, without_columns={"macro_bias_logs": "decision"})
        code, output, _, _ = self._run(
            database, ["--database-url", "postgresql://u:p@h/ai_trade_test"]
        )
        self.assertEqual(code, 1)
        self.assertIn("macro_bias_logs.decision", output)
        self.assertIn("005_forex_factory.sql", output)

    def test_the_json_report_carries_the_same_verdict(self):
        database = self.database(without_tables={"bot_settings"})
        code, output, _, _ = self._run(
            database, ["--database-url", "postgresql://u:p@h/ai_trade_test", "--json"]
        )
        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertFalse(report["ok"])
        self.assertEqual(report["counts"], {"table absente": 1})
        self.assertEqual(report["schema"], "public")
        self.assertEqual(report["findings"][0]["table"], "bot_settings")

    def test_the_password_never_ends_up_in_the_report(self):
        code, output, errors, _ = self._run(
            self.clean_database(),
            ["--database-url", "postgresql://postgres:motdepasse@db.projet.supabase.co:5432/postgres"],
        )
        self.assertEqual(code, 0)
        self.assertNotIn("motdepasse", output + errors)
        self.assertIn("postgres:***@", output)

    def test_without_a_target_it_exits_with_two(self):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(apply_migrations.ENV_VAR, None)
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = check_schema_drift.main([])
        self.assertEqual(code, 2)
        self.assertIn(apply_migrations.ENV_VAR, errors.getvalue())

    def test_an_unreachable_database_exits_one_without_a_traceback(self):
        """Une URL donnée veut dire que quelqu'un attend une réponse : pas de silence."""
        code, _, errors, _ = self._run(
            None,
            ["--database-url", "postgresql://u:p@127.0.0.1:1/ai_trade_test"],
            connect_side_effect=RuntimeError("connexion impossible"),
        )
        self.assertEqual(code, 1)
        self.assertIn("connexion impossible", errors)

    def test_bad_usage_exits_with_two(self):
        with self.assertRaises(SystemExit) as raised:
            check_schema_drift.main(["--inconnu"])
        self.assertEqual(raised.exception.code, 2)

    def test_the_repair_sql_goes_to_stdout_and_the_report_to_stderr(self):
        """`> 012_rattrapage.sql` doit donner un fichier propre, et le verdict rester visible."""
        database = database_matching(self.declared, extra={"users": {"colonne_ajoutee"}})
        code, output, errors, _ = self._run(
            database, ["--database-url", "postgresql://u:p@h/ai_trade_test", "--repair-sql"]
        )
        self.assertEqual(code, 1, "écrire la migration ne fait pas disparaître la dérive")
        self.assertIn("-- Migration de rattrapage", output)
        self.assertIn("drop column if exists colonne_ajoutee", output)
        self.assertIn("colonne en base, jamais déclarée", errors)

    def test_the_hint_names_the_next_free_migration_number(self):
        database = database_matching(self.declared, extra={"users": {"colonne_ajoutee"}})
        _, _, errors, _ = self._run(
            database, ["--database-url", "postgresql://u:p@h/ai_trade_test", "--repair-sql"]
        )
        self.assertIn(
            f"database/migrations/{check_schema_drift.next_migration_name()}", errors
        )

    def test_a_clean_database_writes_a_file_that_says_so(self):
        code, output, errors, _ = self._run(
            self.clean_database(), ["--database-url", "postgresql://u:p@h/ai_trade_test", "--repair-sql"]
        )
        self.assertEqual(code, 0)
        self.assertIn("Rien à rattraper", output)
        self.assertIn("Aucun écart", errors)

    def test_the_repair_sql_does_not_create_the_file_it_suggests(self):
        """L'outil écrit sa proposition sur la sortie standard, il ne la range pas."""
        suggested = check_schema_drift.next_migration_name()
        path = REPO_ROOT / "database" / "migrations" / suggested
        self.assertFalse(path.exists(), f"{suggested} existe déjà : le nom n'est plus libre")
        database = database_matching(self.declared, extra={"users": {"colonne_ajoutee"}})
        self._run(database, ["--database-url", "postgresql://u:p@h/ai_trade_test", "--repair-sql"])
        self.assertFalse(path.exists(), "l'outil a écrit dans database/migrations/")

    def test_the_json_report_carries_the_plan(self):
        """Celui qui automatise n'a pas à relire le rapport en clair."""
        database = database_matching(self.declared, extra={"users": {"colonne_ajoutee"}})
        code, output, _, _ = self._run(
            database, ["--database-url", "postgresql://u:p@h/ai_trade_test", "--json"]
        )
        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertEqual(
            report["repair"]["drop"], [{"table": "users", "column": "colonne_ajoutee"}]
        )
        self.assertEqual(report["repair"]["declare"], [])

    def test_the_sql_and_the_json_cannot_share_the_stdout(self):
        """Un fichier de migration avec du JSON dedans ne s'applique pas."""
        with self.assertRaises(SystemExit) as raised:
            check_schema_drift.main(["--json", "--repair-sql"])
        self.assertEqual(raised.exception.code, 2)

    def test_the_password_never_ends_up_in_the_repair_file(self):
        database = database_matching(self.declared, extra={"users": {"colonne_ajoutee"}})
        _, output, errors, _ = self._run(
            database,
            [
                "--database-url",
                "postgresql://postgres:motdepasse@db.projet.supabase.co:5432/postgres",
                "--repair-sql",
            ],
        )
        self.assertNotIn("motdepasse", output + errors)
        self.assertIn("postgres:***@", output)

    def test_the_tool_is_documented(self):
        """Un outil qui n'est pas documenté est un outil que personne ne lance."""
        self.assertIn("check_schema_drift.py", README.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
