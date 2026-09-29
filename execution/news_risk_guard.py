from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from core.macro_engine import get_currencies_for_symbol

PRE_HIGH_IMPACT_MINUTES = 30
POST_HIGH_IMPACT_MINUTES = 15

PRE_MEDIUM_IMPACT_MINUTES = 15
POST_MEDIUM_IMPACT_MINUTES = 10


def parse_event_dt(event_date_raw: Any) -> datetime:
    """Parse ISO date or datetime object into UTC datetime."""
    if isinstance(event_date_raw, datetime):
        if event_date_raw.tzinfo is None:
            return event_date_raw.replace(tzinfo=timezone.utc)
        return event_date_raw
    try:
        s = str(event_date_raw)
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def check_news_risk(
    symbol: str,
    events: List[Dict[str, Any]],
    now_dt: Optional[datetime] = None,
    pre_high_mins: int = PRE_HIGH_IMPACT_MINUTES,
    post_high_mins: int = POST_HIGH_IMPACT_MINUTES,
) -> Dict[str, Any]:
    """
    Check if a trade signal on a given symbol is near high-impact economic calendar news.
    Returns:
    {
        "allowed": bool,
        "risk_level": "BLOCK" | "DEGRADE" | "ALLOWED",
        "reason": str,
        "active_event": Optional[Dict[str, Any]],
        "penalty": float # confidence penalty (0.0 to 0.25)
    }
    """
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    elif now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    currencies = get_currencies_for_symbol(symbol)
    if not events:
        return {
            "allowed": True,
            "risk_level": "ALLOWED",
            "reason": "Filtre News : Aucun événement actif.",
            "active_event": None,
            "penalty": 0.0
        }

    for event in events:
        currency = (event.get("currency") or "").upper()
        if currency not in currencies and currency != "USD":
            continue

        impact = event.get("impact", "Low")
        event_dt = parse_event_dt(event.get("event_date"))
        time_diff_mins = (event_dt - now_dt).total_seconds() / 60.0

        # HIGH IMPACT CHECK -> BLOCK
        if impact == "High":
            # Window: pre_high_mins before event until post_high_mins after event
            if -post_high_mins <= time_diff_mins <= pre_high_mins:
                if time_diff_mins > 0:
                    time_txt = f"dans {int(time_diff_mins)} min"
                else:
                    time_txt = f"il y a {int(abs(time_diff_mins))} min"

                title = event.get("title", "High Impact News")
                return {
                    "allowed": False,
                    "risk_level": "BLOCK",
                    "reason": f"🛑 TRADE BLOQUÉ (News Risk) : Événement majeur imminent/récent pour {currency} [{title} {time_txt}]",
                    "active_event": event,
                    "penalty": 0.30
                }

        # MEDIUM IMPACT CHECK -> DEGRADE
        elif impact == "Medium":
            if -POST_MEDIUM_IMPACT_MINUTES <= time_diff_mins <= PRE_MEDIUM_IMPACT_MINUTES:
                title = event.get("title", "Medium Impact News")
                return {
                    "allowed": True,
                    "risk_level": "DEGRADE",
                    "reason": f"⚠️ CONFANCE DÉGRADÉE (News Risk) : Événement Medium Impact [{currency} - {title}]",
                    "active_event": event,
                    "penalty": 0.15
                }

    return {
        "allowed": True,
        "risk_level": "ALLOWED",
        "reason": f"✅ Filtre News OK : Aucun événement majeur imminente pour {symbol}",
        "active_event": None,
        "penalty": 0.0
    }
