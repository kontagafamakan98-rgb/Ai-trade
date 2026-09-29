from fastapi import APIRouter, Query, HTTPException, Depends
from typing import Optional, List, Dict, Any

from api.security import require_api_key
from database.supabase_client import get_economic_events
from core.macro_engine import calculate_symbol_macro_bias, get_currencies_for_symbol
from execution.news_risk_guard import check_news_risk
from workers.forex_factory_worker import fetch_and_push_forex_factory_events

router = APIRouter(
    prefix="/macro",
    tags=["Macro & Forex Factory"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/calendar")
def get_calendar(
    currency: Optional[str] = Query(None, description="ISO Currency code e.g. USD, EUR, GBP"),
    impact: Optional[str] = Query(None, description="Impact level: High, Medium, Low"),
    limit: int = Query(50, ge=1, le=200)
) -> Dict[str, Any]:
    """Retrieve economic calendar events from Forex Factory database."""
    events = get_economic_events(currency=currency, impact=impact, limit=limit)
    high_impact_count = sum(1 for e in events if e.get("impact") == "High")
    return {
        "status": "ok",
        "count": len(events),
        "high_impact_count": high_impact_count,
        "currency_filter": currency,
        "impact_filter": impact,
        "events": events
    }


@router.get("/bias/{symbol}")
def get_symbol_macro_bias(symbol: str) -> Dict[str, Any]:
    """Calculate macro bias score, label, and narrative for a given symbol."""
    symbol = symbol.upper()
    currencies = get_currencies_for_symbol(symbol)
    events = get_economic_events(limit=100)
    
    score, bias_label, reasons = calculate_symbol_macro_bias(symbol, events)
    news_risk = check_news_risk(symbol, events)

    return {
        "symbol": symbol,
        "currencies": currencies,
        "macro_score": round(score, 4),
        "bias_label": bias_label,
        "news_risk_level": news_risk["risk_level"],
        "news_risk_allowed": news_risk["allowed"],
        "news_risk_reason": news_risk["reason"],
        "reasons": reasons,
        "active_news_event": news_risk.get("active_event")
    }


@router.get("/news-risk/{symbol}")
def get_news_risk_status(symbol: str) -> Dict[str, Any]:
    """Get high-impact news risk status for trade execution guard."""
    symbol = symbol.upper()
    events = get_economic_events(limit=100)
    risk_report = check_news_risk(symbol, events)
    return {
        "symbol": symbol,
        "risk_level": risk_report["risk_level"],
        "allowed": risk_report["allowed"],
        "reason": risk_report["reason"],
        "penalty": risk_report["penalty"],
        "active_event": risk_report.get("active_event")
    }


@router.post("/refresh")
async def trigger_calendar_refresh() -> Dict[str, Any]:
    """Manually trigger a Forex Factory calendar refresh."""
    res = await fetch_and_push_forex_factory_events()
    return {
        "ok": True,
        "refresh_result": res
    }
