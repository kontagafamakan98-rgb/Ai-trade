import asyncio
import time
from datetime import datetime, timezone

from ai.decision_engine import EmotionlessDecisionEngine
from scrapers.news_geo import fetch_and_push_geopolitical, fetch_fear_greed, fetch_and_push_web_research
from scrapers.telegram_channel import fetch_and_push_telegram_channel
from scrapers.obsidian_sync import fetch_and_sync_obsidian_vault
from database.supabase_client import supabase, create_pending_signal
from notifications.notify import send_signal_to_user
from workers.signal_guard import recently_sent
from workers.performance_tracker import check_open_signals_performance
from utils.market_data import get_last_price
from database.preferences import get_all_active_preferences, DEFAULT_WATCHLIST
from database.system_state import is_paused

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


_last_web_research = 0.0
WEB_RESEARCH_EVERY = 7200  # 2h — évite de retaper la limite journalière du modèle compound

_last_telegram_scan = 0.0
TELEGRAM_SCAN_EVERY = 1800  # 30 min — scraping simple, pas de quota LLM en jeu
TELEGRAM_CHANNELS = ["thehalalwinningteam"]

_last_obsidian_sync = 0.0
OBSIDIAN_SYNC_EVERY = 6 * 3600  # 6h — les notes changent rarement, pas besoin de plus


async def refresh_collective_data():
    global _last_web_research, _last_telegram_scan, _last_obsidian_sync
    print(f"[{datetime.now(timezone.utc).isoformat()}] 🔄 Refresh data collective...")
    try:
        geo = await fetch_and_push_geopolitical()
        fg = await fetch_fear_greed()
        print(f"   → geo={geo}, fear_greed={fg}")
    except Exception as e:
        print(f"   ❌ refresh error: {e}")

    now_ts = time.time()

    # Watchlist effective = défaut + toutes les watchlists personnalisées,
    # pour que le cache LLM et la recherche web couvrent aussi les actifs
    # ajoutés via /watchlist (sinon ils ne bénéficieraient jamais du cache).
    prefs_list = get_all_active_preferences()
    effective_watchlist = set(WATCHLIST)
    for p in prefs_list:
        effective_watchlist.update(p.get("watchlist") or [])
    effective_watchlist = sorted(effective_watchlist) or DEFAULT_WATCHLIST

    if now_ts - _last_telegram_scan >= TELEGRAM_SCAN_EVERY:
        for channel in TELEGRAM_CHANNELS:
            try:
                n = await fetch_and_push_telegram_channel(channel)
                print(f"   → canal Telegram @{channel}: {n} messages ajoutés")
            except Exception as e:
                print(f"   ❌ Telegram channel error: {e}")
        _last_telegram_scan = now_ts

    if now_ts - _last_obsidian_sync >= OBSIDIAN_SYNC_EVERY:
        try:
            n = await fetch_and_sync_obsidian_vault()
            print(f"   → synchro Obsidian: {n} notes mises à jour")
        except Exception as e:
            print(f"   ❌ Obsidian sync error: {e}")
        _last_obsidian_sync = now_ts

    if now_ts - _last_web_research >= WEB_RESEARCH_EVERY:
        try:
            n = await fetch_and_push_web_research(effective_watchlist)
            print(f"   → recherche web autonome: {n} insight ajouté")
            _last_web_research = now_ts
        except Exception as e:
            print(f"   ❌ web research error: {e}")

    try:
        from database.supabase_client import get_recent_insights
        insights = get_recent_insights(limit=20)
        engine._news_cache.warm_batch(effective_watchlist, insights)
    except Exception as e:
        print(f"   ❌ LLM warm_batch error: {e}")


async def analyze_watchlist_and_notify():
    if is_paused():
        print("   🔴 Bot en pause (kill-switch admin) — analyse suspendue.")
        return

    prefs_list = get_all_active_preferences()

    # Watchlist effective = union de la watchlist par défaut + toutes les
    # watchlists personnalisées des utilisateurs actifs. On analyse chaque
    # actif UNE SEULE FOIS (efficace), et on filtre au moment de notifier.
    effective_watchlist = set(WATCHLIST)
    for p in prefs_list:
        effective_watchlist.update(p.get("watchlist") or [])
    effective_watchlist = sorted(effective_watchlist) or DEFAULT_WATCHLIST

    print(f"[{datetime.now(timezone.utc).isoformat()}] 🧠 Analyse watchlist ({len(effective_watchlist)} actifs, {len(prefs_list)} users actifs)...")

    for asset in effective_watchlist:
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

            # Ne notifie que les users qui suivent CET actif et dont le seuil
            # de confiance personnel est satisfait.
            for p in prefs_list:
                if asset not in (p.get("watchlist") or []):
                    continue
                if signal["confidence"] < p.get("min_confidence", 0.55):
                    continue
                if recently_sent(asset, signal["direction"], p["user_id"]):
                    continue
                signal_id = create_pending_signal(p["user_id"], signal)
                await send_signal_to_user(p["telegram_chat_id"], signal, signal_id)
                print(f"      notifié user {p['user_id']}")

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