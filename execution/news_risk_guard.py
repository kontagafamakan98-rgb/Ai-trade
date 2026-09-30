from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple
from core.macro_engine import get_currencies_for_symbol

PRE_HIGH_IMPACT_MINUTES = 30
POST_HIGH_IMPACT_MINUTES = 15

PRE_MEDIUM_IMPACT_MINUTES = 15
POST_MEDIUM_IMPACT_MINUTES = 10


def parse_event_dt(event_date_raw: Any) -> Optional[datetime]:
    """Parse ISO date or datetime object into UTC datetime.

    Renvoie `None` quand la date est **illisible**, jamais « maintenant ». Un
    repli sur l'instant présent faisait entrer l'événement dans *toutes* les
    fenêtres (`time_diff ≈ 0`), donc une seule date illisible — ou `NULL` en
    base — bloquait le symbole au motif d'un « événement imminent » que rien
    n'étayait. C'est à l'appelant de décider du sort d'un événement non datable,
    et `check_news_risk` le fait par précaution, en le nommant.
    """
    if event_date_raw is None:
        return None
    if isinstance(event_date_raw, datetime):
        if event_date_raw.tzinfo is None:
            return event_date_raw.replace(tzinfo=timezone.utc)
        return event_date_raw
    try:
        s = str(event_date_raw).strip()
        if not s or s.lower() in {"none", "null", "nan"}:
            return None
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _news_result(
    allowed: bool,
    level: str,
    reason: str,
    active_event: Optional[Dict[str, Any]],
    penalty: float,
    undated: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Verdict du filtre.

    `undated_events` figure dans **tous** les verdicts : un événement qu'on n'a
    pas su dater est un fait du rapport, pas une case vide. Ceux qui décident
    (voir `ai/decision_engine.py`) lisent `allowed`/`risk_level`/`reason`, mais un
    poste de supervision peut voir d'un coup d'œil ce que la décision ignorait.
    """
    return {
        "allowed": allowed,
        "risk_level": level,
        "reason": reason,
        "active_event": active_event,
        "penalty": penalty,
        "undated_events": list(undated or []),
    }


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
        "penalty": float # confidence penalty (0.0 to 0.25),
        "undated_events": List[Dict[str, Any]] # événements dont la date est illisible
    }
    """
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    elif now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    currencies = get_currencies_for_symbol(symbol)
    if not events:
        return _news_result(
            True, "ALLOWED", "Filtre News : Aucun événement actif.", None, 0.0
        )

    # Deux temps : on trie d'abord ce qui est *datable* de ce qui ne l'est pas.
    # Le tri se fait une fois pour toutes, sinon un verdict rendu en cours de
    # parcours ne pourrait plus dire ce qu'il a laissé de côté.
    dated: List[Tuple[Dict[str, Any], datetime]] = []
    undated: List[Dict[str, Any]] = []
    for event in events:
        currency = (event.get("currency") or "").upper()
        if currency not in currencies and currency != "USD":
            continue
        event_dt = parse_event_dt(event.get("event_date"))
        if event_dt is None:
            undated.append(event)
        else:
            dated.append((event, event_dt))

    for event, event_dt in dated:
        currency = (event.get("currency") or "").upper()
        impact = event.get("impact", "Low")
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
                return _news_result(
                    False,
                    "BLOCK",
                    f"🛑 TRADE BLOQUÉ (News Risk) : Événement majeur imminent/récent pour {currency} [{title} {time_txt}]",
                    event,
                    0.30,
                    undated,
                )

        # MEDIUM IMPACT CHECK -> DEGRADE
        elif impact == "Medium":
            if -POST_MEDIUM_IMPACT_MINUTES <= time_diff_mins <= PRE_MEDIUM_IMPACT_MINUTES:
                title = event.get("title", "Medium Impact News")
                return _news_result(
                    True,
                    "DEGRADE",
                    f"⚠️ CONFANCE DÉGRADÉE (News Risk) : Événement Medium Impact [{currency} - {title}]",
                    event,
                    0.15,
                    undated,
                )

    # Aucun événement datable dans la fenêtre : reste à décider du sort de ceux
    # qu'on n'a pas su situer. Les ignorer serait un feu vert dont on sait qu'il
    # repose sur une donnée manquante — on refuse donc par précaution, en le
    # disant, plutôt que de laisser croire que la fenêtre a été vérifiée.
    precaution = _undated_precaution(undated)
    if precaution is not None:
        return precaution

    return _news_result(
        True,
        "ALLOWED",
        f"✅ Filtre News OK : Aucun événement majeur imminente pour {symbol}",
        None,
        0.0,
        undated,
    )


def _undated_precaution(undated: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Le verdict d'un événement que rien ne permet de situer dans le temps.

    Un fait **High** non datable bloque (comme le faisait le repli fautif, mais
    pour la bonne raison : on ne peut pas prouver qu'on est hors fenêtre), un fait
    **Medium** dégrade. Un fait Low, ou d'une devise déjà filtrée en amont, ne
    change rien : il ne pouvait déjà rien changer quand il était datable.
    """
    high = [e for e in undated if e.get("impact") == "High"]
    if high:
        event = high[0]
        currency = (event.get("currency") or "").upper()
        title = event.get("title", "High Impact News")
        return _news_result(
            False,
            "BLOCK",
            f"🛑 TRADE BLOQUÉ (News Risk, précaution) : événement High pour {currency} "
            f"dont la date est illisible — impossible de vérifier la fenêtre [{title}]",
            event,
            0.30,
            undated,
        )

    medium = [e for e in undated if e.get("impact") == "Medium"]
    if medium:
        event = medium[0]
        currency = (event.get("currency") or "").upper()
        title = event.get("title", "Medium Impact News")
        return _news_result(
            True,
            "DEGRADE",
            f"⚠️ CONFANCE DÉGRADÉE (News Risk, précaution) : événement Medium pour {currency} "
            f"dont la date est illisible [{title}]",
            event,
            0.15,
            undated,
        )

    return None
