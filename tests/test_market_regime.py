"""Tests du détecteur de régime de marché (portage du Kotlin).

On verrouille la classification sur des séries synthétiques : chaque régime doit
être atteignable, et les seuils doivent rester ceux du `DecisionEngine` Android.
"""
from __future__ import annotations

import unittest

from core import market_regime as mr


class DetectMarketRegimeTest(unittest.TestCase):
    def test_short_series_defaults_to_ranging(self):
        self.assertEqual(mr.detect_market_regime([100.0] * 10), mr.RANGING_SIDEWAYS)

    def test_high_volatility_crisis(self):
        closes = [100.0, 110.0] * 10  # écarts de 10 % -> ATR% = 10 > 3.2
        self.assertEqual(mr.detect_market_regime(closes), mr.HIGH_VOLATILITY_CRISIS)

    def test_uptrend_is_trending_bull(self):
        closes = [100.0 * (1.015 ** i) for i in range(60)]
        self.assertEqual(mr.detect_market_regime(closes), mr.TRENDING_BULL)

    def test_downtrend_is_trending_bear(self):
        closes = [100.0 * (0.985 ** i) for i in range(60)]
        self.assertEqual(mr.detect_market_regime(closes), mr.TRENDING_BEAR)

    def test_chop_is_ranging(self):
        closes = [100.0 + (0.5 if i % 2 else -0.5) for i in range(40)]
        self.assertEqual(mr.detect_market_regime(closes), mr.RANGING_SIDEWAYS)

    def test_non_positive_last_price_defaults_to_ranging(self):
        self.assertEqual(mr.detect_market_regime([0.0] * 25), mr.RANGING_SIDEWAYS)

    def test_all_regimes_are_declared(self):
        self.assertEqual(
            set(mr.REGIMES),
            {
                mr.TRENDING_BULL,
                mr.TRENDING_BEAR,
                mr.RANGING_SIDEWAYS,
                mr.HIGH_VOLATILITY_CRISIS,
            },
        )


if __name__ == "__main__":
    unittest.main()
