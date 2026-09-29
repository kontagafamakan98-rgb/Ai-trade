from fastapi import APIRouter, Query, HTTPException, Body, Depends
from typing import Optional, List, Dict, Any

from api.security import require_api_key
from core.adaptive_learning import (
    get_learning_summary,
    get_adaptive_parameters,
    record_trade_settlement_and_learn,
    _refresh_knowledge_base_lessons
)

router = APIRouter(
    prefix="/learning",
    tags=["AI Self-Learning & Auto-Correction"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/summary")
def get_ai_learning_summary() -> Dict[str, Any]:
    """Retrieve complete summary of AI post-mortem learning, weights, and rules."""
    return get_learning_summary()


@router.get("/parameters/{asset}")
def get_asset_learning_parameters(asset: str) -> Dict[str, Any]:
    """Get dynamic weights, SL/TP multipliers, and adaptive confidence threshold for a specific asset."""
    return get_adaptive_parameters(asset.upper())


@router.post("/feedback")
def submit_trade_feedback(
    signal_id: str = Body(..., embed=True),
    asset: str = Body(..., embed=True),
    direction: str = Body("BUY", embed=True),
    outcome: str = Body(..., embed=True),  # 'won' or 'lost'
    entry_price: Optional[float] = Body(None, embed=True),
    exit_price: Optional[float] = Body(None, embed=True),
    confidence: Optional[float] = Body(0.60, embed=True)
) -> Dict[str, Any]:
    """Submit a trade outcome to trigger instant AI post-mortem learning and weight recalibration.

    Un même `signal_id` n'est appris qu'**une** fois : `record_trade_settlement_and_learn`
    est idempotent par identité, donc un rejeu (retry d'un client, appel manuel répété)
    est ignoré au lieu d'écrire une seconde ligne de post-mortem et de compter deux
    fois le trade. Le résultat le dit (`learning_result.status`).
    """
    outcome_clean = outcome.lower()
    if outcome_clean not in ["won", "lost"]:
        raise HTTPException(status_code=400, detail="Outcome must be 'won' or 'lost'")

    signal_payload = {
        "id": signal_id,
        "asset": asset.upper(),
        "direction": direction.upper(),
        "price": entry_price or 100.0,
        "confidence": confidence
    }

    result = record_trade_settlement_and_learn(
        signal_data=signal_payload,
        outcome=outcome_clean,
        exit_price=exit_price
    )
    #: Un rejeu est un succès — le trade **est** réglé — mais il ne faut pas annoncer
    #: un apprentissage qui n'a pas eu lieu : ni ligne ni compteur n'ont bougé.
    replayed = result.get("status") == "already_settled"
    return {
        "ok": True,
        "message": (
            f"Apprentissage IA déjà enregistré pour {asset} (rejeu ignoré)"
            if replayed
            else f"Apprentissage IA enregistré pour {asset}"
        ),
        "learning_result": result
    }


@router.post("/recalibrate")
def trigger_learning_recalibration() -> Dict[str, Any]:
    """Force refresh of knowledge base post-mortems and adaptive model parameters."""
    _refresh_knowledge_base_lessons()
    summary = get_learning_summary()
    return {
        "ok": True,
        "message": "Recalibrage des modèles d'apprentissage terminé avec succès.",
        "summary": summary
    }
