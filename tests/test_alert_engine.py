"""Tests du moteur d'alerte interne (remplace TradingView).

On teste les fonctions **pures** (EMA, ATR de Wilder, détection de croisement) et
le constructeur de signal partagé avec l'endpoint d'ingestion. Aucun accès réseau
n'est nécessaire : `build_signal` voit sa source d'insights neutralisée.
"""
from __future__ import annotations

import unittest
from unittest import mock

from core import alert_engine as ae


def _flat_candles(closes):
    """Chandelles dégénérées high = low = close (ATR = amplitude close-à-close)."""
    return [{"high": c, "low": c, "close": c} for c in closes]


class EmaTest(unittest.TestCase):
    def test_empty_when_not_enough_values(self):
        self.assertEqual(ae.ema([1.0, 2.0], 3), [])

    def test_seeded_by_simple_average(self):
        # span 2 : seed = (1+2)/2 = 1.5 ; puis 3 -> 3*(2/3) + 1.5*(1/3) = 2.5
        out = ae.ema([1.0, 2.0, 3.0], 2)
        self.assertEqual(len(out), 2)
        self.assertAlmostEqual(out[0], 1.5, places=9)
        self.assertAlmostEqual(out[1], 2.5, places=9)

    def test_constant_series_stays_constant(self):
        out = ae.ema([5.0] * 5, 3)
        self.assertEqual(len(out), 3)
        for value in out:
            self.assertAlmostEqual(value, 5.0, places=9)


class AtrTest(unittest.TestCase):
    def test_none_when_not_enough_candles(self):
        self.assertIsNone(ae.atr(_flat_candles([1.0, 2.0]), 5))

    def test_true_range_uses_high_low(self):
        candles = [
            {"high": 10.0, "low": 8.0, "close": 9.0},
            {"high": 12.0, "low": 9.0, "close": 11.0},
        ]
        # TR0 = 10-8 = 2 ; TR1 = max(3, |12-9|, |9-9|) = 3
        self.assertEqual(ae.true_ranges(candles), [2.0, 3.0])

    def test_wilder_smoothing(self):
        # TR = [0, 0, 10] ; seed = (0+0)/2 = 0 ; puis (0*1 + 10)/2 = 5
        self.assertAlmostEqual(ae.atr(_flat_candles([10.0, 10.0, 20.0]), 2), 5.0, places=9)

    def test_zero_when_high_equals_low_and_flat(self):
        self.assertEqual(ae.atr(_flat_candles([10.0, 10.0, 10.0, 10.0]), 2), 0.0)


class DetectCrossTest(unittest.TestCase):
    def test_bullish_cross_produces_buy_with_atr_stops(self):
        candles = _flat_candles([10.0, 10.0, 10.0, 10.0, 20.0])
        alert = ae.detect_cross(candles, ema_fast=2, ema_slow=3, atr_period=2)
        self.assertIsNotNone(alert)
        self.assertEqual(alert["action"], "buy")
        self.assertAlmostEqual(alert["price"], 20.0, places=9)
        # ATR = 5 ; SL = 20 - 1.5*5 = 12.5 ; TP = 20 + 3*5 = 35
        self.assertAlmostEqual(alert["stop_loss"], 12.5, places=9)
        self.assertAlmostEqual(alert["take_profit"], 35.0, places=9)
        self.assertIn("crossover", alert["message"])

    def test_bearish_cross_produces_sell(self):
        candles = _flat_candles([20.0, 20.0, 20.0, 20.0, 10.0])
        alert = ae.detect_cross(candles, ema_fast=2, ema_slow=3, atr_period=2)
        self.assertIsNotNone(alert)
        self.assertEqual(alert["action"], "sell")
        self.assertLess(alert["take_profit"], alert["price"])
        self.assertGreater(alert["stop_loss"], alert["price"])
        self.assertIn("crossunder", alert["message"])

    def test_flat_market_returns_none(self):
        candles = _flat_candles([10.0] * 10)
        self.assertIsNone(ae.detect_cross(candles, ema_fast=2, ema_slow=3, atr_period=2))

    def test_insufficient_history_returns_none(self):
        candles = _flat_candles([10.0, 20.0])
        self.assertIsNone(ae.detect_cross(candles, ema_fast=2, ema_slow=5, atr_period=2))

    def test_default_parameters_match_original_pine_script(self):
        # Garde-fou : la stratégie remplacée utilisait EMA 20/50, ATR 14, 1.5x/3.0x.
        self.assertEqual((ae.EMA_FAST, ae.EMA_SLOW, ae.ATR_PERIOD), (20, 50, 14))
        self.assertEqual((ae.SL_ATR_MULT, ae.TP_ATR_MULT), (1.5, 3.0))


def _documented_series(n: int = 60):
    """Série déterministe et documentée utilisée par les goldens.

    Formule volontairement reproductible à la main — quiconque lit ce test peut
    régénérer les chandelles et recalculer les valeurs :

    * ``close[i] = 100 + (i % 7) + (i // 7) * 3`` ;
    * ``high[i] = close[i] + 1 + (i % 3)`` ;
    * ``low[i]  = close[i] - 1 - (i % 4)``.
    """
    candles = []
    for i in range(n):
        close = 100 + (i % 7) + (i // 7) * 3
        candles.append(
            {"high": close + 1 + (i % 3), "low": close - 1 - (i % 4), "close": close}
        )
    return candles


class EmaReferenceTest(unittest.TestCase):
    """Non-régression : l'EMA est figée sur des valeurs de référence.

    Les goldens proviennent d'un calcul **indépendant** (décimal, précision 50)
    suivant la convention ``ta.ema`` : amorçage par la moyenne simple des `span`
    premières valeurs, puis ``alpha = 2 / (span + 1)``. L'accord avec
    l'implémentation du moteur a été vérifié à 1e-12. Toute dérive du calcul
    (amorçage, facteur de lissage, ordre des valeurs) déplace ces nombres et fait
    échouer le test — c'est précisément le but.
    """

    def test_small_series_is_hand_verifiable(self):
        # span 3 : seed = (1+2+3)/3 = 2, alpha = 0.5, puis chaque pas vaut le
        # milieu entre la valeur courante et l'EMA précédente → 2,3,4,…,9.
        self.assertEqual(ae.ema([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0], 3),
                         [2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0])

    def test_production_spans_match_reference_values(self):
        closes = [c["close"] for c in _documented_series()]

        fast = ae.ema(closes, ae.EMA_FAST)  # EMA 20
        slow = ae.ema(closes, ae.EMA_SLOW)  # EMA 50

        # Longueur : n - span + 1 (une valeur par chandelle à partir de la
        # span-ième) — verrouille aussi la convention d'amorçage.
        self.assertEqual(len(fast), 60 - ae.EMA_FAST + 1)
        self.assertEqual(len(slow), 60 - ae.EMA_SLOW + 1)

        # Valeurs de référence (indices choisis : seed, intérieurs, dernière).
        expected_fast = {0: 105.7, 1: 106.3, 5: 107.726751, 10: 109.943239,
                         20: 114.293468, 30: 118.677111, 40: 122.818494}
        expected_slow = {0: 112.18, 1: 112.565098, 3: 113.406690,
                         5: 114.337356, 10: 116.418560}
        for index, value in expected_fast.items():
            self.assertAlmostEqual(fast[index], value, places=6, msg=f"EMA20[{index}]")
        for index, value in expected_slow.items():
            self.assertAlmostEqual(slow[index], value, places=6, msg=f"EMA50[{index}]")

    def test_constant_series_is_exactly_constant(self):
        # Un EMA à amorçage SMA ne doit jamais dériver sur une série plate :
        # vérifie que le facteur de lissage somme bien à 1 (k + (1-k)).
        out = ae.ema([42.0] * 30, 20)
        self.assertEqual(len(out), 11)
        self.assertTrue(all(value == 42.0 for value in out))


class AtrReferenceTest(unittest.TestCase):
    """Non-régression : l'ATR est figé sur des valeurs de référence.

    Goldens issus du même calcul indépendant, suivant ``ta.atr`` : true range de
    Wilder puis moyenne mobile de Wilder (RMA), amorcée par la moyenne simple des
    `period` premiers true ranges.
    """

    def test_true_range_convention(self):
        candles = [
            {"high": 10.0, "low": 8.0, "close": 9.0},
            {"high": 12.0, "low": 9.0, "close": 11.0},
            {"high": 11.0, "low": 10.0, "close": 10.5},
        ]
        # TR0 = 10-8 = 2 ; TR1 = max(3, |12-9|, |9-9|) = 3 ;
        # TR2 = max(1, |11-11|, |10-11|) = 1
        self.assertEqual(ae.true_ranges(candles), [2.0, 3.0, 1.0])

    def test_production_period_matches_reference_value(self):
        candles = _documented_series()
        self.assertAlmostEqual(ae.atr(candles, ae.ATR_PERIOD), 4.730047, places=6)

    def test_small_period_matches_reference_value(self):
        # Verrouille la RMA de Wilder (facteur 1/period), pas un EMA classique.
        self.assertAlmostEqual(ae.atr(_documented_series(), 2), 5.695734, places=6)

    def test_wilder_smoothing_step_by_step(self):
        # TR = [0, 0, 10] ; seed = (0+0)/2 = 0 ; puis (0*1 + 10)/2 = 5.
        self.assertAlmostEqual(ae.atr(_flat_candles([10.0, 10.0, 20.0]), 2), 5.0, places=9)


class BuildSignalTest(unittest.TestCase):
    def _signal(self, **overrides):
        params = {
            "ticker": "BTC-USD",
            "action": "buy",
            "price": 100.0,
            "stop_loss": 98.5,
            "take_profit": 104.5,
            "message": "EMA20 crossover EMA50 (haussier)",
        }
        params.update(overrides)
        with mock.patch.object(ae, "get_recent_insights", lambda *a, **k: []):
            return ae.build_signal(**params)

    def test_maps_action_to_direction_and_keeps_source(self):
        signal = self._signal(source=ae.INTERNAL_SOURCE)
        self.assertEqual(signal["direction"], "BUY")
        self.assertEqual(signal["asset"], "BTC-USD")
        self.assertEqual(signal["source"], ae.INTERNAL_SOURCE)
        self.assertIn("moteur d'alerte interne", signal["reasoning"])
        self.assertIn("EMA20 crossover", signal["ta_summary"])

    def test_external_source_is_preserved(self):
        signal = self._signal(source=ae.EXTERNAL_SOURCE)
        self.assertEqual(signal["source"], ae.EXTERNAL_SOURCE)

    def test_sell_mapping(self):
        signal = self._signal(
            action="sell", price=100.0, stop_loss=101.5, take_profit=95.5
        )
        self.assertEqual(signal["direction"], "SELL")

    def test_missing_price_falls_back_to_demo(self):
        signal = self._signal(price=None, stop_loss=None, take_profit=None)
        self.assertTrue(signal.get("is_demo"))


if __name__ == "__main__":
    unittest.main()
