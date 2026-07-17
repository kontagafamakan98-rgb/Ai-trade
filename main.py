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
from database.broker_credentials import set_broker_credentials, get_broker_credentials, delete_broker_credentials
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

        get_preferences(str(user.id))  # crée la ligne de préférences par défaut si absente

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
            f"/risk 1.5\n"
            f"/watchlist AAPL,MSFT,BTC-USD"
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

    # DEMO avec VRAI prix si pas de signal fort
    if not signal:
        print(">>> Pas de signal fort → DEMO avec prix live")
        price = get_last_price(asset) or 0.0
        if price and price > 0:
            sl = round(price * 0.99, 5)
            tp = round(price * 1.02, 5)
        else:
            sl = 0
            tp = 0
        try:
            insights = get_recent_insights(limit=20)
            llm_result = engine._news_cache.get(asset, insights)
            if llm_result:
                geo_txt = f"Analyse IA : {llm_result['reasoning']}"
                sent_txt = f"Biais IA : {llm_result['bias']} (score {llm_result['score']:.2f})"
            else:
                _, geo_txt = engine._score_geo(insights)
                _, sent_txt = engine._score_sentiment(insights)
                geo_txt += " [fallback: clé LLM absente ou erreur]"
        except Exception:
            geo_txt, sent_txt = "Indisponible", "Indisponible"

        signal = {
            "asset": asset,
            "direction": "BUY",
            "entry": price,
            "stop_loss": sl,
            "take_profit": tp,
            "confidence": 0.50,
            "ta_summary": "DEMO — aucun signal fort (workflow paper)",
            "geo_summary": geo_txt,
            "sentiment_summary": sent_txt,
            "reasoning": (
                "Signal de démonstration uniquement (aucun croisement RSI/EMA "
                "assez marqué actuellement). Contexte géo/sentiment réel affiché "
                "à titre informatif. Boutons pour tester le workflow de validation. "
                "Pas un trade réel du moteur."
            ),
            "is_demo": True,
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
        print(f"❌ ERREUR button_handler : {e}")
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
            f"• Mode : PAPER TRADING"
        )
    except Exception as e:
        await update.message.reply_text(f"Erreur status : {e}")


async def set_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage : /risk 1.5")
        return
    try:
        new_risk = float(context.args[0].replace(",", "."))
        if new_risk <= 0 or new_risk > 5:
            await update.message.reply_text("❌ Risque entre 0.1% et 5% (plafond paper trading).")
            return
        user_id = str(update.effective_user.id)
        set_user_risk(user_id, new_risk)
        await update.message.reply_text(f"✅ Risque par trade fixé à : {new_risk}%")
    except ValueError:
        await update.message.reply_text("❌ Nombre invalide. Ex: /risk 1.0")
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur BDD : {e}")


async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not context.args:
        prefs = get_preferences(user_id)
        current = prefs.get("watchlist") or []
        await update.message.reply_text(
            f"📋 Ta watchlist actuelle :\n{', '.join(current)}\n\n"
            f"Pour la changer : /watchlist AAPL,MSFT,BTC-USD"
        )
        return

    raw = " ".join(context.args)
    assets = [a.strip() for a in raw.split(",") if a.strip()]
    if not assets:
        await update.message.reply_text("❌ Liste invalide. Ex: /watchlist AAPL,MSFT,BTC-USD")
        return

    prefs = set_user_watchlist(user_id, assets)
    await update.message.reply_text(
        f"✅ Watchlist mise à jour :\n{', '.join(prefs.get('watchlist') or [])}"
    )


async def test_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    asset = context.args[0].upper() if context.args else "AAPL"
    direction = context.args[1].upper() if len(context.args) > 1 else "BUY"
    if direction not in ("BUY", "SELL"):
        direction = "BUY"

    price = get_last_price(asset)
    if not price or price <= 0:
        await update.message.reply_text(f"❌ Impossible de récupérer le prix de {asset} pour le moment.")
        return

    if direction == "BUY":
        sl, tp = round(price * 0.99, 5), round(price * 1.02, 5)
    else:
        sl, tp = round(price * 1.01, 5), round(price * 0.98, 5)

    signal = {
        "asset": asset,
        "direction": direction,
        "entry": price,
        "stop_loss": sl,
        "take_profit": tp,
        "confidence": 1.0,
        "ta_summary": "Ordre de TEST manuel (déclenché volontairement, pas un vrai signal du moteur IA)",
        "geo_summary": "N/A — test manuel",
        "sentiment_summary": "N/A — test manuel",
        "reasoning": (
            "Test manuel explicite via /test_order, pour vérifier ta connexion "
            "broker personnelle. Prix réel utilisé, mais ce n'est pas une "
            "recommandation du moteur IA."
        ),
    }

    signal_id = create_pending_signal(str(update.effective_user.id), signal)

    text = (
        f"🚨 PROPOSITION — TEST MANUEL (VALIDATION HUMAINE OBLIGATOIRE)\n\n"
        f"Actif : {signal.get('asset')}\n"
        f"Direction : {signal.get('direction')}\n"
        f"Confiance : {signal.get('confidence')}\n\n"
        f"Paramètres :\n"
        f"• Entry : {signal.get('entry')}\n"
        f"• Stop Loss : {signal.get('stop_loss')}\n"
        f"• Take Profit : {signal.get('take_profit')}\n\n"
        f"Analyse Technique :\n{signal.get('ta_summary')}\n\n"
        f"Raisonnement :\n{signal.get('reasoning')}\n\n"
        f"⚠️ Aucune exécution sans ton accord."
    )
    keyboard = [[
        InlineKeyboardButton("✅ APPROUVER (paper)", callback_data=f"approve:{signal_id}"),
        InlineKeyboardButton("❌ REJETER", callback_data=f"reject:{signal_id}"),
    ]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        won = supabase.table("pending_signals").select("id", count="exact").eq("status", "won").execute()
        lost = supabase.table("pending_signals").select("id", count="exact").eq("status", "lost").execute()
        open_pos = supabase.table("pending_signals").select("id", count="exact").eq("status", "executed").execute()
        won_count = won.count or 0
        lost_count = lost.count or 0
        total = won_count + lost_count
        wr = (won_count / total * 100) if total else 0.0
        await update.message.reply_text(
            f"📊 Performance Collective\n"
            f"• Gagnés : {won_count}\n"
            f"• Perdus : {lost_count}\n"
            f"• En cours : {open_pos.count or 0}\n"
            f"🏆 Win Rate : {wr:.1f}%"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur stats : {e}")


async def connect_broker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Sécurité : seulement en message privé, jamais dans un groupe (où
    # d'autres personnes verraient les clés en clair).
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "⚠️ Pour ta sécurité, cette commande ne fonctionne qu'en message "
            "privé avec le bot, jamais dans un groupe."
        )
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage : /connect_broker TA_CLE_API TON_SECRET [live]\n\n"
            "⚠️ Envoie ce message uniquement ici en privé. Supprime-le juste "
            "après l'envoi (appui long sur le message → Supprimer) — je "
            "n'ai besoin de le voir qu'une fois pour chiffrer tes clés.\n\n"
            "Par défaut le compte est traité comme PAPER (simulation). "
            "Ajoute 'live' à la fin uniquement si tu es sûr de vouloir du "
            "trading réel — et seulement si tu as déjà un compte Alpaca "
            "live vérifié et financé."
        )
        return

    api_key = context.args[0]
    api_secret = context.args[1]
    is_live = len(context.args) > 2 and context.args[2].lower() == "live"

    try:
        user_id = str(update.effective_user.id)
        set_broker_credentials(user_id, api_key, api_secret, paper=not is_live)
        mode = "LIVE (argent réel)" if is_live else "PAPER (simulation)"
        await update.message.reply_text(
            f"✅ Compte broker connecté et chiffré. Mode : {mode}\n\n"
            f"🗑️ Supprime maintenant ton message précédent contenant tes clés "
            f"en clair — je n'en ai plus besoin, elles sont chiffrées en base."
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur lors de la connexion : {e}")


async def disconnect_broker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    try:
        delete_broker_credentials(user_id)
        await update.message.reply_text("✅ Compte broker déconnecté et supprimé de la base.")
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur : {e}")


async def broker_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    creds = get_broker_credentials(user_id)
    if not creds:
        await update.message.reply_text(
            "🔌 Aucun compte broker personnel connecté.\n"
            "Tes ordres utilisent le compte partagé du bot (tests uniquement).\n\n"
            "Pour connecter le tien : /connect_broker TA_CLE TON_SECRET"
        )
        return
    mode = "PAPER (simulation)" if creds["paper"] else "⚠️ LIVE (argent réel)"
    masked = creds["api_key"][:4] + "•" * 8 + creds["api_key"][-4:] if len(creds["api_key"]) > 8 else "••••"
    await update.message.reply_text(
        f"🔌 Compte broker connecté : {creds['broker']}\n"
        f"Mode : {mode}\n"
        f"Clé : {masked}"
    )


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN manquant")
        return

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
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("test_order", test_order))
    app.add_handler(CommandHandler("connect_broker", connect_broker))
    app.add_handler(CommandHandler("disconnect_broker", disconnect_broker))
    app.add_handler(CommandHandler("broker_status", broker_status))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("✅ Bot + Moteur IA prêts")
    print("🚀 Polling...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
    )


if __name__ == "__main__":
    main()