import asyncio
import os
import threading

import uvicorn
from fastapi import FastAPI

from main import main as run_bot
from workers.auto_loop import run_forever

# Mini API pour que Render Free accepte le service
api = FastAPI()


@api.get("/")
def root():
    return {"status": "alive", "mode": "paper", "service": "trading-ai"}


@api.get("/health")
def health():
    return {"status": "ok"}


def start_api():
    # Render fournit le port via la variable PORT
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(api, host="0.0.0.0", port=port, log_level="info")


def start_auto_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_forever())


if __name__ == "__main__":
    # 1) Serveur web (obligatoire pour Render Free)
    threading.Thread(target=start_api, daemon=True).start()
    print("✅ Web server lancé (Render Free)")

    # 2) Boucle trading
    threading.Thread(target=start_auto_loop, daemon=True).start()
    print("✅ Auto-loop lancée")

    # 3) Bot Telegram
    print("✅ Bot Telegram en polling...")
    run_bot()