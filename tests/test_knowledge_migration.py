"""Contrat de la migration `009_knowledge_vectors.sql`.

Comme pour la 008, une migration ne se compile pas ici : elle est exécutée à la
main dans Supabase. Ces tests lisent le fichier et verrouillent ce qui, s'il
manquait, casserait la recherche vectorielle en silence :

* `media_id` devient nullable (sinon les notes ne peuvent pas être indexées) ;
* les colonnes de filtre (`source`, `note_source`, `asset`, `regime`,
  `embedding_model`) et l'index associé existent ;
* la fonction `match_knowledge_chunks` filtre bien sur la source, l'actif et le
  régime, ordonne par distance cosinus `operator(extensions.<=>)` (qualifié, voir
  le contrat de tri) et n'oublie pas les vecteurs nuls ;
* le filtre d'actif reste **préférentiel** (un morceau non étiqueté reste
  candidat, un morceau étiqueté d'un autre actif est exclu, et la correspondance
  explicite passe devant) : c'est ce contrat qui donne son sens à l'étiquetage
  des médias (`notifications/telegram_media.tag_media`) ;
* la migration est idempotente et respecte le « deny by default ».
"""
from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQL_PATH = REPO_ROOT / "database" / "migrations" / "009_knowledge_vectors.sql"


class KnowledgeVectorsMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SQL_PATH.read_text(encoding="utf-8")
        self.lower = re.sub(r"[ \t]+", " ", self.text.lower())

    def test_media_id_becomes_nullable(self) -> None:
        self.assertIn("alter column media_id drop not null", self.lower)

    def test_filter_columns_are_added_idempotently(self) -> None:
        for column in ("source", "note_source", "asset", "regime", "embedding_model"):
            with self.subTest(column=column):
                self.assertIn(f"add column if not exists {column}", self.lower)

    def test_filter_index_exists(self) -> None:
        self.assertIn("idx_knowledge_chunks_filters", self.lower)
        self.assertRegex(self.lower, r"on knowledge_chunks \(source, asset, regime\)")

    def test_match_function_is_created_with_the_vector_dimension(self) -> None:
        self.assertIn("create or replace function match_knowledge_chunks", self.lower)
        self.assertIn("query_embedding extensions.vector(768)", self.lower)

    def test_match_function_filters_on_source_asset_and_regime(self) -> None:
        for placeholder in ("filter_source", "filter_asset", "filter_regime"):
            with self.subTest(placeholder=placeholder):
                self.assertIn(placeholder, self.lower)
        self.assertIn("kc.embedding is not null", self.lower)

    def test_the_asset_filter_stays_preferential(self) -> None:
        """Le contrat dont dépend l'étiquetage des médias.

        Un morceau **non étiqueté** reste candidat (NULL = joker), un morceau
        étiqueté d'un **autre** actif est exclu. Écrire `kc.asset = filter_asset`
        sans les `is null` ferait disparaître en silence tous les médias jamais
        étiquetés de chaque recherche filtrée.
        """
        self.assertIn(
            "and (filter_asset is null or kc.asset is null or kc.asset = filter_asset)",
            self.lower,
        )

    def test_an_explicit_asset_match_wins_the_ordering(self) -> None:
        """Le « bonus » de classement : sans lui, étiqueter ne changerait rien."""
        self.assertIn("(kc.asset = filter_asset) desc nulls last", self.lower)

    def test_match_function_orders_by_cosine_distance(self) -> None:
        """Le tri final est bien la distance cosinus — et l'opérateur est qualifié.

        Le `<=>` est écrit `operator(extensions.<=>)`, jamais nu : le corps d'une
        fonction `language sql` est analysé **à sa création**, donc un opérateur
        non qualifié serait résolu selon le `search_path` de la session qui
        applique le fichier. Présent sur Supabase (`public, extensions`), absent
        d'un PostgreSQL nu (`"$user", public`) — et la migration y échouerait sur
        « operator does not exist: extensions.vector <=> extensions.vector ».
        C'est un défaut qui ne se voit que sur une base réelle : ce test le
        verrouille dans les deux occurrences (la similarité rendue et le tri).
        """
        # Les deux occurrences — la similarité rendue et le tri — portent la
        # qualification : le compte est ce qui distingue « l'une des deux a été
        # oubliée » de « c'est fait ».
        self.assertEqual(
            self.lower.count("kc.embedding operator(extensions.<=>) query_embedding"), 2
        )
        self.assertNotIn("kc.embedding <=>", self.lower)
        self.assertIn("order by", self.lower)
        self.assertIn("limit greatest(match_count, 1)", self.lower)

    def test_public_roles_cannot_execute_the_function(self) -> None:
        self.assertIn(
            "revoke execute on function match_knowledge_chunks from anon, authenticated",
            self.lower,
        )


if __name__ == "__main__":
    unittest.main()
