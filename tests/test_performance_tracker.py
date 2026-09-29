"""Le règlement des signaux ouverts : ce qui est transmis à l'apprentissage.

`workers/performance_tracker.py` est le chemin de règlement de **production** :
c'est lui qui écrit les post-mortems que la descente de gradient consomme, et
c'est donc lui qui décide si le trade en cours peut être reconnu dans l'historique.
Deux propriétés y sont verrouillées, parce qu'elles se perdent sans bruit :

1. l'apprentissage reçoit l'**identité** du signal réglé (`pending_signals.id`).
   Le payload `signal` ne la porte pas — il vient du moteur, pas de la base — donc
   sans cette pièce le post-mortem s'enregistrerait sous le repli `sig_0`, commun à
   tous les règlements sans identifiant, et le trade ne pourrait plus être tenu hors
   de son propre entraînement (`core/adaptive_learning.py`) ;
2. seuls les signaux vraiment réglés (TP ou SL touché) déclenchent l'apprentissage :
   un prix qui n'a touché ni l'un ni l'autre ne doit ni écrire, ni apprendre.

La doublure partagée du dossier porte `supabase` ; `get_last_price` est le seul
morceau du monde extérieur qu'il faut simuler, plus les deux écritures de bilan
(leçons, run card) qui sortent du sujet.
"""

import asyncio
import contextlib
import io
import unittest
from unittest import mock

from tests import supabase_double
from workers import performance_tracker


def _open_signal(sig_id: str, *, direction: str = "BUY", tp: float = 110.0, sl: float = 90.0) -> dict:
    """Une ligne `pending_signals` exécutée, telle que le balayage la relit."""
    return {
        "id": sig_id,
        "status": "executed",
        "signal": {
            "asset": "EURUSD",
            "direction": direction,
            "take_profit": tp,
            "stop_loss": sl,
            "confidence": 0.7,
        },
    }


class SettlementTest(unittest.IsolatedAsyncioTestCase):
    """Un balayage, sur une file de signaux exécutés."""

    def setUp(self):
        self.client = supabase_double.use_supabase(
            self, supabase_double.SupabaseDouble(), performance_tracker
        )

    def _sweep(self, price, *rows, learning=None):
        """Un passage du balayage à ce prix, et ce qui a été transmis à l'apprentissage.

        `learning` est ce que l'apprentissage rend : par défaut un règlement ordinaire,
        un autre dict pour éprouver ce que le tracker **dit** de ce qu'il a reçu.
        """
        self.client.store("pending_signals").rows = [dict(row) for row in rows]
        recorded: list = []

        def fake_record(**kwargs):
            recorded.append(kwargs)
            return dict(learning or {"post_mortem": {"lesson": "leçon de test"}})

        for patcher in (
            mock.patch.object(performance_tracker, "get_last_price", lambda asset: price),
            mock.patch.object(
                performance_tracker, "record_trade_settlement_and_learn", fake_record
            ),
            mock.patch.object(performance_tracker, "update_lessons", lambda: None),
            mock.patch.object(performance_tracker, "export_run_card", lambda: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        #: Le worker raconte son avancement à l'écran : on le retient pour les tests
        #: qui lisent ce qu'il dit, et on le tait de la sortie de la suite.
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            asyncio.run(performance_tracker.check_open_signals_performance())
        self.stdout = output.getvalue()
        return recorded

    def test_the_settlement_carries_the_identity_of_the_signal(self):
        """Sans `id`, le post-mortem s'enregistre sous `sig_0` et ne se reconnaît plus."""
        recorded = self._sweep(111.0, _open_signal("sig-42"))

        self.assertEqual(len(recorded), 1)
        data = recorded[0]["signal_data"]
        self.assertIn("id", data, "le post-mortem s'enregistrerait sous le repli sig_0")
        self.assertEqual(data["id"], "sig-42")

    def test_the_identity_is_added_without_losing_the_signal(self):
        """L'identité **s'ajoute** au payload : les sous-notes doivent survivre."""
        row = _open_signal("sig-43")
        row["signal"]["ta_score"] = 0.71
        recorded = self._sweep(111.0, row)

        data = recorded[0]["signal_data"]
        self.assertEqual(data["asset"], "EURUSD")
        self.assertEqual(data["direction"], "BUY")
        self.assertEqual(float(data["take_profit"]), 110.0)
        self.assertEqual(data["ta_score"], 0.71)

    def test_the_stored_payload_is_not_modified(self):
        """Le `signal` rangé en base est recopié, pas enrichi sur place."""
        signal = _open_signal("sig-44")
        self._sweep(111.0, signal)

        self.assertNotIn("id", signal["signal"])

    def test_a_signal_that_touches_its_target_is_won(self):
        recorded = self._sweep(111.0, _open_signal("sig-45"))

        self.assertEqual(recorded[0]["outcome"], "won")
        self.assertAlmostEqual(recorded[0]["exit_price"], 111.0)

    def test_a_signal_that_touches_its_stop_is_lost(self):
        recorded = self._sweep(89.0, _open_signal("sig-46"))

        self.assertEqual(recorded[0]["outcome"], "lost")

    def test_an_untouched_signal_is_not_settled(self):
        """Ni TP ni SL : rien n'est écrit, et rien n'est appris."""
        recorded = self._sweep(100.0, _open_signal("sig-47"))

        self.assertEqual(recorded, [])
        self.assertEqual(self.client.store("pending_signals").updated, [])

    def test_only_the_settled_signal_is_learned(self):
        """Deux signaux ouverts, un seul au but : l'apprentissage en voit un."""
        untouched = _open_signal("sig-49", tp=200.0, sl=50.0)
        recorded = self._sweep(111.0, _open_signal("sig-48"), untouched)

        self.assertEqual([call["signal_data"]["id"] for call in recorded], ["sig-48"])

    def test_a_replayed_settlement_is_not_announced_as_learned(self):
        """Un rattrapage ne doit pas se lire comme un nouvel apprentissage.

        Le règlement est idempotent par identité : quand il n'y a rien à apprendre,
        le module le dit (`status: already_settled`) et le journal du tracker ne
        peut pas annoncer « appliqué » — une action annoncée qui n'a pas eu lieu est
        exactement ce qu'on ne veut pas relire six mois plus tard.
        """
        learning = {"status": "already_settled", "post_mortem": {"lesson": "déjà apprise"}}
        recorded = self._sweep(111.0, _open_signal("sig-50"), learning=learning)

        self.assertEqual(len(recorded), 1)
        self.assertIn("Déjà appris (rejeu ignoré) : déjà apprise", self.stdout)
        self.assertNotIn("Apprentissage IA appliqué", self.stdout)


if __name__ == "__main__":
    unittest.main()
