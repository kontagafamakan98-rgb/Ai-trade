from datetime import datetime, timezone
from database.supabase_client import supabase
from utils.market_data import get_last_price
from execution.self_review import update_lessons
from reports.performance_report import export_run_card
from core.adaptive_learning import record_trade_settlement_and_learn


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

    any_settled = False

    for item in open_signals:
        sig_id = item["id"]
        signal = item.get("signal") or {}
        asset = signal.get("asset")
        direction = signal.get("direction")
        tp = float(signal.get("take_profit") or 0)
        sl = float(signal.get("stop_loss") or 0)

        if not asset or tp <= 0 or sl <= 0:
            continue

        current_price = get_last_price(asset)
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
            print(
                f"   🏆 Signal {sig_id} ({asset} {direction}) -> "
                f"RÉSULTAT : {outcome.upper()} (Prix : {current_price:.4f})"
            )
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

                # Trigger AI Self-Learning post-mortem and weight recalibration.
                #
                # `id` est la clé **du signal réglé** (`pending_signals.id`) : le
                # payload `signal` ne la porte pas (c'est le moteur qui l'a produit,
                # pas la base qui l'a rangé). Sans elle, le post-mortem s'enregistre
                # sous le repli `sig_0`, donc le trade n'est plus reconnaissable — et
                # `record_trade_settlement_and_learn` ne peut plus le tenir hors de
                # son propre entraînement (voir `core/adaptive_learning.py`).
                learning_res = record_trade_settlement_and_learn(
                    signal_data={**signal, "id": sig_id},
                    outcome=outcome,
                    exit_price=current_price
                )
                lesson = learning_res.get("post_mortem", {}).get("lesson")
                if learning_res.get("status") == "already_settled":
                    #: Le trade **est** réglé, mais rien n'a été appris ici : le
                    #: règlement est idempotent par identité, et un rattrapage ne doit
                    #: pas se lire comme un nouvel apprentissage.
                    print(f"   🧠 Déjà appris (rejeu ignoré) : {lesson}")
                else:
                    print(f"   🧠 Apprentissage IA appliqué: {lesson}")

                any_settled = True
            except Exception as e:
                print(f"   ❌ Erreur update résultat / apprentissage : {e}")

    if any_settled:
        try:
            update_lessons()
            export_run_card()
            print("   → bilan de performance (leçons + run card) mis à jour")
        except Exception as e:
            print(f"   ❌ Erreur mise à jour du bilan : {e}")
