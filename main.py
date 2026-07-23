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
)
from ai.decision_engine import EmotionlessDecisionEngine
from execution.order_executor import execute_validated_order
from utils.monitoring import logger
from config import TELEGRAM_BOT_TOKEN

engine = EmotionlessDecisionEngine()

print("✅ Environnement chargé v2")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot v2 prêt !\nUtilise /analyze BTC-USD")


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
            await query.edit_message_text("Signal introuvable.")
            return

        record = row.data[0]
        signal = record.get("signal") or {}

        if action == "approve":
            result = await execute_validated_order(str(record["user_id"]), signal)
            update_signal_status(signal_id, "executed", result)
            await query.edit_message_text(f"✅ APPROUVÉ\n{signal.get('asset')} {signal.get('direction')}")
        else:
            update_signal_status(signal_id, "rejected")
            await query.edit_message_text(f"❌ REJETÉ\n{signal.get('asset')}")

    except Exception as e:
        logger.error(f"Button error: {e}")
        await query.edit_message_text("Erreur lors du traitement.")


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage : /analyze BTC-USD")
        return

    asset = context.args[0].upper()
    msg = await update.message.reply_text(f"🧠 Analyse de {asset} en cours...")

    try:
        signal = await asyncio.wait_for(
            asyncio.to_thread(engine.analyze, asset), 
            timeout=30.0
        )

        if not signal:
            await msg.edit_text(f"🤔 Pas de signal fort pour {asset}.")
            return

        user_id = str(update.effective_user.id)
        signal_id = create_pending_signal(user_id, signal)

        text = (
            f"🚨 PROPOSITION IA\n\n"
            f"Actif : {signal.get('asset')}\n"
            f"Direction : {signal.get('direction')}\n"
            f"Confiance : {signal.get('confidence'):.2f}\n\n"
            f"Entry : {signal.get('entry')}\n"
            f"SL : {signal.get('stop_loss')}\n"
            f"TP : {signal.get('take_profit')}\n\n"
            f"**Technique**:\n{signal.get('ta_summary', 'N/A')}\n\n"
            f"**Géopolitique**:\n{signal.get('geo_summary', 'N/A')}\n\n"
            f"**Sentiment**:\n{signal.get('sentiment_summary', 'N/A')}\n\n"
            f"**Raisonnement**:\n{signal.get('reasoning', 'N/A')}"
        )

        keyboard = [[
            InlineKeyboardButton("✅ APPROUVER", callback_data=f"approve:{signal_id}"),
            InlineKeyboardButton("❌ REJETER", callback_data=f"reject:{signal_id}"),
        ]]

        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

    except asyncio.TimeoutError:
        await msg.edit_text("⏱️ Analyse trop longue.")
    except Exception as e:
        logger.error(f"Analyze error: {e}")
        await msg.edit_text(f"❌ Erreur : {str(e)[:150]}")


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("🚀 Bot Telegram v2 démarré")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()