# main.py
import os
import asyncio
from dotenv import load_dotenv

load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from database.supabase_client import (
    supabase,
    create_pending_signal,
    update_signal_status,
    get_recent_insights,
)
from ai.decision_engine import EmotionlessDecisionEngine
from scrapers.news_geo import fetch_and_push_geopolitical, fetch_fear_greed
from execution.order_executor import execute_validated_order
from utils.market_data import get_last_price
from database.preferences import get_preferences, set_risk as set_user_risk, set_watchlist as set_user_watchlist
from database.knowledge_base import upsert_note, list_notes
from database.broker_credentials import set_broker_credentials, get_broker_credentials, delete_broker_credentials
from execution.risk_guard import can_trade as risk_can_trade
from config import TELEGRAM_BOT_TOKEN
from utils.monitoring import logger, log_and_alert

engine = EmotionlessDecisionEngine()

print("✅ Environnement chargé v2")


# ====================== HANDLERS ======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    try:
        supabase.table("users").upsert({
            "id": str(user.id),
            "username": user.username,
            "first_name": user.first_name,
            "telegram_chat_id": chat_id,
            "paper_mode": True
        }).execute()

        get_preferences(str(user.id))

        await update.message.reply_text(
            f"✅ **Ai-Trade v2** prêt !\n"
            f"Bienvenue {user.first_name} 👋\n\n"
            f"Commandes :\n"
            f"/analyze BTC-USD\n"
            f"/portfolio\n"
            f"/risk 1.5\n"
            f"/watchlist AAPL,MSFT,BTC-USD\n"
            f"/status\n"
            f"/help"
        )
    except Exception as e:
        await update.message.reply_text(f"Erreur d'initialisation : {e}")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        data = query.data or ""
        if ":" not in data:
            await query.edit_message_text("Callback invalide.")
            return

        action, signal_id = data.split(":", 1)

        row = supabase.table("pending_signals").select("*").eq("id", signal_id).execute()
        if not row.data:
            await query.edit_message_text("❌ Signal introuvable.")
            return

        record = row.data[0]
        signal = record.get("signal") or {}

        if action == "approve":
            result = await execute_validated_order(str(record["user_id"]), signal)
            update_signal_status(signal_id, "executed", result)
            await query.edit_message_text(f"✅ APPROUVÉ (PAPER)\nActif : {signal.get('asset')}")
        else:
            update_signal_status(signal_id, "rejected")
            await query.edit_message_text(f"❌ REJETÉ\nActif : {signal.get('asset')}")

    except Exception as e:
        logger.error(f"Button handler error: {e}")
        await query.edit_message_text(f"⚠️ Erreur : {str(e)[:150]}")


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage : /analyze BTC-USD")
        return
    # Tu peux remettre ta fonction complète ici si tu veux
    await update.message.reply_text(f"Analyse de {context.args[0]} en cours...")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Status : Bot en ligne (v2)")


# ====================== MAIN ======================
def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN manquant")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # === Tous les handlers ===
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot démarré avec tous les handlers")
    print("🚀 Bot Telegram v2 en polling...")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()