"""Contrat de la migration `008_telegram_media.sql`.

Une migration SQL ne se compile pas dans ce dépôt : elle est exécutée à la main
dans l'éditeur SQL Supabase, donc une erreur ne se voit qu'au moment où on
l'applique — c'est-à-dire trop tard. Ces tests lisent le fichier et verrouillent
ce qui, s'il manquait, casserait la fonctionnalité en silence :

* le bucket est bien créé, et **privé** ;
* les deux tables existent, `knowledge_chunks` porte un `extensions.vector` et
  une clé étrangère en `on delete cascade` (sinon supprimer un média laisserait
  ses morceaux derrière lui) ;
* la migration est **idempotente** (`if not exists`, `on conflict … do nothing`,
  `drop trigger if exists`) : elle doit pouvoir être relancée sans erreur ;
* la RLS est activée et les privilèges par défaut révoqués, comme le veut le
  principe « deny by default » de la migration 007.

On vérifie aussi l'invariant de nommage des migrations (numéro à 3 chiffres,
unique, croissant) : un doublon de numéro rendrait l'ordre d'application
ambigu.
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "database" / "migrations"
MEDIA_SQL = MIGRATIONS / "008_telegram_media.sql"
README = REPO_ROOT / "README.md"

BUCKET = "telegram-media"
VECTOR_DIM = 768


def sql() -> str:
    return MEDIA_SQL.read_text(encoding="utf-8")


class MigrationFilesTest(unittest.TestCase):
    def test_files_are_numbered_consistently(self) -> None:
        found = sorted(MIGRATIONS.glob("*.sql"))
        self.assertGreaterEqual(len(found), 8, f"migrations lues : {[p.name for p in found]}")
        numbers = []
        for path in found:
            match = re.fullmatch(r"(\d{3})_[a-z0-9_]+\.sql", path.name)
            self.assertIsNotNone(match, f"nom de migration inattendu : {path.name}")
            numbers.append(int(match.group(1)))
        self.assertEqual(len(numbers), len(set(numbers)), f"numéros de migration dupliqués : {numbers}")
        self.assertEqual(numbers, sorted(numbers), "les migrations doivent être ordonnées par numéro")

    def test_the_media_migration_is_documented_in_the_readme(self) -> None:
        """Une migration non documentée est une migration qu'on oublie d'appliquer."""
        self.assertIn(MEDIA_SQL.name, README.read_text(encoding="utf-8"))


class MediaMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = sql()
        # Le SQL aligne ses colonnes à coups d'espaces : on normalise les espaces
        # horizontaux pour que les assertions ne dépendent pas de cet
        # alignement purement cosmétique (une double espace casserait sinon un
        # test qui, lui, ne vérifie pas la mise en forme).
        self.lower = re.sub(r"[ \t]+", " ", self.text.lower())

    def test_enables_pgvector_in_the_extensions_schema(self) -> None:
        """Supabase installe ses extensions dans `extensions`."""
        self.assertIn("create extension if not exists vector with schema extensions", self.lower)

    def test_vector_column_is_schema_qualified(self) -> None:
        """Sans qualification, le type dépend du `search_path` de la session."""
        self.assertIn(f"embedding extensions.vector({VECTOR_DIM})", self.lower)

    def test_creates_a_private_storage_bucket_idempotently(self) -> None:
        self.assertIn("insert into storage.buckets", self.lower)
        self.assertIn(f"'{BUCKET}'", self.lower)
        self.assertRegex(
            self.lower,
            r"values\s*\(\s*'telegram-media'\s*,\s*'telegram-media'\s*,\s*false\s*\)",
            "le bucket doit être créé en `public = false`",
        )
        self.assertIn("on conflict (id) do nothing", self.lower)

    def test_creates_both_tables_idempotently(self) -> None:
        self.assertIn("create table if not exists knowledge_media", self.lower)
        self.assertIn("create table if not exists knowledge_chunks", self.lower)

    def test_chunks_cascade_on_media_deletion(self) -> None:
        self.assertRegex(
            self.lower,
            r"references\s+knowledge_media\s*\(\s*id\s*\)\s*on delete cascade",
            "supprimer un média doit supprimer ses morceaux",
        )

    def test_chunks_are_unique_per_media_and_index(self) -> None:
        self.assertRegex(
            self.lower,
            r"unique\s*\(\s*media_id\s*,\s*chunk_index\s*\)",
            "rejouer la découpe d'un média doit rester idempotent",
        )

    def test_media_path_is_unique(self) -> None:
        self.assertRegex(
            self.lower,
            r"storage_path text not null unique",
            "deux lignes ne doivent pas pointer sur le même objet",
        )

    def test_updated_at_trigger_is_recreated_safely(self) -> None:
        self.assertIn("drop trigger if exists trg_knowledge_media_updated_at", self.lower)
        self.assertIn("execute function update_updated_at()", self.lower)

    def test_vector_similarity_index_exists(self) -> None:
        """Sans index, la recherche sémantique fera un parcours complet."""
        self.assertRegex(self.lower, r"using hnsw\s*\(\s*embedding extensions\.vector_cosine_ops")

    def test_row_level_security_is_enabled_and_default_grants_revoked(self) -> None:
        for table in ("knowledge_media", "knowledge_chunks"):
            with self.subTest(table=table):
                self.assertIn(
                    f"alter table if exists {table} enable row level security", self.lower
                )
                self.assertIn(f"revoke all on {table} from anon, authenticated", self.lower)


if __name__ == "__main__":
    unittest.main()
