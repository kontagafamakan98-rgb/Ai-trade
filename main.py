import io
import os
import asyncio
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
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
from execution.order_executor import (
    AlpacaSDKUnavailable,
    execute_validated_order,
    get_alpaca_client,
    get_user_equity,
)
from utils.market_data import get_last_price
from database.preferences import get_preferences, set_risk as set_user_risk, set_watchlist as set_user_watchlist
from database.knowledge_base import (
    SEARCH_POOL_SIZE,
    upsert_note,
    list_notes,
    parse_search_args,
    search_knowledge,
    format_search_results,
    format_search_more,
    format_rag_selection,
)
from core import rag_loop, search_history
from notifications import telegram_channels, telegram_filters, telegram_media
from database.broker_credentials import (
    BrokerCredentialsUnreadable,
    set_broker_credentials,
    get_broker_credentials,
    delete_broker_credentials,
)
from execution.risk_guard import can_trade as risk_can_trade, _get_or_init_state as risk_get_state
from config import TELEGRAM_ADMIN_CHAT_ID, TELEGRAM_BOT_TOKEN
from reports.performance_report import build_performance_summary, export_run_card
from core.config_runtime import enforce_secure_config

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
            f"/report\n"
            f"/risk 1.5\n"
            f"/watchlist AAPL,MSFT,BTC-USD\n"
            f"/search niveaux de support bitcoin\n"
            f"/search_more (passages suivants, sans relancer la recherche)\n"
            f"/use BTC-USD 1,3 (analyse avec ces passages validés, tout de suite)\n"
            f"/search --help (options : --asset, --source, -n)\n"
            f"/media (derniers médias, avec « ▶️ Suivants » pour descendre la liste)\n"
            f"/pending (extractions sans verdict : celles à relire, quel que soit leur âge)\n"
            f"/pending seul (liste paginée des extractions à relire, « ▶️ Suivants » ; "
            f"« ✅ @canal » ouvre l'aperçu d'un lot — ce qu'il viserait, extrait par "
            f"extraction — avec « ✅ Confirmer le lot » ou « ✖️ Annuler »)\n"
            f"/tag BTC-USD (en réponse à un média : l'étiquette oriente la recherche)\n"
            f"/tag --help\n"
            f"/transcribe (relance l'extraction d'un média déjà stocké : rattrapage "
            f"d'un vocal sans clé Groq, d'une image sans clé Gemini)\n"
            f"/transcribe seul (liste paginée des textes à rattraper, « ▶️ Suivants »)\n"
            f"/transcribe --help\n"
            f"/channels (canaux balayés : lire, ajouter, retirer, période)\n"
            f"/channels --help\n\n"
            f"📎 Envoie une photo, vidéo, document ou note vocale : le média est "
            f"stocké dans ta base de connaissances (Supabase Storage), et tu peux "
            f"valider ou retirer son extraction avec les boutons du compte-rendu."
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

    await run_analysis(update.message, str(update.effective_user.id), context.args[0].upper())


async def run_analysis(
    message,
    user_id: str,
    asset: str,
    extra_context: Optional[str] = None,
    rag_label: Optional[str] = None,
):
    """Analyse un actif et propose un signal à validation humaine.

    Partagé par `/analyze` (contexte construit par le moteur) et par la boucle RAG
    (`/use`, contexte **choisi** par l'utilisateur) : le chemin de validation et la
    forme du message ne doivent pas diverger — c'est le même signal, avec ou sans
    extraits injectés.

    Le premier argument est le **message** (et non l'`Update`) parce que le chemin
    RAG arrive par un clic : `update.message` y vaut `None`, seul
    `callback_query.message` permet de répondre.
    """
    await message.reply_text(f"🧠 Analyse de {asset} en cours (logique pure)...")

    try:
        signal = engine.analyze(asset, extra_context=extra_context)
        print(f">>> Signal brut : {signal}")
    except Exception as e:
        print(f"❌ Erreur engine.analyze : {e}")
        await message.reply_text(f"Erreur analyse : {e}")
        return

    # Aucun signal fort : on NE fabrique PAS de faux BUY (pas de recommandation
    # trompeuse). On affiche seulement le contexte informatif.
    if not signal:
        print(">>> Pas de signal fort → message informatif (aucun signal fabriqué)")
        price = get_last_price(asset) or 0.0
        try:
            insights = get_recent_insights(limit=20)
            # Les extraits validés sont repassés ici : le message informatif doit
            # refléter ce que l'analyse a réellement lu, pas un second avis sans eux.
            llm_result = engine._news_cache.get(asset, insights, extra_context=extra_context)
            if llm_result:
                geo_txt = f"Analyse IA : {llm_result['reasoning']}"
                sent_txt = f"Biais IA : {llm_result['bias']} (score {llm_result['score']:.2f})"
            else:
                _, geo_txt = engine._score_geo(insights)
                _, sent_txt = engine._score_sentiment(insights)
                geo_txt += " [fallback: clé LLM absente ou erreur]"
        except Exception:
            geo_txt, sent_txt = "Indisponible", "Indisponible"

        price_txt = f"{price:.5f}" if price else "N/A"
        await message.reply_text(
            f"🧠 Aucun signal exploitable pour {asset}\n\n"
            f"Prix actuel : {price_txt}\n\n"
            f"{rag_label or ''}"
            f"Le moteur n'a détecté aucune configuration RSI/EMA assez marquée "
            f"ET/OU un filtre de risque est actif. Aucune proposition de trade "
            f"n'est générée (pas de fausse recommandation).\n\n"
            f"Contexte informatif (non exploitable comme signal) :\n"
            f"Géopolitique : {geo_txt}\n"
            f"Sentiment : {sent_txt}\n\n"
            f"Pour tester le workflow de validation manuellement : /test_order {asset}"
        )
        return

    try:
        signal_id = create_pending_signal(user_id, signal)
        print(f">>> signal_id : {signal_id}")
    except Exception as e:
        print(f"❌ create_pending_signal : {e}")
        signal_id = "demo-local"

    text = (
        f"🚨 PROPOSITION IA — VALIDATION HUMAINE OBLIGATOIRE\n\n"
        f"{rag_label or ''}"
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

    await message.reply_text(
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


async def add_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Usage : /add_note titre court | contenu de la règle\n\n"
            "Ex: /add_note stop_loss_regle | Ne jamais risquer plus de 1% "
            "sur les cryptos en dessous de 50 de Fear&Greed."
        )
        return

    raw = " ".join(context.args)
    if "|" in raw:
        title, content = raw.split("|", 1)
        title, content = title.strip(), content.strip()
    else:
        title, content = "Note", raw.strip()

    if len(content) > 1500:
        content = content[:1500] + "\n[...tronqué, garde tes notes courtes]"

    try:
        upsert_note(source=f"manuel:{title.lower().replace(' ', '_')}", title=title, content=content)
        await update.message.reply_text(f"✅ Note ajoutée à la base de connaissances : \"{title}\"")
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur : {e}")


async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        notes = list_notes()
        if not notes:
            await update.message.reply_text("📚 Base de connaissances vide pour l'instant.")
            return
        lines = [f"• {n['title']} ({n.get('char_count', 0)} car.)" for n in notes]
        await update.message.reply_text("📚 Notes en base :\n" + "\n".join(lines))
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur : {e}")


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Recherche sémantique dans la base de connaissances (`knowledge_chunks`).

    Options : `--asset`, `--source` (notes/médias), `--regime`, `-n/--limit`.
    Sans requête ou avec `--help`, la commande affiche son aide — les options ne
    doivent pas s'apprendre en lisant le code.

    Un seul lot est interrogé (`SEARCH_POOL_SIZE` passages) : la première page est
    affichée, le reste est confié à `search_history` pour `/search_more`, qui
    déroule la suite **sans** nouvel appel réseau.
    """
    parsed = parse_search_args(context.args or [])
    if not parsed["ok"]:
        # Aide demandée, requête vide ou option fautive : `format_search_results`
        # rend le message adapté (et rappelle la commande d'aide).
        await update.message.reply_text(format_search_results(parsed))
        return
    try:
        # La recherche appelle un embedder (réseau) : hors de la boucle async.
        result = await asyncio.to_thread(
            search_knowledge,
            parsed["query"],
            top_k=SEARCH_POOL_SIZE,
            asset=parsed["asset"],
            source=parsed["source"],
            regime=parsed["regime"],
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur : {e}")
        return

    if not result.get("ok"):
        # Recherche vectorielle indisponible, requête refusée… : rien à mémoriser.
        await update.message.reply_text(format_search_results(result, options=parsed))
        return

    user_id = str(update.effective_user.id)
    page = search_history.start(user_id, parsed, result.get("hits") or [])
    # `options` : les filtres actifs sont rappelés en tête de réponse, sinon un
    # résultat unique paraît arbitraire.
    await update.message.reply_text(
        format_search_results(
            page,
            options=page["options"],
            start=page["start"],
            total=page["total"],
        ),
        reply_markup=_analysis_keyboard(page["hits"]),
    )


def _analysis_keyboard(hits):
    """Boutons « analyser cet actif » sous des résultats de recherche.

    La boucle RAG commence donc par un bouton, pas par une commande à connaître.
    Les actifs sont déduits des passages affichés (un extrait non étiqueté ne
    propose rien : on ne devine pas un actif à la place de l'utilisateur) ; au plus
    trois, pour ne pas transformer la réponse en clavier.
    """
    assets = []
    for hit in hits or []:
        asset = str(hit.get("asset") or "").strip().upper()
        if asset and asset not in assets:
            assets.append(asset)
    rows = [
        [InlineKeyboardButton(f"🎯 Analyser {asset}", callback_data=f"rag:new:{asset}")]
        for asset in assets[:3]
    ]
    return InlineKeyboardMarkup(rows) if rows else None


async def use_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/use <ACTIF> [rangs]` : analyse un actif avec des passages **validés**.

    Rien n'est exécuté ici : la sélection devient une proposition, et c'est la
    validation (bouton ✅) qui déclenche l'analyse — avec, dans le prompt, les
    extraits exactement tels qu'ils viennent d'être montrés.
    """
    args = list(context.args or [])
    if not args:
        await update.message.reply_text(
            "Usage : /use <ACTIF> [rangs]\n\n"
            "Ex : /use BTC-USD 1,3  — analyse avec les passages 1 et 3 de ta "
            "dernière recherche (sans rang : les 3 premiers).\n"
            "Lance d'abord `/search <texte>` pour constituer le lot."
        )
        return

    asset = args[0].upper()
    spec = " ".join(args[1:]).strip()
    proposal = rag_loop.propose(str(update.effective_user.id), asset, spec)
    if not proposal["ok"]:
        await update.message.reply_text(format_rag_selection(proposal))
        return

    keyboard = [[
        InlineKeyboardButton("✅ LANCER L'ANALYSE", callback_data="rag:run"),
        InlineKeyboardButton("❌ ANNULER", callback_data="rag:cancel"),
    ]]
    await update.message.reply_text(
        format_rag_selection(proposal), reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def rag_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Boutons de la boucle RAG : proposer un actif, lancer, annuler.

    « Lancer » **consomme** la proposition (`rag_loop.take`) : un double clic ne
    produit pas deux analyses, ni deux propositions de trade.
    """
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)
    parts = (query.data or "").split(":", 2)
    action = parts[1] if len(parts) > 1 else ""

    if action == "new" and len(parts) == 3:
        proposal = rag_loop.propose(user_id, parts[2])
        if not proposal["ok"]:
            await query.edit_message_text(format_rag_selection(proposal))
            return
        keyboard = [[
            InlineKeyboardButton("✅ LANCER L'ANALYSE", callback_data="rag:run"),
            InlineKeyboardButton("❌ ANNULER", callback_data="rag:cancel"),
        ]]
        await query.edit_message_text(
            format_rag_selection(proposal), reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if action == "cancel":
        rag_loop.cancel(user_id)
        await query.edit_message_text("❌ Analyse annulée — aucun extrait injecté.")
        return

    if action != "run":
        await query.edit_message_text("Callback RAG inconnu.")
        return

    proposal = rag_loop.take(user_id)
    if proposal is None:
        await query.edit_message_text(
            "⚠️ Proposition déjà utilisée ou expirée : relance `/use <ACTIF> [rangs]`."
        )
        return

    label = (
        f"🧩 Contexte injecté : {len(proposal.excerpts)} extrait(s) validé(s) de ta "
        f"base (rangs {', '.join(str(rank) for rank in proposal.ranks)}).\n\n"
    )
    await query.edit_message_text(label + "⏳ Analyse en cours…")
    await run_analysis(
        query.message,
        user_id,
        proposal.asset,
        extra_context=proposal.context,
        rag_label=label,
    )


async def search_more_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Passages suivants de la dernière recherche (`/search_more`).

    Aucun appel réseau : la suite du lot est déjà en mémoire, et le classement
    reste celui qu'a vu l'utilisateur.
    """
    user_id = str(update.effective_user.id)
    result = search_history.next_page(user_id)
    text = format_search_more(result)
    if context.args:
        # Ne pas ignorer en silence ce que l'utilisateur a tapé : la taille de
        # page est fixée par la recherche d'origine, et le dire évite de croire
        # que l'argument a été pris en compte.
        text = (
            "ℹ️ `/search_more` ignore les arguments : la taille de page est celle "
            "de la recherche d'origine (`/search … -n N`).\n\n" + text
        )
    await update.message.reply_text(text)


async def risk_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    try:
        prefs = get_preferences(user_id)
        equity = get_user_equity(user_id)

        try:
            client, _ = get_alpaca_client(user_id)
            acct = client.get_account()
            balance = float(acct.equity)
            balance_note = "(solde réel Alpaca)"
        except BrokerCredentialsUnreadable:
            # Le compte existe : « pas de compte réel » serait faux, et le solde
            # papier ferait croire à une absence de compte.
            balance = equity
            balance_note = "(⚠️ identifiants broker illisibles — anneau de clés incomplet)"
        except AlpacaSDKUnavailable:
            # Ici le repli est légitime (on **affiche** un solde, on ne décide pas
            # d'un ordre) — mais la note doit dire la vraie cause : « pas de compte
            # réel » enverrait chercher un compte là où il manque un paquet.
            balance = equity
            balance_note = "(⚠️ SDK Alpaca absent — solde paper configuré, aucun compte interrogé)"
        except Exception:
            balance = equity
            balance_note = "(equity paper configurée, pas de compte réel)"

        state = risk_get_state(user_id, balance)
        starting = float(state.get("starting_balance") or balance)
        daily_start = float(state.get("daily_start_balance") or balance)

        daily_loss_pct = (daily_start - balance) / daily_start * 100 if daily_start > 0 else 0
        total_dd_pct = (starting - balance) / starting * 100 if starting > 0 else 0

        allowed, reason = risk_can_trade(user_id, balance)
        status_txt = "✅ Trading autorisé" if allowed else f"🛑 Trading bloqué : {reason}"

        await update.message.reply_text(
            f"📊 État du risque {balance_note}\n\n"
            f"Solde actuel : {balance:.2f}\n"
            f"Solde début de journée : {daily_start:.2f}\n"
            f"Solde initial : {starting:.2f}\n\n"
            f"Perte du jour : {daily_loss_pct:.2f}% (limite {prefs.get('max_daily_loss_pct', 5.0)}%)\n"
            f"Drawdown total : {total_dd_pct:.2f}% (limite {prefs.get('max_total_drawdown_pct', 10.0)}%)\n\n"
            f"{status_txt}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur : {e}")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        summary = build_performance_summary()
        counts = summary.get("counts") or {}
        await update.message.reply_text(
            f"📊 Performance Collective\n"
            f"• Gagnés : {counts.get('won', 0)}\n"
            f"• Perdus : {counts.get('lost', 0)}\n"
            f"• En cours : {counts.get('executed', 0)}\n"
            f"• En attente : {counts.get('pending', 0)}\n"
            f"🏆 Win Rate : {summary.get('win_rate', 0)}%\n"
            f"📈 Rendement moyen réglé : {summary.get('avg_settled_return_pct', 0)}%"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur stats : {e}")


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    try:
        summary = build_performance_summary(user_id)
        files = export_run_card(user_id)
        counts = summary.get("counts") or {}
        best_assets = summary.get("best_assets") or []
        top_line = "Aucun trade réglé" if not best_assets else ", ".join(
            f"{item['asset']} ({item['win_rate']}%)" for item in best_assets[:3]
        )
        await update.message.reply_text(
            f"🧾 Ton run card perso\n"
            f"• Trades réglés : {counts.get('settled', 0)}\n"
            f"• Win rate : {summary.get('win_rate', 0)}%\n"
            f"• Rendement moyen réglé : {summary.get('avg_settled_return_pct', 0)}%\n"
            f"• Meilleurs actifs : {top_line}\n\n"
            f"Artifacts générés : {files.get('json_file')} + {files.get('markdown_file')}"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur report : {e}")


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
    try:
        creds = get_broker_credentials(user_id)
    except BrokerCredentialsUnreadable as exc:
        await update.message.reply_text(
            "⚠️ Un compte broker est bien connecté, mais son chiffré ne se rouvre "
            "pas :\n\n"
            f"{exc}\n\n"
            "Tant que la clé manquante n'est pas remise dans "
            "`ENCRYPTION_KEYS_PREVIOUS`, tes ordres seront refusés — jamais "
            "exécutés sur le compte partagé."
        )
        return
    if not creds:
        await update.message.reply_text(
            "🔌 Aucun compte broker personnel connecté.\n"
            "Tes ordres utilisent le compte partagé du bot (tests uniquement).\n\n"
            "Pour connecter le tien : /connect_broker TA_CLE TON_SECRET"
        )
        return
    mode = "PAPER (simulation)" if creds["paper"] else "⚠️ LIVE (argent réel)"
    masked = creds["api_key"][:4] + "•" * 8 + creds["api_key"][-4:] if len(creds["api_key"]) > 8 else "••••"
    # La version de la clé qui a chiffré cette ligne : c'est ce qui rend une
    # rotation visible depuis le bot, sans avoir à passer par la base.
    version = creds.get("key_version")
    version_line = f"\nChiffré avec : v{version}" if version is not None else ""
    await update.message.reply_text(
        f"🔌 Compte broker connecté : {creds['broker']}\n"
        f"Mode : {mode}\n"
        f"Clé : {masked}"
        f"{version_line}"
    )


async def media_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Liste les derniers médias, un lien signé pour chacun, et la revue par ligne.

    C'est aussi le **rattrapage** d'une revue qui n'a pas eu lieu : un compte-rendu
    d'ingestion jamais ouvert (publication de canal, message noyé dans
    l'historique) ne laisse aucune trace actionnable, alors que la liste porte les
    boutons de revue de chaque média, avec le canal d'origine de chacun. Elle est
    bornée aux derniers médias : ce qui attend encore un verdict, quel que soit son
    âge, se lit avec `/pending`.
    """
    try:
        # Lecture en base + signature des liens : réseau, donc hors de l'event loop.
        view = await asyncio.to_thread(telegram_media.media_list_view)
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur : {e}")
        return
    await update.message.reply_text(
        view["text"], reply_markup=_list_keyboard(view["keyboard"])
    )


async def pending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/pending` — les extractions **sans verdict**, quel que soit leur âge.

    Le complément de `/media` : sa lecture est bornée aux derniers médias, donc une
    extraction ancienne jamais relue en sort dès qu'il y a eu dix ingestions
    depuis. Ici la table est balayée **en entier** et seules les lignes sans
    verdict sont listées — avec leur canal d'origine et leurs boutons de revue.
    """
    try:
        # Balayage paginé de la table + signature des liens : réseau.
        view = await asyncio.to_thread(telegram_media.pending_review_view)
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur : {e}")
        return
    await update.message.reply_text(
        view["text"], reply_markup=_list_keyboard(view["keyboard"])
    )


async def _telegram_downloader(context: ContextTypes.DEFAULT_TYPE):
    """Fonction de téléchargement Telegram (`file_id` → octets).

    Partagée par les deux routes d'ingestion — chat et canal — pour qu'elles ne
    puissent pas diverger sur la façon de récupérer un fichier.
    """

    async def _download(file_id: str) -> bytes:
        tg_file = await context.bot.get_file(file_id)
        return bytes(await tg_file.download_as_bytearray())

    return _download


async def _handle_telegram_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Télécharge le média du message et l'enregistre dans Supabase Storage.

    Un média qui appartient à un **album** n'est pas traité ici : il est mis de
    côté, et c'est le silence qui suit le dernier élément qui déclenche le lot
    (`_deliver_album`). Rien n'est donc répondu maintenant — c'est ce délai, et lui
    seul, qui remplace une réponse par photo par une réponse pour tout l'envoi.
    """
    message = update.effective_message
    group = telegram_media.album_group(message)
    if group:
        await _album_buffer.add(group, (message, context))
        return
    await _ingest_one(
        message, download=await _telegram_downloader(context), bot=context.bot
    )


async def _ingest_one(message, *, download, bot) -> None:
    """Ingère un média **seul** : compte-rendu, revue et `.txt`.

    Partagé par le média hors album et par l'album réduit à un seul élément —
    Telegram en livre parfois un seul (mise à jour isolée), et il n'y a alors
    aucune raison de changer de compte-rendu.
    """
    result = await telegram_media.ingest_media(message, download=download)
    await message.reply_text(
        telegram_media.format_report(result),
        reply_markup=_review_keyboard(result),
    )
    await _send_extracted_text(bot, message.chat_id, result)


async def _deliver_album(group_id: str, items) -> None:
    """Traite un album **entier** : un seul lot, une seule réponse.

    Les charges mises de côté sont des `(message, context)` : le contexte sert à
    télécharger les fichiers (`context.bot.get_file`), et celui du **dernier**
    élément suffit — c'est le même bot pour tout l'album.

    La réponse part sous le dernier élément : c'est là que Telegram l'affiche
    (sous l'album, pas au milieu), et c'est donc le média que `/tag` et
    `/transcribe` résolvent quand on répond à ce compte-rendu. Les autres éléments
    se désignent par leur référence, que leur ligne porte.
    """
    messages = [item[0] for item in items]
    context = items[-1][1]
    download = await _telegram_downloader(context)
    if len(messages) == 1:
        await _ingest_one(messages[0], download=download, bot=context.bot)
        return

    report = await telegram_media.ingest_album(messages, download=download)
    last = messages[-1]
    view = await asyncio.to_thread(telegram_media.album_report_view, report)
    await last.reply_text(
        view["text"], reply_markup=_list_keyboard(view["keyboard"])
    )
    #: Une extraction trop longue pour tenir dans un message part en `.txt`,
    #: **par élément** : ce sont les textes à relire, pas des réponses de plus.
    for result in report["results"]:
        await _send_extracted_text(context.bot, last.chat_id, result)


#: Albums : les éléments d'un même envoi sont rassemblés par `media_group_id`
#: avant d'être traités (voir `telegram_media.AlbumBuffer` : fenêtre de silence,
#: et pourquoi la livraison ne peut pas attendre dans le handler).
_album_buffer = telegram_media.AlbumBuffer(
    telegram_media.ALBUM_WINDOW_SECONDS, _deliver_album
)


async def tag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/tag <ACTIF> [référence]` — associe un actif à un média déjà ingéré.

    L'actif étiqueté est l'`asset` des morceaux indexés, donc le filtre
    **préférentiel** de la recherche vectorielle : le média remonte en tête des
    analyses de cet actif et disparaît de celles des autres. Deux façons de
    désigner le média — en réponse à son message (rien à copier), ou par sa
    référence — et `--clear` pour revenir à « non étiqueté » (le joker).
    """
    parsed = telegram_media.parse_tag_args(context.args or [])
    if not parsed["ok"]:
        await update.message.reply_text(telegram_media.format_tag_help(parsed))
        return

    resolved = await telegram_media.find_media(
        reference=parsed["reference"], replied=update.message.reply_to_message
    )
    if not resolved["ok"]:
        await update.message.reply_text(telegram_media.format_tag_help(resolved))
        return

    result = await telegram_media.tag_media(
        resolved["media_id"],
        parsed["asset"],
        reviewer=str(update.effective_user.id),
    )
    await update.message.reply_text(telegram_media.format_tag_report(result))


async def transcribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/transcribe [référence]` — refait l'extraction d'un média déjà stocké.

    Le rattrapage d'une ingestion faite **sans la clef qu'il fallait** : sans
    `GROQ_API_KEY` (vocal, vidéo) ou sans `GEMINI_API_KEY` (image), le fichier est
    stocké mais son texte n'existe pas — et le renvoyer sur Telegram ne réindexe
    rien (il est reconnu comme déjà stocké).

    Sans argument et sans message en réponse, la question n'est plus « comment »
    mais « lesquels » : on liste les médias dont l'extraction a échoué, avec ce
    qui manque à chacun. Le reste — l'ordre des vérifications, ce qui est dit
    dans chaque cas — vit dans le module, qui est testable sans Telegram.
    """
    parsed = telegram_media.parse_transcribe_args(context.args or [])
    replied = update.message.reply_to_message
    if parsed["ok"] and parsed["reference"] is None and replied is None:
        # La liste est **paginée** : elle balaie toute la table, donc elle ne tient
        # pas dans un message — sans clavier, les rattrapages au-delà de la
        # première page seraient aussi invisibles qu'avec l'ancienne borne.
        view = await asyncio.to_thread(telegram_media.transcribe_candidates)
        await update.message.reply_text(
            view["text"], reply_markup=_list_keyboard(view["keyboard"])
        )
        return
    if not parsed["ok"]:
        await update.message.reply_text(telegram_media.format_transcribe_help(parsed))
        return

    resolved = await telegram_media.find_media(
        reference=parsed["reference"], replied=replied
    )
    if not resolved["ok"]:
        await update.message.reply_text(telegram_media.format_transcribe_help(resolved))
        return

    result = await telegram_media.retranscribe_media(
        resolved["media_id"], reviewer=str(update.effective_user.id)
    )
    await update.message.reply_text(
        telegram_media.format_transcribe_report(result),
        reply_markup=_follow_up_keyboard(result),
    )
    # Une extraction refaite peut être trop longue pour le message : le texte
    # complet part en `.txt`, comme après une revue.
    await _send_extracted_text(context.bot, update.message.chat_id, result)


async def channels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/channels` — lire, ajouter, retirer les canaux balayés et régler la période.

    Le réglage est **global** (celui du worker, pas d'un utilisateur) : c'est le
    module qui décide qui a le droit d'écrire — le chat `TELEGRAM_ADMIN_CHAT_ID` —
    et qui compose la réponse, ce qui rend tout ça vérifiable sans Telegram. Les
    réglages effectifs sont relus par la fonction du worker, pour que la réponse
    dise ce qui sera balayé et non ce qu'on a voulu écrire.

    Analyse, lecture et écriture touchent la configuration et la base :
    bloquantes, donc hors de l'event loop, comme les commandes média.
    """
    parsed = telegram_channels.parse_channels_args(context.args or [])
    result = await asyncio.to_thread(
        telegram_channels.channels_command,
        parsed,
        chat_id=update.effective_chat.id,
        admin_chat_id=TELEGRAM_ADMIN_CHAT_ID,
    )
    await update.message.reply_text(result["text"])


async def _send_extracted_text(bot, chat_id, result) -> Optional[str]:
    """Envoie le texte extrait **complet** en `.txt` quand le message ne peut pas le porter.

    Le compte-rendu affiche l'aperçu entier quand il tient dans `EXCERPT_CHARS`, et
    annonce la pièce jointe sinon : sans elle, on validerait une extraction sur un
    fragment (un PDF de plusieurs pages, une transcription de vingt minutes).
    Le fichier contient le texte **indexé** (légende + contenu), pas une
    réextraction : c'est ce que les prompts et `/search` liront.

    Si l'envoi échoue, on le dit et on rend l'aperçu tronqué : sinon l'utilisateur
    attend un fichier qui n'arrivera jamais, et il n'a plus rien pour juger. Le
    motif d'échec est retourné (et non seulement journalisé) pour être testable.
    """
    attachment = telegram_media.attachment_for(result)
    if attachment is None:
        return None
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=InputFile(
                io.BytesIO(attachment["content"]), filename=attachment["filename"]
            ),
            caption=attachment["caption"],
        )
    except Exception as e:
        reason = f"{type(e).__name__}: {str(e)[:200]}"
        excerpt = ((result.get("extraction") or {}).get("excerpt") or "").strip()
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ Texte extrait complet non envoyé en pièce jointe ({reason}).\n"
                f"• Aperçu : {excerpt}"
            ),
        )
        return reason
    return None


def _review_keyboard(result):
    """Clavier de revue d'une extraction, ou `None` s'il n'y a rien à relire.

    Le module décide **quand** des boutons ont un sens (`review_target`) : une
    ingestion échouée, un doublon ou un média sans morceau indexé n'affichent
    rien, plutôt que des boutons sans effet.
    """
    media_id = telegram_media.review_target(result)
    if not media_id:
        return None
    return _keyboard(telegram_media.review_buttons(media_id))


def _follow_up_keyboard(result):
    """Clavier à afficher après un verdict (vide = on retire le clavier)."""
    return _keyboard(telegram_media.follow_up_buttons(result))


def _keyboard(buttons):
    """`InlineKeyboardMarkup` d'une liste de `(libellé, callback_data)`.

    Une seule rangée : la Bot API empêche de toute façon un message de plus de
    quelques boutons, et les libellés sont déjà explicites.
    """
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in buttons]]
    )


def _list_keyboard(rows):
    """`InlineKeyboardMarkup` d'un clavier de liste : une rangée par média.

    Une **rangée** par média, et non tous les boutons sur une ligne : le numéro
    porté par un bouton est celui de la ligne de texte, donc l'alignement vertical
    est ce qui relie le bouton à ce qu'il change. Les rangées sont construites par
    `telegram_media.list_buttons`, qui décide aussi quels boutons ont un sens.
    """
    if not rows:
        return None
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=data) for label, data in row]
            for row in rows
        ]
    )


async def _review_from_list(update: Update, context: ContextTypes.DEFAULT_TYPE, *, prefix, view):
    """Corps commun des verdicts cliqués **depuis une liste**.

    Le message est la liste elle-même : un verdict rendu ici ne la remplace donc
    pas, sinon les boutons des lignes pas encore relues disparaîtraient avec elle.
    On répond par une notification courte (ce qui vient de changer), puis on
    réaffiche la liste, où la ligne modifiée porte son nouveau verdict.

    `/media` et `/pending` partagent tout ce geste : n'accepter que **son**
    préfixe, et réafficher la liste d'où l'on vient — c'est le seul point qui les
    distingue, et il est passé en paramètre.
    """
    query = update.callback_query
    parsed = telegram_media.parse_review_callback(query.data, prefix=prefix)
    if not parsed:
        await query.answer("⚠️ Bouton de revue non reconnu.")
        return

    verdict, media_id = parsed
    try:
        result = await telegram_media.review_media(
            media_id, verdict, reviewer=str(update.effective_user.id)
        )
    except Exception as e:
        await query.answer(f"⚠️ Revue impossible : {str(e)[:150]}")
        return

    await query.answer(telegram_media.review_toast(result)[:200])
    try:
        rendered = await asyncio.to_thread(view)
    except Exception:
        # La liste est momentanément illisible : le compte-rendu du verdict dit au
        # moins ce qui vient d'être changé, là où un message figé laisserait croire
        # que rien n'a bougé.
        await query.edit_message_text(
            telegram_media.format_review_report(result),
            reply_markup=_follow_up_keyboard(result),
        )
        return
    await query.edit_message_text(
        rendered["text"], reply_markup=_list_keyboard(rendered["keyboard"])
    )
    # Une réindexation produit une extraction **neuve**, que la liste ne montre pas
    # (elle n'affiche que les lignes) : elle part en `.txt`, comme depuis le
    # compte-rendu d'ingestion.
    await _send_extracted_text(context.bot, update.effective_chat.id, result)


async def media_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Boutons ✅ / ❌ / ↩️ d'une **ligne** de `/media` : on réaffiche `/media`."""
    await _review_from_list(
        update,
        context,
        prefix=telegram_media.LIST_REVIEW_PREFIX,
        view=telegram_media.media_list_view,
    )


async def media_list_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Boutons « ◀️ Précédents » / « ▶️ Suivants » de la liste `/media`.

    Le seul geste de `/media` qui **ne rend aucun verdict** : il ne touche ni la
    base ni l'index, il relit la vue au rang porté par le bouton. C'est ce qui
    permet de descendre une liste longue — au-delà de la page de dix médias que
    `/media` affiche — sans en traiter un seul.

    La page est **recalculée** à chaque clic, jamais mémorisée : entre deux clics,
    une ingestion a pu ajouter des médias en tête, et une page figée montrerait
    alors d'autres lignes que celles qu'on croyait lire. Un rang devenu hors bornes
    — la liste a changé — n'affiche pas « aucun média » mais le dit, et garde de
    quoi revenir.
    """
    query = update.callback_query
    offset = telegram_media.parse_media_page(query.data)
    if offset is None:
        await query.answer("⚠️ Bouton de liste non reconnu.")
        return
    try:
        view = await asyncio.to_thread(telegram_media.media_list_view, offset=offset)
    except Exception as e:
        await query.answer(f"⚠️ Liste impossible à relire : {str(e)[:150]}")
        return
    await query.answer()
    await query.edit_message_text(
        view["text"], reply_markup=_list_keyboard(view["keyboard"])
    )


async def media_pending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Boutons ✅ / ❌ / ↩️ d'une **ligne** de `/pending`.

    La liste réaffichée est celle des extractions **encore** sans verdict : la ligne
    qui vient d'être traitée en a désormais un, elle disparaît, et la suivante —
    même plus ancienne — remonte à sa place.
    """
    await _review_from_list(
        update,
        context,
        prefix=telegram_media.PENDING_REVIEW_PREFIX,
        view=telegram_media.pending_review_view,
    )


async def media_pending_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Boutons « ◀️ Précédents » / « ▶️ Suivants » de la liste `/pending`.

    Le seul geste de `/pending` qui **ne rend aucun verdict** : il ne touche ni la
    base ni l'index, il relit la vue au rang porté par le bouton. C'est ce qui
    permet de parcourir toute la file — jusqu'à la plus ancienne extraction sans
    verdict — sans devoir en traiter une seule.

    La page est **recalculée** à chaque clic, jamais mémorisée : entre deux clics,
    un verdict rendu ailleurs (ou depuis un autre message) a fait tomber des
    lignes, et une page figée en montrerait qui n'attendent plus rien. Un rang
    devenu hors bornes est ramené par la vue elle-même (`_pending_start`).
    """
    query = update.callback_query
    offset = telegram_media.parse_pending_page(query.data)
    if offset is None:
        await query.answer("⚠️ Bouton de liste non reconnu.")
        return
    try:
        view = await asyncio.to_thread(
            telegram_media.pending_review_view, offset=offset
        )
    except Exception as e:
        await query.answer(f"⚠️ Liste impossible à relire : {str(e)[:150]}")
        return
    await query.answer()
    await query.edit_message_text(
        view["text"], reply_markup=_list_keyboard(view["keyboard"])
    )


async def media_pending_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Boutons « ✅ @canal » / « ↩️ @canal » de la liste `/pending` : l'**aperçu**.

    Ce clic **n'écrit rien**. Il ouvre la question que le lot ne posait pas :
    « qu'est-ce que ce lot va toucher ? ». Un lot écrit sur plusieurs lignes d'un
    coup — jusqu'à `CHANNEL_BULK_MAX` —, et rien, avant, ne les avait montrées :
    l'étiquette du bouton ne portait qu'un nom de canal et un nombre. L'aperçu
    (`bulk_preview_view`) relit la file, nomme les extractions visées, marque celles
    que le lot laisserait de côté, et propose de confirmer ou d'annuler.

    La cible est relue **ici**, à l'ouverture, comme elle le sera à la confirmation :
    un verdict tombé entre l'affichage de la liste et ce clic n'est jamais rouvert.
    """
    query = update.callback_query
    parsed = telegram_media.parse_channel_bulk(query.data)
    if not parsed:
        await query.answer("⚠️ Bouton de lot non reconnu.")
        return

    verdict, channel = parsed
    try:
        preview = await asyncio.to_thread(
            telegram_media.bulk_preview_view, channel, verdict
        )
    except Exception as e:
        await query.answer(f"⚠️ Aperçu impossible : {str(e)[:150]}")
        return

    await query.answer(telegram_media.preview_toast(preview)[:200])
    await query.edit_message_text(
        preview["text"], reply_markup=_list_keyboard(preview["keyboard"])
    )


async def media_pending_channel_confirm_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Bouton « ✅ Confirmer le lot » de l'aperçu : **ici** le lot s'exécute.

    C'est le seul endroit du chemin d'un lot qui écrit en base, et il faut deux
    clics distincts pour y arriver : celui de la liste (qui montre), et celui-ci
    (qui agit). Le geste reste « tout un canal d'un clic » pour qui a lu l'aperçu,
    mais il ne peut plus partir d'une liste qu'on parcourt.

    La cible est **relue à la confirmation**, jamais rejouée depuis ce que l'aperçu
    affichait : entre les deux, un verdict a pu tomber — depuis cette liste, depuis
    `/media`, ou depuis un autre message. Si plus rien ne reste, le compte-rendu le
    **dit** au lieu de ne rien faire en silence.

    Le message est la liste : le compte-rendu la **précède** au lieu de la
    remplacer. Remplacer la liste par le seul rapport obligerait à retaper
    `/pending` pour reprendre la revue des autres canaux, alors que les verdicts du
    lot viennent justement d'en faire disparaître des lignes.
    """
    query = update.callback_query
    parsed = telegram_media.parse_channel_bulk(
        query.data, prefix=telegram_media.CHANNEL_BULK_CONFIRM_PREFIX
    )
    if not parsed:
        await query.answer("⚠️ Confirmation non reconnue.")
        return

    verdict, channel = parsed
    try:
        result = await telegram_media.bulk_review_channel(
            channel, verdict, reviewer=str(update.effective_user.id)
        )
    except Exception as e:
        await query.answer(f"⚠️ Lot impossible : {str(e)[:150]}")
        return

    await query.answer(telegram_media.bulk_toast(result)[:200])
    try:
        rendered = await asyncio.to_thread(telegram_media.pending_review_view)
    except Exception:
        # La liste est momentanément illisible : le compte-rendu du lot dit au
        # moins ce qui vient d'être changé, là où un message figé laisserait croire
        # que rien n'a bougé.
        await query.edit_message_text(telegram_media.format_bulk_report(result))
        return
    await query.edit_message_text(
        f"{telegram_media.format_bulk_report(result)}\n\n{rendered['text']}",
        reply_markup=_list_keyboard(rendered["keyboard"]),
    )


async def media_pending_channel_cancel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
):
    """Bouton « ✖️ Annuler » de l'aperçu : on revient à la liste, rien de plus.

    Le seul geste de tout ce chemin qui ne lit même pas la file du lot : il rend la
    liste `/pending` telle qu'elle est **maintenant**. Sans lui, un aperçu serait un
    cul-de-sac — l'opérateur n'aurait plus que `/pending` à retaper pour retrouver
    ce qu'il avait sous les yeux.
    """
    query = update.callback_query
    parsed = telegram_media.parse_channel_bulk(
        query.data, prefix=telegram_media.CHANNEL_BULK_CANCEL_PREFIX
    )
    if not parsed:
        await query.answer("⚠️ Annulation non reconnue.")
        return

    await query.answer("✖️ Lot annulé : rien n'a été modifié.")
    try:
        view = await asyncio.to_thread(telegram_media.pending_review_view)
    except Exception as e:
        await query.answer(f"⚠️ Liste impossible à relire : {str(e)[:150]}")
        return
    await query.edit_message_text(
        view["text"], reply_markup=_list_keyboard(view["keyboard"])
    )


async def media_transcribe_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Boutons « ◀️ Précédents » / « ▶️ Suivants » de la liste `/transcribe`.

    La page est **recalculée** à chaque clic — aucun état n'est mémorisé : entre
    deux clics, une extraction peut avoir été relancée depuis
    `/transcribe <référence>`, et la liste des rattrapages s'en trouve plus courte.
    Une page figée enverrait alors sur du travail déjà fait ; recalculée, elle dit
    ce qu'il reste, à partir du rang qu'on lui demande.
    """
    query = update.callback_query
    offset = telegram_media.parse_transcribe_page(query.data)
    if offset is None:
        await query.answer("⚠️ Bouton de liste non reconnu.")
        return
    try:
        view = await asyncio.to_thread(
            telegram_media.transcribe_candidates, offset=offset
        )
    except Exception as e:
        await query.answer(f"⚠️ Liste impossible à relire : {str(e)[:150]}")
        return
    await query.answer()
    await query.edit_message_text(
        view["text"], reply_markup=_list_keyboard(view["keyboard"])
    )


async def media_review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Boutons ✅ / ❌ / ↩️ sous le compte-rendu d'un média ingéré.

    Le rejet **supprime les morceaux indexés** : c'est la seule action
destructrice du bot, et elle ne touche pas l'objet Storage — c'est ce qui
permet de revenir en arrière (bouton « ↩️ Réindexer »).
    """
    query = update.callback_query
    await query.answer()
    parsed = telegram_media.parse_review_callback(query.data)
    if not parsed:
        # Le handler est filtré sur `^med:` ; arriver ici veut dire qu'un clavier
        # plus ancien portait un format qu'on ne sait plus lire.
        await query.edit_message_text(
            "⚠️ Bouton de revue non reconnu : renvoie le média ou relance l'extraction."
        )
        return

    verdict, media_id = parsed
    try:
        result = await telegram_media.review_media(
            media_id, verdict, reviewer=str(update.effective_user.id)
        )
    except Exception as e:
        await query.edit_message_text(f"⚠️ Revue impossible : {str(e)[:300]}")
        return

    await query.edit_message_text(
        telegram_media.format_review_report(result),
        reply_markup=_follow_up_keyboard(result),
    )
    # Une réindexation produit une extraction **neuve** : elle peut, elle aussi,
    # être trop longue pour le message et partir en pièce jointe.
    await _send_extracted_text(context.bot, update.effective_chat.id, result)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PHOTO → la plus haute résolution disponible est stockée."""
    await _handle_telegram_media(update, context)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """VIDEO → stockée dans Supabase Storage."""
    await _handle_telegram_media(update, context)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """DOCUMENT → stocké dans Supabase Storage."""
    await _handle_telegram_media(update, context)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """VOICE (note vocale) → stockée dans Supabase Storage."""
    await _handle_telegram_media(update, context)


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """AUDIO (fichier audio envoyé comme fichier) → stocké puis transcrit.

    Distingué de VOICE : Telegram réserve `audio` aux fichiers en pièce jointe
    (mp3, m4a…), `voice` aux messages vocaux enregistrés. Les deux passent par
    le même extracteur, mais sans ce handler un mp3 serait silencieusement ignoré.
    """
    await _handle_telegram_media(update, context)


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """CHANNEL_POST → le média d'une publication de canal, ingéré **sans bruit**.

    Route complémentaire du scraper public (`scrapers/telegram_channel.py`), pas
    un remplacement : l'aperçu `t.me/s/<canal>` a un document pour rien (nom et
    taille, pas de fichier), là où le bot — administrateur du canal — reçoit le
    message et télécharge le **fichier d'origine** par l'API.

    Rien n'est publié dans le canal : un `reply_text` sur un `channel_post`
    s'afficherait devant tous les abonnés, avec la référence du média et les
    sous-titres internes. Le compte-rendu, la revue ✅/❌ et le `.txt` partent donc
    en chat privé (voir `_report_channel_media`).
    """
    message = update.effective_message
    channel = telegram_media.channel_origin(message)
    result = await telegram_media.ingest_media(
        message,
        download=await _telegram_downloader(context),
        channel=channel,
    )
    await _report_channel_media(context.bot, channel, result)


async def _report_channel_media(bot, channel, result) -> Optional[str]:
    """Compte-rendu + revue + `.txt` en **chat privé** pour un média de canal.

    Les cibles sont essayées dans l'ordre de `channel_report_targets` : l'admin qui
    a publié, puis le chat configuré. Le premier échoue légitimement — Telegram
    refuse d'écrire à quelqu'un qui n'a jamais ouvert la conversation avec le bot —
    et c'est la cible suivante qui compte. Aucune n'aboutit ? On le journalise avec
    la marche à suivre : l'ingestion, elle, a bien eu lieu, et c'est la **revue**
    qui est reportée.

    Retourne le chat utilisé (ou `None`) plutôt que de se taire : un envoi qui ne
    part pas doit être visible dans les logs, pas invisible.
    """
    note = telegram_media.format_channel_report(result, channel)
    for chat_id in telegram_media.channel_report_targets(channel, TELEGRAM_ADMIN_CHAT_ID):
        try:
            await bot.send_message(chat_id=chat_id, text=note, reply_markup=_review_keyboard(result))
        except Exception as e:
            print(
                f"   [canal] compte-rendu non envoye a {chat_id} : "
                f"{type(e).__name__}: {str(e)[:200]}"
            )
            continue
        await _send_extracted_text(bot, chat_id, result)
        return str(chat_id)
    print(
        "   [canal] compte-rendu non envoye : publie le message depuis ton compte "
        "(et non « en tant que canal »), ou renseigne TELEGRAM_ADMIN_CHAT_ID."
    )
    return None


def main():
    # Fail-closed : pas de démarrage avec des secrets par défaut/absents.
    enforce_secure_config()

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
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("test_order", test_order))
    app.add_handler(CommandHandler("risk_status", risk_status))
    app.add_handler(CommandHandler("add_note", add_note))
    app.add_handler(CommandHandler("notes", notes_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("search_more", search_more_cmd))
    app.add_handler(CommandHandler("use", use_cmd))
    app.add_handler(CommandHandler("media", media_cmd))
    app.add_handler(CommandHandler("pending", pending_cmd))
    app.add_handler(CommandHandler("tag", tag_cmd))
    app.add_handler(CommandHandler("transcribe", transcribe_cmd))
    app.add_handler(CommandHandler("channels", channels_cmd))
    app.add_handler(CommandHandler("connect_broker", connect_broker))
    app.add_handler(CommandHandler("disconnect_broker", disconnect_broker))
    app.add_handler(CommandHandler("broker_status", broker_status))
    # Médias Telegram → Supabase Storage (bucket privé `telegram-media`).
    # La route CANAL vient en premier : une seule mise à jour est remise à un
    # handler par groupe (le premier qui l'accepte), et une publication de canal
    # est aussi une photo/un document — sans cette priorité, elle tomberait dans
    # le handler de chat, qui répondrait dans le canal.
    app.add_handler(MessageHandler(telegram_filters.CHANNEL_MEDIA, handle_channel_post))
    app.add_handler(MessageHandler(telegram_filters.DIRECT_PHOTO, handle_photo))
    app.add_handler(MessageHandler(telegram_filters.DIRECT_VIDEO, handle_video))
    app.add_handler(MessageHandler(telegram_filters.DIRECT_DOCUMENT, handle_document))
    app.add_handler(MessageHandler(telegram_filters.DIRECT_VOICE, handle_voice))
    app.add_handler(MessageHandler(telegram_filters.DIRECT_AUDIO, handle_audio))
    # Les callbacks filtrés doivent précéder le gestionnaire générique, qui
    # attend `approve:`/`reject:` et un identifiant de signal : sans l'ordre, les
    # boutons RAG et de revue média tomberaient dans le mauvais handler.
    app.add_handler(CallbackQueryHandler(rag_callback, pattern=r"^rag:"))
    # Les claviers média ont des préfixes distincts et **disjoints** — le
    # deux-points de `^med:` ne peut pas suivre le `l`/`p`/`t` de `medl:`/`medp:`/
    # `medt:` — mais ils doivent tous précéder le gestionnaire générique : la même
    # action, et le message à réécrire dépend de la liste d'où vient le clic.
    # `medt:` et `medpg:` ne rendent aucun verdict : ils **naviguent**, l'un dans
    # `/transcribe`, l'autre dans `/pending`. `^medpg:` ne peut pas être confondu
    # avec `^medp:` : ce dernier exige un deux-points juste après `medp`. `medc:`
    # solde un **canal** entier — un lot, pas une ligne : lui aussi disjoint, par le
    # `c` qui suit `med` là où `^med:` exige un deux-points.
    #
    # Trois temps pour ce lot, donc trois préfixes : `medc:` ouvre l'aperçu,
    # `medck:` confirme (le seul qui écrit), `medcx:` annule et rend la liste.
    # `^medck:`/`^medcx:` ne peuvent pas être pris pour `^medc:` : ce dernier exige
    # un deux-points juste après `medc`, là où eux portent un `k`/`x` — même
    # argument que `^medp:` face à `^medpg:`.
    app.add_handler(CallbackQueryHandler(media_list_callback, pattern=r"^medl:"))
    # La navigation de `/media` : `^medlg:` ne peut pas être pris pour `^medl:`,
    # qui exige un deux-points juste après `medl` — même argument que `^medpg:`
    # face à `^medp:`. Elle ne rend aucun verdict, comme `medpg:`/`medt:`.
    app.add_handler(CallbackQueryHandler(media_list_page_callback, pattern=r"^medlg:"))
    app.add_handler(CallbackQueryHandler(media_pending_callback, pattern=r"^medp:"))
    app.add_handler(CallbackQueryHandler(media_pending_page_callback, pattern=r"^medpg:"))
    app.add_handler(
        CallbackQueryHandler(media_pending_channel_confirm_callback, pattern=r"^medck:")
    )
    app.add_handler(
        CallbackQueryHandler(media_pending_channel_cancel_callback, pattern=r"^medcx:")
    )
    app.add_handler(CallbackQueryHandler(media_pending_channel_callback, pattern=r"^medc:"))
    app.add_handler(CallbackQueryHandler(media_transcribe_callback, pattern=r"^medt:"))
    app.add_handler(CallbackQueryHandler(media_review_callback, pattern=r"^med:"))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("✅ Bot + Moteur IA prêts")
    print("🚀 Polling...")
    app.run_polling(
        drop_pending_updates=True,
        # `channel_post` doit être demandé explicitement : sans lui, Telegram ne
        # livre aucune publication de canal, et la route canal resterait morte
        # même correctement câblée.
        allowed_updates=["message", "callback_query", "channel_post"],
    )


if __name__ == "__main__":
    main()
