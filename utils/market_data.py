import csv
import io
import time
from typing import Optional, List, Dict, Any

import httpx

try:
    from config import FINNHUB_API_KEY
except Exception:
    import os
    FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}


def get_last_price(asset: str) -> Optional[float]:
    asset = asset.upper().strip()

    # 0) Finnhub en premier pour les stocks US (fiable sur Render)
    if not _is_crypto(asset) and FINNHUB_API_KEY:
        p = _price_finnhub(asset)
        if p is not None:
            return p

    # 1) Yahoo chart
    p = _yahoo_last_price(asset)
    if p is not None:
        return p

    # 2) Crypto
    if _is_crypto(asset):
        p = _price_binance(asset)
        if p is not None:
            return p
        p = _price_coingecko(asset)
        if p is not None:
            return p

    # 3) Stooq (actions uniquement, pas adapté aux cryptos)
    if not _is_crypto(asset):
        p = _price_stooq(asset)
        if p is not None:
            return p

    return None


def get_closes(asset: str, limit: int = 80) -> List[float]:
    asset = asset.upper().strip()

    if not _is_crypto(asset) and FINNHUB_API_KEY:
        closes = _closes_finnhub(asset, limit=max(limit, 60))
        if len(closes) >= 30:
            return closes[-limit:]

    closes = _yahoo_closes(asset, limit=max(limit, 60))
    if len(closes) >= 30:
        return closes[-limit:]

    if _is_crypto(asset):
        closes = _closes_binance(asset, limit=limit)
        if len(closes) >= 30:
            return closes
        closes = _closes_coingecko(asset, limit=limit)
        if len(closes) >= 30:
            return closes
        return []

    closes = _closes_stooq(asset)
    if len(closes) >= 30:
        return closes[-limit:]

    return []


def _is_crypto(asset: str) -> bool:
    a = asset.upper()
    return (
        a.endswith("-USD")
        or a.endswith("USDT")
        or a in ("BTCUSD", "ETHUSD", "BTC", "ETH")
    )


def _finnhub_symbol(asset: str) -> str:
    # AAPL, MSFT...
    return asset.upper().replace("-USD", "").replace("USD", "")


def _price_finnhub(asset: str) -> Optional[float]:
    try:
        sym = _finnhub_symbol(asset)
        url = "https://finnhub.io/api/v1/quote"
        with httpx.Client(timeout=15, headers=HEADERS) as client:
            r = client.get(url, params={"symbol": sym, "token": FINNHUB_API_KEY})
            if r.status_code != 200:
                print(f"   finnhub price fail {asset}: HTTP {r.status_code}")
                return None
            data = r.json()
            # c = current price
            price = data.get("c")
            if price is not None and float(price) > 0:
                return float(price)
    except Exception as e:
        print(f"   finnhub price fail {asset}: {type(e).__name__}")
    return None


def _closes_finnhub(asset: str, limit: int = 80) -> List[float]:
    """Chandelles daily Finnhub (gratuit)."""
    try:
        import time as _t
        sym = _finnhub_symbol(asset)
        now = int(_t.time())
        # ~120 jours de daily
        fr = now - 120 * 24 * 3600
        url = "https://finnhub.io/api/v1/stock/candle"
        with httpx.Client(timeout=20, headers=HEADERS) as client:
            r = client.get(
                url,
                params={
                    "symbol": sym,
                    "resolution": "D",
                    "from": fr,
                    "to": now,
                    "token": FINNHUB_API_KEY,
                },
            )
            if r.status_code != 200:
                print(f"   finnhub candles fail {asset}: HTTP {r.status_code}")
                return []
            data = r.json()
            if data.get("s") != "ok":
                return []
            closes = data.get("c") or []
            out = [float(x) for x in closes if x is not None]
            return out[-limit:]
    except Exception as e:
        print(f"   finnhub candles fail {asset}: {type(e).__name__}")
        return []


def _crypto_base(asset: str) -> Optional[str]:
    """Extrait le ticker de base (BTC, ETH...) depuis n'importe quel format
    d'entrée (BTC, BTCUSD, BTC-USD, BTCUSDT, btc...)."""
    if not _is_crypto(asset):
        return None
    a = asset.upper().strip()
    for suffix in ("-USD", "USDT", "USD"):
        if a.endswith(suffix):
            a = a[: -len(suffix)]
            break
    return a or None


def _yahoo_symbol(asset: str) -> str:
    base = _crypto_base(asset)
    if base:
        return f"{base}-USD"
    return asset.upper().strip()


def _yahoo_chart(asset: str, interval: str = "1d", range_: str = "6mo") -> Optional[Dict[str, Any]]:
    sym = _yahoo_symbol(asset)
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval={interval}&range={range_}",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?interval={interval}&range={range_}",
    ]
    for url in urls:
        try:
            with httpx.Client(timeout=20, follow_redirects=True, headers=HEADERS) as client:
                r = client.get(url)
                if r.status_code != 200:
                    continue
                data = r.json()
                result = (data.get("chart") or {}).get("result") or []
                if result:
                    return result[0]
        except Exception:
            pass
    return None


def _yahoo_last_price(asset: str) -> Optional[float]:
    try:
        time.sleep(0.3)
        result = _yahoo_chart(asset, interval="1d", range_="5d")
        if not result:
            return None
        meta = result.get("meta") or {}
        for key in ("regularMarketPrice", "previousClose", "chartPreviousClose"):
            if meta.get(key) is not None:
                return float(meta[key])
        quote = (result.get("indicators") or {}).get("quote") or []
        if quote:
            closes = [c for c in (quote[0].get("close") or []) if c is not None]
            if closes:
                return float(closes[-1])
    except Exception as e:
        print(f"   yahoo price fail {asset}: {type(e).__name__}")
    return None


def _yahoo_closes(asset: str, limit: int = 80) -> List[float]:
    try:
        time.sleep(0.3)
        result = _yahoo_chart(asset, interval="1d", range_="6mo")
        if not result:
            return []
        quote = (result.get("indicators") or {}).get("quote") or []
        if not quote:
            return []
        closes = [float(c) for c in (quote[0].get("close") or []) if c is not None]
        return closes[-limit:]
    except Exception as e:
        print(f"   yahoo closes fail {asset}: {type(e).__name__}")
        return []


def _stooq_symbol(asset: str) -> str:
    a = asset.upper().replace("-USD", "").replace("USD", "")
    if a.isalpha() and len(a) <= 5:
        return f"{a.lower()}.us"
    return a.lower()


def _price_stooq(asset: str) -> Optional[float]:
    try:
        sym = _stooq_symbol(asset)
        urls = [
            f"https://stooq.pl/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv",
            f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv",
        ]
        with httpx.Client(timeout=15, follow_redirects=True, headers=HEADERS) as client:
            for url in urls:
                r = client.get(url)
                if r.status_code != 200:
                    continue
                text = r.text.strip()
                if not text or "N/D" in text:
                    continue
                reader = csv.DictReader(io.StringIO(text))
                row = next(reader, None)
                if not row:
                    continue
                close = row.get("Close") or row.get("close")
                if close and close != "N/D":
                    return float(close)
    except Exception as e:
        print(f"   stooq price fail {asset}: {type(e).__name__}")
    return None


def _closes_stooq(asset: str) -> List[float]:
    try:
        sym = _stooq_symbol(asset)
        urls = [
            f"https://stooq.pl/q/d/l/?s={sym}&i=d",
            f"https://stooq.com/q/d/l/?s={sym}&i=d",
        ]
        with httpx.Client(timeout=20, follow_redirects=True, headers=HEADERS) as client:
            text = ""
            for url in urls:
                r = client.get(url)
                if r.status_code == 200:
                    text = r.text.strip()
                    break
            if not text or text.lower().startswith("<!"):
                return []
            reader = csv.DictReader(io.StringIO(text))
            closes = []
            for row in reader:
                c = row.get("Close") or row.get("close")
                if c and c != "N/D":
                    try:
                        closes.append(float(c))
                    except Exception:
                        pass
            return closes
    except Exception as e:
        print(f"   stooq closes fail {asset}: {type(e).__name__}")
        return []


def _coingecko_id(asset: str) -> Optional[str]:
    a = _crypto_base(asset)
    if not a:
        return None
    mapping = {
        "BTC": "bitcoin",
        "ETH": "ethereum",
        "SOL": "solana",
        "BNB": "binancecoin",
        "XRP": "ripple",
        "ADA": "cardano",
        "DOGE": "dogecoin",
    }
    return mapping.get(a)


def _price_coingecko(asset: str) -> Optional[float]:
    try:
        cid = _coingecko_id(asset)
        if not cid:
            return None
        url = "https://api.coingecko.com/api/v3/simple/price"
        with httpx.Client(timeout=15, headers=HEADERS) as client:
            r = client.get(url, params={"ids": cid, "vs_currencies": "usd"})
            r.raise_for_status()
            data = r.json()
            return float(data[cid]["usd"])
    except Exception as e:
        print(f"   coingecko fail {asset}: {type(e).__name__}")
        return None


def _closes_coingecko(asset: str, limit: int = 80) -> List[float]:
    """Historique quotidien via CoinGecko (fallback quand Yahoo ET Binance échouent,
    ex: Binance géobloqué depuis certains hébergeurs cloud)."""
    try:
        cid = _coingecko_id(asset)
        if not cid:
            return []
        days = min(90, max(limit, 30))
        url = f"https://api.coingecko.com/api/v3/coins/{cid}/market_chart"
        with httpx.Client(timeout=20, headers=HEADERS) as client:
            r = client.get(url, params={"vs_currency": "usd", "days": days, "interval": "daily"})
            r.raise_for_status()
            data = r.json()
            prices = data.get("prices") or []
            closes = [float(p[1]) for p in prices if p and p[1] is not None]
            return closes[-limit:]
    except Exception as e:
        print(f"   coingecko closes fail {asset}: {type(e).__name__}")
        return []


def _binance_symbol(asset: str) -> Optional[str]:
    base = _crypto_base(asset)
    return f"{base}USDT" if base else None


def _price_binance(asset: str) -> Optional[float]:
    try:
        sym = _binance_symbol(asset)
        if not sym:
            return None
        url = "https://api.binance.com/api/v3/ticker/price"
        with httpx.Client(timeout=15, headers=HEADERS) as client:
            r = client.get(url, params={"symbol": sym})
            r.raise_for_status()
            return float(r.json()["price"])
    except Exception as e:
        print(f"   binance price fail {asset}: {type(e).__name__}")
        return None


def _closes_binance(asset: str, limit: int = 80) -> List[float]:
    try:
        sym = _binance_symbol(asset)
        if not sym:
            return []
        url = "https://api.binance.com/api/v3/klines"
        with httpx.Client(timeout=20, headers=HEADERS) as client:
            r = client.get(url, params={"symbol": sym, "interval": "1h", "limit": limit})
            r.raise_for_status()
            data = r.json()
            return [float(x[4]) for x in data]
    except Exception as e:
        print(f"   binance closes fail {asset}: {type(e).__name__}")
        return []