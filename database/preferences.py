from typing import List, Dict, Any, Optional
from database.supabase_client import supabase

DEFAULT_WATCHLIST = ["AAPL", "MSFT", "GOOGL", "BTC-USD", "ETH-USD"]


def get_preferences(user_id: str) -> Dict[str, Any]:
    res = (
        supabase.table("user_preferences")
        .select("*")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    if res and res.data:
        return res.data[0]

    payload = {
        "user_id": user_id,
        "watchlist": DEFAULT_WATCHLIST,
        "risk_pct": 1.0,
        "paper_equity": 100000,
        "notify_enabled": True,
        "min_confidence": 0.55,
    }
    supabase.table("user_preferences").upsert(payload).execute()
    return payload


def set_watchlist(user_id: str, assets: List[str]) -> Dict[str, Any]:
    assets = [a.upper().strip() for a in assets if a and a.strip()]
    if not assets:
        assets = DEFAULT_WATCHLIST
    supabase.table("user_preferences").upsert({
        "user_id": user_id,
        "watchlist": assets,
    }).execute()
    return get_preferences(user_id)


def set_risk(
    user_id: str,
    risk_pct: float,
    paper_equity: Optional[float] = None,
) -> Dict[str, Any]:
    risk_pct = max(0.1, min(float(risk_pct), 5.0))  # 0.1% → 5% max paper
    payload: Dict[str, Any] = {
        "user_id": user_id,
        "risk_pct": risk_pct,
    }
    if paper_equity is not None:
        payload["paper_equity"] = float(paper_equity)
    supabase.table("user_preferences").upsert(payload).execute()
    return get_preferences(user_id)


RISK_MODES = {
    "conservateur": {
        "risk_pct": 0.5,
        "max_daily_loss_pct": 3.0,
        "max_total_drawdown_pct": 6.0,
        "min_confidence": 0.65,
        "max_open_trades": 2,
    },
    "equilibre": {
        "risk_pct": 1.0,
        "max_daily_loss_pct": 5.0,
        "max_total_drawdown_pct": 10.0,
        "min_confidence": 0.55,
        "max_open_trades": 3,
    },
    "agressif": {
        "risk_pct": 2.0,
        "max_daily_loss_pct": 8.0,
        "max_total_drawdown_pct": 15.0,
        "min_confidence": 0.50,
        "max_open_trades": 5,
    },
}


def apply_mode(user_id: str, mode: str) -> Optional[Dict[str, Any]]:
    """Applique un préréglage complet de risque (voir RISK_MODES).
    Retourne les préférences mises à jour, ou None si le mode est inconnu."""
    mode = mode.lower().strip()
    # tolère les variantes courantes d'accents/orthographe
    mode = {"équilibré": "equilibre", "équilibre": "equilibre", "balanced": "equilibre",
            "conservative": "conservateur", "aggressive": "agressif"}.get(mode, mode)
    preset = RISK_MODES.get(mode)
    if not preset:
        return None
    payload = {"user_id": user_id, **preset}
    supabase.table("user_preferences").upsert(payload).execute()
    return get_preferences(user_id)


def get_all_active_preferences() -> List[Dict[str, Any]]:
    """Tous les users paper avec Telegram + prefs."""
    users = (
        supabase.table("users")
        .select("id, telegram_chat_id, paper_mode")
        .eq("paper_mode", True)
        .execute()
        .data
        or []
    )
    out: List[Dict[str, Any]] = []
    for u in users:
        if not u.get("telegram_chat_id"):
            continue
        prefs = get_preferences(u["id"])
        if not prefs.get("notify_enabled", True):
            continue
        out.append({
            "user_id": u["id"],
            "telegram_chat_id": u["telegram_chat_id"],
            "watchlist": prefs.get("watchlist") or DEFAULT_WATCHLIST,
            "risk_pct": float(prefs.get("risk_pct") or 1.0),
            "paper_equity": float(prefs.get("paper_equity") or 100000),
            "min_confidence": float(prefs.get("min_confidence") or 0.55),
        })
    return out