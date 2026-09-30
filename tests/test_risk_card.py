"""La carte de `/risk` : elle lit le verdict, elle ne le recompose pas.

Le garde-fou rend un `RiskDecision` — une phrase, un code, des seuils, des valeurs
mesurées, des champs fautifs. Cette carte est ce que l'utilisateur lit : elle doit
montrer **où en est le compte** (solde, départ du jour, départ initial, perte du
jour et son seuil, drawdown et son seuil, positions), et, en cas de refus, le motif
et le champ à corriger.

Deux propriétés valent d'être verrouillées :

* **une valeur non mesurable s'affiche `—`, jamais `0`** — zéro est un chiffre, et
  un chiffre faux : un solde illisible n'a pas une perte de 0 % ;
* **un code inconnu est affiché tel quel** plutôt que masqué — un verdict qu'on ne
  sait pas nommer reste un verdict.
"""
from __future__ import annotations

import unittest

from execution.risk_guard import (
    CODE_ALLOWED,
    CODE_BALANCE_UNREADABLE,
    CODE_REFERENCE_UNUSABLE,
    RiskDecision,
)
from notifications import risk_card


def _decision(**fields) -> RiskDecision:
    base = {"allowed": True, "code": CODE_ALLOWED, "reason": ""}
    base.update(fields)
    return RiskDecision(**base)


HEALTHY = {
    "balance": 9800.0,
    "daily_start_balance": 10000.0,
    "starting_balance": 10000.0,
    "daily_loss_pct": 2.0,
    "total_drawdown_pct": 2.0,
    "open_positions": 1,
}
LIMITS = {"max_daily_loss_pct": 5.0, "max_total_drawdown_pct": 10.0, "max_open_trades": 3}


class RenderTest(unittest.TestCase):
    """Ce que la carte affiche, et ce qu'elle refuse d'inventer."""

    def test_an_allowed_card_shows_the_account_and_the_thresholds(self):
        text = risk_card.render(_decision(limits=LIMITS, measured=HEALTHY), "(solde réel Alpaca)")

        self.assertIn("📊 État du risque (solde réel Alpaca)", text)
        self.assertIn("Solde actuel : 9 800.00", text)
        self.assertIn("Départ du jour : 10 000.00", text)
        self.assertIn("Perte du jour : 2.00 % (limite 5.00 %)", text)
        self.assertIn("Drawdown total : 2.00 % (limite 10.00 %)", text)
        self.assertIn("Positions ouvertes : 1 / 3", text)
        self.assertIn("✅ Trading autorisé", text)

    def test_a_missing_value_is_a_dash_not_a_zero(self):
        decision = _decision(
            allowed=False,
            code=CODE_BALANCE_UNREADABLE,
            reason="Solde de compte indisponible ou nul.",
            limits={"max_daily_loss_pct": 5.0},
            measured={
                "balance": 0.0,
                "daily_start_balance": None,
                "starting_balance": None,
                "daily_loss_pct": None,
                "total_drawdown_pct": None,
                "open_positions": None,
            },
            fields=("balance",),
        )

        text = risk_card.render(decision)

        self.assertIn("Départ du jour : —", text)
        self.assertIn("Départ initial : —", text)
        self.assertIn("Drawdown total : —", text)
        self.assertNotIn("Départ du jour : 0", text, "l'absence n'est pas un zéro")

    def test_a_refusal_shows_the_label_the_reason_and_the_field(self):
        decision = _decision(
            allowed=False,
            code=CODE_REFERENCE_UNUSABLE,
            reason="Référence de solde inutilisable (starting_balance ≤ 0) : …",
            fields=("starting_balance",),
        )

        text = risk_card.render(decision)

        self.assertIn("🛑 Trading bloqué — référence de solde inutilisable", text)
        self.assertIn("Référence de solde inutilisable (starting_balance ≤ 0)", text)
        self.assertIn("Champ(s) à corriger : starting_balance", text)

    def test_an_unknown_code_is_shown_rather_than_hidden(self):
        text = risk_card.render(_decision(allowed=False, code="something-new", reason="x"))
        self.assertIn("🛑 Trading bloqué — something-new", text)


if __name__ == "__main__":
    unittest.main()
