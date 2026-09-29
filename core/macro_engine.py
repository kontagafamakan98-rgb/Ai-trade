from typing import List, Dict, Any, Tuple, Optional
import math
from datetime import datetime, timezone

# Indicators where Higher Actual than Forecast is BULLISH for Currency
STANDARD_BULLISH_INDICATORS = [
    "cpi", "ppi", "gdp", "nfp", "employment", "payroll", "pmi", "retail sales",
    "interest rate", "rate decision", "trade balance", "durable goods", "confidence"
]

# Indicators where Higher Actual than Forecast is BEARISH for Currency (Inverse Indicators)
INVERSE_BEARISH_INDICATORS = [
    "unemployment", "jobless claims", "claims", "unemployment rate"
]

# Symbol to Currency Mapping
SYMBOL_CURRENCY_MAP = {
    "BTC-USD": ["USD"],
    "ETH-USD": ["USD"],
    "SOL-USD": ["USD"],
    "BNB-USD": ["USD"],
    "XRP-USD": ["USD"],
    "DOGE-USD": ["USD"],
    "XAUUSD": ["USD"],
    "GOLD": ["USD"],
    "EURUSD": ["EUR", "USD"],
    "GBPUSD": ["GBP", "USD"],
    "USDJPY": ["USD", "JPY"],
    "AUDUSD": ["AUD", "USD"],
    "USDCAD": ["USD", "CAD"],
    "AAPL": ["USD"],
    "MSFT": ["USD"],
    "GOOGL": ["USD"],
    "AMZN": ["USD"],
    "NVDA": ["USD"],
    "TSLA": ["USD"],
}


def get_currencies_for_symbol(symbol: str) -> List[str]:
    """Return list of relevant currencies for a given trading asset symbol."""
    sym = symbol.upper()
    if sym in SYMBOL_CURRENCY_MAP:
        return SYMBOL_CURRENCY_MAP[sym]
    # Default currency extraction heuristics
    if "USD" in sym or sym.endswith("-USD"):
        return ["USD"]
    if sym.startswith("EUR"):
        return ["EUR", "USD"]
    if sym.startswith("GBP"):
        return ["GBP", "USD"]
    return ["USD"]


def evaluate_event_impact(event: Dict[str, Any]) -> Tuple[float, str]:
    """
    Evaluate actual vs forecast/previous for a single economic event.
    Returns (score_delta, reason) where score_delta ranges from -1.0 to +1.0.
    """
    title = (event.get("title") or "").lower()
    actual = event.get("actual_num")
    forecast = event.get("forecast_num")
    previous = event.get("previous_num")

    # If actual is not yet released, use neutral or forecast vs previous
    benchmark = forecast if forecast is not None else previous
    if actual is None or benchmark is None:
        return 0.0, f"{event.get('title')}: Données incomplètes (Actual ou Forecast absent)"

    diff = actual - benchmark
    if abs(diff) < 1e-7:
        return 0.0, f"{event.get('title')}: Conforme aux prévisions ({actual})"

    is_inverse = any(inv in title for inv in INVERSE_BEARISH_INDICATORS)
    
    # Check direction
    if is_inverse:
        # Higher Unemployment -> Bearish Currency
        is_bullish = diff < 0
    else:
        # Higher GDP/CPI/NFP -> Bullish Currency
        is_bullish = diff > 0

    impact_weight = 1.0 if event.get("impact") == "High" else 0.5 if event.get("impact") == "Medium" else 0.2

    # Percentage deviation estimate
    base_denom = abs(benchmark) if abs(benchmark) > 1e-5 else 1.0
    pct_diff = abs(diff) / base_denom
    score_magnitude = min(1.0, 0.3 + 0.7 * math.tanh(pct_diff * 5.0)) * impact_weight

    score = score_magnitude if is_bullish else -score_magnitude
    direction_str = "Bullish" if is_bullish else "Bearish"
    reason = f"{event.get('title')} ({event.get('currency')}): Actual {actual} vs Forecast/Prev {benchmark} -> {direction_str} (score {score:+.2f})"

    return score, reason


def calculate_currency_macro_score(currency: str, events: List[Dict[str, Any]]) -> Tuple[float, List[str]]:
    """
    Calculate an aggregated macro bias score (-1.0 to +1.0) for a specific currency based on recent events.
    """
    relevant_events = [e for e in events if (e.get("currency") or "").upper() == currency.upper()]
    if not relevant_events:
        return 0.0, [f"Aucun événement économique récent trouvé pour {currency}."]

    total_weight = 0.0
    weighted_score_sum = 0.0
    reasons = []

    for event in relevant_events:
        score_delta, reason = evaluate_event_impact(event)
        impact = event.get("impact", "Low")
        weight = 3.0 if impact == "High" else 1.5 if impact == "Medium" else 0.5
        
        weighted_score_sum += score_delta * weight
        total_weight += weight
        if abs(score_delta) > 0.01:
            reasons.append(reason)

    if total_weight <= 0:
        return 0.0, [f"Événements neutres pour {currency}."]

    aggregated_score = max(-1.0, min(1.0, weighted_score_sum / total_weight))
    if not reasons:
        reasons.append(f"{currency}: Impact macro globalement neutre / équilibré.")

    return aggregated_score, reasons


def calculate_symbol_macro_bias(symbol: str, events: List[Dict[str, Any]]) -> Tuple[float, str, List[str]]:
    """
    Calculate symbol-level macro bias score and narrative.
    Returns (score, bias_label, reasons) where score is in [-1.0, +1.0].
    """
    currencies = get_currencies_for_symbol(symbol)
    if not currencies:
        return 0.0, "NEUTRAL", ["Symbole non mappé."]

    if len(currencies) == 1:
        c1 = currencies[0]
        score, reasons = calculate_currency_macro_score(c1, events)
        # For Risk Assets (BTC, Tech Stocks, Gold) priced in USD, strong USD score can be restrictive
        if symbol in ["BTC-USD", "ETH-USD", "XAUUSD", "GOLD", "AAPL", "NVDA", "TSLA"]:
            # Strong USD -> Liquidity tightening -> Bearish Risk assets
            final_score = -0.5 * score if c1 == "USD" else score
        else:
            final_score = score
    else:
        # Pair like EURUSD (base=EUR, quote=USD)
        base_curr, quote_curr = currencies[0], currencies[1]
        base_score, base_reasons = calculate_currency_macro_score(base_curr, events)
        quote_score, quote_reasons = calculate_currency_macro_score(quote_curr, events)

        final_score = max(-1.0, min(1.0, base_score - quote_score))
        reasons = base_reasons + quote_reasons

    if final_score >= 0.25:
        bias_label = "BULLISH"
    elif final_score <= -0.25:
        bias_label = "BEARISH"
    else:
        bias_label = "NEUTRAL"

    return final_score, bias_label, reasons
