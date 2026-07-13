import asyncio
import os
import threading

import uvicorn
from fastapi import FastAPI

from main import main as run_bot
from workers.auto_loop import run_forever
from api.webhook import app as webhook_app

api = FastAPI()

# Monte le webhook sous /webhook
api.mount("/webhook", webhook_app)


@api.get("/")
def root():
    return {"status": "alive", "mode": "paper", "service": "trading-ai"}


@api.get("/health")
def health():
    return {"status": "ok"}


def start_api():
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(api, host="0.0.0.0", port=port, log_level="info", loop="asyncio")


def start_auto_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_forever())


if __name__ == "__main__":
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