"""Détection du régime de marché — portage Python de `DecisionEngine.kt`.

L'app Android classe déjà chaque actif selon un `MarketRegime`
(`TRENDING_BULL`, `TRENDING_BEAR`, `RANGING_SIDEWAYS`,
`HIGH_VOLATILITY_CRISIS`) à partir de la volatilité (ATR en % du prix) et de
l'écart entre les EMA 20/50. Le backend reprend **les mêmes seuils** afin que le
filtrage de la base de connaissances parle le même langage des deux côtés.

Fonction pure, sans dépendance : elle ne fait que classer une série de clôtures.
"""
from __future__ import annotations

from typing import List, Sequence

TRENDING_BULL = "TRENDING_BULL"
TRENDING_BEAR = "TRENDING_BEAR"
RANGING_SIDEWAYS = "RANGING_SIDEWAYS"
HIGH_VOLATILITY_CRISIS = "HIGH_VOLATILITY_CRISIS"

#: Tous les régimes possibles (utile pour valider un filtre).
REGIMES = (TRENDING_BULL, TRENDING_BEAR, RANGING_SIDEWAYS, HIGH_VOLATILITY_CRISIS)

#: Seuils, identiques à `DecisionEngine.detectMarketRegime` (Kotlin).
_MIN_CLOSES = 20
_ATR_PERIOD = 14
_HIGH_VOLATILITY_ATR_PCT = 3.2
_TREND_EMA_DIFF_PCT = 0.8
_TREND_MIN_ATR_PCT = 1.2


def _ema(values: Sequence[float], span: int) -> float:
    """EMA amorcée par la première valeur (convention de l'app Android).

    Volontairement identique à `calculateEma` du Kotlin : une valeur est produite
    même si la série est plus courte que `span`, pour que les seuils donneurs de
    régime réagissent pareil des deux côtés.
    """
    if not values:
        return 0.0
    k = 2.0 / (span + 1.0)
    out = float(values[0])
    for value in values[1:]:
        out = value * k + out * (1.0 - k)
    return out


def detect_market_regime(closes: Sequence[float]) -> str:
    """Retourne le régime de marché déduit d'une série de clôtures.

    `RANGING_SIDEWAYS` est le défaut : en l'absence d'information (série trop
    courte, volatilité modérée), on ne prétend pas être dans une tendance.
    """
    if len(closes) < _MIN_CLOSES:
        return RANGING_SIDEWAYS

    entry = float(closes[-1])
    if entry <= 0:
        return RANGING_SIDEWAYS

    diffs: List[float] = [abs(float(closes[i]) - float(closes[i - 1])) for i in range(1, len(closes))]
    if len(diffs) >= _ATR_PERIOD:
        atr = sum(diffs[-_ATR_PERIOD:]) / _ATR_PERIOD
    else:
        atr = entry * 0.015
    atr_pct = (atr / entry) * 100.0

    if atr_pct > _HIGH_VOLATILITY_ATR_PCT:
        return HIGH_VOLATILITY_CRISIS

    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    ema_diff_pct = ((ema20 - ema50) / ema50) * 100.0 if ema50 else 0.0

    if ema_diff_pct > _TREND_EMA_DIFF_PCT and atr_pct > _TREND_MIN_ATR_PCT:
        return TRENDING_BULL
    if ema_diff_pct < -_TREND_EMA_DIFF_PCT and atr_pct > _TREND_MIN_ATR_PCT:
        return TRENDING_BEAR
    return RANGING_SIDEWAYS


__all__ = [
    "HIGH_VOLATILITY_CRISIS",
    "RANGING_SIDEWAYS",
    "REGIMES",
    "TRENDING_BEAR",
    "TRENDING_BULL",
    "detect_market_regime",
]
