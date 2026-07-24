from typing import Optional, Dict, Any
from datetime import datetime, timezone

from database.supabase_client import get_recent_insights
from config import MIN_CONFIDENCE
from utils.market_data import get_closes
from ai.news_analyzer import NewsAnalysisCache


class EmotionlessDecisionEngine:
    def __init__(self, min_conf: float = MIN_CONFIDENCE):
        self.min_conf = min_conf
        self._news_cache = NewsAnalysisCache(ttl_seconds=600)

    def _rsi(self, closes, period=14):
        if len(closes) <= period:
            return 50.0
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i - 1]
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _ema(self, values, span):
        if not values:
            return []
        k = 2 / (span + 1)
        out = [values[0]]
        for v in values[1:]:
            out.append(v * k + out[-1] * (1 - k))
        return out

    def _macd(self, closes, fast=12, slow=26, signal=9):
        """Retourne (macd_line, signal_line, histogram) — dernières valeurs."""
        ema_fast = self._ema(closes, fast)
        ema_slow = self._ema(closes, slow)
        macd_series = [f - s for f, s in zip(ema_fast, ema_slow)]
        signal_series = self._ema(macd_series, signal)
        macd_line = macd_series[-1]
        signal_line = signal_series[-1]
        return macd_line, signal_line, macd_line - signal_line

    def _bollinger(self, closes, period=20, num_std=2):
        """Retourne (bande_basse, moyenne, bande_haute) sur les `period` dernières valeurs."""
        window = closes[-period:]
        mean = sum(window) / len(window)
        variance = sum((c - mean) ** 2 for c in window) / len(window)
        std = variance ** 0.5
        return mean - num_std * std, mean, mean + num_std * std

    def analyze(self, asset: str) -> Optional[Dict[str, Any]]:
        # Bougies horaires (au lieu de journalières) : un croisement RSI/EMA
        # peut désormais se former plusieurs fois par jour au lieu d'une seule.
        closes = get_closes(asset, interval="1h")
        if len(closes) < 40:  # marge de sécurité pour la stabilisation du MACD
            return None

        rsi = self._rsi(closes)
        ema20 = self._ema(closes, 20)[-1]
        ema50 = self._ema(closes, 50)[-1]
        macd_line, macd_signal, macd_hist = self._macd(closes)
        bb_lower, bb_mid, bb_upper = self._bollinger(closes)
        entry = closes[-1]

        diffs = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
        atr = sum(diffs[-14:]) / 14 if len(diffs) >= 14 else entry * 0.015
        if atr <= 0:
            atr = entry * 0.015

        ta_score = 0.0
        reasons = []

        # Poids rééquilibrés maintenant que 4 indicateurs contribuent (au lieu
        # de 2) — évite de saturer le score en permanence à l'extrême.
        if rsi < 32:
            ta_score += 0.22
            reasons.append(f"RSI oversold ({rsi:.1f})")
        elif rsi > 68:
            ta_score -= 0.20
            reasons.append(f"RSI overbought ({rsi:.1f})")

        if ema20 > ema50:
            ta_score += 0.18
            reasons.append("EMA20 > EMA50 (bullish)")
        else:
            ta_score -= 0.12
            reasons.append("EMA20 < EMA50 (bearish)")

        if macd_line > macd_signal:
            ta_score += 0.18
            reasons.append(f"MACD > Signal ({macd_hist:+.4f}, bullish)")
        else:
            ta_score -= 0.15
            reasons.append(f"MACD < Signal ({macd_hist:+.4f}, bearish)")

        if entry <= bb_lower:
            ta_score += 0.17
            reasons.append("Prix ≤ bande de Bollinger basse (survente)")
        elif entry >= bb_upper:
            ta_score -= 0.17
            reasons.append("Prix ≥ bande de Bollinger haute (surachat)")

        insights = get_recent_insights(limit=20)
        llm_result = self._news_cache.get(asset, insights)

        if llm_result:
            news_score = llm_result["score"]
            geo_sum = f"Analyse IA : {llm_result['reasoning']}"
            sent_sum = f"Biais IA : {llm_result['bias']} (score {llm_result['score']:.2f})"
        else:
            geo_score, geo_sum = self._score_geo(insights)
            sent_score, sent_sum = self._score_sentiment(insights)
            news_score = (geo_score + sent_score) / 2
            geo_sum += " [fallback: clé LLM absente ou erreur]"

        final_prob = 0.50 * max(0, min(1, ta_score + 0.5)) + 0.50 * news_score

        direction = None
        if final_prob >= 0.58:
            direction = "BUY"
        elif final_prob <= 0.42:
            direction = "SELL"

        if direction is None or abs(final_prob - 0.5) < (self.min_conf - 0.5):
            return None

        if direction == "BUY":
            sl = round(entry - 1.5 * atr, 5)
            tp = round(entry + 3.0 * atr, 5)
        else:
            sl = round(entry + 1.5 * atr, 5)
            tp = round(entry - 3.0 * atr, 5)

        return {
            "asset": asset,
            "direction": direction,
            "entry": round(entry, 5),
            "stop_loss": sl,
            "take_profit": tp,
            "stop_limit": None,
            "confidence": round(final_prob, 3),
            "ta_summary": " | ".join(reasons) + f" | RSI={rsi:.1f}",
            "geo_summary": geo_sum,
            "sentiment_summary": sent_sum,
            "reasoning": f"Probabiliste TA/Geo/Sentiment. Zéro émotion. Seuil {self.min_conf}.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _score_geo(self, insights):
        relevant = [i for i in insights if i.get("type") == "geopolitical"]
        score = min(0.85, 0.35 + 0.08 * len(relevant))
        summary = relevant[0]["title"] if relevant else "Aucun événement geo majeur récent"
        return score, summary

    def _score_sentiment(self, insights):
        sent = [i for i in insights if i.get("type") == "sentiment"]
        if not sent:
            return 0.5, "Sentiment neutre"
        score = sent[0].get("data", {}).get("normalized", 0.5)
        return float(score), sent[0].get("title", "Fear & Greed")