"""Contrat de `tests/hook_support.py` : « conclu » et « l'outillage est mort » ne se confondent pas.

Le mode de défaillance à éliminer est celui-ci, observé sous MSYS2 : le hook
démarre, son `dirname` meurt (`cygheap read copy failed` → `cd: null directory`),
et git rend **1 sans un mot**. Ce code de retour est celui d'un refus légitime,
donc les tests des hooks ne peuvent pas décider sur lui.

La doublure utilisée ici **compte ses propres exécutions** dans un fichier : un
rejeu devient ainsi visible, au lieu d'être une hypothèse. Elle n'utilise ni git,
ni `sh` — seulement l'interpréteur courant — pour que le contrat soit vérifié
même sur une machine où ces outils manquent.

Deux propriétés, opposées et toutes les deux tenues :

* un **verdict** n'est jamais rejoué : rejouer un refus ne changerait pas sa
  conclusion, et le prendre pour une machine fatiguée laisserait passer une fuite
  de secret ;
* une **mort d'outillage** ne devient jamais un verdict : sinon la suite
  prétendrait vérifier un comportement que le hook n'a jamais eu.

Une troisième propriété concerne l'invocation **directe** d'un wrapper
(`run_wrapper_conclusion`) : quand l'enfant du wrapper n'a pas été lancé — `fork`
saturé, le scan rend 127, et le wrapper conclut honnêtement `3` — le test est
rejoué puis **passé**. Sans quoi la machine, et non le hook, déciderait du verdict.
Ce repli ne se déclenche que sur le bruit du runtime (`_RUNTIME_NOISE`) : un `3`
sans bruit reste une conclusion, et un refus reste un refus.
"""

import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.hook_support import (
    COMMIT_ANNOUNCEMENTS,
    COMMIT_VERDICTS,
    HOOK_TOOLING_EXIT,
    PUSH_ANNOUNCEMENTS,
    HookNeverConcluded,
    HookRun,
    hook_run,
    hook_wrapper,
    run_hook_or_skip,
    run_or_skip,
    run_wrapper_conclusion,
)

#: Une doublure de hook : elle compte ses exécutions, puis fait ce que le test
#: lui demande. Le compteur est la preuve qu'un essai a bien été rejoué (ou pas).
_SCRIPT = (
    "import pathlib, sys\n"
    "counter = pathlib.Path(sys.argv[1])\n"
    "counter.write_text((counter.read_text() if counter.exists() else '') + 'x')\n"
    "runs = len(counter.read_text())\n"
    "{body}\n"
)

#: Ce que le runtime MSYS2 laisse derrière lui quand `fork` échoue.
_CYGWIN_DEATH = "child_copy: cygheap read copy failed"


class _Skipped(Exception):
    """Le `skipTest` d'un `TestCase`, capturé au lieu d'arrêter la suite."""


class _Recorder:
    """Assez d'un `TestCase` pour `run_hook_or_skip` : de quoi voir le `skipTest`."""

    def __init__(self) -> None:
        self.skipped: list = []

    def skipTest(self, reason: str) -> None:
        self.skipped.append(reason)
        raise _Skipped(reason)


class HookRunContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="hook-support-")
        self.addCleanup(self.directory.cleanup)
        self.counter = pathlib.Path(self.directory.name) / "runs"

    def command(self, body: str) -> list:
        """Une commande qui compte ses exécutions, puis exécute `body`."""
        return [sys.executable, "-c", _SCRIPT.format(body=body), str(self.counter)]

    def count(self) -> int:
        return len(self.counter.read_text()) if self.counter.exists() else 0

    # --- le verdict est ce qui décide, jamais le code de retour ------------- #

    def test_a_refusal_is_returned_as_is_and_never_replayed(self) -> None:
        run = hook_run(self.command('print("COMMIT REFUSÉ"); raise SystemExit(1)'))

        self.assertTrue(run.refused)
        self.assertEqual(run.attempts, 1)
        self.assertEqual(self.count(), 1, "un verdict ne se rejoue pas")
        self.assertEqual(run.proc.returncode, 1)

    def test_a_refusal_that_comes_after_an_incident_is_honoured(self) -> None:
        """Le cas réel : le premier essai meurt, le second conclut au refus.

        Le refus doit être **rendu** (et pas rejoué une troisième fois), et c'est
        bien lui qui décide — l'incident du premier essai ne l'efface pas.
        """
        run = hook_run(
            self.command(
                f"if runs == 1:\n"
                f"    print({_CYGWIN_DEATH!r})\n"
                f"    raise SystemExit(1)\n"
                f'print("COMMIT REFUSÉ")\n'
                f"raise SystemExit(1)"
            )
        )

        self.assertTrue(run.refused)
        self.assertEqual(run.attempts, 2)
        self.assertEqual(self.count(), 2)
        self.assertIsNone(run.incident, "un verdict efface la trace d'incident")

    def test_a_refusal_is_recognised_without_any_code_of_return(self) -> None:
        """La règle qui interdit la confusion : `refused` exige un verdict."""
        silent = HookRun(
            proc=subprocess.CompletedProcess([], 1),
            output="",
            verdict=None,
            incident=None,
            attempts=3,
        )
        refused = HookRun(
            proc=subprocess.CompletedProcess([], 1),
            output="DÉPLOIEMENT REFUSÉ",
            verdict="DÉPLOIEMENT REFUSÉ",
            incident=None,
            attempts=1,
        )
        self.assertFalse(silent.refused, "un code 1 sans verdict n'est pas un refus")
        self.assertFalse(silent.concluded)
        self.assertTrue(refused.refused)
        self.assertTrue(refused.concluded)

    # --- l'incident d'OS est rejoué, et finit par passer le test ------------ #

    def test_a_cygwin_death_is_retried_until_the_attempts_run_out(self) -> None:
        with self.assertRaises(HookNeverConcluded) as caught:
            hook_run(
                self.command(f'print({_CYGWIN_DEATH!r}); raise SystemExit(1)'),
                attempts=3,
            )

        self.assertEqual(caught.exception.run.attempts, 3)
        self.assertEqual(self.count(), 3)
        self.assertIsNotNone(caught.exception.run.incident)
        self.assertIn(_CYGWIN_DEATH, caught.exception.run.output)
        self.assertIsNone(caught.exception.run.verdict)

    def test_a_silent_death_is_retried_too(self) -> None:
        """Sortie vide et git qui échoue : le wrapper écrit toujours quand il tourne."""
        with self.assertRaises(HookNeverConcluded) as caught:
            hook_run(self.command("raise SystemExit(1)"), attempts=2)

        self.assertEqual(caught.exception.run.attempts, 2)
        self.assertEqual(caught.exception.run.output, "")
        self.assertEqual(self.count(), 2)

    def test_a_success_without_a_verdict_is_normal_and_not_replayed(self) -> None:
        """Git avale la sortie d'un hook qui **réussit** : rien à rejouer, rien à exiger.

        Le hook a bien conclu (il a laissé passer), mais sa phrase n'arrive jamais
        jusqu'ici. La prendre pour un incident ferait échouer — ou pire, rejouer
        un push déjà passé — sur une machine parfaitement juste.
        """
        run = hook_run(self.command("raise SystemExit(0)"))

        self.assertEqual(run.proc.returncode, 0)
        self.assertFalse(run.concluded)
        self.assertFalse(run.refused)
        self.assertEqual(self.count(), 1, "un succès ne se rejoue pas")

    def test_git_that_cannot_spawn_the_hook_is_retried(self) -> None:
        """`cannot spawn` sur une commande que git laisse passer : incident, pas verdict.

        Git avertit puis continue : la commande réussit sans que le hook ait
        tourné. C'est le **seul** cas de succès qui se rejoue — et ce n'est pas du
        bruit : git dit lui-même que rien n'a été exercé, donc le succès ne prouve
        rien. Tout autre incident sur une commande réussie est ignoré (voir
        `test_a_success_is_not_replayed_for_runtime_noise_alone`).
        """
        with self.assertRaises(HookNeverConcluded) as caught:
            hook_run(
                self.command('print("cannot spawn .githooks/pre-push", file=sys.stderr)'),
                attempts=2,
            )

        self.assertEqual(caught.exception.run.attempts, 2)
        self.assertEqual(caught.exception.run.proc.returncode, 0)
        self.assertFalse(caught.exception.run.started)

    def test_a_transport_that_died_before_the_hook_is_an_incident(self) -> None:
        """Le hoquet résiduel de `test_push_is_gated_on_rotation_health`.

        Mesuré sur une suite saturée : le `push` échoue sur `fatal: Could not read
        from remote repository` **sans que le hook ait tourné** — git lit les
        références du distant avant d'appeler `pre-push`, donc son transport est
        mort avant le hook. Le test attendait un refus et rougissait : le verdict
        n'existe pas, et l'assertion mesurait la machine. C'est un incident, donc
        un rejeu, puis un test **passé**.
        """
        recorder = _Recorder()
        transport = (
            'print("fatal: Could not read from remote repository.", file=sys.stderr); '
            'print("", file=sys.stderr); '
            "raise SystemExit(128)"
        )

        with self.assertRaises(_Skipped):
            run_hook_or_skip(recorder, self.command(transport), attempts=3)

        self.assertEqual(self.count(), 3, "un incident se réessaie avant de passer le test")
        self.assertEqual(len(recorder.skipped), 1)
        self.assertIn("Could not read from remote repository", recorder.skipped[0])

    def test_a_transport_whose_cause_is_named_is_not_absorbed(self) -> None:
        """Quand git **nomme** la cause, c'est l'échafaudage du test qui est en faute.

        Un dépôt distant qui n'existe pas n'est pas une machine fatiguée : le
        passer sous silence masquerait un test qui pointe ailleurs.
        """
        named = (
            'print("fatal: \'../absent.git\' does not appear to be a git repository", '
            'file=sys.stderr); '
            'print("fatal: Could not read from remote repository.", file=sys.stderr); '
            "raise SystemExit(128)"
        )
        run = hook_run(self.command(named))

        self.assertEqual(run.attempts, 1, "ce n'est pas un incident d'OS")
        self.assertEqual(self.count(), 1)
        self.assertIsNone(run.incident)
        self.assertFalse(run.concluded)
        self.assertIn("does not appear", run.output)

    def test_a_trace_of_incident_on_a_success_is_only_noise(self) -> None:
        """Le hook a démarré **et** réussi : la trace de fork ne le rejoue pas."""
        run = hook_run(
            self.command(
                f'print("hook démarré", file=sys.stderr)\n'
                f'print({_CYGWIN_DEATH!r}, file=sys.stderr)'
            ),
            announcements=("hook démarré",),
        )

        self.assertTrue(run.started)
        self.assertEqual(run.proc.returncode, 0)
        self.assertEqual(self.count(), 1)

    def test_a_success_is_not_replayed_for_runtime_noise_alone(self) -> None:
        """Le cas qui a échoué en suite complète et jamais seul.

        Une commande qui **réussit** en laissant une trace de `fork` sur sa sortie
        — sans bannière, parce que rien ne prouve ici qu'un hook a tourné — ne se
        rejoue pas : rejouer un succès ne peut que **fabriquer un échec** (un
        `git commit` rejoué trouve « rien à committer », un `git push` rejoué
        « everything up-to-date »). C'est le bruit de fork, plus fréquent sur une
        suite complète saturée, qui avait transformé ce succès en échec.
        """
        run = hook_run(
            self.command(f'print({_CYGWIN_DEATH!r}, file=sys.stderr)'),
            announcements=("hook démarré",),
        )

        self.assertEqual(run.proc.returncode, 0)
        self.assertFalse(run.started, "aucune bannière : rien ne prouve qu'un hook a tourné")
        self.assertIsNotNone(run.incident, "la trace de fork est bien vue")
        self.assertEqual(self.count(), 1, "un succès ne se rejoue pas pour du bruit")

    def test_the_windows_loader_code_is_an_incident_even_with_a_message(self) -> None:
        """`0xC0000142` (`STATUS_DLL_INIT_FAILED`) n'est pas un code de retour."""
        with self.assertRaises(HookNeverConcluded) as caught:
            hook_run(
                self.command('print("un mot"); raise SystemExit(3221225794)'), attempts=2
            )

        self.assertEqual(caught.exception.run.attempts, 2)
        self.assertEqual(self.count(), 2)

    def test_the_wrapper_tooling_code_is_an_incident_even_when_it_speaks(self) -> None:
        """Le seul incident qui **parle** : c'est donc le code qui doit décider.

        Un wrapper qui n'a pas pu démarrer son outillage écrit son message — la
        règle « aucune sortie » le laisserait passer pour un verdict. Le code
        distinct est ce qui rétablit la distinction, sans lire un mot.
        """
        with self.assertRaises(HookNeverConcluded) as caught:
            hook_run(
                self.command(
                    'print("pre-commit: ⚠️ outillage NON DÉMARRÉ", file=sys.stderr); '
                    f"raise SystemExit({HOOK_TOOLING_EXIT})"
                ),
                attempts=2,
            )

        self.assertEqual(caught.exception.run.attempts, 2, "un incident se rejoue")
        self.assertEqual(self.count(), 2)
        self.assertEqual(caught.exception.run.proc.returncode, HOOK_TOOLING_EXIT)
        self.assertIsNone(caught.exception.run.verdict)
        self.assertIsNone(
            caught.exception.run.incident, "décidé par le code, sans marqueur textuel"
        )

    # --- l'échafaudage d'un test de bout en bout --------------------------- #

    def test_the_os_refusing_to_start_the_plumbing_passes_the_test(self) -> None:
        """Un `git` qui n'a jamais démarré n'a rien à voir avec le hook testé."""
        recorder = _Recorder()

        with self.assertRaises(_Skipped):
            run_or_skip(
                recorder, self.command("raise SystemExit(3221225794)"), capture_output=True
            )

        self.assertEqual(len(recorder.skipped), 1)
        self.assertIn("n'a pas pu démarrer", recorder.skipped[0])

    def test_a_plumbing_failure_that_speaks_is_not_absorbed(self) -> None:
        """`git` a tourné et a échoué : c'est un échec, avec sa sortie."""
        recorder = _Recorder()
        proc = run_or_skip(
            recorder,
            self.command('print("fatal: pas un dépôt"); raise SystemExit(1)'),
            capture_output=True,
        )

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(recorder.skipped, [], "rien à passer ici")

    def test_the_plumbing_still_honours_check(self) -> None:
        recorder = _Recorder()
        with self.assertRaises(subprocess.CalledProcessError):
            run_or_skip(
                recorder, self.command("raise SystemExit(1)"), check=True, capture_output=True
            )
        self.assertEqual(recorder.skipped, [])

    def test_a_broken_wrapper_is_not_retried(self) -> None:
        """Une erreur du wrapper n'est pas un incident d'OS : elle doit se voir.

        Le garde-fou contre le remède pire que le mal : trois reprises
        masqueraient une régression du hook, et la suite prétendrait l'avoir vue
        tourner.
        """
        run = hook_run(
            self.command('print("pre-commit: syntax error", file=sys.stderr); raise SystemExit(2)')
        )

        self.assertEqual(run.attempts, 1)
        self.assertEqual(self.count(), 1)
        self.assertFalse(run.concluded)
        self.assertIsNone(run.incident)
        self.assertIn("syntax error", run.output)

    def test_the_skip_names_the_cause_and_the_attempts(self) -> None:
        recorder = _Recorder()

        with self.assertRaises(_Skipped):
            run_hook_or_skip(
                recorder,
                self.command(f'print({_CYGWIN_DEATH!r}); raise SystemExit(1)'),
                attempts=2,
            )

        self.assertEqual(self.count(), 2, "on réessaie avant de passer le test")
        self.assertEqual(len(recorder.skipped), 1)
        self.assertIn("MSYS2", recorder.skipped[0])
        self.assertIn("2 essai(s)", recorder.skipped[0])

    def test_a_concluded_run_is_not_skipped(self) -> None:
        recorder = _Recorder()
        run = run_hook_or_skip(recorder, self.command('print("COMMIT REFUSÉ")'))

        self.assertEqual(recorder.skipped, [])
        self.assertTrue(run.refused)

    # --- invoquer un wrapper directement : son code survit, l'incident aussi -- #

    def fake_hook(self, body: str) -> pathlib.Path:
        """Un faux wrapper, en shell pur : ce que le vrai rend, sans ses enfants.

        Le chemin du compteur est passé en séparateurs POSIX : sous `sh`, un
        antislash n'a rien à faire dans un chemin Windows.
        """
        path = pathlib.Path(self.directory.name) / "faux-hook"
        counter = str(self.counter).replace("\\", "/")
        path.write_text(
            f'#!/bin/sh\nprintf x >> "{counter}"\n{body}\n',
            encoding="utf-8",
            newline="\n",
        )
        path.chmod(0o755)
        return path

    def test_an_honest_tooling_exit_is_kept(self) -> None:
        """Un `3` sans bruit de runtime est une **conclusion** du wrapper."""
        recorder = _Recorder()
        hook = self.fake_hook('echo "outillage NON DÉMARRÉ" >&2; exit 3')

        proc = run_wrapper_conclusion(recorder, hook)

        self.assertEqual(proc.returncode, HOOK_TOOLING_EXIT)
        self.assertEqual(recorder.skipped, [])
        self.assertEqual(self.count(), 1, "une conclusion ne se rejoue pas")

    def test_a_refusal_with_runtime_noise_is_not_absorbed(self) -> None:
        """Le bruit ne requalifie pas un refus : le code décide, et il décide 1."""
        recorder = _Recorder()
        hook = self.fake_hook(f'echo "{_CYGWIN_DEATH}" >&2; exit 1')

        proc = run_wrapper_conclusion(recorder, hook)

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(recorder.skipped, [])
        self.assertEqual(self.count(), 1)

    def test_a_launch_incident_is_retried_then_passes_the_test(self) -> None:
        """Le cas réel : le wrapper a raison, mais son enfant n'a jamais tourné.

        Mesuré sur une suite complète : `sh` meurt (`child_copy: cygheap read
        copy failed`), le scan rend 127, et le wrapper conclut `3`. Le test ne
        doit ni rougir pour si peu, ni prétendre avoir exercé son scénario.
        """
        recorder = _Recorder()
        hook = self.fake_hook(f'echo "{_CYGWIN_DEATH}" >&2; exit {HOOK_TOOLING_EXIT}')

        with self.assertRaises(_Skipped):
            run_wrapper_conclusion(recorder, hook, attempts=3)

        self.assertEqual(self.count(), 3, "on réessaie avant de passer le test")
        self.assertEqual(len(recorder.skipped), 1)
        self.assertIn("MSYS2", recorder.skipped[0])
        self.assertIn("3 essai(s)", recorder.skipped[0])
        self.assertIn("child_copy", recorder.skipped[0], "le skip nomme l'incident lu")

    def test_a_wrapper_that_never_started_is_retried_then_passes_the_test(self) -> None:
        """L'autre forme de l'incident : le wrapper lui-même n'a pas démarré.

        `0xC0000142` (`STATUS_DLL_INIT_FAILED`) ne sort d'aucun `exit` de shell —
        un code de retour est tronqué à 8 bits — donc la seule façon de l'éprouver
        est de le fabriquer : c'est le chargeur Windows qui le rend.
        """
        recorder = _Recorder()
        never = subprocess.CompletedProcess([], 3221225794, b"", b"")

        with mock.patch("tests.hook_support.run_wrapper", return_value=never) as patched:
            with self.assertRaises(_Skipped):
                run_wrapper_conclusion(recorder, pathlib.Path("faux-hook"), attempts=2)

        self.assertEqual(patched.call_count, 2, "un lancement raté se réessaie")
        self.assertEqual(len(recorder.skipped), 1)
        self.assertIn("n'a pas pu démarrer", recorder.skipped[0])

    # --- la doublure elle-même --------------------------------------------- #

    def test_the_verdict_vocabulary_is_the_one_of_the_tools(self) -> None:
        """Les marqueurs se lisent dans les scripts, ils ne s'inventent pas ici."""
        root = pathlib.Path(__file__).resolve().parents[1]
        pre_commit = (root / "scripts" / "pre_commit_secrets.py").read_text(encoding="utf-8")
        verify = (root / "scripts" / "verify_secrets.py").read_text(encoding="utf-8")

        self.assertIn("COMMIT REFUSÉ", pre_commit)
        self.assertIn("Aucune valeur de secret dans", pre_commit)
        self.assertIn("DÉPLOIEMENT REFUSÉ", verify)
        self.assertIn("Tous les secrets sont présents", verify)
        self.assertIn("AUCUN contrôle n'a eu lieu", verify)
        self.assertTrue(
            set(COMMIT_VERDICTS) <= {"COMMIT REFUSÉ", "Aucune valeur de secret dans", "aucun fichier indexé"}
        )

    def test_the_hook_tests_share_this_helper_instead_of_their_own_retry(self) -> None:
        """Deux boucles de reprise écrites séparément finiraient par diverger."""
        root = pathlib.Path(__file__).resolve().parents[1]
        for name in ("test_pre_commit_hook_e2e.py", "test_pre_push_hook.py"):
            text = (root / "tests" / name).read_text(encoding="utf-8")
            self.assertIn("run_hook_or_skip", text, f"{name} doit passer par le verdict partagé")
            self.assertNotIn("REFUSÉ\" in output", text, f"{name} ne doit plus lire le texte à la main")
            self.assertNotIn("_HOOK_BANNER", text)

    def test_the_commit_paths_ask_for_the_startup_banner(self) -> None:
        """Sans bannière, un commit qui **réussit** serait rejoué sur une trace de fork.

        Git avale la sortie d'un hook qui passe : la bannière est sa seule preuve
        de vie, et sans elle le second essai trouverait « rien à committer » —
        un succès transformé en échec par le harnais lui-même.
        """
        root = pathlib.Path(__file__).resolve().parents[1]
        for name in ("test_pre_commit_hook_e2e.py", "test_pre_push_hook.py"):
            text = (root / "tests" / name).read_text(encoding="utf-8")
            self.assertIn("COMMIT_ANNOUNCEMENTS", text, f"{name} doit exiger la bannière")

    def test_the_announcements_are_the_ones_the_wrappers_print(self) -> None:
        """Les bannières se lisent dans les wrappers, elles ne s'inventent pas ici."""
        for markers, name in (
            (COMMIT_ANNOUNCEMENTS, "pre-commit"),
            (PUSH_ANNOUNCEMENTS, "pre-push"),
        ):
            text = hook_wrapper(name).read_text(encoding="utf-8")
            for marker in markers:
                self.assertIn(marker, text, f"{name} doit annoncer « {marker} »")

    def test_the_tooling_contract_is_verified_in_one_place_only(self) -> None:
        """Le contrat vaut pour les **deux** wrappers : éprouvé une fois, ensemble.

        Le dupliquer par hook le ferait diverger — et c'est précisément lui qui
        distingue un incident d'OS d'un refus, l'erreur la plus chère du dossier.
        """
        root = pathlib.Path(__file__).resolve().parents[1]
        contract = (root / "tests" / "test_hook_tooling_contract.py").read_text(
            encoding="utf-8"
        )
        for helper in ("path_without_python", "path_with_shim", "run_wrapper"):
            self.assertIn(helper, contract, f"le contrat doit passer par le helper partagé {helper}")
        for name in ("test_pre_commit_hook.py", "test_pre_commit_hook_e2e.py", "test_pre_push_hook.py"):
            text = (root / "tests" / name).read_text(encoding="utf-8")
            self.assertNotIn("EXIT_TOOLING", text, f"{name} n'a pas à redupliquer le contrat")

    def test_nothing_here_needs_git_or_a_posix_shell(self) -> None:
        """La preuve du contrat doit tenir sur une machine qui n'a ni l'un ni l'autre."""
        with mock.patch("shutil.which", return_value=None):
            run = hook_run(self.command('print("Aucune valeur de secret dans 0 fichier")'))

        self.assertTrue(run.concluded)
        self.assertEqual(self.count(), 1)


if __name__ == "__main__":
    unittest.main()
