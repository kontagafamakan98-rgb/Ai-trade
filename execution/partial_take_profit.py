"""
Take-profit partiel : à 2R de gain latent (2x la distance de risque
initiale entre l'entrée et le stop d'origine), on clôture 50% de la
position pour sécuriser du profit. Le reste continue vers l'objectif final
(take_profit d'origine), protégé par le trailing stop déjà en place.

Comportement selon le type de compte :
- Compte personnel connecté avec un vrai ordre soumis -> vrai ordre de
  clôture partielle envoyé à Alpaca.
- Simulation pure (pas d'ordre réel) -> suivi virtuel seulement, pour les
  stats (self_review, /portfolio).

⚠️ Limite connue : le calcul final de gain/perte (won/lost) dans
performance_tracker.py porte sur les 50% restants uniquement — le profit
déjà sécurisé par le take-profit partiel n'est pas encore agrégé dans les
stats de win-rate. Amélioration possible plus tard, pas critique pour
l'instant.
"""
from typing import Dict, Any, Optional
from database.supabase_client import supabase
from utils.market_data import get_last_price

PARTIAL_TP_R_MULTIPLE = 2.0  # se déclenche à 2x le risque initial
PARTIAL_TP_FRACTION = 0.5    # clôture 50% de la position


def apply_partial_take_profit(item: Dict[str, Any], user_id: str) -> Optional[Dict[str, Any]]:
    sig_id = item["id"]
    signal = dict(item.get("signal") or {})
    exec_res = dict(item.get("execution_result") or {})

    if signal.get("partial_tp_taken"):
        return signal  # déjà fait, rien à refaire

    asset = signal.get("asset")
    direction = signal.get("direction")
    try:
        entry = float(signal.get("entry") or 0)
    except (TypeError, ValueError):
        return None

    initial_risk = signal.get("initial_risk")
    if initial_risk is None:
        try:
            initial_risk = abs(entry - float(signal.get("stop_loss") or 0))
        except (TypeError, ValueError):
            return None
    else:
        initial_risk = float(initial_risk)

    if not asset or entry <= 0 or initial_risk <= 0 or direction not in ("BUY", "SELL"):
        return None

    current_price = get_last_price(asset)
    if current_price is None:
        return None

    favorable_move = (current_price - entry) if direction == "BUY" else (entry - current_price)
    if favorable_move < PARTIAL_TP_R_MULTIPLE * initial_risk:
        return None  # pas encore atteint 2R

    method = exec_res.get("method")
    total_qty = exec_res.get("qty")

    if method == "alpaca_paper" and total_qty:
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            from execution.order_executor import get_alpaca_client

            is_stock = "/" not in asset and asset.replace("-", "").isalpha()
            if is_stock:
                close_qty = int(float(total_qty) * PARTIAL_TP_FRACTION)
                total_int = int(float(total_qty))
                if close_qty <= 0 or close_qty >= total_int:
                    # Position trop petite pour être coupée en 2 sans tout
                    # clôturer par erreur -> on ignore le partiel cette fois.
                    print(f"   ℹ️ Take-profit partiel {asset} ignoré (position trop petite : qty={total_qty})")
                    signal["partial_tp_taken"] = True  # ne réessaie pas en boucle
                    signal["partial_tp_skipped"] = "position_too_small"
                    supabase.table("pending_signals").update({"signal": signal}).eq("id", sig_id).execute()
                    return signal
            else:
                close_qty = round(float(total_qty) * PARTIAL_TP_FRACTION, 6)
                if close_qty <= 0:
                    return signal

            client, _ = get_alpaca_client(user_id)
            side = OrderSide.SELL if direction == "BUY" else OrderSide.BUY
            order = client.submit_order(MarketOrderRequest(
                symbol=exec_res.get("symbol", asset),
                qty=close_qty,
                side=side,
                time_in_force=TimeInForce.DAY,
            ))
            print(f"   💰 Take-profit partiel {asset} {direction} : {close_qty} clôturés à {current_price:.4f} (2R atteint)")
            signal["partial_tp_taken"] = True
            signal["partial_tp_price"] = current_price
            signal["partial_tp_order_id"] = str(order.id)
        except Exception as e:
            print(f"   ❌ partial_tp execution error ({asset}): {type(e).__name__}: {e}")
            return None
    else:
        # Simulation pure -> suivi virtuel seulement, pas d'ordre réel.
        signal["partial_tp_taken"] = True
        signal["partial_tp_price"] = current_price
        print(f"   💰 Take-profit partiel {asset} {direction} : simulé à {current_price:.4f} (2R atteint)")

    try:
        supabase.table("pending_signals").update({"signal": signal}).eq("id", sig_id).execute()
    except Exception as e:
        print(f"   ❌ partial_tp DB update error ({asset}): {type(e).__name__}: {e}")

    return signal
