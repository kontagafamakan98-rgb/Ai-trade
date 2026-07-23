# main.py
import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

from database.supabase_client import supabase, create_pending_signal
from ai.decision_engine import EmotionlessDecisionEngine
from execution.order_executor import execute_validated_order
from utils.monitoring import logger
from config import TELEGRAM_BOT_TOKEN

engine = EmotionlessDecisionEngine()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot v2 prêt !\n/analyze BTC-USD pour tester.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (garde ta version)
    pass


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage : /analyze BTC-USD")
        return

    asset = context.args[0].upper()
    msg = await update.message.reply_text(f"🧠 Analyse de {asset} en cours...")

    try:
        # Timeout pour éviter blocage
        signal = await asyncio.wait_for(
            asyncio.to_thread(engine.analyze, asset), 
            timeout=25.0
        )

        if not signal:
            await msg.edit_text(f"🤔 Pas de signal fort pour {asset} actuellement.")
            return

        signal_id = create_pending_signal(str(update.effective_user.id), signal)

        text = f"🚨 Signal {asset} | Direction : {signal['direction']} | Confiance : {signal['confidence']}"
        # Ajoute boutons ici si tu veux

        await msg.edit_text(text)

    except asyncio.TimeoutError:
        await msg.edit_text("⏱️ Analyse trop longue. Réessaie plus tard.")
    except Exception as e:
        logger.error(f"Analyze error: {e}")
        await msg.edit_text(f"❌ Erreur analyse : {str(e)[:100]}")


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("🚀 Bot démarré (polling)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()