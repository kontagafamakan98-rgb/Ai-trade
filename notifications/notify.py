from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from config import TELEGRAM_BOT_TOKEN

bot = Bot(token=TELEGRAM_BOT_TOKEN)


async def send_signal_to_user(chat_id: int, signal: dict, signal_id: str):
    text = (
        f"🚨 PROPOSITION IA AUTO — VALIDATION OBLIGATOIRE\n\n"
        f"Actif : {signal.get('asset')}\n"
        f"Direction : {signal.get('direction')}\n"
        f"Confiance : {signal.get('confidence')}\n\n"
        f"Entry : {signal.get('entry')}\n"
        f"Stop Loss : {signal.get('stop_loss')}\n"
        f"Take Profit : {signal.get('take_profit')}\n\n"
        f"TA : {signal.get('ta_summary')}\n"
        f"Geo : {signal.get('geo_summary')}\n"
        f"Sentiment : {signal.get('sentiment_summary')}\n\n"
        f"Raisonnement : {signal.get('reasoning')}\n\n"
        f"⚠️ Rien n'est exécuté sans ton accord."
    )

    keyboard = [[
        InlineKeyboardButton("✅ APPROUVER (paper)", callback_data=f"approve:{signal_id}"),
        InlineKeyboardButton("❌ REJETER", callback_data=f"reject:{signal_id}"),
    ]]

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
