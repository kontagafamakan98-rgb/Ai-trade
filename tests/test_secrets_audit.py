import base64
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from core import secrets_audit as sa

REPO_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 26, 12, 0, 0)


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")


def _key(seed: int = 0) -> str:
    """Une clé Fernet **distincte par `seed`** : l'anneau en veut plusieurs."""
    return base64.urlsafe_b64encode(bytes((seed + i) % 256 for i in range(32))).decode("ascii")


def strong_values() -> dict:
    return {
        "WEBHOOK_SECRET": "whsec_9f3a2c7b1d4e6f8a",
        "INTERNAL_API_KEY": "intkey_5b8e2d1c9a7f4e3b",
        "ENCRYPTION_KEY": _fernet_key(),
        "SUPABASE_SERVICE_KEY": "sbp_9a1b2c3d4e5f67890a1b2c3d4e5f6789",
        "TELEGRAM_BOT_TOKEN": "123456789:AAF7c3d2e1b0a9f8e7d6c5b4a3f2e1d0",
    }


#: Tous les noms que l'audit lit — y compris ceux que ces tests ne posent pas.
SECRET_NAMES = tuple(spec.name for spec in sa.DEFAULT_SPECS)


def _fixture_environment(values: dict) -> dict:
    """Pose `values` dans l'environnement et **retire** les autres secrets connus.

    `--no-env-file` empêche l'audit de *lire* `.env` ; il ne peut pas défaire
    `config.py`, qui l'a déjà chargé dans `os.environ` à son import. Sous
    `unittest discover`, l'anneau du `.env` réel (`ENCRYPTION_KEYS_PREVIOUS`) est
    donc présent, et la clé de ce module est elle aussi en v1 : l'audit refuse
    alors « deux clés pour une version », pour une raison qui ne parle pas du code
    mais de la machine — qui a pourtant tourné sa clé comme il faut.

    Rend la sauvegarde complète, à passer à `_restore_environment`.
    """
    names = set(SECRET_NAMES) | set(values)
    saved = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    os.environ.update(values)
    return saved


def _restore_environment(saved: dict) -> None:
    """Remet l'environnement exactement comme `_fixture_environment` l'a trouvé."""
    for name, previous in saved.items():
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def _fresh_ledger(values: dict, now: datetime = NOW) -> dict:
    return {"version": sa.LEDGER_VERSION, "secrets": sa.build_ledger_entries(values, now=now)}


class TestStrengthChecks(unittest.TestCase):
    def test_complete_strong_set_is_ok(self):
        values = strong_values()
        result = sa.run_audit(values, ledger=_fresh_ledger(values), now=NOW)
        self.assertTrue(result.ok, result.as_dict())
        self.assertEqual(result.errors, [])

    def test_missing_required_secret_is_flagged(self):
        values = strong_values()
        values.pop("WEBHOOK_SECRET")
        result = sa.run_audit(values, ledger=_fresh_ledger(values), now=NOW)
        self.assertFalse(result.ok)
        self.assertTrue(any(i.name == "WEBHOOK_SECRET" for i in result.errors))

    def test_placeholder_webhook_is_flagged(self):
        values = strong_values()
        values["WEBHOOK_SECRET"] = "change-me-super-secret"
        result = sa.run_audit(values, ledger=_fresh_ledger(values), now=NOW)
        self.assertFalse(result.ok)
        self.assertTrue(any("remplacement" in i.message for i in result.errors))

    def test_too_short_secret_is_flagged(self):
        values = strong_values()
        values["INTERNAL_API_KEY"] = "short"
        result = sa.run_audit(values, ledger=_fresh_ledger(values), now=NOW)
        self.assertFalse(result.ok)
        self.assertTrue(any("trop court" in i.message for i in result.errors))

    def test_low_entropy_secret_is_flagged(self):
        values = strong_values()
        values["INTERNAL_API_KEY"] = "aaaaaaaaaaaaaaaaaaaaaaaa"
        result = sa.run_audit(values, ledger=_fresh_ledger(values), now=NOW)
        self.assertFalse(result.ok)
        self.assertTrue(any("entropie" in i.message for i in result.errors))

    def test_shared_secret_between_usages_is_flagged(self):
        values = strong_values()
        values["INTERNAL_API_KEY"] = values["WEBHOOK_SECRET"]
        result = sa.run_audit(values, ledger=_fresh_ledger(values), now=NOW)
        self.assertFalse(result.ok)
        self.assertTrue(any("identique" in i.message for i in result.errors))

    def test_invalid_fernet_key_is_flagged(self):
        values = strong_values()
        values["ENCRYPTION_KEY"] = ("x1Y2z3Q4" * 6)[:44]  # 44 car. mais 33 octets décodés
        result = sa.run_audit(values, ledger=_fresh_ledger(values), now=NOW)
        self.assertFalse(result.ok)
        self.assertTrue(any("Fernet" in i.message for i in result.errors))

    def test_valid_fernet_key_accepted(self):
        self.assertTrue(sa.is_valid_fernet_key(_fernet_key()))
        self.assertFalse(sa.is_valid_fernet_key("abc"))
        self.assertFalse(sa.is_valid_fernet_key(base64.urlsafe_b64encode(b"x").decode()))

    def test_optional_secret_absent_is_ignored(self):
        values = strong_values()
        result = sa.run_audit(values, ledger=_fresh_ledger(values), now=NOW)
        self.assertNotIn("GEMINI_API_KEY", result.checked)

    def test_optional_secret_placeholder_is_warning_not_error(self):
        values = strong_values()
        values["GROQ_API_KEY"] = "change-me-groq"
        result = sa.run_audit(values, ledger=_fresh_ledger(values), now=NOW)
        self.assertTrue(result.ok, result.as_dict())
        self.assertTrue(any(i.name == "GROQ_API_KEY" and not i.is_error for i in result.issues))


class TestFingerprintAndLedger(unittest.TestCase):
    def test_fingerprint_stable_and_value_dependent(self):
        a = sa.secret_fingerprint("WEBHOOK_SECRET", "value-1")
        b = sa.secret_fingerprint("WEBHOOK_SECRET", "value-1")
        c = sa.secret_fingerprint("WEBHOOK_SECRET", "value-2")
        d = sa.secret_fingerprint("INTERNAL_API_KEY", "value-1")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertNotEqual(a, d)
        self.assertNotIn("value-1", a)

    def test_ledger_roundtrip(self):
        values = strong_values()
        entries = sa.build_ledger_entries(values, now=NOW)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            sa.save_ledger(path, {"version": 1, "secrets": entries})
            loaded = sa.load_ledger(path)
        self.assertEqual(set(loaded["secrets"]), set(entries))
        self.assertEqual(loaded["secrets"]["WEBHOOK_SECRET"]["rotated_at"], "2026-09-26")

    def test_the_ledger_is_written_in_lf_even_on_windows(self):
        """Le registre s'écrit en octets : `write_text` donnerait du CRLF sur Windows.

        Le défaut ne se voyait pas ici — la traduction des fins de ligne est
        justement ce qui ne se lit pas sur Linux — mais il faisait échouer le
        contrôle de fins de ligne du dépôt après un simple `--record`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            sa.save_ledger(path, _fresh_ledger(strong_values()))
            raw = path.read_bytes()
        self.assertNotIn(b"\r\n", raw)
        self.assertTrue(raw.endswith(b"\n"))

    def test_missing_ledger_file_is_empty(self):
        loaded = sa.load_ledger(Path(tempfile.gettempdir()) / "does-not-exist-xyz.json")
        self.assertEqual(loaded["secrets"], {})


class TestLedgerReadState(unittest.TestCase):
    """« absent » et « illisible » ne sont pas le même fait.

    Les deux donnent un registre vide, donc le même « rotation non enregistrée »
    pour chaque secret — et le même conseil trompeur de relancer `--record`, qui
    écraserait l'historique d'un fichier seulement tronqué. Ces tests fixent la
    distinction, et le refus d'écrire par-dessus un registre qu'on n'a pas su lire.
    """

    def _write(self, tmp: str, body: str) -> Path:
        path = Path(tmp) / "ledger.json"
        path.write_text(body, encoding="utf-8")
        return path

    def test_absent_ledger_is_missing_not_corrupt(self):
        path = Path(tempfile.gettempdir()) / "does-not-exist-xyz.json"
        ledger, state, detail = sa.read_ledger(path)
        self.assertEqual(ledger["secrets"], {})
        self.assertEqual(state, sa.LEDGER_STATE_MISSING)
        self.assertEqual(sa.ledger_issues(path), [])
        self.assertIn("absent", detail)

    def test_readable_ledger_is_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, json.dumps({"version": 1, "secrets": {"A": {}}}))
            _ledger, state, _detail = sa.read_ledger(path)
            issues = sa.ledger_issues(path)
        self.assertEqual(state, sa.LEDGER_STATE_OK)
        self.assertEqual(issues, [])

    def test_truncated_json_is_corrupt_and_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, '{"version": 1, "secrets": {"WEBHOOK_SECRET": {"rot')
            _ledger, state, detail = sa.read_ledger(path)
            issues = sa.ledger_issues(path)
        self.assertEqual(state, sa.LEDGER_STATE_CORRUPT)
        self.assertIn("JSON", detail)
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0].is_error)
        self.assertEqual(issues[0].name, sa.LEDGER_ISSUE_NAME)
        self.assertIn(str(path), issues[0].message)
        self.assertIn("illisible", issues[0].message)

    def test_a_non_object_root_is_corrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "[1, 2, 3]")
            _ledger, state, detail = sa.read_ledger(path)
        self.assertEqual(state, sa.LEDGER_STATE_CORRUPT)
        self.assertIn("objet JSON", detail)

    def test_undecodable_bytes_are_corrupt_not_missing(self):
        """Un registre en latin-1 ou tronqué au milieu d'un caractère reste un registre."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            path.write_bytes(b'{"secrets": {"A": "\xff\xfe"}}')
            _ledger, state, detail = sa.read_ledger(path)
        self.assertEqual(state, sa.LEDGER_STATE_CORRUPT)
        self.assertIn("lecture impossible", detail)

    def test_load_ledger_still_yields_an_empty_ledger(self):
        """La compatibilité de `load_ledger` : un dict vide, jamais une exception."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "pas du json")
            loaded = sa.load_ledger(path)
        self.assertEqual(loaded, {"version": sa.LEDGER_VERSION, "secrets": {}})

    def test_run_audit_carries_the_ledger_problem(self):
        """Sans `ledger_problems`, un registre corrompu passerait pour un registre neuf."""
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "pas du json")
            result = sa.run_audit(
                strong_values(),
                ledger=sa.load_ledger(path),
                now=NOW,
                allow_missing_rotation=True,
                environment_problems=sa.ledger_issues(path),
            )
        self.assertFalse(result.ok)
        self.assertTrue(any(i.name == sa.LEDGER_ISSUE_NAME for i in result.errors))

    def test_enforce_secret_rotation_refuses_a_corrupt_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "pas du json")
            with self.assertRaises(RuntimeError) as caught:
                sa.enforce_secret_rotation(
                    strong_values(), ledger_path=str(path), allow_missing=True
                )
        self.assertIn("illisible", str(caught.exception))


class TestLedgerSummary(unittest.TestCase):
    """La synthèse du registre de rotation : ce qu'on lit sans ouvrir le JSON."""

    def test_empty_ledger_counters_are_zero_not_none(self):
        summary = sa.ledger_summary({"version": 1, "secrets": {}}, now=NOW)
        self.assertEqual(summary["entries"], 0)
        self.assertEqual(summary["dated"], 0)
        self.assertIsNone(summary["oldest"])
        self.assertIsNone(summary["oldest_days"])

    def test_oldest_newest_and_age_are_derived_from_the_dates(self):
        ledger = {
            "version": 1,
            "secrets": {
                "A": {"fingerprint": "x", "rotated_at": "2026-01-03"},
                "B": {"fingerprint": "y", "rotated_at": "2026-09-01"},
            },
        }
        summary = sa.ledger_summary(ledger, now=NOW)
        self.assertEqual(summary["entries"], 2)
        self.assertEqual(summary["dated"], 2)
        self.assertEqual(summary["oldest"], "2026-01-03")
        self.assertEqual(summary["newest"], "2026-09-01")
        self.assertEqual(summary["oldest_days"], (NOW.date() - date(2026, 1, 3)).days)
        self.assertEqual(summary["unreadable"], 0)

    def test_an_illegible_date_is_counted_not_silently_dropped(self):
        ledger = {"secrets": {"A": {"rotated_at": "pas-une-date"}}}
        summary = sa.ledger_summary(ledger, now=NOW)
        self.assertEqual(summary["entries"], 1)
        self.assertEqual(summary["dated"], 0)
        self.assertEqual(summary["unreadable"], 1)
        self.assertIsNone(summary["oldest"])


class EffectiveSettingsTest(unittest.TestCase):
    """Les réglages effectifs de l'audit — publiés sans lire un seul secret."""

    def test_the_default_cap_is_published_with_its_source(self):
        settings = sa.effective_settings({})
        self.assertEqual(settings["max_age_days"], sa.DEFAULT_MAX_AGE_DAYS)
        self.assertEqual(settings["max_age_source"], sa.MAX_AGE_SOURCE_DEFAULT)
        self.assertIsNone(settings["max_age_problem"])

    def test_an_env_cap_is_the_effective_one(self):
        settings = sa.effective_settings({sa.MAX_AGE_ENV: "180"})
        self.assertEqual(settings["max_age_days"], 180)
        self.assertEqual(settings["max_age_source"], sa.MAX_AGE_SOURCE_ENV)

    def test_an_unapplied_cap_is_named_here_too(self):
        """Un réglage écrit mais non appliqué ne doit pas passer pour appliqué."""
        settings = sa.effective_settings({sa.MAX_AGE_ENV: "90j"})
        self.assertEqual(settings["max_age_days"], sa.DEFAULT_MAX_AGE_DAYS)
        self.assertEqual(settings["max_age_source"], sa.MAX_AGE_SOURCE_ENV)
        self.assertIn("illisible", settings["max_age_problem"])

    def test_the_ledger_state_comes_from_the_path_the_env_designates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registre.json"
            path.write_text('{"version": 1, "secrets": {"A": {}}}', encoding="utf-8")
            settings = sa.effective_settings({sa.LEDGER_PATH_ENV: str(path)})
        self.assertEqual(settings["ledger_path"], str(path))
        self.assertEqual(settings["ledger_state"], sa.LEDGER_STATE_OK)
        self.assertEqual(settings["ledger_entries"], 1)

    def test_the_published_keys_are_the_audit_policy_not_secret_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registre.json"
            path.write_text('{"secrets": {"A": {"fingerprint": "deadbeef"}}}', encoding="utf-8")
            settings = sa.effective_settings({sa.LEDGER_PATH_ENV: str(path)})
        self.assertEqual(
            set(settings),
            {
                "max_age_days",
                "max_age_source",
                "max_age_problem",
                "ledger_path",
                "ledger_state",
                "ledger_detail",
                "ledger_entries",
            },
        )
        self.assertNotIn("deadbeef", json.dumps(settings), "une empreinte n'est pas un réglage")


class TestMaxAgeSetting(unittest.TestCase):
    """Un plafond de rotation écrit de travers ne doit pas passer pour appliqué.

    Il était avalé : `SECRET_MAX_AGE_DAYS=90j` (l'unité recopiée par habitude), ou
    `=0`, ramenaient l'audit au défaut — ou à 1 jour — en rendant le même verdict
    qu'une machine sans réglage. Or c'est ce plafond qui décide si une rotation est
    « en retard » : un opérateur qui croit avoir réglé 180 jours reçoit un refus
    qu'il ne s'explique pas.
    """

    def test_nothing_set_is_not_a_problem(self):
        self.assertEqual(sa.max_age_issues({}), [])
        self.assertEqual(sa.max_age_issues({sa.MAX_AGE_ENV: ""}), [])

    def test_a_readable_value_is_not_a_problem(self):
        self.assertEqual(sa.max_age_issues({sa.MAX_AGE_ENV: "7"}), [])
        self.assertEqual(sa.resolve_max_age_days(environ={sa.MAX_AGE_ENV: "7"}), 7)

    def test_an_unreadable_value_is_named_and_falls_back(self):
        issues = sa.max_age_issues({sa.MAX_AGE_ENV: "90j"})
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0].is_error)
        self.assertEqual(issues[0].name, sa.MAX_AGE_ENV)
        self.assertIn("illisible", issues[0].message)
        self.assertIn(str(sa.DEFAULT_MAX_AGE_DAYS), issues[0].message)
        # Le repli reste celui d'avant : on le signale, on ne change pas le verdict.
        self.assertEqual(
            sa.resolve_max_age_days(environ={sa.MAX_AGE_ENV: "90j"}),
            sa.DEFAULT_MAX_AGE_DAYS,
        )

    def test_a_non_positive_value_is_named_and_clamped(self):
        issues = sa.max_age_issues({sa.MAX_AGE_ENV: "0"})
        self.assertEqual(len(issues), 1)
        self.assertIn("1 jour", issues[0].message)
        self.assertEqual(sa.resolve_max_age_days(environ={sa.MAX_AGE_ENV: "0"}), 1)

    def test_run_audit_carries_the_setting_problem(self):
        values = strong_values()
        result = sa.run_audit(
            values,
            ledger=_fresh_ledger(values),
            now=NOW,
            allow_missing_rotation=True,
            environment_problems=sa.max_age_issues({sa.MAX_AGE_ENV: "90j"}),
        )
        self.assertFalse(result.ok)
        self.assertTrue(any(i.name == sa.MAX_AGE_ENV for i in result.errors))


class TestRotationChecks(unittest.TestCase):
    def test_missing_rotation_entry_is_error(self):
        values = strong_values()
        result = sa.run_audit(values, ledger={"version": 1, "secrets": {}}, now=NOW)
        self.assertFalse(result.ok)
        self.assertTrue(any("non enregistrée" in i.message for i in result.errors))

    def test_missing_rotation_allowed_is_warning(self):
        values = strong_values()
        result = sa.run_audit(
            values,
            ledger={"version": 1, "secrets": {}},
            now=NOW,
            allow_missing_rotation=True,
        )
        self.assertTrue(result.ok, result.as_dict())
        self.assertTrue(any("non enregistrée" in i.message for i in result.warnings))

    def test_changed_secret_without_record_is_error(self):
        values = strong_values()
        ledger = _fresh_ledger(values)
        values["WEBHOOK_SECRET"] = "whsec_DIFFERENT_VALUE_here"
        result = sa.run_audit(values, ledger=ledger, now=NOW)
        self.assertFalse(result.ok)
        self.assertTrue(any("modifié sans rotation" in i.message for i in result.errors))

    def test_expired_rotation_is_error(self):
        values = strong_values()
        ledger = _fresh_ledger(values, now=NOW - timedelta(days=120))
        result = sa.run_audit(values, ledger=ledger, now=NOW, max_age_days=90)
        self.assertFalse(result.ok)
        self.assertTrue(any("rotation en retard" in i.message for i in result.errors))

    def test_fresh_rotation_is_ok(self):
        values = strong_values()
        ledger = _fresh_ledger(values, now=NOW - timedelta(days=10))
        result = sa.run_audit(values, ledger=ledger, now=NOW, max_age_days=90)
        self.assertTrue(result.ok, result.as_dict())
        self.assertEqual(result.rotation["WEBHOOK_SECRET"]["status"], "ok")
        self.assertEqual(result.rotation["WEBHOOK_SECRET"]["age_days"], 10)

    def test_enforce_secret_rotation_raises_on_failure(self):
        values = strong_values()
        with self.assertRaises(RuntimeError):
            sa.enforce_secret_rotation(values, ledger_path="/nonexistent/ledger.json")


class TestEnvHelpers(unittest.TestCase):
    def test_parse_env_file(self):
        content = (
            "# commentaire\n"
            "[TEMPLATE]\n"
            'WEBHOOK_SECRET="abc123"\n'
            "export INTERNAL_API_KEY=xyz # inline\n"
            "\n"
            "PLAIN=value\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(content, encoding="utf-8")
            values = sa.parse_env_file(path)
        self.assertEqual(values["WEBHOOK_SECRET"], "abc123")
        self.assertEqual(values["INTERNAL_API_KEY"], "xyz")
        self.assertEqual(values["PLAIN"], "value")
        self.assertNotIn("[TEMPLATE]", values)

    def test_environment_overrides_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("WEBHOOK_SECRET=from-file\n", encoding="utf-8")
            merged = sa.load_effective_env(path, environ={"WEBHOOK_SECRET": "from-env"})
        self.assertEqual(merged["WEBHOOK_SECRET"], "from-env")


class TestCli(unittest.TestCase):
    def _load_cli(self):
        spec = importlib.util.spec_from_file_location(
            "verify_secrets", REPO_ROOT / "scripts" / "verify_secrets.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_cli_fails_closed_without_secrets(self):
        cli = self._load_cli()
        with tempfile.TemporaryDirectory() as tmp:
            code = cli.main(
                ["--no-env-file", "--ledger", str(Path(tmp) / "ledger.json"), "--json"]
            )
        self.assertEqual(code, 1)

    def test_cli_records_then_passes(self):
        cli = self._load_cli()
        values = strong_values()
        saved = _fixture_environment(values)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ledger = str(Path(tmp) / "ledger.json")
                self.assertEqual(cli.main(["--no-env-file", "--ledger", ledger, "--record"]), 0)
                recorded = json.loads(Path(ledger).read_text(encoding="utf-8"))
                self.assertIn("WEBHOOK_SECRET", recorded["secrets"])
                # `--no-scan-repo` : ce test porte sur la rotation ; les valeurs
                # utilisées sont des fixtures présentes dans ce fichier de test,
                # donc le scan anti-fuite les signalerait légitimement.
                self.assertEqual(
                    cli.main(
                        ["--no-env-file", "--ledger", ledger, "--quiet", "--no-scan-repo"]
                    ),
                    0,
                )
        finally:
            _restore_environment(saved)

    def test_cli_record_refuses_to_overwrite_a_corrupt_ledger(self):
        """Enregistrer sur un registre illisible effacerait l'historique qu'il porte."""
        cli = self._load_cli()
        saved = _fixture_environment(strong_values())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ledger = Path(tmp) / "ledger.json"
                ledger.write_text(
                    '{"version": 1, "secrets": {"WEBHOOK_SECRET": {"rot', encoding="utf-8"
                )
                before = ledger.read_bytes()
                code = cli.main(["--no-env-file", "--ledger", str(ledger), "--record"])
                after = ledger.read_bytes()
        finally:
            _restore_environment(saved)
        self.assertEqual(code, 2)
        self.assertEqual(after, before, "un registre illisible ne doit jamais être écrasé")

    def test_cli_refuses_a_scan_without_any_target(self):
        """Le gate de CI, éprouvé de bout en bout : sans valeur de référence, refus."""
        cli = self._load_cli()
        saved = _fixture_environment({})
        captured = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with redirect_stdout(captured):
                    code = cli.main(
                        [
                            "--no-env-file",
                            "--ledger",
                            str(Path(tmp) / "ledger.json"),
                            "--json",
                            "--require-scan-targets",
                        ]
                    )
        finally:
            _restore_environment(saved)
        report = json.loads(captured.getvalue())
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any(
                issue["name"] == sa.SCAN_NAME and "aucune valeur de référence" in issue["message"]
                for issue in report["issues"]
            ),
            report,
        )

    def test_cli_refuses_require_scan_targets_without_the_scan(self):
        """Exiger des cibles quand le scan est éteint ne vérifie rien : refus d'appel."""
        cli = self._load_cli()
        self.assertEqual(cli.main(["--require-scan-targets", "--no-scan-repo"]), 2)

    def test_cli_refuses_a_rotation_age_setting_it_cannot_apply(self):
        """Le réglage est lu dans l'environnement : le CLI doit le porter à l'audit."""
        cli = self._load_cli()
        saved = _fixture_environment(strong_values())
        captured = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ledger = str(Path(tmp) / "ledger.json")
                with mock.patch.dict(os.environ, {sa.MAX_AGE_ENV: "quatre-vingt-dix"}):
                    with redirect_stdout(captured):
                        code = cli.main(
                            [
                                "--no-env-file",
                                "--ledger",
                                ledger,
                                "--json",
                                "--no-scan-repo",
                            ]
                        )
        finally:
            _restore_environment(saved)
        report = json.loads(captured.getvalue())
        self.assertEqual(code, 1)
        self.assertTrue(
            any(issue["name"] == sa.MAX_AGE_ENV for issue in report["issues"]), report
        )

    def test_cli_reports_a_corrupt_ledger_as_a_named_error(self):
        cli = self._load_cli()
        saved = _fixture_environment(strong_values())
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ledger = Path(tmp) / "ledger.json"
                ledger.write_text("pas du json", encoding="utf-8")
                code = cli.main(
                    ["--no-env-file", "--ledger", str(ledger), "--quiet", "--no-scan-repo"]
                )
        finally:
            _restore_environment(saved)
        self.assertEqual(code, 1)

    def test_a_rotated_machine_does_not_leak_its_ring_into_the_audit(self):
        """L'anneau du `.env` réel ne doit pas décider du verdict de ces tests.

        `config.py` charge `.env` dans `os.environ` à son import : c'est vérifié ici
        en posant l'anneau **avant** les fixtures, comme le ferait un `unittest
        discover` sur une machine qui a déjà tourné sa clé. Sans la neutralisation,
        l'anneau et la clé de test portent tous deux la v1, et l'audit refuse pour
        une raison étrangère au code.
        """
        cli = self._load_cli()
        os.environ["ENCRYPTION_KEYS_PREVIOUS"] = f"v1:{_key(7)}"
        saved = _fixture_environment(strong_values())
        self.assertNotIn(
            "ENCRYPTION_KEYS_PREVIOUS",
            os.environ,
            "l'anneau laissé par la machine doit être retiré, jamais hérité",
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ledger = str(Path(tmp) / "ledger.json")
                self.assertEqual(cli.main(["--no-env-file", "--ledger", ledger, "--record"]), 0)
                self.assertEqual(
                    cli.main(
                        ["--no-env-file", "--ledger", ledger, "--quiet", "--no-scan-repo"]
                    ),
                    0,
                )
        finally:
            _restore_environment(saved)

    def test_cli_bad_env_file_returns_usage_error(self):
        cli = self._load_cli()
        code = cli.main(["--env-file", str(Path(tempfile.gettempdir()) / "nope-xyz.env")])
        self.assertEqual(code, 2)


class TestRepoLeakScan(unittest.TestCase):
    """Scan anti-fuite : un secret verbatim dans un fichier suivi doit être signalé."""

    SECRET = "whsec_LEAK_9f3a2c7b1d4e6f8a"

    def _values(self):
        return {"WEBHOOK_SECRET": self.SECRET}

    def test_detects_verbatim_secret_with_line_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "config.py").write_text(
                f"x = 1\nAPI = '{self.SECRET}'\n", encoding="utf-8"
            )
            issues = sa.scan_repo_for_secrets(
                self._values(), root=tmp, tracked_files=["config.py"]
            )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].name, "WEBHOOK_SECRET")
        self.assertTrue(issues[0].is_error)
        self.assertIn("config.py:2", issues[0].message)

    def test_only_scans_provided_tracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "leaked.txt").write_text(self.SECRET, encoding="utf-8")
            Path(tmp, "other.txt").write_text(self.SECRET, encoding="utf-8")
            issues = sa.scan_repo_for_secrets(
                self._values(), root=tmp, tracked_files=["other.txt"]
            )
        self.assertEqual(len(issues), 1)
        self.assertIn("other.txt", issues[0].message)

    def test_clean_repo_reports_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "main.py").write_text("print('hello')\n", encoding="utf-8")
            issues = sa.scan_repo_for_secrets(
                self._values(), root=tmp, tracked_files=["main.py"]
            )
        self.assertEqual(issues, [])

    def test_an_oversized_tracked_file_is_named(self):
        """Écarté pour sa taille n'est pas « propre » : le scan ne l'a pas lu."""
        with tempfile.TemporaryDirectory() as tmp:
            body = ("x" * 100 + "\n") * 25_000
            self.assertGreater(len(body), sa.DEFAULT_SCAN_MAX_BYTES)
            Path(tmp, "big.log").write_text(body, encoding="utf-8")
            issues = sa.scan_repo_for_secrets(
                self._values(), root=tmp, tracked_files=["big.log"]
            )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].name, sa.SCAN_NAME)
        self.assertFalse(issues[0].is_error, "hors périmètre : signalé, mais non bloquant")
        self.assertIn("big.log", issues[0].message)

    def test_a_non_regular_tracked_path_is_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "dir.txt").mkdir()
            issues = sa.scan_repo_for_secrets(
                self._values(), root=tmp, tracked_files=["dir.txt"]
            )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].name, sa.SCAN_NAME)
        self.assertIn("dir.txt", issues[0].message)

    def test_an_unreadable_file_is_an_error(self):
        """Lisible pour `stat` mais pas pour `read` : le scan ne peut pas innocenter."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "locked.txt").write_text("rien\n", encoding="utf-8")
            with mock.patch.object(Path, "read_bytes", side_effect=OSError("verrou")):
                issues = sa.scan_repo_for_secrets(
                    self._values(), root=tmp, tracked_files=["locked.txt"]
                )
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0].is_error)
        self.assertEqual(issues[0].name, sa.SCAN_NAME)
        self.assertIn("locked.txt", issues[0].message)

    def test_a_scan_without_reference_values_is_refused_when_required(self):
        """« Le scan n'avait rien à chercher » n'est pas « aucune fuite ».

        C'est le silence exact d'un clone sans `.env` : le hook le dit et laisse
        passer (sinon tout commit serait impossible sur une machine non
        configurée). Un gate de CI, lui, doit refuser ce feu vert-là.
        """
        values = {"WEBHOOK_SECRET": "change-me-super-secret"}  # factice : aucun couple
        self.assertEqual(sa.scannable_targets(values), [])
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.txt").write_text("rien\n", encoding="utf-8")
            result = sa.run_audit(
                values,
                ledger=_fresh_ledger(values),
                now=NOW,
                scan_repo=True,
                repo_root=tmp,
                tracked_files=["a.txt"],
                allow_missing_rotation=True,
                require_scan_targets=True,
            )
        self.assertFalse(result.ok)
        self.assertTrue(
            any(
                i.name == sa.SCAN_NAME and "aucune valeur de référence" in i.message
                for i in result.errors
            ),
            result.as_dict(),
        )

    def test_the_vacuity_gate_stays_quiet_when_there_is_a_target(self):
        values = strong_values()
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.txt").write_text("rien\n", encoding="utf-8")
            result = sa.run_audit(
                values,
                ledger=_fresh_ledger(values),
                now=NOW,
                scan_repo=True,
                repo_root=tmp,
                tracked_files=["a.txt"],
                allow_missing_rotation=True,
                require_scan_targets=True,
            )
        self.assertTrue(result.ok, result.as_dict())

    def test_iter_scan_files_names_what_it_skips_without_noise_on_empty_files(self):
        """Un fichier vide ne peut rien porter : l'écarter ne retire aucune preuve."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "big.txt").write_text("x" * 50, encoding="utf-8")
            Path(tmp, "empty.txt").write_text("", encoding="utf-8")
            skipped: list = []
            kept = [
                Path(p).name
                for p in sa.iter_scan_files(
                    tmp, max_bytes=10, on_skip=lambda p, reason: skipped.append((Path(p).name, reason))
                )
            ]
        self.assertEqual(kept, [])
        self.assertEqual([name for name, _ in skipped], ["big.txt"])
        self.assertIn("limite du scan", skipped[0][1])

    def test_scan_repo_reports_what_it_did_not_read(self):
        """« Aucune fuite » vaut pour un **périmètre** : le scan doit le nommer."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "leak.txt").write_text(self.SECRET, encoding="utf-8")
            Path(tmp, ".env").write_text(self.SECRET, encoding="utf-8")
            Path(tmp, "logo.png").write_bytes(b"\x89PNG\x00")
            Path(tmp, "empty.txt").write_text("", encoding="utf-8")
            vendor = Path(tmp) / ".venv"
            vendor.mkdir()
            (vendor / "lib.py").write_text("rien\n", encoding="utf-8")

            outcome = sa.scan_repo(self._values(), root=tmp)

        self.assertEqual(outcome.targets, 1)
        self.assertEqual(outcome.scanned, 1)
        self.assertEqual(outcome.env, [".env"])
        self.assertEqual(outcome.directories, [".venv"])
        self.assertEqual([label for label, _ in outcome.suffixes], ["logo.png"])
        self.assertEqual(outcome.empty, 1)
        self.assertEqual(outcome.excluded_total, 4)
        # Le seul fichier lu porte la fuite : un seul verdict, nommé.
        self.assertEqual([i.name for i in outcome.issues], ["WEBHOOK_SECRET"])

    def test_scan_repo_scope_is_serialisable(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "clean.txt").write_text("rien\n", encoding="utf-8")
            Path(tmp, ".env").write_text("SECRET=x\n", encoding="utf-8")
            outcome = sa.scan_repo(self._values(), root=tmp, tracked_files=["clean.txt", ".env"])

        report = outcome.as_dict()
        self.assertEqual(report["targets"], 1)
        self.assertEqual(report["scanned"], 1)
        self.assertEqual(report["excluded"]["env"], [".env"])
        self.assertEqual(report["unreadable"], [])

    def test_iter_scan_files_keeps_skips_and_policy_exclusions_apart(self):
        """Écarté par politique et illisible ne disent pas la même chose."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env").write_text("x=1\n", encoding="utf-8")
            Path(tmp, "b.png").write_bytes(b"\x00")
            Path(tmp, "keep.txt").write_text("ok\n", encoding="utf-8")
            excluded: list = []
            skipped: list = []
            kept = [
                Path(p).name
                for p in sa.iter_scan_files(
                    tmp,
                    on_skip=lambda p, reason: skipped.append(Path(p).name),
                    on_excluded=lambda label, reason, kind: excluded.append((label, kind)),
                )
            ]

        self.assertEqual(kept, ["keep.txt"])
        self.assertEqual(skipped, [])
        kinds = {label: kind for label, kind in excluded}
        self.assertEqual(kinds[".env"], sa.EXCLUSION_ENV)
        self.assertEqual(kinds["b.png"], sa.EXCLUSION_SUFFIX)

    def test_run_audit_carries_the_scan_scope_in_its_report(self):
        values = strong_values()
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "clean.txt").write_text("rien\n", encoding="utf-8")
            Path(tmp, ".env").write_text("SECRET=x\n", encoding="utf-8")
            result = sa.run_audit(
                values,
                ledger=_fresh_ledger(values),
                now=NOW,
                scan_repo=True,
                repo_root=tmp,
                tracked_files=["clean.txt", ".env"],
            )
        scan = result.as_dict()["scan"]
        self.assertEqual(scan["targets"], len(sa.scannable_targets(values)))
        self.assertEqual(scan["scanned"], 1)
        self.assertEqual(scan["excluded"]["env"], [".env"])

    def test_a_report_without_scan_does_not_pretend_to_have_one(self):
        values = strong_values()
        result = sa.run_audit(values, ledger=_fresh_ledger(values), now=NOW)
        self.assertIsNone(result.as_dict()["scan"])

    def test_message_masks_the_secret(self):
        """Un rapport de fuite ne doit jamais ré-exposer le secret en clair."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "leak.py").write_text(f"K='{self.SECRET}'", encoding="utf-8")
            issues = sa.scan_repo_for_secrets(
                self._values(), root=tmp, tracked_files=["leak.py"]
            )
        self.assertEqual(len(issues), 1)
        self.assertNotIn(self.SECRET, issues[0].message)
        self.assertIn(sa.mask_secret(self.SECRET), issues[0].message)

    def test_placeholder_and_short_values_are_not_scanned(self):
        values = {
            "WEBHOOK_SECRET": "change-me-super-secret",
            "FINNHUB_API_KEY": "short",
        }
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.txt").write_text(
                "change-me-super-secret short\n", encoding="utf-8"
            )
            issues = sa.scan_repo_for_secrets(values, root=tmp, tracked_files=["a.txt"])
        self.assertEqual(issues, [])

    def test_walk_skips_local_env_binary_and_excluded_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "visible.txt").write_text(self.SECRET, encoding="utf-8")
            Path(tmp, ".env").write_text(self.SECRET, encoding="utf-8")
            Path(tmp, "image.png").write_bytes(b"\x89PNG\x00" + self.SECRET.encode())
            vendor = Path(tmp, ".venv")
            vendor.mkdir()
            (vendor / "lib.py").write_text(self.SECRET, encoding="utf-8")
            cache = Path(tmp, "__pycache__")
            cache.mkdir()
            (cache / "m.py").write_text(self.SECRET, encoding="utf-8")

            issues = sa.scan_repo_for_secrets(self._values(), root=tmp)

        labels = [i.message for i in issues]
        self.assertEqual(len(issues), 1)
        self.assertIn("visible.txt", labels[0])

    def test_project_cloned_under_excluded_name_is_still_scanned(self):
        """L'exclusion porte sur le chemin relatif, pas sur le chemin absolu.

        Sinon un projet cloné sous un dossier nommé `build`/`env`/`target`
        serait intégralement ignoré (faux négatif silencieux).
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "build" / "proj"
            root.mkdir(parents=True)
            (root / "leak.txt").write_text(self.SECRET, encoding="utf-8")
            issues = sa.scan_repo_for_secrets(
                self._values(), root=root, tracked_files=["leak.txt"]
            )
        self.assertEqual(len(issues), 1)
        self.assertIn("leak.txt", issues[0].message)

    def test_run_audit_with_scan_repo_fails_closed(self):
        values = strong_values()
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "leaked.env.txt").write_text(
                values["WEBHOOK_SECRET"], encoding="utf-8"
            )
            result = sa.run_audit(
                values,
                ledger=_fresh_ledger(values),
                now=NOW,
                scan_repo=True,
                repo_root=tmp,
                tracked_files=["leaked.env.txt"],
            )
        self.assertFalse(result.ok)
        self.assertTrue(any("fuite" in i.message for i in result.errors))

    def test_run_audit_without_scan_stays_clean(self):
        values = strong_values()
        result = sa.run_audit(values, ledger=_fresh_ledger(values), now=NOW)
        self.assertTrue(result.ok, result.as_dict())

    def test_cli_fails_when_repo_contains_a_secret(self):
        cli = self._load_cli()
        values = strong_values()
        saved = _fixture_environment(values)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "planted.txt").write_text(
                    values["INTERNAL_API_KEY"], encoding="utf-8"
                )
                ledger = str(Path(tmp) / "ledger.json")
                self.assertEqual(
                    cli.main(["--no-env-file", "--ledger", ledger, "--record"]), 0
                )
                self.assertEqual(
                    cli.main(["--no-env-file", "--ledger", ledger, "--scan-root", tmp]),
                    1,
                )
                self.assertEqual(
                    cli.main(
                        [
                            "--no-env-file",
                            "--ledger",
                            ledger,
                            "--scan-root",
                            tmp,
                            "--no-scan-repo",
                        ]
                    ),
                    0,
                )
        finally:
            _restore_environment(saved)

    def test_cli_report_names_the_scope_and_the_ledger_summary(self):
        """Le rapport humain doit se lire : périmètre du scan et registre, sans JSON."""
        cli = self._load_cli()
        values = strong_values()
        saved = _fixture_environment(values)
        captured = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                Path(tmp, "clean.txt").write_text("rien\n", encoding="utf-8")
                Path(tmp, ".env").write_text("X=1\n", encoding="utf-8")
                vendor = Path(tmp) / "node_modules"
                vendor.mkdir()
                (vendor / "dep.js").write_text("rien\n", encoding="utf-8")
                ledger = str(Path(tmp) / "ledger.json")
                self.assertEqual(
                    cli.main(["--no-env-file", "--ledger", ledger, "--record"]), 0
                )
                with redirect_stdout(captured):
                    code = cli.main(
                        [
                            "--no-env-file",
                            "--ledger",
                            ledger,
                            "--scan-root",
                            tmp,
                            "--scan-repo",
                        ]
                    )
        finally:
            _restore_environment(saved)

        text = captured.getvalue()
        self.assertEqual(code, 0, text)
        # Synthèse du registre, lisible sans ouvrir le JSON.
        self.assertIn("entrée(s)", text)
        # Périmètre du scan : ce qui a été lu, et ce qui a été écarté par politique.
        self.assertIn("fichier(s) lu(s)", text)
        self.assertIn("écarté(s) par politique", text)
        self.assertIn(".env", text)
        self.assertIn("node_modules", text)

    def _load_cli(self):
        spec = importlib.util.spec_from_file_location(
            "verify_secrets", REPO_ROOT / "scripts" / "verify_secrets.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module


class TestRotationProcedureDoc(unittest.TestCase):
    """La procédure de rotation est vérifiable, pas décorative.

    Deux dérives sont silencieuses : une commande citée par la doc qui ne produit
    plus une valeur acceptée par l'audit (le lecteur qui la suit n'atteint jamais
    un déploiement vert), et un secret requis que la procédure oublie. La première
    ne se voit qu'en **exécutant** la commande — c'est ce que fait ce test.
    """

    DOC = REPO_ROOT / "docs" / "SECRETS.md"

    def test_generation_commands_produce_secrets_the_audit_accepts(self):
        """Les commandes de la doc, exécutées telles quelles, passent la barrière."""
        import secrets as stdlib_secrets

        from cryptography.fernet import Fernet

        values = {
            # les trois que la procédure fait générer localement
            "WEBHOOK_SECRET": stdlib_secrets.token_urlsafe(32),
            "INTERNAL_API_KEY": stdlib_secrets.token_urlsafe(32),
            "ENCRYPTION_KEY": Fernet.generate_key().decode(),
            # les deux qui viennent de services externes : formes attendues
            "SUPABASE_SERVICE_KEY": "sb_secret_" + stdlib_secrets.token_urlsafe(32),
            "TELEGRAM_BOT_TOKEN": "8123456789:" + stdlib_secrets.token_urlsafe(30),
        }

        errors = [issue for issue in sa.check_presence_and_strength(values) if issue.is_error]
        self.assertEqual(errors, [], [issue.message for issue in errors])
        self.assertEqual(sa.check_distinctness(values), [])
        self.assertTrue(sa.is_valid_fernet_key(values["ENCRYPTION_KEY"]))

    def test_the_procedure_names_every_required_secret(self):
        """Un secret requis ajouté au code doit apparaître dans la procédure."""
        text = self.DOC.read_text(encoding="utf-8")

        required = [spec.name for spec in sa.DEFAULT_SPECS if spec.required]

        self.assertEqual(len(required), 5, "la procédure est écrite pour cinq secrets")
        missing = [name for name in required if name not in text]
        self.assertEqual(missing, [], f"secret(s) requis absent(s) de la doc : {missing}")

    def test_the_procedure_says_what_a_fernet_rotation_costs(self):
        """Ce que la rotation d'ENCRYPTION_KEY casse doit être écrit, pas supposé."""
        text = self.DOC.read_text(encoding="utf-8")

        self.assertIn("user_broker_credentials", text)
        self.assertIn("--record", text)
        self.assertIn("--allow-missing-rotation", text)

    def test_the_procedure_says_how_the_production_is_checked(self):
        """Un `0` local ne décrit que cette machine : la procédure doit le dire.

        La barrière sait comparer ses empreintes à celles du processus déployé.
        Si la procédure n'en parle pas, l'opérateur croira que son `0` prouve la
        production — le feu vert trompeur qu'on vient justement de supprimer.
        """
        text = self.DOC.read_text(encoding="utf-8")

        self.assertIn(sa.DEPLOYED_FINGERPRINTS_PATH, text)
        self.assertIn("--require-remote", text)
        self.assertIn(sa.DEPLOYED_URL_ENV, text)


class TestKeyRingAudit(unittest.TestCase):
    """L'anneau de clés est vérifié comme un secret : entrée par entrée, et par version.

    L'enjeu n'est pas la propreté : une clé retirée mal recopiée, c'est du chiffré
    historique qui ne se rouvrira plus — et qui ne le dira qu'en refusant un ordre.
    Le contrôle doit donc échouer **au déploiement**, là où quelqu'un peut encore
    corriger, et nommer l'entrée fautive sans jamais recopier la clé.
    """

    def _values(self, ring: str, primary: str = "") -> dict:
        values = strong_values()
        values["ENCRYPTION_KEYS_PREVIOUS"] = ring
        if primary:
            values["ENCRYPTION_KEY"] = primary
        return values

    def test_a_valid_ring_is_accepted_and_controlled(self):
        values = self._values(f"v1:{_key(40)}", primary=f"v2:{_key(80)}")

        result = sa.run_audit(values, ledger=_fresh_ledger(values), now=NOW)

        self.assertTrue(result.ok, result.as_dict())
        self.assertIn(sa.RING_SECRET_NAME, result.checked)

    def test_a_ring_may_hold_several_retired_keys(self):
        values = self._values(f"v1:{_key(1)},v2:{_key(2)}", primary=f"v3:{_key(3)}")

        self.assertEqual(sa.check_key_ring(values), [])

    def test_a_malformed_entry_is_named_by_position_without_the_key(self):
        values = self._values(f"v1:{_key(1)},pas-une-cle")

        issues = sa.check_key_ring(values)

        self.assertEqual([i.name for i in issues], [sa.RING_SECRET_NAME] * 2)
        self.assertTrue(all(i.is_error for i in issues))
        self.assertIn("entrée 2", issues[0].message)
        self.assertNotIn(_key(1), "".join(i.message for i in issues))

    def test_a_bad_version_prefix_is_reported_as_such(self):
        values = self._values(f"deux:{_key(1)}")

        issues = sa.check_key_ring(values)

        self.assertIn("préfixe de version", issues[0].message)

    def test_two_entries_with_the_same_version_are_refused(self):
        values = self._values(f"v1:{_key(1)},v1:{_key(2)}")

        issues = sa.check_key_ring(values)

        self.assertTrue(any("v1 écrite deux fois" in i.message for i in issues), issues)

    def test_an_entry_repeating_the_active_key_is_refused(self):
        """La recopier ne rouvre rien de plus et prolonge un secret à retirer."""
        values = self._values(f"v9:{_key(7)}", primary=_key(7))

        issues = sa.check_key_ring(values)

        self.assertTrue(any("recopie ENCRYPTION_KEY" in i.message for i in issues), issues)

    def test_an_entry_claiming_the_active_version_is_refused(self):
        values = self._values(f"v2:{_key(11)}", primary=f"v2:{_key(12)}")

        issues = sa.check_key_ring(values)

        self.assertTrue(any("indécidable" in i.message for i in issues), issues)

    def test_the_active_key_accepts_a_version_prefix(self):
        values = strong_values()
        values["ENCRYPTION_KEY"] = f"v2:{_key(5)}"

        result = sa.run_audit(values, ledger=_fresh_ledger(values), now=NOW)

        self.assertTrue(result.ok, result.as_dict())

    def test_an_invalid_version_prefix_on_the_active_key_is_an_error(self):
        values = strong_values()
        values["ENCRYPTION_KEY"] = f"vX:{_key(5)}"

        result = sa.run_audit(values, ledger=_fresh_ledger(values), now=NOW)

        self.assertFalse(result.ok)
        self.assertTrue(any(i.name == "ENCRYPTION_KEY" for i in result.errors))

    def test_each_retired_key_is_searched_as_a_live_secret(self):
        """Une clé retirée rouvre encore l'ancien chiffré : elle doit être traquée."""
        values = self._values(f"v1:{_key(3)}", primary=f"v2:{_key(4)}")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "fuite.md").write_text(f"clé d'avant : {_key(3)}", encoding="utf-8")
            issues = sa.scan_repo_for_secrets(values, root=tmp, tracked_files=["fuite.md"])

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].name, sa.RING_SECRET_NAME)
        self.assertNotIn(_key(3), issues[0].message, "le rapport de fuite masque la valeur")

    def test_retiring_a_key_asks_for_a_recorded_rotation(self):
        """L'anneau est un secret : le raccourcir est une rotation, et se date."""
        before = self._values(f"v1:{_key(1)},v2:{_key(2)}", primary=f"v3:{_key(3)}")
        ledger = _fresh_ledger(before)
        after = self._values(f"v2:{_key(2)}", primary=f"v3:{_key(3)}")

        result = sa.run_audit(after, ledger=ledger, now=NOW)

        self.assertFalse(result.ok)
        self.assertTrue(
            any(i.name == sa.RING_SECRET_NAME and "sans rotation enregistrée" in i.message
                for i in result.errors),
            result.as_dict(),
        )


if __name__ == "__main__":
    unittest.main()
