"""L'état du risque en un message — lu dans le verdict, pas recomposé.

`execution.risk_guard.evaluate()` rend un `RiskDecision` : une phrase de refus, et
les chiffres qui la portent (seuils configurés, valeurs mesurées, champs fautifs).
Ce module en fait le message de `/risk` : « où en est le compte », ligne à ligne.

Ce qui est affiché est **exactement** ce sur quoi le garde a décidé — pas un second
calcul qui pourrait diverger du premier. Quand une valeur n'est pas mesurable à ce
point du contrôle (un solde illisible n'a pas de drawdown), elle s'affiche en `—`
plutôt qu'en zéro : zéro est un chiffre, l'absence n'en est pas un.

Le module ne fait que composer du texte : il n'envoie rien et ne touche pas à la
base. `main.py` décide de l'envoi.
"""
from __future__ import annotations

from typing import Any, List, Optional

from execution.risk_guard import (
    CODE_BALANCE_UNREADABLE,
    CODE_DAILY_LOSS,
    CODE_ERROR,
    CODE_MAX_OPEN_TRADES,
    CODE_REFERENCE_UNUSABLE,
    CODE_TOTAL_DRAWDOWN,
    RiskDecision,
)

#: Ce qu'on écrit pour une valeur qui n'est pas mesurable ici. Jamais « 0 » : ce
#: serait un chiffre, et un chiffre faux.
MISSING = "—"

#: Les codes du verdict, traduits en mots. Un code inconnu s'affiche tel quel plutôt
#: que d'être masqué : un verdict qu'on ne sait pas nommer reste un verdict.
CODE_LABELS = {
    CODE_BALANCE_UNREADABLE: "solde illisible",
    CODE_REFERENCE_UNUSABLE: "référence de solde inutilisable",
    CODE_MAX_OPEN_TRADES: "plafond de positions atteint",
    CODE_DAILY_LOSS: "perte journalière atteinte",
    CODE_TOTAL_DRAWDOWN: "drawdown total atteint",
    CODE_ERROR: "contrôle du risque indisponible",
}


def _money(value: Optional[float]) -> str:
    """Un montant lisible, ou `—`. L'espace est le séparateur de milliers."""
    if value is None:
        return MISSING
    return f"{float(value):,.2f}".replace(",", " ")


def _percent(value: Optional[float]) -> str:
    if value is None:
        return MISSING
    return f"{float(value):.2f} %"


def _percent_line(name: str, value: Optional[float], limit: Optional[float]) -> str:
    """« Perte du jour : 5.02 % (limite 5.0 %) » — le mesuré d'abord, le seuil ensuite."""
    if limit is None:
        return f"{name} : {_percent(value)}"
    return f"{name} : {_percent(value)} (limite {_percent(limit)})"


def _count_line(name: str, value: Optional[float], limit: Optional[float]) -> str:
    left = MISSING if value is None else str(int(value))
    right = MISSING if limit is None else str(int(limit))
    return f"{name} : {left} / {right}"


def render(decision: RiskDecision, note: str = "") -> str:
    """La carte de `/risk` : l'état du compte, puis le verdict et ses champs fautifs."""
    measured = decision.measured or {}
    limits = decision.limits or {}

    lines: List[str] = [f"📊 État du risque {note}".rstrip(), ""]
    lines.append(f"Solde actuel : {_money(measured.get('balance'))}")
    lines.append(f"Départ du jour : {_money(measured.get('daily_start_balance'))}")
    lines.append(f"Départ initial : {_money(measured.get('starting_balance'))}")
    lines.append("")
    lines.append(
        _percent_line(
            "Perte du jour",
            measured.get("daily_loss_pct"),
            limits.get("max_daily_loss_pct"),
        )
    )
    lines.append(
        _percent_line(
            "Drawdown total",
            measured.get("total_drawdown_pct"),
            limits.get("max_total_drawdown_pct"),
        )
    )
    lines.append(
        _count_line(
            "Positions ouvertes",
            measured.get("open_positions"),
            limits.get("max_open_trades"),
        )
    )
    lines.append("")

    if decision.allowed:
        lines.append("✅ Trading autorisé")
        return "\n".join(lines)

    label = CODE_LABELS.get(decision.code, decision.code)
    lines.append(f"🛑 Trading bloqué — {label}")
    if decision.reason:
        lines.append(decision.reason)
    if decision.fields:
        lines.append(f"Champ(s) à corriger : {', '.join(decision.fields)}")
    return "\n".join(lines)


__all__ = ["CODE_LABELS", "MISSING", "render"]
