"""Tests de la publication de l'arbre local (`core/tree_publish`, `scripts/publish_tree.py`).

Ce qui est éprouvé ici est le **périmètre** et la **preuve**, pas la mécanique de git :

* ce qui part, ce qui est élagué, ce qui est écarté — et le fait que rien ne soit
  écarté en silence (un `.env` non publié est *nommé*, comme les dossiers d'outillage) ;
* les fichiers **débris** de la racine (vide, nom d'essai `_…`, sauvegarde), qui ont
  deux fois de plus fini publiés qu'ils n'ont été vus ;
* la vérification, fichier par fichier : l'empreinte de contenu (identifiant de
  `blob` git) recalculée sur les octets du poste, la seule exception des fins de
  ligne — nommée — et la dérive, qui doit faire échouer la publication.

Trois niveaux, comme ailleurs dans ce dépôt : des fonctions **pures** (aucun git),
un dossier de travail **réel** (dépôt nu jetable, clone, commit, push), et la
**CLI**. L'échafaudage git passe par `tests.hook_support.run_or_skip` : sous MSYS2
saturé, un `git` qui n'a pas pu démarrer **passe** le test au lieu de le faire
rougir — c'est la CI (Linux) qui porte la preuve.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from core import tree_publish as tp
from tests.hook_support import run_or_skip

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "scripts" / "publish_tree.py"

#: Identité de contenu d'un fichier vide, telle que git la calcule. C'est le seul
#: point de recoupement possible avec git sans lui parler : si `blob_id` dérive,
#: cette constante le dit.
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def _load_cli():
    spec = importlib.util.spec_from_file_location("publish_tree", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


def _write(root: Path, relative: str, content: bytes | str) -> Path:
    """Écrit un fichier **en LF**, quel que soit le système.

    `write_text` traduit `\n` en `\r\n` sous Windows : les octets écrits ne
    seraient alors pas ceux qu'on vient d'écrire, et une épreuve d'empreinte
    mesurerait la traduction du système au lieu de la publication. Les fichiers
    du dépôt sont en LF (`.gitattributes`), donc le test écrit en LF.
    """
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8", newline="\n")
    else:
        path.write_bytes(content)
    return path


def _rewrite(root: Path, relative: str, content: bytes | str) -> None:
    """Écrit un fichier **en préservant sa date** : rien n'est caché par un horodatage."""
    path = root / relative
    stat = path.stat()
    _write(root, relative, content)
    os.utime(path, (stat.st_atime, stat.st_mtime))


#: Ce que git imprime quand il n'a pas pu **lire** le dépôt distant : son enfant
#: (`git-receive-pack`, ici un chemin local) n'a pas démarré — la saturation de
#: `fork` sous MSYS2, telle que `tests/hook_support.py` la décrit. L'échec est en
#: **lecture**, donc avant toute mise à jour de référence : rien n'a été poussé, et
#: rejouer est sûr. Quand git **nomme** la cause, elle n'est pas absorbée : c'est
#: l'échafaudage du test qui est en faute, et cela doit se voir.
_TRANSPORT_DEAD = "Could not read from remote repository"
_TRANSPORT_CAUSE = ("does not appear to be", "unable to access", "Permission denied")


def _transport_trouble(*texts: str) -> bool:
    """Un transport mort **sans cause nommée**, et rien d'autre."""
    joined = " ".join(texts)
    return _TRANSPORT_DEAD in joined and not any(cause in joined for cause in _TRANSPORT_CAUSE)


def _decode(proc) -> str:
    """Sortie d'un processus, décodée sans dépendre de la locale."""
    return (proc.stdout or b"").decode("utf-8", "replace") + (
        proc.stderr or b""
    ).decode("utf-8", "replace")


def _scaffold(testcase, argv, *, cwd, env, attempts: int = 3):
    """`git` d'échafaudage : rejoue un **transport mort**, passe si ça persiste.

    Un `git push` de montage peut mourir avant d'écrire quoi que ce soit
    (`Could not read from remote repository`, sans cause nommée) : c'est la
    saturation de `fork` de MSYS2, déjà décrite dans `tests/hook_support.py`.
    L'échec est en **lecture**, donc rejouer est sûr ; s'il persiste, c'est la
    machine, et la CI (Linux) exécute le contrôle pour de bon. Tout autre échec
    est rendu tel quel — l'échafaudage doit se voir quand il est en faute.
    """
    last = None
    for _ in range(attempts):
        proc = run_or_skip(
            testcase, argv, cwd=str(cwd), env=env, capture_output=True, timeout=180
        )
        last = proc
        if proc.returncode == 0:
            return proc
        if not _transport_trouble(_decode(proc)):
            testcase.fail(
                f"`{' '.join(argv[:3])}` a échoué (code {proc.returncode}) : "
                f"{_decode(proc)[:300]}"
            )
    testcase.skipTest(f"transport mort trois fois — {_decode(last)[:200]}")
    raise AssertionError("inatteignable : `skipTest` lève toujours")


def _why(report: dict) -> str:
    """Le refus, **avec ce qui a dérivé** : une assertion qui échoue doit nommer le coupable.

    « False is not true » ne dit pas si le push a été refusé, si la branche a bougé
    ailleurs, ou si un fichier n'est pas le même des deux côtés — et ces trois
    causes s'instruisent à trois endroits différents.
    """
    verification = report.get("verification") or {}
    details = list(report.get("problems") or [])
    if verification:
        details.append(f"dérive : {[item['path'] for item in verification['drift']]}")
    return " ; ".join(details) or "aucun détail"


#: Échafaudage commun aux épreuves qui montent de **vrais** dépôts : le modèle de
#: dépôt nu (semé une fois pour la suite, recopié par épreuve) et des appels `git`
#: qui rejouent un incident de lancement au lieu de faire rougir le test.
class RemoteTestCase(unittest.TestCase):
    """Échafaudage partagé : un dépôt nu semé une fois, **recopié** par épreuve."""

    env: dict
    remote: Path
    work_tree: Path
    branch = "import/arbre-local"

    def _seed(self) -> None:
        shutil.copytree(_remote_template(self, self.env), self.remote)

    def _git(self, *args: str) -> None:
        """`git` d'échafaudage dans le dossier de travail (le test écrit, pas le module)."""
        _scaffold(self, ["git", *args], cwd=self.work_tree, env=self.env)

    def _head(self) -> str:
        """Le commit du dossier de travail, en réessayant un incident de lancement."""
        for _ in range(3):
            done = tp.git("rev-parse", "HEAD", cwd=self.work_tree)
            if done is not None and done.returncode == 0:
                return tp.decode(done).strip()
        self.skipTest("HEAD illisible trois fois — saturation de fork MSYS2")
        raise AssertionError("inatteignable : `skipTest` lève toujours")

    def _is_ancestor(self, ancestor: str, revision: str) -> bool:
        """`git merge-base --is-ancestor` : 0 = oui, 1 = non, autre = pas de réponse."""
        for _ in range(3):
            done = tp.git("merge-base", "--is-ancestor", ancestor, revision, cwd=self.work_tree)
            if done is not None and done.returncode in (0, 1):
                return done.returncode == 0
        self.skipTest("merge-base muet trois fois — saturation de fork MSYS2")
        raise AssertionError("inatteignable : `skipTest` lève toujours")

    def _move_the_remote_branch(self) -> str:
        """Une autre copie publie : le distant avance **sans nous**.

        La copie se pose d'abord sur la branche publiée : bricoler depuis `main`
        (la tête du dépôt nu) produirait une poussée rejetée, et le test mesurerait
        ce refus-là au lieu du recul du clone.
        """
        other = Path(self._tmp.name) / "autre-copie"
        _scaffold(
            self,
            ["git", "clone", "-q", str(self.remote), str(other)],
            cwd=Path(self._tmp.name),
            env=self.env,
        )
        _scaffold(
            self,
            ["git", "checkout", "-q", "-B", self.branch, f"origin/{self.branch}"],
            cwd=other,
            env=self.env,
        )
        _write(other, "pkg/mod.py", "x = 42\n")
        _scaffold(self, ["git", "add", "-A"], cwd=other, env=self.env)
        _scaffold(self, ["git", "commit", "-m", "le distant avance"], cwd=other, env=self.env)
        _scaffold(
            self, ["git", "push", "origin", f"HEAD:refs/heads/{self.branch}"], cwd=other, env=self.env
        )
        return self._remote_head()

    def _remote_head(self) -> str:
        """L'identifiant que porte la branche distante, en réessayant un incident.

        `remote_sha` lit le distant : sous MSYS2 saturé, le `git` qu'elle lance peut
        ne pas démarrer. Rien n'a alors été écrit ni poussé — l'échec est en
        lecture — donc rejouer est sûr : c'est la même règle que
        `tests/hook_support.py`, et le test est **passé** si l'incident persiste.
        """
        for _ in range(3):
            sha = tp.remote_sha(self.work_tree, "origin", self.branch)
            if sha is not None:
                return sha
        self.skipTest("le distant n'a pas répondu trois fois — saturation de fork MSYS2")
        raise AssertionError("inatteignable : `skipTest` lève toujours")

    def _remote_entries(self) -> dict:
        """Le contenu de la branche distante : chemin → identifiant d'objet."""
        for _ in range(3):
            entries = tp.tree_entries(self.work_tree, self._remote_head())
            if entries is not None:
                return entries
        self.skipTest("l'arbre du distant n'a pas pu être lu trois fois — fork MSYS2")
        raise AssertionError("inatteignable : `skipTest` lève toujours")


def _seed_remote(testcase: unittest.TestCase, base: Path, env: dict, remote: Path) -> None:
    """Un `main` initial sur un dépôt **nu** jetable : le clone a de quoi se poser.

    Sans lui, le clonage d'un dépôt vide n'aurait pas de tête de branche, et le
    test mesurerait ce cas-là plutôt que la publication.
    """
    seed = base / "graine"
    seed.mkdir()

    def _git(*args: str) -> None:
        _scaffold(testcase, ["git", *args], cwd=seed, env=env)

    _git("init", "-q", "-b", "main")
    _write(seed, "README.md", "graine\n")
    _git("add", "-A")
    _git("commit", "-m", "graine")
    _git("init", "-q", "--bare", "-b", "main", str(remote))
    _git("remote", "add", "origin", str(remote))
    _git("push", "origin", "main")


#: Le dépôt nu **déjà semé**, construit une fois pour toute la suite : le semer à
#: chaque épreuve coûtait une dizaine de processus `git` par test, et cette suite
#: en compte une vingtaine. Le recopier coûte un `copytree`, et le contenu publié
#: est rigoureusement le même.
_TEMPLATE: Path | None = None
_TEMPLATE_TMP: tempfile.TemporaryDirectory | None = None


def _remote_template(testcase: unittest.TestCase, env: dict) -> Path:
    global _TEMPLATE, _TEMPLATE_TMP
    if _TEMPLATE is None:
        _TEMPLATE_TMP = tempfile.TemporaryDirectory()
        _TEMPLATE = Path(_TEMPLATE_TMP.name) / "modele.git"
        _seed_remote(testcase, Path(_TEMPLATE_TMP.name), env, _TEMPLATE)
    return _TEMPLATE


def tearDownModule():
    """Le modèle de dépôt nu ne survit pas à la suite."""
    global _TEMPLATE, _TEMPLATE_TMP
    if _TEMPLATE_TMP is not None:
        _TEMPLATE_TMP.cleanup()
        _TEMPLATE = None
        _TEMPLATE_TMP = None


def _git_env(tmp: Path) -> dict:
    """Un environnement git **hermétique** : ni config de la machine, ni signature.

    Sans cela, un `commit.gpgsign=true` ou un `hookspath` posé dans la config
    globale de qui lance la suite déciderait de l'issue du test — l'échafaudage
    déciderait à la place de ce qu'il mesure.
    """
    neutral = tmp / "gitconfig-vide"
    neutral.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_GLOBAL": str(neutral),
            "GIT_CONFIG_SYSTEM": str(neutral),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


# --------------------------------------------------------------------------- #
# 1. Le parcours du poste : ce qui part, et ce qui est écarté (nommé)
# --------------------------------------------------------------------------- #


class CollectTest(unittest.TestCase):
    def test_it_keeps_the_tree_and_names_everything_it_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "README.md", "bonjour\n")
            _write(root, "pkg/mod.py", "x = 1\n")
            _write(root, ".env", "SECRET=1\n")
            _write(root, "notes.log", "trace\n")
            _write(root, ".venv/lib/site.py", "interdit\n")
            _write(root, ".pgtest/data.py", "interdit\n")
            _write(root, "pkg/__pycache__/mod.cpython-311.pyc", b"\x00\x01")
            _write(root, "pkg/build/gen.py", "artefact\n")
            # L'état local du client : publié une fois par une publication à la
            # main, faute d'être nommé par un `.gitignore`.
            _write(root, ".freebuff/project-id", "3f1c9d2a\n")

            harvest = tp.collect(root)

        self.assertEqual(
            sorted(candidate.relative for candidate in harvest.candidates),
            ["README.md", "pkg/mod.py"],
        )
        # Nommés : un périmètre qui ne dit pas ce qu'il laisse de côté se lit
        # comme une couverture totale.
        self.assertEqual(
            sorted(item.relative for item in harvest.skipped_dirs),
            [".freebuff", ".pgtest", ".venv", "pkg/__pycache__", "pkg/build"],
        )
        self.assertEqual(
            [(item.relative, item.reason) for item in harvest.skipped_files],
            [
                (".env", "fichier réservé (secret local)"),
                ("notes.log", "artefact local"),
            ],
        )
        self.assertTrue(all(item.reason for item in harvest.skipped_dirs))

    def test_the_walk_order_is_stable(self):
        """Deux exécutions produisent le même rapport : sinon il n'est pas relisible."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("b.py", "a.py", "c.py"):
                _write(root, f"sub/{name}", "x\n")
            first = [c.relative for c in tp.collect(root).candidates]
            second = [c.relative for c in tp.collect(root).candidates]
        self.assertEqual(first, sorted(first))
        self.assertEqual(first, second)

    def test_a_file_that_vanishes_mid_walk_is_skipped_not_fatal(self):
        """Un fichier disparu entre le parcours et la lecture ne fait pas échouer l'export."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            phantom = (str(root), [], ["fantome.py"])
            with mock.patch.object(os, "walk", side_effect=lambda _root: iter([phantom])):
                harvest = tp.collect(root)
        self.assertEqual(harvest.candidates, [])
        self.assertEqual(harvest.skipped_files, [])


class StrayTest(unittest.TestCase):
    def test_root_droppings_are_named(self):
        self.assertEqual(tp.stray_reason("_scratch.py", 12), "nom d'essai (`_…`)")
        self.assertEqual(tp.stray_reason("git", 0), "fichier vide")
        self.assertEqual(tp.stray_reason("notes.bak", 4), "nom de sauvegarde")

    def test_conventions_elsewhere_are_not_droppings(self):
        """Hors racine, un fichier vide ou un nom privé est une convention."""
        self.assertIsNone(tp.stray_reason("pkg/py.typed", 0))
        self.assertIsNone(tp.stray_reason("pkg/_private.py", 10))

    def test_an_ordinary_root_file_is_kept(self):
        self.assertIsNone(tp.stray_reason("README.md", 20))
        self.assertIsNone(tp.stray_reason("pyproject.toml", 300))


# --------------------------------------------------------------------------- #
# 2. L'empreinte : identité de contenu, et rien d'autre
# --------------------------------------------------------------------------- #


class BlobIdTest(unittest.TestCase):
    def test_it_matches_gits_own_identity(self):
        self.assertEqual(tp.blob_id(b""), EMPTY_BLOB)

    def test_one_byte_changes_the_identity(self):
        self.assertNotEqual(tp.blob_id(b"x = 1\n"), tp.blob_id(b"x = 2\n"))

    def test_the_readable_fingerprint_is_a_sha256(self):
        digest = tp.sha256_of(b"bonjour\n")
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, tp.blob_id(b"bonjour\n"))

    def test_without_a_repository_the_default_is_declared(self):
        """Hors dépôt, `rev-parse` échoue : le défaut est rendu, pas deviné au hasard."""
        self.assertEqual(tp.object_format(REPO_ROOT), "sha1")


class CompareTest(unittest.TestCase):
    """La vérification : ce qui est comparable, et ce qui doit faire échouer."""

    def test_identical_bytes_are_proven_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "a.py", "x = 1\n")
            stored = {"a.py": tp.blob_id(b"x = 1\n")}
            report = tp.compare(root, stored)
        self.assertEqual(report["identical"], ["a.py"])
        self.assertEqual(report["drift"], [])
        self.assertEqual(report["files"], 1)

    def test_a_line_ending_is_normalized_and_named(self):
        """Le seul écart que `.gitattributes` autorise — et il est compté à part."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "gradlew.bat", b"@echo off\r\ncall gradle\r\n")
            stored = {"gradlew.bat": tp.blob_id(b"@echo off\ncall gradle\n")}
            report = tp.compare(root, stored)
        self.assertEqual(report["identical"], [])
        self.assertEqual(report["normalized"], ["gradlew.bat"])
        self.assertEqual(report["drift"], [])

    def test_any_other_difference_is_a_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "a.py", "x = 1\n")
            stored = {"a.py": tp.blob_id(b"x = 2\n")}
            report = tp.compare(root, stored)
        self.assertEqual(report["identical"], [])
        self.assertEqual(report["normalized"], [])
        self.assertEqual(len(report["drift"]), 1)
        self.assertEqual(report["drift"][0]["path"], "a.py")
        self.assertEqual(report["drift"][0]["poste"], tp.sha256_of(b"x = 1\n"))
        self.assertEqual(report["drift"][0]["branche"], tp.blob_id(b"x = 2\n"))

    def test_a_published_file_missing_at_the_post_is_a_drift_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = tp.compare(Path(tmp), {"disparu.py": tp.blob_id(b"x")})
        self.assertEqual(report["missing_local"], ["disparu.py"])


# --------------------------------------------------------------------------- #
# 3. Le commit et le push : ce qui est nommé, et ce qui ne l'est jamais
# --------------------------------------------------------------------------- #


class PushArgvTest(unittest.TestCase):
    def test_the_refspec_is_explicit(self):
        self.assertEqual(tp.push_argv("origin", "import/arbre-local"), [
            "git", "push", "origin", "import/arbre-local:import/arbre-local"
        ])

    def test_the_hooks_are_never_bypassed(self):
        """Les hooks sont la raison d'être de ce chemin : ils ne se contournent pas."""
        argv = tp.push_argv("origin", "import/arbre-local")
        self.assertNotIn("--no-verify", argv)
        self.assertFalse([arg for arg in argv if arg.startswith("--force")])

    def test_the_branch_is_named_twice(self):
        """`push.default` ne doit pas pouvoir décider à notre place."""
        argv = tp.push_argv("origin", "ma-branche")
        self.assertEqual(argv[-1], "ma-branche:ma-branche")


class MessageTest(unittest.TestCase):
    def test_the_message_describes_what_was_indexed(self):
        status = ["A\tnouveau.py", "M\tmodifie.py", "D\tsupprime.py"]
        text = tp.compose_message("import/arbre-local", status)
        self.assertIn("import/arbre-local", text)
        self.assertIn("1 fichier(s) ajouté(s), 1 modifié(s), 1 supprimé(s)", text)
        self.assertIn("* nouveau.py", text)
        self.assertIn("scripts/publish_tree.py", text)

    def test_a_long_list_is_counted_rather_than_truncated_silently(self):
        status = [f"M\tfichier_{index:03d}.py" for index in range(100)]
        text = tp.compose_message("branche", status)
        self.assertIn("(60 autre(s))", text)
        self.assertNotIn("fichier_099.py", text)


class GitAbsentTest(unittest.TestCase):
    """Git absent : « rien à publier » serait un mensonge, `None` est la réponse."""

    def test_the_plan_refuses_to_invent_one(self):
        with mock.patch.object(tp.subprocess, "run", side_effect=OSError("git absent")):
            self.assertIsNone(tp.plan(REPO_ROOT, Path(".")))

    def test_the_publication_says_so_and_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(tp.subprocess, "run", side_effect=OSError("git absent")):
                report = tp.publish(Path(tmp), Path(tmp) / "clone")
        self.assertFalse(report["ok"])
        self.assertTrue(report["problems"])

    def test_a_failed_program_is_named(self):
        done = tp.subprocess.CompletedProcess([], 128, b"", b"fatal: pas un depot")
        self.assertIn("pas un depot", tp.failure(done, "test"))


# --------------------------------------------------------------------------- #
# 4. Le geste complet, sur un dépôt nu jetable
# --------------------------------------------------------------------------- #


class PublishEndToEndTest(RemoteTestCase):
    """Export, commit, push, vérification — sur un vrai dépôt nu.

    Ce que la maquette ne couvre pas : les hooks du dépôt (l'arbre de test n'a pas
    de `.githooks`, ce qui les rend inactifs — et c'est voulu : leur comportement
    a ses propres épreuves de bout en bout).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.local = base / "poste"
        self.remote = base / "distant.git"
        self.work_tree = base / "dossier-de-travail"
        self.env = _git_env(base)

        _write(self.local, "README.md", "bonjour\n")
        _write(self.local, "pkg/mod.py", "x = 1\n")
        _write(self.local, ".gitignore", "secret.json\n.env\n")
        _write(self.local, "secret.json", '{"cle": "valeur"}\n')
        _write(self.local, ".env", "SUPABASE_URL=https://exemple.invalid\n")
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    # -- échafaudage -------------------------------------------------------- #

    def _publish(self, **kwargs):
        """Publie, en réessayant un incident de **lancement** — jamais un refus.

        Rejouer une publication n'écrit rien de plus : elle recopie ce qui diffère
        (rien, la fois suivante), ne commite que s'il y a un diff, et pousse la
        même référence. Ce que le réessai ne fait jamais, c'est absorber un refus :
        il n'est déclenché que par la signature d'un transport mort **sans cause
        nommée**, et après trois essais le test est **passé** (l'incident est un
        comportement de la machine ; la CI Linux, elle, l'exécute pour de bon).
        """
        report = None
        for _ in range(3):
            with mock.patch.dict(os.environ, self.env):
                report = tp.publish(
                    self.local,
                    self.work_tree,
                    branch=self.branch,
                    url=str(self.remote),
                    **kwargs,
                )
            push = report.get("push") or {}
            if not _transport_trouble(*[*(report.get("problems") or []), push.get("error") or ""]):
                return report
        self.skipTest(f"transport mort avant toute écriture (saturation de fork MSYS2) ; {_why(report)}")
        raise AssertionError("inatteignable : `skipTest` lève toujours")

    # -- les épreuves ------------------------------------------------------- #

    def test_it_publishes_and_proves_every_file(self):
        report = self._publish()

        self.assertTrue(report["ok"], _why(report))
        # Le `.gitignore` local fait partie de ce qui part : c'est lui qui gouverne
        # tout le reste, et il doit donc être sur la branche.
        self.assertEqual(report["copied"], 3, "README.md, .gitignore et pkg/mod.py")
        verification = report["verification"]
        self.assertEqual(verification["files"], 3)
        self.assertEqual(
            sorted(verification["identical"]), [".gitignore", "README.md", "pkg/mod.py"]
        )
        self.assertEqual(verification["drift"], [])
        self.assertEqual(verification["normalized"], [])
        self.assertEqual(verification["missing_local"], [])

        entries = self._remote_entries()
        self.assertEqual(sorted(entries), [".gitignore", "README.md", "pkg/mod.py"])
        self.assertEqual(tp.blob_id(self.local.joinpath("README.md").read_bytes()), entries["README.md"])

    def test_the_local_secrets_never_reach_the_branch(self):
        report = self._publish()

        entries = self._remote_entries()
        self.assertNotIn(".env", entries)
        self.assertNotIn("secret.json", entries)
        names = [item["path"] for item in report["plan"]["excluded"]]
        self.assertIn("secret.json", names)
        self.assertIn(".env", names)

    def test_a_commit_is_one_and_carries_the_diff(self):
        report = self._publish()

        commit = report["commit"]
        self.assertIsNotNone(commit["sha"])
        self.assertEqual(commit["files"], 3, "README.md, pkg/mod.py, .gitignore")

    def test_a_second_publish_only_sends_what_changed(self):
        first = self._publish()
        _rewrite(self.local, "pkg/mod.py", "x = 2\n")
        second = self._publish()

        self.assertTrue(second["ok"], _why(second))
        self.assertEqual(second["copied"], 1)
        self.assertNotEqual(second["commit"]["sha"], first["commit"]["sha"])

    def test_a_change_that_is_not_published_is_a_drift(self):
        """Le cœur de la promesse : modifié au poste sans publier ⇒ la vérification le voit."""
        self._publish()
        _rewrite(self.local, "pkg/mod.py", "x = 99\n")

        entries = self._remote_entries()
        report = tp.compare(self.local, entries, tp.object_format(self.work_tree))

        self.assertEqual(report["drift"][0]["path"], "pkg/mod.py")
        self.assertEqual(report["drift"][0]["branche"], entries["pkg/mod.py"])
        self.assertNotEqual(report["drift"][0]["poste"], entries["pkg/mod.py"])

    def test_nothing_to_send_is_not_a_failure(self):
        self._publish()
        again = self._publish()

        self.assertTrue(again["ok"], again["problems"])
        self.assertEqual(again["copied"], 0)
        self.assertEqual(again["commit"]["files"], 0)
        self.assertIsNone(again["commit"]["sha"], "rien à committer ⇒ aucun commit")
        self.assertIsNone(again["push"]["error"])

    def test_a_file_the_post_no_longer_has_leaves_the_branch(self):
        self._publish()
        (self.local / "pkg" / "mod.py").unlink()
        report = self._publish()

        self.assertTrue(report["ok"], _why(report))
        self.assertEqual(report["plan"]["prunes"], ["pkg/mod.py"])
        entries = self._remote_entries()
        self.assertEqual(sorted(entries), [".gitignore", "README.md"])

    def test_a_dropping_is_refused_named_and_publishable_on_request(self):
        _write(self.local, "_scratch.py", "print('essai')\n")
        refused = self._publish()

        self.assertEqual([item["path"] for item in refused["plan"]["strays"]], ["_scratch.py"])
        self.assertNotIn("_scratch.py", self._remote_entries())

        accepted = self._publish(include_strays=True)
        self.assertTrue(accepted["ok"], _why(accepted))
        self.assertEqual(accepted["plan"]["strays"], [])
        self.assertIn("_scratch.py", self._remote_entries())

    def test_an_empty_root_file_is_a_dropping_too(self):
        _write(self.local, "git", b"")
        report = self._publish()

        self.assertEqual([item["path"] for item in report["plan"]["strays"]], ["git"])
        self.assertIn("fichier vide", report["plan"]["strays"][0]["reason"])

    def test_an_impossible_write_is_named_and_stops_before_the_commit(self):
        """Une copie qui échoue ne part pas en silence, et rien n'est committé après elle."""
        self._publish()
        before = self._remote_head()
        # Un dossier là où le poste a un fichier : la copie ne peut pas aboutir.
        (self.work_tree / "pkg" / "mod.py").unlink()
        (self.work_tree / "pkg" / "mod.py").mkdir()
        _rewrite(self.local, "pkg/mod.py", "x = 2\n")

        report = self._publish()

        self.assertFalse(report["ok"])
        self.assertEqual([Path(path).name for path in report["plan"]["copies"]], ["mod.py"])
        self.assertIn("écriture(s) impossible(s)", report["problems"][0])
        self.assertIn("pkg/mod.py", report["problems"][0])
        # Le geste s'arrête **avant** le commit : une copie manquante ferait partir
        # une branche qui n'est pas le miroir du poste.
        self.assertIsNone(report["commit"], "aucun commit ne doit partir")
        self.assertEqual(self._remote_head(), before)

    def test_the_dry_run_writes_nothing(self):
        self._publish()
        before = self._remote_head()
        _rewrite(self.local, "pkg/mod.py", "x = 3\n")

        report = self._publish(dry_run=True)

        self.assertTrue(report["dry_run"])
        self.assertEqual([Path(path).name for path in report["plan"]["copies"]], ["mod.py"])
        self.assertEqual(self._remote_head(), before)
        # La copie de travail du clone n'a pas bougé non plus : sans publication,
        # la modification du poste est toujours une dérive.
        verification = tp.compare(self.local, self._remote_entries(), tp.object_format(self.work_tree))
        self.assertEqual([item["path"] for item in verification["drift"]], ["pkg/mod.py"])

    def test_a_failed_fetch_stops_before_anything_is_touched(self):
        """Un `fetch` qui échoue **nommément** arrête tout, avant la bascule de branche.

        C'est l'incident réel : une référence de suivi périmée (le `fetch` était mort
        sur un `fork` saturé) a fait **reculer** la branche locale, puis le push a été
        refusé en non-fast-forward — après qu'un commit eut été écrit par-dessus un
        état ancien. Un distant injoignable doit donc arrêter le geste, pas le
        laisser suivre une référence qu'il n'a pas relue.
        """
        self._publish()
        head = self._head()
        self._git("remote", "set-url", "origin", str(Path(self._tmp.name) / "absent.git"))
        _rewrite(self.local, "pkg/mod.py", "x = 7\n")

        report = self._publish()

        self.assertFalse(report["ok"])
        self.assertIn("récupération", report["problems"][0])
        self.assertIn("rien n'a été touché", report["problems"][0])
        self.assertEqual(self._head(), head, "aucune copie, aucun commit")

    def test_a_narrowed_fetch_refspec_cannot_make_the_branch_go_backwards(self):
        """Constaté : `git fetch <distant>` n'obéit qu'à `remote.<distant>.fetch`.

        Sur un clone qui ne suit qu'une autre branche (`--single-branch`, ou
        `git remote set-branches`), la référence de suivi de la branche publiée
        **ne bouge pas**, la bascule de branche fait reculer le clone, et le push
        est refusé en non-fast-forward. La récupération nomme donc sa branche.
        """
        self._publish()
        self._git("config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")
        moved = self._move_the_remote_branch()

        report = self._publish()

        self.assertTrue(report["ok"], _why(report))
        # Le commit local **descend** du distant : le clone a suivi au lieu de reculer.
        self.assertTrue(
            self._is_ancestor(moved, "HEAD"),
            "le clone est parti d'un état antérieur au distant",
        )
        self.assertEqual(sorted(self._remote_entries()), [".gitignore", "README.md", "pkg/mod.py"])

    def test_local_commits_the_remote_lacks_are_reported(self):
        """Un miroir se rebâtit depuis le poste — mais un recul se **dit**."""
        self._publish()
        self._git("commit", "--allow-empty", "-m", "commit local que le distant n'a pas")
        self.assertNotEqual(self._head(), self._remote_head())

        report = self._publish()

        self.assertTrue(report["ok"], _why(report))
        self.assertEqual(report["work_tree_state"]["resets"], 1)
        self.assertEqual(self._head(), self._remote_head(), "la branche suit le distant")

    def test_a_work_tree_that_is_not_a_repository_is_named(self):
        self.work_tree.mkdir(parents=True)
        _write(self.work_tree, "intrus.txt", "déjà là\n")
        report = self._publish()

        self.assertFalse(report["ok"])
        self.assertIn("sans être un dépôt git", report["problems"][0])

    def test_a_missing_url_for_the_first_clone_is_a_named_refusal(self):
        with mock.patch.dict(os.environ, self.env):
            report = tp.publish(self.local, self.work_tree, branch=self.branch)
        self.assertFalse(report["ok"])
        self.assertIn("--url", report["problems"][0])


# --------------------------------------------------------------------------- #
# 5. La CLI
# --------------------------------------------------------------------------- #


class CliTest(RemoteTestCase):
    """Le rendu : codes de sortie, sortie machine, et ce que le rapport dit à l'œil."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.local = base / "poste"
        self.remote = base / "distant.git"
        self.work_tree = base / "dossier-de-travail"
        self.env = _git_env(base)
        _write(self.local, "README.md", "bonjour\n")
        _write(self.local, ".env", "SECRET=1\n")
        self._seed()

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, argv):
        """Exécute la CLI, en réessayant un incident de transport (jamais un refus)."""
        last = (1, "", "")
        for _ in range(3):
            with mock.patch.dict(os.environ, self.env):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = cli.main(
                        [
                            "--root",
                            str(self.local),
                            "--work-tree",
                            str(self.work_tree),
                            "--url",
                            str(self.remote),
                            *argv,
                        ]
                    )
            last = (code, out.getvalue(), err.getvalue())
            if not _transport_trouble(last[1]):
                return last
        self.skipTest(f"transport mort avant toute écriture (saturation de fork MSYS2) ; {last[1][:200]}")
        raise AssertionError("inatteignable : `skipTest` lève toujours")

    def test_a_dry_run_on_an_absent_work_tree_explains_itself(self):
        """Sans clone, le `.gitignore` du dépôt est hors de portée : le dire, pas inventer."""
        code, out, _ = self._run(["--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("dossier de travail", out)

    def test_a_full_run_ends_verified(self):
        code, out, _ = self._run([])

        self.assertEqual(code, 0, out)
        self.assertIn("Publié et vérifié", out)
        self.assertIn("hors périmètre", out)
        self.assertIn(".env", out)
        # Le refspec est **montré**, pas suggéré : ce qui part est nommé.
        self.assertIn("git push origin import/arbre-local:import/arbre-local", out)

    def test_the_json_report_is_machine_readable(self):
        code, out, _ = self._run(["--json"])

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["branch"], tp.DEFAULT_BRANCH)
        self.assertIn("verification", payload)

    def test_a_dropping_is_refused_by_default_and_counted(self):
        _write(self.local, "_essai.py", "x = 1\n")
        code, out, _ = self._run(["--json"])

        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual([item["path"] for item in payload["plan"]["strays"]], ["_essai.py"])

    def test_the_default_branch_is_the_documented_one(self):
        self.assertEqual(cli._build_parser().parse_args([]).branch, "import/arbre-local")


if __name__ == "__main__":
    unittest.main()
