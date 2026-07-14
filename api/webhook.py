from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional, Dict, Any
import os

from database.supabase_client import supabase, create_pending_signal, get_recent_insights
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
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


def _build_signal(alert: TVAlert) -> Dict[str, Any]:
    direction = "BUY" if alert.action.lower() in ("buy", "long") else "SELL"
    asset = alert.ticker.upper()

    try:
        insights = get_recent_insights(limit=20)
        llm_result = _engine._news_cache.get(asset, insights)
        if llm_result:
            geo_txt = f"Analyse IA : {llm_result['reasoning']}"
            sent_txt = f"Biais IA : {llm_result['bias']} (score {llm_result['score']:.2f})"
        else:
            _, geo_txt = _engine._score_geo(insights)
            _, sent_txt = _engine._score_sentiment(insights)
            geo_txt += " [fallback: clé LLM absente ou erreur]"
    except Exception:
        geo_txt, sent_txt = "Indisponible", "Indisponible"

    return {
        "asset": alert.ticker.upper(),
        "direction": direction,
        "entry": alert.price or 0,
        "stop_loss": alert.stop_loss,
        "take_profit": alert.take_profit,
        "confidence": 0.70,
        "ta_summary": f"Alerte TradingView: {alert.message or alert.action}",
        "geo_summary": geo_txt,
        "sentiment_summary": sent_txt,
        "reasoning": (
            "Signal issu d'une alerte Pine Script TradingView (TA calculée côté "
            "TradingView). Contexte géo/sentiment ajouté par le backend à titre "
            "informatif. Validation humaine obligatoire avant exécution."
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