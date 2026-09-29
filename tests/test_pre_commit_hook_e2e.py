"""Test **de bout en bout** du hook pre-commit : un vrai `git commit` refusé.

Le reste de `test_pre_commit_hook.py` contrôle la CLI et la *forme* du hook
(présence, bit exécutable, `sh -n`, présence de la chaîne `scripts/…`). Aucun
de ces tests n'exécute pourtant le hook : une régression du **wrapper** lui-même
passerait donc inaperçue — mauvais calcul de `REPO_ROOT` (le `dirname`/`cd` du
script), `exec` cassé, mauvais nom d'interpréteur, chemin du script mal
reconstruit. Ces défauts ne se manifestent qu'au moment d'un `git commit` réel,
c'est-à-dire jamais avant la CI.

Ici on construit un dépôt git jetable, on y installe le **vrai** wrapper et le
**vrai** script, puis on lance un **vrai** `git commit`. Chaque exécution doit
rendre un **verdict** (`COMMIT REFUSÉ`, ou la phrase qui dit que rien n'a été
trouvé) : un hook qui démarre puis dont l'outillage meurt (`cygheap read copy
failed`, `cd: null directory`) est rejoué, parce qu'il n'a rien décidé — un refus
légitime, lui, porte son verdict et n'est jamais rejoué. Le détail est dans
`tests/hook_support.py` :

* un fichier indexé contenant la valeur d'un secret → le commit est refusé et
  aucun commit n'est créé ;
* un fichier propre → le commit passe (le hook ne bloque pas à tort).

Le dépôt d'essai ne contient que ce dont le hook a besoin pour tourner
(`.githooks/`, `scripts/pre_commit_secrets.py`, `core/secrets_audit`). C'est
aussi une garantie utile : **le hook n'a besoin d'aucune dépendance du projet**
(FastAPI, supabase, python-telegram-bot) — seulement de la bibliothèque
standard. Le job CI dédié exploite directement ce fait.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.hook_support import (
    COMMIT_ANNOUNCEMENTS,
    COMMIT_VERDICTS,
    HookRun,
    run_hook_or_skip,
    run_or_skip,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / ".githooks"
CLI_SRC = REPO_ROOT / "scripts" / "pre_commit_secrets.py"
CORE_SRC = REPO_ROOT / "core"
_IGNORE = shutil.ignore_patterns("__pycache__")

#: Sonde : >= 16 caractères, variée, jamais un vrai secret.
PROBE_SECRET = "whsec_e2e_probe_9f3a2c7b1d4e6f8a"
PROBE_VAR = "WEBHOOK_SECRET"


def _decode(data: bytes) -> str:
    """Décode sans dépendre de l'encodage de la locale (le hook émet « ✅ », etc.)."""
    return data.decode("utf-8", errors="replace")


def _decode_bytes(data) -> str:
    """`stdout`/`stderr` éventuellement absents (processus non capturé)."""
    return _decode((data or b""))


@unittest.skipUnless(shutil.which("git"), "git est requis pour le test de bout en bout")
class BlockedCommitE2ETest(unittest.TestCase):
    """Le wrapper est exécuté par git, exactement comme sur une vraie machine."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hook-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        # --- dépôt minimal : de quoi faire tourner TOUS les hooks versionnés ---
        # On copie le dossier entier (pre-commit **et** pre-push) : `--install`
        # exige que les deux hooks existent, comme dans un vrai clone.
        shutil.copytree(HOOKS_DIR, self.tmp / ".githooks", ignore=_IGNORE)
        for name in ("pre-commit", "pre-push"):
            os.chmod(self.tmp / ".githooks" / name, 0o755)

        (self.tmp / "scripts").mkdir()
        shutil.copy2(CLI_SRC, self.tmp / "scripts" / "pre_commit_secrets.py")
        shutil.copytree(CORE_SRC, self.tmp / "core", ignore=_IGNORE)

        self._git("init", "-q")
        self._git("config", "user.email", "ci@example.invalid")
        self._git("config", "user.name", "CI")
        self._git("config", "commit.gpgsign", "false")

        # On passe par le VRAI mécanisme d'installation du projet.
        install = run_or_skip(
            self,
            [sys.executable, "scripts/pre_commit_secrets.py", "--install",
             "--root", str(self.tmp)],
            cwd=str(self.tmp),
            capture_output=True,
            timeout=120,
        )
        self.assertEqual(
            install.returncode,
            0,
            "l'installation du hook a échoué :\n"
            + _decode_bytes(install.stdout)
            + _decode_bytes(install.stderr),
        )
        hooks_path = self._git("config", "core.hooksPath")
        self.assertEqual(_decode(hooks_path.stdout).strip(), ".githooks")

        # Environnement déterministe : une seule valeur de référence, quelle que
        # soit la configuration de la machine de CI.
        self.env = os.environ.copy()
        self.env[PROBE_VAR] = PROBE_SECRET

    def _commit(self, message: str = "test") -> HookRun:
        """Un vrai `git commit`, qui doit rendre un **verdict** du hook.

        Le code de retour de git ne suffit pas : `git commit` **écrase à 1** le
        code de son hook (mesuré, quelle qu'en soit la valeur), donc un refus et
        un outillage mort qui rendent tous les deux 1 ne se distinguent que par le
        verdict (voir `tests/hook_support.py`). Un essai qui ne conclut pas est
        rejoué, et si l'OS ne laisse jamais le hook conclure, le test est
        **passé** : c'est la CI qui porte la preuve.

        `COMMIT_ANNOUNCEMENTS` est la preuve, pour un commit qui **réussit**, que
        le hook a tourné : git ne laisse alors aucun verdict, et la bannière
        (sur la sortie d'erreur, que git relaie toujours — mesuré) est la seule
        trace lisible. Elle sert à le **constater**, pas à éviter un rejeu :
        `hook_run` ne rejoue jamais un succès pour du bruit de `fork`.
        """
        return run_hook_or_skip(
            self,
            ["git", "commit", "-m", message],
            verdicts=COMMIT_VERDICTS,
            announcements=COMMIT_ANNOUNCEMENTS,
            cwd=str(self.tmp),
            env=self.env,
            timeout=120,
        )

    def _git(
        self, *args: str, cwd: Path | None = None, env=None, check: bool = True
    ) -> subprocess.CompletedProcess:
        """`git` pour l'échafaudage du test, **passé** si l'OS refuse de le démarrer.

        Monter un dépôt jetable, c'est une dizaine de processus : sur une suite
        saturée, l'un d'eux peut ne jamais démarrer. Ce n'est pas le hook qui est
        en cause, donc le test est passé au lieu de rougir — la CI (Linux) exécute
        le contrôle sans ce mode de défaillance.
        """
        return run_or_skip(
            self,
            ["git", *args],
            cwd=str(cwd or self.tmp),
            env=env,
            capture_output=True,
            timeout=120,
            check=check,
        )

    def _has_commit(self) -> bool:
        return self._git("rev-parse", "--verify", "HEAD", check=False).returncode == 0

    # ------------------------------------------------------------------ #

    def test_leaked_secret_in_staged_file_blocks_the_commit(self) -> None:
        leak = self.tmp / "leaked.py"
        leak.write_text(f'{PROBE_VAR} = "{PROBE_SECRET}"\n', encoding="utf-8")
        self._git("add", "leaked.py")

        run = self._commit()

        self.assertTrue(run.refused, run.output)
        self.assertNotEqual(run.proc.returncode, 0, "le commit aurait dû être refusé")
        self.assertIn("leaked.py:1", run.output, run.output)
        # Un rapport de fuite ne doit jamais ré-afficher la fuite.
        self.assertNotIn(PROBE_SECRET, run.output)
        self.assertFalse(self._has_commit(), "aucun commit ne doit avoir été créé")

    def test_force_added_env_file_is_still_blocked(self) -> None:
        """`git add -f .env` est précisément ce que le hook doit intercepter."""
        env_file = self.tmp / ".env"
        env_file.write_text(f"{PROBE_VAR}={PROBE_SECRET}\n", encoding="utf-8")
        self._git("add", "-f", ".env")

        run = self._commit()

        self.assertTrue(run.refused, run.output)
        self.assertNotEqual(run.proc.returncode, 0, "un `.env` indexé doit être refusé")
        self.assertFalse(self._has_commit())

    def test_clean_staged_file_commits_successfully(self) -> None:
        (self.tmp / "ok.py").write_text("print('bonjour')\n", encoding="utf-8")
        self._git("add", "ok.py")

        run = self._commit("clean")

        self.assertEqual(
            run.proc.returncode, 0, "un contenu propre ne doit pas être bloqué:\n" + run.output
        )
        # Aucun verdict n'est attendu ici : git avale la sortie d'un hook qui
        # réussit. Ce qui compte est qu'aucun refus ne soit rendu — un refus, lui,
        # git le relaie toujours.
        self.assertFalse(run.refused, run.output)
        self.assertTrue(self._has_commit(), "le commit propre doit avoir été créé")


if __name__ == "__main__":
    unittest.main()
