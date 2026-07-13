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

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    ALPACA_OK = True
except Exception:
    ALPACA_OK = False


def get_user_risk_pct(user_id: str) -> float:
    """Lit le risque personnalisé depuis user_sessions, sinon défaut config."""
    try:
        res = (
            supabase.table("user_sessions")
            .select("risk_params")
            .eq("user_id", str(user_id))
            .limit(1)
            .execute()
        )
        if res.data:
            params = res.data[0].get("risk_params") or {}
            if isinstance(params, dict) and params.get("max_risk_pct") is not None:
                val = float(params["max_risk_pct"])
                if 0.1 <= val <= 10:
                    return val
    except Exception as e:
        print(f"get_user_risk_pct error: {e}")
    return float(DEFAULT_RISK_PCT)


def get_user_equity(user_id: str) -> float:
    """Equity paper par user si stockée, sinon défaut."""
    try:
        res = (
            supabase.table("user_sessions")
            .select("risk_params")
            .eq("user_id", str(user_id))
            .limit(1)
            .execute()
        )
        if res.data:
            params = res.data[0].get("risk_params") or {}
            if isinstance(params, dict) and params.get("equity") is not None:
                eq = float(params["equity"])
                if eq > 0:
                    return eq
    except Exception as e:
        print(f"get_user_equity error: {e}")
    return float(DEFAULT_PAPER_EQUITY)


def _normalize_symbol(asset: str) -> str:
    a = (asset or "").upper().strip()
    if a.endswith("-USD"):
        return a.replace("-USD", "USD")
    if a.endswith("=X"):
        return a
    return a


def compute_qty(signal: dict, equity: float, risk_pct: float) -> float:
    """
    qty ≈ (equity * risk_pct%) / |entry - stop_loss|
    """
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
    # actions ≈ qty entière ; crypto ≈ décimal
    if not asset.endswith("-USD") and "BTC" not in asset and "ETH" not in asset:
        qty = max(1, int(qty))
    else:
        qty = round(qty, 4)

    return qty


def get_alpaca_client():
    return TradingClient(
        api_key=ALPACA_API_KEY,
        secret_key=ALPACA_SECRET_KEY,
        paper=True,
        url_override=ALPACA_BASE_URL if ALPACA_BASE_URL else None,
    )


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

    payload = {
        "status": "simulated_paper",
        "asset": signal.get("asset"),
        "direction": signal.get("direction"),
        "qty": qty,
        "risk_pct": risk_pct,
        "equity": equity,
        "entry": signal.get("entry"),
        "stop_loss": signal.get("stop_loss"),
        "take_profit": signal.get("take_profit"),
        "method": "simulated",
        "user_id": str(user_id),
    }

    if not ALPACA_OK or not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        payload["note"] = "Clés Alpaca absentes → simulation pure (risk user appliqué)"
        return payload

    try:
        client = get_alpaca_client()
        symbol = _normalize_symbol(signal.get("asset", ""))
        side = OrderSide.BUY if signal.get("direction") == "BUY" else OrderSide.SELL

        order = client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
        )
        return {
            "status": "submitted_paper",
            "order_id": str(order.id),
            "symbol": getattr(order, "symbol", symbol),
            "side": str(getattr(order, "side", side)),
            "qty": str(getattr(order, "qty", qty)),
            "risk_pct": risk_pct,
            "equity": equity,
            "method": "alpaca_paper",
            "user_id": str(user_id),
        }
    except Exception as e:
        payload["status"] = "error"
        payload["error"] = str(e)
        payload["method"] = "alpaca_paper"
        return payload