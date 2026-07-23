"""
Kill-switch global — pause d'urgence pour tout le bot, réservée à l'admin.
État persisté en base (survit aux redémarrages), une seule ligne.
"""
from typing import Tuple
from database.supabase_client import supabase

_cache = {"paused": False, "ts": 0.0}
_CACHE_TTL = 15  # secondes — évite de taper la BDD à chaque check


def is_paused() -> bool:
    import time
    now = time.time()
    if now - _cache["ts"] < _CACHE_TTL:
        return _cache["paused"]
    try:
        res = supabase.table("system_state").select("paused").eq("id", 1).limit(1).execute()
        paused = bool(res.data[0]["paused"]) if res and res.data else False
        _cache["paused"] = paused
        _cache["ts"] = now
        return paused
    except Exception as e:
        print(f"   ⚠️ system_state read error (fail-closed → pause considérée active): {e}")
        return True  # en cas de doute, on bloque plutôt que de laisser trader


def set_paused(paused: bool, by: str = "", reason: str = "") -> Tuple[bool, str]:
    try:
        supabase.table("system_state").update({
            "paused": paused,
            "paused_by": by,
            "paused_reason": reason,
        }).eq("id", 1).execute()
        _cache["paused"] = paused
        _cache["ts"] = 0.0  # force un refresh au prochain is_paused()
        return True, ""
    except Exception as e:
        return False, str(e)
