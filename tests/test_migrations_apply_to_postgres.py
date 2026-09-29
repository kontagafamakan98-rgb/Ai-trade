"""Les migrations **exécutées** sur un vrai PostgreSQL, puis vérifiées.

Les autres contrats de migration (`tests/test_core_tables_migration.py`,
`tests/test_engine_tables_migration.py`) relisent les fichiers SQL et les
sources Python. C'est utile, mais ça ne dit qu'une chose : le texte dit ce qu'il
veut dire. Rien là-dedans n'exécute le SQL, donc rien n'attrape ce qu'un
`psql` refuserait du premier coup : un `do $$` non refermé, un `alter table` qui
vise une colonne disparue, une parenthèse oubliée, une contrainte que le type
refuse, un `revoke` sur un rôle absent. C'est exactement ce que fait ce fichier —
et c'est ce que fait le job CI `migrations-postgres`, sur une image qui porte
`pgvector`.

Ce qui est vérifié ici est **le schéma obtenu**, pas les fichiers :

* chaque table déclarée existe, et chaque colonne que le **code** utilise aussi —
  c'est le sens qui compte : un `insert` en production vise une colonne, pas une
  déclaration ;
* la RLS est active sur toutes les tables et **aucune policy** n'existe (le
  « deny by default » des migrations 005 à 011, vu depuis la base au lieu d'être
  lu dans un commentaire), et les rôles publics n'ont aucun privilège ;
* chaque table portant un `updated_at` a son trigger `trg_<table>_updated_at`,
  règle dérivée du schéma réel (ajouter la colonne sans le trigger fait échouer
  le test, sans qu'aucune liste ne soit à tenir à jour) ;
* la clé étrangère `pending_signals.user_id → users.id` existe et reste
  `not valid` ;
* `knowledge_chunks.embedding` est bien `vector(768)` après passage par la
  migration 008 puis 009 ;
* **relancer les migrations ne change rien** : c'est l'idempotence des fichiers,
  mesurée sur l'empreinte du schéma et non sur ce qu'en dit leur en-tête ;
* `scripts/check_schema_drift.py` — l'outil qu'on lance à la main — rend **zéro
  écart** sur cette base. C'est la seule fois où il est confronté à un vrai
  PostgreSQL : un outil qui crierait sur une base juste ne se verrait pas dans
  ses propres tests, qui comparent le schéma déclaré à lui-même.

Deux garde-fous, parce que ce fichier **modifie un schéma** :

* il est **ignoré** sans `MIGRATION_DATABASE_URL` — la même variable que
  `scripts/apply_migrations.py`, et aucune autre : il ne doit pas pouvoir partir
  sur la base d'un autre outil par héritage de l'environnement ;
* il refuse une base dont le nom ne contient pas « test », sauf
  `MIGRATION_TEST_ALLOW_ANY=1`. C'est un harnais de test, pas un outil
  d'exploitation : quelqu'un qui veut appliquer les migrations pour de vrai passe
  par `scripts/apply_migrations.py`, qui affiche sa cible avant d'écrire.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import pathlib
import re
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

from scripts import apply_migrations, check_schema_drift
from tests import sql_columns

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
README = REPO_ROOT / "README.md"

DSN_ENV = apply_migrations.ENV_VAR
ALLOW_ANY_ENV = "MIGRATION_TEST_ALLOW_ANY"


def require_psycopg() -> None:
    """Le pilote, ou un test ignoré qui dit comment l'installer.

    La connexion passe ensuite par `apply_migrations.connect()`, pour que le test
    éprouve exactement le chemin de l'outil (dont le délai de connexion) et pas
    un `psycopg.connect()` parallèle qui pourrait diverger.
    """
    if importlib.util.find_spec("psycopg") is None:
        raise unittest.SkipTest("psycopg absent : `pip install -r requirements.txt`")


def looks_like_a_scratch_database(url: str) -> bool:
    """Le nom de la base contient-il « test » ?

    Volontairement grossier : il ne s'agit pas de deviner une intention, mais de
    refuser par défaut une base de production nommée `ai_trade`.
    """
    name = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
    return "test" in name.lower()


def target_url() -> str:
    """L'URL cible, ou un test ignoré — jamais une base non annoncée."""
    url = os.environ.get(DSN_ENV, "").strip()
    if not url:
        raise unittest.SkipTest(
            f"aucune base de test : définis {DSN_ENV} "
            "(voir la section « Migrations » du README)"
        )
    if not looks_like_a_scratch_database(url) and os.environ.get(ALLOW_ANY_ENV) != "1":
        raise unittest.SkipTest(
            f"la base visée ne ressemble pas à une base de test "
            f"({apply_migrations.redacted(url)}) : passe {ALLOW_ANY_ENV}=1 pour l'assumer"
        )
    return url


class PostgresSchemaTest(unittest.TestCase):
    """Connexion, lecture du schéma et empreinte — partagés par deux classes.

    Deux classes interrogent la même base : celle qui applique les migrations sur
    une base neuve, et celle qui les rejoue sur une base **déjà en service**. Elles
    doivent mesurer la même chose avec le même code — une empreinte recopiée
    finirait par ne plus décrire la même base, et les deux verdicts divergeraient
    sans que personne ne le voie.
    """

    connection = None

    @classmethod
    def open(cls) -> None:
        """Connexion, puis migrations appliquées : l'état de référence des deux classes."""
        require_psycopg()
        # Une connexion impossible est un **échec**, pas un test ignoré : si la
        # variable est définie, c'est que quelqu'un attend ce test, et une CI
        # mal configurée ne doit pas passer au vert en silence.
        cls.connection = apply_migrations.connect(target_url())
        # Ces deux classes mêlent lectures, DDL et insertions : une transaction
        # laissée ouverte entre deux requêtes garderait des verrous sur les tables
        # qu'on vient d'altérer. `apply_all()` continue de délimiter ses fichiers.
        cls.connection.autocommit = True
        cls.files = apply_migrations.migration_files()
        cls.declared = sql_columns.declared_schema(cls.files)
        cls.applied = list(
            apply_migrations.apply_all(cls.connection, cls.files, platform_stub=True)
        )

    @classmethod
    def close(cls) -> None:
        if cls.connection is not None:
            cls.connection.close()
            cls.connection = None

    @classmethod
    def rows(cls, statement: str, parameters: tuple = ()) -> list:
        with cls.connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchall()

    @classmethod
    def real_columns(cls) -> dict:
        """Table → colonnes, telles que la base les porte réellement."""
        columns: dict = {}
        for table, column in cls.rows(
            "select table_name, column_name from information_schema.columns "
            "where table_schema = 'public'"
        ):
            columns.setdefault(table, set()).add(column)
        return columns

    #: Ce que l'empreinte relève : chaque objet du schéma public, étiqueté, pour
    #: pouvoir dire **ce qui** a changé et pas seulement « ce n'est plus pareil ».
    FINGERPRINT_QUERIES = (
        (
            "colonne",
            "select table_name, column_name, data_type, is_nullable, "
            "coalesce(column_default, '') from information_schema.columns "
            "where table_schema = 'public'",
        ),
        (
            "table",
            "select c.relname, c.relrowsecurity from pg_class c "
            "join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relkind = 'r'",
        ),
        ("trigger", "select tgname from pg_trigger where not tgisinternal"),
        ("index", "select indexname from pg_indexes where schemaname = 'public'"),
    )

    @classmethod
    def fingerprint(cls) -> list:
        """Le schéma public, une ligne étiquetée par objet, triée.

        Colonnes, RLS, triggers et index par **nom** (jamais par OID : un trigger
        recréé change d'OID sans changer de schéma). Rend une liste plutôt qu'un
        bloc : le test qui compare deux états peut alors nommer les divergences.
        """
        lines = []
        for label, query in cls.FINGERPRINT_QUERIES:
            for row in cls.rows(query):
                cells = [label, *("" if value is None else str(value) for value in row)]
                lines.append("|".join(cells))
        return sorted(lines)


class PostgresMigrationsTest(PostgresSchemaTest):
    """Applique les migrations sur une base **neuve**, puis interroge le schéma obtenu."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.open()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.close()

    def test_the_platform_stub_comes_first(self) -> None:
        """La 008 écrit dans `storage.buckets` et révoque pour `anon` : le stub les crée."""
        self.assertEqual("platform-stub", self.applied[0].file)
        self.assertTrue(self.declared, "aucune table lue dans les migrations")

    def test_the_files_are_applied_in_numeric_order(self) -> None:
        applied = [item.file for item in self.applied if item.file != "platform-stub"]
        self.assertEqual([path.name for path in self.files], applied)
        numbers = [int(name[:3]) for name in applied]
        self.assertEqual(sorted(numbers), numbers)
        self.assertEqual(len(numbers), len(set(numbers)))

    def test_every_declared_table_exists(self) -> None:
        real = {row[0] for row in self.rows(
            "select table_name from information_schema.tables where table_schema = 'public'"
        )}
        self.assertEqual(sorted(set(self.declared) - real), [])

    def test_the_columns_used_by_the_code_exist(self) -> None:
        """Le sens qui compte : ce que le code interroge doit exister en base."""
        used = sql_columns.columns_used_by_code(tuple(self.declared))
        real = self.real_columns()
        missing = {}
        for table, by_file in used.items():
            if not by_file:
                continue
            unknown = set().union(*by_file.values()) - real.get(table, set())
            if unknown:
                missing[table] = {
                    column: sorted(path for path, cols in by_file.items() if column in cols)
                    for column in sorted(unknown)
                }
        self.assertEqual(missing, {}, f"colonnes utilisées par le code, absentes de la base : {missing}")
        self.assertIn("user_id", set().union(*used["pending_signals"].values()))

    def test_the_declared_columns_exist(self) -> None:
        real = self.real_columns()
        missing = {
            table: sorted(columns - real.get(table, set()))
            for table, columns in self.declared.items()
            if columns - real.get(table, set())
        }
        self.assertEqual(missing, {}, f"colonnes déclarées mais absentes de la base : {missing}")

    def test_rls_is_enabled_everywhere_and_no_policy_exists(self) -> None:
        without_rls = sorted(
            row[0]
            for row in self.rows(
                "select c.relname from pg_class c "
                "join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity"
            )
        )
        self.assertEqual(without_rls, [], "tables sans RLS")
        policies = self.rows("select policyname from pg_policies where schemaname = 'public'")
        self.assertEqual(policies, [], "deny by default : aucune policy ne doit exister")

    def test_the_public_roles_have_no_privilege(self) -> None:
        """Le `revoke` des migrations, vérifié sur les rôles et non sur les fichiers."""
        granted = [
            (table, role)
            for table in sorted(self.declared)
            for role in ("anon", "authenticated")
            if self.rows(
                f"select has_table_privilege('{role}', 'public.{table}', 'SELECT')"
            )[0][0]
            or self.rows(
                f"select has_table_privilege('{role}', 'public.{table}', 'INSERT')"
            )[0][0]
        ]
        self.assertEqual(granted, [], "rôles publics avec accès à une table applicative")

    def test_every_table_with_an_updated_at_has_its_trigger(self) -> None:
        """Règle dérivée du schéma réel : pas de liste de tables à maintenir."""
        with_timestamp = sorted(
            table
            for table, columns in self.real_columns().items()
            if "updated_at" in columns
        )
        self.assertTrue(with_timestamp, "aucune table avec `updated_at` : le DDL a-t-il été appliqué ?")
        triggers = {row[0] for row in self.rows("select tgname from pg_trigger where not tgisinternal")}
        missing = [t for t in with_timestamp if f"trg_{t}_updated_at" not in triggers]
        self.assertEqual(missing, [], f"tables avec `updated_at` sans trigger : {missing}")
        self.assertTrue(
            self.rows("select 1 from pg_proc where proname = 'update_updated_at'"),
            "la fonction `update_updated_at()` (migration 001) est absente",
        )

    def test_the_legacy_foreign_key_is_not_validated(self) -> None:
        rows = self.rows(
            "select convalidated from pg_constraint where conname = 'pending_signals_user_id_fkey'"
        )
        self.assertEqual([(False,)], rows)

    def test_the_vector_column_keeps_its_768_dimensions(self) -> None:
        rows = self.rows(
            "select format_type(a.atttypid, a.atttypmod) from pg_attribute a "
            "join pg_class c on c.oid = a.attrelid "
            "join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = 'public' and c.relname = 'knowledge_chunks' "
            "and a.attname = 'embedding'"
        )
        self.assertEqual(1, len(rows), "knowledge_chunks.embedding introuvable")
        self.assertTrue(
            rows[0][0].endswith("vector(768)"),
            f"dimension inattendue : {rows[0][0]}",
        )

    def test_the_drift_check_finds_nothing_on_a_database_built_from_the_migrations(self) -> None:
        """L'outil de dérive, confronté à un vrai PostgreSQL — le seul endroit où ça arrive.

        Ses propres tests comparent le schéma déclaré à un schéma écrit à la main :
        s'il criait sur la base que les migrations viennent de construire, personne
        ne le verrait là-bas. Ici, si.
        """
        findings, context = check_schema_drift.findings_for(self.connection)
        self.assertEqual(findings, [], f"écarts signalés : {findings}")
        self.assertEqual(context["schema"], "public")
        self.assertEqual(context["declared_tables"], len(self.declared))

    def test_re_running_every_migration_changes_nothing(self) -> None:
        """L'idempotence, mesurée : c'est elle qui autorise à relancer la CI."""
        before = self.fingerprint()
        applied = list(apply_migrations.apply_all(self.connection, self.files))
        self.assertEqual(len(self.files), len(applied))
        self.assertEqual(before, self.fingerprint())

    def test_the_target_is_a_database_meant_for_this(self) -> None:
        """Le garde-fou est vérifiable, donc il n'est pas qu'une intention."""
        self.assertTrue(looks_like_a_scratch_database(target_url()))
        self.assertFalse(looks_like_a_scratch_database("postgresql://u:p@host/ai_trade"))
        self.assertTrue(looks_like_a_scratch_database("postgresql://u:p@host/ai_trade_test"))
        self.assertTrue(looks_like_a_scratch_database("postgresql://u:p@host/testing?sslmode=x"))


class ApplierContractTest(unittest.TestCase):
    """L'outil lui-même : ce qu'il refuse, et ce qu'il ne montre jamais."""

    def test_it_lists_the_migrations_without_touching_a_database(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(0, apply_migrations.main(["--dry-run"]))
        text = output.getvalue()
        for name in ("001_user_preferences.sql", "005_forex_factory.sql", "011_core_tables.sql"):
            self.assertIn(name, text)

    def test_it_refuses_to_run_without_a_target(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop(DSN_ENV, None)
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                self.assertEqual(2, apply_migrations.main([]))
        self.assertIn(DSN_ENV, errors.getvalue())
        self.assertIn("--database-url", errors.getvalue())

    def test_the_password_never_ends_up_in_the_target_it_prints(self) -> None:
        url = "postgresql://postgres:motdepasse@localhost:5432/ai_trade_test"
        shown = apply_migrations.redacted(url)
        self.assertNotIn("motdepasse", shown)
        self.assertIn("localhost:5432/ai_trade_test", shown)

    def test_the_migration_list_is_the_one_the_repository_has(self) -> None:
        files = apply_migrations.migration_files()
        self.assertGreaterEqual(len(files), 11, [path.name for path in files])
        self.assertEqual([path.name for path in files], sorted(path.name for path in files))

    def test_a_file_without_a_number_has_no_application_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            (folder / "sans_numero.sql").write_text("select 1;", encoding="utf-8")
            with self.assertRaises(RuntimeError) as caught:
                apply_migrations.migration_files(folder)
        self.assertIn("sans_numero.sql", str(caught.exception))

    def test_a_duplicated_number_is_refused(self) -> None:
        """Deux migrations au même numéro : l'ordre d'application n'existe plus."""
        with tempfile.TemporaryDirectory() as directory:
            folder = pathlib.Path(directory)
            for name in ("007_a.sql", "007_b.sql"):
                (folder / name).write_text("select 1;", encoding="utf-8")
            with self.assertRaises(RuntimeError) as caught:
                apply_migrations.migration_files(folder)
        self.assertIn("007", str(caught.exception))

    def test_the_platform_stub_creates_what_supabase_provides(self) -> None:
        """Sans ces objets, la 008 échoue sur un Postgres nu — autant le dire ici."""
        for expected in ("schema if not exists extensions", "schema if not exists storage",
                         "storage.buckets", "create role"):
            with self.subTest(expected=expected):
                self.assertIn(expected.lower(), apply_migrations.PLATFORM_STUB.lower())
        for role in ("anon", "authenticated", "service_role"):
            self.assertIn(role, apply_migrations.PLATFORM_STUB)

    def test_the_driver_is_declared_in_the_requirements(self) -> None:
        self.assertRegex(REQUIREMENTS.read_text(encoding="utf-8"), r"(?m)^psycopg")

    def test_the_migration_files_are_the_ones_the_other_contracts_read(self) -> None:
        """Le lister et le relire ne doivent pas diverger d'un fichier."""
        self.assertEqual(
            [path.name for path in apply_migrations.migration_files()],
            sorted(path.name for path in apply_migrations.MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")),
        )


class GuardedDdlTest(unittest.TestCase):
    """La moitié statique de la même promesse : chaque DDL est rejouable.

    Un `create table` sans `if not exists` échoue sur une base **en service**
    (« relation already exists »), un `drop column` sans `if exists` échoue sur
    une base **neuve**. Les deux cas comptent : ces fichiers s'appliquent dans
    l'éditeur SQL d'un Supabase en service **et** sur la base jetable de la CI.

    Ce contrat-ci ne se contente pas de le promettre : il le lit. Il attrape donc
    l'oubli au moment où il est écrit, sans attendre qu'un serveur le refuse.
    """

    def test_no_migration_ships_an_unguarded_statement(self) -> None:
        unguarded = sql_columns.unguarded_ddl(apply_migrations.migration_files())
        self.assertEqual(unguarded, [], f"DDL sans garde-fou : {unguarded}")

    def test_the_detector_sees_an_unguarded_statement(self) -> None:
        """Sinon le contrat ci-dessus passerait au vert en ne vérifiant rien."""
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "999_probe.sql"
            path.write_text(
                "create table sonde (id text);\n"
                "alter table sonde add column garde text;\n"
                "alter table sonde drop column disparue;\n"
                "drop index idx_sonde;\n",
                encoding="utf-8",
            )
            unguarded = sql_columns.unguarded_ddl([path])
        self.assertCountEqual(
            [statement for _, _, statement in unguarded],
            [
                "create table : create table",
                "drop index : drop index",
                "add column : add column garde",
                "drop column : drop column disparue",
            ],
        )
        self.assertEqual(
            sorted(line for _, line, _ in unguarded), [1, 2, 3, 4], "lignes du fichier"
        )

    def test_a_comment_citing_ddl_is_not_a_statement(self) -> None:
        """Ces fichiers expliquent leur DDL en prose — elle ne doit pas être lue.

        C'est arrivé pour de vrai : un commentaire de `011` citant « `add column if
        not exists` » faisait déclarer une colonne nommée `if` sur `users`, que
        seule l'exécution réelle signalait.
        """
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "999_probe.sql"
            path.write_text(
                "-- on écrit `alter table t add column x` et `create table t (...)`\n"
                "select 1;\n",
                encoding="utf-8",
            )
            self.assertEqual(sql_columns.unguarded_ddl([path]), [])
            self.assertEqual(sql_columns.declared_schema_by_file([path]), {})
            self.assertEqual(sql_columns.added_columns_by_file([path]), {})

    def test_the_tables_in_service_have_their_repair_statements(self) -> None:
        """`004` ajoute trois colonnes à une table de `001` : le cas type.

        Ces tables-là existaient en base avant que leur fichier ne les décrive, et
        c'est `add column if not exists` qui les répare. La liste est épinglée ici
        parce qu'elle est la définition de « base déjà en service » que le test
        dynamique utilise : la voir se vider serait un signal, pas un détail.
        """
        added = sql_columns.added_columns_by_file(apply_migrations.migration_files())
        self.assertEqual(
            sorted(added["user_preferences"]),
            ["max_daily_loss_pct", "max_open_trades", "max_total_drawdown_pct"],
        )
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
                self.assertTrue(added.get(table), f"{table} : aucune colonne de réparation")


class AlreadyInServiceTest(PostgresSchemaTest):
    """Rejoue les migrations sur une base qui a **déjà servi**.

    L'autre classe les applique sur une base neuve : elle ne peut donc pas voir ce
    qui casse quand les tables existent déjà, avec des données dedans et des
    colonnes d'une version antérieure. C'est pourtant le cas réel — Supabase est en
    service, on y relance un fichier complété après coup (005, 006, 009 et 011
    l'ont été) — et rien de tout cela ne doit perdre une ligne.

    Le scénario, dans cet ordre, et **une seule fois** (`setUpClass` : les tests ne
    font qu'observer ce qui a été mesuré) :

    1. l'état de référence est relevé (empreinte complète du schéma) ;
    2. la base est **dégradée** : les colonnes qu'une migration ajoute par
       `add column if not exists` sont retirées — c'est la définition, faite par le
       dépôt lui-même, de ce qu'une base plus ancienne n'a pas, plutôt qu'un
       schéma ancien inventé pour la circonstance. Une colonne qu'une version
       antérieure de `006` créait est aussi réintroduite ;
    3. des **données** sont écrites dans cet état ancien ;
    4. les migrations sont rejouées ;
    5. ce qui a été observé est vérifié, assertion par assertion.

    Les colonnes portées par un index sont écartées de la dégradation (voir
    `protected_columns`) : rien dans ces fichiers ne sait recréer un index perdu.
    """

    #: L'utilisateur des lignes anciennes. Fixé, parce que `pending_signals.user_id`
    #: référence `users.id` : la ligne doit exister pour que le signal soit accepté.
    USER = "11111111-1111-4111-8111-111111111111"

    #: Les tables écrites dans l'état ancien, vidées ensuite : la base visée est
    #: jetable (le nom doit contenir « test », sinon tout ce fichier est ignoré),
    #: et sans ce nettoyage la deuxième exécution buterait sur les identifiants.
    TOUCHED = (
        "pending_signals",
        "insights",
        "user_preferences",
        "user_risk_state",
        "economic_events",
        "macro_bias_logs",
        "trade_post_mortems",
        "adaptive_model_weights",
        "knowledge_chunks",
        "users",
    )

    #: Un littéral par type, pour n'avoir à fournir que les colonnes obligatoires.
    LITERAL = {
        "text": "'ancienne valeur'",
        "character varying": "'ancienne valeur'",
        "uuid": f"'{USER}'",
        "bigint": "7",
        "integer": "7",
        "smallint": "7",
        "numeric": "1.5",
        "double precision": "1.5",
        "real": "1.5",
        "boolean": "false",
        "jsonb": "'{}'::jsonb",
        "timestamptz": "now()",
        "timestamp with time zone": "now()",
        "date": "current_date",
        "text[]": "'{EURUSD}'::text[]",
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.open()
        cls.reset()
        cls.reference = cls.fingerprint()
        cls.removed = cls.degraded_columns()
        cls.degrade()
        cls.write_legacy_rows()
        cls.replayed = len(list(apply_migrations.apply_all(cls.connection, cls.files)))
        cls.repaired = cls.fingerprint()
        cls.findings, cls.context = check_schema_drift.findings_for(cls.connection)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tidy()
        cls.close()

    @classmethod
    def reset(cls) -> None:
        """Reconstruit les tables depuis zéro : une référence vraiment **neuve**.

        Sans cela, la référence serait le schéma laissé par l'exécution
        précédente — c'est-à-dire, si elle a déjà dégradé cette base, le schéma
        **réparé**. Le test comparerait alors son propre résidu à lui-même, et la
        divergence qu'il doit mesurer (ce qu'un `add column` ne peut pas rejouer)
        aurait disparu de la référence : il passerait au vert en ne vérifiant
        rien.

        Les tables sont recréées juste après, par les migrations — c'est leur
        travail — et la base visée est jetable : son nom doit contenir « test »,
        sinon tout ce fichier est ignoré.
        """
        for table in sorted(cls.declared):
            cls.connection.execute(f"drop table if exists public.{table} cascade")
        list(apply_migrations.apply_all(cls.connection, cls.files, platform_stub=True))

    @classmethod
    def tidy(cls) -> None:
        """Vide les tables de sonde, dans l'ordre inverse de la clé étrangère."""
        if cls.connection is None:
            return
        for table in cls.TOUCHED:
            cls.connection.execute(f"delete from public.{table}")

    @classmethod
    def protected_columns(cls, table: str) -> set:
        """Colonnes qu'un rejeu ne peut **pas** ressusciter : celles d'un index.

        Les retirer demanderait de recréer l'index ou la clé, ce qu'aucun
        `add column if not exists` de ce dépôt ne fait. Une base où elles auraient
        disparu n'est pas réparable par rejeu : le test écarte ce cas
        explicitement plutôt que de le contourner en silence.
        """
        return {
            row[0]
            for row in cls.rows(
                "select a.attname from pg_index i "
                "join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey) "
                "where i.indrelid = %s::regclass",
                (f"public.{table}",),
            )
        }

    @classmethod
    def degraded_columns(cls) -> dict:
        """Ce qu'on retire : les colonnes qu'une migration promet de réparer."""
        added = sql_columns.added_columns_by_file(apply_migrations.migration_files())
        columns = cls.real_columns()
        removed = {}
        for table in sorted(added):
            if table not in columns:
                continue
            keep = sorted(set(added[table]) & columns[table] - cls.protected_columns(table))
            if keep:
                removed[table] = keep
        return removed

    @classmethod
    def degrade(cls) -> None:
        for table, columns in cls.removed.items():
            clauses = ", ".join(f"drop column if exists {column}" for column in columns)
            cls.connection.execute(f"alter table public.{table} {clauses}")
        # Une colonne qu'une version antérieure de `006` créait, et que `006` retire
        # aujourd'hui : sur une base en service, elle est là.
        cls.connection.execute(
            "alter table public.adaptive_model_weights "
            "add column if not exists min_confidence_threshold numeric default 0.6"
        )

    @classmethod
    def insert_legacy(cls, table: str, **provided: str) -> None:
        """Écrit une ligne ancienne : l'obligatoire, plus ce que l'appelant nomme.

        Les valeurs sont des **expressions SQL** (`'texte'`, `7`, `null`), pour que
        l'appelant dise exactement ce que cette base ancienne contenait. Les
        colonnes `not null` sans défaut sont remplies d'un littéral du bon type :
        elles ont survécu à la dégradation, donc les fournir une par une dans le
        test ferait dépendre chaque table de son DDL.
        """
        required = [
            (row[0], row[1])
            for row in cls.rows(
                "select column_name, data_type from information_schema.columns "
                "where table_schema = 'public' and table_name = %s "
                "and is_nullable = 'NO' and column_default is null order by ordinal_position",
                (table,),
            )
        ]
        payload = {}
        for column, data_type in required:
            if data_type not in cls.LITERAL:
                raise AssertionError(f"type sans littéral pour {table}.{column} : {data_type}")
            payload[column] = cls.LITERAL[data_type]
        payload.update(provided)
        if not payload:
            cls.connection.execute(f"insert into public.{table} default values")
            return
        columns = ", ".join(payload)
        values = ", ".join(payload.values())
        cls.connection.execute(f"insert into public.{table} ({columns}) values ({values})")

    @classmethod
    def write_legacy_rows(cls) -> None:
        """Une ligne par table concernée, chacune avec sa raison d'être là."""
        cls.insert_legacy("users", id=f"'{cls.USER}'")
        cls.insert_legacy(
            "user_preferences",
            user_id=f"'{cls.USER}'",
            watchlist="'{EURUSD}'::text[]",
        )
        cls.insert_legacy("user_risk_state", user_id=f"'{cls.USER}'")
        cls.insert_legacy(
            "pending_signals", user_id=f"'{cls.USER}'", id="gen_random_uuid()"
        )
        cls.insert_legacy("insights")
        cls.insert_legacy("economic_events", id="'ff_probe_legacy'", currency="'USD'")
        cls.insert_legacy("macro_bias_logs")
        cls.insert_legacy("trade_post_mortems")
        cls.insert_legacy(
            "adaptive_model_weights", asset="'PROBELEGACY'", min_confidence_threshold="0.9"
        )
        cls.insert_legacy("knowledge_chunks")

    def test_the_scenario_removed_enough_to_prove_something(self) -> None:
        """Sinon tout ce fichier passerait au vert sans rien éprouver."""
        self.assertGreaterEqual(
            sum(len(columns) for columns in self.removed.values()),
            40,
            f"trop peu de colonnes retirées : {self.removed}",
        )
        self.assertEqual(self.replayed, len(self.files))

    def test_every_column_the_migrations_promise_to_repair_is_back(self) -> None:
        """Le cœur du sujet : `add column if not exists` sur une table déjà peuplée."""
        real = self.real_columns()
        missing = {
            table: sorted(set(columns) - real.get(table, set()))
            for table, columns in self.removed.items()
            if set(columns) - real.get(table, set())
        }
        self.assertEqual(missing, {}, "colonnes qu'une base en service n'a pas récupérées")

    def test_the_application_can_read_the_repaired_schema(self) -> None:
        """La moitié qui compte : c'est le code qui lit ces colonnes, pas un test."""
        self.assertEqual(self.findings, [], f"écarts signalés : {self.findings}")
        self.assertEqual(self.context["schema"], "public")

    def test_the_rows_written_before_the_repair_are_still_there(self) -> None:
        """Une colonne ajoutée à une table peuplée ne doit rien perdre."""
        self.assertEqual(
            self.rows("select count(*) from public.users where id = %s", (self.USER,)),
            [(1,)],
        )
        self.assertEqual(
            self.rows(
                "select watchlist from public.user_preferences where user_id = %s", (self.USER,)
            ),
            [(["EURUSD"],)],
        )
        self.assertEqual(
            self.rows(
                "select count(*) from public.pending_signals where user_id = %s", (self.USER,)
            ),
            [(1,)],
        )
        self.assertEqual(
            self.rows("select count(*) from public.economic_events where id = 'ff_probe_legacy'"),
            [(1,)],
        )

    def test_a_column_born_after_the_row_gets_its_declared_default(self) -> None:
        """Postgres remplit les lignes déjà là : c'est ce que `default` déclare.

        C'est le point que rien d'autre ne vérifie — une colonne ajoutée à une
        table **vide** ne prouve aucun défaut, elle ne prouve qu'une déclaration.
        """
        self.assertEqual(
            self.rows(
                "select ta_weight, total_trades, consecutive_losses "
                "from public.adaptive_model_weights where asset = 'PROBELEGACY'"
            ),
            [(Decimal("0.40"), 0, 0)],
        )
        self.assertEqual(
            self.rows(
                "select currency, created_at is not null from public.economic_events "
                "where id = 'ff_probe_legacy'"
            ),
            [("USD", True)],
        )

    def test_a_legacy_column_that_no_code_uses_disappears(self) -> None:
        """L'autre sens : `drop column if exists`, sur une base qui l'avait."""
        self.assertEqual(
            self.rows(
                "select count(*) from information_schema.columns "
                "where table_schema = 'public' and table_name = 'adaptive_model_weights' "
                "and column_name = 'min_confidence_threshold'"
            ),
            [(0,)],
        )

    def test_a_serviced_database_converges_except_on_repair_columns(self) -> None:
        """La convergence : une base en service rejoint le schéma d'une neuve.

        **À une exception près, mesurée** — elle est nommée ici plutôt que passée
        sous silence. Onze colonnes restent en écart, et deux raisons seulement :

        * dix d'entre elles sont `not null` dans le `create table` et **nullables**
          après réparation (`economic_events.title`, `macro_bias_logs.symbol`,
          `currency`, `macro_score`, `news_risk_level`, `decision`,
          `trade_post_mortems.signal_id`, `direction`, `outcome`,
          `learned_lesson`). Un `add column … not null` sans défaut est refusé par
          Postgres dès que la table a des lignes — et on n'invente pas une valeur
          de remplissage pour un actif ou une leçon. Une base en service ne peut
          donc pas récupérer cette contrainte, seulement la colonne ;
        * `insights.type` porte un défaut (`'geopolitical'`) dans son `add column`
          et pas dans son `create table` : c'est la valeur qui permet de poser la
          colonne `not null` sur une base déjà peuplée.

        Rien d'autre n'a le droit de diverger : ni une colonne absente, ni un type,
        ni un index, ni un trigger, ni un défaut perdu. L'exception est vérifiée
        comme telle — sa forme (même nom, même type) et sa non-vacuité — pour
        qu'un élargissement se voie au lieu de se fondre dans un « ça diverge ».
        """
        reference, repaired = set(self.reference), set(self.repaired)
        explained, unexplained = [], []
        # Une divergence se présente des **deux** côtés : la ligne d'avant a
        # disparu, celle d'après est apparue. Les deux se lisent ensemble.
        for line in sorted(reference ^ repaired):
            label, table, column, data_type, *_rest = [*line.split("|"), "", "", ""]
            other = repaired if line in reference else reference
            same_shape = [
                candidate
                for candidate in other
                if candidate.startswith(f"colonne|{table}|{column}|{data_type}")
            ]
            # La colonne est revenue, sous le même nom et le même type : seule sa
            # contrainte a changé, ce qu'un `add column` sur une table peuplée ne
            # peut pas rejouer. Tout le reste — une colonne absente, un index, un
            # trigger, un type — n'a pas d'explication et doit faire échouer.
            if column not in self.removed.get(table, []) or len(same_shape) != 1:
                unexplained.append(line)
                continue
            explained.append(line)
        self.assertEqual(
            unexplained, [], f"divergences de schéma non expliquées : {unexplained}"
        )
        self.assertGreater(
            len(explained),
            0,
            "aucune colonne de réparation sans sa contrainte : le DDL aurait changé "
            "de forme, ou ce test ne mesure plus rien",
        )


class CiWiringContractTest(unittest.TestCase):
    """Le job qui exécute vraiment les migrations, verrouillé dans le workflow.

    Même raison que `tests/test_kotlin_test_wiring.py` : un réglage absent ne
    produit pas d'erreur, il produit un job qui passe au vert sans rien avoir
    exécuté. Ici, la façon de se tromper est silencieuse — une image sans
    `pgvector`, un service qui n'est pas encore prêt, un `--platform-stub`
    oublié — et le seul endroit où ça se voit est la CI elle-même.
    """

    JOB = "migrations-postgres"

    def setUp(self) -> None:
        self.workflow = WORKFLOW.read_text(encoding="utf-8")
        match = re.search(
            rf"^  {re.escape(self.JOB)}:\n(.*?)(?=^  \S|\Z)",
            self.workflow,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(match, f"job `{self.JOB}` absent de {WORKFLOW.name}")
        self.job = match.group(1)

    def test_the_job_uses_an_image_that_carries_pgvector(self) -> None:
        """`postgres:16` n'a pas l'extension `vector` : la 008 y échouerait."""
        self.assertRegex(self.job, r"image:\s*pgvector/pgvector")

    def test_the_job_waits_for_postgres_to_answer(self) -> None:
        self.assertIn("--health-cmd pg_isready", self.job)

    def test_the_job_runs_the_tool_and_the_verification(self) -> None:
        self.assertIn("scripts/apply_migrations.py", self.job)
        self.assertIn("tests.test_migrations_apply_to_postgres", self.job)
        self.assertIn("--platform-stub", self.job)

    def test_the_job_installs_the_driver(self) -> None:
        self.assertIn("pip install -r requirements.txt", self.job)

    def test_the_job_targets_a_database_named_like_a_test_one(self) -> None:
        url = re.search(rf"{DSN_ENV}:\s*\"?(\S+?)\"?\s*$", self.job, re.MULTILINE)
        self.assertIsNotNone(url, f"{DSN_ENV} non défini dans le job")
        self.assertTrue(looks_like_a_scratch_database(url.group(1)), url.group(1))

    def test_the_main_job_still_collects_the_file(self) -> None:
        """La suite ordinaire doit voir ce test — et l'ignorer sans base."""
        self.assertRegex(self.workflow, r"(?m)^  python:")

    def test_the_tool_is_documented(self) -> None:
        """Un outil qui n'est pas documenté est un outil que personne ne lance."""
        self.assertIn("apply_migrations.py", README.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
