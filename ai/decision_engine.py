# ai/decision_engine.py
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from database.supabase_client import get_recent_insights
from config import MIN_CONFIDENCE
from utils.market_data import get_closes
from ai.news_analyzer import NewsAnalysisCache


class EmotionlessDecisionEngine:
    def __init__(self, min_conf: float = MIN_CONFIDENCE):
        self.min_conf = min_conf
        self._news_cache = NewsAnalysisCache(ttl_seconds=900)  # 15 min

    def _rsi(self, closes: List[float], period: int = 14) -> float:
        if len(closes) <= period:
            return 50.0
        gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _ema(self, values: List[float], span: int) -> List[float]:
        if not values:
            return []
        k = 2 / (span + 1)
        ema = [values[0]]
        for v in values[1:]:
            ema.append(v * k + ema[-1] * (1 - k))
        return ema

    def _macd(self, closes: List[float], fast=12, slow=26, sig=9):
        ema_fast = self._ema(closes, fast)
        ema_slow = self._ema(closes, slow)
        macd = [f - s for f, s in zip(ema_fast, ema_slow)]
        signal = self._ema(macd, sig)
        return macd[-1] - signal[-1] if len(macd) > sig else 0

    def _atr(self, closes: List[float], period: int = 14) -> float:
        if len(closes) < period:
            return closes[-1] * 0.015 if closes else 0.0
        trs = [abs(closes[i] - closes[i-1]) for i in range(1, len(closes))]
        return sum(trs[-period:]) / period

    def analyze(self, asset: str) -> Optional[Dict[str, Any]]:
        closes_1h = get_closes(asset, interval="1h", limit=120)
        closes_4h = get_closes(asset, interval="4h", limit=60)

        if len(closes_1h) < 60:
            return None

        entry = closes_1h[-1]
        rsi1 = self._rsi(closes_1h)
        macd1 = self._macd(closes_1h)
        ema20 = self._ema(closes_1h, 20)[-1]
        ema50 = self._ema(closes_1h, 50)[-1]
        atr = self._atr(closes_1h)

        # Scoring
        ta_score = 0.0
        reasons = []

        if rsi1 < 33:
            ta_score += 0.40
            reasons.append(f"RSI oversold {rsi1:.1f}")
        elif rsi1 > 67:
            ta_score -= 0.35
            reasons.append(f"RSI overbought {rsi1:.1f}")

        if macd1 > 0:
            ta_score += 0.30
            reasons.append("MACD bullish")
        if ema20 > ema50:
            ta_score += 0.25
            reasons.append("EMA bullish")

        # Contexte LLM
        insights = get_recent_insights(limit=25)
        llm = self._news_cache.get(asset, insights) or {}
        news_score = llm.get("score", 0.5)

        final_prob = (0.55 * ta_score) + (0.45 * news_score)

        if final_prob >= 0.63:
            direction = "BUY"
        elif final_prob <= 0.37:
            direction = "SELL"
        else:
            return None

        return {
            "asset": asset,
            "direction": direction,
            "entry": round(entry, 5),
            "stop_loss": round(entry - 1.8 * atr if direction == "BUY" else entry + 1.8 * atr, 5),
            "take_profit": round(entry + 4.2 * atr if direction == "BUY" else entry - 4.2 * atr, 5),
            "confidence": round(final_prob, 3),
            "ta_summary": " | ".join(reasons),
            "geo_summary": llm.get("reasoning", "Analyse IA indisponible"),
            "sentiment_summary": f"Biais: {llm.get('bias', 'neutral')} ({news_score:.2f})",
            "reasoning": "Multi-TF (1H+4H) + LLM consensus v2",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }