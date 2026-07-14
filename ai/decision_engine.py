from typing import Optional, Dict, Any
from datetime import datetime, timezone

from database.supabase_client import get_recent_insights
from config import MIN_CONFIDENCE
from utils.market_data import get_closes


class EmotionlessDecisionEngine:
    def __init__(self, min_conf: float = MIN_CONFIDENCE):
        self.min_conf = min_conf

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

    def analyze(self, asset: str) -> Optional[Dict[str, Any]]:
        # Bougies horaires (au lieu de journalières) : un croisement RSI/EMA
        # peut désormais se former plusieurs fois par jour au lieu d'une seule.
        closes = get_closes(asset, interval="1h")
        if len(closes) < 30:
            return None

        rsi = self._rsi(closes)
        ema20 = self._ema(closes, 20)[-1]
        ema50 = self._ema(closes, 50)[-1]
        entry = closes[-1]

        diffs = [abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))]
        atr = sum(diffs[-14:]) / 14 if len(diffs) >= 14 else entry * 0.015
        if atr <= 0:
            atr = entry * 0.015

        ta_score = 0.0
        reasons = []

        if rsi < 32:
            ta_score += 0.30
            reasons.append(f"RSI oversold ({rsi:.1f})")
        elif rsi > 68:
            ta_score -= 0.25
            reasons.append(f"RSI overbought ({rsi:.1f})")

        if ema20 > ema50:
            ta_score += 0.25
            reasons.append("EMA20 > EMA50 (bullish)")
        else:
            ta_score -= 0.15
            reasons.append("EMA20 < EMA50 (bearish)")

        insights = get_recent_insights(limit=20)
        geo_score, geo_sum = self._score_geo(insights)
        sent_score, sent_sum = self._score_sentiment(insights)

        final_prob = 0.50 * max(0, min(1, ta_score + 0.5)) + 0.25 * geo_score + 0.25 * sent_score

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