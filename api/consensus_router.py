from fastapi import APIRouter, HTTPException, Body, Depends
from typing import Optional, Dict, Any

from api.security import require_api_key
from core.consensus_engine import generate_consensus_report, inject_consensus_signal_into_pipeline

router = APIRouter(
    prefix="/consensus",
    tags=["Multi-Agent Signal Consensus Injection"],
    dependencies=[Depends(require_api_key)],
)


@router.get("/report/{asset}")
def get_consensus_report(asset: str) -> Dict[str, Any]:
    """Generate a multi-agent consensus report for an asset."""
    try:
        return generate_consensus_report(asset)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Consensus generation error: {e}")


@router.post("/inject")
async def inject_consensus_signal(
    user_id: str = Body(..., embed=True),
    asset: str = Body(..., embed=True),
    direction: Optional[str] = Body(None, embed=True),
    entry: Optional[float] = Body(None, embed=True),
    stop_loss: Optional[float] = Body(None, embed=True),
    take_profit: Optional[float] = Body(None, embed=True)
) -> Dict[str, Any]:
    """Synthesize multi-agent consensus signal and inject directly into the live/paper execution pipeline."""
    overrides = {}
    if direction: overrides["direction"] = direction.upper()
    if entry: overrides["entry"] = entry
    if stop_loss: overrides["stop_loss"] = stop_loss
    if take_profit: overrides["take_profit"] = take_profit

    try:
        res = await inject_consensus_signal_into_pipeline(
            user_id=user_id,
            asset=asset,
            custom_overrides=overrides if overrides else None
        )
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Consensus signal injection failed: {e}")
