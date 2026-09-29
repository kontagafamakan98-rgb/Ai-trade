"""Le retour d'un règlement de trade (`api/learning_router.py`).

Ce que ce fichier tient, et qu'une réponse heureuse ne montre pas :

* un **rejeu** est un succès — le trade est réglé — mais la réponse ne peut pas
  annoncer un apprentissage qui n'a pas eu lieu : `record_trade_settlement_and_learn`
  est idempotent par identité, et c'est son `status` que le message suit ;
* une **issue inconnue** est refusée (400) avant tout appel : régler un trade sur
  autre chose que `won` / `lost` écrirait un post-mortem que rien ne saurait relire ;
* l'**identité** transmise à l'apprentissage est celle de la requête (`signal_id`),
  et l'actif part en majuscules : c'est cette identité qui empêche un second
  règlement du même signal d'écrire une ligne de plus.

Les endpoints sont appelés **directement** (ce sont des fonctions), sans passer par
HTTP : l'authentification et l'enregistrement des routes sont tenus ailleurs, dans
`tests/test_api_auth_integration.py`.

FastAPI est une dépendance de l'application, pas de ce module : dans un
environnement de développement minimal où il manque, les tests sont **sautés**
plutôt que de faire échouer la collecte — même règle que `tests/test_media_router.py`.
"""

from __future__ import annotations

import unittest
from unittest import mock

try:
    from fastapi import HTTPException

    from api import learning_router

    _FASTAPI_AVAILABLE = True
except Exception as exc:  # pragma: no cover - dépend de l'environnement
    _FASTAPI_AVAILABLE = False
    _IMPORT_ERROR = exc


def _feedback(outcome: str = "won", signal_id: str = "s-1", asset: str = "eurusd") -> dict:
    """Un appel de `/learning/feedback`, tel que le routeur le reçoit."""
    return {
        "signal_id": signal_id,
        "asset": asset,
        "direction": "BUY",
        "outcome": outcome,
        "entry_price": 1.0,
        "exit_price": 1.01,
        "confidence": 0.7,
    }


@unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI non installé")
class FeedbackTest(unittest.TestCase):
    """Ce que la réponse dit du règlement, rejoué ou non."""

    def _call(self, result):
        with mock.patch.object(
            learning_router, "record_trade_settlement_and_learn", return_value=result
        ) as settle:
            response = learning_router.submit_trade_feedback(**_feedback())
        return response, settle

    def test_a_fresh_settlement_is_announced_as_learned(self):
        response, _ = self._call({"status": "learned"})

        self.assertEqual(response["ok"], True)
        self.assertIn("Apprentissage IA enregistré", response["message"])
        self.assertNotIn("rejeu", response["message"])

    def test_a_replayed_settlement_says_it_was_a_replay(self):
        """Sinon l'appelant croit avoir ajouté un savoir qui était déjà là."""
        response, _ = self._call(
            {"status": "already_settled", "duplicate_of": {"signal_id": "s-1"}}
        )

        self.assertEqual(response["ok"], True)
        self.assertIn("déjà enregistré", response["message"])
        self.assertIn("rejeu ignoré", response["message"])

    def test_the_learning_result_is_returned_whole(self):
        """La route ne résume pas : l'appelant voit ce que le module a décidé."""
        result = {"status": "learned", "post_mortem": {"lesson": "leçon"}, "subscores": {}}
        response, _ = self._call(result)

        self.assertIs(response["learning_result"], result)

    def test_the_settlement_receives_the_identity_and_the_upper_cased_asset(self):
        _, settle = self._call({"status": "learned"})

        payload = settle.call_args.kwargs["signal_data"]
        self.assertEqual(payload["id"], "s-1")
        self.assertEqual(payload["asset"], "EURUSD")

    def test_an_unknown_outcome_is_refused_before_any_settlement(self):
        with mock.patch.object(learning_router, "record_trade_settlement_and_learn") as settle:
            with self.assertRaises(HTTPException) as raised:
                learning_router.submit_trade_feedback(**_feedback(outcome="breakeven"))

        self.assertEqual(raised.exception.status_code, 400)
        self.assertFalse(settle.called, "un règlement refusé ne doit rien écrire")


if __name__ == "__main__":
    unittest.main()
