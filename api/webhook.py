from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional

from database.supabase_client import create_pending_signal, supabase
from ai.decision_engine import EmotionlessDecisionEngine
from notifications.notify import send_signal_to_user
from workers.signal_guard import recently_sent
from config import WEBHOOK_SECRET

app = FastAPI(title="Trading AI Webhook")
engine = EmotionlessDecisionEngine()


class TVAlert(BaseModel):
    secret: Optional[str] = None
    ticker: str
    action: str
    price: Optional[float] = None
    message: Optional[str] = None


@app.get("/health")
def health():
    return {"status": "ok", "mode": "paper"}


@app.post("/webhook/tradingview")
async def tradingview_webhook(
    alert: TVAlert,
    x_webhook_secret: Optional[str] = Header(None),
):
    secret = alert.secret or x_webhook_secret
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    asset = alert.ticker.upper()
    direction = "BUY" if alert.action.lower() in ("buy", "long") else "SELL"

    # Enrichit avec le moteur si possible
    signal = engine.analyze(asset)
    if not signal:
        signal = {
            "asset": asset,
            "direction": direction,
            "entry": alert.price or 0,
            "stop_loss": None,
            "take_profit": None,
            "confidence": 0.7,
            "ta_summary": f"Alerte TradingView: {alert.message or alert.action}",
            "geo_summary": "N/A (webhook)",
            "sentiment_summary": "N/A (webhook)",
            "reasoning": "Signal issu d'une alerte Pine + validation humaine obligatoire.",
        }
    else:
        # respecte la direction TV si tu préfères forcer :
        # signal["direction"] = direction
        pass

    if recently_sent(asset, signal["direction"]):
        return {"ok": True, "skipped": "cooldown", "asset": asset}

    users = supabase.table("users").select("id, telegram_chat_id").eq("paper_mode", True).execute()
    sent = 0
    for u in users.data or []:
        if not u.get("telegram_chat_id"):
            continue
        sid = create_pending_signal(u["id"], signal)
        await send_signal_to_user(u["telegram_chat_id"], signal, sid)
        sent += 1

    return {"ok": True, "asset": asset, "notifications_sent": sent}