import os
from dotenv import load_dotenv
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.72"))
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-me-super-secret")
DEFAULT_RISK_PCT = float(os.getenv("DEFAULT_RISK_PCT", "1.0"))  # 1% du capital paper
DEFAULT_PAPER_EQUITY = float(os.getenv("DEFAULT_PAPER_EQUITY", "100000"))  # capital simulé
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")