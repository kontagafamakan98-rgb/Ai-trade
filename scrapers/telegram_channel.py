"""
Scraper pour canaux Telegram PUBLICS, via la page d'aperçu web que Telegram
expose pour tout canal public (t.me/s/<nom>) — aucune authentification
nécessaire, aucun compte Telegram requis. Fonctionne uniquement pour les
canaux publics (pas les groupes/canaux privés).
"""
import httpx
from bs4 import BeautifulSoup
from database.supabase_client import insert_insight

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


async def fetch_and_push_telegram_channel(channel: str, limit: int = 15) -> int:
    """
    Récupère les derniers messages d'un canal Telegram public et les pousse
    dans la BDD collective (même table `insights` que les autres sources).
    """
    url = f"https://t.me/s/{channel}"
    count = 0
    try:
        async with httpx.AsyncClient(timeout=15, headers=HEADERS) as client:
            r = await client.get(url)
            r.raise_for_status()

        soup = BeautifulSoup(r.text, "lxml")
        messages = soup.select("div.tgme_widget_message_text")[-limit:]

        for msg in messages:
            text = msg.get_text(separator=" ", strip=True)
            if not text or len(text) < 5:
                continue
            insert_insight({
                "type": "telegram_channel",
                "asset": "GLOBAL",
                "title": f"Canal Telegram @{channel}",
                "summary": text[:1200],
                "source": f"https://t.me/{channel}",
                "confidence": 0.55,
                "data": {"channel": channel},
            })
            count += 1
    except Exception as e:
        print(f"   ❌ Telegram channel @{channel} error: {type(e).__name__}: {e}")
    return count
