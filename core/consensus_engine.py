import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone

from ai.decision_engine import EmotionlessDecisionEngine
from core.adaptive_learning import get_adaptive_parameters
from core.signal_quality import validate_signal
from execution.order_executor import execute_validated_order
from utils.market_data import get_closes, get_last_price

logger = logging.getLogger(__name__)

_engine = EmotionlessDecisionEngine()


def generate_consensus_report(asset: str, closes: Optional[list] = None) -> Dict[str, Any]:
    """
    Generates a Multi-Agent Meta-Consensus report aggregating Technicals, Macro, Sentiment,
    and Adaptive Model Weights.
    """
    asset_clean = asset.upper().strip()
    adaptive_params = get_adaptive_parameters(asset_clean)

    if not closes or len(closes) < 5:
        closes = get_closes(asset_clean, count=50)

    entry_price = get_last_price(asset_clean) or (closes[-1] if closes else 100.0)

    # 1. Decision engine analysis (TA + Macro + Sentiment)
    analysis = _engine.analyze(asset_clean)

    # Si le moteur ne produit AUCUN signal exploitable, on ne fabrique PAS de
    # consensus artificiel : on renvoie NEUTRAL_HOLD, sans direction, et on
    # laisse `is_actionable=False` pour que le pipeline d'exécution refuse.
    if not analysis:
        return {
            "asset": asset_clean,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "consensus_action": "NEUTRAL_HOLD",
            "consensus_score": 0.5,
            "confidence_pct": 50.0,
            "suggested_entry": entry_price,
            "suggested_stop_loss": None,
            "suggested_take_profit": None,
            "direction": None,
            "is_actionable": False,
            "ta_summary": "Aucun signal technique exploitable",
            "macro_summary": "Analyse macro indisponible (pas de signal d'entrée)",
            "sentiment_summary": "Analyse sentiment indisponible (pas de signal d'entrée)",
            "synthesized_reasoning": (
                f"Aucun signal exploitable pour {asset_clean} : pas de consensus fabriqué. "
                "Le moteur d'analyse a renvoyé NO_SIGNAL (critères de confiance non atteints "
                "ou fenêtre de news bloquante)."
            ),
            "adaptive_parameters": adaptive_params,
        }

    prob = float(analysis.get("confidence") or 0.5)
    direction = analysis.get("direction", "BUY")
    entry = float(analysis.get("entry") or entry_price)
    sl = float(analysis.get("stop_loss") or (entry * 0.98))
    tp = float(analysis.get("take_profit") or (entry * 1.04))
    reasoning = str(analysis.get("reasoning") or "")
    ta_sum = str(analysis.get("ta_summary") or "")
    macro_sum = str(analysis.get("geo_summary") or "")
    sent_sum = str(analysis.get("sentiment_summary") or "")

    consensus_action = "STRONG_BUY" if prob >= 0.75 else ("BUY" if prob >= 0.60 else ("STRONG_SELL" if prob <= 0.25 else ("SELL" if prob <= 0.40 else "NEUTRAL_HOLD")))

    return {
        "asset": asset_clean,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "consensus_action": consensus_action,
        "consensus_score": round(prob, 3),
        "confidence_pct": round(prob * 100.0, 1),
        "suggested_entry": entry,
        "suggested_stop_loss": sl,
        "suggested_take_profit": tp,
        "direction": direction,
        "is_actionable": consensus_action in ("STRONG_BUY", "BUY", "SELL", "STRONG_SELL"),
        "ta_summary": ta_sum,
        "macro_summary": macro_sum,
        "sentiment_summary": sent_sum,
        "synthesized_reasoning": reasoning,
        "adaptive_parameters": adaptive_params
    }


async def inject_consensus_signal_into_pipeline(
    user_id: str,
    asset: str,
    custom_overrides: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Synthesizes a multi-agent consensus signal and immediately injects it into the execution pipeline.
    """
    report = generate_consensus_report(asset)

    # Refus explicite : pas de signal réel => on n'injecte rien.
    if not report.get("is_actionable", False):
        return {
            "ok": False,
            "status": "no_actionable_signal",
            "reason": (
                "Le moteur n'a produit aucun signal exploitable pour "
                f"{report.get('asset')} — aucune injection effectuée (pas de consensus fabriqué)."
            ),
            "consensus_report": report,
        }

    direction = (custom_overrides or {}).get("direction") or report["direction"]
    entry = (custom_overrides or {}).get("entry") or report["suggested_entry"]
    sl = (custom_overrides or {}).get("stop_loss") or report["suggested_stop_loss"]
    tp = (custom_overrides or {}).get("take_profit") or report["suggested_take_profit"]

    consensus_signal = {
        "asset": report["asset"],
        "direction": direction,
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "confidence": report["consensus_score"],
        "ta_summary": f"Consensus Multi-Agents [{report['consensus_action']}]: {report['ta_summary']}",
        "geo_summary": report["macro_summary"],
        "sentiment_summary": report["sentiment_summary"],
        "reasoning": f"Injected Consensus Signal ({report['confidence_pct']}% Conf) -> {report['synthesized_reasoning']}",
        "source": "multi_agent_consensus_injector"
    }

    # Validate signal
    valid, notes, validated_signal = validate_signal(consensus_signal, allow_demo=True)
    if not valid:
        return {
            "ok": False,
            "status": "consensus_rejected",
            "reason": f"Signal de consensus non valide : {'; '.join(notes)}",
            "consensus_report": report,
            "raw_signal": consensus_signal
        }

    # Execute order through pipeline
    exec_result = await execute_validated_order(
        user_id=str(user_id),
        signal=validated_signal
    )

    return {
        "ok": True,
        "status": "consensus_injected",
        "asset": report["asset"],
        "consensus_report": report,
        "signal": validated_signal,
        "execution_result": exec_result
    }
