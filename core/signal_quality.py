from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Tuple

from core.config_runtime import get_env_config


VALID_DIRECTIONS = {"BUY", "SELL"}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _round(value: float) -> float:
    return round(float(value), 5)


def compute_risk_reward(signal: Dict[str, Any]) -> float:
    entry = _to_float(signal.get("entry"))
    sl = _to_float(signal.get("stop_loss"))
    tp = _to_float(signal.get("take_profit"))
    direction = str(signal.get("direction") or "").upper()
    if entry <= 0 or sl <= 0 or tp <= 0 or direction not in VALID_DIRECTIONS:
        return 0.0
    if direction == "BUY":
        risk = entry - sl
        reward = tp - entry
    else:
        risk = sl - entry
        reward = entry - tp
    if risk <= 0 or reward <= 0:
        return 0.0
    return reward / risk


def normalize_signal(raw_signal: Dict[str, Any], *, allow_demo: bool = False) -> Tuple[Dict[str, Any], List[str]]:
    cfg = get_env_config()
    signal = deepcopy(raw_signal or {})
    warnings: List[str] = []

    signal["asset"] = str(signal.get("asset") or "").upper().strip()
    signal["direction"] = str(signal.get("direction") or "").upper().strip()
    signal["confidence"] = max(0.0, min(1.0, _to_float(signal.get("confidence"), 0.0)))
    signal["entry"] = _round(_to_float(signal.get("entry"), 0.0))
    signal["stop_loss"] = _round(_to_float(signal.get("stop_loss"), 0.0))
    signal["take_profit"] = _round(_to_float(signal.get("take_profit"), 0.0))

    if signal["direction"] not in VALID_DIRECTIONS:
        warnings.append("direction invalide")
        return signal, warnings

    demo_flag = bool(signal.get("is_demo"))
    if signal["entry"] <= 0:
        if allow_demo or demo_flag:
            signal["is_demo"] = True
            warnings.append("entry absent ou invalide -> mode démo")
            return signal, warnings
        warnings.append("entry invalide")
        return signal, warnings

    direction = signal["direction"]
    entry = signal["entry"]
    sl = signal["stop_loss"]
    tp = signal["take_profit"]

    if sl <= 0:
        sl = _round(entry * (0.99 if direction == "BUY" else 1.01))
        signal["stop_loss"] = sl
        warnings.append("stop_loss reconstruit")
    if tp <= 0:
        tp = _round(entry * (1.02 if direction == "BUY" else 0.98))
        signal["take_profit"] = tp
        warnings.append("take_profit reconstruit")

    if direction == "BUY":
        if sl >= entry:
            signal["stop_loss"] = _round(entry * 0.99)
            warnings.append("stop_loss BUY corrigé sous l'entrée")
        if tp <= entry:
            signal["take_profit"] = _round(entry * 1.02)
            warnings.append("take_profit BUY corrigé au-dessus de l'entrée")
    else:
        if sl <= entry:
            signal["stop_loss"] = _round(entry * 1.01)
            warnings.append("stop_loss SELL corrigé au-dessus de l'entrée")
        if tp >= entry:
            signal["take_profit"] = _round(entry * 0.98)
            warnings.append("take_profit SELL corrigé sous l'entrée")

    rr = compute_risk_reward(signal)
    signal["risk_reward_ratio"] = round(rr, 3)
    signal["quality_warnings"] = warnings
    # `quality_ok` = le ratio risque/récompense atteint le minimum configuré.
    signal["quality_ok"] = rr >= cfg.min_risk_reward_ratio
    return signal, warnings


def validate_signal(signal: Dict[str, Any], *, allow_demo: bool = False) -> Tuple[bool, List[str], Dict[str, Any]]:
    cfg = get_env_config()
    normalized, warnings = normalize_signal(signal, allow_demo=allow_demo)
    issues: List[str] = []

    if not normalized.get("asset"):
        issues.append("asset manquant")
    if normalized.get("direction") not in VALID_DIRECTIONS:
        issues.append("direction invalide")

    if normalized.get("is_demo"):
        return True, warnings, normalized

    if _to_float(normalized.get("entry")) <= 0:
        issues.append("entry invalide")

    rr = compute_risk_reward(normalized)
    if rr < cfg.min_risk_reward_ratio:
        issues.append(f"risk/reward trop faible ({rr:.2f} < {cfg.min_risk_reward_ratio:.2f})")

    if _to_float(normalized.get("confidence")) < 0 or _to_float(normalized.get("confidence")) > 1:
        issues.append("confidence hors borne")

    return len(issues) == 0, warnings + issues, normalized
