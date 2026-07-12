import asyncio
from datetime import datetime, timezone
import yfinance as yf

from ai.decision_engine import EmotionlessDecisionEngine
from scrapers.news_geo import fetch_and_push_geopolitical, fetch_fear_greed
from database.supabase_client import supabase, create_pending_signal
from notifications.notify import send_signal_to_user
from workers.signal_guard import recently_sent
from workers.performance_tracker import check_open_signals_performance

WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META",
    "BTC-USD", "ETH-USD",
]

REFRESH_EVERY = 15 * 60   # 15 min
ANALYZE_EVERY = 10 * 60   # 10 min
TRACK_EVERY = 5 * 60      # 5 min

engine = EmotionlessDecisionEngine()


async def get_active_users():
    res = supabase.table("users").select("id, telegram_chat_id").eq("paper_mode", True).execute()
    return [u for u in (res.data or []) if u.get("telegram_chat_id")]


def get_last_price(asset: str):
    try:
        t = yf.Ticker(asset)
        hist = t.history(period="1d", interval="1m")
        if hist is None or hist.empty:
            hist = t.history(period="5d")
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


async def refresh_collective_data():
    print(f"[{datetime.now(timezone.utc).isoformat()}] 🔄 Refresh data collective...")
    try:
        geo = await fetch_and_push_geopolitical()
        fg = await fetch_fear_greed()
        print(f"   → geo={geo}, fear_greed={fg}")
    except Exception as e:
        print(f"   ❌ refresh error: {e}")


async def analyze_watchlist_and_notify():
    print(f"[{datetime.now(timezone.utc).isoformat()}] 🧠 Analyse watchlist ({len(WATCHLIST)} actifs)...")
    users = await get_active_users()
    if not users:
        print("   → aucun utilisateur actif")
        return

    for asset in WATCHLIST:
        try:
            price = get_last_price(asset)
            price_txt = f"{price:.4f}" if price is not None else "N/A"

            signal = engine.analyze(asset)

            if not signal:
                print(f"   → {asset}: prix={price_txt} | pas de signal (neutre)")
                continue

            if recently_sent(asset, signal["direction"]):
                print(f"   → {asset}: signal {signal['direction']} déjà envoyé récemment (skip)")
                continue

            print(
                f"   → {asset}: prix={price_txt} | "
                f"SIGNAL {signal['direction']} conf={signal['confidence']}"
            )

            for u in users:
                if recently_sent(asset, signal["direction"], u["id"]):
                    continue
                signal_id = create_pending_signal(u["id"], signal)
                await send_signal_to_user(u["telegram_chat_id"], signal, signal_id)
                print(f"      notifié user {u['id']}")

        except Exception as e:
            print(f"   ❌ {asset}: {e}")


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