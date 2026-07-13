import csv
import io
import time
from typing import Optional, List, Dict, Any

import httpx

# Désactive le bruit yfinance si importé ailleurs
try:
    import logging
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
except Exception:
    pass

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

    # 1) Yahoo Chart API (souvent mieux que yfinance sur le cloud)
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

    # 3) Stooq (stocks US)
    p = _price_stooq(asset)
    if p is not None:
        return p

    return None


def get_closes(asset: str, limit: int = 80) -> List[float]:
    asset = asset.upper().strip()

    closes = _yahoo_closes(asset, limit=max(limit, 60))
    if len(closes) >= 30:
        return closes[-limit:]

    if _is_crypto(asset):
        closes = _closes_binance(asset, limit=limit)
        if len(closes) >= 30:
            return closes

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


def _yahoo_symbol(asset: str) -> str:
    # Yahoo aime BTC-USD, AAPL, etc.
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
        except Exception as e:
            print(f"   yahoo chart fail {asset}: {type(e).__name__}")
    return None


def _yahoo_last_price(asset: str) -> Optional[float]:
    try:
        time.sleep(0.4)
        result = _yahoo_chart(asset, interval="1d", range_="5d")
        if not result:
            return None

        meta = result.get("meta") or {}
        for key in ("regularMarketPrice", "previousClose", "chartPreviousClose"):
            if meta.get(key) is not None:
                return float(meta[key])

        quote = (result.get("indicators") or {}).get("quote") or []
        if quote:
            closes = quote[0].get("close") or []
            closes = [c for c in closes if c is not None]
            if closes:
                return float(closes[-1])
    except Exception as e:
        print(f"   yahoo price fail {asset}: {type(e).__name__}")
    return None


def _yahoo_closes(asset: str, limit: int = 80) -> List[float]:
    try:
        time.sleep(0.4)
        # d'abord 1h sur 60j, sinon 1d sur 6mo
        result = _yahoo_chart(asset, interval="1h", range_="60d")
        if not result:
            result = _yahoo_chart(asset, interval="1d", range_="6mo")
        if not result:
            return []

        quote = (result.get("indicators") or {}).get("quote") or []
        if not quote:
            return []
        closes = quote[0].get("close") or []
        out = [float(c) for c in closes if c is not None]
        return out[-limit:]
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
        # endpoint CSV simple
        url = f"https://stooq.pl/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
        with httpx.Client(timeout=15, follow_redirects=True, headers=HEADERS) as client:
            r = client.get(url)
            if r.status_code != 200:
                # fallback domaine .com
                url2 = f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
                r = client.get(url2)
            if r.status_code != 200:
                print(f"   stooq price fail {asset}: HTTP {r.status_code}")
                return None
            text = r.text.strip()
            if not text or "N/D" in text:
                return None
            reader = csv.DictReader(io.StringIO(text))
            row = next(reader, None)
            if not row:
                return None
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
    a = asset.upper().replace("-USD", "").replace("USD", "").replace("USDT", "")
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


def _binance_symbol(asset: str) -> Optional[str]:
    a = asset.upper().replace("-USD", "USDT")
    a = a.replace("USD", "USDT") if not a.endswith("USDT") else a
    if a in ("BTC", "ETH"):
        a = a + "USDT"
    if a.endswith("USDT"):
        return a
    return None


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