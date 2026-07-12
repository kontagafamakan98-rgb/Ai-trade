import pandas as pd
import yfinance as yf
from typing import Optional, Dict, Any
from database.supabase_client import get_recent_insights
from config import MIN_CONFIDENCE
from datetime import datetime, timezone

class EmotionlessDecisionEngine:
    """100% logique + probabilités. Zéro émotion."""

    def __init__(self, min_conf: float = MIN_CONFIDENCE):
        self.min_conf = min_conf

    def _fetch_price_df(self, asset: str, period="60d", interval="1h") -> pd.DataFrame:
        try:
            df = yf.download(asset, period=period, interval=interval, progress=False, auto_adjust=True)
            if df.empty:
                return pd.DataFrame()
            df.columns = [c.lower() if isinstance(c, str) else c[0].lower() for c in df.columns]
            return df
        except Exception as e:
            print(f"yfinance error {asset}: {e}")
            return pd.DataFrame()

    def analyze(self, asset: str) -> Optional[Dict[str, Any]]:
        df = self._fetch_price_df(asset)
        if len(df) < 30:
            return None

        # Indicateurs techniques purs
        df["ema20"] = df["close"].ewm(span=20).mean()
        df["ema50"] = df["close"].ewm(span=50).mean()
        delta = df["close"].diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = -delta.clip(upper=0).rolling(14).mean()
        rs = gain / loss
        df["rsi"] = 100 - (100 / (1 + rs))
        df["atr"] = (df["high"] - df["low"]).rolling(14).mean()

        last = df.iloc[-1]
        ta_score = 0.0
        reasons = []

        rsi = float(last["rsi"]) if pd.notna(last["rsi"]) else 50
        if rsi < 32:
            ta_score += 0.30
            reasons.append(f"RSI oversold ({rsi:.1f})")
        elif rsi > 68:
            ta_score -= 0.25
            reasons.append(f"RSI overbought ({rsi:.1f})")

        if last["ema20"] > last["ema50"]:
            ta_score += 0.25
            reasons.append("EMA20 > EMA50 (bullish)")
        else:
            ta_score -= 0.15
            reasons.append("EMA20 < EMA50 (bearish)")

        # Insights collectifs
        insights = get_recent_insights(limit=20)
        geo_score, geo_sum = self._score_geo(insights)
        sent_score, sent_sum = self._score_sentiment(insights)

        # Pondération fixe (jamais émotionnelle)
        final_prob = 0.50 * max(0, min(1, (ta_score + 0.5))) + 0.25 * geo_score + 0.25 * sent_score

        direction = None
        if final_prob >= 0.58:
            direction = "BUY"
        elif final_prob <= 0.42:
            direction = "SELL"

        if direction is None or abs(final_prob - 0.5) < (1 - self.min_conf):
            return None

        atr = float(last["atr"]) if pd.notna(last["atr"]) else float(last["close"]) * 0.015
        entry = float(last["close"])
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
            "reasoning": (
                f"Score purement probabiliste : TA 50% + Geo 25% + Sentiment 25%. "
                f"Aucune émotion. RR ~1:2 basé ATR. Seuil {self.min_conf}."
            ),
            "timestamp": datetime.now(timezone.utc).isoformat()
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