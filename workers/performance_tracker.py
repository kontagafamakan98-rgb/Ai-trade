from datetime import datetime, timezone
import yfinance as yf
from database.supabase_client import supabase

def _get_current_price(asset: str) -> float | None:
    try:
        t = yf.Ticker(asset)
        hist = t.history(period="1d", interval="5m")
        if hist is None or hist.empty:
            hist = t.history(period="5d")
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None

async def check_open_signals_performance():
    print(f"[{datetime.now(timezone.utc).isoformat()}] 🎯 Vérification des positions ouvertes (TP / SL)...")
    
    try:
        res = supabase.table("pending_signals").select("*").eq("status", "executed").execute()
        open_signals = res.data or []
    except Exception as e:
        print(f"   ❌ Erreur lecture pending_signals : {e}")
        return

    if not open_signals:
        return

    for item in open_signals:
        sig_id = item["id"]
        signal = item.get("signal") or {}
        asset = signal.get("asset")
        direction = signal.get("direction")
        tp = float(signal.get("take_profit") or 0)
        sl = float(signal.get("stop_loss") or 0)

        if not asset or tp <= 0 or sl <= 0:
            continue

        current_price = _get_current_price(asset)
        if current_price is None:
            continue

        outcome = None
        if direction == "BUY":
            if current_price >= tp:
                outcome = "won"
            elif current_price <= sl:
                outcome = "lost"
        elif direction == "SELL":
            if current_price <= tp:
                outcome = "won"
            elif current_price >= sl:
                outcome = "lost"

        if outcome:
            print(f"   🏆 Signal {sig_id} ({asset} {direction}) -> RÉSULTAT : {outcome.upper()} (Prix : {current_price:.4f})")
            try:
                exec_res = item.get("execution_result") or {}
                if isinstance(exec_res, dict):
                    exec_res["final_outcome"] = outcome
                    exec_res["closed_price"] = current_price
                    exec_res["closed_at"] = datetime.now(timezone.utc).isoformat()

                supabase.table("pending_signals").update({
                    "status": outcome,
                    "execution_result": exec_res
                }).eq("id", sig_id).execute()
            except Exception as e:
                print(f"   ❌ Erreur update résultat : {e}")