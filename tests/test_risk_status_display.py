"""L'état du risque affiché à l'utilisateur : aucune note ne doit mentir.

`/risk` interroge le compte Alpaca et, si l'interrogation échoue, **affiche** quand
même un solde (l'equity papier). Le repli est légitime — on ne décide pas d'un ordre
à partir de cet affichage — mais la note doit dire une chose vraie. Le repli générique
disait « pas de compte réel », ce qui envoyait chercher un compte là où le défaut est
ailleurs (réseau coupé, réponse illisible). Les deux pannes *connues* sont déjà nommées
par leurs propres branches (`BrokerCredentialsUnreadable`, `AlpacaSDKUnavailable`) ;
la branche générique doit faire de même, avec le type de l'exception.

`main.py` n'est pas importable ici (`feedparser` absent) : comme les autres contrôles
de ce dépôt, on lit sa **source** — c'est le message affiché qui est le contrat.
"""
from __future__ import annotations

import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = REPO_ROOT / "main.py"


class UnknownBalanceCauseTest(unittest.TestCase):
    """Le repli qui ne connaît pas la cause ne doit pas en inventer une."""

    def _source(self) -> str:
        return MAIN.read_text(encoding="utf-8")

    def test_the_generic_fallback_names_the_cause_and_claims_nothing(self):
        source = self._source()
        # La note d'origine était un diagnostic inventé : elle envoyait chercher un
        # compte là où le défaut est un réseau coupé ou une réponse illisible.
        self.assertNotIn('"(equity paper configurée, pas de compte réel)"', source)
        self.assertIn("compte non interrogé", source)
        self.assertIn("type(exc).__name__", source)

    def test_the_two_known_failures_still_name_their_own_cause(self):
        source = self._source()
        self.assertIn("BrokerCredentialsUnreadable", source)
        self.assertIn("AlpacaSDKUnavailable", source)

    def test_the_display_reads_the_structured_verdict(self):
        """`/risk` n'affiche pas un couple (autorisé, phrase) : il rend le verdict.

        Le garde pose les chiffres une fois ; l'affichage les lit (`measured`,
        `limits`, `fields`) au lieu de les recomposer — deux calculs finiraient par
        diverger, et l'utilisateur lirait un chiffre que le garde n'a pas utilisé.
        """
        source = self._source()
        self.assertIn("risk_evaluate(user_id, balance)", source)
        self.assertIn("risk_card.render(decision", source)


if __name__ == "__main__":
    unittest.main()
