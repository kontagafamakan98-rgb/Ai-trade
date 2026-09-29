import asyncio
import time
from datetime import datetime, timezone

from ai.decision_engine import EmotionlessDecisionEngine
from scrapers.news_geo import fetch_and_push_geopolitical, fetch_fear_greed, fetch_and_push_web_research
from scrapers.telegram_channel import fetch_and_push_telegram_channel
from scrapers.obsidian_sync import fetch_and_sync_obsidian_vault
from database.supabase_client import supabase, create_pending_signal, prune_insights
from notifications.notify import send_signal_to_user
from workers import media_reconcile, scan_backlog, supabase_watch
from workers.signal_guard import recently_sent
from workers.performance_tracker import check_open_signals_performance
from utils.market_data import get_last_price
from core.alert_engine import build_signal, scan_asset, INTERNAL_SOURCE
from database.preferences import get_all_active_preferences, DEFAULT_WATCHLIST
from database import settings

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
# Scan d'alertes EMA20/50 (remplace TradingView). Les chandelles horaires ne se
# retournent pas plus vite : inutile de recalculer toutes les 10 min.
ALERT_EVERY = 15 * 60

engine = EmotionlessDecisionEngine()


async def get_active_users():
    res = supabase.table("users").select("id, telegram_chat_id").eq("paper_mode", True).execute()
    return [u for u in (res.data or []) if u.get("telegram_chat_id")]


_last_web_research = 0.0
WEB_RESEARCH_EVERY = 3600  # 1h — même logique que le cache LLM, pour ménager le quota

# Balayage des canaux Telegram : ni la liste des canaux, ni la période, ni le
# plafond d'extractions ne sont écrits ici. Ils sont **configurables** —
# environnement (`TELEGRAM_CHANNELS`, `TELEGRAM_SCAN_MINUTES`,
# `TELEGRAM_SCAN_MAX_EXTRACTIONS`, validés par `core.config_runtime`) et
# surchargeables en base (`database.settings`), pour changer la liste sans
# redéployer le worker.
_last_telegram_scan = 0.0

#: Le suivi du report d'extractions vit **entre** les balayages : ce qu'il cherche
#: est une suite (« trois balayages d'affilée »), pas le compte d'un seul passage.
#: Gardé en mémoire : un redémarrage repart de zéro, et c'est le comportement sûr
#: — on ne peut pas annoncer bloqué un canal qu'on n'a pas encore observé.
_scan_backlog = scan_backlog.ScanBacklog()

_last_obsidian_sync = 0.0
OBSIDIAN_SYNC_EVERY = 6 * 3600  # 6h — les notes changent rarement, pas besoin de plus


async def refresh_collective_data():
    global _last_web_research, _last_telegram_scan, _last_obsidian_sync
    print(f"[{datetime.now(timezone.utc).isoformat()}] 🔄 Refresh data collective...")
    try:
        geo = await fetch_and_push_geopolitical()
        fg = await fetch_fear_greed()
        from workers.forex_factory_worker import fetch_and_push_forex_factory_events
        ff_res = await fetch_and_push_forex_factory_events()
        print(f"   → geo={geo}, fear_greed={fg}, forex_factory={ff_res.get('status')} ({ff_res.get('count')} events)")
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

    # Lecture des réglages effectifs (base, sinon environnement) : bloquante, donc
    # hors de l'event loop, comme tout le reste du cycle.
    scan_settings = await asyncio.to_thread(settings.telegram_scan_settings)
    channels = scan_settings["channels"]

    if now_ts - _last_telegram_scan >= scan_settings["scan_minutes"] * 60:
        # Ce qui a été écarté dans la configuration est dit **ici**, une fois par
        # période : un canal mal orthographié se voit sans qu'on ait à chercher
        # pourquoi il ne remonte jamais.
        for raw in scan_settings["rejected"]:
            print(
                f"   ⚠️ canal Telegram ignoré : {raw!r} "
                "(pseudo public attendu, ex. « moncanal »)"
            )
        #: Le plafond est dit **avant** la boucle : c'est ce qui explique pourquoi
        #: un canal chargé n'est pas entièrement traité, et sans cette ligne un
        #: balayage partiel se lirait comme un balayage en échec.
        cap = scan_settings["max_extractions"]
        print(
            "   → balayage Telegram : "
            + (", ".join(f"@{channel}" for channel in channels) or "aucun canal")
            + f" (toutes les {scan_settings['scan_minutes']} min, "
            + (f"au plus {cap} extraction(s) par canal" if cap else "sans plafond d'extraction")
            + ")"
        )
        # Le compte de chaque canal sert deux fois : la ligne de journal, et le
        # suivi du balayage — une **suite** d'observations, tenue par le module.
        # `None` = balayage en erreur : rien n'a été mesuré, ce n'est donc ni un
        # report ni une résolution, et la séquence de ce canal n'est pas touchée.
        deferred_by_channel = {}
        #: Le motif des canaux dont la **lecture** a échoué. Sans lui, un canal
        #: injoignable se présenterait comme un canal calme (tous les comptes à
        #: zéro), et c'est précisément l'angle mort que le suivi doit fermer.
        errors_by_channel = {}
        for channel in channels:
            try:
                #: Le surplus est **reporté** : l'aperçu redonne les mêmes
                #: publications au cycle suivant, et celles déjà indexées en sont
                #: écartées sans rien coûter.
                stats = await fetch_and_push_telegram_channel(
                    channel, max_extractions=cap
                )
                error = stats.get("error")
                if error:
                    # Rien n'a été lu : les comptes ne mesurent rien, et les
                    # journaliser comme un canal calme serait un mensonge. Le
                    # suivi reçoit le motif, pas un zéro.
                    errors_by_channel[channel] = error
                    deferred_by_channel[channel] = None
                    print(
                        f"   ❌ canal Telegram @{channel} : aucune lecture "
                        f"— {error}"
                    )
                    continue
                # Les deux comptes ne disent pas la même chose : un média peut être
                # stocké sans texte (vision indisponible, média sans légende), et
                # c'est l'indexé qui alimente `/search` et le contexte du moteur.
                skipped = stats.get("skipped_documents", 0)
                deferred = stats.get("deferred", 0)
                deferred_by_channel[channel] = deferred
                print(
                    f"   → canal Telegram @{channel}: {stats['insights']} messages, "
                    f"{stats['media']} média(s) stocké(s), "
                    f"{stats.get('indexed', 0)} indexé(s) pour la recherche"
                    + (f", {skipped} document(s) ignoré(s) sans URL" if skipped else "")
                    + (f", {deferred} reporté(s) au prochain balayage" if deferred else "")
                )
            except Exception as e:
                print(f"   ❌ Telegram channel error: {e}")
                deferred_by_channel[channel] = None
                errors_by_channel[channel] = f"{type(e).__name__}: {e}"
        await scan_backlog.observe_sweep(
            deferred_by_channel,
            errors=errors_by_channel,
            watch=_scan_backlog,
            cap=cap,
        )
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

    # Rétention : `insights` grossit à chaque refresh (toutes les 15 min) sans
    # purge. On borne la table pour éviter une croissance infinie.
    try:
        removed = prune_insights(keep=1000)
        if removed:
            print(f"   → purge insights: {removed} lignes anciennes supprimées")
    except Exception as e:
        print(f"   ❌ prune_insights error: {e}")


async def analyze_watchlist_and_notify():
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
            # `get_last_price` et `engine.analyze` sont SYNCHRONES et font des
            # appels réseau/DB bloquants : on les exécute hors de l'event loop
            # pour ne pas geler les autres coroutines (idem time.sleep(0.3)
            # interne à market_data).
            price = await asyncio.to_thread(get_last_price, asset)
            price_txt = f"{price:.4f}" if price is not None else "N/A"
            print(f"   → {asset}: prix={price_txt} (fetch done)")

            signal = await asyncio.to_thread(engine.analyze, asset)

            if not signal:
                print(f"   → {asset}: prix={price_txt} | pas de signal (neutre)")
                await asyncio.sleep(1.2)
                continue

            if recently_sent(asset, signal["direction"]):
                print(f"   → {asset}: signal {signal['direction']} déjà envoyé récemment (skip)")
                await asyncio.sleep(1.2)
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

            await asyncio.sleep(1.2)

        except Exception as e:
            print(f"   ❌ {asset}: {e}")
            await asyncio.sleep(1.2)


async def scan_internal_alerts_and_notify():
    """Scanner d'alertes interne (EMA20/50 + ATR) — remplace TradingView.

    Contrairement à `analyze_watchlist_and_notify` (analyse d'état pondérée),
    ce scanner ne réagit qu'à un **événement de croisement** : l'EMA rapide
    franchit l'EMA lente sur la dernière chandelle close. On notifie les
    utilisateurs qui suivent l'actif et dont le seuil de confiance personnel
    est satisfait, avec le même anti-spam que le reste de la boucle.
    """
    prefs_list = get_all_active_preferences()

    effective_watchlist = set(WATCHLIST)
    for p in prefs_list:
        effective_watchlist.update(p.get("watchlist") or [])
    effective_watchlist = sorted(effective_watchlist) or DEFAULT_WATCHLIST

    print(
        f"[{datetime.now(timezone.utc).isoformat()}] 📡 Scan alertes internes "
        f"({len(effective_watchlist)} actifs)..."
    )

    for asset in effective_watchlist:
        try:
            # `scan_asset` et `build_signal` sont bloquants (réseau/DB/LLM) : on
            # les exécute hors de l'event loop pour ne pas geler les coroutines.
            alert = await asyncio.to_thread(scan_asset, asset)
            if not alert:
                continue

            print(f"   → {asset}: ALERTE {alert['action'].upper()} — {alert['message']}")

            signal = await asyncio.to_thread(
                build_signal,
                ticker=alert["ticker"],
                action=alert["action"],
                price=alert["price"],
                stop_loss=alert["stop_loss"],
                take_profit=alert["take_profit"],
                message=alert["message"],
                source=INTERNAL_SOURCE,
            )

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

            await asyncio.sleep(1.2)

        except Exception as e:
            print(f"   ❌ alerte {asset}: {e}")
            await asyncio.sleep(1.2)


# Réconciliation média (bucket ↔ `knowledge_media`) : ni la période ni le seuil
# ne sont écrits ici — ils viennent de la base ou de l'environnement
# (`database.settings`, `core.config_runtime`) pour pouvoir être changés sans
# redéployer. Seule la cadence de **réexamen** d'une veille éteinte est locale :
# sans elle, un `0` en base ne serait jamais relu, et la veille resterait
# éteinte jusqu'au prochain redémarrage.
_last_reconcile = 0.0
_reconcile_settings = None
DISABLED_RECHECK_EVERY = 3600


async def reconcile_media_and_alert():
    """Un cycle de veille média, au rythme configuré — **sans rien supprimer**.

    Le cycle lui-même vit dans `workers.media_reconcile` (testable sans base ni
    Telegram) ; ici, on ne fait que décider **quand** il tourne et lui passer le
    seuil effectif. Les réglages ne sont relus qu'à l'échéance : les interroger à
    chaque tour de boucle (toutes les 10 s) coûterait deux lectures de base pour
    ne rien apprendre.
    """
    global _last_reconcile, _reconcile_settings
    now = asyncio.get_event_loop().time()
    period = (
        _reconcile_settings["minutes"] * 60
        if _reconcile_settings and _reconcile_settings["minutes"] > 0
        else DISABLED_RECHECK_EVERY
    )
    if _last_reconcile and now - _last_reconcile < period:
        return

    resolved = await asyncio.to_thread(settings.media_reconcile_settings)
    _reconcile_settings = resolved
    _last_reconcile = now
    if resolved["minutes"] <= 0:
        print("   [réconciliation] désactivée (MEDIA_RECONCILE_MINUTES=0)")
        return

    print(
        f"[{datetime.now(timezone.utc).isoformat()}] 🩺 Réconciliation média "
        f"(toutes les {resolved['minutes']} min, seuil {resolved['orphan_threshold']})"
    )
    await media_reconcile.reconcile_once(
        threshold=resolved["orphan_threshold"]
    )


# Veille Supabase en **lecture seule** : la période vient de la base ou de
# l'environnement (`SUPABASE_WATCH_MINUTES`), et rien n'est écrit — ni par la
# sonde, ni ici. Même forme que la réconciliation média, y compris le réexamen
# périodique d'une veille éteinte : sans lui, un `0` en base ne serait relu
# qu'au prochain redémarrage.
_last_supabase_watch = 0.0
_supabase_watch_settings = None

#: La **mémoire des verdicts** vit entre les passages : ce qu'on annonce est un
#: changement, pas un état. Gardée en mémoire comme celle du report d'extractions
#: — un redémarrage repart d'un constat, et un constat d'échec est justement ce
#: qu'on veut apprendre au démarrage.
_supabase_watch = supabase_watch.VerdictWatch()


async def supabase_watch_and_alert():
    """Un passage de la veille Supabase, au rythme réglé — **sans rien écrire**.

    La décision (ce qui a changé, quoi en dire, à qui) vit dans
    `workers.supabase_watch`, qui est testable sans base ni Telegram ; ici, on ne
    décide que **quand** le passage a lieu. Les réglages ne sont relus qu'à
    l'échéance : les interroger à chaque tour de boucle (toutes les 10 s) coûterait
    une lecture de base pour ne rien apprendre.
    """
    global _last_supabase_watch, _supabase_watch_settings
    now = asyncio.get_event_loop().time()
    period = (
        _supabase_watch_settings["minutes"] * 60
        if _supabase_watch_settings and _supabase_watch_settings["minutes"] > 0
        else DISABLED_RECHECK_EVERY
    )
    if _last_supabase_watch and now - _last_supabase_watch < period:
        return

    resolved = await asyncio.to_thread(settings.supabase_watch_settings)
    _supabase_watch_settings = resolved
    _last_supabase_watch = now
    if resolved["minutes"] <= 0:
        print("   [veille supabase] désactivée (SUPABASE_WATCH_MINUTES=0)")
        return

    outcome = await supabase_watch.watch_once(watch=_supabase_watch)
    if outcome["reason"] == "probe_failed":
        print(f"   [veille supabase] sonde en échec : {outcome.get('error')}")
        return
    if outcome["reason"]:
        # Le module a déjà journalisé la raison ; on dit seulement que le passage
        # a eu lieu et que rien n'est parti, pour que ça ne se lise pas comme une
        # absence de problème.
        print(
            f"   [veille supabase] {outcome['checked']} vérification(s), "
            f"{outcome['failing']} en échec — alerte NON envoyée ({outcome['reason']})"
        )
        return
    if outcome["changes"]:
        annoncés = outcome["changes"]
        print(
            f"   [veille supabase] {len(annoncés)} changement(s) annoncé(s) : "
            + ", ".join(change["name"] for change in annoncés)
        )
        return
    print(
        f"   [veille supabase] {outcome['checked']} vérification(s), "
        f"{outcome['failing']} en échec — aucun changement"
    )


async def run_forever():
    print("✅ Auto-loop démarrée (paper, validation humaine obligatoire)")
    print(f"   Watchlist: {', '.join(WATCHLIST)}")
    # Dit au démarrage ce qui sera réellement balayé : une liste surchargée en
    # base n'apparaît nulle part ailleurs, et un « plus rien ne remonte » se
    # diagnostique d'abord là.
    scan_settings = await asyncio.to_thread(settings.telegram_scan_settings)
    cap = scan_settings["max_extractions"]
    print(
        "   Canaux Telegram: "
        + (", ".join(f"@{channel}" for channel in scan_settings["channels"]) or "aucun")
        + f" (toutes les {scan_settings['scan_minutes']} min, "
        + (f"au plus {cap} extraction(s) par canal" if cap else "sans plafond d'extraction")
        + ")"
    )
    # La veille média se lit elle aussi (période, seuil) : une veille éteinte et
    # une veille qui tourne sans chat admin se ressemblent trop — « aucun
    # réglage » doit se voir au démarrage, pas se déduire d'un silence.
    reconcile_settings = await asyncio.to_thread(settings.media_reconcile_settings)
    print(
        "   Réconciliation média: "
        + (
            f"toutes les {reconcile_settings['minutes']} min, seuil "
            f"{reconcile_settings['orphan_threshold']} orphelin(s)"
            if reconcile_settings["minutes"] > 0
            else "désactivée (MEDIA_RECONCILE_MINUTES=0)"
        )
    )
    # La veille Supabase se lit aussi : « éteinte » doit se voir au démarrage, pas
    # se déduire de l'absence d'alerte — c'est justement ce qu'elle surveille.
    watch_settings = await asyncio.to_thread(settings.supabase_watch_settings)
    print(
        "   Veille Supabase (lecture seule): "
        + (
            f"toutes les {watch_settings['minutes']} min"
            if watch_settings["minutes"] > 0
            else "désactivée (SUPABASE_WATCH_MINUTES=0)"
        )
    )

    await refresh_collective_data()
    await analyze_watchlist_and_notify()
    await scan_internal_alerts_and_notify()
    await check_open_signals_performance()
    await reconcile_media_and_alert()
    await supabase_watch_and_alert()

    last_refresh = last_analyze = last_track = asyncio.get_event_loop().time()
    last_alert = last_analyze

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

        if now - last_alert >= ALERT_EVERY:
            await scan_internal_alerts_and_notify()
            last_alert = now

        await reconcile_media_and_alert()
        await supabase_watch_and_alert()

        await asyncio.sleep(10)
