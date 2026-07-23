from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional
from utils.monitoring import logger, log_and_alert

from database.supabase_client import supabase, create_pending_signal
from notifications.notify import send_signal_to_user
from workers.signal_guard import recently_sent
from config import WEBHOOK_SECRET
from ai.decision_engine import EmotionlessDecisionEngine

app = FastAPI(title="Trading AI Webhook")
_engine = EmotionlessDecisionEngine()


class TVAlert(BaseModel):
    secret: Optional[str] = None
    ticker: str
    action: str
    price: Optional[float] = None
    message: Optional[str] = None


@app.get("/health")
async def health():
    return {"status": "ok", "service": "webhook", "version": "2.0"}


@app.post("/tradingview")
async def tradingview_webhook(alert: TVAlert, x_webhook_secret: Optional[str] = Header(None)):
    try:
        secret = alert.secret or x_webhook_secret
        if secret != WEBHOOK_SECRET:
            logger.warning("invalid_webhook_secret")
            raise HTTPException(status_code=401, detail="Invalid secret")

        asset = alert.ticker.upper()
        logger.info("webhook_received", asset=asset, action=alert.action)

        if recently_sent(asset, alert.action.upper()):
            return {"ok": True, "skipped": "cooldown"}

        # ... (le reste de ta logique existante pour créer le signal)

        return {"ok": True, "asset": asset}

    except Exception as e:
        await log_and_alert(f"Webhook error on {alert.ticker}: {e}", "ERROR")
        raise HTTPException(status_code=500, detail=str(e))