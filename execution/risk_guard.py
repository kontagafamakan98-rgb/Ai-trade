"""
Garde-fous de risque par utilisateur — inspirés du RiskManager du bot
TradeLocker (limite de perte journalière + plafond de drawdown total +
nombre max de positions ouvertes), adaptés au multi-tenant.

Contrairement au bot TradeLocker (qui bloque avant de placer un ordre en
mémoire), ici l'état est persisté en base (table user_risk_state) pour
survivre aux redémarrages et fonctionner correctement avec plusieurs
utilisateurs en parallèle.

⚠️ Limite connue : pour les comptes utilisant la clé Alpaca PARTAGÉE
(pas de compte broker personnel connecté), on n'a pas de solde réel à
suivre — le calcul utilise `paper_equity` (statique) comme approximation.
Pour un compte personnel connecté, le vrai solde Alpaca est utilisé.
"""
from datetime import date
from typing import Tuple
from database.supabase_client import supabase
from database.preferences import get_preferences

TABLE = "user_risk_state"

# Groupes de corrélation STATIQUES (pas un vrai calcul statistique de
# corrélation sur historique de prix — ce serait un chantier à part entière.
# Ici, une simplification honnête et transparente : des actifs du même
# groupe ont tendance à bouger ensemble, donc on limite le nombre de
# positions ouvertes simultanément dans un même groupe).
CORRELATION_GROUPS = {
    "us_tech": {"AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "TSLA"},
    "crypto_major": {"BTC-USD", "ETH-USD"},
    "crypto_alt": {"SOL-USD", "BNB-USD", "XRP-USD", "DOGE-USD"},
}
MAX_CORRELATED_POSITIONS = 2  # max de positions ouvertes dans un même groupe


def _correlation_group(asset: str) -> str:
    asset = asset.upper()
    for group_name, members in CORRELATION_GROUPS.items():
        if asset in members:
            return group_name
    return "other"


def _count_open_in_group(user_id: str, group: str) -> int:
    res = (
        supabase.table("pending_signals")
        .select("signal")
        .eq("user_id", user_id)
        .eq("status", "executed")
        .execute()
    )
    count = 0
    for row in (res.data or []):
        sig = row.get("signal") or {}
        if _correlation_group(str(sig.get("asset", "")).upper()) == group:
            count += 1
    return count


def _get_or_init_state(user_id: str, balance: float) -> dict:
    res = supabase.table(TABLE).select("*").eq("user_id", user_id).limit(1).execute()
    if res and res.data:
        state = res.data[0]
        today = date.today().isoformat()
        if state.get("daily_date") != today:
            # nouveau jour -> on repart d'un compteur journalier frais
            supabase.table(TABLE).update({
                "daily_start_balance": balance,
                "daily_date": today,
            }).eq("user_id", user_id).execute()
            state["daily_start_balance"] = balance
            state["daily_date"] = today
        return state

    # première fois pour cet utilisateur -> initialisation
    new_state = {
        "user_id": user_id,
        "starting_balance": balance,
        "daily_start_balance": balance,
        "daily_date": date.today().isoformat(),
    }
    supabase.table(TABLE).insert(new_state).execute()
    return new_state


def _count_open_trades(user_id: str) -> int:
    res = (
        supabase.table("pending_signals")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("status", "executed")
        .execute()
    )
    return res.count or 0


def can_trade(user_id: str, current_balance: float, asset: str = "") -> Tuple[bool, str]:
    """Retourne (True, '') si l'utilisateur peut trader, sinon (False, raison)."""
    try:
        prefs = get_preferences(user_id)
        max_daily_loss_pct = float(prefs.get("max_daily_loss_pct") or 5.0)
        max_drawdown_pct = float(prefs.get("max_total_drawdown_pct") or 10.0)
        max_open_trades = int(prefs.get("max_open_trades") or 3)

        state = _get_or_init_state(user_id, current_balance)
        starting_balance = float(state.get("starting_balance") or current_balance)
        daily_start_balance = float(state.get("daily_start_balance") or current_balance)

        open_count = _count_open_trades(user_id)
        if open_count >= max_open_trades:
            return False, f"Nombre max de positions ouvertes atteint ({max_open_trades})."

        if asset:
            group = _correlation_group(asset)
            if group != "other":
                group_count = _count_open_in_group(user_id, group)
                if group_count >= MAX_CORRELATED_POSITIONS:
                    return False, (
                        f"Trop de positions déjà ouvertes sur des actifs corrélés "
                        f"({group}, max {MAX_CORRELATED_POSITIONS}) — évite de tout "
                        f"miser sur le même mouvement de marché."
                    )

        if daily_start_balance > 0:
            daily_loss_pct = (daily_start_balance - current_balance) / daily_start_balance * 100
            if daily_loss_pct >= max_daily_loss_pct:
                return False, f"Limite de perte journalière atteinte ({daily_loss_pct:.2f}% ≥ {max_daily_loss_pct}%)."

        if starting_balance > 0:
            total_dd_pct = (starting_balance - current_balance) / starting_balance * 100
            if total_dd_pct >= max_drawdown_pct:
                return False, f"Plafond de drawdown total atteint ({total_dd_pct:.2f}% ≥ {max_drawdown_pct}%)."

        return True, ""
    except Exception as e:
        # Fail-CLOSED volontaire : un garde-fou de risque qui laisse passer
        # en cas de doute perd tout son sens. Mieux vaut bloquer un trade
        # par précaution (l'utilisateur peut retenter) qu'ignorer un vrai
        # problème de calcul de risque.
        print(f"   ⚠️ risk_guard.can_trade error (fail-closed, trade bloqué): {type(e).__name__}: {e}")
        return False, "Vérification du risque indisponible temporairement — trade bloqué par précaution."
