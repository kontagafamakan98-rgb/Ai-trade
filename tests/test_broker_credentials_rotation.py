"""La rotation, vue depuis les identifiants broker et depuis le routage des ordres.

Trois défauts sont éprouvés ici, dans l'ordre de leur gravité :

* un chiffré qu'on ne sait **pas** rouvrir rendait `None`, exactement comme
  « aucun compte connecté ». L'appelant se rabattait alors sur le compte partagé —
  l'ordre d'un client parti sur le compte du propriétaire, en silence. C'est
  désormais une exception, et un ordre **refusé** ;
* poser une clé neuve laissait les anciennes lignes illisibles pour toujours faute
  de pouvoir les recenser : `reencrypt_all` les compte (sans rien écrire) puis les
  réécrit avec la clé active, ce qui **termine** la rotation ;
* la sonde de solde, elle, se **taisait** : une clé révoquée ou une panne réseau
  faisait valider l'ordre par le garde-fou de risque sur l'equity statique des
  préférences — un chiffre présenté comme le solde du compte. `EquityProbeTest`
  exige maintenant que la sonde parle : le solde inconnu refuse l'ordre.

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


class EquityProbeTest(RingTestBase):
    """La sonde de solde : elle nourrit le garde-fou de risque, donc elle ne se tait pas.

    Deux déguisements y étaient possibles, et aucun ne se voyait à l'œil nu :

    * un SDK absent ressortait en `NameError` sur `TradingClient`, avalé plus loin
      en `status: "error"` — avec un conseil parlant de clés, de symbole et de
      marché ouverts. Une dépendance manquante se répare ailleurs, et se dit
      autrement ;
    * une sonde qui échouait laissait le garde-fou évaluer l'ordre sur l'equity
      **statique** des préférences : une clé révoquée ressemblait alors à un solde
      connu, et l'ordre partait quand même.

    Le paquet `alpaca-py` n'est pas installé ici (`ALPACA_OK` est faux), donc les
    quatre noms importés n'existent même pas dans le module : la doublure les pose
    avec `create=True`. C'est ce qui rend le chemin d'envoi éprouvable sans
    dépendre du paquet — et sans qu'un `NameError` fasse passer un test pour une
    réussite.
    """

    SIGNAL = {
        "asset": "AAPL",
        "direction": "BUY",
        "entry": 100.0,
        "stop_loss": 98.0,
        "take_profit": 104.0,
        "confidence": 0.7,
    }

    def _sdk(self, *, equity="1234.5", account_error=None):
        """(fabrique de client, client) — le SDK absent, remplacé par des doublures."""
        client = mock.Mock()
        if account_error is not None:
            client.get_account.side_effect = account_error
        else:
            client.get_account.return_value = mock.Mock(equity=equity)
        client.submit_order.return_value = mock.Mock(id="o-1", symbol="AAPL")
        factory = mock.Mock(return_value=client)
        for name, value in (
            ("ALPACA_OK", True),
            ("TradingClient", factory),
            ("MarketOrderRequest", lambda **kw: mock.Mock(**kw)),
            ("OrderSide", mock.Mock(BUY="buy", SELL="sell")),
            ("TimeInForce", mock.Mock(DAY="day")),
        ):
            patcher = mock.patch.object(oe, name, value, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        return factory, client

    def _execute(self, *, user_id="u1", signal=None):
        return asyncio.run(oe.execute_validated_order(user_id, dict(signal or self.SIGNAL)))

    def test_the_probed_balance_is_what_the_risk_guard_sees(self):
        """Le solde du compte, pas celui des préférences — et le compte est le sien."""
        self.seed("u1")
        factory, _client = self._sdk(equity="1234.5")

        with mock.patch.object(oe, "risk_can_trade", return_value=(True, "")) as risk:
            result = self._execute()

        self.assertEqual(result["status"], "submitted_paper")
        self.assertEqual(result["account"], "personal")
        self.assertEqual(result["method"], "alpaca_paper")
        risk.assert_called_once_with("u1", 1234.5)
        self.assertEqual(
            factory.call_args.kwargs["api_key"],
            "cle-api",
            "le client doit être bâti depuis la ligne déchiffrée de cet utilisateur",
        )

    def test_a_failed_probe_blocks_instead_of_sizing_on_a_static_figure(self):
        """Une clé révoquée ne doit pas ressembler à un solde connu."""
        self.seed("u1")
        self._sdk(account_error=RuntimeError("clé révoquée"))

        with mock.patch.object(oe, "risk_can_trade") as risk:
            result = self._execute()

        self.assertEqual(result["status"], "blocked_equity_unknown")
        self.assertEqual(result["method"], "blocked")
        risk.assert_not_called()
        self.assertNotIn("account", result, "aucun compte n'a été choisi")
        self.assertIn("RuntimeError", result["error"], "la cause d'origine est nommée")
        self.assertIn("statique", result["note"], "le refus dit ce qui n'a PAS été fait")

    def test_a_missing_sdk_is_named_rather_than_a_vague_broker_error(self):
        self.seed("u1")

        with mock.patch.object(oe, "ALPACA_OK", False), mock.patch.object(
            oe, "TradingClient", create=True
        ) as trading_client, mock.patch.object(oe, "risk_can_trade") as risk:
            result = self._execute()

        self.assertEqual(result["status"], "blocked_broker_sdk_missing")
        self.assertEqual(result["method"], "blocked")
        trading_client.assert_not_called()
        risk.assert_not_called()
        self.assertIn("alpaca-py", result["error"])
        self.assertNotIn("symbole", result["note"], "pas de conseil de marché ici")

    def test_the_shared_path_names_the_missing_sdk_too(self):
        """Sans compte personnel, l'absence du SDK reste la cause à réparer."""
        with mock.patch.object(oe, "ALPACA_OK", False), mock.patch.object(
            oe, "TradingClient", create=True
        ) as trading_client:
            with self.assertRaises(oe.AlpacaSDKUnavailable) as caught:
                oe.get_alpaca_client("u1")

        trading_client.assert_not_called()
        self.assertIn("alpaca-py", str(caught.exception))

    def test_the_shared_key_still_builds_a_client_when_no_account_is_connected(self):
        """Le repli partagé reste un chemin légitime : le durcissement ne le ferme pas."""
        factory, _client = self._sdk()

        with mock.patch.object(oe, "ALPACA_API_KEY", "cle-partagee"), mock.patch.object(
            oe, "ALPACA_SECRET_KEY", "secret-partage"
        ):
            _client_built, source = oe.get_alpaca_client("u1")

        self.assertEqual(source, "shared")
        self.assertEqual(
            factory.call_args.kwargs,
            {"api_key": "cle-partagee", "secret_key": "secret-partage", "paper": True},
        )

    def test_the_two_reasons_to_simulate_are_named_separately(self):
        """« Aucun compte connecté » ne doit pas servir aussi à dire « SDK absent ».

        La seconde branche était **inatteignable** : la première avalait déjà le cas
        du SDK manquant et le présentait comme une absence de compte, donc comme un
        défaut de configuration à corriger ailleurs.
        """
        with mock.patch.object(oe, "ALPACA_OK", False):
            without_sdk = self._execute()

        with mock.patch.object(oe, "ALPACA_OK", True), mock.patch.object(
            oe, "ALPACA_API_KEY", ""
        ), mock.patch.object(oe, "ALPACA_SECRET_KEY", ""):
            without_keys = self._execute()

        self.assertEqual(without_sdk["status"], "simulated_paper")
        self.assertEqual(without_keys["status"], "simulated_paper")
        self.assertNotEqual(without_sdk["note"], without_keys["note"])
        self.assertIn("SDK", without_sdk["note"])
        self.assertIn("Aucun compte connecté", without_keys["note"])


if __name__ == "__main__":
    unittest.main()
