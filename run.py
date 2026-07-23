import asyncio
import os
import threading
import uvicorn
from fastapi import FastAPI

from main import main as run_bot
from workers.auto_loop import run_forever
from api.webhook import app as webhook_app
from utils.monitoring import logger, log_and_alert

api = FastAPI(title="Ai-Trade Improved")

# Monte le webhook
api.mount("/webhook", webhook_app)


@api.get("/")
@api.get("/health")
async def health():
    return {
        "status": "healthy",
        "mode": "paper",
        "version": "2.0-improved",
        "service": "ai-trading"
    }


def start_api():
    port = int(os.getenv("PORT", "10000"))
    logger.info("api_server_starting", port=port)
    uvicorn.run(api, host="0.0.0.0", port=port, log_level="info")


def start_auto_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_forever())
    except Exception as e:
        asyncio.run(log_and_alert(f"Auto-loop crashed: {e}", "CRITICAL"))


if __name__ == "__main__":
    logger.info("ai_trade_bot_starting", version="2.0")

    try:
        # Démarrage des services en parallèle
        threading.Thread(target=start_api, daemon=True).start()
        threading.Thread(target=start_auto_loop, daemon=True).start()

        logger.info("services_started")
        print("✅ Ai-Trade v2 (avec monitoring) démarré")
        
        run_bot()

    except Exception as e:
        asyncio.run(log_and_alert(f"Bot crashed on startup: {e}", "CRITICAL"))
        logger.critical("startup_failed", error=str(e))