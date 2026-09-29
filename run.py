import asyncio
import os
import threading

import uvicorn
from fastapi import FastAPI

from main import main as run_bot
from workers.auto_loop import run_forever
from api.webhook import app as webhook_app
from core.config_runtime import safe_preflight, enforce_secure_config

api = FastAPI()

# Monte le webhook sous /webhook
api.mount("/webhook", webhook_app)


@api.get("/")
@api.head("/")
def root():
    preflight = safe_preflight()
    return {
        "status": "alive",
        "mode": "paper",
        "service": "trading-ai",
        "ready": preflight.get("ok", False),
    }


@api.get("/health")
@api.head("/health")
def health():
    preflight = safe_preflight()
    return {
        "status": "ok" if preflight.get("ok") else "degraded",
        "ready": bool(preflight.get("ok")),
    }


def start_api():
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(api, host="0.0.0.0", port=port, log_level="info", loop="asyncio")


def start_auto_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_forever())


if __name__ == "__main__":
    # Fail-closed : on refuse de démarrer si les secrets de sécurité sont
    # absents ou laissés aux valeurs par défaut.
    enforce_secure_config()

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    threading.Thread(target=start_api, daemon=True).start()
    print("✅ Web server lancé (Render Free)")

    threading.Thread(target=start_auto_loop, daemon=True).start()
    print("✅ Auto-loop lancée")

    print("✅ Bot Telegram en polling...")
    run_bot()
