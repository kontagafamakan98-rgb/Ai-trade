"""Tests de la boucle RAG (`core/rag_loop.py`) et de l'injection dans l'analyse.

La promesse de cette boucle tient à trois propriétés, et ce fichier les teste
séparément du reste :

* **la sélection est bornée par ce qui a été affiché** — un rang inexistant est
  refusé avec la plage réelle, jamais raboté en silence ;
* **ce qui est montré est ce qui est injecté** — le bloc construit ici est
  exactement celui transmis au prompt, aux plafonds près ;
* **une validation s'use** — et l'analyse reçoit bien les extraits, jusqu'au
  moteur (`EmotionlessDecisionEngine.analyze`).
"""

from __future__ import annotations

import unittest
from unittest import mock

from ai import decision_engine
from core import rag_loop, search_history


def _hits(count: int, content_length: int = 60):
    return [
        {
            "content": f"passage {index} " + "x" * content_length,
            "similarity": 0.9 - index / 100,
            "source": "telegram" if index % 3 == 0 else "note",
            "asset": "BTC-USD" if index % 2 == 0 else None,
        }
        for index in range(count)
    ]


def _options(top_k: int = 5, query: str = "cassure des 100k"):
    return {
        "ok": True,
        "query": query,
        "top_k": top_k,
        "asset": None,
        "source": None,
        "regime": None,
    }


class RagLoopTest(unittest.TestCase):
    def setUp(self):
        search_history.reset()
        rag_loop.reset()
        self.addCleanup(search_history.reset)
        self.addCleanup(rag_loop.reset)

    def _remember(
        self, count: int = 12, content_length: int = 60, top_k: int = 5, user: str = "u1"
    ):
        search_history.start(user, _options(top_k=top_k), _hits(count, content_length))


class ParseRanksTest(RagLoopTest):
    def test_default_selection_is_the_head_of_the_pool(self):
        parsed = rag_loop.parse_ranks(None, available=12)
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["ranks"], [1, 2, 3])
        self.assertEqual(rag_loop.parse_ranks(None, available=2)["ranks"], [1, 2])

    def test_explicit_ranks_and_ranges(self):
        cases = {
            "1,3": [1, 3],
            "2-4": [2, 3, 4],
            "1 3": [1, 3],
            "3,3": [3],
            " 1 , 2 ": [1, 2],
            "5": [5],
        }
        for spec, expected in cases.items():
            with self.subTest(spec=spec):
                parsed = rag_loop.parse_ranks(spec, available=12)
                self.assertTrue(parsed["ok"], parsed)
                self.assertEqual(parsed["ranks"], expected)

    def test_rank_beyond_the_pool_names_the_real_range(self):
        parsed = rag_loop.parse_ranks("13", available=12)
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["reason"], "out_of_range")
        self.assertEqual(parsed["available"], 12)

    def test_nonsense_rank_is_rejected(self):
        for spec in ("abc", "0", "-3", "4-2", "1;2"):
            with self.subTest(spec=spec):
                parsed = rag_loop.parse_ranks(spec, available=12)
                self.assertFalse(parsed["ok"], parsed)

    def test_empty_pool_cannot_be_selected_from(self):
        parsed = rag_loop.parse_ranks(None, available=0)
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["reason"], "empty_pool")


class ProposeTest(RagLoopTest):
    def test_proposal_carries_the_exact_injected_block(self):
        self._remember(count=12)
        proposal = rag_loop.propose("u1", "btc-usd", "1,3")
        self.assertTrue(proposal["ok"], proposal)
        self.assertEqual(proposal["asset"], "BTC-USD")
        self.assertEqual(proposal["ranks"], [1, 3])
        self.assertEqual(len(proposal["excerpts"]), 2)
        self.assertEqual(proposal["chars"], len(proposal["context"]))
        # « Ce qui est montré est ce qui sera injecté » : chaque extrait affiché
        # doit se retrouver littéralement dans le bloc transmis au prompt.
        for excerpt in proposal["excerpts"]:
            self.assertIn(excerpt, proposal["context"])

    def test_excerpt_numbering_matches_the_search_display(self):
        self._remember(count=12)
        context = rag_loop.propose("u1", "BTC-USD", "3")["context"]
        self.assertTrue(context.startswith("3. ["), context)
        self.assertIn("similarité 0.880", context)

    def test_without_history_there_is_nothing_to_select(self):
        proposal = rag_loop.propose("u1", "BTC-USD", "1")
        self.assertFalse(proposal["ok"])
        self.assertEqual(proposal["reason"], "no_history")

    def test_missing_asset_is_rejected(self):
        self._remember()
        proposal = rag_loop.propose("u1", "   ", "1")
        self.assertFalse(proposal["ok"])
        self.assertEqual(proposal["reason"], "bad_asset")

    def test_rank_is_checked_against_the_pool_size(self):
        self._remember(count=4)
        proposal = rag_loop.propose("u1", "BTC-USD", "9")
        self.assertFalse(proposal["ok"])
        self.assertEqual(proposal["reason"], "out_of_range")
        self.assertEqual(proposal["available"], 4)

    def test_long_excerpts_are_truncated_per_excerpt(self):
        self._remember(count=4, content_length=2000)
        context = rag_loop.propose("u1", "BTC-USD", "1")["context"]
        self.assertIn("…", context)
        self.assertLess(len(context), rag_loop.EXCERPT_CHARS + 100)

    def test_the_block_never_exceeds_its_budget(self):
        """Un plus grand nombre d'extraits ne doit pas faire grossir le prompt."""
        self._remember(count=20, content_length=2000)
        proposal = rag_loop.propose("u1", "BTC-USD", "1-20")
        self.assertTrue(proposal["ok"], proposal)
        self.assertLessEqual(proposal["chars"], rag_loop.MAX_CONTEXT_CHARS)
        # Le plafond rogne des extraits **entiers** : jamais un extrait à moitié,
        # sans que l'utilisateur sache lequel a été coupé. Et ce qui est annoncé
        # (`ranks`, `excerpts`) est exactement ce qui est injecté.
        self.assertLess(len(proposal["excerpts"]), 20)
        self.assertEqual(proposal["requested"], 20)
        # Les rangs retenus sont bien un préfixe de la sélection demandée.
        self.assertEqual(proposal["ranks"], list(range(1, len(proposal["ranks"]) + 1)))
        for excerpt in proposal["excerpts"]:
            self.assertIn(excerpt, proposal["context"])
        self.assertEqual(
            len(proposal["excerpts"]),
            len([part for part in proposal["context"].split("\n\n") if part]),
        )

    def test_proposal_budget_stays_within_the_prompt_budget(self):
        """Le plafond du bloc doit rester sous celui du prompt, qui rogne en dernier."""
        from ai import news_analyzer

        self.assertLessEqual(rag_loop.MAX_CONTEXT_CHARS, news_analyzer.MAX_RAG_CONTEXT_CHARS)

    def test_selection_is_per_user(self):
        self._remember()
        self.assertFalse(rag_loop.propose("u2", "BTC-USD", "1")["ok"])


class ValidationTest(RagLoopTest):
    def test_validation_is_single_use(self):
        """Un double clic sur « Lancer » ne doit pas produire deux analyses."""
        self._remember()
        rag_loop.propose("u1", "BTC-USD", "1")
        self.assertIsNotNone(rag_loop.take("u1"))
        self.assertIsNone(rag_loop.take("u1"))

    def test_pending_does_not_consume(self):
        self._remember()
        rag_loop.propose("u1", "BTC-USD", "1,2")
        first = rag_loop.pending("u1")
        second = rag_loop.pending("u1")
        self.assertEqual(first, second)
        self.assertEqual(first.ranks, [1, 2])

    def test_cancel_drops_the_proposal(self):
        self._remember()
        rag_loop.propose("u1", "BTC-USD", "1")
        self.assertTrue(rag_loop.cancel("u1"))
        self.assertIsNone(rag_loop.pending("u1"))
        self.assertIsNone(rag_loop.take("u1"))
        self.assertFalse(rag_loop.cancel("u1"))

    def test_a_new_proposal_replaces_the_previous_one(self):
        self._remember()
        rag_loop.propose("u1", "BTC-USD", "1")
        rag_loop.propose("u1", "ETH-USD", "2")
        self.assertEqual(rag_loop.pending("u1").asset, "ETH-USD")
        self.assertEqual(rag_loop.pending("u1").ranks, [2])

    def test_a_new_search_invalidates_the_old_selection(self):
        """Les rangs désignent le **dernier** lot : un nouveau `/search` les déplace."""
        self._remember(count=12)
        rag_loop.propose("u1", "BTC-USD", "9")
        search_history.start("u1", _options(top_k=5, query="autre"), _hits(3))
        proposal = rag_loop.propose("u1", "BTC-USD", "9")
        self.assertFalse(proposal["ok"])
        self.assertEqual(proposal["reason"], "out_of_range")

    def test_oldest_user_is_evicted(self):
        """Le bot tourne en continu : la borne d'utilisateurs doit être réelle."""
        for index in range(rag_loop.MAX_USERS + 2):
            user = f"u{index}"
            self._remember(user=user)
            rag_loop.propose(user, "BTC-USD", "1")
        self.assertIsNone(rag_loop.pending("u0"))
        self.assertIsNotNone(rag_loop.pending(f"u{rag_loop.MAX_USERS + 1}"))


class EngineInjectionTest(unittest.TestCase):
    """Les extraits doivent atteindre le moteur, et laisser une trace dans le signal."""

    def setUp(self):
        self.engine = decision_engine.EmotionlessDecisionEngine()
        self.closes = [100.0 + index * 0.5 for index in range(80)]
        self.patches = [
            mock.patch.object(
                decision_engine,
                "get_adaptive_parameters",
                return_value={
                    "ta_weight": 0.3,
                    "sentiment_weight": 0.5,
                    "macro_weight": 0.2,
                    "min_confidence": 0.55,
                    "sl_multiplier": 1.5,
                    "tp_multiplier": 3.0,
                },
            ),
            mock.patch.object(decision_engine, "get_economic_events", return_value=[]),
            mock.patch.object(
                decision_engine,
                "check_news_risk",
                return_value={
                    "allowed": True,
                    "risk_level": "LOW",
                    "reason": "ok",
                    "penalty": 0.0,
                },
            ),
            mock.patch.object(
                decision_engine, "calculate_symbol_macro_bias", return_value=(0.2, "Bullish", ["x"])
            ),
            mock.patch.object(decision_engine, "get_currencies_for_symbol", return_value=["USD"]),
            mock.patch.object(decision_engine, "get_closes", return_value=self.closes),
            mock.patch.object(
                decision_engine, "detect_market_regime", return_value="TRENDING_BULL"
            ),
            mock.patch.object(
                decision_engine,
                "get_recent_insights",
                return_value=[{"type": "sentiment", "title": "F&G", "data": {"normalized": 0.9}}],
            ),
            mock.patch.object(decision_engine, "log_macro_decision"),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _analyse(self, extra_context):
        usable = {"bias": "bullish", "score": 0.95, "reasoning": "extraits lus"}
        with mock.patch.object(self.engine._news_cache, "get", return_value=usable) as cache:
            signal = self.engine.analyze("BTC-USD", extra_context=extra_context)
        return signal, cache

    def test_excerpts_are_forwarded_to_the_qualitative_analysis(self):
        _signal, cache = self._analyse("1. [note] — similarité 0.9\nzone d'achat 64k")
        self.assertEqual(
            cache.call_args.kwargs["extra_context"], "1. [note] — similarité 0.9\nzone d'achat 64k"
        )

    def test_signal_records_that_validated_excerpts_were_used(self):
        signal, _cache = self._analyse("1. [note] — similarité 0.9\nzone d'achat 64k")
        self.assertIsNotNone(signal)
        self.assertIn("Contexte RAG validé", signal["reasoning"])

    def test_a_plain_analysis_carries_no_rag_mention(self):
        signal, cache = self._analyse(None)
        self.assertIsNotNone(signal)
        self.assertNotIn("Contexte RAG", signal["reasoning"])
        self.assertIsNone(cache.call_args.kwargs["extra_context"])


if __name__ == "__main__":
    unittest.main()
