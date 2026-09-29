"""Notifications Telegram.

Le `Bot` est construit **paresseusement** : importer ce module ne valide aucun
token et ne requiert pas `python-telegram-bot`. C'était le point de rupture du
démarrage — `Bot(token=...)` levait `InvalidToken` à l'import, ce qui faisait
échouer tout module l'important (`api.webhook`, donc `run.py`, `workers/auto_loop`).

Désormais un token absent ou invalide ne casse plus le chargement : seule la
tentative d'envoi échoue, avec un message explicite (`RuntimeError`), et
l'appelant décide quoi en faire.
"""

from typing import Any, Optional

#: Bot mémorisé après la première construction réussie.
_bot: Optional[Any] = None


def _load_telegram():
    """Importe `python-telegram-bot` au premier besoin réel.

    L'import reste local à cette fonction : le paquet ne fait donc pas partie du
    graphe de dépendances à l'import de ce module.
    """
    try:
        from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
    except Exception as e:  # pragma: no cover - dépend de l'environnement
        raise RuntimeError(
            "python-telegram-bot est requis pour envoyer des notifications "
            "(pip install python-telegram-bot)"
        ) from e
    return Bot, InlineKeyboardButton, InlineKeyboardMarkup


def _telegram_token() -> str:
    """Token lu au moment de l'usage (jamais figé à l'import du module)."""
    from config import TELEGRAM_BOT_TOKEN

    return TELEGRAM_BOT_TOKEN or ""


def get_bot():
    """Retourne le `Bot` partagé, construit une seule fois.

    Lève une `RuntimeError` explicite si le paquet est absent ou le token
    inutilisable — jamais au moment de l'import.
    """
    global _bot
    if _bot is None:
        Bot, _button, _markup = _load_telegram()
        token = _telegram_token()
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN absent : aucune notification ne peut être envoyée."
            )
        try:
            _bot = Bot(token=token)
        except Exception as e:
            raise RuntimeError(f"TELEGRAM_BOT_TOKEN inutilisable : {e}") from e
    return _bot


async def send_signal_to_user(chat_id: int, signal: dict, signal_id: str):
    _Bot, InlineKeyboardButton, InlineKeyboardMarkup = _load_telegram()
    bot = get_bot()

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


async def send_admin_message(chat_id: Any, text: str) -> bool:
    """Message d'**exploitation** au chat administrateur (jamais une proposition).

    Distinct de `send_signal_to_user` : rien à approuver ici, aucun clavier, et
    le destinataire est l'opérateur — pas la liste des utilisateurs paper. Une
    alerte qui partirait à tout le monde ferait du bruit là où personne ne peut
    agir.

    Lève si le token est inutilisable ou si Telegram refuse : c'est à l'appelant
    de décider ce qu'une alerte non partie implique (voir `media_reconcile`, qui
    ne note l'anomalie comme signalée qu'après un envoi réussi).
    """
    bot = get_bot()
    await bot.send_message(chat_id=chat_id, text=text)
    return True


__all__ = ["get_bot", "send_admin_message", "send_signal_to_user"]
