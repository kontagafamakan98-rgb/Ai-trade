"""Tests du hook pre-commit anti-fuite.

Trois niveaux sont couverts :

1. le **scan du contenu indexé** (`core.secrets_audit.scan_staged_for_secrets`) :
   détection, masquage, exclusions, et surtout lecture de l'**index** et non de
   la copie de travail ;
2. la **CLI** `scripts/pre_commit_secrets.py` : codes de sortie 0/1/2, sortie
   JSON, et l'avertissement explicite quand aucun secret de référence n'est
   configuré (un scan sans cible ne prouve rien) ;
3. le **hook** lui-même : présence, exécutabilité, syntaxe et délégation à la CLI.

Les tests de la CLI vident l'environnement (`clear=True`) : d'autres tests du
dépôt y injectent de faux secrets, ce qui rendrait les cibles du scan imprévisibles.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from core import secrets_audit as sa
from tests.hook_support import assert_valid_posix_shell

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = REPO_ROOT / ".githooks" / "pre-commit"
CLI_PATH = REPO_ROOT / "scripts" / "pre_commit_secrets.py"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: Valeur de sonde : assez longue (>= 16) et suffisamment variée pour passer les
#: contrôles de robustesse, mais volontairement factice.
PROBE_SECRET = "whsec_hook_probe_9f3a2c7b1d4e6f8a"
PROBE_ENV = {"WEBHOOK_SECRET": PROBE_SECRET}


def _load_cli():
    """La CLI est chargée par chemin, comme dans `test_secrets_audit.py`."""
    spec = importlib.util.spec_from_file_location("pre_commit_secrets", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


def _issues_for(files, values=None, reader=None, **kwargs):
    values = PROBE_ENV if values is None else values
    return sa.scan_staged_for_secrets(
        values,
        root=".",
        staged_files=list(files),
        content_reader=reader,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# 1. Scan du contenu indexé
# --------------------------------------------------------------------------- #


class ScanStagedContentTest(unittest.TestCase):
    def test_detects_leak_with_file_and_line(self):
        content = f"line one\nOTHER={PROBE_SECRET}\nline three\n"
        issues = _issues_for(["config.py"], reader=lambda rel: content)
        errors = [i for i in issues if i.severity == sa.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].name, "WEBHOOK_SECRET")
        self.assertIn("config.py:2", errors[0].message)

    def test_reported_value_is_masked_never_echoed(self):
        """Un rapport de fuite ne doit pas contenir la fuite."""
        issues = _issues_for(["config.py"], reader=lambda rel: f"SECRET={PROBE_SECRET}\n")
        self.assertTrue(issues)
        for issue in issues:
            self.assertNotIn(PROBE_SECRET, issue.message)
        self.assertIn(sa.mask_secret(PROBE_SECRET), issues[0].message)

    def test_clean_content_produces_nothing(self):
        self.assertEqual(_issues_for(["clean.py"], reader=lambda rel: "print('bonjour')\n"), [])

    def test_bytes_reader_is_supported(self):
        issues = _issues_for(["config.py"], reader=lambda rel: f"x={PROBE_SECRET}".encode())
        self.assertEqual(len(issues), 1)

    def test_env_file_is_scanned_when_staged(self):
        """`git add -f .env` est précisément le cas que le hook doit bloquer.

        Le scan du dépôt ignore les `.env` locaux (ce sont les *sources* des
        secrets) ; le scan des fichiers indexés doit au contraire les analyser.
        """
        issues = _issues_for([".env"], reader=lambda rel: f"WEBHOOK_SECRET={PROBE_SECRET}\n")
        self.assertEqual(len(issues), 1)

        # Contrôle croisé : le scan du dépôt, lui, ignore ce même fichier.
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".env").write_text(f"WEBHOOK_SECRET={PROBE_SECRET}\n", encoding="utf-8")
            repo_issues = sa.scan_repo_for_secrets(PROBE_ENV, root=tmp, tracked_files=[".env"])
        self.assertEqual(repo_issues, [])

    def test_binary_content_is_skipped(self):
        issues = _issues_for(["image.dat"], reader=lambda rel: b"\x00\x01" + PROBE_SECRET.encode())
        self.assertEqual(issues, [])

    def test_binary_suffix_is_skipped(self):
        issues = _issues_for(["logo.png"], reader=lambda rel: PROBE_SECRET.encode())
        self.assertEqual(issues, [])

    def test_oversized_content_is_named_not_silent(self):
        """Trop gros pour être lu : écarté, mais **nommé** — et sans bloquer."""
        big = ("x" * 10 + "\n") * 200_000 + PROBE_SECRET
        self.assertGreater(len(big), sa.DEFAULT_SCAN_MAX_BYTES)
        issues = _issues_for(["huge.log"], reader=lambda rel: big.encode())
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, sa.WARNING)
        self.assertIn("huge.log", issues[0].message)

    def test_reader_failure_is_named_not_swallowed(self):
        """Un blob indexé illisible échoue le commit au lieu de passer en silence.

        Le hook ne peut pas prouver qu'un fichier qu'il n'a pas lu est propre :
        rendre `[]` revenait à dire « aucune fuite » sur un fichier jamais
        regardé — précisément le rapport que ce hook existe pour empêcher.
        """

        def _boom(rel):
            raise OSError("permission refusee")

        issues = _issues_for(["locked.py"], reader=_boom)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, sa.ERROR)
        self.assertIn("locked.py", issues[0].message)

    def test_missing_blob_is_named_not_swallowed(self):
        """`git cat-file` sans réponse : le fichier est indexé, donc il sera commité."""
        issues = _issues_for(["gone.py"], reader=lambda rel: None)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, sa.ERROR)
        self.assertIn("gone.py", issues[0].message)

    def test_no_reference_values_means_no_scan(self):
        """Sans valeur de référence, le scan ne peut rien trouver."""
        issues = _issues_for([".env"], values={}, reader=lambda rel: f"x={PROBE_SECRET}")
        self.assertEqual(issues, [])

    def test_placeholder_reference_values_are_ignored(self):
        values = {"WEBHOOK_SECRET": "change-me-super-secret"}
        issues = _issues_for(
            ["config.py"], values=values, reader=lambda rel: "WEBHOOK_SECRET=change-me-super-secret"
        )
        self.assertEqual(issues, [])

    def test_short_reference_values_are_ignored(self):
        issues = _issues_for(["config.py"], values={"WEBHOOK_SECRET": "abc"}, reader=lambda rel: "abc")
        self.assertEqual(issues, [])

    def test_multiple_files_are_all_scanned(self):
        contents = {
            "a.py": "rien\n",
            "b.py": f"k={PROBE_SECRET}\n",
            "c.py": f"k2={PROBE_SECRET}\n",
        }
        issues = _issues_for(list(contents), reader=contents.get)
        labels = {i.message.split("dans ")[1].split(":")[0] for i in issues}
        self.assertEqual(labels, {"b.py", "c.py"})

    def test_scannable_targets_reports_what_will_be_searched(self):
        self.assertEqual([n for n, _ in sa.scannable_targets(PROBE_ENV)], ["WEBHOOK_SECRET"])
        self.assertEqual(sa.scannable_targets({}), [])


class GitCommandTest(unittest.TestCase):
    """Vérifie les commandes git elles-mêmes : c'est la partie non pure."""

    def test_staged_files_uses_the_index_and_excludes_deletions(self):
        completed = subprocess.CompletedProcess([], 0, b"a.py\0b.py\0", b"")
        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            files = sa.git_staged_files(".")
        argv = run.call_args[0][0]
        self.assertEqual(files, ["a.py", "b.py"])
        self.assertIn("--cached", argv)
        filter_arg = next(a for a in argv if a.startswith("--diff-filter="))
        # A(ajout) C(copie) M(modif) R(renommage) — pas de D(suppression) :
        # un fichier supprimé n'a plus de contenu à analyser.
        self.assertEqual(filter_arg, "--diff-filter=ACMR")
        self.assertNotIn("D", filter_arg.split("=")[1])

    def test_empty_index_is_distinguishable_from_missing_git(self):
        with mock.patch.object(
            subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"", b"")
        ):
            self.assertEqual(sa.git_staged_files("."), [])
        with mock.patch.object(subprocess, "run", side_effect=OSError("git absent")):
            self.assertIsNone(sa.git_staged_files("."))
        with mock.patch.object(
            subprocess, "run", return_value=subprocess.CompletedProcess([], 128, b"", b"")
        ):
            self.assertIsNone(sa.git_staged_files("."))

    def test_staged_content_reads_the_index_blob(self):
        completed = subprocess.CompletedProcess([], 0, b"contenu indexe", b"")
        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            content = sa.git_staged_content(".", "src/app.py")
        argv = run.call_args[0][0]
        self.assertEqual(content, b"contenu indexe")
        # `:src/app.py` désigne le blob de l'index, pas le fichier du disque.
        self.assertIn("cat-file", argv)
        self.assertIn(":src/app.py", argv)

    def test_staged_content_returns_none_when_git_fails(self):
        with mock.patch.object(
            subprocess, "run", return_value=subprocess.CompletedProcess([], 128, b"", b"")
        ):
            self.assertIsNone(sa.git_staged_content(".", "absent.py"))


# --------------------------------------------------------------------------- #
# 2. CLI
# --------------------------------------------------------------------------- #


class PreCommitCliTest(unittest.TestCase):
    def _run(self, argv, staged, contents, env=None):
        """Exécute la CLI avec git simulé : index + contenu indexé."""
        env = PROBE_ENV if env is None else env

        def reader(_root, path):
            value = contents.get(path)
            return value.encode() if isinstance(value, str) else value

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, env, clear=True
        ), mock.patch.object(
            cli, "git_staged_files", lambda root, **kwargs: staged
        ), mock.patch.object(
            sa, "git_staged_content", reader
        ):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["--root", tmp, "--no-env-file"] + argv)
        return code, out.getvalue(), err.getvalue()

    def test_returns_1_on_leak_and_never_echoes_the_secret(self):
        code, out, _ = self._run([], ["config.py"], {"config.py": f"K={PROBE_SECRET}\n"})
        self.assertEqual(code, 1)
        self.assertIn("COMMIT REFUSÉ", out)
        self.assertIn("config.py:1", out)
        self.assertNotIn(PROBE_SECRET, out)

    def test_returns_0_on_clean_index(self):
        code, out, _ = self._run([], ["config.py"], {"config.py": "print(1)\n"})
        self.assertEqual(code, 0)
        self.assertNotIn("COMMIT REFUSÉ", out)

    def test_returns_0_when_nothing_is_staged(self):
        code, _, _ = self._run([], [], {})
        self.assertEqual(code, 0)

    def test_warns_loudly_when_no_reference_secret(self):
        """Sans secret configuré, l'absence de fuite n'est pas une preuve."""
        code, out, _ = self._run([], ["config.py"], {"config.py": f"K={PROBE_SECRET}\n"}, env={})
        self.assertEqual(code, 0)
        self.assertIn("aucune valeur de référence", out)

    def test_skips_when_git_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, PROBE_ENV, clear=True
        ), mock.patch.object(cli, "git_staged_files", lambda root, **kwargs: None):
            sink = io.StringIO()
            with redirect_stdout(sink):
                code = cli.main(["--root", tmp, "--no-env-file", "--verbose"])
        self.assertEqual(code, 0)
        self.assertIn("dépôt git introuvable", sink.getvalue())

    def test_json_output_is_machine_readable(self):
        code, out, _ = self._run(["--json"], ["config.py"], {"config.py": f"K={PROBE_SECRET}\n"})
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["staged_files"], ["config.py"])
        self.assertEqual(payload["reference_secrets"], ["WEBHOOK_SECRET"])
        self.assertNotIn(PROBE_SECRET, out)

    def test_verbose_lists_what_was_checked(self):
        code, out, _ = self._run(["--verbose"], ["clean.py"], {"clean.py": "ok\n"})
        self.assertEqual(code, 0)
        self.assertIn("clean.py", out)
        self.assertIn("WEBHOOK_SECRET", out)

    def test_missing_explicit_env_file_is_a_usage_error(self):
        _, err = io.StringIO(), io.StringIO()
        with redirect_stderr(err):
            code = cli.main(["--env-file", "vraiment-absent.env"])
        self.assertEqual(code, 2)
        self.assertIn("introuvable", err.getvalue())

    def test_strict_rotation_blocks_when_rotation_is_unrecorded(self):
        """Le contrôle de rotation est opt-in, mais bloquant quand il est demandé."""
        code, out, _ = self._run(
            ["--strict-rotation", "--ledger", "registre-absent.json"],
            ["clean.py"],
            {"clean.py": "ok\n"},
        )
        self.assertEqual(code, 1)
        self.assertIn("rotation", out)

    def test_install_requires_the_hook_file(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--install", "--root", tmp]), 2)

    def test_install_requires_both_versioned_hooks(self):
        """`core.hooksPath` active tout `.githooks` : les deux hooks doivent être là.

        Un `pre-commit` seul (dossier incomplet) rendrait le gate de push
        silencieusement absent — l'installation doit le refuser.
        """
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / ".githooks"
            hooks.mkdir()
            (hooks / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err):
                code = cli.main(["--install", "--root", tmp])
        self.assertEqual(code, 2)
        self.assertIn("pre-push", err.getvalue())

    def test_install_outside_a_git_repository_is_a_usage_error(self):
        """Le hook existe, mais `git config` échoue hors dépôt → code 2."""
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / ".githooks"
            hooks.mkdir()
            (hooks / "pre-commit").write_text("#!/bin/sh\n", encoding="utf-8")
            failed = subprocess.CompletedProcess([], 128, b"", b"fatal: not a git repository")
            with mock.patch.object(subprocess, "run", return_value=failed), redirect_stderr(
                io.StringIO()
            ):
                self.assertEqual(cli.main(["--install", "--root", tmp]), 2)


# --------------------------------------------------------------------------- #
# 3. Le hook versionné
# --------------------------------------------------------------------------- #


class HookFileTest(unittest.TestCase):
    def test_hook_exists_and_is_executable(self):
        self.assertTrue(HOOK_PATH.is_file(), f"{HOOK_PATH} manquant")
        self.assertTrue(
            os.access(HOOK_PATH, os.X_OK),
            "le hook doit être exécutable (chmod +x .githooks/pre-commit)",
        )

    def test_hook_delegates_to_the_cli(self):
        text = HOOK_PATH.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!"))
        self.assertIn("scripts/pre_commit_secrets.py", text)
        self.assertIn("core.hooksPath", text)  # documente l'installation

    def test_hook_is_valid_shell(self):
        """`sh -n` valide la syntaxe sans exécuter le hook."""
        assert_valid_posix_shell(self, HOOK_PATH)


if __name__ == "__main__":
    unittest.main()
