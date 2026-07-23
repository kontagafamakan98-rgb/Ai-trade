# utils/monitoring.py
import logging
import structlog
from datetime import datetime
from telegram import Bot
from config import TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID  # Ajoute ADMIN_CHAT_ID dans .env

# Configuration structlog (beaux logs)
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer()
    ]
)
logger = structlog.get_logger()

bot = Bot(token=TELEGRAM_BOT_TOKEN)

async def send_admin_alert(message: str, level: str = "INFO"):
    """Envoie une alerte au canal admin"""
    try:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"[{level}] {datetime.utcnow().isoformat()} UTC\n\n{message}"
        )
    except Exception as e:
        logger.error("admin_alert_failed", error=str(e))

# Exemple d'utilisation :
# await send_admin_alert("Circuit breaker activé sur Groq", "WARNING")