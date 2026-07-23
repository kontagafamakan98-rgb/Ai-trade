# execution/risk_guard.py
"""
Garde-fous de risque avancés - Version compatible avec l'ancien code
"""
from datetime import date
from typing import Tuple
from database.supabase_client import supabase
from database.preferences import get_preferences

# ====================== FONCTIONS LEGACY (pour compatibilité) ======================
def can_trade(user_id: str, current_balance: float, new_asset: str = None) -> Tuple[bool, str]:
    """Fonction legacy pour ne pas casser order_executor.py"""
    return RiskGuard.can_trade(user_id, current_balance, new_asset)


def _get_or_init_state(user_id: str, balance: float):
    """Legacy"""
    return RiskGuard._get_or_init_state(user_id, balance)


# ====================== NOUVELLE IMPLÉMENTATION ======================
class RiskGuard:
    @staticmethod
    def _get_or_init_state(user_id: str, balance: float):
        res = supabase.table("user_risk_state").select("*").eq("user_id", user_id).limit(1).execute()
        if res.data:
            state = res.data[0]
            today = date.today().isoformat()
            if state.get("daily_date") != today:
                supabase.table("user_risk_state").update({
                    "daily_start_balance": balance,
                    "daily_date": today,
                }).eq("user_id", user_id).execute()
                state["daily_start_balance"] = balance
                state["daily_date"] = today
            return state

        new_state = {
            "user_id": user_id,
            "starting_balance": balance,
            "daily_start_balance": balance,
            "daily_date": date.today().isoformat(),
        }
        supabase.table("user_risk_state").insert(new_state).execute()
        return new_state

    @staticmethod
    def can_trade(user_id: str, current_balance: float, new_asset: str = None) -> Tuple[bool, str]:
        try:
            prefs = get_preferences(user_id)
            max_daily_loss_pct = float(prefs.get("max_daily_loss_pct", 5.0))
            max_drawdown_pct = float(prefs.get("max_total_drawdown_pct", 10.0))
            max_open_trades = int(prefs.get("max_open_trades", 4))

            state = RiskGuard._get_or_init_state(user_id, current_balance)
            starting_balance = float(state.get("starting_balance", current_balance))
            daily_start_balance = float(state.get("daily_start_balance", current_balance))

            # Nombre de positions ouvertes
            open_count_res = supabase.table("pending_signals").select("id", count="exact")\
                .eq("user_id", user_id).eq("status", "executed").execute()
            open_count = open_count_res.count or 0

            if open_count >= max_open_trades:
                return False, f"Nombre maximum de positions ouvertes atteint ({max_open_trades})"

            # Perte journalière
            if daily_start_balance > 0:
                daily_loss_pct = (daily_start_balance - current_balance) / daily_start_balance * 100
                if daily_loss_pct >= max_daily_loss_pct:
                    return False, f"Limite de perte journalière atteinte ({daily_loss_pct:.2f}%)"

            # Drawdown total
            if starting_balance > 0:
                total_dd_pct = (starting_balance - current_balance) / starting_balance * 100
                if total_dd_pct >= max_drawdown_pct:
                    return False, f"Plafond de drawdown total atteint ({total_dd_pct:.2f}%)"

            return True, ""

        except Exception as e:
            print(f"❌ RiskGuard error: {e}")
            return False, "Erreur lors de la vérification du risque - trade bloqué par sécurité"