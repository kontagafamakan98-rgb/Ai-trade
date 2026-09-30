"""Ce que `scripts/generate_secrets.py` doit garantir.

Trois propriétés, chacune payée par un défaut qu'on ne voit pas à l'œil nu :

* une valeur générée ne sort **jamais** — ni sur stdout, ni sur stderr, ni dans le
  JSON ; un secret affiché vit ensuite dans un scrollback, un journal de CI ou une
  capture d'écran, et il faut le tourner ;
* une valeur **existante** n'est jamais écrasée : `ENCRYPTION_KEY` fait perdre les
  identifiants broker déjà chiffrés en base, et rien ne la régénère ; et ce qui
  n'est pas visé est rendu intact, commentaires compris (c'est un fichier que
  l'humain documente) ;
* `--check` n'écrit rien — le mode qui répond ne doit pas modifier.

Le fichier `.env` de ce dépôt n'est jamais touché : chaque test travaille dans un
répertoire temporaire, avec un chemin **absolu**. La barrière qui écrit vraiment
est `scripts/generate_secrets.py` lui-même, et elle ne s'exécute que là où on la
lance.
"""
from __future__ import annotations

import base64
import contextlib
import datetime
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest

from core import secrets_audit as sa
from utils import encryption as enc

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Les valeurs générables par le script : ce sont **elles** qu'on cherche à ne
#: jamais voir dans une sortie.
GENERATED_NAMES = ("WEBHOOK_SECRET", "INTERNAL_API_KEY", "ENCRYPTION_KEY")

#: Les deux qui viennent d'un service externe : le script les nomme, il ne les invente pas.
MANUAL_NAMES = ("SUPABASE_SERVICE_KEY", "TELEGRAM_BOT_TOKEN")


def _load_cli():
    """Le script, importé par son chemin (comme le font les autres tests de `scripts/`).

    Il doit être **enregistré dans `sys.modules` avant** d'être exécuté : `dataclasses`
    y résout le module de la classe pour lire ses annotations différées, et un module
    chargé hors de `sys.modules` le fait lever sur `cls.__module__` (Python 3.14) — le
    script, lui, s'importe normalement quand on le lance.
    """
    spec = importlib.util.spec_from_file_location(
        "generate_secrets", REPO_ROOT / "scripts" / "generate_secrets.py"
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


def _strong_values() -> dict:
    """Un jeu *valide* et factice (aucune valeur réelle du dépôt)."""
    return {
        "WEBHOOK_SECRET": "whsec_9f3a2c7b1d4e6f8a",
        "INTERNAL_API_KEY": "intkey_5b8e2d1c9a7f4e3b",
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(bytes(range(32))).decode("ascii"),
        "SUPABASE_SERVICE_KEY": "sb_secret_a1b2c3d4e5f6a1b2c3d4e5f6",
        "TELEGRAM_BOT_TOKEN": "8123456789:Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_Ab1_",
    }


def _manual_values() -> dict:
    return {k: v for k, v in _strong_values().items() if k in MANUAL_NAMES}


def _env_text(values: dict) -> str:
    return "".join(f'{name}="{value}"\n' for name, value in values.items())


def _today() -> str:
    """La date que le script écrit dans le registre (ISO, jour local)."""
    return datetime.date.today().isoformat()


def _run(cli, argv):
    """Exécute le script en capturant **les deux** flux : un secret peut sortir partout."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.cli = _load_cli()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = pathlib.Path(self._tmp.name)

    @property
    def env_file(self) -> pathlib.Path:
        return self.tmp / ".env"

    def _run(self, *argv):
        return _run(self.cli, ["--env-file", str(self.env_file), *argv])


class CheckModeTest(CliTest):
    """`--check` répond et ne touche à rien."""

    def test_it_names_every_missing_secret_and_creates_no_file(self):
        code, out, err = self._run("--check")

        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        for name in (*GENERATED_NAMES, *MANUAL_NAMES):
            self.assertIn(name, out)
        self.assertFalse(self.env_file.exists(), "--check ne doit pas créer le fichier")

    def test_an_empty_assignment_counts_as_missing(self):
        self.env_file.write_text(
            'WEBHOOK_SECRET=""\nSUPABASE_SERVICE_KEY=""\n', encoding="utf-8"
        )

        code, out, _err = self._run("--check")

        self.assertEqual(code, 1)
        self.assertIn("WEBHOOK_SECRET", out)
        self.assertIn("vide dans le fichier", out, "une ligne vide n'est pas une ligne absente")
        self.assertIn("absent du fichier", out, "TELEGRAM_BOT_TOKEN, lui, est absent")

    def test_an_example_left_in_place_counts_as_missing(self):
        self.env_file.write_text(
            'WEBHOOK_SECRET="change-me-super-secret"\n', encoding="utf-8"
        )

        code, out, _err = self._run("--check")

        self.assertEqual(code, 1)
        self.assertIn("WEBHOOK_SECRET", out)

    def test_a_complete_file_reports_nothing_missing_and_names_no_secret(self):
        self.env_file.write_text(_env_text(_strong_values()), encoding="utf-8")

        code, out, _err = self._run("--check")

        self.assertEqual(code, 0)
        self.assertIn("ne manque", out)
        for name in (*GENERATED_NAMES, *MANUAL_NAMES):
            self.assertNotIn(name, out, "un secret présent n'a rien à faire dans ce rapport")

    def test_a_present_but_unusable_value_is_reported_without_being_called_missing(self):
        """Une clé Fernet tronquée n'est pas « manquante » — et reste intouchée."""
        values = _strong_values()
        values["ENCRYPTION_KEY"] = "tronquee"
        self.env_file.write_text(_env_text(values), encoding="utf-8")

        code, out, _err = self._run("--check")

        self.assertEqual(code, 1)
        self.assertIn("ENCRYPTION_KEY", out)
        self.assertIn("Aucun des 5 secrets requis ne manque", out)
        self.assertIn(values["ENCRYPTION_KEY"], self.env_file.read_text(encoding="utf-8"))

    def test_check_is_read_only_even_when_values_are_examples(self):
        before = _env_text({"WEBHOOK_SECRET": "change-me", "SUPABASE_URL": "https://x.supabase.co"})
        self.env_file.write_text(before, encoding="utf-8")

        self._run("--check")

        self.assertEqual(self.env_file.read_text(encoding="utf-8"), before)


class GenerationTest(CliTest):
    """Le mode par défaut remplit ce qui manque, et rien d'autre."""

    def test_the_values_written_are_never_printed(self):
        code, out, err = self._run()

        self.assertEqual(code, 1, "les deux secrets à renseigner à la main manquent encore")
        written = sa.parse_env_file(self.env_file)
        for name in GENERATED_NAMES:
            self.assertTrue(written.get(name), f"{name} aurait dû être écrit")
            self.assertNotIn(written[name], out, "la valeur ne doit pas passer par l'écran")
            self.assertNotIn(written[name], err)
        self.assertIn("empreinte", out)
        for name in MANUAL_NAMES:
            self.assertNotIn(name, written, "le script ne fabrique pas un jeton externe")

    def test_the_json_output_carries_no_value_either(self):
        code, out, _err = self._run("--json")

        self.assertEqual(code, 1)
        payload = json.loads(out)
        written = sa.parse_env_file(self.env_file)
        self.assertEqual(
            sorted(entry["name"] for entry in payload["generated"]), sorted(GENERATED_NAMES)
        )
        self.assertEqual(
            payload["to_generate"], [], "ce qui vient d'être écrit n'est plus « à générer »"
        )
        for name in GENERATED_NAMES:
            self.assertNotIn(written[name], out)
            self.assertEqual(
                payload["missing_manual"].get(name), None, "le manuel n'est pas du généré"
            )

    def test_what_it_writes_passes_the_pre_deploy_audit(self):
        self._run()

        written = sa.parse_env_file(self.env_file)
        problems = [
            issue
            for issue in sa.check_presence_and_strength(written)
            if issue.name in GENERATED_NAMES
        ]
        self.assertEqual([i.message for i in problems], [])
        self.assertEqual(sa.check_distinctness(written), [])
        self.assertTrue(sa.is_valid_fernet_key(written["ENCRYPTION_KEY"]))

    def test_two_runs_leave_the_file_byte_for_byte_identical(self):
        self._run()
        first = self.env_file.read_bytes()

        code, out, _err = self._run()

        self.assertEqual(code, 1)
        self.assertEqual(self.env_file.read_bytes(), first)
        self.assertIn("Rien à générer", out)

    def test_a_new_encryption_key_starts_above_the_key_ring(self):
        """Tourner = perdre la clé active ; une clé nue entrerait en collision avec v1."""
        previous = _strong_values()["ENCRYPTION_KEY"]
        self.env_file.write_text(
            f'ENCRYPTION_KEYS_PREVIOUS="v1:{previous}"\n', encoding="utf-8"
        )

        self._run()

        written = sa.parse_env_file(self.env_file)
        self.assertTrue(written["ENCRYPTION_KEY"].startswith("v2:"))
        self.assertTrue(sa.is_valid_key_entry(written["ENCRYPTION_KEY"]))
        self.assertEqual(sa.check_key_ring(written), [], "l'anneau reste cohérent")

    def test_the_report_names_the_active_key_version(self):
        """L'anneau est ce qui décide de la version : elle doit se lire dans le rapport."""
        code, out, _err = self._run()

        self.assertEqual(code, 1)
        self.assertIn("ENCRYPTION_KEY active : v1", out)

    def test_running_on_a_missing_file_creates_it_with_a_trailing_newline(self):
        self._run()

        raw = self.env_file.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(self.env_file.read_text(encoding="utf-8").count("ENCRYPTION_KEY"), 1)


class ExistingContentTest(CliTest):
    """Un `.env` n'est pas qu'une liste de secrets : ce qu'il dit doit survivre."""

    def test_existing_values_comments_and_settings_are_left_untouched(self):
        values = _strong_values()
        before = (
            "# Où la trouver : tableau de bord Supabase → Project Settings → API\n"
            f'WEBHOOK_SECRET="{values["WEBHOOK_SECRET"]}"\n'
            f'ENCRYPTION_KEY="{values["ENCRYPTION_KEY"]}"\n'
            'SUPABASE_URL="https://abcdefgh.supabase.co"\n'
            "\n"
            "# réglages\n"
            'PAPER_TRADING="true"\n'
        )
        self.env_file.write_text(before, encoding="utf-8")

        code, out, _err = self._run()

        after = self.env_file.read_text(encoding="utf-8")
        self.assertEqual(code, 1, "les deux secrets externes manquent toujours")
        for text in (
            "# Où la trouver",
            'SUPABASE_URL="https://abcdefgh.supabase.co"',
            'PAPER_TRADING="true"',
            f'WEBHOOK_SECRET="{values["WEBHOOK_SECRET"]}"',
            f'ENCRYPTION_KEY="{values["ENCRYPTION_KEY"]}"',
        ):
            self.assertIn(text, after, "le fichier existant doit être rendu tel quel")
        self.assertIn("INTERNAL_API_KEY", after, "le seul manquant générable est ajouté")
        self.assertIn("Déjà en place et inchangé(s)", out)

    def test_an_example_is_replaced_in_place_by_a_value_the_audit_accepts(self):
        self.env_file.write_text(
            "# clé de signature\n"
            'WEBHOOK_SECRET="change-me-super-secret"\n'
            'INTERNAL_API_KEY="change-me-internal-api-key"\n',
            encoding="utf-8",
        )

        self._run()

        after = self.env_file.read_text(encoding="utf-8")
        self.assertTrue(after.startswith("# clé de signature\n"))
        self.assertNotIn("change-me", after)
        written = sa.parse_env_file(self.env_file)
        errors = [
            issue.message
            for issue in sa.check_presence_and_strength(written)
            if issue.is_error and issue.name in written
        ]
        self.assertEqual(errors, [])

    def test_a_weak_but_real_value_is_reported_and_never_overwritten(self):
        self.env_file.write_text(
            'INTERNAL_API_KEY="abc"\n' f'WEBHOOK_SECRET="{_strong_values()["WEBHOOK_SECRET"]}"\n',
            encoding="utf-8",
        )

        code, out, _err = self._run()

        self.assertEqual(code, 1)
        after = self.env_file.read_text(encoding="utf-8")
        self.assertIn('INTERNAL_API_KEY="abc"', after, "un secret réel ne s'écrase pas")
        self.assertIn("INTERNAL_API_KEY", out)
        self.assertIn("inutilisable", out)

    def test_the_line_ending_style_of_the_file_is_preserved(self):
        self.env_file.write_bytes(b'WEBHOOK_SECRET="change-me"\r\n')

        self._run()

        raw = self.env_file.read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertEqual(
            raw.count(b"\n"), raw.count(b"\r\n"), "aucun LF orphelin dans un fichier CRLF"
        )

    def test_a_duplicated_key_is_refused_rather_than_guessed(self):
        before = 'WEBHOOK_SECRET="change-me"\nWEBHOOK_SECRET="change-me-again"\n'
        self.env_file.write_text(before, encoding="utf-8")

        code, _out, err = self._run()

        self.assertEqual(code, 2)
        self.assertIn("plusieurs fois", err)
        self.assertEqual(self.env_file.read_text(encoding="utf-8"), before)


class TargetSafetyTest(CliTest):
    """Les secrets ne s'écrivent que dans un fichier qui ne part pas dans un commit."""

    def test_it_refuses_a_template_file(self):
        target = self.tmp / ".env.example"

        code, _out, err = _run(self.cli, ["--env-file", str(target)])

        self.assertEqual(code, 2)
        self.assertIn(".env.example", err)
        self.assertFalse(target.exists(), "un fichier suivi ne doit jamais recevoir un secret")

    def test_it_refuses_a_file_that_is_not_a_dotenv(self):
        target = self.tmp / "secrets.txt"

        code, _out, err = _run(self.cli, ["--env-file", str(target)])

        self.assertEqual(code, 2)
        self.assertFalse(target.exists())
        self.assertIn("n'est pas un fichier d'environnement local", err)

    def test_a_relative_path_is_resolved_from_the_project_root(self):
        """Sinon le `.env` atterrit dans le répertoire d'où l'on a lancé le script.

        Le chemin est lu par la fonction qui le résout plutôt qu'écrit pour de bon :
        un cas réel viserait le `.env` du dépôt, que ces tests ne touchent jamais.
        """
        resolved = self.cli._resolve_env_file(".env")

        self.assertEqual(resolved, REPO_ROOT / ".env")


class RotationCliTest(CliTest):
    """Comme `CliTest`, mais avec un registre de rotation en répertoire temporaire.

    Le défaut du script est le registre **du dépôt** (`security/secret_rotation.json`) :
    un test qui l'écrirait ferait dire à la barrière que les secrets de cette machine
    ont été tournés aujourd'hui. Ces tests ne touchent donc jamais l'état réel du
    dépôt, et c'est aussi la raison pour laquelle `--ledger` existe.
    """

    @property
    def ledger(self) -> pathlib.Path:
        return self.tmp / "ledger.json"

    def _run(self, *argv):
        return _run(
            self.cli,
            ["--env-file", str(self.env_file), "--ledger", str(self.ledger), *argv],
        )

    def _write(self, values=None):
        """Un `.env` complet : la rotation doit pouvoir être jugée par l'audit."""
        self.env_file.write_text(_env_text(values or _strong_values()), encoding="utf-8")


class RotationAnnouncementTest(RotationCliTest):
    """L'annonce dit ce que la rotation casse — et n'écrit rien."""

    def test_the_catalogue_says_what_every_rotation_breaks(self):
        self._write()
        before = self.env_file.read_bytes()

        code, out, _err = self._run("--rotate")

        self.assertEqual(code, 0)
        self.assertEqual(self.env_file.read_bytes(), before, "le catalogue n'écrit rien")
        for spec in sa.DEFAULT_SPECS:
            with self.subTest(secret=spec.name):
                self.assertIn(spec.name, out)
                self.assertIn(sa.ROTATION_IMPACT[spec.name], out, "la conséquence vient de l'audit")
        self.assertIn("Se tournent ici", out)

    def test_the_catalogue_tells_empty_from_absent_like_the_barrier(self):
        """« vide » et « absent » demandent le même geste, pas la même recherche.

        Le catalogue doit donc les distinguer *exactement* comme `--check` :
        une ligne présente et vide n'est pas une ligne absente, et l'uniformiser
        enverrait relire un fichier qu'on connaît déjà.
        """
        self._write({"WEBHOOK_SECRET": _strong_values()["WEBHOOK_SECRET"], "SUPABASE_SERVICE_KEY": ""})

        code, out, _err = self._run("--rotate")

        def catalogue_line(name):
            prefix = f"   • {name} —"
            return next(line for line in out.splitlines() if line.startswith(prefix))

        self.assertEqual(code, 0)
        self.assertIn("vide dans le fichier", catalogue_line("SUPABASE_SERVICE_KEY"))
        self.assertIn("absent du fichier", catalogue_line("TELEGRAM_BOT_TOKEN"))

        _code, check_out, _check_err = self._run("--check")
        self.assertIn("vide dans le fichier", check_out, "le catalogue ne doit pas diverger")


    def test_no_name_lists_the_impact_of_every_secret(self):
        """Un secret ajouté sans conséquence écrite ne peut pas tourner à l'aveugle."""
        self.assertEqual(
            set(sa.ROTATION_IMPACT), {spec.name for spec in sa.DEFAULT_SPECS}
        )

    def test_the_announcement_shows_the_cost_and_writes_nothing(self):
        values = _strong_values()
        self._write(values)
        before = self.env_file.read_bytes()

        code, out, err = self._run("--rotate", "WEBHOOK_SECRET")

        self.assertEqual(code, 0)
        self.assertEqual(self.env_file.read_bytes(), before, "l'annonce n'écrit rien")
        self.assertIn(sa.ROTATION_IMPACT["WEBHOOK_SECRET"], out)
        self.assertIn("Rien n'a été écrit", out)
        self.assertIn("--apply", out)
        self.assertIn("--remote", out, "la suite de la procédure est rappelée")
        self.assertNotIn(values["WEBHOOK_SECRET"], out + err, "la valeur ne s'affiche jamais")
        self.assertNotIn("Rotation appliquée", out)

    def test_the_announcement_of_a_fernet_rotation_names_the_ring_destination(self):
        self._write()

        _code, out, _err = self._run("--rotate", "ENCRYPTION_KEY")

        self.assertIn("ENCRYPTION_KEYS_PREVIOUS", out)
        self.assertIn("la nouvelle prend v2", out)
        self.assertIn("rotate_encryption_key.py", out)

    def test_the_ledger_default_is_the_repository_one(self):
        """La rotation et la barrière doivent viser le **même** registre."""
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"SECRET_ROTATION_LEDGER": ""}):
            resolved = self.cli._resolve_ledger_path(None)

        self.assertEqual(resolved, REPO_ROOT / "security" / "secret_rotation.json")


class RotationApplyTest(RotationCliTest):
    """La rotation écrit le seul secret visé, et le registre suit."""

    def test_it_replaces_only_the_designated_secret(self):
        values = _strong_values()
        self._write(values)

        code, out, err = self._run("--rotate", "WEBHOOK_SECRET", "--apply")

        self.assertEqual(code, 0)
        after = sa.parse_env_file(self.env_file)
        self.assertNotEqual(after["WEBHOOK_SECRET"], values["WEBHOOK_SECRET"])
        for name, value in values.items():
            if name != "WEBHOOK_SECRET":
                self.assertEqual(after[name], value, f"{name} ne devait pas bouger")
        for text in (after["WEBHOOK_SECRET"], values["WEBHOOK_SECRET"]):
            self.assertNotIn(text, out + err)
        self.assertIn("Rotation appliquée", out)
        self.assertIn("Registre horodaté", out)

    def test_the_ledger_has_followed_and_the_barrier_is_satisfied(self):
        values = _strong_values()
        self._write(values)
        sa.save_ledger(self.ledger, {"version": 1, "secrets": sa.build_ledger_entries(values)})

        self._run("--rotate", "INTERNAL_API_KEY", "--apply")

        after = sa.parse_env_file(self.env_file)
        ledger = sa.load_ledger(self.ledger)
        issues, _report = sa.check_rotation(after, ledger)
        self.assertEqual([issue.message for issue in issues], [])
        combined = (
            sa.check_presence_and_strength(after)
            + sa.check_distinctness(after)
            + sa.check_key_ring(after)
        )
        self.assertEqual([issue.message for issue in combined], [])
        self.assertEqual(
            ledger["secrets"]["INTERNAL_API_KEY"]["fingerprint"],
            sa.secret_fingerprint("INTERNAL_API_KEY", after["INTERNAL_API_KEY"]),
        )

    def test_a_rotation_left_unrecorded_is_refused_by_the_barrier(self):
        """`--no-record` n'est pas un oubli silencieux : l'audit le voit et refuse."""
        values = _strong_values()
        self._write(values)
        sa.save_ledger(self.ledger, {"version": 1, "secrets": sa.build_ledger_entries(values)})

        code, out, _err = self._run("--rotate", "WEBHOOK_SECRET", "--apply", "--no-record")

        self.assertEqual(code, 0)
        self.assertIn("NON horodaté", out)
        issues, _report = sa.check_rotation(sa.parse_env_file(self.env_file), sa.load_ledger(self.ledger))
        messages = {issue.name: issue.message for issue in issues if issue.is_error}
        self.assertIn("sans rotation enregistrée", messages.get("WEBHOOK_SECRET", ""))
        self.assertNotIn("INTERNAL_API_KEY", messages, "les autres n'ont pas bougé")

    def test_the_json_output_carries_no_value_either(self):
        values = _strong_values()
        self._write(values)

        code, out, _err = self._run("--rotate", "ENCRYPTION_KEY", "--apply", "--json")

        self.assertEqual(code, 0)
        rotation = json.loads(out)["rotation"]
        self.assertTrue(rotation["applied"])
        self.assertEqual(rotation["version"], 2)
        self.assertEqual(rotation["previous_version"], 1)
        self.assertEqual(rotation["recorded"], ["ENCRYPTION_KEY", "ENCRYPTION_KEYS_PREVIOUS"])
        after = sa.parse_env_file(self.env_file)
        for value in (
            values["ENCRYPTION_KEY"],
            after["ENCRYPTION_KEY"],
            after["ENCRYPTION_KEYS_PREVIOUS"],
        ):
            self.assertNotIn(value, out)


class FernetRotationTest(RotationCliTest):
    """Tourner `ENCRYPTION_KEY` est une affaire d'anneau, pas de remplacement sec."""

    def test_the_outgoing_key_lands_in_the_ring(self):
        values = _strong_values()
        self._write(values)
        old = values["ENCRYPTION_KEY"]

        code, out, err = self._run("--rotate", "ENCRYPTION_KEY", "--apply")

        self.assertEqual(code, 0)
        after = sa.parse_env_file(self.env_file)
        self.assertTrue(after["ENCRYPTION_KEY"].startswith("v2:"))
        self.assertEqual(after["ENCRYPTION_KEYS_PREVIOUS"], f"v1:{old}")
        self.assertEqual(sa.check_key_ring(after), [])
        self.assertEqual([issue.message for issue in sa.check_presence_and_strength(after)], [])
        for text in (old, after["ENCRYPTION_KEY"]):
            self.assertNotIn(text, out + err)

    def test_a_second_rotation_takes_the_next_version_and_keeps_the_older_entries(self):
        self._write()

        self._run("--rotate", "ENCRYPTION_KEY", "--apply")
        first = sa.parse_env_file(self.env_file)
        self._run("--rotate", "ENCRYPTION_KEY", "--apply")
        second = sa.parse_env_file(self.env_file)

        self.assertTrue(second["ENCRYPTION_KEY"].startswith("v3:"))
        entries = sa.ring_entries(second["ENCRYPTION_KEYS_PREVIOUS"])
        self.assertEqual([sa.parse_key_entry(entry)[0] for entry in entries], [2, 1])
        self.assertIn(first["ENCRYPTION_KEY"].split(":", 1)[1], second["ENCRYPTION_KEYS_PREVIOUS"])
        self.assertEqual(sa.check_key_ring(second), [])

    def test_rotating_an_absent_key_is_a_setup_and_leaves_the_ring_empty(self):
        values = _strong_values()
        values.pop("ENCRYPTION_KEY")
        self._write(values)

        code, out, _err = self._run("--rotate", "ENCRYPTION_KEY", "--apply")

        self.assertEqual(code, 0)
        after = sa.parse_env_file(self.env_file)
        # Pas de préfixe de version, et non « ne commence pas par v » : une clé
        # Fernet peut légitimement commencer par « v » (~1,5 % d'entre elles),
        # donc ce n'est pas un signe de version. Le seul signe, c'est le « : ».
        self.assertIsNone(enc.token_version(after["ENCRYPTION_KEY"]))
        self.assertNotIn(":", after["ENCRYPTION_KEY"])
        self.assertTrue(sa.is_valid_fernet_key(after["ENCRYPTION_KEY"]))
        self.assertNotIn("ENCRYPTION_KEYS_PREVIOUS", after, "un anneau vide ne se fabrique pas")
        self.assertIn("mise en place", out)

    def test_an_unusable_current_key_is_refused_rather_than_copied(self):
        """Recopier une clé illisible dans l'anneau ferait refuser l'audit ensuite."""
        values = _strong_values()
        values["ENCRYPTION_KEY"] = "tronquee"
        self._write(values)
        before = self.env_file.read_bytes()

        code, out, _err = self._run("--rotate", "ENCRYPTION_KEY", "--apply")

        self.assertEqual(code, 1)
        self.assertEqual(self.env_file.read_bytes(), before)
        self.assertIn("rotation refusée", out)
        self.assertFalse(self.ledger.exists(), "rien à horodater")

    def test_the_new_version_skips_the_ones_the_ring_already_holds(self):
        """Reprendre une version déjà prise rendrait l'ouverture indécidable."""
        values = _strong_values()
        older = base64.urlsafe_b64encode(bytes(range(5, 37))).decode("ascii")
        values["ENCRYPTION_KEYS_PREVIOUS"] = f"v5:{older}"
        self._write(values)

        code, _out, _err = self._run("--rotate", "ENCRYPTION_KEY", "--apply")

        self.assertEqual(code, 0)
        after = sa.parse_env_file(self.env_file)
        self.assertTrue(after["ENCRYPTION_KEY"].startswith("v6:"), after["ENCRYPTION_KEY"][:3])
        versions = [
            sa.parse_key_entry(entry)[0]
            for entry in sa.ring_entries(after["ENCRYPTION_KEYS_PREVIOUS"])
        ]
        self.assertEqual(versions, [1, 5], "l'ancienne clé entre, les autres restent")
        self.assertEqual(sa.check_key_ring(after), [])

    def test_a_ring_that_already_holds_the_active_version_is_refused(self):
        values = _strong_values()
        other = base64.urlsafe_b64encode(bytes(range(1, 33))).decode("ascii")
        values["ENCRYPTION_KEYS_PREVIOUS"] = f"v1:{other}"
        self._write(values)
        before = self.env_file.read_bytes()

        code, out, _err = self._run("--rotate", "ENCRYPTION_KEY", "--apply")

        self.assertEqual(code, 1)
        self.assertEqual(self.env_file.read_bytes(), before)
        self.assertIn("déjà dans ENCRYPTION_KEYS_PREVIOUS", out)
        self.assertIn("indécidable", out)


class RotationRefusalTest(RotationCliTest):
    """Ce qui ne se tourne pas ici le dit, avec sa source et sa sortie de secours."""

    def test_a_secret_that_comes_from_elsewhere_is_refused_with_its_source(self):
        self._write()
        before = self.env_file.read_bytes()

        code, out, err = self._run("--rotate", "SUPABASE_SERVICE_KEY", "--apply")

        self.assertEqual(code, 1)
        self.assertEqual(self.env_file.read_bytes(), before)
        self.assertIn("tableau de bord Supabase", out)
        self.assertIn(sa.ROTATION_IMPACT["SUPABASE_SERVICE_KEY"], out)
        self.assertIn("--record-only SUPABASE_SERVICE_KEY", out)
        self.assertNotIn(sa.parse_env_file(self.env_file)["SUPABASE_SERVICE_KEY"], out + err)

    def test_the_ring_itself_is_not_rotatable(self):
        self._write()

        code, out, _err = self._run("--rotate", "ENCRYPTION_KEYS_PREVIOUS", "--apply")

        self.assertEqual(code, 1)
        self.assertIn("--rotate ENCRYPTION_KEY --apply", out)

    def test_an_unknown_name_is_an_invocation_error_that_lists_what_exists(self):
        self._write()

        code, _out, err = self._run("--rotate", "WEBHOOK_SECRETT", "--apply")

        self.assertEqual(code, 2)
        self.assertIn("WEBHOOK_SECRETT", err)
        for spec in sa.DEFAULT_SPECS:
            self.assertIn(spec.name, err)

    def test_apply_without_a_rotation_is_an_invocation_error(self):
        self._write()

        code, _out, err = self._run("--apply")

        self.assertEqual(code, 2)
        self.assertIn("--apply", err)

    def test_the_contradictory_combinations_are_refused(self):
        self._write()
        for argv in (
            ("--record-only", "WEBHOOK_SECRET", "--no-record"),
            ("--rotate", "WEBHOOK_SECRET", "--record-only", "WEBHOOK_SECRET"),
            ("--check", "--rotate", "WEBHOOK_SECRET"),
            ("--record-only", ""),
        ):
            with self.subTest(argv=" ".join(argv)):
                code, _out, _err = self._run(*argv)
                self.assertEqual(code, 2)


class RecordOnlyTest(RotationCliTest):
    """Horodater un secret sans le toucher : le cas des valeurs qui viennent d'ailleurs."""

    def test_it_stamps_only_the_named_secret(self):
        values = _strong_values()
        self._write(values)
        sa.save_ledger(
            self.ledger,
            {
                "version": 1,
                "secrets": {
                    "WEBHOOK_SECRET": {
                        "fingerprint": sa.secret_fingerprint(
                            "WEBHOOK_SECRET", values["WEBHOOK_SECRET"]
                        ),
                        "rotated_at": "2026-01-15",
                    }
                },
            },
        )
        before = self.env_file.read_bytes()
        # La date que le script va écrire est celle de **son** horloge, prise pendant
        # l'appel. La lire une seule fois avant (ou après) rendrait le test faux si
        # minuit tombe entre les deux — une fois par jour et par machine, ce qui est
        # plus fréquent qu'un échec réel. On encadre donc l'appel, comme le fait
        # `tests/test_pre_push_hook.py` avec la marge « aujourd'hui ou hier ».
        stamp_before = _today()

        code, out, err = self._run("--record-only", "SUPABASE_SERVICE_KEY")

        stamp_after = _today()
        self.assertEqual(code, 0)
        self.assertEqual(self.env_file.read_bytes(), before, "la valeur ne bouge pas")
        entries = sa.load_ledger(self.ledger)["secrets"]
        self.assertEqual(
            entries["WEBHOOK_SECRET"]["rotated_at"],
            "2026-01-15",
            "tourner un secret ne repousse pas l'échéance des autres",
        )
        self.assertEqual(
            entries["SUPABASE_SERVICE_KEY"]["fingerprint"],
            sa.secret_fingerprint("SUPABASE_SERVICE_KEY", values["SUPABASE_SERVICE_KEY"]),
        )
        self.assertIn(
            entries["SUPABASE_SERVICE_KEY"]["rotated_at"],
            {stamp_before, stamp_after},
            "horodaté le jour où le script a tourné — le second n'est admis que si "
            "minuit est passé pendant l'appel, jamais une date quelconque",
        )
        self.assertIn("inchangée", out)
        self.assertNotIn(values["SUPABASE_SERVICE_KEY"], out + err)

    def test_it_refuses_to_stamp_a_secret_without_a_value(self):
        values = _strong_values()
        values.pop("TELEGRAM_BOT_TOKEN")
        self._write(values)

        code, _out, err = self._run("--record-only", "TELEGRAM_BOT_TOKEN")

        self.assertEqual(code, 1)
        self.assertIn("aucune valeur", err)
        self.assertIn("BotFather", err, "la source est rappelée")
        self.assertFalse(self.ledger.exists(), "rien n'est horodaté pour une valeur absente")

    def test_an_unknown_name_is_refused(self):
        self._write()

        code, _out, err = self._run("--record-only", "PAS_UN_SECRET")

        self.assertEqual(code, 2)
        self.assertIn("PAS_UN_SECRET", err)

    def test_a_corrupt_ledger_is_refused_rather_than_overwritten(self):
        """Horodater *réécrit* le registre : sur un fichier illisible, il efface.

        Le fichier peut ne plus contenir que des dates de rotation qu'aucun autre
        exemplaire ne porte. On refuse, et la trace dit quoi regarder — la même
        règle que `verify_secrets.py --record`.
        """
        self._write()
        self.ledger.write_text('{"secrets": {"WEBHOOK_SECRET": {"rot', encoding="utf-8")
        before = self.ledger.read_bytes()

        code, _out, err = self._run("--record-only", "SUPABASE_SERVICE_KEY")

        self.assertEqual(code, 2)
        self.assertIn("illisible", err)
        self.assertEqual(self.ledger.read_bytes(), before, "le registre n'est pas écrasé")


class IllisibleRingTest(RotationCliTest):
    """Un anneau dont une version ne se lit pas : refuser, jamais deviner.

    `_ring_versions` sautait l'entrée illisible en s'autorisant de l'idée que
    `check_key_ring` la refuserait — c'est vrai de l'audit, faux de ce chemin-ci :
    `--rotate` écrit d'abord, l'audit ne repasse qu'au push suivant. La version
    choisie pouvait donc en reprendre une, et le refus n'arrivait que bien plus
    tard, très loin du geste fautif.
    """

    #: Un préfixe de version illisible : `parse_key_entry` refuse, donc la version
    #: occupée est inconnue et aucune version libre ne peut être choisie sans risque.
    UNREADABLE = "v0:quelquechose"

    def test_rotating_with_an_unreadable_ring_entry_is_refused(self):
        values = _strong_values()
        values["ENCRYPTION_KEYS_PREVIOUS"] = self.UNREADABLE
        self._write(values)
        before = self.env_file.read_bytes()

        code, out, _err = self._run("--rotate", "ENCRYPTION_KEY", "--apply")

        self.assertEqual(code, 1)
        self.assertEqual(self.env_file.read_bytes(), before, "rien n'est écrit")
        self.assertIn("rotation refusée", out)
        self.assertIn("illisible", out)

    def test_generating_a_missing_key_with_an_unreadable_ring_is_refused(self):
        values = _strong_values()
        values.pop("ENCRYPTION_KEY")
        values["ENCRYPTION_KEYS_PREVIOUS"] = self.UNREADABLE
        self._write(values)
        before = self.env_file.read_bytes()

        code, _out, err = self._run()

        self.assertEqual(code, 2)
        self.assertEqual(self.env_file.read_bytes(), before, "rien n'est écrit")
        self.assertIn("illisible", err)


class DeclarationTest(unittest.TestCase):
    """Chaque secret requis est soit générable ici, soit nommé avec sa source."""

    def setUp(self):
        self.cli = _load_cli()

    def test_generators_and_manual_sources_cover_every_required_secret(self):
        required = {spec.name for spec in sa.DEFAULT_SPECS if spec.required}
        optional = {spec.name for spec in sa.DEFAULT_SPECS if not spec.required}
        declared = set(self.cli.GENERATORS) | set(self.cli.MANUAL_SOURCES)

        self.assertTrue(required <= declared, "un secret requis ajouté au code doit être traité")
        # Ce qui est déclaré en plus d'un secret requis ne peut être qu'un secret
        # **facultatif** — l'anneau, ici, qui ne se tourne pas lui-même mais se nomme
        # avec sa provenance comme les autres valeurs qui viennent d'ailleurs.
        self.assertEqual(declared - required, {"ENCRYPTION_KEYS_PREVIOUS"})
        self.assertTrue(declared - required <= optional)
        self.assertEqual(
            set(self.cli.GENERATORS) & set(self.cli.MANUAL_SOURCES),
            set(),
            "un secret est généré ici OU rapporté — pas les deux",
        )

    def test_each_generator_produces_two_distinct_values_the_audit_accepts(self):
        for name, generate in self.cli.GENERATORS.items():
            with self.subTest(secret=name):
                first, second = generate(), generate()
                self.assertNotEqual(first, second, "deux tirages ne peuvent pas coïncider")
                problems = [
                    issue.message
                    for issue in sa.check_presence_and_strength({name: first})
                    if issue.name == name
                ]
                self.assertEqual(problems, [])


class DocumentedTest(unittest.TestCase):
    """La procédure écrite et le script ne peuvent pas diverger en silence."""

    def test_the_rotation_procedure_points_at_this_script(self):
        text = (REPO_ROOT / "docs" / "SECRETS.md").read_text(encoding="utf-8")

        self.assertIn("scripts/generate_secrets.py", text)
        self.assertIn("--check", text)

    def test_the_assisted_rotation_is_documented_with_its_guard_rail(self):
        """Un mode qui écrit une valeur neuve doit se lire **avant** d'être lancé."""
        text = (REPO_ROOT / "docs" / "SECRETS.md").read_text(encoding="utf-8")

        self.assertIn("--rotate", text)
        self.assertIn("--record-only", text)
        self.assertIn("--apply", text, "l'annonce et l'exécution sont deux gestes distincts")

    def test_the_readme_names_the_assisted_rotation(self):
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("scripts/generate_secrets.py", text)
        self.assertIn("--rotate", text)

    def test_the_example_file_points_at_this_script(self):
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertIn("scripts/generate_secrets.py", text)


if __name__ == "__main__":
    unittest.main()
