"""Contrat d'outillage des wrappers `.githooks/*`, prouvé par le **code** seul.

`git commit` et `git push` **écrasent à 1** le code de leurs hooks : un refus, un
outillage mort et un binaire introuvable rendent donc tous les trois 1. La
distinction que le dépôt veut tenir — « l'outil a conclu » contre « l'outillage
n'a pas démarré » — ne se lisait alors que dans la sortie, ce qui obligeait tout
appelant à analyser du texte pour savoir s'il avait affaire à un refus.

Les wrappers la portent maintenant dans leur **propre code** : `HOOK_TOOLING_EXIT`
(3) quand rien n'a pu être contrôlé, et les trois codes de l'outil (0/1/2) quand
il a conclu. Ces tests invoquent donc les wrappers **directement** — le seul
chemin où ce code survit — et tranchent **sans lire un seul mot de sortie** :

* interpréteur introuvable, script de l'outil absent, outillage mort sans rien
  dire, code hors contrat → 3 : rien n'a été contrôlé ;
* refus légitime de l'outil → 1, rendu tel quel : le refus n'est pas maquillé.

Ce qui doit, en revanche, rester lisible est vérifié à part : le rapport de
l'outil traverse le wrapper, sans quoi un refus arriverait sans son explication.
Et parce qu'un hook peut toujours être **contourné** (`--no-verify`), le dernier
test vérifie que le nouveau code n'a pas rendu le commit permissif : sous git, un
outillage absent refuse toujours.

Tous ces tests passent par `run_wrapper_conclusion` et non par `run_wrapper`,
parce que le code du wrapper n'est une réponse que si son **enfant** a tourné.
Sous MSYS2 saturé, `sh` meurt avant de lancer la doublure : le wrapper conclut
alors `3` (« le scan a rendu 127, hors de son contrat ») — vrai, et sans rapport
avec le scénario du test, qui n'a jamais été exercé. Ce `3`-là est rejoué puis le
test est **passé** (voir `tests/hook_support.py`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.hook_support import (
    HOOK_TOOLING_EXIT,
    declared_tooling_exit,
    hook_wrapper,
    path_with_shim,
    path_without_python,
    run_wrapper_conclusion,
    run_with_spawn_retry,
)

#: Les deux wrappers versionnés, et le script que chacun délègue.
HOOKS = {
    "pre-commit": "scripts/pre_commit_secrets.py",
    "pre-push": "scripts/verify_secrets.py",
}

#: Ce que l'outil écrit quand il a **conclu** qu'il fallait refuser.
REFUSAL_REPORT = "❌ COMMIT REFUSÉ : 1 fuite(s) de secret."

#: Ce qu'un outillage mort laisse derrière lui : rien. C'est la signature de la
#: saturation de fork sous MSYS2 — le hook démarre, son outillage meurt, et le
#: code rendu est celui d'un refus.
SILENT_DEATH = "exit 1"

#: Un code que les deux outils n'émettent jamais : leur contrat s'arrête à 2.
OUT_OF_CONTRACT = "exit 127"

#: Le mot que le wrapper met dans **chaque** message d'outillage manquant. Il
#: nomme l'issue là où git n'aura laissé qu'un 1 muet.
MARKER = "NON DÉMARRÉ"


def _decode(data: bytes) -> str:
    """Décode sans dépendre de la locale (les wrappers émettent « ⚠️ »…)."""
    return data.decode("utf-8", errors="replace")


def _output(proc: subprocess.CompletedProcess) -> str:
    return _decode(proc.stdout or b"") + _decode(proc.stderr or b"")


class ToolingExitContractTest(unittest.TestCase):
    """Le code distinct, éprouvé en invoquant le wrapper lui-même."""

    def test_the_wrappers_declare_the_code_this_suite_uses(self) -> None:
        """Le code vit dans les wrappers ; ce module ne fait que le refléter."""
        for name in HOOKS:
            with self.subTest(hook=name):
                self.assertEqual(
                    declared_tooling_exit(hook_wrapper(name)),
                    HOOK_TOOLING_EXIT,
                    f"{name} doit déclarer EXIT_TOOLING={HOOK_TOOLING_EXIT}",
                )

    def test_a_missing_interpreter_is_not_a_refusal(self) -> None:
        """Rien à démarrer : 3 — et surtout pas 0, qui laisserait passer un commit.

        C'est le changement assumé de ce contrat : le wrapper ne décide plus
        silencieusement de « laisser passer » quand il n'a rien pu lancer.
        """
        for name in HOOKS:
            with self.subTest(hook=name), tempfile.TemporaryDirectory(
                prefix="hook-nopython-"
            ) as tmp:
                proc = run_wrapper_conclusion(
                    self, hook_wrapper(name), path=path_without_python(Path(tmp))
                )
                output = _output(proc)

                self.assertEqual(proc.returncode, HOOK_TOOLING_EXIT, output)
                self.assertIn(MARKER, output)
                self.assertIn("Python", output)

    def test_a_silent_death_is_not_a_refusal(self) -> None:
        """Le cas qui motive ce code : mourir **sans un mot**, en rendant 1."""
        for name in HOOKS:
            with self.subTest(hook=name), tempfile.TemporaryDirectory(
                prefix="hook-silent-"
            ) as tmp:
                proc = run_wrapper_conclusion(
                    self,
                    hook_wrapper(name),
                    path=path_with_shim(Path(tmp), "python3", SILENT_DEATH),
                )
                output = _output(proc)

                self.assertEqual(proc.returncode, HOOK_TOOLING_EXIT, output)
                self.assertIn(MARKER, output)

    def test_a_code_outside_the_contract_is_not_a_refusal(self) -> None:
        """Les deux outils s'arrêtent à 2 : au-delà, ils n'ont rien décidé."""
        for name in HOOKS:
            with self.subTest(hook=name), tempfile.TemporaryDirectory(
                prefix="hook-127-"
            ) as tmp:
                proc = run_wrapper_conclusion(
                    self,
                    hook_wrapper(name),
                    path=path_with_shim(Path(tmp), "python3", OUT_OF_CONTRACT),
                )
                output = _output(proc)

                self.assertEqual(proc.returncode, HOOK_TOOLING_EXIT, output)
                self.assertIn(MARKER, output)

    def test_a_refusal_is_passed_through_untouched(self) -> None:
        """Le garde-fou du garde-fou : un refus n'est jamais requalifié en incident.

        Le code rendu est celui de l'outil, **et** son rapport traverse le
        wrapper : c'est ce qui distingue « refusé » de « rien n'a tourné ».
        """
        shim = f'printf "%s\\n" "{REFUSAL_REPORT}"; exit 1'
        for name in HOOKS:
            with self.subTest(hook=name), tempfile.TemporaryDirectory(
                prefix="hook-refusal-"
            ) as tmp:
                proc = run_wrapper_conclusion(
                    self,
                    hook_wrapper(name),
                    path=path_with_shim(Path(tmp), "python3", shim),
                )
                output = _output(proc)

                self.assertEqual(proc.returncode, 1, output)
                self.assertNotIn(MARKER, output)
                self.assertIn(REFUSAL_REPORT, output)

    def test_a_missing_tool_script_is_not_a_refusal(self) -> None:
        """Le script délégué absent : le wrapper le dit, au lieu de laisser passer."""
        for name, script in HOOKS.items():
            with self.subTest(hook=name), tempfile.TemporaryDirectory(
                prefix="hook-noscript-"
            ) as tmp:
                root = Path(tmp)
                hooks_dir = root / ".githooks"
                hooks_dir.mkdir()
                shutil.copy2(hook_wrapper(name), hooks_dir / name)

                proc = run_wrapper_conclusion(self, hooks_dir / name)
                output = _output(proc)

                self.assertEqual(proc.returncode, HOOK_TOOLING_EXIT, output)
                self.assertIn(MARKER, output)
                if shutil.which("python3") or shutil.which("python"):
                    # Le garde atteint est bien celui du script, et non celui de
                    # l'interpréteur — sinon ce test prouverait autre chose.
                    self.assertIn(script, output)


@unittest.skipUnless(shutil.which("git"), "git est requis pour ce test de bout en bout")
class ToolingIncidentStillBlocksTest(unittest.TestCase):
    """Le code distinct n'a pas rendu le hook permissif : git refuse toujours.

    Le cas qui compte est celui de l'interpréteur introuvable : c'est le seul où
    l'ancien wrapper décidait `exit 0` — un commit passait alors **sans aucun
    contrôle**, indiscernable d'un scan propre.
    """

    def setUp(self) -> None:
        self.git = shutil.which("git")
        assert self.git is not None
        self.tmp = tempfile.TemporaryDirectory(prefix="hook-tooling-")
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / ".githooks").mkdir(parents=True)
        for name in HOOKS:
            shutil.copy2(hook_wrapper(name), self.repo / ".githooks" / name)
            (self.repo / ".githooks" / name).chmod(0o755)

        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "ci@example.invalid")
        self._git("config", "user.name", "CI")
        self._git("config", "commit.gpgsign", "false")
        self._git("config", "core.hooksPath", ".githooks")
        (self.repo / "a.txt").write_text("bonjour\n", encoding="utf-8")
        self._git("add", "a.txt")

    def _git(self, *args: str, path: str | None = None) -> subprocess.CompletedProcess:
        env = None if path is None else {**os.environ, "PATH": path}
        return run_with_spawn_retry(
            [self.git, *args], cwd=str(self.repo), env=env, capture_output=True, timeout=120
        )

    def test_a_commit_cannot_slip_through_without_an_interpreter(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hook-nopython-") as tmp:
            proc = self._git("commit", "-m", "sans outillage", path=path_without_python(Path(tmp)))

        self.assertNotEqual(proc.returncode, 0, _output(proc))
        self.assertIn(MARKER, _output(proc))
        self.assertNotEqual(
            self._git("rev-parse", "--verify", "HEAD").returncode,
            0,
            "aucun commit ne doit avoir été créé",
        )

    def test_a_commit_cannot_slip_through_when_the_tool_is_gone(self) -> None:
        """Sans le script délégué, l'ancien wrapper `exit 0` laissait tout passer."""
        proc = self._git("commit", "-m", "sans script")

        self.assertNotEqual(proc.returncode, 0, _output(proc))
        self.assertIn(MARKER, _output(proc))
        self.assertNotEqual(self._git("rev-parse", "--verify", "HEAD").returncode, 0)


if __name__ == "__main__":
    unittest.main()
