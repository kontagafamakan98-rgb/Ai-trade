# execution/risk_guard.py
from datetime import date
from typing import Tuple
from database.supabase_client import supabase
from database.preferences import get_preferences

# === Pour compatibilité avec l'ancien code ===
def can_trade(user_id: str, current_balance: float, new_asset: str = None) -> Tuple[bool, str]:
    """Fonction legacy pour compatibilité"""
    return RiskGuard.can_trade(user_id, current_balance, new_asset)


def _get_or_init_state(user_id: str, balance: float):
    """Legacy"""
    return RiskGuard._get_or_init_state(user_id, balance)


# === Nouvelle implémentation propre ===
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
            max_daily = float(prefs.get("max_daily_loss_pct", 5.0))
            max_dd = float(prefs.get("max_total_drawdown_pct", 10.0))
            max_open = int(prefs.get("max_open_trades", 4))

            state = RiskGuard._get_or_init_state(user_id, current_balance)
            daily_start = float(state.get("daily_start_balance", current_balance))
            starting = float(state.get("starting_balance", current_balance))

            # Compte positions ouvertes
            open_count = supabase.table("pending_signals").select("id", count="exact")\
                .eq("user_id", user_id).eq("status", "executed").execute().count or 0

            if open_count >= max_open:
                return False, f"Max positions ({max_open}) atteint"

            # Daily loss
            if daily_start > 0:
                daily_loss_pct = (daily_start - current_balance) / daily_start * 100
                if daily_loss_pct >= max_daily:
                    return False, f"Perte journalière max ({daily_loss_pct:.1f}%)"

            # Drawdown total
            if starting > 0:
                dd_pct = (starting - current_balance) / starting * 100
                if dd_pct >= max_dd:
                    return False, f"Drawdown max ({dd_pct:.1f}%)"

            return True, "OK"

        except Exception as e:
            print(f"RiskGuard error: {e}")
            return False, "Erreur risk guard - trade bloqué"