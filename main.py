async def analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage : /analyze BTC-USD")
        return

    asset = context.args[0].upper()
    msg = await update.message.reply_text(f"🧠 Analyse de {asset} en cours (logique pure)...")

    try:
        signal = await asyncio.wait_for(
            asyncio.to_thread(engine.analyze, asset), 
            timeout=30.0
        )

        if not signal:
            await msg.edit_text(f"🤔 Pas de signal fort pour {asset} actuellement.")
            return

        user_id = str(update.effective_user.id)
        signal_id = create_pending_signal(user_id, signal)

        text = (
            f"🚨 PROPOSITION IA — VALIDATION HUMAINE OBLIGATOIRE\n\n"
            f"Actif : {signal.get('asset')}\n"
            f"Direction : {signal.get('direction')}\n"
            f"Confiance : {signal.get('confidence'):.2f}\n\n"
            f"Paramètres :\n"
            f"• Entry : {signal.get('entry')}\n"
            f"• Stop Loss : {signal.get('stop_loss')}\n"
            f"• Take Profit : {signal.get('take_profit')}\n\n"
            f"Analyse Technique :\n{signal.get('ta_summary', 'N/A')}\n\n"
            f"Géopolitique :\n{signal.get('geo_summary', 'N/A')}\n\n"
            f"Sentiment :\n{signal.get('sentiment_summary', 'N/A')}\n\n"
            f"Raisonnement :\n{signal.get('reasoning', 'N/A')}\n\n"
            f"⚠️ Aucune exécution sans ton accord."
        )

        keyboard = [[
            InlineKeyboardButton("✅ APPROUVER (paper)", callback_data=f"approve:{signal_id}"),
            InlineKeyboardButton("❌ REJETER", callback_data=f"reject:{signal_id}"),
        ]]

        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    except asyncio.TimeoutError:
        await msg.edit_text("⏱️ Analyse trop longue (timeout). Réessaie.")
    except Exception as e:
        logger.error(f"Analyze error for {asset}: {e}")
        await msg.edit_text(f"❌ Erreur lors de l'analyse : {str(e)[:150]}")