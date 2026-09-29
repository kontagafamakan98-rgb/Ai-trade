"""Contrat de la migration `011_core_tables.sql` (users, insights, pending_signals).

Ces trois tables existaient **avant** le dossier de migrations et n'avaient aucun
DDL dans le dépôt : la migration 007 les suppose présentes, et une base neuve ne
pouvait donc pas démarrer. Ce fichier verrouille la migration dans les deux sens,
parce que c'est tout l'intérêt d'un DDL écrit après coup :

* **le code ne demande rien qui manque** — les colonnes utilisées dans les
  requêtes (extraites des sources Python, pas d'une liste recopiée : l'extraction
  est celle de `tests/sql_columns.py`, partagée avec le contrat des migrations 005
  et 006) doivent toutes être déclarées, sinon un `insert` en production échoue en
  pleine nuit ;
* **la migration n'invente rien** — chaque colonne déclarée est justifiée par le
  code qui l'écrit ou la lit (ou marquée « convention » quand elle n'existe que
  pour le trigger `updated_at`), pour qu'elle ne devienne pas un schéma idéal
  déconnecté de l'application.

S'y ajoute le contrat habituel des migrations de ce dépôt : idempotence, RLS
« deny by default » avec révocation, triggers `updated_at`, et documentation dans
le README (une migration non documentée est une migration qu'on oublie
d'appliquer).
"""
from __future__ import annotations

import pathlib
import re
import unittest
from typing import Dict, List, Set, Tuple

from tests import sql_columns

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "database" / "migrations" / "011_core_tables.sql"
README = REPO_ROOT / "README.md"

TABLES = ("users", "insights", "pending_signals")

#: Opérations de requête dont le **premier argument** nomme une colonne.
FILTER_OPS = (
    "select|eq|neq|gt|gte|lt|lte|like|ilike|is_|in_|order|contains|filter|not_|or_"
)
QUOTED_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")
#: Colonnes qui n'existent que par convention (défaut `now()` + trigger) : elles
#: ne sont lues par aucune requête, et c'est normal.
CONVENTION = "#convention"

#: Colonnes attendues, chacune **justifiée** : soit par un jeton présent dans un
#: fichier qui l'écrit ou la lit, soit par la convention `updated_at`/`created_at`.
EXPECTED: Dict[str, Dict[str, Tuple[str, str]]] = {
    "users": {
        "id": ("main.py", '"id": str(user.id)'),
        "username": ("main.py", '"username": user.username'),
        "first_name": ("main.py", '"first_name": user.first_name'),
        "telegram_chat_id": ("main.py", '"telegram_chat_id": chat_id'),
        "paper_mode": ("main.py", '"paper_mode": True'),
        "created_at": (CONVENTION, "défaut `now()`"),
        "updated_at": (CONVENTION, "trigger `updated_at`"),
    },
    "insights": {
        "id": ("database/supabase_client.py", 'select("id", count="exact")'),
        "type": ("ai/decision_engine.py", 'i.get("type") == "geopolitical"'),
        "asset": ("database/supabase_client.py", '.eq("asset", asset)'),
        "title": ("ai/decision_engine.py", 'relevant[0]["title"]'),
        "summary": ("scrapers/news_geo.py", '"summary": (entry.get("summary") or "")[:1200]'),
        "source": ("scrapers/news_geo.py", '"source": feed_url'),
        "url": ("scrapers/news_geo.py", '"url": entry.get("link")'),
        "confidence": ("scrapers/news_geo.py", '"confidence": 0.65'),
        "data": ("ai/decision_engine.py", 'sent[0].get("data", {})'),
        "created_at": ("database/supabase_client.py", 'insight.setdefault("created_at"'),
        "updated_at": (CONVENTION, "trigger `updated_at`"),
    },
    "pending_signals": {
        "id": ("main.py", '.eq("id", signal_id)'),
        "user_id": ("database/supabase_client.py", '"user_id": user_id'),
        "signal": ("database/supabase_client.py", '"signal": signal'),
        "status": ("database/supabase_client.py", '"status": "pending"'),
        "execution_result": ("workers/performance_tracker.py", '"execution_result": exec_res'),
        "created_at": ("workers/signal_guard.py", '.gte("created_at", since)'),
        "validated_at": ("database/supabase_client.py", '"validated_at":'),
        "updated_at": (CONVENTION, "trigger `updated_at`"),
    },
}

#: Statuts écrits par le code (verrouillés ici : le schéma ne les contraint pas,
#: une contrainte `check` ajoutée à une table en service refuserait l'historique).
STATUSES = ("pending", "executed", "rejected", "won", "lost")


def sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def declared_columns() -> Dict[str, Set[str]]:
    """Colonnes déclarées dans le `create table` de chaque table (migration 011)."""
    return sql_columns.declared_columns(MIGRATION, TABLES)


def columns_used_by_code() -> Dict[str, Dict[str, Set[str]]]:
    """Colonnes touchées par le code, par table et par fichier (extraction réelle)."""
    return sql_columns.columns_used_by_code(TABLES)


class DdlCoversTheCodeColumnsTest(unittest.TestCase):
    """Aucune requête du code ne doit viser une colonne absente du DDL."""

    def test_every_column_used_by_a_query_is_declared(self):
        declared = declared_columns()
        used = columns_used_by_code()
        missing = {}
        for table, by_file in used.items():
            unknown = set().union(*by_file.values()) - declared[table] if by_file else set()
            if unknown:
                missing[table] = {name: sorted(files) for name in sorted(unknown) for files in [self._files_for(by_file, name)]}
        self.assertEqual(missing, {}, f"colonnes utilisées mais non déclarées : {missing}")

    @staticmethod
    def _files_for(by_file: Dict[str, Set[str]], column: str) -> List[str]:
        return sorted(path for path, columns in by_file.items() if column in columns)

    def test_the_scan_actually_sees_the_queries(self):
        """Un extracteur qui ne trouve rien ferait passer le test précédent à vide."""
        used = columns_used_by_code()
        for table in TABLES:
            with self.subTest(table=table):
                self.assertTrue(used[table], f"aucune requête reconnue sur {table}")
        self.assertIn("user_id", set().union(*used["pending_signals"].values()))
        self.assertIn("paper_mode", set().union(*used["users"].values()))

    def test_the_scan_does_not_borrow_the_next_query_columns(self):
        """`macro_bias_logs` ne doit pas prêter ses colonnes à `pending_signals`."""
        used = set().union(*columns_used_by_code()["pending_signals"].values())
        for other in ("symbol", "currency", "macro_score", "decision", "reasoning"):
            self.assertNotIn(other, used)

    def test_a_document_column_does_not_lend_its_keys(self):
        """`signal` et `execution_result` sont des **colonnes**, pas des schémas.

        La sonde de configuration y écrit un document JSON (`asset`, `direction`,
        `stop_loss`…) : ses clés ne sont pas des colonnes de `pending_signals`, et
        les prendre pour telles réclamerait à la migration des colonnes que
        l'application ne touche jamais.
        """
        used = set().union(*columns_used_by_code()["pending_signals"].values())
        for key in ("asset", "direction", "entry", "stop_loss", "take_profit", "method"):
            with self.subTest(key=key):
                self.assertNotIn(key, used)


class DeclaredColumnsAreJustifiedTest(unittest.TestCase):
    """Le DDL ne déclare pas de colonne que personne n'utilise."""

    def test_nothing_outside_the_expected_set(self):
        declared = declared_columns()
        for table in TABLES:
            with self.subTest(table=table):
                self.assertEqual(declared[table], set(EXPECTED[table]))

    def test_each_column_is_backed_by_the_code(self):
        for table, columns in EXPECTED.items():
            for column, (source, token) in columns.items():
                if source == CONVENTION:
                    continue
                with self.subTest(table=table, column=column):
                    text = (REPO_ROOT / source).read_text(encoding="utf-8")
                    self.assertIn(
                        token, text, f"{table}.{column} : {source} ne contient plus {token!r}"
                    )

    def test_the_statuses_written_by_the_code_are_documented(self):
        """Le vocabulaire des statuts est verrouillé ici, faute de contrainte SQL."""
        text = sql()
        for status in STATUSES:
            with self.subTest(status=status):
                self.assertIn(status, text)

    def test_no_existing_column_type_is_changed(self):
        """`alter column … type` réécrit une table en service : à faire à part."""
        statements = [
            line.split("--")[0].lower()
            for line in sql().splitlines()
            if not line.strip().startswith("--")
        ]
        self.assertNotIn("alter column", "\n".join(statements))


class MigrationContractTest(unittest.TestCase):
    """Idempotence, RLS, triggers, clés étrangères, index — et documentation."""

    def setUp(self) -> None:
        self.text = sql()

    def test_the_migration_is_documented_in_the_readme(self):
        self.assertIn(MIGRATION.name, README.read_text(encoding="utf-8"))

    def test_it_can_be_applied_to_a_database_already_in_service(self):
        self.assertIn("create table if not exists", self.text)
        self.assertIn("add column if not exists", self.text)
        self.assertIn("create index if not exists", self.text)

    def test_every_table_can_be_re_run_safely(self):
        for table in TABLES:
            with self.subTest(table=table):
                self.assertIn(f"create table if not exists {table} ", self.text)
                self.assertIn(
                    f"drop trigger if exists trg_{table}_updated_at on {table}", self.text
                )
                self.assertIn(
                    f"create trigger trg_{table}_updated_at", self.text
                )
                self.assertIn(f"alter table if exists {table}", self.text)

    def test_deny_by_default(self):
        for table in TABLES:
            with self.subTest(table=table):
                self.assertRegex(
                    self.text,
                    rf"alter table if exists {table}\s+enable row level security",
                )
        self.assertIn("revoke all on users", self.text)
        self.assertIn("revoke all on insights", self.text)
        self.assertIn("revoke all on pending_signals", self.text)
        self.assertIn("from anon, authenticated", self.text)

    def test_no_policy_is_created(self):
        """Deny by default : une policy ouvrirait la table aux clés publiques."""
        self.assertNotIn("create policy", self.text.lower())

    def test_the_foreign_key_tolerates_legacy_rows(self):
        """`not valid` : la contrainte s'applique aux nouvelles lignes seulement."""
        self.assertIn("pending_signals_user_id_fkey", self.text)
        self.assertIn("references users (id) on delete cascade not valid", self.text)
        self.assertIn("select 1 from pg_constraint where conname", self.text)

    def test_each_index_serves_a_query_of_the_code(self):
        """Un index sans requête est un coût d'écriture, pas une optimisation."""
        expected = {
            "idx_users_paper_mode": "paper_mode",
            "idx_insights_created_at": "created_at",
            "idx_insights_asset_created_at": "asset",
            "idx_pending_signals_user_status": "user_id",
            "idx_pending_signals_status": "status",
            "idx_pending_signals_created_at": "created_at",
            "idx_pending_signals_validated_at": "validated_at",
        }
        for index, column in expected.items():
            with self.subTest(index=index):
                self.assertIn(f"create index if not exists {index}", self.text)
                block = self.text.split(f"create index if not exists {index}", 1)[1]
                self.assertIn(column, block.split(";", 1)[0])

    def test_the_json_columns_have_a_default(self):
        """Un `not null` sans défaut ferait échouer l'insertion du code."""
        self.assertIn("signal jsonb not null default '{}'::jsonb", self.text)
        self.assertIn("data jsonb not null default '{}'::jsonb", self.text)

    def test_the_dollar_quoted_block_is_closed(self):
        """Un `do $$` non refermé ne se voit qu'au moment où on l'applique."""
        self.assertEqual(self.text.count("$$"), 2 * self.text.count("do $$"))
        self.assertIn("end $$;", self.text)

    def test_every_statement_is_terminated(self):
        """Un point-virgule oublié ferait avaler la suite du fichier à Postgres.

        Une instruction suivante qui **démarre** alors que la précédente n'est pas
        close (et qu'on n'est pas dans une parenthèse) est exactement le symptôme.
        """
        begins = re.compile(
            r"^(create|alter|drop|revoke|grant|insert|update|delete|select|do|comment)\b",
            re.IGNORECASE,
        )
        depth = 0
        quoted = 0  # `$$` : le corps d'un bloc `do` n'est pas du SQL de premier niveau
        pending: List[str] = []
        for line in self.text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            quoted += stripped.count("$$")
            depth += sum(line.count(char) for char in "([")
            depth -= sum(line.count(char) for char in ")]")
            inside_block = quoted % 2 == 1
            if pending and depth == 0 and not inside_block and begins.match(stripped):
                self.fail(f"instruction non terminée : {pending[0]!r}")
            pending.append(stripped)
            if depth <= 0 and not inside_block and stripped.endswith(";"):
                pending = []
        self.assertEqual(pending, [], f"dernière instruction non terminée : {pending}")


if __name__ == "__main__":
    unittest.main()
