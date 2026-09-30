from typing import Dict, Any, Optional
from config import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALPACA_BASE_URL,
    PAPER_TRADING,
    DEFAULT_RISK_PCT,
    DEFAULT_PAPER_EQUITY,
    MAX_POSITION_QTY,
)
from core.signal_quality import validate_signal
from database.supabase_client import supabase
from database.preferences import get_preferences
from database.broker_credentials import (
    BrokerCredentialsUnreadable,
    get_broker_credentials,
)
from execution.risk_guard import evaluate as risk_evaluate

#: Le SDK est-il utilisable ? C'est la même question que celle d'
#: `execution.alpaca_sdk.alpaca_sdk_available()` — posée sans rien importer, pour
#: le préflight — mais ici la réponse fait foi à l'exécution : ce sont ces quatre
#: noms-là qui construisent un client, donc les importer vraiment est la seule
#: preuve qui compte pour passer un ordre.
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    ALPACA_OK = True
except Exception:
    # sans signal : SDK absent ; `ALPACA_OK` le publie, `AlpacaSDKUnavailable` le nomme
    ALPACA_OK = False


class AlpacaSDKUnavailable(RuntimeError):
    """Le SDK Alpaca manque : aucun client ne peut être construit, pour personne.

    Sans cette exception, l'absence du paquet se manifestait par un `NameError`
    sur `TradingClient`, avalé plus loin en `status: "error"` avec un conseil qui
    parlait de clés, de symbole et de marché — trois pistes pour une dépendance
    manquante. Une panne d'environnement ne doit pas se déguiser en panne
    d'identifiants : les deux se réparent à des endroits différents.
    """


def get_user_risk_pct(user_id: str) -> float:
    try:
        prefs = get_preferences(str(user_id))
        val = float(prefs.get("risk_pct") or DEFAULT_RISK_PCT)
        if 0.1 <= val <= 10:
            return val
    except Exception as e:
        print(f"get_user_risk_pct error: {e}")
    return float(DEFAULT_RISK_PCT)


def get_user_equity(user_id: str) -> float:
    try:
        prefs = get_preferences(str(user_id))
        eq = float(prefs.get("paper_equity") or DEFAULT_PAPER_EQUITY)
        if eq > 0:
            return eq
    except Exception as e:
        print(f"get_user_equity error: {e}")
    return float(DEFAULT_PAPER_EQUITY)


def _normalize_symbol(asset: str) -> str:
    """Alpaca stocks: AAPL | crypto souvent BTC/USD selon compte."""
    a = (asset or "").upper().strip()
    if a.endswith("-USD"):
        # format crypto yfinance → essai BTC/USD
        base = a.replace("-USD", "")
        return f"{base}/USD"
    if a.endswith("USD") and len(a) > 3 and not a.isalpha():
        return a
    # action pure
    return a.replace("-USD", "").replace("/USD", "")


def compute_qty(signal: dict, equity: float, risk_pct: float) -> float:
    entry = float(signal.get("entry") or 0)
    sl = float(signal.get("stop_loss") or 0)

    if entry <= 0:
        return 1.0

    risk_amount = equity * (risk_pct / 100.0)
    stop_distance = abs(entry - sl) if sl else entry * 0.01
    if stop_distance <= 0:
        stop_distance = entry * 0.01

    qty = risk_amount / stop_distance
    qty = max(0.001, min(qty, MAX_POSITION_QTY))

    asset = str(signal.get("asset", ""))
    if not asset.endswith("-USD") and "BTC" not in asset and "ETH" not in asset:
        qty = max(1, int(qty))
    else:
        qty = round(qty, 6)

    return qty


def get_alpaca_client(user_id: Optional[str] = None):
    """
    Priorité 1 : compte personnel de l'utilisateur (chiffré en base), s'il en
    a connecté un via /connect_broker.
    Priorité 2 (repli) : la clé Alpaca partagée du propriétaire du bot
    (config.py), utile pour tes propres tests, mais PAS pour un vrai
    déploiement multi-clients.

    Le paramètre `paper` vient TOUJOURS du choix explicite de l'utilisateur
    (ou True par défaut pour la clé partagée) — jamais deviné.

    Lève `BrokerCredentialsUnreadable` — et ne se rabat **pas** sur la clé
    partagée — quand le compte personnel existe mais que l'anneau de clés ne le
    rouvre pas : exécuter l'ordre d'un client sur le compte du propriétaire n'est
    pas un repli, c'est un autre trade, et rien ne le dirait.

    Lève `AlpacaSDKUnavailable` quand `alpaca-py` n'est pas installé : la
    dépendance se nomme, au lieu de ressortir en `NameError` sur `TradingClient`.
    """
    creds = get_broker_credentials(str(user_id)) if user_id else None
    # L'ordre des deux refus compte : la ligne de **cet** utilisateur d'abord (son
    # verdict la nomme), l'environnement ensuite. Les deux bloquent l'ordre, mais
    # un seul dit quoi réparer ; inverser ferait disparaître la ligne illisible
    # derrière « le SDK manque », ce que `ReadTest` refuse déjà côté lecture.
    if not ALPACA_OK:
        raise AlpacaSDKUnavailable(
            "le SDK Alpaca est absent (`alpaca-py`) : installe les dépendances "
            "(`pip install -r requirements.txt`). Sans lui, aucun client ne peut "
            "être construit — ni le compte personnel, ni la clé partagée."
        )
    if creds:
        # garde-fou de sécurité : si le kill-switch global PAPER_TRADING
        # est actif, on refuse d'utiliser un compte marqué "live" même si
        # l'utilisateur l'a connecté ainsi.
        paper = creds["paper"] or PAPER_TRADING
        return TradingClient(
            api_key=creds["api_key"],
            secret_key=creds["api_secret"],
            paper=paper,
        ), "personal"

    # paper=True suffit : le SDK alpaca-py construit lui-même la bonne URL
    # (https://paper-api.alpaca.markets/v2). Passer url_override en plus
    # casse le routing interne (404 Not Found sur toutes les routes).
    return TradingClient(
        api_key=ALPACA_API_KEY,
        secret_key=ALPACA_SECRET_KEY,
        paper=True,
    ), "shared"


def _is_demo_or_invalid(signal: dict) -> bool:
    entry = float(signal.get("entry") or 0)
    ta = str(signal.get("ta_summary") or "")
    reasoning = str(signal.get("reasoning") or "")
    if entry <= 0:
        return True
    if "DEMO" in ta.upper() or "démonstration" in reasoning.lower() or "demonstration" in reasoning.lower():
        return True
    return False


async def execute_validated_order(
    user_id: str,
    signal: Dict[str, Any],
    qty: Optional[float] = None,
) -> Dict[str, Any]:
    if not PAPER_TRADING:
        return {"error": "Live trading désactivé. PAPER only."}

    valid_signal, notes, signal = validate_signal(signal, allow_demo=True)
    if not valid_signal:
        return {
            "status": "rejected_invalid_signal",
            "method": "validator",
            "error": "; ".join(notes),
            "signal": signal,
        }

    risk_pct = get_user_risk_pct(user_id)
    equity = get_user_equity(user_id)

    if qty is None:
        qty = compute_qty(signal, equity=equity, risk_pct=risk_pct)

    base = {
        "asset": signal.get("asset"),
        "direction": signal.get("direction"),
        "qty": qty,
        "risk_pct": risk_pct,
        "equity": equity,
        "entry": signal.get("entry"),
        "stop_loss": signal.get("stop_loss"),
        "take_profit": signal.get("take_profit"),
        "user_id": str(user_id),
        "risk_reward_ratio": signal.get("risk_reward_ratio"),
        "quality_warnings": signal.get("quality_warnings") or [],
    }

    # DEMO / entry=0 → simulation pure, PAS d'appel Alpaca
    if _is_demo_or_invalid(signal):
        return {
            **base,
            "status": "simulated_paper",
            "method": "simulated",
            "note": "Signal DEMO ou entry invalide → pas d'envoi broker. Risk user quand même appliqué.",
        }

    try:
        has_personal = get_broker_credentials(str(user_id)) is not None
    except BrokerCredentialsUnreadable as exc:
        return {
            **base,
            "status": "blocked_broker_credentials",
            "method": "blocked",
            "note": (
                "🛑 Ordre refusé : tes identifiants broker sont chiffrés avec une clé "
                "qui ne les rouvre pas — le compte partagé n'est PAS utilisé à la place."
            ),
            "error": str(exc),
        }
    if not has_personal and not ALPACA_OK:
        return {
            **base,
            "status": "simulated_paper",
            "method": "simulated",
            "note": (
                "SDK Alpaca non installé → simulation pure (risk user appliqué). "
                "`pip install -r requirements.txt` pour envoyer de vrais ordres paper."
            ),
        }
    if not has_personal and (not ALPACA_API_KEY or not ALPACA_SECRET_KEY):
        return {
            **base,
            "status": "simulated_paper",
            "method": "simulated",
            "note": "Aucun compte connecté et aucune clé partagée → simulation pure (risk user appliqué)",
        }

    # Le solde **réel** est une entrée du garde-fou de risque, donc il ne se
    # remplace pas : un repli silencieux sur `equity` (préférence utilisateur, ou
    # défaut) ferait valider l'ordre sur un chiffre statique **en le présentant
    # comme le solde du compte**. C'est ainsi qu'une clé morte, un compte révoqué
    # ou une panne réseau ressemblaient à un solde connu.
    try:
        client_probe, _ = get_alpaca_client(str(user_id))
        real_balance = float(client_probe.get_account().equity)
    except AlpacaSDKUnavailable as exc:
        return {
            **base,
            "status": "blocked_broker_sdk_missing",
            "method": "blocked",
            "note": (
                "🛑 Ordre refusé : le SDK Alpaca n'est pas installé, donc aucun solde "
                "ne peut être lu — le compte partagé ne prend pas le relais pour autant."
            ),
            "error": str(exc),
        }
    except Exception as exc:
        return {
            **base,
            "status": "blocked_equity_unknown",
            "method": "blocked",
            "note": (
                "🛑 Ordre refusé : le solde du compte n'a pas pu être lu, donc le "
                "garde-fou de risque n'a pas de solde sur quoi décider (le chiffre "
                "statique n'est pas utilisé à sa place)."
            ),
            "error": f"{type(exc).__name__}: {exc}",
        }

    decision = risk_evaluate(str(user_id), real_balance)
    if not decision.allowed:
        return {
            **base,
            "status": "blocked_risk_guard",
            "method": "blocked",
            "note": f"🛑 Trade bloqué par le garde-fou de risque : {decision.reason}",
            # Le verdict structuré voyage avec le refus : l'appelant affiche où en
            # est le compte et quel seuil a mordu, sans relire la phrase.
            "risk": decision.as_dict(),
        }

    try:
        client, source = get_alpaca_client(str(user_id))
        symbol = _normalize_symbol(signal.get("asset", ""))
        side = OrderSide.BUY if signal.get("direction") == "BUY" else OrderSide.SELL

        # Stocks US: AAPL — qty entière
        order_qty = qty
        if "/" not in symbol and symbol.isalpha():
            order_qty = max(1, int(float(qty)))

        order = client.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=order_qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
        )
        return {
            **base,
            "status": "submitted_paper",
            "order_id": str(order.id),
            "symbol": getattr(order, "symbol", symbol),
            "side": str(getattr(order, "side", side)),
            "qty": str(getattr(order, "qty", order_qty)),
            "method": "alpaca_paper",
            "account": source,  # "personal" ou "shared"
        }
    except AlpacaSDKUnavailable as exc:
        # Le SDK ne peut pas disparaître entre la sonde et l'envoi dans le même
        # processus : si on est ici, c'est que la ligne broker a changé sous nos
        # pieds. Le nom reste le même — celui de la cause, pas un « error » vague.
        return {
            **base,
            "status": "blocked_broker_sdk_missing",
            "method": "blocked",
            "error": str(exc),
        }
    except Exception as e:
        return {
            **base,
            "status": "error",
            "error": str(e),
            "method": "alpaca_paper",
            "note": "Échec Alpaca — risk_pct déjà calculé. Vérifie clés paper + symbole + marché ouvert.",
        }
