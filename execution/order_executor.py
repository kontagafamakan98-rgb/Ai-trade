from typing import Dict, Any
from config import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALPACA_BASE_URL,
    PAPER_TRADING,
    DEFAULT_RISK_PCT,
    DEFAULT_PAPER_EQUITY,
)

try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    ALPACA_OK = True
except Exception:
    ALPACA_OK = False


def _normalize_symbol(asset: str) -> str:
    a = asset.upper().strip()
    if a.endswith("-USD"):
        return a.replace("-USD", "USD")
    return a


def compute_qty(signal: dict, equity: float = None, risk_pct: float = None) -> float:
    equity = equity if equity is not None else DEFAULT_PAPER_EQUITY
    risk_pct = risk_pct if risk_pct is not None else DEFAULT_RISK_PCT

    entry = float(signal.get("entry") or 0)
    sl = float(signal.get("stop_loss") or 0)
    if entry <= 0:
        return 1.0

    risk_amount = equity * (risk_pct / 100.0)
    stop_distance = abs(entry - sl) if sl else entry * 0.01
    if stop_distance <= 0:
        return 1.0

    qty = risk_amount / stop_distance
    qty = max(0.001, min(qty, 1000))

    asset = str(signal.get("asset", ""))
    if asset.endswith("-USD") or "BTC" in asset or "ETH" in asset:
        return round(qty, 4)
    return float(max(1, int(qty)))


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
    qty: float = None,
) -> Dict[str, Any]:
    if not PAPER_TRADING:
        return {"error": "Live trading désactivé. PAPER only."}

    equity = float(signal.get("user_equity") or DEFAULT_PAPER_EQUITY)
    risk_pct = float(signal.get("user_risk_pct") or DEFAULT_RISK_PCT)
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
    }

    if not ALPACA_OK or not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        payload["note"] = "Clés Alpaca absentes → simulation pure"
        return payload

    try:
        client = get_alpaca_client()
        symbol = _normalize_symbol(str(signal.get("asset", "")))
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
        }
    except Exception as e:
        payload["status"] = "error"
        payload["error"] = str(e)
        payload["method"] = "alpaca_paper"
        return payload