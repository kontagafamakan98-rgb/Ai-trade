import time
import csv
import io
from typing import Optional, List

import httpx
import yfinance as yf

try:
    import logging
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
except Exception:
    pass


def _sleep_brief():
    time.sleep(0.8)


def get_last_price(asset: str) -> Optional[float]:
    asset = asset.upper().strip()

    p = _price_yfinance(asset)
    if p is not None:
        return p

    if asset.endswith("-USD") or asset in ("BTCUSD", "ETHUSD"):
        p = _price_coingecko(asset)
        if p is not None:
            return p
        p = _price_binance(asset)
        if p is not None:
            return p

    p = _price_stooq(asset)
    if p is not None:
        return p

    return None


def get_closes(asset: str, limit: int = 80) -> List[float]:
    asset = asset.upper().strip()

    closes = _closes_yfinance(asset)
    if len(closes) >= 30:
        return closes[-limit:]

    if asset.endswith("-USD") or asset in ("BTCUSD", "ETHUSD"):
        closes = _closes_binance(asset, limit=limit)
        if len(closes) >= 30:
            return closes

    closes = _closes_stooq(asset)
    if len(closes) >= 30:
        return closes[-limit:]

    return []


def _price_yfinance(asset: str) -> Optional[float]:
    try:
        _sleep_brief()
        t = yf.Ticker(asset)
        try:
            fi = t.fast_info
            for key in ("last_price", "lastPrice", "regular_market_price"):
                if hasattr(fi, key) and getattr(fi, key):
                    return float(getattr(fi, key))
                if isinstance(fi, dict) and fi.get(key):
                    return float(fi[key])
        except Exception:
            pass

        hist = t.history(period="5d", interval="1d", auto_adjust=True)
        if hist is not None and not hist.empty:
            col = "Close" if "Close" in hist.columns else "close"
            return float(hist[col].iloc[-1])
    except Exception as e:
        print(f"   yfinance price fail {asset}: {type(e).__name__}")
    return None


def _closes_yfinance(asset: str) -> List[float]:
    try:
        _sleep_brief()
        df = yf.download(
            asset,
            period="60d",
            interval="1h",
            progress=False,
            auto_adjust=True,
            threads=False,
        )
        if df is None or len(df) < 10:
            df = yf.download(
                asset,
                period="6mo",
                interval="1d",
                progress=False,
                auto_adjust=True,
                threads=False,
            )
        if df is None or len(df) < 10:
            return []

        if "Close" in df.columns:
            s = df["Close"]
        elif "close" in df.columns:
            s = df["close"]
        else:
            return []

        if hasattr(s, "ndim") and s.ndim > 1:
            s = s.iloc[:, 0]
        return [float(x) for x in s.dropna().tolist()]
    except Exception as e:
        print(f"   yfinance closes fail {asset}: {type(e).__name__}")
        return []


def _stooq_symbol(asset: str) -> str:
    a = asset.upper().replace("-USD", "").replace("USD", "")
    if a.isalpha() and len(a) <= 5:
        return f"{a.lower()}.us"
    return a.lower()


def _price_stooq(asset: str) -> Optional[float]:
    try:
        sym = _stooq_symbol(asset)
        url = f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            r = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
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
        url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
        with httpx.Client(timeout=20, follow_redirects=True) as client:
            r = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            text = r.text.strip()
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
    a = asset.upper().replace("-USD", "").replace("USD", "")
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
        with httpx.Client(timeout=15) as client:
            r = client.get(url, params={"ids": cid, "vs_currencies": "usd"})
            r.raise_for_status()
            data = r.json()
            return float(data[cid]["usd"])
    except Exception as e:
        print(f"   coingecko fail {asset}: {type(e).__name__}")
        return None


def _binance_symbol(asset: str) -> Optional[str]:
    a = asset.upper().replace("-USD", "USDT").replace("USD", "USDT")
    if a.endswith("USDT"):
        return a
    if a in ("BTC", "ETH"):
        return a + "USDT"
    return None


def _price_binance(asset: str) -> Optional[float]:
    try:
        sym = _binance_symbol(asset)
        if not sym:
            return None
        url = "https://api.binance.com/api/v3/ticker/price"
        with httpx.Client(timeout=15) as client:
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
        with httpx.Client(timeout=20) as client:
            r = client.get(url, params={"symbol": sym, "interval": "1h", "limit": limit})
            r.raise_for_status()
            data = r.json()
            return [float(x[4]) for x in data]
    except Exception as e:
        print(f"   binance closes fail {asset}: {type(e).__name__}")
        return []