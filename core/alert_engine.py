"""Moteur d'alerte interne — remplace TradingView.

Jusqu'ici, la technique d'alerte vivait **hors du backend** : un script Pine
Script (`pinescript/ema_cross_alert.pine`) tournait sur TradingView et poussait
un JSON vers `/webhook/tradingview`. Ce module réimplémente cette logique **en
Python**, sans dépendance externe ni compte, en s'appuyant sur les sources de
marché gratuites déjà utilisées ailleurs (`utils/market_data.py` : Yahoo,
Binance, Finnhub, Stooq, CoinGecko).

Stratégie (identique au Pine d'origine) :

* EMA rapide (20) / EMA lente (50) calculées sur les clôtures ;
* signal BUY au croisement haussier, SELL au croisement baissier ;
* stop-loss et take-profit dérivés d'un ATR(14) de Wilder :
  ``SL = entrée ∓ 1.5 × ATR``, ``TP = entrée ± 3.0 × ATR``.

`build_signal` est partagé avec l'endpoint d'ingestion `/webhook/alert` : une
alerte, qu'elle vienne du scanner interne ou d'une source externe, suit le même
pipeline de validation et le même anti-spam.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ai.decision_engine import EmotionlessDecisionEngine
from core.signal_quality import validate_signal
from database.supabase_client import get_recent_insights
from utils.market_data import get_candles

#: Paramètres par défaut — identiques au Pine Script d'origine.
EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
SL_ATR_MULT = 1.5
TP_ATR_MULT = 3.0

#: Confiance attribuée à une alerte de croisement. Un croisement est un
#: **événement binaire** : l'alerte ne prétend pas quantifier une probabilité,
#: elle signale que le croisement a eu lieu. La validation humaine reste
#: obligatoire, comme pour tout signal.
ALERT_CONFIDENCE = 0.70

#: Source enregistrée sur les signaux produits par le scanner interne.
INTERNAL_SOURCE = "internal_alert_engine"

#: Source enregistrée sur les signaux poussés par l'ingestion HTTP.
EXTERNAL_SOURCE = "alert_webhook"

#: Moteur de décision partagé (contexte géo/sentiment), construit paresseusement
#: pour ne pas figer sa configuration à l'import.
_engine: Optional[EmotionlessDecisionEngine] = None


def ema(values: List[float], span: int) -> List[float]:
    """EMA amorcée par une moyenne simple (convention ``ta.ema``).

    Renvoie une liste vide tant que `span` valeurs ne sont pas disponibles : une
    EMA amorcée sur une seule valeur serait un artefact, pas une mesure.
    """
    if span <= 0 or len(values) < span:
        return []
    k = 2.0 / (span + 1.0)
    seed = sum(values[:span]) / span
    out = [seed]
    for value in values[span:]:
        out.append(value * k + out[-1] * (1.0 - k))
    return out


def true_ranges(candles: List[Dict[str, float]]) -> List[float]:
    """True range de Wilder : ``max(high-low, |high-prevClose|, |low-prevClose|)``.

    Un true range est une **amplitude**, donc jamais négatif — y compris sur la
    première chandelle, où la formule de Wilder se réduit à ``high - low``. Une
    source de marché peut rendre une ligne mal formée (`high` < `low`) : prendre
    la valeur absolue évite d'injecter un true range négatif dans la RMA, d'où il
    ne ressortait qu'au bout de dizaines de chandelles — le temps de faire
    rejeter tous les croisements (`volatility <= 0`) sans que rien ne le dise.
    """
    trs: List[float] = []
    for i, candle in enumerate(candles):
        high = float(candle["high"])
        low = float(candle["low"])
        if i == 0:
            trs.append(abs(high - low))
            continue
        prev_close = float(candles[i - 1]["close"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return trs


def atr(candles: List[Dict[str, float]], period: int = ATR_PERIOD) -> Optional[float]:
    """ATR de Wilder (RMA du true range), identique à ``ta.atr(period)``.

    Renvoie `None` s'il n'y a pas au moins `period + 1` chandelles : une RMA
    amorcée sur moins de valeurs n'a pas de sens.
    """
    if period <= 0 or len(candles) < period + 1:
        return None
    trs = true_ranges(candles)
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def detect_cross(
    candles: List[Dict[str, float]],
    *,
    ema_fast: int = EMA_FAST,
    ema_slow: int = EMA_SLOW,
    atr_period: int = ATR_PERIOD,
    sl_atr_mult: float = SL_ATR_MULT,
    tp_atr_mult: float = TP_ATR_MULT,
) -> Optional[Dict[str, Any]]:
    """Détecte un croisement EMA sur la dernière chandelle close.

    Retourne un descripteur d'alerte (`action`, `price`, `stop_loss`,
    `take_profit`, `message`, valeurs d'EMA/ATR) ou `None` s'il n'y a pas de
    croisement exploitable (historique insuffisant ou ATR nul).
    """
    closes = [float(c["close"]) for c in candles]
    if len(closes) < max(ema_fast, ema_slow) + 1:
        return None

    fast = ema(closes, ema_fast)
    slow = ema(closes, ema_slow)
    if len(fast) < 2 or len(slow) < 2:
        return None

    f_now, f_prev = fast[-1], fast[-2]
    s_now, s_prev = slow[-1], slow[-2]
    cross_up = f_prev <= s_prev and f_now > s_now
    cross_down = f_prev >= s_prev and f_now < s_now
    if not (cross_up or cross_down):
        return None

    volatility = atr(candles, atr_period)
    if volatility is None or volatility <= 0:
        return None

    price = closes[-1]
    if cross_up:
        action = "buy"
        stop_loss = price - sl_atr_mult * volatility
        take_profit = price + tp_atr_mult * volatility
        message = f"EMA{ema_fast} crossover EMA{ema_slow} (haussier)"
    else:
        action = "sell"
        stop_loss = price + sl_atr_mult * volatility
        take_profit = price - tp_atr_mult * volatility
        message = f"EMA{ema_fast} crossunder EMA{ema_slow} (baissier)"

    return {
        "action": action,
        "price": round(price, 5),
        "stop_loss": round(stop_loss, 5),
        "take_profit": round(take_profit, 5),
        "message": message,
        "ema_fast": round(f_now, 5),
        "ema_slow": round(s_now, 5),
        "atr": round(volatility, 5),
    }


def scan_asset(
    asset: str,
    *,
    interval: str = "1h",
    limit: int = 120,
    **params: Any,
) -> Optional[Dict[str, Any]]:
    """Récupère les chandelles d'un actif et renvoie l'alerte éventuelle.

    Réseau bloquant : à appeler hors de l'event loop (``asyncio.to_thread``).
    ``**params`` est transmis à :func:`detect_cross` (périodes, multiplicateurs).
    """
    candles = get_candles(asset, limit=limit, interval=interval)
    alert = detect_cross(candles, **params)
    if alert is None:
        return None
    alert["ticker"] = asset.upper().strip()
    alert["interval"] = interval
    return alert


def _get_engine() -> EmotionlessDecisionEngine:
    global _engine
    if _engine is None:
        _engine = EmotionlessDecisionEngine()
    return _engine


def build_signal(
    *,
    ticker: str,
    action: str,
    price: Optional[float] = None,
    stop_loss: Optional[float] = None,
    take_profit: Optional[float] = None,
    message: Optional[str] = None,
    source: str = INTERNAL_SOURCE,
) -> Dict[str, Any]:
    """Construit un signal validé à partir d'une alerte (interne ou externe).

    Le contexte géopolitique/sentiment est ajouté par le moteur de décision : la
    technique vient de l'alerte, le contexte reste informatif et n'exécute rien.
    La validation (``validate_signal``) impose le mode démo si l'entrée est
    absente, et le pipeline aval (anti-spam, notification) reste inchangé.
    """
    engine = _get_engine()
    direction = "BUY" if str(action).lower() in ("buy", "long") else "SELL"
    asset = str(ticker).upper().strip()

    try:
        insights = get_recent_insights(limit=20)
        llm_result = engine._news_cache.get(asset, insights)
        if llm_result:
            geo_txt = f"Analyse IA : {llm_result['reasoning']}"
            sent_txt = f"Biais IA : {llm_result['bias']} (score {llm_result['score']:.2f})"
        else:
            _, geo_txt = engine._score_geo(insights)
            _, sent_txt = engine._score_sentiment(insights)
            geo_txt += " [fallback: clé LLM absente ou erreur]"
    except Exception:
        geo_txt, sent_txt = "Indisponible", "Indisponible"

    raw_signal = {
        "asset": asset,
        "direction": direction,
        "entry": price or 0,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "confidence": ALERT_CONFIDENCE,
        "ta_summary": f"Alerte: {message or action}",
        "geo_summary": geo_txt,
        "sentiment_summary": sent_txt,
        "reasoning": (
            "Signal issu du moteur d'alerte interne (EMA20/50 + ATR, calculé "
            "côté backend à partir de sources de marché gratuites). Contexte "
            "géo/sentiment ajouté à titre informatif. Validation humaine "
            "obligatoire avant exécution."
        ),
        "source": source,
    }
    _, notes, normalized = validate_signal(raw_signal, allow_demo=True)
    if notes:
        normalized["reasoning"] = (
            f"{normalized.get('reasoning', '')} | Qualité: {' ; '.join(notes[:3])}".strip()
        )
    return normalized


__all__ = [
    "ALERT_CONFIDENCE",
    "ATR_PERIOD",
    "EMA_FAST",
    "EMA_SLOW",
    "EXTERNAL_SOURCE",
    "INTERNAL_SOURCE",
    "SL_ATR_MULT",
    "TP_ATR_MULT",
    "atr",
    "build_signal",
    "detect_cross",
    "ema",
    "scan_asset",
    "true_ranges",
]
