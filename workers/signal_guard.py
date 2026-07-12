from datetime import datetime, timezone, timedelta
from database.supabase_client import supabase

# Ne pas renvoyer le même actif+direction pendant X minutes
COOLDOWN_MINUTES = 60


def recently_sent(asset: str, direction: str, user_id: str | None = None) -> bool:
    """True si un signal similaire a déjà été envoyé récemment."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=COOLDOWN_MINUTES)).isoformat()

    q = (
        supabase.table("pending_signals")
        .select("id, signal, created_at, user_id")
        .gte("created_at", since)
        .order("created_at", desc=True)
        .limit(50)
    )
    rows = q.execute().data or []

    for row in rows:
        sig = row.get("signal") or {}
        if sig.get("asset") == asset and sig.get("direction") == direction:
            if user_id is None or row.get("user_id") == user_id:
                return True
    return False