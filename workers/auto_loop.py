import asyncio
import time
from datetime import datetime, timezone

from ai.decision_engine import EmotionlessDecisionEngine
from scrapers.news_geo import fetch_and_push_geopolitical, fetch_fear_greed
from database.supabase_client import supabase, create_pending_signal
from notifications.notify import send_signal_to_user
from workers.signal_guard import recently_sent
from workers.performance_tracker import check_open_signals_performance
from utils.market_data import get_last_price

# Réduite pour tests cloud (tu pourras réélargir après)
WATCHLIST = [
    # Actions US (mega-caps, forte liquidité)
    "AAPL",
    "MSFT",
    "GOOGL",
    "AMZN",
    "NVDA",
    "TSLA",
    # Crypto (supportées par Binance + CoinGecko en fallback)
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "BNB-USD",
    "XRP-USD",
    "DOGE-USD",
]

REFRESH_EVERY = 15 * 60
ANALYZE_EVERY = 10 * 60
TRACK_EVERY = 5 * 60

engine = EmotionlessDecisionEngine()


async def get_active_users():
    res = supabase.table("users").select("id, telegram_chat_id").eq("paper_mode", True).execute()
    return [u for u in (res.data or []) if u.get("telegram_chat_id")]


async def refresh_collective_data():
    print(f"[{datetime.now(timezone.utc).isoformat()}] 🔄 Refresh data collective...")
    try:
        geo = await fetch_and_push_geopolitical()
        fg = await fetch_fear_greed()
        print(f"   → geo={geo}, fear_greed={fg}")
    except Exception as e:
        print(f"   ❌ refresh error: {e}")

    try:
        from database.supabase_client import get_recent_insights
        insights = get_recent_insights(limit=20)
        engine._news_cache.warm_batch(WATCHLIST, insights)
    except Exception as e:
        print(f"   ❌ LLM warm_batch error: {e}")


async def analyze_watchlist_and_notify():
    print(f"[{datetime.now(timezone.utc).isoformat()}] 🧠 Analyse watchlist ({len(WATCHLIST)} actifs)...")
    users = await get_active_users()
    if not users:
        print("   → aucun utilisateur actif")
        # on analyse quand même les prix pour le debug
    else:
        print(f"   → users actifs: {len(users)}")

    for asset in WATCHLIST:
        try:
            price = get_last_price(asset)
            price_txt = f"{price:.4f}" if price is not None else "N/A"
            print(f"   → {asset}: prix={price_txt} (fetch done)")

            signal = engine.analyze(asset)

            if not signal:
                print(f"   → {asset}: prix={price_txt} | pas de signal (neutre)")
                time.sleep(1.2)
                continue

            if recently_sent(asset, signal["direction"]):
                print(f"   → {asset}: signal {signal['direction']} déjà envoyé récemment (skip)")
                time.sleep(1.2)
                continue

            print(
                f"   → {asset}: prix={price_txt} | "
                f"SIGNAL {signal['direction']} conf={signal['confidence']}"
            )

            for u in users or []:
                if recently_sent(asset, signal["direction"], u["id"]):
                    continue
                signal_id = create_pending_signal(u["id"], signal)
                await send_signal_to_user(u["telegram_chat_id"], signal, signal_id)
                print(f"      notifié user {u['id']}")

            time.sleep(1.2)

        except Exception as e:
            print(f"   ❌ {asset}: {e}")
            time.sleep(1.2)


async def run_forever():
    print("✅ Auto-loop démarrée (paper, validation humaine obligatoire)")
    print(f"   Watchlist: {', '.join(WATCHLIST)}")

    await refresh_collective_data()
    await analyze_watchlist_and_notify()
    await check_open_signals_performance()

    last_refresh = last_analyze = last_track = asyncio.get_event_loop().time()

    while True:
        now = asyncio.get_event_loop().time()

        if now - last_refresh >= REFRESH_EVERY:
            await refresh_collective_data()
            last_refresh = now

        if now - last_analyze >= ANALYZE_EVERY:
            await analyze_watchlist_and_notify()
            last_analyze = now

        if now - last_track >= TRACK_EVERY:
            await check_open_signals_performance()
            last_track = now

        await asyncio.sleep(10)