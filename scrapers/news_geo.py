import feedparser
import httpx
from datetime import datetime, timezone
from database.supabase_client import insert_insight

async def fetch_and_push_geopolitical():
    """Pousse des news géopolitiques/financières dans la BDD collective."""
    feeds = [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://www.reutersagency.com/feed/?best-topics=business-finance&post_type=best",
    ]
    count = 0
    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:5]:
                insert_insight({
                    "type": "geopolitical",
                    "asset": "GLOBAL",
                    "title": (entry.get("title") or "")[:500],
                    "summary": (entry.get("summary") or "")[:1200],
                    "source": feed_url,
                    "url": entry.get("link"),
                    "confidence": 0.65,
                    "data": {"published": entry.get("published")},
                })
                count += 1
        except Exception as e:
            print(f"Geo error: {e}")
    return count

async def fetch_fear_greed():
    """Fear & Greed Index → BDD collective."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.alternative.me/fng/?limit=1")
            data = r.json()["data"][0]
            value = int(data["value"])
            insert_insight({
                "type": "sentiment",
                "asset": "GLOBAL",
                "title": f"Fear & Greed: {value} ({data['value_classification']})",
                "summary": f"Index = {value}/100",
                "confidence": 0.9,
                "data": {
                    "score": value,
                    "class": data["value_classification"],
                    "normalized": value / 100.0
                },
            })
            return value
    except Exception as e:
        print(f"Fear&Greed error: {e}")
        return None