import feedparser
import httpx
from datetime import datetime, timezone
from database.supabase_client import insert_insight
from config import GROQ_API_KEY

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


async def fetch_and_push_web_research(watchlist):
    """
    Recherche web autonome via groq/compound (agent intégré à Groq, alimenté
    par Tavily) — même clé API que le reste, pas de nouveau fournisseur.

    Appelée une fois par heure (même rythme que le reste du refresh) pour
    éviter de reproduire le problème de quota déjà rencontré. Le résultat
    est poussé comme un insight normal, lu ensuite par l'analyse LLM comme
    n'importe quelle autre source.
    """
    if not GROQ_API_KEY:
        return 0
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)

        asset_list = ", ".join(watchlist)
        query = (
            f"Recherche les actualités financières et géopolitiques les plus "
            f"importantes des dernières heures pouvant affecter ces actifs : "
            f"{asset_list}. Résume en 4-5 points factuels courts, avec la date "
            f"de chaque actualité si disponible. Reste factuel, ne spécule pas."
        )

        resp = client.chat.completions.create(
            model="groq/compound",
            messages=[{"role": "user", "content": query}],
        )
        summary = (resp.choices[0].message.content or "").strip()
        if not summary:
            return 0

        insert_insight({
            "type": "web_research",
            "asset": "GLOBAL",
            "title": "Recherche web autonome (IA)",
            "summary": summary[:1200],
            "source": "groq/compound (Tavily)",
            "confidence": 0.6,
            "data": {"watchlist": watchlist},
        })
        return 1
    except Exception as e:
        print(f"   ❌ Web research error: {type(e).__name__}: {e}")
        return 0