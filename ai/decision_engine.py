from typing import Optional, Dict, Any
from datetime import datetime, timezone

from database.supabase_client import get_recent_insights, get_economic_events, log_macro_decision
from config import MIN_CONFIDENCE
from core.market_regime import detect_market_regime
from utils.market_data import get_closes
from ai.news_analyzer import NewsAnalysisCache
from execution.news_risk_guard import check_news_risk
from core.macro_engine import calculate_symbol_macro_bias, get_currencies_for_symbol
from core.adaptive_learning import get_adaptive_parameters


def _as_score(value: Any, default: float = 0.5) -> float:
    """Coerce une sous-note en float borné à [0, 1].

    Les sous-notes (`ta`, `sentiment`, `macro`) sont désormais persistées avec
    le signal et servent de variables explicatives à l'apprentissage supervisé
    des poids : une valeur non numérique ne doit pas faire échouer l'analyse.
    """
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


class EmotionlessDecisionEngine:
    def __init__(self, min_conf: float = MIN_CONFIDENCE):
        self.min_conf = min_conf
        # TTL aligné sur la documentation (1 h) : le contexte géo/sentiment
        # n'a pas besoin d'être recalculé à chaque cycle de 10 min.
        self._news_cache = NewsAnalysisCache(ttl_seconds=3600)

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

    def analyze(
        self, asset: str, extra_context: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Analyse un actif et rend un signal, ou `None` si rien ne se dégage.

        `extra_context` est le bloc d'extraits **choisis et validés par
        l'utilisateur** (boucle RAG : `/search` → `/use`). Il est injecté dans le
        prompt de l'analyse qualitative, donc il pèse sur la composante
        sentiment/géo — jamais sur la technique, qui reste calculée sur les
        prix. Une analyse portée par ce contexte n'est pas mise en cache (un
        choix ponctuel ne doit pas devenir le contexte du cycle automatique).
        """
        # 1. Retrieve Adaptive Learning Parameters for Asset
        adaptive_params = get_adaptive_parameters(asset)
        ta_w = adaptive_params["ta_weight"]
        sent_w = adaptive_params["sentiment_weight"]
        macro_w = adaptive_params["macro_weight"]
        effective_min_conf = max(self.min_conf, adaptive_params["min_confidence"])
        sl_mult = adaptive_params["sl_multiplier"]
        tp_mult = adaptive_params["tp_multiplier"]

        # 2. Fetch economic events & run High Impact News Risk Filter
        try:
            econ_events = get_economic_events(limit=60)
        except Exception as e:
            print(f"⚠️ Warning: Could not fetch economic events from DB (fallback active): {e}")
            econ_events = []

        news_risk = check_news_risk(asset, econ_events)
        currencies = get_currencies_for_symbol(asset)
        primary_curr = currencies[0] if currencies else "USD"

        # Rule: Block trades if High Impact news window is active
        if not news_risk["allowed"]:
            log_macro_decision(
                symbol=asset,
                currency=primary_curr,
                macro_score=0.0,
                news_risk_level=news_risk["risk_level"],
                decision="BLOCKED",
                reasoning=news_risk["reason"]
            )
            print(f"   🛑 {asset}: {news_risk['reason']}")
            return None

        # 3. Calculate Forex Factory Macro Bias Score
        macro_score, macro_label, macro_reasons = calculate_symbol_macro_bias(asset, econ_events)
        # Convert macro_score in [-1, +1] to a 0.0 - 1.0 probability factor
        macro_factor = max(0.0, min(1.0, 0.50 + 0.35 * macro_score))

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

        # Régime de marché déduit des clôtures : il filtre la base de
        # connaissances (recherche vectorielle par actif + régime).
        market_regime = detect_market_regime(closes)
        insights = get_recent_insights(limit=20)
        llm_result = self._news_cache.get(
            asset, insights, regime=market_regime, extra_context=extra_context
        )

        if llm_result:
            news_score = _as_score(llm_result.get("score"))
            geo_sum = f"Analyse IA : {llm_result['reasoning']}"
            sent_sum = f"Biais IA : {llm_result['bias']} (score {llm_result['score']:.2f})"
        else:
            geo_score, geo_sum = self._score_geo(insights)
            sent_score, sent_sum = self._score_sentiment(insights)
            news_score = (geo_score + sent_score) / 2
            geo_sum += " [fallback: clé LLM absente ou erreur]"

        # Multi-Factor Dynamic Adaptive Weighting
        normalized_ta = max(0.0, min(1.0, ta_score + 0.5))
        final_prob = (ta_w * normalized_ta) + (sent_w * news_score) + (macro_w * macro_factor)

        # Apply degrade penalty if medium impact news risk
        if news_risk["risk_level"] == "DEGRADE":
            penalty = news_risk.get("penalty", 0.15)
            final_prob = max(0.0, final_prob - penalty)
            reasons.append(f"Penalité news risk ({news_risk['reason']})")

        direction = None
        if final_prob >= effective_min_conf:
            direction = "BUY"
        elif final_prob <= (1.0 - effective_min_conf):
            direction = "SELL"

        decision_code = direction if direction else "NO_SIGNAL"
        log_macro_decision(
            symbol=asset,
            currency=primary_curr,
            macro_score=macro_score,
            news_risk_level=news_risk["risk_level"],
            decision=decision_code,
            reasoning=f"Macro {macro_label} ({macro_score:+.2f}). Adaptative Conf={effective_min_conf:.2f}. " + " ; ".join(macro_reasons[:2])
        )

        if direction is None or abs(final_prob - 0.5) < (effective_min_conf - 0.5):
            return None

        # Apply dynamic SL/TP multipliers adapted from past trade learnings
        if direction == "BUY":
            sl = round(entry - sl_mult * atr, 5)
            tp = round(entry + tp_mult * atr, 5)
        else:
            sl = round(entry + sl_mult * atr, 5)
            tp = round(entry - tp_mult * atr, 5)

        macro_summary_txt = f"Forex Factory Macro: {macro_label} (score {macro_score:+.2f}) | News Risk: {news_risk['risk_level']}"
        learning_txt = f"IA Apprenante: Poids TA={int(ta_w*100)}% Macro={int(macro_w*100)}% Sent={int(sent_w*100)}% | Seuil={effective_min_conf:.2f} | ATR SL={sl_mult}x TP={tp_mult}x"
        # Traçabilité : le signal persisté doit dire qu'il a été évalué avec des
        # extraits choisis à la main (et lesquels, via le message d'origine),
        # sinon rien ne distingue plus tard cette analyse du cycle automatique.
        rag_note = ""
        if (extra_context or "").strip():
            rag_note = " Contexte RAG validé (extraits sélectionnés par l'utilisateur)."

        return {
            "asset": asset,
            "direction": direction,
            "entry": round(entry, 5),
            "stop_loss": sl,
            "take_profit": tp,
            "stop_limit": None,
            "confidence": round(final_prob, 3),
            "ta_summary": " | ".join(reasons) + f" | RSI={rsi:.1f}",
            "geo_summary": geo_sum + f" | {macro_summary_txt}",
            "sentiment_summary": sent_sum,
            # Sous-notes réellement utilisées dans final_prob : elles sont
            # persistées avec le signal (puis dans trade_post_mortems) pour
            # permettre un apprentissage supervisé des poids au lieu de
            # l'heuristique historique (voir core/adaptive_learning.py).
            "ta_score": round(_as_score(normalized_ta), 4),
            "sentiment_score": round(_as_score(news_score), 4),
            "macro_score": round(_as_score(macro_factor), 4),
            "reasoning": (
                f"Engine Auto-Apprenant ({learning_txt}). Seuil {effective_min_conf:.2f}. "
                f"Macro: {macro_label}.{rag_note}"
            ),
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
