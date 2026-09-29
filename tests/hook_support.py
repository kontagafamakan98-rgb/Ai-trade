"""Aides partagées par les tests des hooks git.

Deux problèmes très différents se ressemblent sous MSYS2, et les confondre coûte
cher dans les deux sens :

* **un incident d'OS** — Windows/Git-Bash démarre un processus par `fork`, et le
  runtime échoue par saturation quand la suite a déjà lancé des centaines de
  processus (`child_copy: cygheap read copy failed`, puis `exit code 0xC0000142`
  — `STATUS_DLL_INIT_FAILED`). Le hook a alors démarré et son outillage est mort
  **sans rien décider**. Vue d'un cran plus loin, la même saturation empêche le
  **transport** du `push` de démarrer : git s'arrête sur `fatal: Could not read
  from remote repository`, et le hook n'a même pas été **atteint** — git lit les
  références du distant avant de l'appeler ;
* **un refus légitime** — le hook a conclu, et sa conclusion est dans sa sortie
  (`COMMIT REFUSÉ`, `DÉPLOIEMENT REFUSÉ`…).

Ce qui les distingue n'est donc **pas** le code de retour **de git** : un refus
rend 1, et un outillage mort aussi — mesuré, `git commit` comme `git push`
écrasent à 1 le code de leur hook, quelle qu'en soit la valeur (1, 3, 127,
binaire introuvable). Les **wrappers**, eux, portent la réponse dans leur propre
code (`HOOK_TOOLING_EXIT` quand rien n'a pu être contrôlé), mais ce code ne
survit que si le wrapper est invoqué **directement** — ce dont s'occupe
`tests/test_hook_tooling_contract.py`. À travers git, la seule marque restante
est donc la **présence d'un verdict**. Les deux tests de bout en bout réessayent
quand il n'y en a pas, et ne rejouent jamais quand il y en a un — rejouer un
refus ne changerait pas sa conclusion, et prendre une fuite de secret pour une
machine fatiguée est précisément la faute à ne pas commettre.

La suite de ce module concerne la forme des wrappers : valider la syntaxe POSIX
d'un `.githooks/*` demande d'appeler `sh -n`, et ce `sh` peut lui aussi ne pas
démarrer. On réessaie quelques fois, puis on **passe** ce test-là : l'assertion
n'a de sens que si le shell a réellement tourné. La CI (Linux) exécute le
contrôle pour de bon, sans ce mode de défaillance.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence

#: Codes « le processus n'a pas pu démarrer » (jamais une erreur de syntaxe) :
#: 0xC0000142 = STATUS_DLL_INIT_FAILED, observé sur l'échec de fork MSYS2.
_UNSTARTABLE = {0xC0000142, -0x3FFFFEBE, 3221225794, -1073741502}

#: Code du **wrapper** (`.githooks/pre-commit`, `.githooks/pre-push`) : « mon
#: outillage n'a pas pu démarrer, rien n'a été contrôlé ». Il n'appartient à
#: aucun des deux outils — leur contrat s'arrête à 2, et son en-tête le déclare —
#: donc un appelant qui invoque le hook directement le reconnaît **sans lire un
#: seul mot de sortie**. Les wrappers l'écrivent aussi dans leur message, parce
#: que git, lui, écrase le code à 1.
HOOK_TOOLING_EXIT = 3

#: Ce que les outils des hooks écrivent quand ils ont **conclu** — et le
#: vocabulaire appartient aux scripts (`scripts/pre_commit_secrets.py`,
#: `scripts/verify_secrets.py`) : il est repris ici pour que les deux hooks
#: s'accordent sur ce qu'« avoir conclu » veut dire. Y compris « AUCUN contrôle
#: n'a eu lieu », qui est une conclusion : le hook dit qu'il laisse passer.
COMMIT_VERDICTS = (
    "COMMIT REFUSÉ",
    "Aucune valeur de secret dans",
    "aucun fichier indexé",
)
PUSH_VERDICTS = (
    "DÉPLOIEMENT REFUSÉ",
    "Tous les secrets sont présents",
    "AUCUN contrôle n'a eu lieu",
)

#: Ce que le wrapper écrit sur sa **sortie d'erreur** dès qu'il démarre. Git relaie
#: toujours la sortie d'erreur d'un hook, mais **avale sa sortie standard quand le
#: hook réussit** : sur un push autorisé, aucun verdict n'est visible, et sans
#: cette bannière, rien ne prouverait que le hook n'a pas simplement été ignoré.
PUSH_ANNOUNCEMENTS = ("pre-push : audit des secrets",)

#: La même bannière, pour le commit : git la relaie (mesuré, y compris sur un
#: commit qui réussit), et elle est donc la seule preuve que le hook a tourné.
#: Ce n'est pas elle qui protège du rejeu — `hook_run` ne rejoue **jamais** un
#: succès pour du bruit de `fork` — mais c'est elle qui permet de le **constater**
#: (`HookRun.started`) au lieu de le supposer.
COMMIT_ANNOUNCEMENTS = ("pre-commit : scan anti-fuite",)

#: Ce que **git** imprime quand il n'a pas pu lancer le hook. C'est le seul
#: incident qui survit à un succès : git avertit puis **continue**, donc la
#: commande réussit sans que rien n'ait été exercé — le succès ne prouve rien,
#: et rejouer est le seul moyen de faire tourner le hook pour de bon.
_HOOK_NOT_LAUNCHED = (
    "cannot spawn",
    "cannot run",
)

#: Ce que **git** imprime quand il n'a pas pu atteindre le dépôt distant : son
#: propre enfant (`git-receive-pack`, pour un chemin local) n'a pas démarré. Le
#: hook, lui, n'a **pas été atteint** — git lit les références du distant *avant*
#: d'appeler `pre-push` (mesuré : la bannière du hook n'apparaît jamais sur cet
#: échec). Rien n'a donc été décidé, et rien n'a été poussé : l'échec est en
#: **lecture**, donc avant toute mise à jour de référence. C'est un incident, et
#: le rejouer est sûr.
_TRANSPORT_DEAD = ("Could not read from remote repository",)

#: La même phrase, mais **la cause est nommée** : un dépôt distant qui n'existe
#: pas, un droit refusé. Ce n'est plus un incident d'OS mais une erreur de
#: l'échafaudage du test (il pointe ailleurs) — la passer sous silence masquerait
#: un vrai bug, donc elle est rendue telle quelle.
_TRANSPORT_CAUSE = (
    "does not appear to be a git repository",
    "unable to access",
    "Permission denied",
)

#: Ce que le **runtime** (MSYS2/Windows) laisse derrière lui quand un processus
#: n'a pas démarré : bruit de `fork`, de chargeur. **Aucune de ces lignes n'est un
#: verdict** : elles disent seulement que quelque chose n'a pas tourné. Sur une
#: commande qui a **échoué** sans verdict, c'est la signature d'un outillage mort
#: avant de conclure — on rejoue. Sur une commande qui a **réussi**, c'est du bruit
#: et rien de plus : la rejouer ne peut que fabriquer un échec.
_RUNTIME_NOISE = (
    "cygheap read copy failed",
    "child_copy:",
    "fork: Resource temporarily unavailable",
    "Resource temporarily unavailable",
    "null directory",
    "0xC0000142",
)

_INCIDENTS = _HOOK_NOT_LAUNCHED + _RUNTIME_NOISE


def _decode(data: bytes) -> str:
    """Décode sans dépendre de la locale (les hooks émettent « ✅ », « ❌ »…)."""
    return data.decode("utf-8", errors="replace")


def first_marker(output: str, markers: Sequence[str]) -> Optional[str]:
    """Le premier marqueur présent dans la sortie, ou `None`."""
    found = [marker for marker in markers if marker in output]
    if not found:
        return None
    return min(found, key=output.index)


@dataclass(frozen=True)
class HookRun:
    """Une commande git qui exécute un hook : ce qui s'est dit, et ce qui s'est passé.

    `verdict` et `incident` ne sont jamais remplis tous les deux : soit le hook a
    conclu, soit son outillage est mort avant. C'est ce qui interdit de confondre
    un refus avec un incident de fork.

    Un hook qui **réussit** ne laisse pas de verdict : git avale sa sortie
    standard. `announcement` est donc la preuve qu'il a démarré (le wrapper écrit
    sur sa sortie d'erreur, toujours relayée), et `concluded` la preuve qu'il est
    allé jusqu'à un verdict — refus comme acceptation.
    """

    proc: subprocess.CompletedProcess
    output: str
    verdict: Optional[str]
    incident: Optional[str]
    attempts: int
    announcement: Optional[str] = None

    @property
    def concluded(self) -> bool:
        """Le hook a rendu son verdict, quel qu'il soit."""
        return self.verdict is not None

    @property
    def started(self) -> bool:
        """Le wrapper a démarré — même si sa conclusion a été avalée par git."""
        return self.announcement is not None

    @property
    def refused(self) -> bool:
        """Le hook a conclu **par un refus** — jamais déduit d'un code de retour."""
        return self.verdict is not None and "REFUS" in self.verdict.upper()


class HookNeverConcluded(RuntimeError):
    """L'outillage du hook est mort avant de conclure, à chaque essai."""

    def __init__(self, run: HookRun) -> None:
        super().__init__(
            f"le hook n'a pas conclu après {run.attempts} essai(s) "
            f"(code {run.proc.returncode}, incident {run.incident or 'silencieux'})"
        )
        self.run = run


def hook_run(
    argv: Sequence[str],
    *,
    verdicts: Sequence[str] = COMMIT_VERDICTS,
    announcements: Sequence[str] = (),
    attempts: int = 3,
    **kwargs: Any,
) -> HookRun:
    """Lance `git <…>` et rend un résultat **porteur d'un verdict**, ou rejoue.

    Ce qui décide, dans l'ordre :

    1. **un verdict est lu** → il est rendu tel quel, quelle que soit sa valeur.
       Un refus n'est jamais rejoué : rejouer ne changerait pas sa conclusion, et
       le confondre avec un incident ferait passer une fuite de secret pour une
       machine fatiguée ;
    2. **aucune trace d'incident** → rien à rejouer, même sans verdict. C'est le
       cas d'un commit ou d'un push qui a **réussi** : git avale la sortie
       standard d'un hook qui réussit, donc l'absence de verdict n'y prouve rien.
       C'est aussi celui d'un wrapper cassé (erreur de syntaxe, `exec` invalide) :
       le test doit le voir, et trois reprises le masqueraient ;
    3. **une commande qui a réussi** (`code 0`) n'est rejouée que si git dit
       lui-même qu'il **n'a pas lancé le hook** (`cannot spawn`, `cannot run`). Le
       bruit du runtime MSYS2, sur un succès, n'est que du bruit.

       Ce n'est pas une commodité : rejouer une commande qui a réussi ne peut que
       **fabriquer un échec**. Un `git commit` rejoué trouve « rien à committer »,
       un `git push` rejoué « everything up-to-date » — et le harnais transforme
       alors un succès en échec. C'est arrivé : sur une suite complète, où la
       saturation de `fork` est plus fréquente, le commit initial d'un test a été
       rejoué sur une trace de fork et a échoué avec « nothing added to commit ».
       L'outillage mort, lui, ne peut **pas** se cacher derrière un succès : le
       wrapper le transforme en code `3`, que git écrase en échec de commande ;
    4. **une trace d'incident sans verdict sur une commande qui a échoué** — un
       message du runtime, un code de chargement `0xC0000142`, une sortie **vide**
       alors que git a échoué (le wrapper écrit toujours quand il tourne) → rien
       n'a été décidé : on réessaie ;
    5. **un transport mort avant le hook** (`Could not read from remote
       repository`, sans cause nommée) : `pre-push` reçoit les références du
       distant, donc git le contacte **avant** d'appeler le hook — mesuré, la
       bannière du hook n'apparaît jamais sur cet échec. Rien n'a été décidé, et
       rien n'a été poussé (l'échec est en lecture) : on réessaie. Quand git
       **nomme** la cause, c'est l'échafaudage du test qui est en faute, et
       l'échec est rendu tel quel.

    Après `attempts` essais sans conclusion et avec une trace d'incident, lève
    `HookNeverConcluded` : c'est un incident d'OS, pas un comportement du hook.
    """
    check = kwargs.pop("check", False)
    kwargs.pop("capture_output", None)
    last: HookRun | None = None
    for attempt in range(1, attempts + 1):
        proc = subprocess.run(list(argv), check=False, capture_output=True, **kwargs)
        output = _decode(proc.stdout or b"") + _decode(proc.stderr or b"")
        # Le transport est mort **avant** le hook : cet échec ne dit rien de lui.
        # Quand git nomme la cause, c'est l'échafaudage du test qui est en faute.
        transport_cause = first_marker(output, _TRANSPORT_CAUSE)
        transport_lost = (
            first_marker(output, _TRANSPORT_DEAD) is not None and transport_cause is None
        )
        run = HookRun(
            proc=proc,
            output=output,
            verdict=first_marker(output, verdicts),
            incident=first_marker(output, _INCIDENTS)
            or (_TRANSPORT_DEAD[0] if transport_lost else None),
            attempts=attempt,
            announcement=first_marker(output, announcements),
        )
        incident_trace = (
            run.incident is not None
            or proc.returncode in _UNSTARTABLE
            # Le wrapper l'annonce lui-même, code à l'appui : rien à lire, et
            # rien à confondre avec un refus. (À travers git, ce code est écrasé
            # à 1 — c'est alors le verdict qui reprend la main.)
            or proc.returncode == HOOK_TOOLING_EXIT
            or (proc.returncode != 0 and not output.strip())
        )
        # Un succès ne se rejoue que si git avoue ne pas avoir lancé le hook.
        not_launched = first_marker(output, _HOOK_NOT_LAUNCHED) is not None
        retryable = incident_trace and not (proc.returncode == 0 and not not_launched)
        if run.concluded or not retryable:
            if check:
                proc.check_returncode()
            return run
        last = run
    assert last is not None
    raise HookNeverConcluded(last)


def run_hook_or_skip(
    testcase: unittest.TestCase,
    argv: Sequence[str],
    *,
    verdicts: Sequence[str] = COMMIT_VERDICTS,
    announcements: Sequence[str] = (),
    attempts: int = 3,
    **kwargs: Any,
) -> HookRun:
    """`hook_run` pour un test de bout en bout : un incident d'OS **passe** le test.

    Le test doit prouver un comportement du hook, pas la santé du `fork` de la
    machine. La CI (Linux) exécute le contrôle sans ce mode de défaillance, et
    c'est elle qui porte la preuve.
    """
    try:
        return hook_run(
            argv,
            verdicts=verdicts,
            announcements=announcements,
            attempts=attempts,
            **kwargs,
        )
    except HookNeverConcluded as incident:
        testcase.skipTest(
            f"{incident} — saturation de fork MSYS2 ; dernière sortie : "
            f"{incident.run.output[:200]!r}"
        )
        raise  # inatteignable : `skipTest` lève toujours.


def run_or_skip(
    testcase: unittest.TestCase,
    argv: List[str],
    *,
    attempts: int = 3,
    check: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess:
    """`run_with_spawn_retry` pour l'**échafaudage** d'un test de bout en bout.

    Un test de hook monte un dépôt jetable, configure git, installe les hooks :
    autant de processus que l'OS peut refuser de démarrer quand la suite a déjà
    lancé des centaines de `fork`. Ce n'est pas un comportement du hook, c'est la
    santé de la machine — au même titre que dans `run_hook_or_skip`, et pour la
    même raison, le test est alors **passé** (`skipTest`) au lieu de rougir.

    L'unique issue absorbée est le code de **chargeur** (`_UNSTARTABLE`) : c'est
    celui qui dit « le programme n'a jamais tourné ». Un `git` qui démarre,
    échoue et parle reste un échec, avec sa sortie — le confondre avec un incident
    d'OS masquerait exactement ce que le test doit voir.
    """
    proc = run_with_spawn_retry(argv, attempts=attempts, **kwargs)
    if proc.returncode in _UNSTARTABLE:
        testcase.skipTest(
            f"`{' '.join(argv[:2])}` n'a pas pu démarrer (code {proc.returncode}) — "
            "saturation de fork MSYS2 ; le contrôle sera exécuté en CI (Linux)"
        )
    if check:
        proc.check_returncode()
    return proc


def run_with_spawn_retry(argv: List[str], attempts: int = 3, **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` en réessayant si l'OS a refusé de démarrer le processus.

    Windows/Git-Bash sait échouer par saturation (`fork`), avec un code de
    chargement qui n'est jamais un code de retour normal. Réessayer est sûr :
    le programme n'a pas tourné, donc rien n'a été fait deux fois.
    """
    check = kwargs.pop("check", False)
    last: subprocess.CompletedProcess | None = None
    for _ in range(attempts):
        proc = subprocess.run(argv, check=False, **kwargs)
        if proc.returncode not in _UNSTARTABLE:
            if check:
                proc.check_returncode()
            return proc
        last = proc
    assert last is not None
    if check:
        last.check_returncode()
    return last


# --------------------------------------------------------------------------- #
# Invoquer un wrapper directement : le seul chemin où son code survit
# --------------------------------------------------------------------------- #

#: Un `dirname` de secours, écrit en shell pur. Le test qui retire Python du PATH
#: doit laisser au wrapper de quoi localiser son **propre** dossier : sinon il
#: échouerait sur le garde « racine du dépôt », et prouverait autre chose que ce
#: qu'il annonce.
_SHIM_DIRNAME = """\
[ "${1:-}" = "--" ] && shift
case "${1:-}" in
    */*) printf '%s\\n' "${1%/*}" ;;
    *) printf '%s\\n' "." ;;
esac
"""


def hook_wrapper(name: str) -> Path:
    """Le wrapper versionné `.githooks/<name>`."""
    return Path(__file__).resolve().parents[1] / ".githooks" / name


def declared_tooling_exit(hook: Path) -> Optional[int]:
    """Le code que le wrapper déclare pour « l'outillage n'a pas démarré ».

    Lu dans le script plutôt que supposé : c'est l'accord entre ce module et les
    deux wrappers qui est vérifié, et une dérive se voit tout de suite.
    """
    text = hook.read_text(encoding="utf-8")
    match = re.search(r"^EXIT_TOOLING=(\d+)$", text, re.MULTILINE)
    return int(match.group(1)) if match else None


def make_shim(directory: Path, name: str, body: str) -> Path:
    """Une doublure exécutable : `name` exécute `body` sous `sh`."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


#: Noms qu'un interpréteur peut porter, selon la plateforme.
_PYTHON_NAMES = ("python", "python3", "python.exe", "python3.exe")


def _directories_without_python() -> List[str]:
    """Les dossiers du PATH où aucun interpréteur ne répond.

    On filtre au lieu de tout jeter : `sh` et `git` doivent rester joignables —
    sous Windows, git s'en sert pour lancer le hook — alors que le wrapper, lui,
    ne doit plus trouver d'interpréteur.
    """
    kept: List[str] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry:
            continue
        if any((Path(entry) / name).exists() for name in _PYTHON_NAMES):
            continue
        kept.append(entry)
    return kept


def path_without_python(directory: Path) -> str:
    """Un PATH où Python ne répond pas, mais où `sh` et `git` restent joignables.

    `dirname` y est une doublure en shell pur : le wrapper doit pouvoir localiser
    son **propre** dossier, sans quoi il échouerait sur le garde « racine du
    dépôt » et le test prouverait autre chose que ce qu'il annonce.
    """
    make_shim(directory, "dirname", _SHIM_DIRNAME)
    return os.pathsep.join([str(directory), *_directories_without_python()])


def path_with_shim(directory: Path, name: str, body: str) -> str:
    """Un PATH où `name` est une doublure, **avant** le vrai outil."""
    make_shim(directory, name, body)
    return str(directory) + os.pathsep + os.environ.get("PATH", "")


def run_wrapper(
    testcase: unittest.TestCase,
    hook: Path,
    *,
    path: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Exécute un wrapper **directement**, et rend son code de retour.

    C'est le seul chemin où ce code survit (`git commit` et `git push` l'écrasent
    à 1), et donc le seul endroit où « l'outil a conclu » se distingue de
    « l'outillage n'a pas démarré » **par le code** plutôt que par la lecture
    d'un verdict.
    """
    shell = shutil.which("sh")
    if shell is None:
        testcase.skipTest("aucun `sh` pour invoquer un wrapper directement")
    assert shell is not None
    environment = dict(os.environ)
    if path is not None:
        environment["PATH"] = path
    return subprocess.run(
        [shell, str(hook)], env=environment, capture_output=True, timeout=120
    )


def run_wrapper_conclusion(
    testcase: unittest.TestCase,
    hook: Path,
    *,
    path: Optional[str] = None,
    attempts: int = 3,
) -> subprocess.CompletedProcess:
    """Un wrapper invoqué directement, en réessayant un incident de **lancement**.

    Le code du wrapper est la réponse — mais seulement si l'**enfant** a tourné.
    Sous MSYS2 saturé, ce n'est pas le wrapper qui échoue, c'est le processus qu'il
    lance : mesuré sur une suite complète, `sh` meurt (`child_copy: cygheap read
    copy failed`) et le scan rend 127. Le wrapper conclut alors, à raison, `3`
    (« le scan a rendu 127, hors de son contrat : il n'a rien décidé ») — un
    verdict honnête sur une machine fatiguée, et **pas** ce que le test mesurait.
    Ce `3`-là est donc rejoué, puis le test est **passé**.

    Deux formes d'incident, et deux seulement, sont absorbées :

    * `HOOK_TOOLING_EXIT` **accompagné du bruit du runtime** (`_RUNTIME_NOISE`) :
      l'enfant du wrapper n'a pas été lancé, et le `3` le dit honnêtement ;
    * un code de **chargeur** (`_UNSTARTABLE`) : le wrapper lui-même n'a jamais
      démarré — mesuré aussi (`0xC0000142`, `STATUS_DLL_INIT_FAILED`).

    Tout le reste est rendu tel quel. Un `3` sans bruit est une **conclusion** du
    wrapper — interpréteur introuvable, script absent, outillage muet — et n'est
    jamais absorbé : c'est précisément ce que vérifient les tests de
    `tests/test_hook_tooling_contract.py`. Et un refus accompagné de bruit reste
    un refus : un code rendu est un verdict, le bruit ne le requalifie pas.
    """
    incident = "aucun essai"
    for _ in range(attempts):
        proc = run_wrapper(testcase, hook, path=path)
        if proc.returncode in _UNSTARTABLE:
            incident = f"le wrapper n'a pas pu démarrer (code {proc.returncode})"
            continue
        if proc.returncode == HOOK_TOOLING_EXIT:
            output = _decode(proc.stdout or b"") + _decode(proc.stderr or b"")
            noise = first_marker(output, _RUNTIME_NOISE)
            if noise is not None:
                incident = f"l'outillage a rendu {proc.returncode} sur un incident ({noise!r})"
                continue
        return proc
    testcase.skipTest(
        f"`{hook.name}` : rien n'a été contrôlé — {incident}, après {attempts} "
        "essai(s) ; saturation de fork MSYS2, le contrôle sera exécuté en CI (Linux)"
    )
    raise AssertionError("inatteignable : `skipTest` lève toujours")


def assert_valid_posix_shell(
    testcase: unittest.TestCase, path: Path, attempts: int = 3
) -> None:
    """Vérifie `sh -n <path>`, avec repli explicite si `sh` ne peut pas démarrer."""
    if shutil.which("sh") is None:
        testcase.skipTest("aucun `sh` disponible pour valider la syntaxe POSIX")

    last = None
    for _ in range(attempts):
        proc = subprocess.run(
            ["sh", "-n", str(path)], capture_output=True, text=True, timeout=60
        )
        if proc.returncode == 0:
            return
        if proc.returncode not in _UNSTARTABLE:
            testcase.fail(
                f"`sh -n {path.name}` a échoué (code {proc.returncode}) : {proc.stderr}"
            )
        last = proc

    testcase.skipTest(
        f"impossible de démarrer `sh` sur cette machine (code {last.returncode}) — "
        "saturation de fork MSYS2 ; le contrôle sera exécuté en CI"
    )
