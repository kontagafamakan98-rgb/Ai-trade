from typing import Dict, Any, Optional
from config import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALPACA_BASE_URL,
    PAPER_TRADING,
    DEFAULT_RISK_PCT,
    DEFAULT_PAPER_EQUITY,
)
from database.supabase_client import supabase
from database.preferences import get_preferences

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    ALPACA_OK = True
except Exception:
    ALPACA_OK = False


def get_user_risk_pct(user_id: str) -> float:
    try:
        prefs = get_preferences(str(user_id))
        val = float(prefs.get("risk_pct") or DEFAULT_RISK_PCT)
        if 0.1 <= val <= 10:
            return val
    except Exception as e:
        print(f"get_user_risk_pct error: {e}")
    return float(DEFAULT_RISK_PCT)


def get_user_equity(user_id: str) -> float:
    try:
        prefs = get_preferences(str(user_id))
        eq = float(prefs.get("paper_equity") or DEFAULT_PAPER_EQUITY)
        if eq > 0:
            return eq
    except Exception as e:
        print(f"get_user_equity error: {e}")
    return float(DEFAULT_PAPER_EQUITY)


def _normalize_symbol(asset: str) -> str:
    """Alpaca stocks: AAPL | crypto souvent BTC/USD selon compte."""
    a = (asset or "").upper().strip()
    if a.endswith("-USD"):
        # format crypto yfinance → essai BTC/USD
        base = a.replace("-USD", "")
        return f"{base}/USD"
    if a.endswith("USD") and len(a) > 3 and not a.isalpha():
        return a
    # action pure
    return a.replace("-USD", "").replace("/USD", "")


def compute_qty(signal: dict, equity: float, risk_pct: float) -> float:
    entry = float(signal.get("entry") or 0)
    sl = float(signal.get("stop_loss") or 0)

    if entry <= 0:
        return 1.0

    risk_amount = equity * (risk_pct / 100.0)
    stop_distance = abs(entry - sl) if sl else entry * 0.01
    if stop_distance <= 0:
        stop_distance = entry * 0.01

    qty = risk_amount / stop_distance
    qty = max(0.001, min(qty, 1000))

    asset = str(signal.get("asset", ""))
    if not asset.endswith("-USD") and "BTC" not in asset and "ETH" not in asset:
        qty = max(1, int(qty))
    else:
        qty = round(qty, 6)

    return qty


def get_alpaca_client():
    # paper=True suffit : le SDK alpaca-py construit lui-même la bonne URL
    # (https://paper-api.alpaca.markets/v2). Passer url_override en plus
    # casse le routing interne (404 Not Found sur toutes les routes).
    return TradingClient(
        api_key=ALPACA_API_KEY,
        secret_key=ALPACA_SECRET_KEY,
        paper=True,
    )


def _is_demo_or_invalid(signal: dict) -> bool:
    entry = float(signal.get("entry") or 0)
    ta = str(signal.get("ta_summary") or "")
    reasoning = str(signal.get("reasoning") or "")
    if entry <= 0:
        return True
    if "DEMO" in ta.upper() or "démonstration" in reasoning.lower() or "demonstration" in reasoning.lower():
        return True
    return False


async def execute_validated_order(
    user_id: str,
    signal: Dict[str, Any],
    qty: Optional[float] = None,
) -> Dict[str, Any]:
    if not PAPER_TRADING:
        return {"error": "Live trading désactivé. PAPER only."}

    risk_pct = get_user_risk_pct(user_id)
    equity = get_user_equity(user_id)

    if qty is None:
        qty = compute_qty(signal, equity=equity, risk_pct=risk_pct)

    base = {
        "asset": signal.get("asset"),
        "direction": signal.get("direction"),
        "qty": qty,
        "risk_pct": risk_pct,
        "equity": equity,
        "entry": signal.get("entry"),
        "stop_loss": signal.get("stop_loss"),
        "take_profit": signal.get("take_profit"),
        "user_id": str(user_id),
    }

    # DEMO / entry=0 → simulation pure, PAS d'appel Alpaca
    if _is_demo_or_invalid(signal):
        return {
            **base,
            "status": "simulated_paper",
            "method": "simulated",
            "note": "Signal DEMO ou entry invalide → pas d'envoi broker. Risk user quand même appliqué.",
        }

    if not ALPACA_OK or not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return {
            **base,
            "status": "simulated_paper",
            "method": "simulated",
            "note": "Clés Alpaca absentes → simulation pure (risk user appliqué)",
        }

    try:
        client = get_alpaca_client()
        symbol = _normalize_symbol(signal.get("asset", ""))
        side = OrderSide.BUY if signal.get("direction") == "BUY" else OrderSide.SELL

        # Stocks US: AAPL — qty entière
        order_qty = qty
        if "/" not in symbol and symbol.isalpha():
            order_qty = max(1, int(float(qty)))

        order = client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=order_qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
        )
        return {
            **base,
            "status": "submitted_paper",
            "order_id": str(order.id),
            "symbol": getattr(order, "symbol", symbol),
            "side": str(getattr(order, "side", side)),
            "qty": str(getattr(order, "qty", order_qty)),
            "method": "alpaca_paper",
        }
    except Exception as e:
        return {
            **base,
            "status": "error",
            "error": str(e),
            "method": "alpaca_paper",
            "note": "Échec Alpaca — risk_pct déjà calculé. Vérifie clés paper + symbole + marché ouvert.",
        }