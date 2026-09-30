"""Réparer un état de risque sans effacer une perte : ce qui est proposé, et pourquoi.

Le garde-fou refuse de trader quand une référence de solde est nulle ou négative —
c'est correct, « 5 % de 0 » n'est pas un plafond. Mais une ligne refusée est
**réparable**, et la manière de la réparer décide de tout : reprendre le **solde
courant** remettrait le drawdown à zéro, donc un compte en ruine retraderait. C'est
l'effacement silencieux que ce module interdit, et c'est ce que ces tests verrouillent
— sur les deux formes du défaut :

* une référence **présente mais non positive** (0, négative), que le garde refuse ;
* une référence **absente (`NULL`)**, que le garde **dérive du solde courant** — le
  piège, puisqu'il ne refuse pas et que la perte disparaît sans un mot.

Trois propriétés, chacune un lot de cas :

* **la reprise est le capital configuré**, jamais le solde courant — c'est la valeur
  que l'initialisation aurait écrite ;
* **rien n'est inventé** : sans capital connu, la ligne reste `needs-value` et
  l'opérateur est le seul à pouvoir fournir la valeur (`--set`) ;
* **on ne supprime aucune ligne**, et on n'écrit aucune valeur non positive.

La base est la doublure partagée (`tests/supabase_double.py`) : les écritures y
persistent et les lectures y sont filtrées, donc un `update` qui n'a pas eu lieu ne
peut pas passer pour une réussite.
"""
from __future__ import annotations

import importlib.util
import io
import pathlib
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from database import risk_state_repair as repair
from tests.supabase_double import SupabaseDouble

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TODAY = "2026-09-30"
YESTERDAY = "2026-09-29"


def _load_cli():
    """Le script, importé par son chemin — comme les autres tests de script."""
    spec = importlib.util.spec_from_file_location(
        "repair_risk_state", REPO_ROOT / "scripts" / "repair_risk_state.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


class DiagnoseTest(unittest.TestCase):
    """Le verdict d'une ligne : quoi corriger, et vers quelle valeur."""

    def test_a_missing_starting_reference_is_restored_from_the_capital(self):
        diagnosis = repair.diagnose(
            {
                "user_id": "u1",
                "starting_balance": None,
                "daily_start_balance": 900.0,
                "daily_date": TODAY,
            },
            configured_capital=100000.0,
            today=TODAY,
        )
        self.assertEqual(diagnosis.status, repair.STATE_REPAIRABLE)
        self.assertEqual(diagnosis.proposal, {repair.STARTING_BALANCE: 100000.0})

    def test_a_zero_reference_is_restored_to_the_capital_not_the_balance(self):
        # Le piège : reprendre le solde courant remettrait la perte à zéro.
        diagnosis = repair.diagnose(
            {
                "user_id": "u1",
                "starting_balance": 0,
                "daily_start_balance": 900.0,
                "daily_date": TODAY,
            },
            configured_capital=42000.0,
            today=TODAY,
        )
        self.assertEqual(diagnosis.proposal, {repair.STARTING_BALANCE: 42000.0})

    def test_a_negative_reference_is_flagged(self):
        diagnosis = repair.diagnose(
            {
                "user_id": "u1",
                "starting_balance": -50.0,
                "daily_start_balance": 900.0,
                "daily_date": TODAY,
            },
            configured_capital=42000.0,
            today=TODAY,
        )
        self.assertEqual(diagnosis.status, repair.STATE_REPAIRABLE)
        self.assertIn("-50", diagnosis.problems[0])

    def test_without_a_known_capital_nothing_is_invented(self):
        diagnosis = repair.diagnose(
            {
                "user_id": "u1",
                "starting_balance": None,
                "daily_start_balance": 900.0,
                "daily_date": TODAY,
            },
            configured_capital=None,
            today=TODAY,
        )
        self.assertEqual(diagnosis.status, repair.STATE_NEEDS_VALUE)
        self.assertEqual(diagnosis.proposal, {})
        self.assertIn("--set", diagnosis.problems[0])

    def test_a_closed_day_is_reported_without_a_write(self):
        # Une journée close n'a pas de perte « déjà subie » : le garde réinitialise.
        diagnosis = repair.diagnose(
            {
                "user_id": "u1",
                "starting_balance": 1000.0,
                "daily_start_balance": 0,
                "daily_date": YESTERDAY,
            },
            configured_capital=1000.0,
            today=TODAY,
        )
        self.assertEqual(diagnosis.status, repair.STATE_OK)
        self.assertEqual(diagnosis.proposal, {})
        self.assertTrue(diagnosis.notes)

    def test_an_open_day_falls_back_to_the_capital_reference(self):
        diagnosis = repair.diagnose(
            {
                "user_id": "u1",
                "starting_balance": 0,
                "daily_start_balance": 0,
                "daily_date": TODAY,
            },
            configured_capital=5000.0,
            today=TODAY,
        )
        self.assertEqual(
            diagnosis.proposal,
            {repair.STARTING_BALANCE: 5000.0, repair.DAILY_START_BALANCE: 5000.0},
        )

    def test_a_healthy_row_needs_nothing(self):
        diagnosis = repair.diagnose(
            {
                "user_id": "u1",
                "starting_balance": 1000.0,
                "daily_start_balance": 900.0,
                "daily_date": TODAY,
            },
            configured_capital=1000.0,
            today=TODAY,
        )
        self.assertEqual(diagnosis.status, repair.STATE_OK)
        self.assertEqual(diagnosis.problems, [])
        self.assertFalse(diagnosis.touched)

    def test_an_explicit_value_wins_over_the_capital(self):
        diagnosis = repair.diagnose(
            {
                "user_id": "u1",
                "starting_balance": 0,
                "daily_start_balance": 900.0,
                "daily_date": TODAY,
            },
            configured_capital=5000.0,
            today=TODAY,
            overrides={repair.STARTING_BALANCE: 7500.0},
        )
        self.assertEqual(diagnosis.proposal, {repair.STARTING_BALANCE: 7500.0})

    def test_a_non_positive_override_is_ignored_like_the_defect(self):
        diagnosis = repair.diagnose(
            {
                "user_id": "u1",
                "starting_balance": None,
                "daily_start_balance": 900.0,
                "daily_date": TODAY,
            },
            configured_capital=5000.0,
            today=TODAY,
            overrides={repair.STARTING_BALANCE: 0.0},
        )
        self.assertEqual(diagnosis.proposal, {repair.STARTING_BALANCE: 5000.0})


class ParseOverridesTest(unittest.TestCase):
    """`--set` : une valeur non positive est le défaut, pas une réparation."""

    def test_a_valid_pair_is_parsed(self):
        parsed = repair.parse_overrides(["u1:starting_balance=5000"])
        self.assertEqual(parsed, {"u1": {repair.STARTING_BALANCE: 5000.0}})

    def test_a_zero_or_negative_value_is_refused(self):
        for raw in ("0", "-1"):
            with self.subTest(value=raw):
                with self.assertRaises(ValueError):
                    repair.parse_overrides([f"u1:starting_balance={raw}"])

    def test_an_unknown_field_is_refused(self):
        with self.assertRaises(ValueError):
            repair.parse_overrides(["u1:current_balance=10"])

    def test_a_malformed_item_is_refused(self):
        for raw in ("u1", "u1:starting_balance", ":starting_balance=1", "u1:starting_balance=x"):
            with self.subTest(item=raw):
                with self.assertRaises(ValueError):
                    repair.parse_overrides([raw])


class SurveyAndApplyTest(unittest.TestCase):
    """La lecture, l'écriture — et ce qui ne doit jamais arriver."""

    def setUp(self):
        self.double = SupabaseDouble()
        mock.patch.object(repair, "supabase", self.double).start()
        self.addCleanup(mock.patch.stopall)

    def _seed(self, states, prefs):
        self.double.store(repair.TABLE).rows = states
        self.double.store(repair.PREFERENCES_TABLE).rows = prefs

    def test_survey_reads_the_capital_and_flags_broken_rows(self):
        self._seed(
            states=[
                {"user_id": "good", "starting_balance": 1000.0, "daily_start_balance": 950.0, "daily_date": TODAY},
                {"user_id": "broken", "starting_balance": 0, "daily_start_balance": 950.0, "daily_date": TODAY},
            ],
            prefs=[
                {"user_id": "good", "paper_equity": 1000.0},
                {"user_id": "broken", "paper_equity": 8000.0},
            ],
        )
        diagnoses = {d.user_id: d for d in repair.survey(today=TODAY)}
        self.assertEqual(diagnoses["good"].status, repair.STATE_OK)
        self.assertEqual(diagnoses["broken"].proposal, {repair.STARTING_BALANCE: 8000.0})

    def test_survey_writes_nothing(self):
        self._seed(
            states=[{"user_id": "broken", "starting_balance": 0, "daily_start_balance": 1.0, "daily_date": TODAY}],
            prefs=[{"user_id": "broken", "paper_equity": 8000.0}],
        )
        repair.survey(today=TODAY)
        self.assertEqual(self.double.writes, [])

    def test_apply_writes_only_positive_proposals_and_never_deletes(self):
        self._seed(
            states=[
                {"user_id": "broken", "starting_balance": 0, "daily_start_balance": 950.0, "daily_date": TODAY},
                {"user_id": "unknown", "starting_balance": None, "daily_start_balance": 950.0, "daily_date": TODAY},
            ],
            prefs=[{"user_id": "broken", "paper_equity": 8000.0}],
        )
        diagnoses = repair.survey(today=TODAY)
        written = repair.apply_repairs(diagnoses)

        self.assertEqual(written, 1)
        self.assertEqual(self.double.write_order("update"), [repair.TABLE])
        self.assertEqual(self.double.updates(), [{repair.STARTING_BALANCE: 8000.0}])
        self.assertFalse(self.double.store(repair.TABLE).deleted)

    def test_a_defensive_non_positive_proposal_is_not_written(self):
        diagnosis = repair.Diagnosis(
            user_id="u1", status=repair.STATE_REPAIRABLE, proposal={repair.STARTING_BALANCE: 0.0}
        )
        written = repair.apply_repairs([diagnosis])
        self.assertEqual(written, 0)
        self.assertEqual(self.double.writes, [])

    def test_run_reports_counts_and_applies_only_when_asked(self):
        self._seed(
            states=[{"user_id": "broken", "starting_balance": 0, "daily_start_balance": 950.0, "daily_date": TODAY}],
            prefs=[{"user_id": "broken", "paper_equity": 8000.0}],
        )
        dry = repair.run(apply=False, today=TODAY)
        self.assertEqual(dry["repairable"], 1)
        self.assertEqual(dry["repaired"], 0)
        self.assertEqual(self.double.writes, [])

        applied = repair.run(apply=True, today=TODAY)
        self.assertEqual(applied["repaired"], 1)
        self.assertEqual(self.double.updates(), [{repair.STARTING_BALANCE: 8000.0}])


class CommandLineTest(unittest.TestCase):
    """Le script : codes de sortie, et un rapport qui nomme ce qu'il reste."""

    def setUp(self):
        self.cli = _load_cli()
        self.double = SupabaseDouble()
        mock.patch.object(repair, "supabase", self.double).start()
        self.addCleanup(mock.patch.stopall)
        self.double.store(repair.TABLE).rows = [
            {"user_id": "broken", "starting_balance": 0, "daily_start_balance": 950.0, "daily_date": TODAY}
        ]
        self.double.store(repair.PREFERENCES_TABLE).rows = [
            {"user_id": "broken", "paper_equity": 8000.0}
        ]

    def _run(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.cli.main(argv)
        return code, out.getvalue()

    def test_a_dry_run_exits_one_and_writes_nothing(self):
        code, text = self._run([])
        self.assertEqual(code, 1)
        self.assertIn("utilisateur broken", text)
        self.assertEqual(self.double.writes, [])

    def test_apply_exits_zero_once_everything_is_repaired(self):
        code, text = self._run(["--apply"])
        self.assertEqual(code, 0)
        self.assertEqual(self.double.updates(), [{repair.STARTING_BALANCE: 8000.0}])
        self.assertIn("réparée", text)

    def test_a_missing_capital_stays_open_and_exits_one(self):
        self.double.store(repair.PREFERENCES_TABLE).rows = []
        code, text = self._run(["--apply"])
        self.assertEqual(code, 1)
        self.assertEqual(self.double.writes, [])
        self.assertIn("--set", text)

    def test_a_non_positive_set_is_a_usage_error(self):
        code, _text = self._run(["--set", "broken:starting_balance=0", "--apply"])
        self.assertEqual(code, 2)
        self.assertEqual(self.double.writes, [])

    def test_an_unconfigured_database_is_a_usage_error(self):
        with mock.patch.object(repair, "supabase", None):
            code, _text = self._run([])
        self.assertEqual(code, 2)

    def test_json_output_carries_the_proposal(self):
        code, text = self._run(["--json"])
        self.assertEqual(code, 1)
        payload = __import__("json").loads(text)
        self.assertEqual(payload["repairable"], 1)
        self.assertEqual(payload["diagnoses"][0]["proposal"], {repair.STARTING_BALANCE: 8000.0})


if __name__ == "__main__":
    unittest.main()
