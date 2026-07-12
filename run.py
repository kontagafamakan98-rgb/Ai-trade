import asyncio
import threading
import uvicorn

from main import main as run_bot
from workers.auto_loop import run_forever
from api.webhook import app


def start_auto_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_forever())


def start_api():
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")


if __name__ == "__main__":
    threading.Thread(target=start_auto_loop, daemon=True).start()
    print("✅ Worker 24/7 lancé")

    threading.Thread(target=start_api, daemon=True).start()
    print("✅ API webhook sur http://127.0.0.1:8000")
    print("   Health : http://127.0.0.1:8000/health")
    print("   TV POST: /webhook/tradingview")

    print("✅ Bot Telegram...")
    run_bot()