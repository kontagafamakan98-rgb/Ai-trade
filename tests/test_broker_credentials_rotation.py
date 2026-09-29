"""La rotation, vue depuis les identifiants broker et depuis le routage des ordres.

Deux défauts sont éprouvés ici, et le premier est le plus grave :

* un chiffré qu'on ne sait **pas** rouvrir rendait `None`, exactement comme
  « aucun compte connecté ». L'appelant se rabattait alors sur le compte partagé —
  l'ordre d'un client parti sur le compte du propriétaire, en silence. C'est
  désormais une exception, et un ordre **refusé** ;
* poser une clé neuve laissait les anciennes lignes illisibles pour toujours faute
  de pouvoir les recenser : `reencrypt_all` les compte (sans rien écrire) puis les
  réécrit avec la clé active, ce qui **termine** la rotation.

La base est la doublure partagée (`tests/supabase_double.py`) : les écritures y
persistent et les lectures y sont filtrées comme PostgREST les filtre, donc un
`update` qui n'a pas eu lieu ne peut pas passer pour une réussite.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import io
import pathlib
import secrets
import sys
import unittest
from unittest import mock

from database import broker_credentials as bc
from database import preferences as prefs
from execution import order_executor as oe
from tests.supabase_double import SupabaseDouble
from utils import encryption as enc

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TABLE = "user_broker_credentials"


def _key() -> str:
    """Une clé Fernet neuve, fabriquée ici — jamais celle du `.env` du dépôt."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _load_rotate_cli():
    """Le script de rotation, importé par son chemin (les autres tests font de même)."""
    spec = importlib.util.spec_from_file_location(
        "rotate_encryption_key", REPO_ROOT / "scripts" / "rotate_encryption_key.py"
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


class RingTestBase(unittest.TestCase):
    """Anneau « v2 active, v1 retirée » et base en mémoire, pour chaque test."""

    def setUp(self):
        self.key_v1, self.key_v2 = _key(), _key()
        self.ring_v1 = enc.build_ring(self.key_v1)
        self.ring = enc.build_ring(f"v2:{self.key_v2}", f"v1:{self.key_v1}")
        mock.patch.object(enc, "ENCRYPTION_KEY", f"v2:{self.key_v2}").start()
        mock.patch.object(enc, "ENCRYPTION_KEYS_PREVIOUS", f"v1:{self.key_v1}").start()
        self.addCleanup(mock.patch.stopall)
        enc.reset_ring()
        self.addCleanup(enc.reset_ring)

        self.double = SupabaseDouble()
        mock.patch.object(bc, "supabase", self.double).start()
        # `get_user_risk_pct` / `get_user_equity` lisent les préférences : sans
        # doublure, elles lèvent, se rabattent sur les défauts, et **l'écrivent
        # dans la sortie du test** — un test qui passe en criant n'apprend rien à
        # celui qui le relit.
        mock.patch.object(prefs, "supabase", self.double).start()

    def seed(self, user_id: str, *, builder=None, key: str = "cle-api", secret: str = "secret-api"):
        """Une ligne chiffrée par `builder` (l'anneau actif par défaut)."""
        ring = builder or self.ring
        row = {
            "user_id": user_id,
            "broker": "alpaca",
            "paper": True,
            "api_key_enc": ring.encrypt(key),
            "api_secret_enc": ring.encrypt(secret),
        }
        self.double.store(TABLE).rows.append(row)
        return row

    def stored(self, user_id: str) -> dict:
        for row in self.double.store(TABLE).rows:
            if row["user_id"] == user_id:
                return row
        raise AssertionError(f"ligne absente pour {user_id}")


class ReadTest(RingTestBase):
    """Lire une ligne : présent, rouvrable, ou franchement illisible."""

    def test_no_row_means_absent_not_unreadable(self):
        self.assertIsNone(bc.get_broker_credentials("inconnu"))

    def test_a_row_written_with_a_retired_key_is_still_readable(self):
        self.seed("u1", builder=self.ring_v1)

        creds = bc.get_broker_credentials("u1")

        self.assertEqual(creds["api_key"], "cle-api")
        self.assertEqual(creds["api_secret"], "secret-api")
        self.assertEqual(creds["key_version"], 1, "la version reste visible pour l'opérateur")
        self.assertTrue(enc.needs_reencryption(self.stored("u1")["api_key_enc"]))

    def test_a_row_written_today_reads_at_the_active_version(self):
        self.seed("u1")

        creds = bc.get_broker_credentials("u1")

        self.assertEqual(creds["key_version"], 2)
        self.assertFalse(enc.needs_reencryption(self.stored("u1")["api_key_enc"]))

    def test_a_row_whose_key_is_gone_raises_and_never_returns_none(self):
        """`None` dirait « aucun compte » — et l'ordre partirait sur le compte partagé."""
        self.seed("u1", builder=enc.build_ring(_key()))

        with self.assertRaises(bc.BrokerCredentialsUnreadable) as caught:
            bc.get_broker_credentials("u1")

        message = str(caught.exception)
        self.assertIn("u1", message)
        self.assertIn("compte partagé", message, "le message dit ce qui N'est PAS fait")
        self.assertNotIn("cle-api", message)
        self.assertNotIn(self.stored("u1")["api_key_enc"], message)

    def test_a_broken_key_ring_refuses_too_instead_of_downgrading(self):
        """Une faute dans la configuration n'est pas « aucun compte connecté »."""
        self.seed("u1")
        mock.patch.object(enc, "ENCRYPTION_KEY", f"vX:{self.key_v2}").start()
        enc.reset_ring()

        with self.assertRaises(bc.BrokerCredentialsUnreadable) as caught:
            bc.get_broker_credentials("u1")

        self.assertIn("préfixe de version", str(caught.exception))

    def test_writing_credentials_stores_the_active_version(self):
        bc.set_broker_credentials("u1", "cle-api", "secret-api")

        stored = self.stored("u1")
        self.assertTrue(stored["api_key_enc"].startswith("v2:"))
        self.assertEqual(bc.get_broker_credentials("u1")["api_key"], "cle-api")


class ReencryptionTest(RingTestBase):
    """Terminer la rotation : compter d'abord, réécrire ensuite, jamais les deux."""

    def test_a_dry_run_counts_without_writing_anything(self):
        self.seed("stale", builder=self.ring_v1)
        self.seed("fresh")

        report = bc.reencrypt_all()

        self.assertEqual(report["rows"], 2)
        self.assertEqual(report["by_version"], {2: 1, 1: 1})
        self.assertEqual(report["to_rotate"], 1)
        self.assertEqual(report["rewritten"], 0)
        self.assertFalse(report["applied"])
        self.assertEqual(self.double.write_order(), [], "un comptage n'écrit rien")
        self.assertTrue(self.stored("stale")["api_key_enc"].startswith("v1:"))

    def test_apply_rewrites_only_the_stale_row(self):
        self.seed("stale", builder=self.ring_v1)
        self.seed("fresh")
        before = self.stored("fresh")["api_key_enc"]

        report = bc.reencrypt_all(apply=True)

        self.assertTrue(report["applied"])
        self.assertEqual(report["rewritten"], 1)
        self.assertEqual(self.double.write_order("update"), [TABLE])
        rewritten = self.stored("stale")["api_key_enc"]
        self.assertTrue(rewritten.startswith("v2:"))
        self.assertEqual(enc.decrypt(rewritten), "cle-api")
        self.assertEqual(enc.decrypt(self.stored("stale")["api_secret_enc"]), "secret-api")
        self.assertEqual(self.stored("fresh")["api_key_enc"], before, "rien à réécrire ici")

    def test_the_rotation_ends_and_can_be_checked_afterwards(self):
        self.seed("stale", builder=self.ring_v1)

        bc.reencrypt_all(apply=True)
        after = bc.reencrypt_all()

        self.assertEqual(after["to_rotate"], 0)
        self.assertEqual(after["by_version"], {2: 1})
        self.assertEqual(after["ring_versions"], [2, 1], "l'anneau, lui, n'a pas changé")

    def test_an_unreadable_row_is_counted_and_left_alone(self):
        foreign = self.seed("perdu", builder=enc.build_ring(_key()))
        before = dict(foreign)

        report = bc.reencrypt_all(apply=True)

        self.assertEqual(report["to_rotate"], 0)
        self.assertEqual([user for user, _reason in report["unreadable"]], ["perdu"])
        self.assertEqual(report["rewritten"], 0)
        self.assertEqual(self.double.write_order(), [])
        self.assertEqual(self.stored("perdu"), before)

    def test_without_supabase_the_report_says_what_is_missing(self):
        with mock.patch.object(bc, "supabase", None):
            with self.assertRaises(RuntimeError) as caught:
                bc.reencrypt_all()

        self.assertIn("SUPABASE_URL", str(caught.exception))


class RotateScriptTest(RingTestBase):
    """Le script de rotation : ce qu'il compte, et le code de sortie qui va avec."""

    def setUp(self):
        super().setUp()
        self.cli = _load_rotate_cli()

    def _run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_a_count_exits_one_and_never_prints_a_value(self):
        self.seed("stale", builder=self.ring_v1)

        code, out, err = self._run()

        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertIn("à réécrire", out)
        self.assertNotIn("cle-api", out)
        self.assertNotIn(self.stored("stale")["api_key_enc"], out)

    def test_apply_exits_zero_once_everything_is_rewritten(self):
        """Ce qui reste décide du code : ce qui vient d'être fait ne le bloque plus."""
        self.seed("stale", builder=self.ring_v1)
        self.seed("fresh")

        code, out, _err = self._run("--apply")

        self.assertEqual(code, 0, out)
        self.assertIn("réécrite(s) avec la clé active", out)
        self.assertTrue(self.stored("stale")["api_key_enc"].startswith("v2:"))
        self.assertEqual(enc.decrypt(self.stored("stale")["api_key_enc"]), "cle-api")

    def test_an_unreadable_row_keeps_the_script_in_failure(self):
        self.seed("perdu", builder=enc.build_ring(_key()))

        code, out, _err = self._run("--apply")

        self.assertEqual(code, 1)
        self.assertIn("perdu", out)


class OrderRoutingTest(RingTestBase):
    """Un compte illisible refuse l'ordre : il ne le déplace pas ailleurs."""

    def test_the_client_is_never_the_shared_one_when_the_row_is_unreadable(self):
        unreadable = bc.BrokerCredentialsUnreadable("u1", "clé absente")

        with mock.patch.object(
            oe, "get_broker_credentials", side_effect=unreadable
        ), mock.patch.object(oe, "TradingClient", create=True) as trading_client:
            with self.assertRaises(bc.BrokerCredentialsUnreadable):
                oe.get_alpaca_client("u1")

        trading_client.assert_not_called()

    def test_a_validated_order_is_refused_instead_of_rerouted(self):
        signal = {
            "asset": "AAPL",
            "direction": "BUY",
            "entry": 100.0,
            "stop_loss": 98.0,
            "take_profit": 104.0,
            "confidence": 0.7,
        }
        unreadable = bc.BrokerCredentialsUnreadable("u1", "clé absente")
        with mock.patch.object(oe, "PAPER_TRADING", True), mock.patch.object(
            oe, "get_broker_credentials", side_effect=unreadable
        ), mock.patch.object(oe, "get_alpaca_client") as personal, mock.patch.object(
            oe, "risk_can_trade"
        ) as risk:
            result = asyncio.run(oe.execute_validated_order("u1", signal))

        self.assertEqual(result["status"], "blocked_broker_credentials")
        self.assertEqual(result["method"], "blocked")
        self.assertNotIn("account", result, "aucun compte n'a été choisi")
        self.assertIn("u1", result["error"])
        personal.assert_not_called()
        risk.assert_not_called()


if __name__ == "__main__":
    unittest.main()
