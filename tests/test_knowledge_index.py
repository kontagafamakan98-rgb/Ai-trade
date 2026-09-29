"""Tests de l'index texte des médias (`database/knowledge_index.py`).

On teste le découpage (bornes respectées, mots non coupés, texte vide) et
l'écriture dans `knowledge_chunks` sur la doublure **partagée** du dossier de
tests (`tests/supabase_double.py`) : la suppression précède toujours
l'insertion, ce qui rend un rejeu idempotent.

Cette doublure **persiste** les écritures et **applique les filtres** comme
PostgREST : une ligne semée sans la colonne sur laquelle la lecture filtre ne
ressort pas. Les tests qui scriptent une lecture le font désormais en semant les
lignes (`client.store(...).rows = [...]`) — et de fait ils exercent la vraie
pagination (`.range()`), au lieu de servir des pages écrites d'avance.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ai import embeddings
from database import knowledge_index
from tests import supabase_double


class ChunkTextTest(unittest.TestCase):
    def test_empty_or_blank_returns_no_chunk(self):
        self.assertEqual(knowledge_index.chunk_text(""), [])
        self.assertEqual(knowledge_index.chunk_text("   \n  "), [])

    def test_short_text_is_a_single_chunk(self):
        self.assertEqual(knowledge_index.chunk_text("bonjour le monde"), ["bonjour le monde"])

    def test_whitespace_is_normalized(self):
        self.assertEqual(knowledge_index.chunk_text("a\n\n\t b"), ["a b"])

    def test_long_text_splits_within_limit_without_cutting_words(self):
        words = [f"mot{i}" for i in range(500)]
        chunks = knowledge_index.chunk_text(" ".join(words), max_chars=100, overlap=20)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 100)
            for word in chunk.split():
                self.assertIn(word, words)  # aucun mot tronqué

        # Le texte est couvert de bout en bout.
        self.assertTrue(chunks[0].startswith("mot0"))
        self.assertTrue(chunks[-1].endswith("mot499"))

    def test_zero_max_chars_is_rejected(self):
        with self.assertRaises(ValueError):
            knowledge_index.chunk_text("x", max_chars=0)


class ReplaceChunksTest(unittest.TestCase):
    """L'écriture des morceaux : suppression puis insertion, et rien d'autre."""

    def setUp(self) -> None:
        client = supabase_double.SupabaseDouble()
        self.table = client.store(knowledge_index.TABLE)
        #: Le journal du client, où les tests cherchent les appels émis.
        self.calls = client.calls
        supabase_double.use_supabase(self, client, knowledge_index)

    def test_deletes_then_inserts_chunks(self):
        count = knowledge_index.replace_chunks("media-1", "texte utile " * 20)
        self.assertEqual(count, 1)
        self.assertEqual(self.calls[0], ("delete",))
        self.assertIn(("eq", "media_id", "media-1"), self.calls)
        inserted = [c for c in self.calls if c[0] == "insert"][0][1]
        self.assertEqual(inserted[0]["media_id"], "media-1")
        self.assertEqual(inserted[0]["chunk_index"], 0)
        self.assertIn("token_count", inserted[0])

    def test_empty_text_deletes_without_inserting(self):
        count = knowledge_index.replace_chunks("m", "")
        self.assertEqual(count, 0)
        self.assertNotIn("insert", [c[0] for c in self.calls])

    def test_unconfigured_client_fails_loudly(self):
        with mock.patch.object(knowledge_index, "supabase", None):
            with self.assertRaises(RuntimeError):
                knowledge_index.replace_chunks("m", "x")

    def test_list_chunks_orders_by_index(self):
        # La lecture filtre sur `media_id` : les lignes semées le portent.
        self.table.rows = [
            {"media_id": "m", "chunk_index": 0},
            {"media_id": "m", "chunk_index": 1},
        ]
        rows = knowledge_index.list_chunks("m")
        self.assertEqual(len(rows), 2)
        self.assertIn(("order", "chunk_index", False), self.calls)

    def test_list_chunks_projects_the_requested_columns(self):
        """La projection doit partir **avec** la requête, pas être filtrée après.

        `select("*")` ramène l'`embedding` de chaque morceau : 768 flottants qui
        traversent le réseau depuis Postgres avant d'être jetés par l'afficheur.
        """
        knowledge_index.list_chunks("m", columns=("chunk_index", "content"))
        selection = [call for call in self.calls if call[0] == "select"][0][1][0]
        self.assertEqual(selection, "chunk_index,content")

    def test_list_chunks_reads_every_column_by_default(self):
        """Sans projection demandée, le comportement d'origine est conservé."""
        knowledge_index.list_chunks("m")
        selection = [call for call in self.calls if call[0] == "select"][0][1][0]
        self.assertEqual(selection, "*")

    def test_note_document_is_deleted_by_note_source(self):
        count = knowledge_index.replace_chunks(
            None, "règle", note_source="obsidian:regles", source=knowledge_index.SOURCE_NOTE
        )
        self.assertEqual(count, 1)
        self.assertIn(("eq", "note_source", "obsidian:regles"), self.calls)
        inserted = [c for c in self.calls if c[0] == "insert"][0][1]
        self.assertIsNone(inserted[0]["media_id"])
        self.assertEqual(inserted[0]["note_source"], "obsidian:regles")
        self.assertEqual(inserted[0]["source"], knowledge_index.SOURCE_NOTE)

    def test_identity_is_required(self):
        with self.assertRaises(ValueError):
            knowledge_index.replace_chunks(None, "texte")

    def test_embeddings_are_stored_when_available(self):
        vector = [0.1] * 768
        with mock.patch.object(
            knowledge_index,
            "_embed_passages",
            lambda chunks: ([vector], "gemini-embedding-001"),
        ):
            knowledge_index.replace_chunks("media-1", "texte")
        row = [c for c in self.calls if c[0] == "insert"][0][1][0]
        self.assertEqual(row["embedding"], vector)
        self.assertEqual(row["embedding_model"], "gemini-embedding-001")

    def test_no_embedding_leaves_model_absent(self):
        with mock.patch.object(knowledge_index, "_embed_passages", lambda chunks: (None, None)):
            knowledge_index.replace_chunks("media-1", "texte")
        row = [c for c in self.calls if c[0] == "insert"][0][1][0]
        self.assertNotIn("embedding", row)


class DeleteChunksTest(unittest.TestCase):
    """Retirer de l'index l'extraction d'un média mal lu.

    Le compte est ce que l'utilisateur voit après un rejet (« 3 morceaux
    retirés ») : il impose une lecture préalable, et une suppression qui ne
    compterait pas laisserait croire que le clic n'a rien fait.
    """

    def setUp(self) -> None:
        client = supabase_double.SupabaseDouble()
        self.table = client.store(knowledge_index.TABLE)
        #: Le journal du client, où les tests cherchent les appels émis.
        self.calls = client.calls
        supabase_double.use_supabase(self, client, knowledge_index)

    def test_returns_the_number_of_removed_chunks(self):
        self.table.rows = [
            {"id": "c1", "media_id": "m1"},
            {"id": "c2", "media_id": "m1"},
            {"id": "c3", "media_id": "m1"},
        ]
        self.assertEqual(knowledge_index.delete_chunks("m1"), 3)
        self.assertIn(("delete",), self.calls)
        self.assertIn(("eq", "media_id", "m1"), self.calls)

    def test_a_media_without_chunks_is_not_deleted_at_all(self):
        """Rejeter deux fois ne doit pas relancer une suppression inutile."""
        self.table.rows = []
        self.assertEqual(knowledge_index.delete_chunks("m1"), 0)
        self.assertNotIn(("delete",), self.calls)

    def test_the_deletion_is_scoped_to_one_media(self):
        """Sans le filtre, un rejet viderait l'index entier."""
        self.table.rows = [{"id": "c1", "media_id": "m1"}]
        knowledge_index.delete_chunks("m1")
        filters = {call for call in self.calls if call[0] == "eq"}
        self.assertEqual(filters, {("eq", "media_id", "m1")})
        index = self.calls.index(("delete",))
        self.assertIn(("eq", "media_id", "m1"), self.calls[index:], "filtre absent du delete")

    def test_unconfigured_client_fails_loudly(self):
        with mock.patch.object(knowledge_index, "supabase", None):
            with self.assertRaises(RuntimeError):
                knowledge_index.delete_chunks("m1")


class SetChunksAssetTest(unittest.TestCase):
    """Réétiqueter les morceaux d'un média : le filtre SQL joue sans réindexation."""

    def setUp(self) -> None:
        client = supabase_double.SupabaseDouble()
        self.table = client.store(knowledge_index.TABLE)
        #: Le journal du client, où les tests cherchent les appels émis.
        self.calls = client.calls
        supabase_double.use_supabase(self, client, knowledge_index)

    def test_the_asset_is_written_and_counted(self):
        self.table.rows = [
            {"id": "c1", "media_id": "m1"},
            {"id": "c2", "media_id": "m1"},
        ]
        self.assertEqual(knowledge_index.set_chunks_asset("m1", "BTC-USD"), 2)
        self.assertIn(("update", {"asset": "BTC-USD"}), self.calls)
        self.assertIn(("eq", "media_id", "m1"), self.calls)

    def test_nothing_indexed_means_no_write(self):
        self.table.rows = []
        self.assertEqual(knowledge_index.set_chunks_asset("m1", "BTC-USD"), 0)
        self.assertNotIn("update", [call[0] for call in self.calls])

    def test_none_restores_the_wildcard(self):
        """Le joker doit être réinscriptible : c'est la sortie d'une étiquette fausse."""
        self.table.rows = [{"id": "c1", "media_id": "m1"}]
        knowledge_index.set_chunks_asset("m1", None)
        self.assertIn(("update", {"asset": None}), self.calls)

    def test_the_update_is_scoped_to_one_media(self):
        self.table.rows = [{"id": "c1", "media_id": "m1"}]
        knowledge_index.set_chunks_asset("m1", "BTC-USD")
        index = self.calls.index(("update", {"asset": "BTC-USD"}))
        self.assertIn(("eq", "media_id", "m1"), self.calls[index:])

    def test_unconfigured_client_fails_loudly(self):
        with mock.patch.object(knowledge_index, "supabase", None):
            with self.assertRaises(RuntimeError):
                knowledge_index.set_chunks_asset("m1", "BTC-USD")


class SearchChunksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [{"content": "morceau", "similarity": 0.9}]
        client = supabase_double.SupabaseDouble()
        #: Un RPC sans gestionnaire rend ces lignes ; un nom inattendu échoue.
        client.rpc_rows = self.rows
        #: Le journal du client, où les tests cherchent les appels émis.
        self.calls = client.calls
        supabase_double.use_supabase(self, client, knowledge_index)

    def test_passes_filters_and_returns_rows(self):
        with mock.patch.object(knowledge_index, "_embed_query", lambda q: [0.0] * 768):
            rows = knowledge_index.search_chunks(
                "BTC", asset="BTC-USD", regime="TRENDING_BULL", source="telegram", top_k=3
            )
        self.assertEqual(rows, self.rows)
        _, name, params = self.calls[0]
        self.assertEqual(name, knowledge_index.MATCH_FUNCTION)
        self.assertEqual(params["filter_asset"], "BTC-USD")
        self.assertEqual(params["filter_regime"], "TRENDING_BULL")
        self.assertEqual(params["filter_source"], "telegram")
        self.assertEqual(params["match_count"], 3)

    def test_blank_query_returns_empty_without_rpc(self):
        self.assertEqual(knowledge_index.search_chunks("   "), [])
        self.assertEqual(self.calls, [])

    def test_without_embeddings_returns_empty(self):
        with mock.patch.object(knowledge_index, "_embed_query", lambda q: None):
            self.assertEqual(knowledge_index.search_chunks("x"), [])
        self.assertEqual(self.calls, [])


class SearchMediaForAssetTest(unittest.TestCase):
    """Recherche sémantique des médias d'un actif."""

    def setUp(self) -> None:
        self.chunks = [
            {"media_id": "m1", "content": "cassure BTC", "similarity": 0.81},
            {"media_id": "m2", "content": "range ETH", "similarity": 0.72},
            {"media_id": "m1", "content": "volume BTC", "similarity": 0.90},
        ]
        self.client = supabase_double.SupabaseDouble()
        self.client.rpc_rows = self.chunks
        self.client.store(knowledge_index.MEDIA_TABLE).rows = [
            {"id": "m1", "file_name": "btc.png"},
            {"id": "m2", "file_name": "eth.png"},
        ]
        supabase_double.use_supabase(self, self.client, knowledge_index)

    def _query_texts(self):
        seen: list = []

        def _recorder(query):
            seen.append(query)
            return [0.0] * 768

        return seen, _recorder

    def test_ranks_media_by_their_best_chunk(self):
        _, recorder = self._query_texts()
        with mock.patch.object(knowledge_index, "_embed_query", recorder):
            results = knowledge_index.search_media_for_asset("BTC")

        self.assertEqual([row["media_id"] for row in results], ["m1", "m2"])
        self.assertAlmostEqual(results[0]["similarity"], 0.90)
        self.assertEqual(results[0]["matched_chunk"], "volume BTC")
        self.assertEqual(results[0]["chunk_count"], 2)
        self.assertEqual(results[0]["file_name"], "btc.png")
        self.assertEqual(results[1]["chunk_count"], 1)

    def test_searches_media_chunks_for_the_requested_asset(self):
        _, recorder = self._query_texts()
        with mock.patch.object(knowledge_index, "_embed_query", recorder):
            knowledge_index.search_media_for_asset("BTC")

        _, name, params = self.client.rpc_calls()[0]
        self.assertEqual(name, knowledge_index.MATCH_FUNCTION)
        self.assertEqual(params["filter_asset"], "BTC")
        self.assertEqual(params["filter_source"], knowledge_index.SOURCE_MEDIA)
        self.assertGreaterEqual(params["match_count"], knowledge_index.DEFAULT_TOP_K * 3)

    def test_regime_is_passed_as_filter(self):
        _, recorder = self._query_texts()
        with mock.patch.object(knowledge_index, "_embed_query", recorder):
            knowledge_index.search_media_for_asset("BTC", regime="TRENDING_BULL")
        _, _name, params = self.client.rpc_calls()[0]
        self.assertEqual(params["filter_regime"], "TRENDING_BULL")

    def test_default_query_mentions_the_asset(self):
        seen, recorder = self._query_texts()
        with mock.patch.object(knowledge_index, "_embed_query", recorder):
            knowledge_index.search_media_for_asset("BTC-USD")
        self.assertEqual(len(seen), 1)
        self.assertIn("BTC-USD", seen[0])

    def test_explicit_query_replaces_the_default(self):
        seen, recorder = self._query_texts()
        with mock.patch.object(knowledge_index, "_embed_query", recorder):
            knowledge_index.search_media_for_asset("BTC", query="cassure des 100k")
        self.assertEqual(seen, ["cassure des 100k"])

    def test_blank_asset_returns_empty_without_rpc(self):
        self.assertEqual(knowledge_index.search_media_for_asset("   "), [])
        self.assertEqual(self.client.calls, [])

    def test_without_embeddings_returns_empty(self):
        with mock.patch.object(knowledge_index, "_embed_query", lambda query: None):
            self.assertEqual(knowledge_index.search_media_for_asset("BTC"), [])
        self.assertEqual(self.client.rpc_calls(), [])

    def test_unreadable_media_table_keeps_the_matches(self):
        self.client.store(knowledge_index.MEDIA_TABLE).read_error = RuntimeError(
            "media table down"
        )
        _, recorder = self._query_texts()
        with mock.patch.object(knowledge_index, "_embed_query", recorder):
            results = knowledge_index.search_media_for_asset("BTC")
        self.assertEqual([row["media_id"] for row in results], ["m1", "m2"])
        self.assertNotIn("file_name", results[0])

    def test_chunks_without_media_id_are_ignored(self):
        self.client.rpc_rows = self.chunks + [{"media_id": None, "content": "note"}]
        _, recorder = self._query_texts()
        with mock.patch.object(knowledge_index, "_embed_query", recorder):
            results = knowledge_index.search_media_for_asset("BTC")
        self.assertEqual(len(results), 2)

    def test_top_k_limits_the_number_of_media(self):
        _, recorder = self._query_texts()
        with mock.patch.object(knowledge_index, "_embed_query", recorder):
            results = knowledge_index.search_media_for_asset("BTC", top_k=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["media_id"], "m1")

    def test_min_similarity_drops_weak_matches(self):
        self.client.rpc_rows = [
            {"media_id": "m1", "content": "cassure BTC", "similarity": 0.81},
            {"media_id": "m2", "content": "range ETH", "similarity": 0.42},
        ]
        _, recorder = self._query_texts()
        with mock.patch.object(knowledge_index, "_embed_query", recorder):
            results = knowledge_index.search_media_for_asset("BTC", min_similarity=0.6)
        self.assertEqual([row["media_id"] for row in results], ["m1"])

    def test_min_similarity_can_return_nothing(self):
        _, recorder = self._query_texts()
        with mock.patch.object(knowledge_index, "_embed_query", recorder):
            results = knowledge_index.search_media_for_asset("BTC", min_similarity=0.99)
        self.assertEqual(results, [])

    def _tagged_case(self):
        """Un média étiqueté BTC, moins proche que le média non étiqueté."""
        self.client.rpc_rows = [
            {"media_id": "m1", "content": "range", "similarity": 0.72, "asset": None},
            {"media_id": "m2", "content": "photo", "similarity": 0.58, "asset": "BTC"},
        ]

    def _search(self, **kwargs):
        _, recorder = self._query_texts()
        with mock.patch.object(knowledge_index, "_embed_query", recorder):
            return knowledge_index.search_media_for_asset("BTC", **kwargs)

    def test_tagged_media_ranks_before_a_closer_untagged_one(self):
        """L'étiquette est un fait, la similarité une estimation : elle passe devant."""
        self._tagged_case()
        results = self._search()
        self.assertEqual([row["media_id"] for row in results], ["m2", "m1"])
        self.assertTrue(results[0]["tagged"])
        self.assertFalse(results[1]["tagged"])

    def test_tagged_media_survives_the_measured_floor(self):
        """Un média que l'utilisateur a désigné pour cet actif reste pertinent même
        sous le bruit du corpus : le seuil mesure l'absence de rapport, et
        l'étiquette affirme le rapport."""
        self._tagged_case()
        results = self._search(min_similarity=0.8)
        self.assertEqual([row["media_id"] for row in results], ["m2"])

    def test_untagged_media_is_still_filtered_by_the_floor(self):
        self._tagged_case()
        results = self._search(min_similarity=0.8)
        self.assertNotIn("m1", [row["media_id"] for row in results])

    def test_a_mistagged_media_below_the_hard_floor_is_dropped(self):
        """Même une étiquette explicite ne fait pas entrer un contenu sans aucun
        rapport avec l'actif (étiquette posée par erreur)."""
        self.client.rpc_rows = [
            {
                "media_id": "m1",
                "content": "recette de tarte",
                "similarity": knowledge_index.TAGGED_HARD_FLOOR - 0.01,
                "asset": "BTC",
            }
        ]
        self.assertEqual(self._search(min_similarity=0.7), [])

    def test_tagged_media_is_kept_below_the_hard_floor_only_when_it_is_relevant(self):
        self.client.rpc_rows = [
            {
                "media_id": "m1",
                "content": "niveaux BTC",
                "similarity": knowledge_index.TAGGED_HARD_FLOOR,
                "asset": "BTC",
            }
        ]
        self.assertEqual([row["media_id"] for row in self._search(min_similarity=0.9)], ["m1"])

    def test_an_untagged_chunk_does_not_cancel_the_tag_of_its_media(self):
        """L'étiquette s'applique au média entier : un morceau resté sans actif ne
        doit pas annuler ceux qui en portent un."""
        self.client.rpc_rows = [
            {"media_id": "m1", "content": "a", "similarity": 0.55, "asset": None},
            {"media_id": "m1", "content": "b", "similarity": 0.50, "asset": "BTC"},
        ]
        results = self._search(min_similarity=0.7)
        self.assertEqual([row["media_id"] for row in results], ["m1"])
        self.assertTrue(results[0]["tagged"])

    def test_a_tag_for_another_asset_does_not_count(self):
        self.client.rpc_rows = [
            {"media_id": "m1", "content": "range ETH", "similarity": 0.55, "asset": "ETH"},
        ]
        self.assertEqual(self._search(min_similarity=0.7), [])


class HasChunksTest(unittest.TestCase):
    """`has_chunks` : « ce média est-il déjà indexé ? » — sans tout relire.

    C'est la garde des balayages répétés : le scraper revoit les mêmes
    publications à chaque passage, et il ne doit pas relancer une extraction
    (vision, Whisper) pour un texte déjà en base.
    """

    def _client(self, rows=()):
        """La table des morceaux de la doublure, semée de ces lignes."""
        client = supabase_double.SupabaseDouble()
        client.store(knowledge_index.TABLE).rows = [dict(row) for row in rows]
        supabase_double.use_supabase(self, client, knowledge_index)
        return client

    def test_a_media_with_chunks_is_recognized(self):
        client = self._client([{"id": 1, "media_id": "m1"}])
        self.assertTrue(knowledge_index.has_chunks("m1"))
        self.assertIn(("eq", "media_id", "m1"), client.calls)
        self.assertIn(("limit", 1), client.calls)

    def test_a_media_without_chunks_is_not(self):
        self._client()
        self.assertFalse(knowledge_index.has_chunks("m1"))

    def test_a_blank_media_id_does_not_query(self):
        client = self._client()
        self.assertFalse(knowledge_index.has_chunks(""))
        self.assertEqual(client.calls, [])


class MediaWithChunksTest(unittest.TestCase):
    """La même question que `has_chunks`, pour toute une page de `/media`.

    Vingt-cinq lectures ponctuelles seraient vingt-cinq allers-retours pour une
    seule information : ce que la liste doit dire, c'est quelles lignes ont du
    texte à relire.
    """

    def _client(self, rows=()):
        """La table des morceaux de la doublure, semée de ces lignes."""
        client = supabase_double.SupabaseDouble()
        client.store(knowledge_index.TABLE).rows = [dict(row) for row in rows]
        supabase_double.use_supabase(self, client, knowledge_index)
        return client

    def test_only_the_medias_with_a_chunk_are_returned(self):
        client = self._client([{"media_id": "m1"}, {"media_id": "m1"}, {"media_id": "m2"}])
        self.assertEqual(knowledge_index.media_with_chunks(["m1", "m2", "m3"]), {"m1", "m2"})
        self.assertIn(("in", "media_id", ("m1", "m2", "m3")), client.calls)

    def test_an_empty_selection_does_not_query(self):
        client = self._client()
        self.assertEqual(knowledge_index.media_with_chunks([]), set())
        self.assertEqual(knowledge_index.media_with_chunks(["", None]), set())
        self.assertEqual(client.calls, [])

    def test_an_unreadable_index_raises_for_the_caller_to_decide(self):
        self._client()
        with mock.patch.object(
            knowledge_index, "_require_client", side_effect=RuntimeError("db down")
        ):
            with self.assertRaises(RuntimeError):
                knowledge_index.media_with_chunks(["m1"])


class ChunkCountsTest(unittest.TestCase):
    """Combien de morceaux par média : le cas **compté** de `media_with_chunks`.

    Une liste de médias dit ce qu'il y a à relire derrière chacun — « douze
    morceaux » n'est pas « du texte ». Deux propriétés ne se voient pas sur une
    réponse heureuse : la lecture est **paginée** (l'API ne rend qu'une partie des
    lignes, et un comptage tronqué annoncerait « 2 » là où il y en a 50), et un
    média **absent** du résultat n'a aucun morceau.
    """

    def _client(self, rows=()):
        """La table des morceaux de la doublure, semée de ces lignes."""
        client = supabase_double.SupabaseDouble()
        client.store(knowledge_index.TABLE).rows = [dict(row) for row in rows]
        supabase_double.use_supabase(self, client, knowledge_index)
        return client

    def test_each_media_is_counted(self):
        self._client(
            [
                {"media_id": "m1"},
                {"media_id": "m1"},
                {"media_id": "m1"},
                {"media_id": "m2"},
            ]
        )
        self.assertEqual(knowledge_index.chunk_counts(["m1", "m2"]), {"m1": 3, "m2": 1})

    def test_a_media_without_a_chunk_is_absent_from_the_result(self):
        """Le zéro n'est pas écrit : `get(id, 0)` le dit, un dict plein de zéros mentirait."""
        self._client([{"media_id": "m1"}])
        counts = knowledge_index.chunk_counts(["m1", "vide"])
        self.assertEqual(counts, {"m1": 1})
        self.assertEqual(counts.get("vide", 0), 0)

    def test_the_count_spans_several_pages(self):
        """5 morceaux lus par pages de 2 : le compte reste 5 (pas de troncature)."""
        client = self._client([{"media_id": "m1"} for _ in range(5)])
        self.assertEqual(knowledge_index.chunk_counts(["m1"], page_size=2), {"m1": 5})
        # 2 + 2 + 1 : une page courte signale la fin du comptage.
        self.assertEqual(len(client.store(knowledge_index.TABLE).operations("range")), 3)

    def test_an_empty_selection_does_not_read_anything(self):
        client = self._client()
        self.assertEqual(knowledge_index.chunk_counts([]), {})
        self.assertEqual(knowledge_index.chunk_counts(["", None]), {})
        self.assertEqual(client.calls, [])

    def test_the_identifiers_are_asked_once_sorted_and_deduplicated(self):
        """La même demande part toujours de la même façon : une seule requête,
        un ordre stable (utile au journal et aux tests qui le lisent)."""
        client = self._client()
        knowledge_index.chunk_counts(["m2", "m1", "m2"])
        self.assertEqual(
            [call for call in client.calls if call[0] == "in"],
            [("in", "media_id", ("m1", "m2"))],
        )

    def test_an_unreadable_index_raises(self):
        client = self._client()
        client.store(knowledge_index.TABLE).read_error = RuntimeError("index down")
        with self.assertRaises(RuntimeError):
            knowledge_index.chunk_counts(["m1"])


class ReembedChunksTest(unittest.TestCase):
    """Ré-vectorisation des morceaux déjà stockés."""

    def _rows(self, count, *, start=1):
        return [
            {"id": start + index, "content": f"texte {index}", "embedding_model": None}
            for index in range(count)
        ]

    def _client(self, rows):
        """La table des morceaux de la doublure, semée de ces lignes."""
        client = supabase_double.SupabaseDouble()
        client.store(knowledge_index.TABLE).rows = [dict(row) for row in rows]
        supabase_double.use_supabase(self, client, knowledge_index)
        return client

    def test_dry_run_counts_without_writing(self):
        client = self._client(self._rows(2))
        self.assertEqual(knowledge_index.reembed_chunks(dry_run=True), 2)
        self.assertEqual([], [call[0] for call in client.calls if call[0] == "update"])

    def test_updates_each_chunk_with_the_model_that_produced_it(self):
        client = self._client(self._rows(2))
        with mock.patch.object(
            knowledge_index,
            "_embed_passages",
            lambda chunks: ([[0.5] * 768 for _ in chunks], "gemini-embedding-001"),
        ):
            updated = knowledge_index.reembed_chunks()

        self.assertEqual(updated, 2)
        payloads = client.updates()
        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["embedding_model"], "gemini-embedding-001")
        self.assertEqual(len(payloads[0]["embedding"]), 768)
        self.assertIn(("eq", "id", 1), client.calls)

    def test_stale_filter_targets_missing_or_foreign_vectors(self):
        client = self._client(self._rows(1))
        knowledge_index.reembed_chunks(dry_run=True)
        expression = [call[1] for call in client.calls if call[0] == "or"][0]
        self.assertIn("embedding.is.null", expression)
        self.assertIn(f"embedding_model.neq.{embeddings.MODEL}", expression)

    def test_all_mode_skips_the_stale_filter(self):
        client = self._client(self._rows(1))
        knowledge_index.reembed_chunks(only_stale=False, dry_run=True)
        self.assertEqual([], [call for call in client.calls if call[0] == "or"])

    def test_model_override_reaches_the_filter(self):
        client = self._client(self._rows(1))
        knowledge_index.reembed_chunks(model="gemini-embedding-001", dry_run=True)
        expression = [call[1] for call in client.calls if call[0] == "or"][0]
        self.assertIn("embedding_model.neq.gemini-embedding-001", expression)

    def test_without_embeddings_nothing_is_written(self):
        client = self._client(self._rows(3))
        with mock.patch.object(knowledge_index, "_embed_passages", lambda chunks: (None, None)):
            self.assertEqual(knowledge_index.reembed_chunks(), 0)
        self.assertEqual(client.updates(), [])

    def test_reads_every_page(self):
        rows = self._rows(50) + self._rows(1, start=100)
        client = self._client(rows)
        with mock.patch.object(
            knowledge_index,
            "_embed_passages",
            lambda chunks: ([[0.0] * 768 for _ in chunks], "gemini-embedding-001"),
        ):
            updated = knowledge_index.reembed_chunks(page_size=50)
        self.assertEqual(updated, 51)
        self.assertEqual(len([call for call in client.calls if call[0] == "range"]), 2)

    def test_max_rows_bounds_the_work(self):
        self._client(self._rows(50))
        self.assertEqual(knowledge_index.reembed_chunks(max_rows=10, dry_run=True), 10)

    def test_unconfigured_client_fails_loudly(self):
        with mock.patch.object(knowledge_index, "supabase", None):
            with self.assertRaises(RuntimeError):
                knowledge_index.reembed_chunks(dry_run=True)


def _distribution(scores, **overrides):
    """Distribution minimale, comme celle que rend `noise_distribution`."""
    values = list(scores)
    base = {
        "pairs": len(values),
        "scores": values,
        "min": min(values),
        "median": knowledge_index.percentile(values, 50),
        "p75": knowledge_index.percentile(values, 75),
        "p90": knowledge_index.percentile(values, 90),
        "p95": knowledge_index.percentile(values, 95),
        "max": max(values),
    }
    return {**base, **overrides}


class PercentileTest(unittest.TestCase):
    def test_an_empty_series_has_no_percentile(self):
        """Rendre 0 ferait croire à une mesure."""
        self.assertIsNone(knowledge_index.percentile([], 95))

    def test_a_single_value_is_the_whole_distribution(self):
        for q in (0, 50, 95, 100):
            with self.subTest(q=q):
                self.assertEqual(knowledge_index.percentile([0.42], q), 0.42)

    def test_the_median_interpolates(self):
        self.assertAlmostEqual(knowledge_index.percentile([1, 2, 3, 4], 50), 2.5)

    def test_the_bounds_are_the_min_and_the_max(self):
        values = [0.1, 0.4, 0.9]
        self.assertAlmostEqual(knowledge_index.percentile(values, 0), 0.1)
        self.assertAlmostEqual(knowledge_index.percentile(values, 100), 0.9)

    def test_out_of_range_quantiles_are_clamped(self):
        values = [0.1, 0.9]
        self.assertAlmostEqual(knowledge_index.percentile(values, -10), 0.1)
        self.assertAlmostEqual(knowledge_index.percentile(values, 150), 0.9)

    def test_the_percentile_is_monotonic(self):
        values = [0.2, 0.35, 0.4, 0.61, 0.77]
        series = [knowledge_index.percentile(values, q) for q in range(0, 101, 5)]
        self.assertEqual(series, sorted(series))


class CosineSimilarityTest(unittest.TestCase):
    """La mesure doit être dans l'unité du seuil qu'elle fixe (celle de `<=>`)."""

    def test_a_known_pair_matches_the_hand_computation(self):
        # 3*4 + 4*3 = 24 ; ||(3,4)|| = ||(4,3)|| = 5 → 24/25.
        self.assertAlmostEqual(
            knowledge_index.cosine_similarity([3.0, 4.0], [4.0, 3.0]), 0.96
        )

    def test_identical_vectors_score_one_and_opposite_ones_minus_one(self):
        self.assertAlmostEqual(knowledge_index.cosine_similarity([1, 2, 3], [1, 2, 3]), 1.0)
        self.assertAlmostEqual(knowledge_index.cosine_similarity([1, 2], [-1, -2]), -1.0)

    def test_orthogonal_vectors_score_zero(self):
        self.assertAlmostEqual(knowledge_index.cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_the_score_does_not_depend_on_the_norm(self):
        """Les vecteurs stockés sont normalisés, mais la fonction ne le suppose pas."""
        unit = knowledge_index.cosine_similarity([1.0, 0.0], [0.6, 0.8])
        scaled = knowledge_index.cosine_similarity([10.0, 0.0], [3.0, 4.0])
        self.assertAlmostEqual(unit, scaled)

    def test_degenerate_inputs_score_zero(self):
        cases = [([], []), ([0.0, 0.0], [1.0, 1.0]), ([1.0], [1.0, 2.0]), ([1.0, 2.0], [])]
        for left, right in cases:
            with self.subTest(left=left, right=right):
                self.assertEqual(knowledge_index.cosine_similarity(left, right), 0.0)


class NoiseDistributionTest(unittest.TestCase):
    def test_pairs_are_the_product_of_both_sides(self):
        distribution = knowledge_index.noise_distribution([[1, 0], [0, 1]], [[1, 0], [0, 1], [1, 1]])
        self.assertEqual(distribution["pairs"], 6)
        self.assertEqual(distribution["documents"], 2)
        self.assertEqual(distribution["probes"], 3)

    def test_without_documents_or_probes_there_is_no_distribution(self):
        self.assertIsNone(knowledge_index.noise_distribution([], [[1.0, 0.0]]))
        self.assertIsNone(knowledge_index.noise_distribution([[1.0, 0.0]], []))

    def test_the_statistics_are_ordered(self):
        documents = [[float(i), 1.0] for i in range(6)]
        probes = [[1.0, 0.0], [0.0, 1.0]]
        distribution = knowledge_index.noise_distribution(documents, probes)
        self.assertLessEqual(distribution["min"], distribution["median"])
        self.assertLessEqual(distribution["median"], distribution["p90"])
        self.assertLessEqual(distribution["p90"], distribution["p95"])
        self.assertLessEqual(distribution["p95"], distribution["max"])

    def test_the_raw_scores_are_kept_for_rethresholding(self):
        """Sans eux, changer de percentile ne changerait que l'affichage."""
        distribution = knowledge_index.noise_distribution([[1, 0], [0, 1]], [[1, 0]])
        self.assertEqual(len(distribution["scores"]), distribution["pairs"])
        self.assertAlmostEqual(max(distribution["scores"]), distribution["max"])

    def test_the_distribution_is_deterministic(self):
        documents = [[0.2, 0.9], [0.7, 0.1]]
        probes = [[0.5, 0.5]]
        self.assertEqual(
            knowledge_index.noise_distribution(documents, probes),
            knowledge_index.noise_distribution(documents, probes),
        )


class SimilarityFloorTest(unittest.TestCase):
    def test_the_floor_is_the_chosen_percentile_plus_the_margin(self):
        scores = [0.30, 0.45, 0.55, 0.62, 0.66, 0.68, 0.70, 0.72]
        distribution = _distribution(scores)
        expected = knowledge_index.percentile(scores, 90) + 0.05
        self.assertAlmostEqual(
            knowledge_index.similarity_floor(distribution, percentile_value=90, margin=0.05),
            expected,
        )

    def test_a_stricter_percentile_gives_a_stricter_floor(self):
        distribution = _distribution([0.4, 0.5, 0.6, 0.62, 0.64, 0.7, 0.75])
        floors = [
            knowledge_index.similarity_floor(distribution, percentile_value=q)
            for q in (50, 75, 90, 95, 100)
        ]
        self.assertEqual(floors, sorted(floors))
        self.assertLess(floors[0], floors[-1])

    def test_the_floor_is_clamped_to_a_usable_band(self):
        """Trop bas il laisse passer du bruit, trop haut plus rien ne passe."""
        low, high = knowledge_index.FLOOR_BOUNDS
        self.assertEqual(
            knowledge_index.similarity_floor(_distribution([0.01, 0.02])), low
        )
        self.assertEqual(
            knowledge_index.similarity_floor(_distribution([0.97, 0.99])), high
        )

    def test_a_homogeneous_base_is_stricter_than_the_constant(self):
        """Le cas que la constante 0,5 traitait mal : un corpus tout en trading."""
        homogeneous = _distribution([0.58, 0.62, 0.65, 0.68, 0.70, 0.72, 0.75])
        floor = knowledge_index.similarity_floor(homogeneous)
        self.assertGreater(floor, 0.5, "le bruit de la base n'est pas pris en compte")

    def test_a_heterogeneous_base_is_more_permissive_than_the_constant(self):
        """L'autre moitié du problème : 0,5 jetait les bons extraits d'une base variée."""
        spread = _distribution([0.12, 0.18, 0.21, 0.25, 0.28, 0.30, 0.34])
        floor = knowledge_index.similarity_floor(spread)
        self.assertLess(floor, 0.5)

    def test_without_a_distribution_there_is_no_floor(self):
        self.assertIsNone(knowledge_index.similarity_floor(None))
        self.assertIsNone(knowledge_index.similarity_floor({"pairs": 0, "scores": []}))


class SampleDocumentVectorsTest(unittest.TestCase):
    """Un morceau par média : les morceaux d'un même document se chevauchent."""

    def _client(self, rows):
        """La table des morceaux de la doublure, semée de ces lignes."""
        client = supabase_double.SupabaseDouble()
        client.store(knowledge_index.TABLE).rows = [dict(row) for row in rows]
        supabase_double.use_supabase(self, client, knowledge_index)
        return client

    @staticmethod
    def _chunk(media_id, embedding):
        """Un morceau de média tel que l'échantillon le lit — `source` compris."""
        return {
            "media_id": media_id,
            "embedding": embedding,
            "source": knowledge_index.SOURCE_MEDIA,
        }

    def test_one_vector_per_media(self):
        self._client(
            [
                self._chunk("m1", [1.0, 0.0]),
                self._chunk("m1", [0.5, 0.5]),
                self._chunk("m2", [0.0, 1.0]),
            ]
        )
        self.assertEqual(
            knowledge_index.sample_document_vectors(limit=10), [[1.0, 0.0], [0.0, 1.0]]
        )

    def test_rows_without_a_vector_are_skipped(self):
        """Un morceau jamais vectorisé n'a rien à comparer."""
        self._client(
            [
                self._chunk("m1", None),
                self._chunk("m2", [0.0, 1.0]),
                self._chunk("m3", []),
            ]
        )
        self.assertEqual(knowledge_index.sample_document_vectors(limit=10), [[0.0, 1.0]])

    def test_rows_without_a_media_are_skipped(self):
        """Les notes n'ont pas de média : elles ne mesurent pas le bruit des médias."""
        self._client(
            [
                self._chunk(None, [1.0, 0.0]),
                self._chunk("m2", [0.0, 1.0]),
            ]
        )
        self.assertEqual(knowledge_index.sample_document_vectors(limit=10), [[0.0, 1.0]])

    def test_the_sample_is_bounded(self):
        rows = [self._chunk(f"m{i}", [1.0, 0.0]) for i in range(30)]
        self._client(rows)
        self.assertEqual(len(knowledge_index.sample_document_vectors(limit=3)), 3)

    def test_pages_are_read_until_enough_vectors(self):
        """Une page entièrement non vectorisée ne doit pas terminer l'échantillon."""
        empty = [self._chunk(f"m{i}", None) for i in range(20)]
        filled = [self._chunk(f"n{i}", [1.0, 0.0]) for i in range(20)]
        client = self._client(empty + filled)
        vectors = knowledge_index.sample_document_vectors(limit=20)
        self.assertEqual(len(vectors), 20)
        self.assertEqual(len([call for call in client.calls if call[0] == "range"]), 2)

    def test_the_sample_targets_the_media_population(self):
        """Le seuil sert aux médias : le mesurer sur les notes serait à côté."""
        client = self._client([self._chunk("m1", [1.0, 0.0])])
        knowledge_index.sample_document_vectors(limit=5)
        self.assertIn(
            ("eq", "source", knowledge_index.SOURCE_MEDIA), client.calls
        )

    def test_the_first_chunk_of_each_media_is_preferred(self):
        """Le premier morceau porte la légende ou le titre."""
        client = self._client([self._chunk("m1", [1.0, 0.0])])
        knowledge_index.sample_document_vectors(limit=5)
        orders = [call for call in client.calls if call[0] == "order"]
        self.assertEqual([call[1] for call in orders], ["chunk_index", "media_id"])

    def test_unconfigured_client_fails_loudly(self):
        with mock.patch.object(knowledge_index, "supabase", None):
            with self.assertRaises(RuntimeError):
                knowledge_index.sample_document_vectors()


class CalibrationTest(unittest.TestCase):
    """La mesure complète : échantillon + sondes, mémorisée le temps du TTL."""

    DOCUMENTS = [[1.0, 0.0], [0.9, 0.1], [0.8, 0.2], [0.7, 0.3], [0.6, 0.4], [0.5, 0.5]]

    def setUp(self) -> None:
        knowledge_index.reset_calibration_cache()
        self.addCleanup(knowledge_index.reset_calibration_cache)

    def _harness(self, documents=None, probes=None):
        calls = {"sample": 0, "embed": 0}

        def sample(**_kwargs):
            calls["sample"] += 1
            return list(self.DOCUMENTS if documents is None else documents)

        def embed(texts, **_kwargs):
            calls["embed"] += 1
            return list(probes if probes is not None else [[1.0, 0.0], [0.0, 1.0]])

        return calls, sample, embed

    def test_a_measure_returns_the_floor_and_its_distribution(self):
        _, sample, embed = self._harness()
        measured = knowledge_index.calibration(sample=sample, embed=embed)
        self.assertTrue(measured["ok"])
        self.assertIsNotNone(measured["floor"])
        self.assertEqual(measured["documents"], len(self.DOCUMENTS))
        self.assertEqual(measured["probes"], 2)
        self.assertEqual(measured["pairs"], 2 * len(self.DOCUMENTS))
        self.assertFalse(measured["cached"])
        self.assertIsNone(measured["reason"])

    def test_the_measurement_is_cached(self):
        """Sinon chaque analyse paierait un appel d'embeddings par sonde."""
        calls, sample, embed = self._harness()
        first = knowledge_index.calibration(sample=sample, embed=embed)
        second = knowledge_index.calibration(sample=sample, embed=embed)
        self.assertEqual(calls, {"sample": 1, "embed": 1})
        self.assertEqual(first["floor"], second["floor"])
        self.assertTrue(second["cached"])

    def test_the_cache_expires(self):
        calls, sample, embed = self._harness()
        clock = {"now": 1000.0}
        knowledge_index.calibration(sample=sample, embed=embed, now=lambda: clock["now"])
        clock["now"] += knowledge_index.CALIBRATION_TTL_SECONDS - 1
        self.assertTrue(
            knowledge_index.calibration(sample=sample, embed=embed, now=lambda: clock["now"])[
                "cached"
            ]
        )
        clock["now"] += 2
        self.assertFalse(
            knowledge_index.calibration(sample=sample, embed=embed, now=lambda: clock["now"])[
                "cached"
            ]
        )
        self.assertEqual(calls["sample"], 2)

    def test_force_bypasses_the_cache(self):
        calls, sample, embed = self._harness()
        knowledge_index.calibration(sample=sample, embed=embed)
        knowledge_index.calibration(force=True, sample=sample, embed=embed)
        self.assertEqual(calls["sample"], 2)

    def test_a_failure_is_cached_too(self):
        """Un échec coûte les mêmes appels : le refaire à chaque analyse serait pire."""
        calls, sample, embed = self._harness(probes=None)

        def no_embed(texts, **_kwargs):
            calls["embed"] += 1
            return None

        first = knowledge_index.calibration(sample=sample, embed=no_embed)
        second = knowledge_index.calibration(sample=sample, embed=no_embed)
        self.assertFalse(first["ok"])
        self.assertEqual(first["reason"], "embeddings indisponibles")
        self.assertTrue(second["cached"])
        self.assertEqual(calls["embed"], 1)
        self.assertIsNone(second["floor"])

    def test_too_few_documents_falls_back(self):
        """Un échantillon trop petit sous-estime le bruit : repli plutôt que seuil faux."""
        _, sample, embed = self._harness(documents=self.DOCUMENTS[:2])
        measured = knowledge_index.calibration(sample=sample, embed=embed)
        self.assertFalse(measured["ok"])
        self.assertIn("trop peu", measured["reason"])
        self.assertIsNone(measured["floor"])
        self.assertEqual(measured["minimum"], knowledge_index.CALIBRATION_MIN_DOCS)

    def test_an_unreachable_base_is_reported_not_raised(self):
        def broken(**_kwargs):
            raise RuntimeError("supabase down")

        measured = knowledge_index.calibration(sample=broken, embed=lambda texts, **k: [[1.0, 0.0]])
        self.assertFalse(measured["ok"])
        self.assertEqual(measured["reason"], "base inaccessible")
        self.assertIn("supabase down", measured["error"])

    def test_probes_are_embedded_as_queries(self):
        """Mesurer en tâche DOCUMENT donnerait une autre échelle que la recherche."""
        seen = {}

        def recorder(texts, **kwargs):
            seen["texts"] = list(texts)
            seen["kind"] = kwargs.get("kind")
            return [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0], [0.5, 0.5]]

        with mock.patch("ai.embeddings.embed_texts", recorder):
            measured = knowledge_index.calibration(
                sample=lambda **kwargs: list(self.DOCUMENTS)
            )
        self.assertTrue(measured["ok"])
        self.assertEqual(seen["kind"], "query")
        self.assertEqual(seen["texts"], list(knowledge_index.NOISE_PROBE_QUERIES))

    def test_the_probes_are_unrelated_to_the_corpus_by_construction(self):
        """Elles mesurent le bruit : si elles parlaient de trading, elles se mesureraient elles-mêmes."""
        keywords = ("trading", "bitcoin", "bourse", "action", "graphique", "marche")
        for probe in knowledge_index.NOISE_PROBE_QUERIES:
            with self.subTest(probe=probe):
                self.assertFalse(
                    any(word in probe.lower() for word in keywords), "sonde non neutre"
                )

    def test_reset_forgets_the_measure(self):
        calls, sample, embed = self._harness()
        knowledge_index.calibration(sample=sample, embed=embed)
        knowledge_index.reset_calibration_cache()
        knowledge_index.calibration(sample=sample, embed=embed)
        self.assertEqual(calls["sample"], 2)


class EmbeddingsAvailableTest(unittest.TestCase):
    """Test local d'accessibilité des embeddings (sans appel réseau)."""

    def test_true_with_key_and_httpx(self):
        with mock.patch.object(embeddings, "GEMINI_API_KEY", "secret"):
            self.assertTrue(knowledge_index.embeddings_available())

    def test_false_without_key(self):
        with mock.patch.object(embeddings, "GEMINI_API_KEY", ""):
            self.assertFalse(knowledge_index.embeddings_available())


if __name__ == "__main__":
    unittest.main()
