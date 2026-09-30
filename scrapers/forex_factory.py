import re
import hashlib
import logging
import json
import urllib.request
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

try:
    import httpx
except ImportError:  # sans signal : httpx optionnel, l'appelant le voit à l'usage
    httpx = None

try:
    import feedparser
except ImportError:  # sans signal : feedparser optionnel, l'absence est annoncée
    feedparser = None

logger = logging.getLogger(__name__)

PRIMARY_JSON_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SECONDARY_JSON_URL = "https://www.forexfactory.com/ff_calendar_thisweek.json"
FALLBACK_RSS_URL = "https://www.forexfactory.com/ff_calendar_thisweek.xml"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/xml, */*",
}


def parse_numeric_value(val: Optional[str]) -> Optional[float]:
    """Parse string numerical representation with suffixes (%, K, M, B) into a float."""
    if not val or not isinstance(val, str):
        return None
    
    clean_val = val.strip().replace(",", "")
    if not clean_val or clean_val.lower() in ("n/a", "--", "null", "none", ""):
        return None

    # Handle suffix factors
    multiplier = 1.0
    if clean_val.endswith("%"):
        clean_val = clean_val[:-1].strip()
    elif clean_val.upper().endswith("K"):
        multiplier = 1000.0
        clean_val = clean_val[:-1].strip()
    elif clean_val.upper().endswith("M"):
        multiplier = 1000000.0
        clean_val = clean_val[:-1].strip()
    elif clean_val.upper().endswith("B"):
        multiplier = 1000000000.0
        clean_val = clean_val[:-1].strip()

    # Remove currency symbols or prefix characters if any
    clean_val = re.sub(r"[^\d.-]", "", clean_val)
    if not clean_val or clean_val == "-" or clean_val == ".":
        return None

    try:
        return float(clean_val) * multiplier
    except ValueError:
        return None


def normalize_impact(impact_str: Optional[str]) -> str:
    """Normalize Forex Factory impact string into High, Medium, Low, or Non-Economic."""
    if not impact_str:
        return "Low"
    s = str(impact_str).strip().capitalize()
    if "High" in s or "Red" in s:
        return "High"
    if "Med" in s or "Orange" in s or "Medium" in s:
        return "Medium"
    if "Low" in s or "Yellow" in s:
        return "Low"
    return "Non-Economic"


class ForexFactoryClient:
    """Modulable client for fetching and parsing Forex Factory economic calendar events."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    def fetch_calendar(self) -> List[Dict[str, Any]]:
        """Fetch economic calendar events using primary JSON feed, with fallback to RSS."""
        # 1. Try Primary JSON feed
        events = self._fetch_json(PRIMARY_JSON_URL)
        if events:
            return events

        # 2. Try Secondary JSON feed
        events = self._fetch_json(SECONDARY_JSON_URL)
        if events:
            return events

        # 3. Fallback to RSS feed
        logger.warning("Primary/Secondary JSON feeds failed. Attempting RSS fallback for Forex Factory calendar.")
        events = self._fetch_rss(FALLBACK_RSS_URL)
        if events:
            return events

        logger.error("All Forex Factory calendar sources failed. Returning empty list as fallback.")
        return []

    async def fetch_calendar_async(self) -> List[Dict[str, Any]]:
        """Async fetch version using httpx.AsyncClient."""
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=self.timeout, follow_redirects=True) as client:
                res = await client.get(PRIMARY_JSON_URL)
                if res.status_code == 200:
                    raw_data = res.json()
                    return self._parse_json_items(raw_data)
        except Exception as e:
            logger.warning(f"Async Forex Factory primary fetch failed: {e}")

        # Sync fallback
        return self.fetch_calendar()

    def _fetch_json(self, url: str) -> List[Dict[str, Any]]:
        if httpx is not None:
            try:
                with httpx.Client(headers=HEADERS, timeout=self.timeout, follow_redirects=True) as client:
                    resp = client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        return self._parse_json_items(data)
            except Exception as e:
                logger.warning(f"Failed to fetch Forex Factory JSON via httpx from {url}: {e}")
        
        # Fallback to urllib.request
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode('utf-8'))
                    return self._parse_json_items(data)
        except Exception as e:
            logger.warning(f"Failed to fetch Forex Factory JSON via urllib from {url}: {e}")
            
        return []

    def _parse_json_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        parsed_events = []
        for item in items:
            title = item.get("title") or item.get("name") or "Economic Event"
            currency = (item.get("country") or item.get("currency") or "USD").upper()
            date_raw = item.get("date") or item.get("datetime") or ""
            impact = normalize_impact(item.get("impact"))
            
            forecast_str = str(item.get("forecast") or "").strip()
            previous_str = str(item.get("previous") or "").strip()
            actual_str = str(item.get("actual") or "").strip()

            # Format ISO datetime
            try:
                if date_raw.endswith("Z"):
                    dt = datetime.fromisoformat(date_raw.replace("Z", "+00:00"))
                elif "+" in date_raw or "-" in date_raw[10:]:
                    dt = datetime.fromisoformat(date_raw)
                else:
                    dt = datetime.fromisoformat(date_raw).replace(tzinfo=timezone.utc)
                event_date_iso = dt.isoformat()
            except Exception:
                event_date_iso = datetime.now(timezone.utc).isoformat()

            # Unique deterministic ID
            event_hash = hashlib.md5(f"{currency}_{title}_{event_date_iso}".encode("utf-8")).hexdigest()[:12]
            event_id = str(item.get("id") or event_hash)

            parsed_events.append({
                "id": f"ff_{event_hash}",
                "event_id": event_id,
                "title": title,
                "country": item.get("country", currency),
                "currency": currency,
                "event_date": event_date_iso,
                "impact": impact,
                "forecast": forecast_str or None,
                "previous": previous_str or None,
                "actual": actual_str or None,
                "forecast_num": parse_numeric_value(forecast_str),
                "previous_num": parse_numeric_value(previous_str),
                "actual_num": parse_numeric_value(actual_str),
                "raw_data": item
            })
        return parsed_events

    def _fetch_rss(self, url: str) -> List[Dict[str, Any]]:
        try:
            feed = feedparser.parse(url)
            parsed_events = []
            for entry in feed.entries:
                title = entry.title
                summary = entry.get("summary", "")
                currency = "USD"
                for c in ["EUR", "USD", "GBP", "JPY", "CAD", "AUD", "CHF", "NZD", "CNY"]:
                    if c in title or c in summary:
                        currency = c
                        break

                event_hash = hashlib.md5(f"{currency}_{title}".encode("utf-8")).hexdigest()[:12]
                parsed_events.append({
                    "id": f"ff_rss_{event_hash}",
                    "event_id": event_hash,
                    "title": title,
                    "country": currency,
                    "currency": currency,
                    "event_date": datetime.now(timezone.utc).isoformat(),
                    "impact": "High" if "High" in summary else "Medium",
                    "forecast": None,
                    "previous": None,
                    "actual": None,
                    "forecast_num": None,
                    "previous_num": None,
                    "actual_num": None,
                    "raw_data": {"rss_title": title, "summary": summary}
                })
            return parsed_events
        except Exception as e:
            logger.error(f"Error parsing RSS fallback: {e}")
            return []
