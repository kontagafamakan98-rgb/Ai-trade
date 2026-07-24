"""
Trailing stop automatique — une fois qu'une position a gagné au moins 1R
(1x la distance de risque initiale entre l'entrée et le stop d'origine),
le stop loss se resserre progressivement pour suivre le prix, sans jamais
se desserrer. Protège les gains sans limiter le potentiel de hausse comme
le ferait un take-profit fixe trop proche.

Fonctionne directement sur la colonne JSON `signal` de `pending_signals` —
aucune nouvelle table nécessaire. Deux champs y sont ajoutés au fil de
l'eau : `initial_risk` (figé une fois pour toutes) et `peak_price`
(meilleur prix atteint depuis l'ouverture).
"""
from typing import Dict, Any, Optional
from database.supabase_client import supabase
from utils.market_data import get_last_price


def apply_trailing_stop(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Met à jour (si nécessaire) le stop loss d'une position ouverte pour
    suivre le prix. Retourne le signal à jour (persisté en base si modifié),
    ou None si rien n'a pu être calculé (données manquantes).
    """
    sig_id = item["id"]
    signal = dict(item.get("signal") or {})
    asset = signal.get("asset")
    direction = signal.get("direction")

    try:
        entry = float(signal.get("entry") or 0)
        stop_loss = float(signal.get("stop_loss") or 0)
    except (TypeError, ValueError):
        return None

    if not asset or entry <= 0 or stop_loss <= 0 or direction not in ("BUY", "SELL"):
        return None

    # Distance de risque initiale (1R) — calculée une seule fois à la
    # première exécution, puis figée pour rester une référence stable même
    # si le stop_loss bouge ensuite.
    initial_risk = signal.get("initial_risk")
    if initial_risk is None:
        initial_risk = abs(entry - stop_loss)
        signal["initial_risk"] = initial_risk
    else:
        try:
            initial_risk = float(initial_risk)
        except (TypeError, ValueError):
            return None

    if initial_risk <= 0:
        return None

    current_price = get_last_price(asset)
    if current_price is None:
        return None

    peak = float(signal.get("peak_price") or entry)
    changed = False

    if direction == "BUY":
        if current_price > peak:
            peak = current_price
            signal["peak_price"] = peak
            changed = True
        favorable_move = peak - entry
        if favorable_move >= initial_risk:
            new_sl = round(peak - initial_risk, 5)
            if new_sl > stop_loss:  # ne resserre que dans le bon sens
                signal["stop_loss"] = new_sl
                changed = True
    else:  # SELL
        if current_price < peak:
            peak = current_price
            signal["peak_price"] = peak
            changed = True
        favorable_move = entry - peak
        if favorable_move >= initial_risk:
            new_sl = round(peak + initial_risk, 5)
            if new_sl < stop_loss:
                signal["stop_loss"] = new_sl
                changed = True

    if changed:
        try:
            supabase.table("pending_signals").update({"signal": signal}).eq("id", sig_id).execute()
            if signal.get("stop_loss") != stop_loss:
                print(f"   📈 Trailing stop {asset} {direction} : SL {stop_loss} → {signal['stop_loss']}")
        except Exception as e:
            print(f"   ❌ trailing_stop update error ({asset}): {type(e).__name__}: {e}")

    return signal
