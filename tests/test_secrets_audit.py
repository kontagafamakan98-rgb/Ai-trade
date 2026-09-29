import base64
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

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
        saved = {k: os.environ.get(k) for k in values}
        os.environ.update(values)
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
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

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
        saved = {k: os.environ.get(k) for k in values}
        os.environ.update(values)
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
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

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
