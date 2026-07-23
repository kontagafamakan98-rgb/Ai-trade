# utils/market_data_service.py
import time
from typing import List, Optional, Dict, Any
import httpx

from config import FINNHUB_API_KEY
from utils.retry import retry_async

class MarketDataService:
    """Service centralisé pour récupérer prix et historiques avec fallback robuste."""

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    @staticmethod
    @retry_async(max_attempts=3)
    async def get_last_price(asset: str) -> Optional[float]:
        asset = asset.upper().strip()
        
        # 1. Finnhub (stocks US)
        if FINNHUB_API_KEY and not MarketDataService._is_crypto(asset):
            price = await MarketDataService._price_finnhub(asset)
            if price is not None:
                return price

        # 2. Yahoo Finance
        price = await MarketDataService._price_yahoo(asset)
        if price is not None:
            return price

        # 3. Crypto
        if MarketDataService._is_crypto(asset):
            price = await MarketDataService._price_binance(asset)
            if price is not None:
                return price
            price = await MarketDataService._price_coingecko(asset)
            if price is not None:
                return price

        # 4. Stooq (fallback stocks)
        if not MarketDataService._is_crypto(asset):
            price = await MarketDataService._price_stooq(asset)
            if price is not None:
                return price

        print(f"⚠️ Impossible de récupérer le prix de {asset}")
        return None

    @staticmethod
    @retry_async(max_attempts=3)
    async def get_closes(asset: str, interval: str = "1h", limit: int = 100) -> List[float]:
        asset = asset.upper().strip()

        if interval == "1d" and FINNHUB_API_KEY and not MarketDataService._is_crypto(asset):
            closes = await MarketDataService._closes_finnhub(asset, limit)
            if len(closes) >= 30:
                return closes[-limit:]

        # Yahoo (principal)
        closes = await MarketDataService._closes_yahoo(asset, interval, limit)
        if len(closes) >= 30:
            return closes[-limit:]

        # Crypto fallbacks
        if MarketDataService._is_crypto(asset):
            closes = await MarketDataService._closes_binance(asset, limit)
            if len(closes) >= 20:
                return closes
            closes = await MarketDataService._closes_coingecko(asset, limit)
            if len(closes) >= 20:
                return closes

        print(f"⚠️ Données historiques limitées pour {asset}")
        return []

    @staticmethod
    def _is_crypto(asset: str) -> bool:
        a = asset.upper()
        return a.endswith("-USD") or a.endswith("USDT") or a in ("BTC", "ETH", "SOL", "BNB")

    # ==================== SOURCES ====================

    @staticmethod
    async def _price_finnhub(asset: str) -> Optional[float]:
        try:
            sym = asset.replace("-USD", "").replace("USD", "")
            url = "https://finnhub.io/api/v1/quote"
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, params={"symbol": sym, "token": FINNHUB_API_KEY})
                if r.status_code == 200:
                    data = r.json()
                    return float(data.get("c") or 0)
        except Exception:
            pass
        return None

    @staticmethod
    async def _price_yahoo(asset: str) -> Optional[float]:
        try:
            sym = asset.replace("-USD", "-USD") if MarketDataService._is_crypto(asset) else asset
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
            async with httpx.AsyncClient(timeout=12, headers=MarketDataService.HEADERS) as client:
                r = await client.get(url)
                data = r.json()
                result = data["chart"]["result"][0]
                return float(result["meta"].get("regularMarketPrice") or result["meta"].get("previousClose"))
        except Exception:
            pass
        return None

    @staticmethod
    async def _closes_yahoo(asset: str, interval: str, limit: int) -> List[float]:
        try:
            range_map = {"1h": "60d", "4h": "60d", "1d": "6mo"}
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{asset}?interval={interval}&range={range_map.get(interval, '6mo')}"
            async with httpx.AsyncClient(timeout=15,