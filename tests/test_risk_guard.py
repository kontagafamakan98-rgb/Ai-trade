"""Le garde-fou de risque — et la remise à zéro qui n'avait jamais été éprouvée.

`execution/risk_guard.py` décide, pour chaque ordre, si le compte a le droit de
trader : limite de perte journalière, plafond de drawdown total, nombre de
positions ouvertes. Aucun test ne l'exerçait — les tests d'exécution doublent
`risk_can_trade` — donc le geste qui remet un compte à zéro chaque matin, et le
seuil qui l'arrête, n'avaient jamais été vérifiés nulle part.

Deux propriétés sont tenues ici, et ce sont les seules qui coûtent :

* **le compteur journalier repart au changement de jour, et lui seul.** Un compte
  qui a perdu 6 % hier doit pouvoir trader aujourd'hui ; son drawdown **total**,
  lui, ne se réinitialise pas — un garde-fou qui remettrait les deux à zéro
  laisserait un compte en ruine retrader indéfiniment ;
* **le seuil bloque au seuil**, et la raison porte le chiffre : c'est ce chiffre
  que l'utilisateur lit quand son ordre est refusé.

L'horloge est **posée** (`_FrozenDate`) au lieu d'être lue : une remise à zéro ne
se prouve qu'en changeant de jour, et un test qui attendrait minuit ne serait pas
un test. Le `date` remplacé est celui **du module**, donc rien d'autre dans la
suite ne voit l'horloge bouger — même propriété que la vérification « trois
fuseaux » du dépôt.

La base est la doublure partagée (`tests/supabase_double.py`) : les écritures y
persistent et les lectures y sont filtrées comme PostgREST les filtre, donc la
remise à zéro se lit dans la ligne **réécrite**, et un comptage qui ignorerait le
`user_id` compterait les positions de quelqu'un d'autre.
"""
from __future__ import annotations

import contextlib
import datetime
import io
import json
import unittest
from unittest import mock

from database import preferences as prefs
from execution import risk_guard as rg
from tests.supabase_double import SupabaseDouble, use_supabase

TODAY = "2026-03-14"
YESTERDAY = "2026-03-13"
USER = "u1"


class _FrozenDate:
    """Le `date` du module : `today()` répond le jour posé, jamais celui du poste.

    Un substitut, et non un `patch` de `datetime.date` : `risk_guard` n'utilise que
    `today()`, et le remplacer ici ne déplace l'horloge que pour ce module. Un
    attribut du module qu'on ajouterait un jour (`date.fromisoformat`, par exemple)
    ferait échouer ces tests bruyamment plutôt que de lire la vraie horloge en
    silence.
    """

    def __init__(self, day: str):
        self.day = day

    def today(self) -> datetime.date:
        return datetime.date.fromisoformat(self.day)


class GuardTestBase(unittest.TestCase):
    """Base commune : la base doublée, l'horloge posée, des semis lisibles."""

    def setUp(self) -> None:
        self.client = SupabaseDouble()
        use_supabase(self, self.client, rg, prefs)
        self.clock = _FrozenDate(TODAY)
        patcher = mock.patch.object(rg, "date", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    # -- semis ------------------------------------------------------------- #

    def _preferences(self, **thresholds) -> None:
        """Une ligne de préférences, avec les seuils du test (défauts du projet sinon)."""
        row = {"user_id": USER}
        row.update(thresholds)
        self.client.store("user_preferences").rows = [row]

    def _state(self, **fields) -> None:
        """L'état de risque : c'est la ligne que la remise à zéro doit réécrire."""
        row = {
            "user_id": USER,
            "starting_balance": 10000.0,
            "daily_start_balance": 10000.0,
            "daily_date": TODAY,
        }
        row.update(fields)
        self.client.store("user_risk_state").rows = [row]

    def _signals(self, user: str, how_many: int, status: str = "executed") -> None:
        """Des signaux en base — seuls les `executed` d'un même utilisateur comptent."""
        self.client.store("pending_signals").rows.extend(
            {"id": f"{user}-{index}", "user_id": user, "status": status}
            for index in range(how_many)
        )

    # -- lectures ---------------------------------------------------------- #

    def _trade(self, balance: float):
        return rg.can_trade(USER, balance)

    def _stored_state(self) -> dict:
        rows = self.client.store("user_risk_state").rows
        self.assertEqual(len(rows), 1, "une seule ligne d'état par utilisateur")
        return rows[0]


class DailyLossTest(GuardTestBase):
    """Le seuil de perte journalière : il bloque **au** seuil, et le dit."""

    def test_the_threshold_blocks(self):
        self._preferences(max_daily_loss_pct=5.0)
        self._state()

        allowed, reason = self._trade(9500.0)

        self.assertFalse(allowed)
        self.assertIn("journalière", reason)
        self.assertIn("5.00%", reason, "la raison porte le chiffre que l'utilisateur lit")

    def test_just_below_the_threshold_still_trades(self):
        self._preferences(max_daily_loss_pct=5.0)
        self._state()

        self.assertEqual(self._trade(9500.01), (True, ""))

    def test_a_gain_is_not_a_loss(self):
        self._preferences()
        self._state()

        self.assertEqual(self._trade(12000.0), (True, ""))

    def test_the_project_defaults_apply_without_preferences(self):
        """Rien de configuré : 5 % de perte journalière reste le seuil."""
        self._state()

        allowed, reason = self._trade(9400.0)

        self.assertFalse(allowed)
        self.assertIn("5.0", reason)


class DailyResetTest(GuardTestBase):
    """Le changement de jour : le compteur journalier repart, le total non."""

    def test_a_new_day_resets_the_daily_counter(self):
        """Le cas vécu : 5 % perdus **hier**, le compte doit retrader ce matin."""
        self._preferences(max_daily_loss_pct=5.0)
        self._state(daily_date=YESTERDAY, daily_start_balance=10000.0)

        allowed, reason = self._trade(9500.0)

        self.assertEqual((allowed, reason), (True, ""), "le nouveau jour repart de zéro")
        state = self._stored_state()
        self.assertEqual(state["daily_date"], TODAY)
        self.assertEqual(
            state["daily_start_balance"],
            9500.0,
            "le compteur repart du solde **courant**, pas de celui d'hier",
        )

    def test_the_same_day_resets_nothing(self):
        """La propriété voisine, aussi nécessaire : le compteur ne se remet pas à chaque ordre."""
        self._preferences(max_daily_loss_pct=5.0)
        self._state(daily_date=TODAY, daily_start_balance=10000.0)

        allowed, _ = self._trade(9500.0)

        self.assertFalse(allowed)
        self.assertEqual(self.client.operations("update"), [], "aucune écriture le même jour")
        self.assertEqual(self._stored_state()["daily_start_balance"], 10000.0)

    def test_the_reset_writes_only_the_daily_pair(self):
        """La ligne d'état garde son point de départ : c'est lui qui porte le drawdown."""
        self._preferences()
        self._state(daily_date=YESTERDAY, starting_balance=10000.0)

        self._trade(9800.0)

        self.assertEqual(
            self.client.updates(),
            [{"daily_start_balance": 9800.0, "daily_date": TODAY}],
        )

    def test_a_new_day_does_not_reset_the_total_drawdown(self):
        """−12 % depuis toujours : le jour neuf ne doit pas rouvrir le compte."""
        self._preferences(max_daily_loss_pct=20.0, max_total_drawdown_pct=10.0)
        self._state(daily_date=YESTERDAY, starting_balance=10000.0)

        allowed, reason = self._trade(8800.0)

        self.assertFalse(allowed)
        self.assertIn("drawdown", reason)
        self.assertEqual(self._stored_state()["starting_balance"], 10000.0)
        self.assertEqual(len(self.client.updates()), 1, "le compteur du jour a bien été remis")

    def test_only_the_stored_date_decides_the_reset(self):
        """A/B : l'horloge ne bouge pas, seule la date en base change de camp."""
        self._preferences(max_daily_loss_pct=5.0)
        self._state(daily_date=YESTERDAY, daily_start_balance=10000.0)
        yesterday_verdict = self._trade(9500.0)

        self._state(daily_date=TODAY, daily_start_balance=10000.0)
        today_verdict = self._trade(9500.0)

        self.assertTrue(yesterday_verdict[0], "la date stockée est périmée : remise à zéro")
        self.assertFalse(today_verdict[0], "même jour : le compteur tient")

    def test_the_reset_follows_the_modules_clock_not_the_postes(self):
        """Le jour qui compte est celui du module, jamais celui du poste."""
        self.clock.day = "2026-12-25"
        self._preferences()
        self._state(daily_date=TODAY)

        self._trade(10000.0)

        self.assertEqual(self._stored_state()["daily_date"], "2026-12-25")


class TotalDrawdownTest(GuardTestBase):
    """Le plafond total, mesuré depuis le **premier** solde connu."""

    def test_the_total_drawdown_blocks(self):
        self._preferences(max_daily_loss_pct=20.0, max_total_drawdown_pct=10.0)
        self._state()

        allowed, reason = self._trade(9000.0)

        self.assertFalse(allowed)
        self.assertIn("drawdown", reason)
        self.assertIn("10.00%", reason)

    def test_the_drawdown_is_measured_from_the_starting_balance(self):
        """Le point de départ du jour n'y change rien : seul le premier compte."""
        self._preferences(max_daily_loss_pct=50.0, max_total_drawdown_pct=10.0)
        self._state(starting_balance=10000.0, daily_start_balance=5000.0)

        allowed, reason = self._trade(8900.0)

        self.assertFalse(allowed)
        self.assertIn("drawdown", reason)


class OpenPositionsTest(GuardTestBase):
    """Le plafond de positions : il compte les `executed` **de cet utilisateur**."""

    def test_the_open_positions_cap_blocks(self):
        self._preferences(max_open_trades=3)
        self._state()
        self._signals(USER, 3)

        allowed, reason = self._trade(10000.0)

        self.assertFalse(allowed)
        self.assertIn("positions", reason)
        self.assertIn("3", reason, "le plafond atteint est nommé")

    def test_the_cap_counts_only_this_users_executed_rows(self):
        """Trois signaux d'un autre, et deux verdicts qui ne sont pas des positions."""
        self._preferences(max_open_trades=3)
        self._state()
        self._signals(USER, 3, status="pending")
        self._signals(USER, 3, status="rejected")
        self._signals("u2", 3)

        self.assertEqual(self._trade(10000.0), (True, ""))

    def test_the_positions_are_checked_before_the_losses(self):
        """Trois seuils dépassés : la raison nomme le premier contrôle, pas le plus grave."""
        self._preferences(max_open_trades=1, max_daily_loss_pct=1.0, max_total_drawdown_pct=1.0)
        self._state()
        self._signals(USER, 1)

        allowed, reason = self._trade(9000.0)

        self.assertFalse(allowed)
        self.assertIn("positions", reason)
        self.assertNotIn("journalière", reason)


class InitialisationTest(GuardTestBase):
    """Le premier appel d'un utilisateur, et les points de départ manquants."""

    def test_the_first_call_writes_the_state(self):
        self._preferences()

        allowed, reason = self._trade(10000.0)

        self.assertEqual((allowed, reason), (True, ""))
        self.assertEqual(
            self.client.store("user_risk_state").inserted,
            [
                {
                    "user_id": USER,
                    "starting_balance": 10000.0,
                    "daily_start_balance": 10000.0,
                    "daily_date": TODAY,
                }
            ],
        )

    def test_a_missing_daily_balance_falls_back_to_the_current_one(self):
        """Une ligne incomplète ne doit pas bloquer : sans départ, aucune perte n'est mesurable."""
        self._preferences(max_total_drawdown_pct=50.0)
        self._state(daily_start_balance=None)

        self.assertEqual(self._trade(9000.0), (True, ""))

class UnmeasurableReferenceTest(GuardTestBase):
    """Zéro n'est pas « pas de limite » : c'est un pourcentage qu'on ne peut pas calculer.

    Le comportement tranché ici remplace un silence : `if daily_start_balance > 0`
    sautait les deux gardes quand le point de départ valait zéro, donc un compte sans
    départ (solde jamais lu, ligne écrite à la main) tradait sans plafond de perte ni
    de drawdown — et rien ne le disait. Refuser est la seule réponse qui ne fabrique
    ni un feu vert, ni un chiffre faux : « 5 % de 0 » n'existe pas.

    Le refus est **réparable** et le dit : corriger la référence, ou supprimer la
    ligne pour qu'elle reparte du prochain solde lu.
    """

    def test_a_zero_reference_is_refused_and_named(self):
        self._preferences(max_daily_loss_pct=1.0, max_total_drawdown_pct=1.0)
        self._state(starting_balance=0.0, daily_start_balance=0.0)

        allowed, reason = self._trade(5000.0)

        self.assertFalse(allowed)
        self.assertIn("inutilisable", reason)
        self.assertIn("starting_balance", reason)
        self.assertIn("daily_start_balance", reason)
        self.assertNotIn(
            "journalière atteinte",
            reason,
            "un plafond qu'on ne peut pas mesurer n'est pas un plafond atteint",
        )

    def test_only_the_broken_reference_is_named(self):
        self._preferences()
        self._state(starting_balance=0.0)

        allowed, reason = self._trade(5000.0)

        self.assertFalse(allowed)
        self.assertIn("starting_balance", reason)
        self.assertNotIn("daily_start_balance", reason)

    def test_a_negative_reference_is_refused_too(self):
        """Un solde négatif (appel de marge) n'est pas davantage un point de départ."""
        self._preferences()
        self._state(daily_start_balance=-12.0)

        self.assertFalse(self._trade(5000.0)[0])

    def test_a_zero_balance_is_refused_before_any_state_is_written(self):
        """Le défaut se créait à l'initialisation : un solde nul y écrivait deux zéros."""
        self._preferences()

        allowed, reason = self._trade(0.0)

        self.assertFalse(allowed)
        self.assertEqual(reason, rg.NO_BALANCE_REASON)
        self.assertEqual(
            self.client.store("user_risk_state").inserted,
            [],
            "une ligne née avec un solde nul porterait deux gardes muets pour toujours",
        )

    def test_a_zero_balance_does_not_forgive_a_healthy_reference(self):
        """Avec une référence valide, le solde nul reste un refus — pas un 0 % de perte."""
        self._preferences(max_daily_loss_pct=5.0)
        self._state()

        allowed, reason = self._trade(0.0)

        self.assertFalse(allowed)
        self.assertEqual(reason, rg.NO_BALANCE_REASON)

    def test_a_missing_reference_is_derived_not_refused(self):
        """Clé jamais écrite : c'est l'initialisation, et c'est le seul cas dérivé."""
        self._preferences(max_total_drawdown_pct=50.0, max_daily_loss_pct=5.0)
        self._state(starting_balance=None, daily_start_balance=None)

        self.assertEqual(self._trade(9500.0), (True, ""))

    def test_a_healthy_state_and_balance_still_trade(self):
        """Le contrôle ajouté ne doit pas refuser un compte mesurable."""
        self._preferences(max_daily_loss_pct=5.0, max_total_drawdown_pct=10.0)
        self._state()

        self.assertEqual(self._trade(9800.0), (True, ""))


class FailClosedTest(GuardTestBase):
    """Un doute bloque — et le motif dit que c'est un doute, pas un seuil atteint."""

    def _refused(self, balance: float = 10000.0):
        """Le verdict et ce que le journal a dit : les deux comptent, donc les deux sortent."""
        noise = io.StringIO()
        with contextlib.redirect_stdout(noise):
            verdict = self._trade(balance)
        return verdict, noise.getvalue()

    def test_an_unreadable_state_blocks_the_trade(self):
        self._preferences()
        self.client.fail("user_risk_state", "select", RuntimeError("db down"))

        (allowed, reason), journal = self._refused()

        self.assertFalse(allowed)
        self.assertIn("précaution", reason)
        self.assertNotIn("journalière", reason, "la panne ne se déguise pas en seuil")
        self.assertNotIn("drawdown", reason)
        self.assertIn("RuntimeError: db down", journal, "le journal nomme la panne")

    def test_an_unreadable_preference_blocks_too(self):
        self.client.fail("user_preferences", "select", RuntimeError("db down"))

        (allowed, reason), journal = self._refused()

        self.assertFalse(allowed)
        self.assertIn("précaution", reason)
        self.assertIn("RuntimeError: db down", journal)

    def test_a_refused_position_count_blocks_too(self):
        """Le comptage est *dans* le try : son échec ferme aussi la porte."""
        self._preferences()
        self._state()
        self.client.fail("pending_signals", "select", RuntimeError("db down"))

        (allowed, reason), journal = self._refused()

        self.assertFalse(allowed)
        self.assertIn("précaution", reason)
        self.assertIn("RuntimeError: db down", journal)


class EvaluateTest(GuardTestBase):
    """Le verdict structuré : la phrase **et** les chiffres, dans le même calcul.

    Le refus ne doit plus être une phrase à lire : il porte le code du contrôle, les
    seuils en jeu, les valeurs mesurées, et les champs à corriger. C'est ce qu'une
    application affiche, et ce que `can_trade` continue de résumer en un couple.
    """

    def test_a_daily_loss_refusal_carries_its_measured_value_and_threshold(self):
        self._preferences(max_daily_loss_pct=5.0)
        self._state()

        decision = rg.evaluate(USER, 9500.0)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.code, rg.CODE_DAILY_LOSS)
        self.assertEqual(decision.limits["max_daily_loss_pct"], 5.0)
        self.assertAlmostEqual(decision.measured["daily_loss_pct"], 5.0)
        self.assertEqual(decision.measured["daily_start_balance"], 10000.0)
        self.assertIn("5.00%", decision.reason, "la phrase est inchangée")

    def test_a_drawdown_refusal_carries_its_measured_value(self):
        self._preferences(max_daily_loss_pct=20.0, max_total_drawdown_pct=10.0)
        self._state()

        decision = rg.evaluate(USER, 9000.0)

        self.assertEqual(decision.code, rg.CODE_TOTAL_DRAWDOWN)
        self.assertAlmostEqual(decision.measured["total_drawdown_pct"], 10.0)
        self.assertEqual(decision.limits["max_total_drawdown_pct"], 10.0)

    def test_an_unusable_reference_names_the_faulty_field(self):
        self._preferences()
        self._state(starting_balance=0.0)

        decision = rg.evaluate(USER, 5000.0)

        self.assertEqual(decision.code, rg.CODE_REFERENCE_UNUSABLE)
        self.assertEqual(decision.fields, (rg.STARTING_FIELD,))
        self.assertEqual(decision.measured["starting_balance"], 0.0)

    def test_an_unreadable_balance_names_the_balance_field(self):
        self._preferences()

        decision = rg.evaluate(USER, 0.0)

        self.assertEqual(decision.code, rg.CODE_BALANCE_UNREADABLE)
        self.assertEqual(decision.fields, ("balance",))

    def test_the_positions_cap_carries_the_count_and_the_limit(self):
        self._preferences(max_open_trades=3)
        self._state()
        self._signals(USER, 3)

        decision = rg.evaluate(USER, 10000.0)

        self.assertEqual(decision.code, rg.CODE_MAX_OPEN_TRADES)
        self.assertEqual(decision.measured["open_positions"], 3)
        self.assertEqual(decision.limits["max_open_trades"], 3)

    def test_an_allowed_verdict_still_carries_where_the_account_stands(self):
        self._preferences(max_daily_loss_pct=5.0, max_total_drawdown_pct=10.0)
        self._state()

        decision = rg.evaluate(USER, 9800.0)

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.code, rg.CODE_ALLOWED)
        self.assertEqual(decision.reason, "")
        self.assertAlmostEqual(decision.measured["daily_loss_pct"], 2.0)
        self.assertAlmostEqual(decision.measured["total_drawdown_pct"], 2.0)

    def test_a_failure_is_flagged_as_such_with_no_threshold_claimed(self):
        self._preferences()
        self.client.fail("user_risk_state", "select", RuntimeError("db down"))

        with contextlib.redirect_stdout(io.StringIO()):
            decision = rg.evaluate(USER, 10000.0)

        self.assertEqual(decision.code, rg.CODE_ERROR)
        self.assertIn("précaution", decision.reason)

    def test_can_trade_returns_exactly_the_structured_verdict(self):
        self._preferences(max_daily_loss_pct=5.0)
        self._state()

        decision = rg.evaluate(USER, 9500.0)

        self.assertEqual(rg.can_trade(USER, 9500.0), (decision.allowed, decision.reason))

    def test_as_dict_is_json_serializable_and_keeps_every_field(self):
        self._preferences()
        self._state()

        payload = rg.evaluate(USER, 9800.0).as_dict()

        self.assertEqual(payload["code"], rg.CODE_ALLOWED)
        self.assertEqual(
            set(payload), {"allowed", "code", "reason", "limits", "measured", "fields"}
        )
        json.dumps(payload)  # lève si une valeur n'est pas sérialisable


class ReferenceUnusableVisibilityTest(GuardTestBase):
    """Le refus pour référence inutilisable laisse une trace **pour l'exploitant**.

    L'utilisateur bloqué lit sa raison ; personne d'autre ne la voit. Le compteur
    dédié et le journal à préfixe unique sont ce qui permet de remarquer qu'une
    ligne `user_risk_state` cassée bloque des comptes — sans quoi elle bloque en
    silence, et c'est exactement le défaut qu'on corrige ici.
    """

    def setUp(self) -> None:
        super().setUp()
        rg.reset_reference_unusable()
        self.addCleanup(rg.reset_reference_unusable)

    def _broken(self, **fields) -> None:
        """Un état dont au moins une référence est inutilisable."""
        self._preferences()
        self._state(**fields)

    def test_the_refusal_is_counted_and_the_faulty_field_is_named(self):
        self._broken(starting_balance=0.0)

        with self.assertLogs("execution.risk_guard", level="WARNING"):
            decision = rg.evaluate(USER, 5000.0)

        self.assertEqual(decision.code, rg.CODE_REFERENCE_UNUSABLE)
        snapshot = rg.reference_unusable_snapshot()
        self.assertEqual(snapshot["total"], 1)
        self.assertEqual(snapshot["users"][USER]["fields"], [rg.STARTING_FIELD])
        self.assertEqual(snapshot["users"][USER]["count"], 1)

    def test_the_journal_line_carries_a_dedicated_prefix_and_the_field(self):
        """Une seule chaîne à chercher dans les journaux de production."""
        self._broken(starting_balance=0.0)

        with self.assertLogs("execution.risk_guard", level="WARNING") as logs:
            rg.evaluate(USER, 5000.0)

        line = logs.output[0]
        self.assertIn(rg.REFERENCE_UNUSABLE_LOG, line)
        self.assertIn(USER, line)
        self.assertIn(rg.STARTING_FIELD, line)

    def test_a_repeated_refusal_counts_without_warning_again(self):
        """Un compte bloqué qui retente ne doit pas remplir le journal à lui seul."""
        self._broken(starting_balance=0.0)

        with self.assertLogs("execution.risk_guard", level="WARNING"):
            rg.evaluate(USER, 5000.0)
        with self.assertNoLogs("execution.risk_guard", level="WARNING"):
            rg.evaluate(USER, 5000.0)

        snapshot = rg.reference_unusable_snapshot()
        self.assertEqual(snapshot["total"], 2)
        self.assertEqual(snapshot["users"][USER]["count"], 2)

    def test_a_changed_field_set_warns_again(self):
        """Une ligne qui casse ailleurs n'est pas la répétition de la même panne."""
        self._broken(starting_balance=0.0)
        with self.assertLogs("execution.risk_guard", level="WARNING"):
            rg.evaluate(USER, 5000.0)

        self._state(daily_start_balance=-1.0)

        with self.assertLogs("execution.risk_guard", level="WARNING") as logs:
            rg.evaluate(USER, 5000.0)
        self.assertIn(rg.DAILY_FIELD, logs.output[0])

    def test_two_blocked_users_are_tracked_separately(self):
        self._broken(starting_balance=0.0)
        self.client.store("user_risk_state").rows.append(
            {
                "user_id": "u2",
                "starting_balance": 0.0,
                "daily_start_balance": 0.0,
                "daily_date": TODAY,
            }
        )

        with self.assertLogs("execution.risk_guard", level="WARNING"):
            rg.evaluate(USER, 5000.0)
        with self.assertLogs("execution.risk_guard", level="WARNING"):
            rg.evaluate("u2", 5000.0)

        snapshot = rg.reference_unusable_snapshot()
        self.assertEqual(snapshot["total"], 2)
        self.assertEqual(set(snapshot["users"]), {USER, "u2"})

    def test_another_refusal_is_not_counted_here(self):
        """Le compteur est dédié à ce refus : un solde illisible n'y entre pas."""
        self._preferences()

        decision = rg.evaluate(USER, 0.0)

        self.assertEqual(decision.code, rg.CODE_BALANCE_UNREADABLE)
        self.assertEqual(rg.reference_unusable_snapshot(), {"total": 0, "users": {}})

    def test_an_allowed_verdict_is_not_counted(self):
        self._preferences(max_daily_loss_pct=5.0, max_total_drawdown_pct=10.0)
        self._state()

        self.assertTrue(rg.evaluate(USER, 9800.0).allowed)
        self.assertEqual(rg.reference_unusable_snapshot()["total"], 0)

    def test_the_snapshot_is_a_copy_not_a_handle(self):
        self._broken(starting_balance=0.0)
        with self.assertLogs("execution.risk_guard", level="WARNING"):
            rg.evaluate(USER, 5000.0)

        snapshot = rg.reference_unusable_snapshot()
        snapshot["total"] = 999
        snapshot["users"][USER]["fields"] = ["x"]

        fresh = rg.reference_unusable_snapshot()
        self.assertEqual(fresh["total"], 1)
        self.assertEqual(fresh["users"][USER]["fields"], [rg.STARTING_FIELD])

    def test_reset_clears_the_counter(self):
        self._broken(starting_balance=0.0)
        with self.assertLogs("execution.risk_guard", level="WARNING"):
            rg.evaluate(USER, 5000.0)

        rg.reset_reference_unusable()

        self.assertEqual(rg.reference_unusable_snapshot(), {"total": 0, "users": {}})


if __name__ == "__main__":
    unittest.main()
