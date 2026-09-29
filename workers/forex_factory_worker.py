import logging
from typing import Dict, Any, List
from scrapers.forex_factory import ForexFactoryClient
from database.supabase_client import upsert_economic_events, get_economic_events

logger = logging.getLogger(__name__)


async def fetch_and_push_forex_factory_events() -> Dict[str, Any]:
    """
    Fetch economic calendar from Forex Factory and persist into Supabase economic_events.
    Includes fallback if primary sources fail.
    """
    client = ForexFactoryClient(timeout=12.0)
    try:
        events = await client.fetch_calendar_async()
        if not events:
            logger.warning("Forex Factory scraper returned 0 events. Attempting database fallback.")
            cached = get_economic_events(limit=50)
            return {
                "status": "fallback",
                "count": len(cached),
                "source": "database_cache",
                "message": "Source distante indisponible, utilisation des événements en cache."
            }

        saved_count = upsert_economic_events(events)
        high_impact_count = sum(1 for e in events if e.get("impact") == "High")
        
        return {
            "status": "success",
            "count": saved_count,
            "high_impact_count": high_impact_count,
            "source": "forex_factory_live"
        }
    except Exception as e:
        logger.error(f"Error in fetch_and_push_forex_factory_events: {e}")
        cached = get_economic_events(limit=50)
        return {
            "status": "error_fallback",
            "error": str(e),
            "count": len(cached),
            "source": "database_cache"
        }
