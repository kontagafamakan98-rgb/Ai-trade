"""Contrat de `scripts/check_github_workflows.py` : un workflow cassé se refuse, jamais en silence.

Le mode de défaillance visé n'est pas une erreur, c'est un **silence** : un YAML
invalide fait ignorer le fichier par GitHub. Plus de job, donc plus rien à
échouer — la branche paraît verte alors que la CI n'existe plus. Le fichier qui
compte le plus est donc `ci.yml` lui-même, et un test le prend pour cible : il est
copié, cassé d'une seule ligne, et l'outil doit le refuser.

Ce qui se teste ici tient en trois familles : ce qui est **cassé** (YAML
illisible, clé écrite deux fois), ce qui est **incohérent pour GitHub** (pas de
`on`, `jobs` vide, étape sans `run` ni `uses`, `needs` inconnu, service sans
`image`), et ce qui est **juste** — un workflow réaliste, avec scalaire bloc,
guillemets, apostrophes, `${{ }}`, `ports: - 5432:5432` et `needs`, ne doit
**rien** déclencher. Un outil qui refuse un fichier valide serait pire que rien.
"""

import contextlib
import io
import json
import pathlib
import tempfile
import unittest

from scripts import check_github_workflows

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
REAL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: Les jobs du workflow du dépôt, écrits en clair : c'est un contrat, pas une
#: constante recopiée du fichier. Renommer un job sans le vouloir se voit ici.
_JOB_IDS = (
    "python",
    "migrations-postgres",
    "git-hooks-e2e",
    "kotlin-lint",
    "kotlin-build-and-test",
)

#: Un workflow **réaliste** et valide : tout ce que l'outil doit savoir lire sans
#: broncher. Il est volontairement plus riche que celui du dépôt (apostrophes
#: françaises, `#` dans un scalaire bloc, `:` dans un `name`, `${{ }}`,
#: collection en flux, `ports: - 5432:5432`, `needs`).
VALID = """\
name: CI

on:
  push:
    branches: ["**"]
  pull_request:

jobs:
  python:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: pgvector/pgvector:pg16
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-retries 5
    steps:
      - uses: actions/checkout@v4
      - name: L'apostrophe française et le « # » d'un scalaire bloc
        run: |
          echo "rien # n'est un commentaire ici"
          grep -c "'" fichier || true
      - name: Vérifier : avec un deux-points dans le nom
        if: ${{ github.event_name == 'push' }}
        run: python scripts/verifier.py --flag 1
  second:
    needs: [python]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
"""


class _Fixture(unittest.TestCase):
    """Écrit un workflow dans un dépôt jetable, puis lance l'outil dessus."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="workflows-")
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)

    def write(self, text: str, name: str = "ci.yml") -> pathlib.Path:
        workflows = self.root / ".github" / "workflows"
        workflows.mkdir(parents=True, exist_ok=True)
        path = workflows / name
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def run_tool(self, *arguments: str):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = check_github_workflows.main(["--root", str(self.root), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def check(self, text: str):
        """Le verdict complet d'un workflow écrit à la main."""
        self.write(text)
        code, output, errors = self.run_tool()
        return code, output + errors

    def kinds(self, output: str):
        return sorted(finding["kind"] for finding in check_github_workflows.audit(self.root)[0])

    def assert_refused(self, text: str, kind: str):
        """Le fichier est refusé, **pour la bonne raison**, et le rapport la nomme."""
        self.write(text)
        findings, _ = check_github_workflows.audit(self.root)
        self.assertTrue(findings, "l'outil n'a rien refusé")
        self.assertIn(kind, [finding["kind"] for finding in findings], findings)
        code, _, errors = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn(kind, errors)
        self.assertIn("ci.yml", errors)
        return findings

    def assert_accepted(self, text: str) -> None:
        code, output = self.check(text)
        self.assertEqual(code, 0, output)
        self.assertIn("Tous les workflows sont valides", output)


class ValidWorkflowTest(_Fixture):
    """Ce qui est juste doit passer — sinon l'outil sera désactivé, et ne servira plus."""

    def test_a_realistic_workflow_is_accepted(self) -> None:
        self.assert_accepted(VALID)

    def test_it_says_what_it_read(self) -> None:
        """Un rapport qui ne dit pas ce qu'il a lu laisse croire qu'il a tout vu."""
        code, output = self.check(VALID)
        self.assertEqual(code, 0)
        self.assertIn("1 fichier(s)", output)
        self.assertIn("2 job(s)", output)
        self.assertIn("4 étape(s)", output)

    def test_a_workflow_of_a_single_scalar_trigger_is_accepted(self) -> None:
        self.assert_accepted("on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n")

    def test_a_reusable_workflow_job_is_accepted(self) -> None:
        """`uses:` remplace `runs-on` **et** `steps` : c'est le cas de la doc."""
        self.assert_accepted("on: push\njobs:\n  a:\n    uses: owner/repo/.github/workflows/x.yml@main\n")

    def test_a_repository_with_several_workflows_is_fully_read(self) -> None:
        """`.yaml` comme `.yml` : GitHub accepte les deux, donc on lit les deux."""
        self.write(VALID)
        self.write(
            "on: pull_request\njobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: true\n",
            "autre.yaml",
        )
        code, output, _ = self.run_tool()
        self.assertEqual(code, 0, output)
        self.assertIn("2 fichier(s)", output)
        self.assertIn("autre.yaml", output)

    def test_a_step_key_that_holds_a_colon_is_read_as_a_value(self) -> None:
        """`- 5432:5432` est un scalaire : le lire comme une clé refuserait un fichier juste."""
        self.assert_accepted(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: true\n        env:\n          PORTS: 5432:5432\n"
        )

    def test_the_reports_of_a_valid_run_go_to_stdout(self) -> None:
        """`> journal.txt` doit donner un journal propre, pas un fichier vide."""
        self.write(VALID)
        _, output, errors = self.run_tool()
        self.assertIn("Tous les workflows sont valides", output)
        self.assertEqual(errors, "")


class BrokenYamlTest(_Fixture):
    """Un fichier que GitHub ignore : aucun job, donc aucun échec."""

    def test_a_tab_indentation_is_refused(self) -> None:
        self.assert_refused("on: push\njobs:\n\tpython:\n\t\truns-on: ubuntu-latest\n", check_github_workflows.BROKEN_YAML)

    def test_an_unclosed_quote_is_refused(self) -> None:
        self.assert_refused('on: push\nname: "oups\njobs:\n  a:\n    runs-on: x\n', check_github_workflows.BROKEN_YAML)

    def test_an_unclosed_flow_sequence_is_refused(self) -> None:
        self.assert_refused("on:\n  push:\n    branches: [main\njobs: {}\n", check_github_workflows.BROKEN_YAML)

    def test_an_indented_document_root_is_refused(self) -> None:
        self.assert_refused("  on: push\n  jobs: {}\n", check_github_workflows.BROKEN_YAML)

    def test_a_root_that_is_not_a_mapping_is_refused(self) -> None:
        self.assert_refused("- on: push\n- jobs: {}\n", check_github_workflows.BROKEN_YAML)

    def test_an_empty_file_is_refused(self) -> None:
        self.assert_refused("# seulement un commentaire\n", check_github_workflows.BROKEN_YAML)

    def test_the_message_says_what_the_silence_costs(self) -> None:
        """La raison d'être de l'outil, dite au moment où elle sert."""
        self.write("on: push\njobs:\n\tpython:\n")
        _, _, errors = self.run_tool()
        self.assertIn("ignoré", errors)

    def test_the_report_says_un_readable_files_silence_github(self) -> None:
        self.write("on: push\njobs:\n\tpython:\n")
        _, _, errors = self.run_tool()
        self.assertIn("aucun job ne tourne", errors)


class DuplicateKeyTest(_Fixture):
    """Le piège du format : la seconde définition **écrase** la première en silence."""

    def test_a_duplicate_jobs_section_is_refused(self) -> None:
        findings = self.assert_refused(
            "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: true\n"
            "jobs:\n  b:\n    runs-on: x\n    steps:\n      - run: true\n",
            check_github_workflows.DUPLICATE_KEY,
        )
        self.assertIn("ligne 2", findings[0]["detail"])

    def test_a_duplicate_job_id_is_refused(self) -> None:
        """Deux fois le même job : le second remplace le premier, sans un mot."""
        self.assert_refused(
            "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: true\n"
            "  a:\n    runs-on: y\n    steps:\n      - run: true\n",
            check_github_workflows.DUPLICATE_KEY,
        )

    def test_a_duplicate_key_inside_a_step_is_refused(self) -> None:
        self.assert_refused(
            "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n"
            "      - run: true\n        run: false\n",
            check_github_workflows.DUPLICATE_KEY,
        )


class GithubStructureTest(_Fixture):
    """Ce que GitHub refuse, et qui emporte le fichier ou le job."""

    def test_a_misspelled_trigger_key_is_refused(self) -> None:
        """`one:` au lieu de `on:` : le workflow ne se déclencherait jamais, en silence."""
        findings = self.assert_refused(
            "one: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: true\n",
            check_github_workflows.UNKNOWN_KEY,
        )
        self.assertIn("one", findings[0]["detail"])

    def test_a_missing_trigger_is_refused(self) -> None:
        self.assert_refused("jobs:\n  a:\n    runs-on: x\n    steps:\n      - run: true\n", check_github_workflows.MISSING_KEY)

    def test_a_missing_jobs_section_is_refused(self) -> None:
        self.assert_refused("on: push\n", check_github_workflows.MISSING_KEY)

    def test_an_empty_jobs_section_is_refused(self) -> None:
        self.assert_refused("on: push\njobs: {}\n", check_github_workflows.EMPTY)

    def test_a_job_without_a_runner_is_refused(self) -> None:
        self.assert_refused("on: push\njobs:\n  a:\n    steps:\n      - run: true\n", check_github_workflows.MISSING_KEY)

    def test_a_job_that_both_calls_and_runs_is_refused(self) -> None:
        self.assert_refused(
            "on: push\njobs:\n  a:\n    uses: o/r/.github/workflows/x.yml@main\n"
            "    steps:\n      - run: true\n",
            check_github_workflows.BAD_JOB,
        )

    def test_empty_steps_are_refused(self) -> None:
        self.assert_refused("on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n", check_github_workflows.EMPTY)

    def test_an_invalid_job_identifier_is_refused(self) -> None:
        self.assert_refused(
            "on: push\njobs:\n  mon.job:\n    runs-on: x\n    steps:\n      - run: true\n",
            check_github_workflows.BAD_JOB,
        )

    def test_a_step_with_run_and_uses_is_refused(self) -> None:
        self.assert_refused(
            "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n"
            "      - name: deux\n        uses: actions/checkout@v4\n        run: true\n",
            check_github_workflows.BAD_STEP,
        )

    def test_a_step_with_neither_run_nor_uses_is_refused(self) -> None:
        self.assert_refused(
            "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - name: vide\n",
            check_github_workflows.BAD_STEP,
        )

    def test_a_misspelled_step_key_is_refused(self) -> None:
        """`command:` au lieu de `run:` : git ne verrait rien, GitHub non plus."""
        self.assert_refused(
            "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - command: true\n",
            check_github_workflows.UNKNOWN_KEY,
        )

    def test_a_needs_on_an_unknown_job_is_refused(self) -> None:
        self.assert_refused(
            "on: push\njobs:\n  a:\n    needs: fantome\n    runs-on: x\n    steps:\n      - run: true\n",
            check_github_workflows.UNKNOWN_NEEDS,
        )

    def test_a_needs_on_a_known_job_is_accepted(self) -> None:
        self.assert_accepted(
            "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: true\n"
            "  b:\n    needs: a\n    runs-on: x\n    steps:\n      - run: true\n"
        )

    def test_a_service_without_an_image_is_refused(self) -> None:
        self.assert_refused(
            "on: push\njobs:\n  a:\n    runs-on: x\n    services:\n      db:\n        env:\n          A: b\n"
            "    steps:\n      - run: true\n",
            check_github_workflows.BAD_SERVICE,
        )

    def test_a_plain_scalar_on_several_lines_is_refused_rather_than_approved(self) -> None:
        """Ce que l'outil ne sait pas lire, il le dit — il ne l'approuve pas."""
        self.assert_refused(
            "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: premiere ligne\n          suite\n",
            check_github_workflows.UNSUPPORTED,
        )


class TheRepositoryItsOwnWorkflowTest(_Fixture):
    """Le fichier le plus dangereux est `ci.yml` : celui dont on attendrait qu'il prévienne."""

    def test_the_workflow_of_this_repository_is_valid(self) -> None:
        code, output = self.check(REAL_WORKFLOW.read_text(encoding="utf-8"))
        self.assertEqual(code, 0, output)
        findings, counted = check_github_workflows.audit(self.root)
        self.assertEqual(findings, [])
        self.assertGreater(counted["jobs"], 0, "aucun job lu : le fichier n'a pas été compris")
        self.assertGreater(counted["steps"], 0)

    def test_the_repository_workflow_is_really_read(self) -> None:
        """Les comptes viennent du fichier, pas d'une constante : ils doivent coller.

        Les identifiants sont écrits ici en clair : renommer un job du dépôt sans
        toucher à cette liste doit se voir, parce que c'est exactement ce que la
        vérification protège (un job renommé à la main, un job disparu).
        """
        self.write(REAL_WORKFLOW.read_text(encoding="utf-8"))
        _, counted = check_github_workflows.audit(self.root)
        self.assertEqual(
            counted["jobs"],
            len(_JOB_IDS),
            "un job du dépôt a été renommé, ajouté ou supprimé",
        )

    def test_the_jobs_of_this_repository_are_the_expected_ones(self) -> None:
        """La liste des jobs lus, comparée aux identifiants que le dépôt déclare."""
        import re

        text = REAL_WORKFLOW.read_text(encoding="utf-8")
        # Seulement la section `jobs:` : une autre clé à deux espaces (comme
        # `push:` sous `on:`) n'est pas un identifiant de job.
        jobs = text.split("\njobs:", 1)[1]
        declared = set(re.findall(r"^  ([A-Za-z_][A-Za-z0-9_-]*):", jobs, re.MULTILINE))
        self.assertEqual(declared, set(_JOB_IDS))

    def test_a_single_broken_line_in_the_real_workflow_is_caught(self) -> None:
        """La preuve que le fichier du dépôt est vraiment vérifiable."""
        broken = REAL_WORKFLOW.read_text(encoding="utf-8").replace("jobs:", "job:", 1)
        self.assertNotEqual(broken, REAL_WORKFLOW.read_text(encoding="utf-8"))
        findings = self.assert_refused(broken, check_github_workflows.UNKNOWN_KEY)
        self.assertTrue(
            any("job" in finding["detail"] for finding in findings),
            findings,
        )

    def test_a_renamed_job_id_in_the_real_workflow_is_caught(self) -> None:
        broken = REAL_WORKFLOW.read_text(encoding="utf-8").replace(
            "  python:\n", "  python:\n    runs-on: ubuntu-latest\n    runs-on: ubuntu-latest\n", 1
        )
        self.assert_refused(broken, check_github_workflows.DUPLICATE_KEY)

    def test_the_checker_is_wired_into_the_workflow_it_checks(self) -> None:
        """Sinon une CI cassée ne serait vue nulle part : la suite, elle, tourne."""
        self.assertIn("check_github_workflows.py", REAL_WORKFLOW.read_text(encoding="utf-8"))

    def test_the_checker_is_documented(self) -> None:
        self.assertIn("check_github_workflows.py", README.read_text(encoding="utf-8"))


class CommandLineTest(_Fixture):
    """Les codes de sortie et la sortie machine, comme les autres outils du dépôt."""

    def test_a_broken_workflow_exits_one_and_writes_the_report_to_stderr(self) -> None:
        self.write("on: push\njob:\n  a:\n")
        code, output, errors = self.run_tool()
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("clé inconnue", errors)

    def test_the_json_report_carries_the_same_verdict(self) -> None:
        self.write("on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - name: vide\n")
        code, output, _ = self.run_tool("--json")
        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertFalse(report["ok"])
        self.assertEqual(report["jobs"], 1)
        self.assertEqual(report["counts"], {check_github_workflows.BAD_STEP: 1})
        self.assertEqual(sum(report["counts"].values()), len(report["findings"]))
        self.assertIn("ci.yml", report["findings"][0]["file"])

    def test_a_valid_workflow_exits_zero(self) -> None:
        self.write(VALID)
        code, output, errors = self.run_tool("--json")
        self.assertEqual(code, 0, errors)
        self.assertTrue(json.loads(output)["ok"])

    def test_a_missing_workflows_directory_exits_two(self) -> None:
        code, _, errors = self.run_tool()
        self.assertEqual(code, 2)
        self.assertIn("rien à vérifier", errors)

    def test_an_empty_workflows_directory_exits_two(self) -> None:
        (self.root / ".github" / "workflows").mkdir(parents=True)
        code, _, errors = self.run_tool()
        self.assertEqual(code, 2)
        self.assertIn("aucun workflow", errors)

    def test_bad_usage_exits_two(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            check_github_workflows.main(["--inconnu"])
        self.assertEqual(raised.exception.code, 2)

    def test_only_workflow_suffixes_are_read(self) -> None:
        """Un `notes.txt` dans le répertoire n'est pas un workflow."""
        self.write(VALID)
        self.write("ceci n'est pas un workflow\n", "notes.txt")
        code, output, errors = self.run_tool("--json")
        self.assertEqual(code, 0, errors)
        self.assertEqual(len(json.loads(output)["files"]), 1)

    def test_it_needs_no_dependency(self) -> None:
        """Cette vérification doit pouvoir tourner dans un job sans installation."""
        source = (REPO_ROOT / "scripts" / "check_github_workflows.py").read_text(encoding="utf-8")
        self.assertNotIn("import yaml", source)
        self.assertIn("Bibliothèque standard uniquement", source)


if __name__ == "__main__":
    unittest.main()
