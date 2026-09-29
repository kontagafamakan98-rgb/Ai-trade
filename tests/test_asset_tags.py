"""Tests de l'étiquetage d'actif (`core/asset_tags.py`).

Le module ne touche à aucune base : on teste la reconnaissance et la
normalisation, en insistant sur ce qui ne doit **pas** être étiqueté — un actif
faux exclut le média des recherches de l'actif réel.
"""
from __future__ import annotations

import unittest

from core import asset_tags


class NormalizeAssetTest(unittest.TestCase):
    def test_crypto_forms_converge_on_the_repo_convention(self):
        for raw in ("btc", "BTC", "btc-usd", "BTC/USD", "btcusdt", "BTCUSD", "$BTC"):
            with self.subTest(raw=raw):
                self.assertEqual(asset_tags.normalize_asset(raw), "BTC-USD")

    def test_long_names_are_recognized(self):
        self.assertEqual(asset_tags.normalize_asset("bitcoin"), "BTC-USD")
        self.assertEqual(asset_tags.normalize_asset("Ethereum"), "ETH-USD")

    def test_stocks_keep_their_form(self):
        for raw in ("aapl", "AAPL", " tsla "):
            with self.subTest(raw=raw):
                self.assertEqual(asset_tags.normalize_asset(raw), raw.strip().upper())

    def test_quote_currencies_and_forex_pairs(self):
        self.assertEqual(asset_tags.normalize_asset("EURUSD"), "EURUSD")
        self.assertEqual(asset_tags.normalize_asset("XAUUSD"), "XAUUSD")

    def test_a_quote_currency_alone_is_not_an_asset(self):
        for raw in ("USD", "usdt", "USDC"):
            with self.subTest(raw=raw):
                self.assertIsNone(asset_tags.normalize_asset(raw))

    def test_empty_and_absurd_inputs_are_refused(self):
        for raw in (None, "", "   ", "$", "un actif avec des espaces", "A" * 30, "BTC-USD-X"):
            with self.subTest(raw=raw):
                self.assertIsNone(asset_tags.normalize_asset(raw))

    def test_an_inner_space_is_refused_not_glued(self):
        """« pas un actif » ne doit pas devenir le symbole `PASUNACTIF`."""
        for raw in ("pas un actif", "BTC USD", "a b"):
            with self.subTest(raw=raw):
                self.assertIsNone(asset_tags.normalize_asset(raw))

    def test_normalization_is_idempotent(self):
        """Sinon un aller-retour par la base changerait l'étiquette."""
        for raw in ("btc", "bitcoin", "aapl", "BTC/USDT", "XAUUSD"):
            first = asset_tags.normalize_asset(raw)
            with self.subTest(raw=raw):
                self.assertEqual(asset_tags.normalize_asset(first), first)


class DetectAssetTest(unittest.TestCase):
    def test_the_caption_wins_over_the_extracted_text(self):
        """La légende nomme l'actif ; une description peut le déduire de travers."""
        self.assertEqual(
            asset_tags.detect_asset("BTC support 64k", "graphique d'une paire ETH/USD"),
            "BTC-USD",
        )

    def test_an_explicit_marker_beats_the_order_of_the_sentence(self):
        self.assertEqual(asset_tags.detect_asset("regarde ETH mais surtout $BTC"), "BTC-USD")

    def test_pair_notation_is_recognized_in_any_case(self):
        for text in ("cassure BTC-USD", "btc/usdt en cours", "niveau sur btcusd"):
            with self.subTest(text=text):
                self.assertEqual(asset_tags.detect_asset(text), "BTC-USD")

    def test_long_names_are_matched_word_by_word(self):
        self.assertEqual(asset_tags.detect_asset("bitcoin franchit les 100k"), "BTC-USD")
        self.assertEqual(asset_tags.detect_asset("tesla en range"), "TSLA")

    def test_wordlike_tickers_need_uppercase(self):
        """« sol », « dot », « link », « ada », « meta », « spy » sont des mots."""
        for text in (
            "le sol du graphique",
            "un lien vers link",
            "dot plot du FED",
            "prénom Ada",
            "meta results",
            "a spy story",
        ):
            with self.subTest(text=text):
                self.assertIsNone(asset_tags.detect_asset(text))

    def test_wordlike_tickers_are_detected_in_uppercase(self):
        self.assertEqual(asset_tags.detect_asset("SOL casse sa résistance"), "SOL-USD")
        self.assertEqual(asset_tags.detect_asset("LINK et DOT"), "LINK-USD")
        self.assertEqual(asset_tags.detect_asset("SPY en range"), "SPY")

    def test_tickers_that_are_not_words_are_read_in_any_case(self):
        """Une légende s'écrit « btc support 64k », pas « BTC support 64k ».

        L'exiger en majuscules revenait à ne rien détecter dans la légende — donc
        à laisser `asset` NULL, donc à priver `match_knowledge_chunks` du signal
        préférentiel qu'il sait classer.
        """
        for text, expected in (
            ("btc support 64k", "BTC-USD"),
            ("eth range bas", "ETH-USD"),
            ("doge pump", "DOGE-USD"),
            ("xrp news", "XRP-USD"),
            ("ltc en range", "LTC-USD"),
            ("matic rebond", "MATIC-USD"),
            ("aapl earnings", "AAPL"),
            ("nvda earnings", "NVDA"),
            ("tsla en range", "TSLA"),
            ("jpm coupe ses prévisions", "JPM"),
        ):
            with self.subTest(text=text):
                self.assertEqual(asset_tags.detect_asset(text), expected)

    def test_a_bare_ticker_is_matched_word_by_word(self):
        """« btc » et « nvda » ne doivent pas sortir de « debitcard » ni de « nvdax »."""
        for text in ("les debitcard arrivent", "nnvda", "btcs", "nonvda"):
            with self.subTest(text=text):
                self.assertIsNone(asset_tags.detect_asset(text))

    def test_the_position_decides_between_two_bare_tickers(self):
        """Les deux tableaux ont la même forme : c'est l'ordre du texte qui tranche.

        Un ticker ambigu ne gagne que s'il est écrit en majuscules **et** arrive en
        premier — pas parce que sa table serait passée avant.
        """
        self.assertEqual(asset_tags.detect_asset("SOL puis btc"), "SOL-USD")
        self.assertEqual(asset_tags.detect_asset("btc puis SOL"), "BTC-USD")

    def test_a_caption_in_real_life_conditions_is_labelled(self):
        """Une légende ordinaire, comme celles qui arrivent vraiment."""
        for caption in (
            "btc support 64k",
            "ETH/USD cassure des 3k",
            "range sur le btc cette semaine",
            "objectif 70k pour le bitcoin",
            "nvda avant les résultats",
        ):
            with self.subTest(caption=caption):
                self.assertIsNotNone(asset_tags.detect_asset(caption))

    def test_every_crypto_base_is_classified_exactly_once(self):
        """Ajouter une base sans dire si son sigle est un mot est un oubli silencieux.

        Sans cette classification, une base nouvelle tomberait soit dans le tableau
        « en toutes lettres » (et « sol » étiquetterait le sol du graphique), soit
        dans aucun des deux (et « btc » ne serait plus détecté du tout).
        """
        classified = {
            key: name
            for name, table in (
                ("sans ambiguïté", asset_tags.UNAMBIGUOUS_TICKERS),
                ("mot", asset_tags.AMBIGUOUS_TICKERS),
            )
            for key in table
        }
        for base in asset_tags.CRYPTO_BASES:
            with self.subTest(base=base):
                self.assertIn(base, classified, f"{base} n'est dans aucun tableau de tickers")
        self.assertEqual(
            sum(base in asset_tags.UNAMBIGUOUS_TICKERS for base in asset_tags.CRYPTO_BASES)
            + sum(base in asset_tags.AMBIGUOUS_TICKERS for base in asset_tags.CRYPTO_BASES),
            len(asset_tags.CRYPTO_BASES),
            "une base est classée deux fois (en toutes lettres *et* en majuscules)",
        )

    def test_the_two_tables_are_disjoint_and_their_union_is_the_vocabulary(self):
        """Un ticker dans les deux tableaux serait reconnu des deux façons — donc
        deux fois dans la même phrase, avec deux gagnants selon l'ordre."""
        self.assertEqual(
            set(asset_tags.UNAMBIGUOUS_TICKERS) & set(asset_tags.AMBIGUOUS_TICKERS), set()
        )
        self.assertEqual(
            set(asset_tags.TICKERS),
            set(asset_tags.UNAMBIGUOUS_TICKERS) | set(asset_tags.AMBIGUOUS_TICKERS),
        )

    def test_one_message_can_still_only_carry_one_label(self):
        for text in ("btc contre SOL", "eth et aapl", "bitcoin, doge, xrp"):
            with self.subTest(text=text):
                self.assertNotIsInstance(asset_tags.detect_asset(text), (list, tuple, set))

    def test_nothing_recognizable_yields_nothing(self):
        """Un média non étiqueté reste un candidat (joker) : mieux que du faux."""
        for text in ("", "   ", None, "analyse du range 64k-68k", "le support tient"):
            with self.subTest(text=text):
                self.assertIsNone(asset_tags.detect_asset(text))

    def test_an_invented_pair_is_not_invented_as_an_asset(self):
        self.assertIsNone(asset_tags.detect_asset("fraisUSD et cashUSDT"))

    def test_english_words_that_look_like_tickers_are_left_alone(self):
        """« apple » est un mot ; c'est le tableau fermé qui protège, pas la chance."""
        self.assertIsNone(asset_tags.detect_asset("near the support, meta analyse"))
        self.assertEqual(asset_tags.detect_asset("near the support, META analyse"), "META")

    def test_only_one_asset_is_returned(self):
        """`asset` est une égalité en base : deux étiquettes n'auraient pas de sens."""
        self.assertEqual(asset_tags.detect_asset("BTC contre ETH"), "BTC-USD")
        self.assertNotIsInstance(asset_tags.detect_asset("BTC contre ETH"), list)

    def test_alias_table_and_tickers_agree_on_the_convention(self):
        for alias, asset in asset_tags.UNAMBIGUOUS_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertEqual(asset_tags.normalize_asset(alias), asset)
        for ticker, asset in asset_tags.TICKERS.items():
            with self.subTest(ticker=ticker):
                self.assertEqual(asset_tags.normalize_asset(ticker), asset)

    def test_every_recognized_asset_is_canonical(self):
        """Aucune détection ne doit produire une étiquette que `/tag` refuserait."""
        samples = ["$BTC", "btc-usdt", "bitcoin", "SOL", "AAPL", "tesla", "XAUUSD"]
        for text in samples:
            found = asset_tags.detect_asset(text)
            with self.subTest(text=text):
                self.assertEqual(asset_tags.normalize_asset(found), found)


if __name__ == "__main__":
    unittest.main()
