from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, Dict, Any
import os

from database.supabase_client import supabase, create_pending_signal
from notifications.notify import send_signal_to_user
from workers.signal_guard import recently_sent
from config import WEBHOOK_SECRET

app = FastAPI(title="Trading AI Webhook")


class TVAlert(BaseModel):
    secret: Optional[str] = None
    ticker: str
    action: str
    price: Optional[float] = None
    message: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


def _build_signal(alert: TVAlert) -> Dict[str, Any]:
    direction = "BUY" if alert.action.lower() in ("buy", "long") else "SELL"
    return {
        "asset": alert.ticker.upper(),
        "direction": direction,
        "entry": alert.price or 0,
        "stop_loss": alert.stop_loss,
        "take_profit": alert.take_profit,
        "confidence": 0.70,
        "ta_summary": f"Alerte TradingView: {alert.message or alert.action}",
        "geo_summary": "N/A (webhook)",
        "sentiment_summary": "N/A (webhook)",
        "reasoning": (
            "Signal issu d'une alerte Pine Script TradingView. "
            "Validation humaine obligatoire avant exécution."
        ),
        "source": "tradingview_webhook",
    }


@app.get("/health")
def health():
    return {"status": "ok", "mode": "paper", "service": "trading-ai"}


@app.get("/")
def root():
    return {"status": "alive", "webhook": "/webhook/tradingview (monté depuis run.py)"}


@app.post("/tradingview")
async def tradingview_webhook(
    alert: TVAlert,
    x_webhook_secret: Optional[str] = Header(None),
):
    # Sécurité
    secret = alert.secret or x_webhook_secret
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    asset = alert.ticker.upper()
    signal = _build_signal(alert)

    # Anti-spam
    if recently_sent(asset, signal["direction"]):
        return {"ok": True, "skipped": "cooldown", "asset": asset}

    # Envoie à TOUS les utilisateurs paper actifs
    users = supabase.table("users").select("id, telegram_chat_id").eq("paper_mode", True).execute()
    sent = 0
    for u in (users.data or []):
        if not u.get("telegram_chat_id"):
            continue
        try:
            signal_id = create_pending_signal(u["id"], signal)
            await send_signal_to_user(u["telegram_chat_id"], signal, signal_id)
            sent += 1
        except Exception as e:
            print(f"Webhook notify error user {u['id']}: {e}")

    return {"ok": True, "asset": asset, "notifications_sent": sent}