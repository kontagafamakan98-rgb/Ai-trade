# utils/monitoring.py
import logging
from datetime import datetime
from telegram import Bot
from config import TELEGRAM_BOT_TOKEN, ADMIN_CHAT_ID

# Configuration logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)


async def send_admin_alert(message: str, level: str = "INFO"):
    """Envoie une alerte à l'admin Telegram"""
    try:
        if not ADMIN_CHAT_ID:
            return
        text = f"[{level}] {datetime.utcnow().isoformat()} UTC\n\n{message}"
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception as e:
        print(f"Admin alert failed: {e}")


async def log_and_alert(message: str, level: str = "INFO"):
    """Log + alerte admin"""
    if level == "ERROR" or level == "CRITICAL":
        logger.error(message)
        await send_admin_alert(message, level)
    elif level == "WARNING":
        logger.warning(message)
        await send_admin_alert(message, level)
    else:
        logger.info(message)