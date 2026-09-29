"""Tests du bloc de contexte des prompts LLM (`ai/news_analyzer.py`).

Le bloc doit distinguer deux natures de contenu : les **règles permanentes** de
la base de connaissances et les **observations datées** que sont les médias
indexés (lectures de graphiques, transcriptions). Chaque section est
best-effort : l'échec de l'une ne prive pas l'autre.
"""
from __future__ import annotations

import unittest
from unittest import mock

from ai import news_analyzer


class KnowledgeBlockTest(unittest.TestCase):
    def _patches(self, *, knowledge="", media=""):
        return (
            mock.patch.object(news_analyzer, "get_knowledge_context", return_value=knowledge),
            mock.patch.object(news_analyzer, "get_media_context", return_value=media),
        )

    def test_no_context_returns_empty_string(self):
        k, m = self._patches()
        with k, m:
            self.assertEqual(news_analyzer._knowledge_block(asset="BTC-USD"), "")

    def test_knowledge_only(self):
        k, m = self._patches(knowledge="Règle de risque")
        with k, m:
            block = news_analyzer._knowledge_block(asset="BTC-USD")
        self.assertIn("règles/principes permanents", block)
        self.assertIn("Règle de risque", block)
        self.assertNotIn("Médias indexés", block)

    def test_batch_path_does_not_search_media(self):
        """Sans actif, les médias ne sont pas cherchés (une lecture de graphique
        n'a de sens que rattachée à un actif précis)."""
        k = mock.patch.object(news_analyzer, "get_knowledge_context", return_value="Règle")
        m = mock.patch.object(news_analyzer, "get_media_context")
        with k, m as media:
            block = news_analyzer._knowledge_block()
        media.assert_not_called()
        self.assertIn("Règle", block)

    def test_media_section_is_separate_and_labelled(self):
        k, m = self._patches(knowledge="Règle", media="MÉDIA · lecture de graphique · cassure")
        with k, m:
            block = news_analyzer._knowledge_block(asset="BTC-USD", regime="RANGING")
        self.assertIn("règles/principes permanents", block)
        self.assertIn("Médias indexés pertinents pour BTC-USD", block)
        self.assertIn("OBSERVATIONS DATÉES", block)
        self.assertIn("cassure", block)

    def test_media_only_when_knowledge_empty(self):
        k, m = self._patches(media="MÉDIA · transcription vocale · stop serré")
        with k, m:
            block = news_analyzer._knowledge_block(asset="BTC-USD")
        self.assertNotIn("Connaissances de référence", block)
        self.assertIn("stop serré", block)

    def test_knowledge_failure_still_yields_media(self):
        m = mock.patch.object(news_analyzer, "get_media_context", return_value="MÉDIA · x")
        with mock.patch.object(
            news_analyzer, "get_knowledge_context", side_effect=RuntimeError("db down")
        ), m:
            block = news_analyzer._knowledge_block(asset="BTC-USD")
        self.assertIn("MÉDIA · x", block)

    def test_media_failure_still_yields_knowledge(self):
        k = mock.patch.object(news_analyzer, "get_knowledge_context", return_value="Règle")
        with k, mock.patch.object(
            news_analyzer, "get_media_context", side_effect=RuntimeError("db down")
        ):
            block = news_analyzer._knowledge_block(asset="BTC-USD")
        self.assertIn("Règle", block)
        self.assertNotIn("Médias indexés", block)

    def test_regime_is_forwarded_to_media_search(self):
        with mock.patch.object(
            news_analyzer, "get_knowledge_context", return_value=""
        ), mock.patch.object(news_analyzer, "get_media_context", return_value="") as media:
            news_analyzer._knowledge_block(asset="BTC-USD", regime="TRENDING_BULL")
        self.assertEqual(media.call_args.kwargs["regime"], "TRENDING_BULL")


class PromptWiringTest(unittest.TestCase):
    def test_single_prompt_embeds_the_media_section(self):
        insights = [{"type": "news", "title": "T", "summary": "S"}]
        with mock.patch.object(
            news_analyzer, "get_knowledge_context", return_value=""
        ), mock.patch.object(
            news_analyzer,
            "get_media_context",
            return_value="MÉDIA · lecture de graphique · cassure des 100k",
        ):
            prompt = news_analyzer._build_single_prompt("BTC-USD", insights, regime="RANGING")
        self.assertIsNotNone(prompt)
        self.assertIn("cassure des 100k", prompt)


class BatchMediaBlockTest(unittest.TestCase):
    """Bloc média du prompt batch : partagé par actif, jamais sans borne."""

    def test_no_assets_returns_empty(self):
        self.assertEqual(news_analyzer._batch_media_block([]), "")

    def test_asset_without_media_is_skipped(self):
        with mock.patch.object(news_analyzer, "get_media_context", return_value=""):
            self.assertEqual(news_analyzer._batch_media_block(["BTC-USD"]), "")

    def test_media_is_grouped_under_its_asset(self):
        def fake(asset=None, max_chars=None, **kwargs):
            return f"extrait {asset}" if asset == "BTC-USD" else ""

        with mock.patch.object(news_analyzer, "get_media_context", side_effect=fake):
            block = news_analyzer._batch_media_block(["BTC-USD", "ETH-USD"])
        self.assertIn("OBSERVATIONS DATÉES", block)
        self.assertIn("### BTC-USD", block)
        self.assertIn("extrait BTC-USD", block)
        self.assertNotIn("### ETH-USD", block)

    def test_per_asset_budget_is_a_share_of_the_total(self):
        seen = {}

        def fake(asset=None, max_chars=None, **kwargs):
            seen[asset] = max_chars
            return "x"

        assets = [f"A{i}" for i in range(12)]
        with mock.patch.object(news_analyzer, "get_media_context", side_effect=fake):
            news_analyzer._batch_media_block(assets)
        self.assertLessEqual(
            seen["A0"], news_analyzer.MAX_BATCH_MEDIA_CHARS // len(assets)
        )
        self.assertLessEqual(seen["A0"], news_analyzer.BATCH_MEDIA_PER_ASSET_CHARS)

    def test_every_asset_of_a_long_watchlist_keeps_its_share(self):
        """La somme des parts doit tenir dans le budget **en-têtes compris** : sinon
        le dernier actif était écarté par la sécurité de fin de boucle, alors que
        la répartition par actif existe précisément pour ne sacrifier personne."""

        def fake(asset=None, max_chars=None, **kwargs):
            return "x" * max_chars  # un média qui respecte son plafond

        for count in (6, 8, 12, 30):
            with self.subTest(count=count):
                assets = [f"ASSET{i}" for i in range(count)]
                with mock.patch.object(news_analyzer, "get_media_context", side_effect=fake):
                    block = news_analyzer._batch_media_block(assets)
                self.assertEqual(block.count("### "), count)
                self.assertIn(f"### {assets[-1]}", block)
                # Le plafond porte sur les sections ; l'en-tête fixe (qui ne grandit
                # pas avec la watchlist) vient en plus.
                self.assertLessEqual(
                    len(block) - len(news_analyzer.BATCH_MEDIA_HEADER),
                    news_analyzer.MAX_BATCH_MEDIA_CHARS,
                )

    def test_the_share_accounts_for_the_header_of_each_block(self):
        seen = {}

        def fake(asset=None, max_chars=None, **kwargs):
            seen[asset] = max_chars
            return ""

        assets = ["BTC-USD", "ETH-USD"]  # l'en-tête le plus long dimensionne la réserve
        with mock.patch.object(news_analyzer, "get_media_context", side_effect=fake):
            news_analyzer._batch_media_block(assets)
        self.assertLess(seen["BTC-USD"], news_analyzer.MAX_BATCH_MEDIA_CHARS // len(assets))

    def test_total_budget_is_bounded(self):
        with mock.patch.object(
            news_analyzer, "get_media_context", return_value="x" * 400
        ):
            block = news_analyzer._batch_media_block([f"A{i}" for i in range(50)])
        sections = block.count("### ")
        self.assertGreaterEqual(sections, 1)
        self.assertLessEqual(sections, 6)

    def test_one_failing_asset_does_not_remove_the_others(self):
        def fake(asset=None, max_chars=None, **kwargs):
            if asset == "BAD":
                raise RuntimeError("db down")
            return f"extrait {asset}"

        with mock.patch.object(news_analyzer, "get_media_context", side_effect=fake):
            block = news_analyzer._batch_media_block(["BAD", "BTC-USD"])
        self.assertNotIn("### BAD", block)
        self.assertIn("### BTC-USD", block)


class BatchPromptMediaTest(unittest.TestCase):
    def test_batch_prompt_embeds_the_per_asset_media(self):
        insights = [{"type": "news", "title": "T", "summary": "S"}]
        with mock.patch.object(
            news_analyzer, "get_knowledge_context", return_value=""
        ), mock.patch.object(
            news_analyzer, "get_media_context", return_value="cassure des 100k"
        ):
            prompt = news_analyzer._build_batch_prompt(["BTC-USD", "ETH-USD"], insights)
        self.assertIsNotNone(prompt)
        self.assertIn("Médias indexés par actif", prompt)
        self.assertIn("### BTC-USD", prompt)
        self.assertIn("cassure des 100k", prompt)


class RagContextTest(unittest.TestCase):
    """Troisième nature de contexte : les extraits **choisis** par l'utilisateur.

    Ils ne doivent être confondus ni avec les règles permanentes, ni avec les
    observations datées : le modèle qui les prend pour une règle générale
    appliquerait à tout un extrait qui ne parle que d'un cas.
    """

    def _prompt(self, extra_context, **kwargs):
        insights = [{"type": "news", "title": "T", "summary": "S"}]
        with mock.patch.object(
            news_analyzer, "get_knowledge_context", return_value=""
        ), mock.patch.object(news_analyzer, "get_media_context", return_value=""):
            return news_analyzer._build_single_prompt(
                "BTC-USD", insights, extra_context=extra_context, **kwargs
            )

    def test_selected_excerpts_are_labelled_as_such(self):
        prompt = self._prompt("1. [note · BTC-USD] — similarité 0.870\nzone d'achat 64k")
        self.assertIn("VALIDÉS PAR L'UTILISATEUR", prompt)
        self.assertIn("zone d'achat 64k", prompt)
        self.assertIn("ne les généralise pas", prompt)

    def test_absent_context_leaves_the_prompt_unchanged(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertNotIn("VALIDÉS PAR L'UTILISATEUR", self._prompt(value))

    def test_context_is_capped_at_the_budget(self):
        prompt = self._prompt("x" * (news_analyzer.MAX_RAG_CONTEXT_CHARS + 500))
        self.assertNotIn("x" * (news_analyzer.MAX_RAG_CONTEXT_CHARS + 1), prompt)
        self.assertIn("x" * news_analyzer.MAX_RAG_CONTEXT_CHARS, prompt)

    def test_batch_prompt_never_carries_a_personal_selection(self):
        """Un choix ponctuel n'a rien à faire dans le prompt de toute la watchlist."""
        insights = [{"type": "news", "title": "T", "summary": "S"}]
        with mock.patch.object(
            news_analyzer, "get_knowledge_context", return_value=""
        ), mock.patch.object(news_analyzer, "get_media_context", return_value=""):
            prompt = news_analyzer._build_batch_prompt(["BTC-USD"], insights)
        self.assertNotIn("VALIDÉS PAR L'UTILISATEUR", prompt)


class RagCacheTest(unittest.TestCase):
    """Une analyse portée par un choix ponctuel ne passe pas par le cache.

    Les deux sens seraient faux : servir une réponse mise en cache sans les
    extraits validerait une analyse qui ne les contient pas, et stocker la
    réponse d'un utilisateur la servirait ensuite au cycle automatique.
    """

    def setUp(self):
        self.insights = [{"type": "news", "title": "T", "summary": "S"}]
        self.cache = news_analyzer.NewsAnalysisCache(ttl_seconds=3600)

    def test_plain_analysis_is_cached(self):
        calls = mock.patch.object(
            news_analyzer, "analyze_news_for_asset", return_value={"bias": "neutral"}
        )
        with calls as analyse:
            self.cache.get("BTC-USD", self.insights)
            self.cache.get("BTC-USD", self.insights)
        self.assertEqual(analyse.call_count, 1)

    def test_selected_context_bypasses_a_warm_cache(self):
        with mock.patch.object(
            news_analyzer, "analyze_news_for_asset", return_value={"bias": "neutral"}
        ):
            self.cache.get("BTC-USD", self.insights)
        with mock.patch.object(
            news_analyzer, "analyze_news_for_asset", return_value={"bias": "bullish"}
        ) as analyse:
            result = self.cache.get("BTC-USD", self.insights, extra_context="extraits")
        analyse.assert_called_once()
        self.assertEqual(analyse.call_args.kwargs["extra_context"], "extraits")
        self.assertEqual(result["bias"], "bullish", "le cache aurait servi l'analyse sans extraits")

    def test_selected_analysis_is_not_stored(self):
        with mock.patch.object(
            news_analyzer, "analyze_news_for_asset", return_value={"bias": "bullish"}
        ):
            self.cache.get("BTC-USD", self.insights, extra_context="extraits")
        with mock.patch.object(
            news_analyzer, "analyze_news_for_asset", return_value={"bias": "neutral"}
        ) as analyse:
            self.cache.get("BTC-USD", self.insights)
        analyse.assert_called_once()
        self.assertEqual(analyse.call_args.kwargs["extra_context"], None)

    def test_blank_selected_context_still_uses_the_cache(self):
        with mock.patch.object(
            news_analyzer, "analyze_news_for_asset", return_value={"bias": "neutral"}
        ) as analyse:
            self.cache.get("BTC-USD", self.insights, extra_context="   ")
            self.cache.get("BTC-USD", self.insights, extra_context="   ")
        self.assertEqual(analyse.call_count, 1)


if __name__ == "__main__":
    unittest.main()
