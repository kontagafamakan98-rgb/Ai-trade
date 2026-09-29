"""Tests du hook pre-push : un push n'est autorisé que si les secrets sont sains.

Deux niveaux :

1. la **forme** du hook (présence, bit exécutable, `sh -n`, délégation à
   `scripts/verify_secrets.py`, et le drapeau qui distingue « aucun secret
   configuré » de « secrets malsains ») ;
2. un **vrai** `git push` vers un dépôt nu jetable, qui exerce le wrapper de
   bout en bout :
     * secrets configurés mais incomplets/faibles → push REFUSÉ ;
     * aucun secret configuré → push autorisé (mais annoncé comme non contrôlé) ;
     * `SECRETS_HOOK_STRICT=1` → le gate s'applique même sans secret configuré.

Ce qui décide de l'issue n'est jamais le code de retour **de git** — `git push`
comme `git commit` écrasent à 1 le code de leur hook, un refus et un outillage
mort rendent donc tous les deux 1 — mais la présence d'un **verdict** dans la
sortie. Un push refusé qui n'en rend pas est rejoué ; un refus légitime, lui, en
rend un et n'est jamais rejoué (voir `tests/hook_support.py`). Le code distinct
que les wrappers rendent quand leur outillage ne démarre pas, lui, ne survit que
par invocation directe (`tests/test_hook_tooling_contract.py`).

L'intérêt d'un `git push` réel : une régression du wrapper (mauvais `REPO_ROOT`,
`exec` cassé, mauvais nom de script, drapeau oublié) ne se voit nulle part
ailleurs — les tests purement Python de `verify_secrets` ne lancent jamais git.

**Ce qui décide ne dépend ni du shell appelant, ni de l'heure :**

* l'environnement des sous-processus est **construit** (`_hook_env`) et non
  hérité : les valeurs des secrets connus ne sont pas transmises (le shell peut
  en détenir, ce n'est pas ce que le test mesure), et surtout les **réglages que
  l'audit lit lui-même** — le chemin du registre de rotation
  (`SECRET_ROTATION_LEDGER`) et la durée de vie maximale (`SECRET_MAX_AGE_DAYS`)
  — sont retirés pour que les défauts s'appliquent. Sans cela, un shell qui les
  définit déciderait de l'issue à la place du test : un registre pointé ailleurs
  et daté d'avance ferait échouer le gate « pour cause de temps », dans une suite
  où tous les autres tests auraient écrit ce même registre ;
* la **seule** entrée d'horloge est une *date* : `--record` écrit celle du jour,
  et l'audit compare la sienne à une marge de `DEFAULT_MAX_AGE_DAYS` (90). Un
  passage de minuit entre les deux ne change l'âge que d'un jour — jamais la
  conclusion. La borne, elle, est vérifiée de façon déterministe là où elle se
  teste vraiment : `check_rotation(now=…)` dans `tests/test_secrets_audit.py`.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from core import secrets_audit as sa
from tests.hook_support import (
    COMMIT_ANNOUNCEMENTS,
    COMMIT_VERDICTS,
    PUSH_ANNOUNCEMENTS,
    PUSH_VERDICTS,
    HookRun,
    assert_valid_posix_shell,
    run_hook_or_skip,
    run_or_skip,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SRC = REPO_ROOT / ".githooks" / "pre-push"
HOOKS_DIR = REPO_ROOT / ".githooks"
VERIFY_SRC = REPO_ROOT / "scripts" / "verify_secrets.py"
CLI_SRC = REPO_ROOT / "scripts" / "pre_commit_secrets.py"
CORE_SRC = REPO_ROOT / "core"

#: Sonde : non vide (pour que l'audit s'exécute) mais forcément incomplète — les
#: autres secrets requis resteront absents, donc l'audit échouera.
PROBE_SECRET = "whsec_prepush_probe_9f3a2c7b1d4e6f8a"

#: Jeu de secrets **valides** (présents, robustes, distincts). Il sert à prouver
#: que le refus vient de la ROTATION et non de la robustesse : tant que le
#: registre n'est pas enregistré, le push est bloqué ; après `--record`, il passe.
#: Les valeurs sont manifestement factices et n'apparaissent nulle part ailleurs.
VALID_SECRETS = {
    "WEBHOOK_SECRET": "whsec_pre_push_9f3a2c7b1d4e6f8a",
    "INTERNAL_API_KEY": "intkey_pre_push_5b8e2d1c9a7f",
    "SUPABASE_SERVICE_KEY": "sbp_pre_push_0123456789abcdef",
    "TELEGRAM_BOT_TOKEN": "987654321:AAF_pretend_pre_push_token_9f3a2c7b1d4e",
    # Clé Fernet structurellement valide : base64 urlsafe de 32 octets.
    "ENCRYPTION_KEY": base64.urlsafe_b64encode(bytes(range(32))).decode(),
}

_SECRET_NAMES = frozenset(spec.name for spec in sa.DEFAULT_SPECS)

#: Réglages que **l'audit** lit dans l'environnement (`core/secrets_audit`). Les
#: noms sont lus dans le module et non recopiés : renommer une variable là-bas ne
#: doit pas rouvrir le trou en silence. `SECRETS_HOOK_STRICT` s'y ajoute parce
#: qu'il change la décision du **wrapper** lui-même.
_GATE_ENV = frozenset({sa.LEDGER_PATH_ENV, sa.MAX_AGE_ENV, "SECRETS_HOOK_STRICT"})


def _hook_env(**overrides: str) -> dict:
    """Environnement des hooks : seuls les secrets et les réglages du test décident.

    Deux retraits, pour deux raisons différentes : les **valeurs** des secrets
    connus (le shell en détient, et l'audit y lirait une référence de plus), puis
    les **réglages** du gate (`_GATE_ENV`), pour que la conclusion soit celle que
    le test écrit et non celle que la machine a laissée traîner. Le drapeau
    strict est **posé à vide** plutôt que laissé absent : l'absence dépendrait du
    shell, alors qu'un vide dit explicitement « non » — et un test qui le veut
    l'écrase par `SECRETS_HOOK_STRICT="1"`.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in _SECRET_NAMES and name not in _GATE_ENV
    }
    env["SECRETS_HOOK_STRICT"] = ""
    env.update(overrides)
    return env


_IGNORE = shutil.ignore_patterns("__pycache__")


def _decode(data: bytes) -> str:
    """Décode sans dépendre de la locale (le hook émet « ✅ », « ❌ »…)."""
    return data.decode("utf-8", errors="replace")


def _decode_bytes(data) -> str:
    """`stdout`/`stderr` éventuellement absents (processus non capturé)."""
    return _decode((data or b""))



class HookEnvironmentTest(unittest.TestCase):
    """Ce qui décide du gate, c'est le test — pas le shell qui l'a lancé.

    L'audit lit deux réglages dans son environnement : le chemin du registre de
    rotation et la durée de vie maximale. Hérités, ils décideraient à la place du
    test — un registre pointant ailleurs, daté d'une autre session, ferait
    échouer le gate « pour cause de temps ». Ces tests-là sont volontairement
    purs (aucun `git`, aucun sous-processus) : ils portent sur la construction de
    l'environnement, pas sur le comportement d'un hook.
    """

    def test_the_audit_settings_of_the_shell_are_dropped(self) -> None:
        ambient = {
            sa.LEDGER_PATH_ENV: "/ailleurs/secret_rotation.json",
            sa.MAX_AGE_ENV: "0",
            "SECRETS_HOOK_STRICT": "1",
            "WEBHOOK_SECRET": "whsec_ambiant_9f3a2c7b1d4e6f8a",
        }
        with mock.patch.dict(os.environ, ambient):
            env = _hook_env()

        for name in (sa.LEDGER_PATH_ENV, sa.MAX_AGE_ENV, "WEBHOOK_SECRET"):
            self.assertNotIn(name, env, name)
        self.assertEqual(
            env["SECRETS_HOOK_STRICT"], "", "le strict s'active depuis le test"
        )

    def test_the_dropped_names_are_really_the_ones_the_audit_reads(self) -> None:
        """Sinon on retirerait des variables que personne ne lit."""
        self.assertEqual(
            sa.resolve_ledger_path(environ={sa.LEDGER_PATH_ENV: "ailleurs.json"}),
            "ailleurs.json",
        )
        self.assertEqual(sa.resolve_max_age_days(environ={sa.MAX_AGE_ENV: "7"}), 7)

    def test_the_documented_defaults_apply_once_the_shell_is_out_of_the_way(self) -> None:
        env = _hook_env(WEBHOOK_SECRET=PROBE_SECRET)

        self.assertEqual(sa.resolve_ledger_path(environ=env), sa.DEFAULT_LEDGER_PATH)
        self.assertEqual(sa.resolve_max_age_days(environ=env), sa.DEFAULT_MAX_AGE_DAYS)
        self.assertEqual(env["WEBHOOK_SECRET"], PROBE_SECRET, "le test pose ses secrets")

    def test_the_test_can_still_pin_the_strict_flag(self) -> None:
        self.assertEqual(_hook_env(SECRETS_HOOK_STRICT="1")["SECRETS_HOOK_STRICT"], "1")


class PrePushHookFileTest(unittest.TestCase):
    def test_hook_exists_and_is_executable(self) -> None:
        self.assertTrue(HOOK_SRC.is_file(), f"{HOOK_SRC} manquant")
        self.assertTrue(
            os.access(HOOK_SRC, os.X_OK),
            "le hook doit être exécutable (chmod +x .githooks/pre-push)",
        )

    def test_hook_delegates_to_the_full_audit(self) -> None:
        text = HOOK_SRC.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!"))
        self.assertIn("scripts/verify_secrets.py", text)
        self.assertIn("core.hooksPath", text)  # documente l'installation

    def test_hook_skips_when_nothing_is_configured(self) -> None:
        """Le drapeau est ce qui évite de bloquer un clone sans secret."""
        self.assertIn("--skip-if-unconfigured", HOOK_SRC.read_text(encoding="utf-8"))

    def test_hook_exposes_a_strict_override(self) -> None:
        self.assertIn("SECRETS_HOOK_STRICT", HOOK_SRC.read_text(encoding="utf-8"))

    def test_hook_is_valid_shell(self) -> None:
        assert_valid_posix_shell(self, HOOK_SRC)


@unittest.skipUnless(shutil.which("git"), "git est requis pour le test de bout en bout")
class PrePushE2ETest(unittest.TestCase):
    """Le wrapper est exécuté par git, exactement comme sur une vraie machine."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="prepush-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.remote = self.tmp / "remote.git"
        self.repo.mkdir()

        # --- dépôt minimal : de quoi faire tourner les deux hooks ---
        shutil.copytree(HOOKS_DIR, self.repo / ".githooks", ignore=_IGNORE)
        for name in ("pre-commit", "pre-push"):
            os.chmod(self.repo / ".githooks" / name, 0o755)
        (self.repo / "scripts").mkdir()
        shutil.copy2(VERIFY_SRC, self.repo / "scripts" / "verify_secrets.py")
        shutil.copy2(CLI_SRC, self.repo / "scripts" / "pre_commit_secrets.py")
        shutil.copytree(CORE_SRC, self.repo / "core", ignore=_IGNORE)

        # Dépôt nu servant de remote : un push local ne demande aucune auth.
        self._git("init", "--bare", "-q", str(self.remote), cwd=self.tmp)

        self._git("init", "-q", "-b", "main", cwd=self.repo)
        self._git("config", "user.email", "ci@example.invalid", cwd=self.repo)
        self._git("config", "user.name", "CI", cwd=self.repo)
        self._git("config", "commit.gpgsign", "false", cwd=self.repo)

        install = run_or_skip(
            self,
            [sys.executable, "scripts/pre_commit_secrets.py", "--install",
             "--root", str(self.repo)],
            cwd=str(self.repo), capture_output=True, timeout=120,
        )
        self.assertEqual(
            install.returncode, 0, "installation des hooks échouée :\n" + _decode_bytes(install.stdout) + _decode_bytes(install.stderr)
        )
        self._git("remote", "add", "origin", str(self.remote), cwd=self.repo)

        # Commit initial : passe par le hook pre-commit (sans secret → toléré).
        # Il exécute donc un hook, et peut mourir sans verdict sous MSYS2 : même
        # contrat que les tests eux-mêmes, sinon la panne tomberait dans le
        # `setUp` — le pire endroit, puisque l'erreur n'y nomme pas la cause.
        (self.repo / "README.md").write_text("bonjour\n", encoding="utf-8")
        self._git("add", "README.md", cwd=self.repo)
        initial = run_hook_or_skip(
            self,
            ["git", "commit", "-m", "init"],
            verdicts=COMMIT_VERDICTS,
            announcements=COMMIT_ANNOUNCEMENTS,
            cwd=str(self.repo),
            env=self._env(),
            timeout=120,
        )
        self.assertEqual(
            initial.proc.returncode, 0, "le commit initial a échoué :\n" + initial.output
        )

    def _env(self, **overrides: str):
        """Environnement du test (voir `_hook_env`) : secrets et réglages posés ici."""
        return _hook_env(**overrides)

    def _git(
        self, *args: str, cwd: Path, env=None, check: bool = True
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
            cwd=str(cwd),
            env=env,
            capture_output=True,
            timeout=120,
            check=check,
        )

    def _push_with_hook(self, env) -> HookRun:
        """Pousse et exige que le hook ait **décidé**, pas seulement démarré.

        Un push **refusé** qui ne rend pas de verdict n'a pas conclu : son
        outillage est mort avant (`cygheap read copy failed`, `cd: null
        directory`), et le push est rejoué — sans quoi ce test rougirait au
        hasard de la santé du `fork` de la machine. Un refus **légitime**, lui,
        rend son verdict et n'est jamais rejoué.

        Un push **accepté** n'a pas de verdict à rendre : le hook a laissé passer,
        et rien ne le dit dans sa sortie. `started` (la bannière, sur la sortie
        d'erreur — celle que git relaie toujours) est alors la seule preuve que le
        hook a tourné. Un succès n'est jamais rejoué pour autant : le rejouer ne
        produirait qu'un « everything up-to-date », qui ne prouve rien de plus.

        Un **transport mort avant le hook** est de la même famille, et c'est le
        hoquet qui a été capturé ici : git lit les références du dépôt distant
        *avant* d'appeler `pre-push`, donc un `fatal: Could not read from remote
        repository` sans cause nommée ne dit rien du hook — le harnais le
        réessaie puis passe le test (voir `tests/hook_support.py`). Quand git
        nomme la cause, c'est l'échafaudage du test qui est en faute, et l'échec
        se voit.
        """
        return run_hook_or_skip(
            self,
            ["git", "push", "origin", "main"],
            verdicts=PUSH_VERDICTS,
            announcements=PUSH_ANNOUNCEMENTS,
            cwd=str(self.repo),
            env=env,
            timeout=120,
        )
    def _remote_has_main(self) -> bool:
        proc = run_or_skip(
            self,
            ["git", "--git-dir", str(self.remote), "rev-parse", "--verify", "refs/heads/main"],
            capture_output=True,
            timeout=60,
        )
        return proc.returncode == 0

    # ------------------------------------------------------------------ #

    def test_push_with_unhealthy_secrets_is_blocked(self) -> None:
        run = self._push_with_hook(self._env(WEBHOOK_SECRET=PROBE_SECRET))

        self.assertTrue(run.refused, run.output)
        self.assertNotEqual(run.proc.returncode, 0, "le push aurait dû être refusé")
        self.assertFalse(self._remote_has_main(), "aucune ref ne doit avoir été poussée")

    def test_push_without_configured_secrets_is_allowed(self) -> None:
        run = self._push_with_hook(self._env())

        self.assertEqual(run.proc.returncode, 0, run.output)
        # Git avale la conclusion d'un hook qui réussit : c'est la bannière, sur
        # la sortie d'erreur (toujours relayée), qui prouve que le hook a tourné
        # — sans elle, ce test passerait aussi sur une machine où il est ignoré.
        self.assertTrue(run.started, "le hook doit avoir démarré :\n" + run.output)
        self.assertFalse(run.refused, run.output)
        self.assertTrue(self._remote_has_main(), "le push propre doit atteindre le remote")

    def test_push_is_gated_on_rotation_health(self) -> None:
        """Cœur du gate : mêmes secrets, seule la rotation change l'issue.

        Secrets robustes mais jamais enregistrés → refus (et c'est bien la
        **rotation** qui bloque, pas la robustesse). Après `--record` → autorisé.

        L'horloge n'y entre que par une **date** (`--record` écrit celle du jour,
        l'audit compare la sienne à 90 jours de marge) : un passage de minuit
        entre les deux sous-processus ne change l'âge que d'un jour, donc jamais
        la conclusion. Le contrôle ci-dessous rend cette hypothèse **visible**
        — et il échouerait si `--record` écrivait une date d'un autre jour.
        """
        env = self._env(**VALID_SECRETS)

        blocked = self._push_with_hook(env)
        self.assertTrue(blocked.refused, blocked.output)
        self.assertNotEqual(
            blocked.proc.returncode, 0, "rotation absente : push attendu refusé"
        )
        self.assertIn("rotation", blocked.output, blocked.output)
        self.assertFalse(self._remote_has_main())
        for value in VALID_SECRETS.values():
            self.assertNotIn(
                value, blocked.output, "l'audit ne doit jamais ré-afficher une valeur"
            )

        # Le geste réel après avoir défini/roté les secrets.
        record = run_or_skip(
            self,
            [sys.executable, "scripts/verify_secrets.py", "--record"],
            cwd=str(self.repo), env=env, capture_output=True, timeout=120,
        )
        self.assertEqual(
            record.returncode, 0,
            "`--record` a échoué :\n"
            + _decode_bytes(record.stdout)
            + _decode_bytes(record.stderr),
        )

        ledger = json.loads(
            (self.repo / sa.DEFAULT_LEDGER_PATH).read_text(encoding="utf-8")
        )
        ages = {
            name: (date.today() - date.fromisoformat(entry["rotated_at"])).days
            for name, entry in ledger["secrets"].items()
        }
        self.assertEqual(sorted(ages), sorted(VALID_SECRETS), "tous les secrets sont enregistrés")
        self.assertTrue(
            all(0 <= age <= 1 for age in ages.values()),
            f"enregistré aujourd'hui (ou hier, si minuit est passé entre-temps) : {ages}",
        )
        self.assertLess(
            max(ages.values()),
            sa.DEFAULT_MAX_AGE_DAYS,
            "la marge du défaut est ce qui rend le passage de minuit inoffensif",
        )

        allowed = self._push_with_hook(env)
        self.assertEqual(
            allowed.proc.returncode,
            0,
            "rotation enregistrée : push attendu accepté\n" + allowed.output,
        )
        self.assertTrue(self._remote_has_main(), "après rotation enregistrée, le push passe")

    def test_strict_mode_blocks_even_when_unconfigured(self) -> None:
        run = self._push_with_hook(self._env(SECRETS_HOOK_STRICT="1"))

        self.assertTrue(run.refused, run.output)
        self.assertNotEqual(run.proc.returncode, 0, "SECRETS_HOOK_STRICT=1 doit imposer le gate")
        self.assertFalse(self._remote_has_main())


if __name__ == "__main__":
    unittest.main()
