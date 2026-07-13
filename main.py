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
from scrapers.news_geo import fetch_and_push_geopolitical, fetch_fear_greed
from execution.order_executor import execute_validated_order
from config import TELEGRAM_BOT_TOKEN

engine = EmotionlessDecisionEngine()

print("✅ Environnement chargé")


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

        await update.message.reply_text(
            f"✅ Bot Trading IA prêt.\n"
            f"Bienvenue {user.first_name}!\n\n"
            f"Mode : PAPER TRADING uniquement.\n"
            f"L'IA n'exécute rien sans ta validation.\n\n"
            f"Commandes :\n"
            f"/analyze BTC-USD\n"
            f"/analyze AAPL\n"
            f"/refresh_data\n"
            f"/status\n"
            f"/stats\n"
            f"/risk 1.5"
        )
    except Exception as e:
        await update.message.reply_text(f"Erreur DB : {e}")


async def refresh_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Mise à jour des données collectives...")
    try:
        geo = await fetch_and_push_geopolitical()
        fg = await fetch_fear_greed()
        await update.message.reply_text(
            f"✅ Données collectives mises à jour\n"
            f"• News geo : {geo}\n"
            f"• Fear & Greed : {fg}"
        )
    except Exception as e:
        await update.message.reply_text(f"Erreur refresh : {e}")


async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage : /analyze BTC-USD  ou  /analyze AAPL")
        return

    asset = context.args[0].upper()
    await update.message.reply_text(f"🧠 Analyse de {asset} en cours (logique pure)...")

    try:
        signal = engine.analyze(asset)
        print(f">>> Signal brut : {signal}")
    except Exception as e:
        print(f"❌ Erreur engine.analyze : {e}")
        await update.message.reply_text(f"Erreur analyse : {e}")
        return

    # Si aucun signal fort → DEMO pour afficher les boutons
    if not signal:
        print(">>> Pas de signal fort → DEMO (boutons forcés)")
        signal = {
            "asset": asset,
            "direction": "BUY",
            "entry": 0,
            "stop_loss": 0,
            "take_profit": 0,
            "confidence": 0.50,
            "ta_summary": "Aucun signal fort (mode DEMO pour tester les boutons)",
            "geo_summary": "N/A",
            "sentiment_summary": "N/A",
            "reasoning": (
                "Signal de démonstration uniquement. "
                "L'IA n'a pas trouvé d'opportunité assez forte. "
                "Boutons affichés pour valider le workflow paper."
            ),
        }

    user_id = str(update.effective_user.id)

    try:
        signal_id = create_pending_signal(user_id, signal)
        print(f">>> signal_id : {signal_id}")
    except Exception as e:
        print(f"❌ create_pending_signal : {e}")
        signal_id = "demo-local"

    text = (
        f"🚨 PROPOSITION IA — VALIDATION HUMAINE OBLIGATOIRE\n\n"
        f"Actif : {signal.get('asset')}\n"
        f"Direction : {signal.get('direction')}\n"
        f"Confiance : {signal.get('confidence')}\n\n"
        f"Paramètres :\n"
        f"• Entry : {signal.get('entry')}\n"
        f"• Stop Loss : {signal.get('stop_loss')}\n"
        f"• Take Profit : {signal.get('take_profit')}\n\n"
        f"Analyse Technique :\n{signal.get('ta_summary')}\n\n"
        f"Géopolitique :\n{signal.get('geo_summary')}\n\n"
        f"Sentiment :\n{signal.get('sentiment_summary')}\n\n"
        f"Raisonnement :\n{signal.get('reasoning')}\n\n"
        f"⚠️ Aucune exécution sans ton accord."
    )

    keyboard = [[
        InlineKeyboardButton("✅ APPROUVER (paper)", callback_data=f"approve:{signal_id}"),
        InlineKeyboardButton("❌ REJETER", callback_data=f"reject:{signal_id}"),
    ]]

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    print(">>> Message + BOUTONS envoyés")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        data = query.data or ""
        if ":" not in data:
            await query.edit_message_text("Callback invalide.")
            return

        action, signal_id = data.split(":", 1)
        print(f"👉 Callback reçu : action={action} | signal_id={signal_id}")

        row = supabase.table("pending_signals").select("*").eq("id", signal_id).execute()
        if not row.data:
            await query.edit_message_text("❌ Signal introuvable en base.")
            return

        record = row.data[0]
        signal = record.get("signal") or {}
        user_id = record.get("user_id")
        print(f"👉 Signal chargé : {signal.get('asset')} {signal.get('direction')}")

        if action == "approve":
            try:
                result = await execute_validated_order(user_id, signal)
            except Exception as e:
                result = {"status": "error", "error": str(e)}
                print(f"❌ Erreur exécution : {e}")

            try:
                update_signal_status(signal_id, "executed", result)
            except Exception as e:
                print(f"❌ Erreur update status : {e}")

            msg = (
                f"✅ APPROUVÉ (PAPER)\n\n"
                f"Actif : {signal.get('asset')}\n"
                f"Direction : {signal.get('direction')}\n"
                f"Résultat : {result}"
            )
            await query.edit_message_text(msg)

        else:
            try:
                update_signal_status(signal_id, "rejected")
            except Exception as e:
                print(f"❌ Erreur reject : {e}")

            await query.edit_message_text(
                f"❌ REJETÉ\n\nActif : {signal.get('asset')} — aucune action."
            )

    except Exception as e:
        print(f"❌ ERREUR GLOBALE button_handler : {e}")
        try:
            await query.edit_message_text(f"⚠️ Erreur : {str(e)[:300]}")
        except Exception:
            if query.message:
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"⚠️ Erreur handler : {str(e)[:300]}"
                )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        insights = supabase.table("insights").select("id", count="exact").execute()
        signals = supabase.table("pending_signals").select("id", count="exact").execute()
        await update.message.reply_text(
            f"📊 Status système\n"
            f"• Insights collectifs : {insights.count}\n"
            f"• Signaux en base : {signals.count}\n"
            f"• Mode : PAPER TRADING\n"
            f"• Moteur : EmotionlessDecisionEngine"
        )
    except Exception as e:
        await update.message.reply_text(f"Erreur status : {e}")


async def set_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage : /risk 1.5  (pour risquer 1.5% du capital par trade)"
        )
        return
    try:
        new_risk = float(context.args[0].replace(",", "."))
        if new_risk <= 0 or new_risk > 10:
            await update.message.reply_text("❌ Le risque doit être entre 0.1% et 10%.")
            return

        user_id = str(update.effective_user.id)
        supabase.table("user_sessions").upsert({
            "user_id": user_id,
            "risk_params": {"max_risk_pct": new_risk},
            "updated_at": "now()"
        }).execute()

        await update.message.reply_text(
            f"✅ Risque par trade fixé à : {new_risk}% du capital."
        )
    except ValueError:
        await update.message.reply_text("❌ Nombre invalide. Exemple : /risk 1.0")
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur BDD : {e}")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        won = supabase.table("pending_signals").select("id", count="exact").eq("status", "won").execute()
        lost = supabase.table("pending_signals").select("id", count="exact").eq("status", "lost").execute()
        open_pos = supabase.table("pending_signals").select("id", count="exact").eq("status", "executed").execute()

        won_count = won.count or 0
        lost_count = lost.count or 0
        total_closed = won_count + lost_count
        win_rate = (won_count / total_closed * 100) if total_closed > 0 else 0.0

        await update.message.reply_text(
            f"📊 Performance Collective de l'IA\n\n"
            f"• Trades gagnants (TP touché) : {won_count}\n"
            f"• Trades perdants (SL touché) : {lost_count}\n"
            f"• Trades en cours : {open_pos.count or 0}\n\n"
            f"🏆 Win Rate Global : {win_rate:.1f}%\n"
            f"(Mis à jour automatiquement par le Tracker TP/SL)"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur lecture des stats : {e}")


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN manquant dans .env / Render")
        return

    # Fix event loop Render
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("analyze", analyze))
    app.add_handler(CommandHandler("refresh_data", refresh_data))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("risk", set_risk))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("✅ Bot + Moteur IA prêts")
    print("🚀 Polling... (Ctrl+C pour arrêter)")

    # drop_pending_updates aide au redémarrage propre
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()