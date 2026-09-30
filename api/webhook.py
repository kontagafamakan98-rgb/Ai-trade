from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from typing import Optional

from api.security import constant_time_equals
from database.supabase_client import supabase, create_pending_signal
from workers.signal_guard import recently_sent
from config import WEBHOOK_SECRET
from core.alert_engine import build_signal, EXTERNAL_SOURCE
from core.config_runtime import public_secrets_audit, safe_preflight
from core.signal_quality import validate_signal
from api.macro_router import router as macro_router
from api.learning_router import router as learning_router
from api.consensus_router import router as consensus_router
from api.reports_router import router as reports_router
from api.media_router import router as media_router
from api.admin_router import router as admin_router

app = FastAPI(title="Trading AI Webhook")
app.include_router(macro_router)
app.include_router(learning_router)
app.include_router(consensus_router)
app.include_router(reports_router)
app.include_router(media_router)
app.include_router(admin_router)


class Alert(BaseModel):
    """Alerte technique entrante (scanner interne ou source externe gratuite).

    Le schéma est volontairement agnostique du fournisseur : n'importe quelle
    source capable de produire un signal BUY/SELL avec entrée + stop + objectif
    peut pousser vers `/webhook/alert` (Pine Script d'un autre outil, n8n, un
    script maison…). Le backend n'exige plus TradingView.
    """

    secret: Optional[str] = None
    ticker: str
    action: str
    price: Optional[float] = None
    message: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@app.get("/health")
def health():
    # Réponse volontairement minimale : ne pas exposer publiquement quelles
    # clés d'API sont configurées (voir /preflight, protégé). L'audit des secrets
    # n'y publie que son **plafond** et l'état de son registre — jamais le chemin
    # du fichier ni le rôle de la clé Supabase, qui disent quelle puissance le
    # service porte.
    preflight = safe_preflight()
    return {
        "status": "ok" if preflight.get("ok") else "degraded",
        "ready": bool(preflight.get("ok")),
        "mode": "paper",
        "service": "trading-ai",
        "secrets_audit": public_secrets_audit(preflight),
    }


@app.get("/")
def root():
    return {"status": "alive", "alert": "/webhook/alert (monté depuis run.py)"}


@app.post("/alert")
async def alert_webhook(
    alert: Alert,
    x_webhook_secret: Optional[str] = Header(None),
):
    """Ingestion HTTP d'une alerte (moteur interne ou source externe gratuite)."""
    # Sécurité : comparaison à temps constant pour éviter une attaque par timing.
    secret = alert.secret or x_webhook_secret
    if not constant_time_equals(secret, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    asset = alert.ticker.upper()
    signal = build_signal(
        ticker=alert.ticker,
        action=alert.action,
        price=alert.price,
        stop_loss=alert.stop_loss,
        take_profit=alert.take_profit,
        message=alert.message,
        source=EXTERNAL_SOURCE,
    )

    valid, notes, signal = validate_signal(signal, allow_demo=True)
    if not valid:
        return {"ok": False, "skipped": "invalid_signal", "asset": asset, "notes": notes}

    # Anti-spam
    if recently_sent(asset, signal["direction"]):
        return {"ok": True, "skipped": "cooldown", "asset": asset}

    # Envoie à TOUS les utilisateurs paper actifs
    users = supabase.table("users").select("id, telegram_chat_id").eq("paper_mode", True).execute()

    # Import paresseux : ce module ne doit dépendre ni de `python-telegram-bot`
    # ni d'un token de bot valide pour être importable (il est chargé par `run.py`
    # au démarrage du serveur web). Une panne Telegram ne doit pas empêcher le
    # web de démarrer ni faire tomber l'endpoint entier.
    try:
        from notifications.notify import send_signal_to_user
    except Exception as e:
        print(f"Webhook : notifications Telegram indisponibles ({e})")
        return {
            "ok": True,
            "asset": asset,
            "notifications_sent": 0,
            "skipped": "notifications_unavailable",
        }

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
