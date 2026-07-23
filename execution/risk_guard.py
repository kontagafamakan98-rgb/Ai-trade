# execution/risk_guard.py
"""
Garde-fous de risque avancés par utilisateur.
Inclut : perte journalière, drawdown total, corrélation, max positions.
"""
from datetime import date, datetime, timezone
from typing import Tuple, Dict, Any
from database.supabase_client import supabase
from database.preferences import get_preferences

TABLE = "user_risk_state"

# Paires fortement corrélées (à étendre)
CORRELATED_PAIRS = {
    ("NVDA", "AAPL"), ("NVDA", "TSLA"), ("AAPL", "MSFT"),
    ("BTC-USD", "ETH-USD"), ("BTC-USD", "SOL-USD"),
    ("TSLA", "NVDA")
}

class RiskGuard:
    @staticmethod
    def _get_or_init_state(user_id: str, balance: float) -> dict:
        res = supabase.table(TABLE).select("*").eq("user_id", user_id).limit(1).execute()
        if res.data:
            state = res.data[0]
            today = date.today().isoformat()
            if state.get("daily_date") != today:
                supabase.table(TABLE).update({
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
        supabase.table(TABLE).insert(new_state).execute()
        return new_state

    @staticmethod
    def _count_open_trades(user_id: str) -> int:
        res = supabase.table("pending_signals").select("id", count="exact")\
            .eq("user_id", user_id)\
            .eq("status", "executed").execute()
        return res.count or 0

    @staticmethod
    def _count_correlated_positions(user_id: str, new_asset: str) -> int:
        """Compte combien de positions ouvertes sont corrélées avec le nouvel actif"""
        res = supabase.table("pending_signals").select("signal")\
            .eq("user_id", user_id)\
            .eq("status", "executed").execute()
        
        count = 0
        for row in (res.data or []):
            asset = (row.get("signal") or {}).get("asset")
            if asset and (asset, new_asset) in CORRELATED_PAIRS or (new_asset, asset) in CORRELATED_PAIRS:
                count += 1
        return count

    @staticmethod
    def can_trade(user_id: str, current_balance: float, new_asset: str = None) -> Tuple[bool, str]:
        try:
            prefs = get_preferences(user_id)
            max_daily_loss_pct = float(prefs.get("max_daily_loss_pct", 5.0))
            max_drawdown_pct = float(prefs.get("max_total_drawdown_pct", 10.0))
            max_open_trades = int(prefs.get("max_open_trades", 4))
            max_correlated = int(prefs.get("max_correlated", 2))

            state = RiskGuard._get_or_init_state(user_id, current_balance)
            starting = float(state.get("starting_balance", current_balance))
            daily_start = float(state.get("daily_start_balance", current_balance))

            open_count = RiskGuard._count_open_trades(user_id)
            if open_count >= max_open_trades:
                return False, f"Max positions ouvertes atteint ({max_open_trades})"

            # Corrélation
            if new_asset:
                correlated = RiskGuard._count_correlated_positions(user_id, new_asset)
                if correlated >= max_correlated:
                    return False, f"Trop de positions corrélées avec {new_asset}"

            # Daily loss
            if daily_start > 0:
                daily_loss = (daily_start - current_balance) / daily_start * 100
                if daily_loss >= max_daily_loss_pct:
                    return False, f"Perte journalière max atteinte ({daily_loss:.1f}%)"

            # Total drawdown
            if starting > 0:
                dd = (starting - current_balance) / starting * 100
                if dd >= max_drawdown_pct:
                    return False, f"Drawdown total max atteint ({dd:.1f}%)"

            return True, ""

        except Exception as e:
            print(f"RiskGuard error: {e}")
            return False, "Erreur garde-fou — trade bloqué par sécurité"