"""Tests de la base de connaissances (`database/knowledge_base.py`).

On vérifie que le contexte vient bien de la **recherche vectorielle** filtrée par
actif et régime, et que tout échec (pas d'embedding, base injoignable) retombe
sur l'**extrait fixe** — un prompt ne doit jamais se retrouver sans contexte.
"""
from __future__ import annotations

import pathlib
import unittest
from types import SimpleNamespace
from unittest import mock

from database import knowledge_base, knowledge_index


class _Query:
    def __init__(self, data, calls):
        self._data = data
        self.calls = calls

    def order(self, field, desc=False):
        self.calls.append(("order", field, desc))
        return self

    def execute(self):
        self.calls.append(("execute",))
        return SimpleNamespace(data=self._data)


class _Table:
    def __init__(self, rows, calls):
        self.rows = rows
        self.calls = calls

    def select(self, *args, **kwargs):
        self.calls.append(("select", args, kwargs))
        return _Query(self.rows, self.calls)

    def upsert(self, payload):
        self.calls.append(("upsert", payload))
        return _Query([{**payload}], self.calls)


def _client(rows):
    calls: list = []
    table = _Table(rows, calls)
    return SimpleNamespace(table=lambda _name: table), table, calls


class GetKnowledgeContextTest(unittest.TestCase):
    def test_vector_hits_are_filtered_by_asset_and_regime(self):
        hits = [{"content": "Règle BTC", "asset": "BTC-USD", "regime": "TRENDING_BULL"}]
        with mock.patch.object(knowledge_index, "search_chunks", return_value=hits) as search:
            context = knowledge_base.get_knowledge_context(
                asset="BTC-USD", regime="TRENDING_BULL"
            )
        self.assertIn("Règle BTC", context)
        self.assertIn("BTC-USD", context)
        self.assertEqual(search.call_args.kwargs["asset"], "BTC-USD")
        self.assertEqual(search.call_args.kwargs["regime"], "TRENDING_BULL")

    def test_no_hits_falls_back_to_fixed_extract(self):
        client, _table, _calls = _client([{"title": "T", "content": "Contenu fixe"}])
        with mock.patch.object(knowledge_index, "search_chunks", return_value=[]), mock.patch.object(
            knowledge_base, "supabase", client
        ):
            context = knowledge_base.get_knowledge_context(asset="X")
        self.assertIn("Contenu fixe", context)

    def test_search_error_falls_back_to_fixed_extract(self):
        client, _table, _calls = _client([{"title": "T", "content": "Repli"}])
        with mock.patch.object(
            knowledge_index, "search_chunks", side_effect=RuntimeError("db down")
        ), mock.patch.object(knowledge_base, "supabase", client):
            context = knowledge_base.get_knowledge_context()
        self.assertIn("Repli", context)

    def test_context_is_bounded(self):
        hits = [{"content": "x" * 400, "asset": None} for _ in range(10)]
        with mock.patch.object(knowledge_index, "search_chunks", return_value=hits):
            context = knowledge_base.get_knowledge_context()
        self.assertLessEqual(len(context), knowledge_base.MAX_CONTEXT_CHARS + 100)

    def test_retrieval_query_mentions_asset_and_regime(self):
        query = knowledge_base._retrieval_query("BTC-USD", "TRENDING_BULL")
        self.assertIn("BTC-USD", query)
        self.assertIn("TRENDING_BULL", query)


class GetMediaContextTest(unittest.TestCase):
    """Extraits de médias indexés (lectures de graphiques, transcriptions).

    Contrairement aux notes, il n'y a **aucun repli** : sans actif, sans vecteur
    ou sans correspondance assez proche, le bloc est vide — jamais d'exception.
    """

    def test_blank_asset_returns_empty_without_search(self):
        with mock.patch.object(knowledge_index, "search_media_for_asset") as search:
            self.assertEqual(knowledge_base.get_media_context(), "")
            self.assertEqual(knowledge_base.get_media_context(asset="   "), "")
        search.assert_not_called()

    def test_no_hit_returns_empty(self):
        with mock.patch.object(knowledge_index, "search_media_for_asset", return_value=[]):
            self.assertEqual(knowledge_base.get_media_context(asset="BTC-USD"), "")

    def test_hits_are_labelled_by_media_type(self):
        hits = [
            {
                "media_id": "m1",
                "media_type": "photo",
                "file_name": "btc.png",
                "matched_chunk": "cassure des 100k",
            },
            {
                "media_id": "m2",
                "media_type": "voice",
                "file_name": "note.ogg",
                "matched_chunk": "stop serré",
            },
        ]
        with mock.patch.object(knowledge_index, "search_media_for_asset", return_value=hits):
            context = knowledge_base.get_media_context(asset="BTC-USD")
        self.assertIn("lecture de graphique", context)
        self.assertIn("transcription vocale", context)
        self.assertIn("btc.png", context)
        self.assertIn("cassure des 100k", context)

    def test_unknown_media_type_gets_a_generic_label(self):
        hits = [{"media_id": "m9", "media_type": "weird", "matched_chunk": "x"}]
        with mock.patch.object(knowledge_index, "search_media_for_asset", return_value=hits):
            context = knowledge_base.get_media_context(asset="BTC")
        self.assertIn("MÉDIA · média", context)

    def test_search_error_is_swallowed(self):
        with mock.patch.object(
            knowledge_index, "search_media_for_asset", side_effect=RuntimeError("db down")
        ):
            self.assertEqual(knowledge_base.get_media_context(asset="BTC-USD"), "")

    def test_context_is_bounded(self):
        hits = [
            {"media_id": f"m{i}", "media_type": "document", "matched_chunk": "x" * 400}
            for i in range(10)
        ]
        with mock.patch.object(knowledge_index, "search_media_for_asset", return_value=hits):
            context = knowledge_base.get_media_context(asset="BTC-USD")
        self.assertLessEqual(len(context), knowledge_base.MAX_MEDIA_CONTEXT_CHARS + 100)

    def test_regime_is_forwarded(self):
        with mock.patch.object(
            knowledge_index, "search_media_for_asset", return_value=[]
        ) as search, mock.patch.object(knowledge_index, "calibration", return_value={"ok": False}):
            knowledge_base.get_media_context(asset="BTC-USD", regime="RANGING")
        self.assertEqual(search.call_args.kwargs["regime"], "RANGING")


class MediaSimilarityFloorTest(unittest.TestCase):
    """Le seuil des médias est **mesuré sur la base**, plus choisi à la main.

    `MIN_MEDIA_SIMILARITY` ne sert plus que de repli quand la mesure est
    impossible : il ne doit pas redevenir la règle par accident.
    """

    def test_the_measured_floor_is_the_default(self):
        with mock.patch.object(
            knowledge_index, "calibration", return_value={"ok": True, "floor": 0.71}
        ) as measured:
            self.assertAlmostEqual(knowledge_base.media_similarity_floor(), 0.71)
        measured.assert_called_once()

    def test_the_signature_no_longer_imposes_the_constant(self):
        import inspect

        default = inspect.signature(knowledge_base.get_media_context).parameters[
            "min_similarity"
        ].default
        self.assertIsNone(default, "le seuil doit venir de la mesure, pas d'une constante")

    def test_the_constant_remains_the_fallback(self):
        for outcome in (
            {"ok": False, "reason": "trop peu de documents", "floor": None},
            {"ok": False, "reason": "embeddings indisponibles", "floor": None},
            # Défense : un « ok » sans valeur ne doit pas être pris pour un seuil.
            {"ok": True, "floor": None},
        ):
            with self.subTest(outcome=outcome):
                with mock.patch.object(knowledge_index, "calibration", return_value=outcome):
                    self.assertEqual(
                        knowledge_base.media_similarity_floor(),
                        knowledge_base.MIN_MEDIA_SIMILARITY,
                    )

    def test_a_measurement_that_raises_does_not_break_the_analysis(self):
        with mock.patch.object(
            knowledge_index, "calibration", side_effect=RuntimeError("boom")
        ):
            self.assertEqual(
                knowledge_base.media_similarity_floor(), knowledge_base.MIN_MEDIA_SIMILARITY
            )

    def test_the_measured_floor_reaches_the_search(self):
        with mock.patch.object(
            knowledge_index, "calibration", return_value={"ok": True, "floor": 0.68}
        ), mock.patch.object(
            knowledge_index, "search_media_for_asset", return_value=[]
        ) as search:
            knowledge_base.get_media_context(asset="BTC-USD")
        self.assertAlmostEqual(search.call_args.kwargs["min_similarity"], 0.68)

    def test_an_explicit_threshold_wins_and_spares_the_measurement(self):
        """Un appelant qui sait mieux que la mesure ne doit pas payer la mesure."""
        with mock.patch.object(knowledge_index, "calibration") as measured, mock.patch.object(
            knowledge_index, "search_media_for_asset", return_value=[]
        ) as search:
            knowledge_base.get_media_context(asset="BTC-USD", min_similarity=0.9)
        measured.assert_not_called()
        self.assertAlmostEqual(search.call_args.kwargs["min_similarity"], 0.9)


class SearchKnowledgeTest(unittest.TestCase):
    """Recherche sémantique de la commande `/search`."""

    def test_blank_query_is_rejected_without_searching(self):
        with mock.patch.object(knowledge_index, "search_chunks") as search:
            result = knowledge_base.search_knowledge("   ")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "empty_query")
        search.assert_not_called()

    def test_hits_are_returned_verbatim(self):
        hits = [{"content": "support", "similarity": 0.81}]
        with mock.patch.object(knowledge_index, "search_chunks", return_value=hits):
            result = knowledge_base.search_knowledge("support bitcoin")
        self.assertTrue(result["ok"])
        self.assertEqual(result["hits"], hits)
        self.assertEqual(result["query"], "support bitcoin")

    def test_top_k_is_clamped_to_the_maximum(self):
        with mock.patch.object(
            knowledge_index, "search_chunks", return_value=[]
        ) as search, mock.patch.object(knowledge_index, "embeddings_available", return_value=True):
            knowledge_base.search_knowledge("x", top_k=1000)
        self.assertEqual(search.call_args.kwargs["top_k"], knowledge_base.MAX_SEARCH_TOP_K)

    def test_search_error_is_reported(self):
        with mock.patch.object(
            knowledge_index, "search_chunks", side_effect=RuntimeError("db down")
        ):
            result = knowledge_base.search_knowledge("x")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "search_failed")
        self.assertIn("db down", result["error"])

    def test_empty_result_flags_unavailable_embeddings(self):
        with mock.patch.object(knowledge_index, "search_chunks", return_value=[]), mock.patch.object(
            knowledge_index, "embeddings_available", return_value=False
        ):
            result = knowledge_base.search_knowledge("x")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "embeddings_unavailable")

    def test_empty_result_with_embeddings_is_a_real_no_match(self):
        with mock.patch.object(knowledge_index, "search_chunks", return_value=[]), mock.patch.object(
            knowledge_index, "embeddings_available", return_value=True
        ):
            result = knowledge_base.search_knowledge("x")
        self.assertTrue(result["ok"])
        self.assertEqual(result["hits"], [])


class ParseSearchArgsTest(unittest.TestCase):
    """Options de la commande `/search` (`parse_search_args`).

    Le point délicat d'un parseur d'arguments de chat : ce qui est une option et
    ce qui est une requête. Une option avalée par erreur, ou un mot de la requête
    pris pour une option, donne une recherche silencieusement fausse.
    """

    def test_bare_query_keeps_the_defaults(self):
        parsed = knowledge_base.parse_search_args(["niveaux", "de", "support", "bitcoin"])
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["query"], "niveaux de support bitcoin")
        self.assertIsNone(parsed["asset"])
        self.assertIsNone(parsed["source"])
        self.assertIsNone(parsed["regime"])
        self.assertEqual(parsed["top_k"], knowledge_base.DEFAULT_SEARCH_TOP_K)

    def test_every_option_is_parsed(self):
        parsed = knowledge_base.parse_search_args(
            ["cassure", "--asset", "btc-usd", "--source", "medias", "--regime", "BULL", "-n", "8"]
        )
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["query"], "cassure")
        self.assertEqual(parsed["asset"], "BTC-USD", "l'actif doit être normalisé en majuscules")
        self.assertEqual(parsed["source"], knowledge_index.SOURCE_MEDIA)
        self.assertEqual(parsed["regime"], "BULL")
        self.assertEqual(parsed["top_k"], 8)

    def test_long_and_short_forms_agree(self):
        short = knowledge_base.parse_search_args(["q", "-a", "eth-usd", "-n", "3"])
        long = knowledge_base.parse_search_args(["q", "--asset", "eth-usd", "--limit", "3"])
        for field in ("query", "asset", "source", "regime", "top_k"):
            self.assertEqual(short[field], long[field], field)

    def test_equals_form_is_accepted(self):
        parsed = knowledge_base.parse_search_args(["q", "--asset=btc-usd", "--source=notes"])
        self.assertEqual(parsed["asset"], "BTC-USD")
        self.assertEqual(parsed["source"], knowledge_index.SOURCE_NOTE)

    def test_source_aliases_cover_both_corpora(self):
        for alias, expected in (
            ("notes", knowledge_index.SOURCE_NOTE),
            ("note", knowledge_index.SOURCE_NOTE),
            ("media", knowledge_index.SOURCE_MEDIA),
            ("medias", knowledge_index.SOURCE_MEDIA),
            ("média", knowledge_index.SOURCE_MEDIA),
            ("telegram", knowledge_index.SOURCE_MEDIA),
        ):
            with self.subTest(alias=alias):
                parsed = knowledge_base.parse_search_args(["q", "--source", alias])
                self.assertTrue(parsed["ok"], parsed)
                self.assertEqual(parsed["source"], expected)

    def test_all_alias_clears_the_source_filter(self):
        parsed = knowledge_base.parse_search_args(["q", "--source", "tout"])
        self.assertTrue(parsed["ok"])
        self.assertIsNone(parsed["source"])

    def test_words_around_the_options_stay_in_order(self):
        parsed = knowledge_base.parse_search_args(["cassure", "-n", "2", "des", "100k"])
        self.assertEqual(parsed["query"], "cassure des 100k")
        self.assertEqual(parsed["top_k"], 2)

    def test_double_dash_ends_the_options(self):
        """Une requête peut commencer par un tiret (`-5% de perte maximum`)."""
        parsed = knowledge_base.parse_search_args(["--", "-5%", "de", "perte", "maximum"])
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["query"], "-5% de perte maximum")

    def test_help_is_requested_with_a_flag(self):
        for token in ("--help", "-h", "help", "aide"):
            with self.subTest(token=token):
                parsed = knowledge_base.parse_search_args([token])
                self.assertFalse(parsed["ok"])
                self.assertEqual(parsed["reason"], "help")

    def test_empty_args_ask_for_help(self):
        parsed = knowledge_base.parse_search_args([])
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["reason"], "empty_query")

    def test_options_without_a_query_are_rejected(self):
        parsed = knowledge_base.parse_search_args(["--asset", "BTC-USD"])
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["reason"], "empty_query")

    def test_unknown_option_names_itself(self):
        parsed = knowledge_base.parse_search_args(["q", "--sauce", "x"])
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["reason"], "bad_option")
        self.assertIn("--sauce", parsed["error"])

    def test_missing_value_is_rejected(self):
        for args in (["q", "--asset"], ["q", "--source"], ["q", "--asset", "--source", "notes"]):
            with self.subTest(args=args):
                parsed = knowledge_base.parse_search_args(args)
                self.assertFalse(parsed["ok"], args)
                self.assertEqual(parsed["reason"], "bad_option")
                self.assertIn("sans valeur", parsed["error"])

    def test_limit_must_be_a_number_within_bounds(self):
        for value in ("0", "21", "999", "huit"):
            with self.subTest(value=value):
                parsed = knowledge_base.parse_search_args(["q", "-n", value])
                self.assertFalse(parsed["ok"], value)
                self.assertEqual(parsed["reason"], "bad_option")

    def test_limit_upper_bound_is_the_documented_one(self):
        parsed = knowledge_base.parse_search_args(["q", "-n", str(knowledge_base.MAX_SEARCH_TOP_K)])
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["top_k"], knowledge_base.MAX_SEARCH_TOP_K)

    def test_unknown_source_lists_the_allowed_values(self):
        parsed = knowledge_base.parse_search_args(["q", "--source", "vault"])
        self.assertFalse(parsed["ok"])
        self.assertIn("notes", parsed["error"])
        self.assertIn("medias", parsed["error"])

    def test_comma_separated_assets_are_rejected(self):
        """Le filtre SQL est une égalité : une liste ne remonterait rien."""
        parsed = knowledge_base.parse_search_args(["q", "--asset", "BTC-USD,ETH-USD"])
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["reason"], "bad_option")
        self.assertIn("un seul actif", parsed["error"])

    def test_parsed_options_reach_the_vector_search(self):
        """Bout en bout : ce que l'utilisateur tape finit dans l'appel SQL."""
        chunks = mock.patch.object(knowledge_index, "search_chunks", return_value=[])
        embeddings = mock.patch.object(
            knowledge_index, "embeddings_available", return_value=True
        )
        with chunks as search, embeddings:
            parsed = knowledge_base.parse_search_args(
                [
                    "cassure", "--asset", "btc-usd", "--source", "medias",
                    "--regime", "BULL", "-n", "7",
                ]
            )
            knowledge_base.search_knowledge(
                parsed["query"],
                top_k=parsed["top_k"],
                asset=parsed["asset"],
                source=parsed["source"],
                regime=parsed["regime"],
            )
        kwargs = search.call_args.kwargs
        self.assertEqual(kwargs["asset"], "BTC-USD")
        self.assertEqual(kwargs["source"], knowledge_index.SOURCE_MEDIA)
        self.assertEqual(kwargs["regime"], "BULL")
        self.assertEqual(kwargs["top_k"], 7)


class FormatSearchHelpTest(unittest.TestCase):
    def test_help_documents_every_option(self):
        text = knowledge_base.format_search_help()
        self.assertIn("Usage", text)
        for option in ("--asset", "--source", "--regime", "--limit", "--help"):
            self.assertIn(option, text)

    def test_help_names_the_allowed_sources_and_the_limit_bound(self):
        text = knowledge_base.format_search_help()
        self.assertIn("notes", text)
        self.assertIn("medias", text)
        self.assertIn(str(knowledge_base.MAX_SEARCH_TOP_K), text)

    def test_help_shows_examples(self):
        self.assertIn("/search ", knowledge_base.format_search_help())


class FormatSearchResultsTest(unittest.TestCase):
    def test_usage_for_empty_query(self):
        text = knowledge_base.format_search_results(
            {"ok": False, "reason": "empty_query", "query": "", "hits": []}
        )
        self.assertIn("Usage", text)

    def test_unavailable_embeddings_is_explained(self):
        text = knowledge_base.format_search_results(
            {"ok": False, "reason": "embeddings_unavailable", "query": "x", "hits": []}
        )
        self.assertIn("GEMINI_API_KEY", text)

    def test_search_failure_is_surfaced(self):
        text = knowledge_base.format_search_results(
            {"ok": False, "reason": "search_failed", "error": "boom", "query": "x", "hits": []}
        )
        self.assertIn("boom", text)

    def test_no_hit_is_a_clear_message(self):
        text = knowledge_base.format_search_results(
            {"ok": True, "query": "support", "hits": []}
        )
        self.assertIn("Aucun passage", text)
        self.assertIn("support", text)

    def test_hits_show_rank_similarity_and_preview(self):
        hits = [
            {"content": "zone d'achat sous 64k", "similarity": 0.873, "source": "note", "asset": "BTC-USD"},
            {"content": "cassure confirmée", "similarity": 0.55, "source": knowledge_index.SOURCE_MEDIA},
        ]
        text = knowledge_base.format_search_results(
            {"ok": True, "query": "bitcoin", "hits": hits}
        )
        self.assertIn("1.", text)
        self.assertIn("2.", text)
        self.assertIn("0.873", text)
        self.assertIn("note · BTC-USD", text)
        self.assertIn("média", text)
        self.assertIn("zone d'achat sous 64k", text)

    def test_long_passage_is_truncated(self):
        hits = [{"content": "x" * 1000, "similarity": 0.9, "source": "note"}]
        text = knowledge_base.format_search_results(
            {"ok": True, "query": "q", "hits": hits}
        )
        self.assertIn("…", text)
        self.assertLess(text.count("x"), knowledge_base.SEARCH_PREVIEW_CHARS + 10)

    def test_missing_similarity_does_not_crash(self):
        hits = [{"content": "a", "source": "note"}]
        text = knowledge_base.format_search_results(
            {"ok": True, "query": "q", "hits": hits}
        )
        self.assertIn("0.000", text)

    def test_help_request_prints_the_help(self):
        text = knowledge_base.format_search_results({"ok": False, "reason": "help"})
        self.assertIn("Usage : /search", text)
        self.assertIn("--source", text)

    def test_bad_option_message_carries_the_help(self):
        parsed = knowledge_base.parse_search_args(["q", "--asset", "BTC-USD,ETH-USD"])
        text = knowledge_base.format_search_results(parsed)
        self.assertIn("un seul actif", text)
        self.assertIn("Usage : /search", text)

    def test_active_filters_are_recalled_in_the_header(self):
        """Sans ce rappel, « 1 passage » ou « aucun » paraît arbitraire."""
        options = knowledge_base.parse_search_args(
            ["q", "--source", "medias", "--asset", "btc-usd", "-n", "2"]
        )
        text = knowledge_base.format_search_results(
            {"ok": True, "query": "q", "hits": [{"content": "a", "similarity": 0.9}]},
            options=options,
        )
        self.assertIn("médias", text)
        self.assertIn("BTC-USD", text)
        self.assertIn("2 par page", text)

    def test_default_options_are_not_echoed(self):
        options = knowledge_base.parse_search_args(["q"])
        text = knowledge_base.format_search_results(
            {"ok": True, "query": "q", "hits": [{"content": "a", "similarity": 0.9}]},
            options=options,
        )
        self.assertNotIn("max ", text)

    def test_paged_title_announces_the_window(self):
        """« 6 à 10 sur 17 » : la page doit dire où elle se situe, sinon on relance."""
        text = knowledge_base.format_search_results(
            {"ok": True, "query": "cassure", "hits": [{"content": "a", "similarity": 0.9}]},
            start=6,
            total=17,
        )
        self.assertIn("passages 6 à 6 sur 17", text)

    def test_single_page_keeps_the_simple_title(self):
        text = knowledge_base.format_search_results(
            {"ok": True, "query": "cassure", "hits": [{"content": "a", "similarity": 0.9}]},
            start=1,
            total=1,
        )
        self.assertIn("1 passage(s) pour « cassure »", text)
        self.assertNotIn("sur 1", text)

    def test_ranks_continue_across_pages(self):
        hits = [{"content": "a", "similarity": 0.9}, {"content": "b", "similarity": 0.8}]
        text = knowledge_base.format_search_results(
            {"ok": True, "query": "q", "hits": hits}, start=6, total=9
        )
        self.assertIn("6.", text)
        self.assertIn("7.", text)
        self.assertNotIn("1.", text)

    def test_remaining_passages_advertise_the_more_command(self):
        hits = [{"content": "a", "similarity": 0.9}]
        text = knowledge_base.format_search_results(
            {"ok": True, "query": "q", "hits": hits}, start=1, total=5
        )
        self.assertIn("/search_more", text)
        self.assertIn("4 passage(s) de plus", text)

    def test_last_page_does_not_advertise_more(self):
        hits = [{"content": "a", "similarity": 0.9}]
        text = knowledge_base.format_search_results(
            {"ok": True, "query": "q", "hits": hits}, start=5, total=5
        )
        self.assertIn("passages 5 à 5 sur 5", text)
        self.assertNotIn("/search_more", text)

    def test_no_hit_with_filters_suggests_broadening(self):
        options = knowledge_base.parse_search_args(["q", "--source", "medias"])
        text = knowledge_base.format_search_results(
            {"ok": True, "query": "q", "hits": []}, options=options
        )
        self.assertIn("Aucun passage", text)
        self.assertIn("--source tout", text)

    def test_no_hit_without_filters_does_not_blame_the_filters(self):
        text = knowledge_base.format_search_results(
            {"ok": True, "query": "q", "hits": []}, options=knowledge_base.parse_search_args(["q"])
        )
        self.assertNotIn("--source tout", text)
        self.assertIn("Reformule", text)

    def test_ignored_arguments_of_search_more_are_announced(self):
        """Ignorer en silence un argument tapé par l'utilisateur serait trompeur."""
        text = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("ignore les arguments", text)

    def test_widening_hint_names_only_the_active_filters(self):
        asset_only = knowledge_base.parse_search_args(["q", "--asset", "BTC-USD"])
        text = knowledge_base.format_search_results(
            {"ok": True, "query": "q", "hits": []}, options=asset_only
        )
        self.assertIn("sans `--asset`", text)
        self.assertNotIn("--source tout", text, "le filtre source n'était pas actif")


class SearchCommandWiringTest(unittest.TestCase):
    """Le handler Telegram doit transmettre les options, sans en perdre une.

    `main.py` n'est pas importable ici (il construit le moteur et lit la config à
    l'import), donc on lit son source : une option parsée mais oubliée dans
    l'appel donnerait une commande qui accepte `--source` sans jamais l'appliquer
    — le pire des deux mondes.
    """

    MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"

    def _search_cmd_body(self) -> str:
        text = self.MAIN.read_text(encoding="utf-8")
        body = text.split("async def search_cmd", 1)[1]
        return body.split("\nasync def ", 1)[0]

    def test_handler_parses_the_arguments(self):
        self.assertIn("parse_search_args(context.args", self._search_cmd_body())

    def test_every_filter_reaches_the_search_call(self):
        body = self._search_cmd_body()
        for option in ("asset", "source", "regime"):
            self.assertIn(f"{option}=parsed[", body, f"option {option} non transmise")

    def test_search_asks_for_the_whole_pool(self):
        """`-n` fixe la **page** ; le lot, lui, est interrogé en une fois."""
        self.assertIn("top_k=SEARCH_POOL_SIZE", self._search_cmd_body())

    def test_pool_is_handed_to_the_history(self):
        body = self._search_cmd_body()
        self.assertIn("search_history.start(user_id, parsed", body)
        self.assertIn("page[\"total\"]", body, "la page doit être rendue avec son total")

    def test_more_command_is_registered(self):
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertIn('CommandHandler("search_more", search_more_cmd)', text)
        self.assertIn("search_history.next_page(user_id)", text)

    def test_search_reply_proposes_an_analysis(self):
        """La boucle RAG doit commencer par un bouton, pas par une commande à connaître."""
        body = self._search_cmd_body()
        self.assertIn("_analysis_keyboard(page[\"hits\"])", body)

    def test_analysis_keyboard_only_offers_labelled_assets(self):
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertIn("rag:new:{asset}", text)
        self.assertIn('if asset and asset not in assets', text)


class RagWiringTest(unittest.TestCase):
    """La boucle RAG, vue depuis `main.py` (non importable : dépendances lourdes)."""

    MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"

    def test_use_command_is_registered(self):
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertIn('CommandHandler("use", use_cmd)', text)

    def test_rag_callback_precedes_the_generic_one(self):
        """Sinon `button_handler` prendrait `rag:` pour un identifiant de signal."""
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertLess(
            text.index('CallbackQueryHandler(rag_callback, pattern=r"^rag:")'),
            text.index("CallbackQueryHandler(button_handler)"),
        )

    def test_validation_is_consumed_before_running(self):
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertIn("proposal = rag_loop.take(user_id)", text)
        self.assertIn("if proposal is None:", text)

    def test_analysis_receives_the_validated_excerpts(self):
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertIn("extra_context=proposal.context", text)
        self.assertIn("signal = engine.analyze(asset, extra_context=extra_context)", text)

    def test_the_no_signal_message_reflects_the_injected_excerpts(self):
        """Le message informatif ne doit pas être un second avis sans les extraits."""
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertIn(
            "engine._news_cache.get(asset, insights, extra_context=extra_context)", text
        )

    def test_start_message_advertises_the_use_command(self):
        self.assertIn("/use BTC-USD 1,3", self.MAIN.read_text(encoding="utf-8"))

    def test_start_message_advertises_the_help_and_the_more_command(self):
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertIn("/search --help", text)
        self.assertIn("/search_more", text)


class FormatSearchMoreTest(unittest.TestCase):
    """Message de `/search_more` : deux échecs, deux remèdes."""

    def test_a_page_is_rendered_like_a_search_page(self):
        text = knowledge_base.format_search_more(
            {
                "ok": True,
                "query": "cassure",
                "options": {"top_k": 5},
                "hits": [{"content": "a", "similarity": 0.9}],
                "start": 6,
                "total": 17,
            }
        )
        self.assertIn("passages 6 à 6 sur 17", text)
        self.assertIn("/search_more", text)

    def test_without_history_it_says_to_search_first(self):
        text = knowledge_base.format_search_more({"ok": False, "reason": "no_history"})
        self.assertIn("/search <texte>", text)

    def test_exhausted_pool_explains_the_bound(self):
        text = knowledge_base.format_search_more(
            {
                "ok": False,
                "reason": "exhausted",
                "query": "cassure",
                "shown": 20,
                "total": 20,
            }
        )
        self.assertIn("20 sur 20", text)
        self.assertIn(str(knowledge_base.SEARCH_POOL_SIZE), text)
        self.assertIn("cassure", text)

    def test_unknown_failure_is_reported(self):
        text = knowledge_base.format_search_more({"ok": False, "reason": "boom"})
        self.assertIn("boom", text)


class FormatRagSelectionTest(unittest.TestCase):
    """Messages de la boucle RAG : la proposition montre ce qui sera lu."""

    def _proposal(self, **overrides):
        proposal = {
            "ok": True,
            "asset": "BTC-USD",
            "ranks": [1, 3],
            "requested": 2,
            "chars": 780,
            "budget": 1500,
            "query": "cassure des 100k",
            "excerpts": ["1. [note · BTC-USD] — similarité 0.870\nzone d'achat 64k"],
        }
        proposal.update(overrides)
        return proposal

    def test_proposal_shows_the_excerpts_and_the_asset(self):
        text = knowledge_base.format_rag_selection(self._proposal())
        self.assertIn("BTC-USD", text)
        self.assertIn("zone d'achat 64k", text)
        self.assertIn("780 caractères", text)
        self.assertIn("cassure des 100k", text)
        self.assertIn("sans attendre", text)

    def test_proposal_announces_that_nothing_is_cached(self):
        text = knowledge_base.format_rag_selection(self._proposal())
        self.assertIn("pas mise en cache", text)

    def test_trimmed_selection_is_admitted(self):
        """Annoncer 20 extraits quand 2 seront lus ferait valider autre chose."""
        text = knowledge_base.format_rag_selection(
            self._proposal(requested=20, ranks=[1], excerpts=["1. [note] — x"], budget=1500)
        )
        self.assertIn("1 extrait(s) sur 20 demandés", text)
        # Le budget annoncé est celui de la sélection, pas celui du prompt de
        # notes : la constante homonyme de ce module vaut autre chose.
        self.assertIn("1500 caractères", text)
        self.assertNotIn(str(knowledge_base.MAX_CONTEXT_CHARS), text)

    def test_no_history_points_at_search(self):
        text = knowledge_base.format_rag_selection(
            {"ok": False, "reason": "no_history", "asset": "BTC-USD"}
        )
        self.assertIn("/search", text)
        self.assertIn("/use BTC-USD", text)

    def test_out_of_range_names_the_real_pool_size(self):
        text = knowledge_base.format_rag_selection(
            {
                "ok": False,
                "reason": "out_of_range",
                "asset": "BTC-USD",
                "value": "99",
                "available": 12,
            }
        )
        self.assertIn("99", text)
        self.assertIn("12", text)
        self.assertIn("de 1 à 12", text)

    def test_empty_pool_is_explained(self):
        text = knowledge_base.format_rag_selection({"ok": False, "reason": "empty_pool"})
        self.assertIn("--source tout", text)

    def test_missing_asset_is_explained(self):
        text = knowledge_base.format_rag_selection({"ok": False, "reason": "bad_asset"})
        self.assertIn("/use BTC-USD", text)


class UpsertNoteTest(unittest.TestCase):
    def test_note_is_indexed_as_chunks(self):
        client, _table, _calls = _client([])
        captured = {}

        def _replace(media_id, text, **kwargs):
            captured["media_id"] = media_id
            captured["text"] = text
            captured["kwargs"] = kwargs
            return 1

        with mock.patch.object(knowledge_base, "supabase", client), mock.patch.object(
            knowledge_index, "replace_chunks", _replace
        ):
            knowledge_base.upsert_note("obsidian:regles", "Titre", "Contenu")

        self.assertIsNone(captured["media_id"])
        self.assertIn("Contenu", captured["text"])
        self.assertEqual(captured["kwargs"]["note_source"], "obsidian:regles")
        self.assertEqual(captured["kwargs"]["source"], knowledge_index.SOURCE_NOTE)

    def test_note_survives_indexing_failure(self):
        client, _table, _calls = _client([])
        with mock.patch.object(knowledge_base, "supabase", client), mock.patch.object(
            knowledge_index, "replace_chunks", side_effect=RuntimeError("no db")
        ):
            knowledge_base.upsert_note("src", "Titre", "Contenu")  # ne doit pas lever


if __name__ == "__main__":
    unittest.main()
