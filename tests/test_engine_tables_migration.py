"""Contrat des migrations `005_forex_factory.sql` et `006_adaptive_learning.sql`.

Quatre tables que le code lit et écrit en production — le calendrier économique,
le journal des décisions macro, les post-mortems de trades et les poids
adaptatifs — n'avaient aucun contrat : rien ne reliait leur DDL aux requêtes.
Une colonne ajoutée côté code, ou retirée côté SQL, ne se serait vue qu'en pleine
nuit, sur un `insert` refusé.

Ce fichier reprend les deux règles du contrat de la migration 011, avec la **même
extraction** (`tests/sql_columns.py`, partagée, pas recopiée) :

* **le code ne demande rien qui manque** — les colonnes touchées par une requête
  doivent toutes être déclarées ;
* **la migration n'invente rien** — chaque colonne déclarée est justifiée par le
  fichier qui l'écrit ou la lit (ou marquée « convention »), et l'extraction ne
  peut pas voir les colonnes d'un dictionnaire construit dans un autre module
  (`scrapers/forex_factory.py` remplit `economic_events`) : c'est `EXPECTED` qui
  les porte, avec le jeton exact qui les prouve.

S'y ajoutent les invariants habituels de ce dépôt : idempotence (relançable sur
une base en service), RLS « deny by default » avec révocation, trigger
`updated_at` **seulement là où la colonne existe**, un index par requête et
aucun index sans requête, et la documentation dans le README.
"""
from __future__ import annotations

import pathlib
import re
import unittest
from typing import Dict, List, Set, Tuple

from tests import sql_columns

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "database" / "migrations"
README = REPO_ROOT / "README.md"

#: Fichier de migration qui porte le DDL de chaque table.
MIGRATION_OF: Dict[str, pathlib.Path] = {
    "economic_events": MIGRATIONS / "005_forex_factory.sql",
    "macro_bias_logs": MIGRATIONS / "005_forex_factory.sql",
    "trade_post_mortems": MIGRATIONS / "006_adaptive_learning.sql",
    "adaptive_model_weights": MIGRATIONS / "006_adaptive_learning.sql",
}
TABLES = tuple(MIGRATION_OF)

#: Colonnes qui n'existent que par convention (défaut `now()`, clé technique) :
#: aucune requête ne les lit, et c'est normal.
CONVENTION = "#convention"

#: Journal en **ajout seul** : une ligne n'est jamais modifiée, donc pas
#: d'`updated_at` — et pas de trigger `updated_at` non plus.
APPEND_ONLY = ("macro_bias_logs", "trade_post_mortems")

#: Colonnes attendues, chacune **justifiée** : soit par un jeton présent dans un
#: fichier qui l'écrit ou la lit, soit par la convention `created_at`/`id`.
EXPECTED: Dict[str, Dict[str, Tuple[str, str]]] = {
    "economic_events": {
        "id": ("scrapers/forex_factory.py", '"id": f"ff_{event_hash}"'),
        "event_id": ("scrapers/forex_factory.py", '"event_id": event_id'),
        "title": ("scrapers/forex_factory.py", '"title": title'),
        "country": ("scrapers/forex_factory.py", '"country": item.get("country", currency)'),
        "currency": ("scrapers/forex_factory.py", '"currency": currency'),
        "event_date": ("database/supabase_client.py", 'order("event_date", desc=True)'),
        "impact": ("scrapers/forex_factory.py", '"impact": impact'),
        "forecast": ("scrapers/forex_factory.py", '"forecast": forecast_str or None'),
        "previous": ("scrapers/forex_factory.py", '"previous": previous_str or None'),
        "actual": ("scrapers/forex_factory.py", '"actual": actual_str or None'),
        "forecast_num": ("scrapers/forex_factory.py", '"forecast_num": parse_numeric_value('),
        "previous_num": ("scrapers/forex_factory.py", '"previous_num": parse_numeric_value('),
        "actual_num": ("scrapers/forex_factory.py", '"actual_num": parse_numeric_value('),
        "raw_data": ("scrapers/forex_factory.py", '"raw_data": item'),
        "created_at": (CONVENTION, "défaut `now()`"),
        "updated_at": ("database/supabase_client.py", 'e["updated_at"] = now_str'),
    },
    "macro_bias_logs": {
        "id": (CONVENTION, "clé technique : jamais lue ni comparée par le code"),
        "symbol": ("database/supabase_client.py", '"symbol": symbol'),
        "currency": ("database/supabase_client.py", '"currency": currency'),
        "macro_score": ("database/supabase_client.py", '"macro_score": round(macro_score, 4)'),
        "news_risk_level": (
            "database/supabase_client.py",
            '"news_risk_level": news_risk_level',
        ),
        "decision": ("database/supabase_client.py", '"decision": decision'),
        "reasoning": ("database/supabase_client.py", '"reasoning": reasoning'),
        "created_at": ("database/supabase_client.py", '"created_at": datetime.now(timezone.utc)'),
    },
    "trade_post_mortems": {
        "id": (CONVENTION, "clé technique : jamais lue ni comparée par le code"),
        "signal_id": ("core/adaptive_learning.py", '"signal_id": sig_id'),
        "asset": ("core/adaptive_learning.py", '"asset": asset'),
        "direction": ("core/adaptive_learning.py", '"direction": direction'),
        "outcome": ("core/adaptive_learning.py", '"outcome": outcome'),
        "entry_price": ("core/adaptive_learning.py", '"entry_price": signal_data.get("price")'),
        "exit_price": ("core/adaptive_learning.py", '"exit_price": exit_price'),
        "pnl": ("core/adaptive_learning.py", '"pnl": signal_data.get("pnl", 0.0)'),
        "ta_score": ("core/adaptive_learning.py", '"ta_score": round(ta_score, 4)'),
        "sentiment_score": ("core/adaptive_learning.py", '"sentiment_score": round(sentiment_score, 4)'),
        "macro_score": ("core/adaptive_learning.py", '"macro_score": round(macro_score, 4)'),
        "confidence": ("core/adaptive_learning.py", '"confidence": signal_data.get("confidence")'),
        "error_type": ("core/adaptive_learning.py", '"error_type": post_mortem["error_type"]'),
        "learned_lesson": ("core/adaptive_learning.py", '"learned_lesson": post_mortem["lesson"]'),
        "created_at": ("core/adaptive_learning.py", 'order("created_at", desc=True)'),
    },
    "adaptive_model_weights": {
        "asset": ("core/adaptive_learning.py", '"asset": asset'),
        "ta_weight": ("core/adaptive_learning.py", '"ta_weight": round(ta_w, 3)'),
        "sentiment_weight": ("core/adaptive_learning.py", '"sentiment_weight": round(sent_w, 3)'),
        "macro_weight": ("core/adaptive_learning.py", '"macro_weight": round(macro_w, 3)'),
        "sl_atr_multiplier": ("core/adaptive_learning.py", '"sl_atr_multiplier": round(sl_mult, 2)'),
        "tp_atr_multiplier": ("core/adaptive_learning.py", '"tp_atr_multiplier": 3.0'),
        "consecutive_losses": ("core/adaptive_learning.py", '"consecutive_losses": consec_losses'),
        "win_rate_pct": ("core/adaptive_learning.py", '"win_rate_pct": round(new_win_rate, 1)'),
        "total_trades": ("core/adaptive_learning.py", '"total_trades": tot_trades'),
        "updated_at": ("core/adaptive_learning.py", '"updated_at": datetime.now(timezone.utc)'),
    },
}

#: Index attendus : nom → colonnes qui doivent apparaître dans sa définition.
#: Chacun sert une requête citée dans la migration qui le pose.
INDEXES: Dict[str, str] = {
    "idx_economic_events_currency_date": "currency, event_date",
    "idx_economic_events_impact": "impact",
    "idx_economic_events_event_date": "event_date desc",
    "idx_trade_post_mortems_asset_created_at": "asset, created_at desc",
    "idx_trade_post_mortems_created_at": "created_at desc",
}

#: Index retirés : aucune requête du dépôt ne filtre sur leur colonne.
DROPPED_INDEXES = ("idx_macro_bias_logs_symbol", "idx_trade_post_mortems_asset")

#: Colonne retirée : aucun chemin de code ne l'écrit ni ne la lit (le seuil de
#: confiance est calculé dans `core/adaptive_learning.py`).
RETIRED_COLUMN = ("adaptive_model_weights", "min_confidence_threshold")


def sql(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def text_for(table: str) -> str:
    return sql(MIGRATION_OF[table])


def declared_columns() -> Dict[str, Set[str]]:
    """Colonnes déclarées dans le `create table` de chaque table, tous fichiers confondus."""
    declared: Dict[str, Set[str]] = {}
    for table, path in MIGRATION_OF.items():
        declared.update(sql_columns.declared_columns(path, (table,)))
    return declared


def columns_used_by_code() -> Dict[str, Dict[str, Set[str]]]:
    """Colonnes touchées par le code, par table et par fichier (extraction réelle)."""
    return sql_columns.columns_used_by_code(TABLES)


def unterminated_statement(text: str) -> List[str]:
    """L'instruction restée ouverte, s'il y en a une.

    Un point-virgule oublié ferait avaler la suite du fichier à Postgres : une
    instruction suivante qui **démarre** alors que la précédente n'est pas close
    (et qu'on n'est pas dans une parenthèse) est exactement le symptôme.
    """
    begins = re.compile(
        r"^(create|alter|drop|revoke|grant|insert|update|delete|select|do|comment)\b",
        re.IGNORECASE,
    )
    depth = 0
    quoted = 0  # `$$` : le corps d'un bloc `do` n'est pas du SQL de premier niveau
    pending: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        quoted += stripped.count("$$")
        depth += sum(line.count(char) for char in "([")
        depth -= sum(line.count(char) for char in ")]")
        inside_block = quoted % 2 == 1
        if pending and depth == 0 and not inside_block and begins.match(stripped):
            return pending
        pending.append(stripped)
        if depth <= 0 and not inside_block and stripped.endswith(";"):
            pending = []
    return pending


class DdlCoversTheCodeColumnsTest(unittest.TestCase):
    """Aucune requête du code ne doit viser une colonne absente du DDL."""

    def test_every_column_used_by_a_query_is_declared(self):
        declared = declared_columns()
        used = columns_used_by_code()
        missing = {}
        for table, by_file in used.items():
            unknown = set().union(*by_file.values()) - declared[table] if by_file else set()
            if unknown:
                missing[table] = {
                    name: [path for path, cols in by_file.items() if name in cols]
                    for name in sorted(unknown)
                }
        self.assertEqual(missing, {}, f"colonnes utilisées mais non déclarées : {missing}")

    def test_the_scan_actually_sees_the_queries(self):
        """Un extracteur qui ne trouve rien ferait passer le test précédent à vide."""
        used = columns_used_by_code()
        for table in TABLES:
            with self.subTest(table=table):
                self.assertTrue(used[table], f"aucune requête reconnue sur {table}")
        extracted = {
            table: set().union(*by_file.values()) for table, by_file in used.items()
        }
        self.assertIn("updated_at", extracted["economic_events"])
        self.assertIn("event_date", extracted["economic_events"])
        self.assertIn("news_risk_level", extracted["macro_bias_logs"])
        self.assertIn("learned_lesson", extracted["trade_post_mortems"])
        self.assertIn("win_rate_pct", extracted["adaptive_model_weights"])

    def test_the_scan_follows_the_application_writer(self):
        """La sonde écrit `raw_data` **via** `upsert_economic_events()`.

        La requête est dans `database/supabase_client.py`, la charge dans
        `scripts/check_supabase.py` : sans ce suivi, ces colonnes n'étaient
        attribuées à **aucune** écriture, et une faute de frappe dans la charge de
        la sonde serait passée inaperçue — `upsert_economic_events` avale ses
        erreurs et rend 0.
        """
        probe = columns_used_by_code()["economic_events"].get(
            "scripts/check_supabase.py", set()
        )
        for column in ("raw_data", "title", "country", "impact", "forecast_num"):
            with self.subTest(column=column):
                self.assertIn(column, probe)

    def test_the_tables_do_not_borrow_each_others_columns(self):
        """`economic_events` ne doit pas hériter des colonnes de `trade_post_mortems`."""
        used = columns_used_by_code()
        extracted = {
            table: set().union(*by_file.values()) for table, by_file in used.items()
        }
        self.assertNotIn("learned_lesson", extracted["economic_events"])
        self.assertNotIn("impact", extracted["adaptive_model_weights"])
        self.assertNotIn("win_rate_pct", extracted["macro_bias_logs"])
        self.assertNotIn("symbol", extracted["trade_post_mortems"])

    def test_every_table_is_one_the_runtime_check_expects(self):
        """Le script de vérification doit chercher ces tables, sinon il n'en dit rien."""
        from database.supabase_client import REQUIRED_TABLES

        for table in TABLES:
            with self.subTest(table=table):
                self.assertIn(table, REQUIRED_TABLES)


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

    def test_the_retired_column_is_neither_declared_nor_used(self):
        """`min_confidence_threshold` n'est lu par aucun chemin de code."""
        table, column = RETIRED_COLUMN
        self.assertNotIn(column, declared_columns()[table])
        self.assertIn(f"drop column if exists {column}", text_for(table))
        used = set().union(*columns_used_by_code()[table].values())
        self.assertNotIn(column, used)

    def test_the_append_only_tables_carry_no_updated_at(self):
        """Une ligne jamais modifiée n'a pas d'`updated_at`, donc pas de trigger."""
        declared = declared_columns()
        for table in APPEND_ONLY:
            with self.subTest(table=table):
                self.assertNotIn("updated_at", declared[table])
                self.assertNotIn(f"trg_{table}_updated_at", text_for(table))

    def test_the_mutated_tables_carry_both_the_column_and_its_trigger(self):
        for table in TABLES:
            if table in APPEND_ONLY:
                continue
            with self.subTest(table=table):
                self.assertIn("updated_at", declared_columns()[table])
                self.assertIn(
                    f"drop trigger if exists trg_{table}_updated_at on {table}",
                    text_for(table),
                )
                self.assertIn(f"create trigger trg_{table}_updated_at", text_for(table))

    def test_the_result_vocabulary_is_documented(self):
        """Le vocabulaire est verrouillé ici, faute de contrainte `check` en SQL."""
        text = text_for("trade_post_mortems")
        for value in ("won", "lost", "macro_divergence", "overbought_entry", "tight_sl"):
            with self.subTest(value=value):
                self.assertIn(value, text)

    def test_no_existing_column_type_is_changed(self):
        """`alter column … type` réécrit une table en service : à faire à part."""
        for path in set(MIGRATION_OF.values()):
            with self.subTest(migration=path.name):
                statements = [
                    line.split("--")[0].lower()
                    for line in sql(path).splitlines()
                    if not line.strip().startswith("--")
                ]
                self.assertNotIn("alter column", "\n".join(statements))


class MigrationContractTest(unittest.TestCase):
    """Idempotence, RLS, triggers, index — et documentation."""

    def test_the_migrations_are_documented_in_the_readme(self):
        readme = README.read_text(encoding="utf-8")
        for path in sorted(set(MIGRATION_OF.values())):
            with self.subTest(migration=path.name):
                self.assertIn(path.name, readme)

    def test_they_can_be_applied_to_a_database_already_in_service(self):
        for path in sorted(set(MIGRATION_OF.values())):
            text = sql(path)
            with self.subTest(migration=path.name):
                self.assertIn("create table if not exists", text)
                self.assertIn("add column if not exists", text)
                self.assertIn("create index if not exists", text)

    def test_every_table_can_be_re_run_safely(self):
        for table, path in MIGRATION_OF.items():
            text = sql(path)
            with self.subTest(table=table):
                self.assertIn(f"create table if not exists {table} ", text)
                self.assertIn(f"alter table if exists {table}", text)

    def test_deny_by_default(self):
        for table, path in MIGRATION_OF.items():
            text = sql(path)
            with self.subTest(table=table):
                self.assertRegex(
                    text, rf"alter table if exists {table}\s+enable row level security"
                )
                self.assertIn(f"revoke all on {table} from anon, authenticated;", text)

    def test_no_policy_is_created(self):
        """Deny by default : une policy ouvrirait la table aux clés publiques."""
        for path in set(MIGRATION_OF.values()):
            with self.subTest(migration=path.name):
                self.assertNotIn("create policy", sql(path).lower())

    def test_every_statement_is_terminated(self):
        for path in sorted(set(MIGRATION_OF.values())):
            with self.subTest(migration=path.name):
                pending = unterminated_statement(sql(path))
                self.assertEqual(pending, [], f"instruction non terminée dans {path.name}")

    def test_each_index_serves_a_query_of_the_code(self):
        """Un index sans requête est un coût d'écriture, pas une optimisation."""
        texts = {path.name: sql(path) for path in set(MIGRATION_OF.values())}
        for index, columns in INDEXES.items():
            with self.subTest(index=index):
                holder = next(
                    (text for text in texts.values() if f"create index if not exists {index}" in text),
                    None,
                )
                self.assertIsNotNone(holder, f"`{index}` n'est créé par aucune migration")
                block = holder.split(f"create index if not exists {index}", 1)[1].split(";", 1)[0]
                self.assertIn(columns, block)

    def test_no_index_is_left_without_a_query(self):
        """Les index que plus aucune requête ne justifie sont retirés, pas laissés."""
        texts = {path.name: sql(path) for path in set(MIGRATION_OF.values())}
        for index in DROPPED_INDEXES:
            with self.subTest(index=index):
                holders = [
                    name for name, text in texts.items() if f"drop index if exists {index};" in text
                ]
                self.assertTrue(holders, f"`{index}` n'est retiré par aucune migration")
                for name, text in texts.items():
                    # `\b` distingue `idx_x` de `idx_x_created_at` : le nom d'un
                    # index remplaçant commence par celui de l'index retiré.
                    self.assertNotRegex(
                        text,
                        rf"create index if not exists {re.escape(index)}\b",
                        f"`{index}` est retiré puis recréé dans {name}",
                    )
        # `macro_bias_logs` n'est qu'écrite : aucun index ne peut y servir une requête.
        self.assertNotRegex(
            text_for("macro_bias_logs"),
            r"create index if not exists \S+[^;]*on macro_bias_logs",
        )


if __name__ == "__main__":
    unittest.main()
