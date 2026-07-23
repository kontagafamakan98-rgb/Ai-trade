from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_KEY
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from utils.retry import retry_call

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def insert_insight(insight: Dict[str, Any]) -> Dict:
    # Pas de retry ici : un retry sur un insert créerait potentiellement
    # une ligne en double si le 1er essai a en fait réussi côté serveur.
    insight.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    res = supabase.table("insights").insert(insight).execute()
    return res.data[0] if res.data else {}

def get_recent_insights(asset: Optional[str] = None, limit: int = 30) -> List[Dict]:
    def _fetch():
        q = supabase.table("insights").select("*").order("created_at", desc=True).limit(limit)
        if asset:
            q = q.eq("asset", asset)
        return q.execute().data or []
    return retry_call(_fetch, retries=2, base_delay=0.8, label="get_recent_insights")

def create_pending_signal(user_id: str, signal: Dict) -> str:
    # Pas de retry ici non plus : même raison que insert_insight (éviter
    # de créer 2 signaux identiques en cas de timeout après succès réel).
    res = supabase.table("pending_signals").insert({
        "user_id": user_id,
        "signal": signal,
        "status": "pending"
    }).execute()
    if not res.data or not isinstance(res.data, list) or not res.data[0]:
        raise RuntimeError("Failed to create pending signal: Supabase returned no data")
    return str(res.data[0].get("id"))

def update_signal_status(signal_id: str, status: str, execution_result: Optional[Dict] = None):
    # Sûr à retenter : mettre le même statut 2 fois de suite est sans danger.
    payload = {
        "status": status,
        "validated_at": datetime.now(timezone.utc).isoformat()
    }
    if execution_result:
        payload["execution_result"] = execution_result

    def _update():
        supabase.table("pending_signals").update(payload).eq("id", signal_id).execute()
    retry_call(_update, retries=2, base_delay=0.8, label="update_signal_status")

def get_user_session(user_id: str) -> Optional[Dict]:
    def _fetch():
        res = supabase.table("user_sessions").select("*").eq("user_id", user_id).limit(1).execute()
        return res.data[0] if res and res.data else None
    return retry_call(_fetch, retries=2, base_delay=0.8, label="get_user_session")