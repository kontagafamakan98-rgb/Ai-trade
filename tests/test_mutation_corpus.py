"""Le corpus d'épreuves de mutation est vérifié — et vérifiable.

`.pgtest/` n'est pas versionné : en CI il n'existe pas, et ces vérifications s'y
ignorent proprement — elles n'ont rien à lire. Là où le corpus est présent, cinq
questions se posent, et aucune ne se déduit des autres :

1. **chaque épreuve est-elle gardée ?** Un `mutate_*.py` sans
   `if __name__ == "__main__":` s'exécute à l'import : l'importer mute un fichier
   de production. C'est arrivé — `mutate_operator` réécrivait la migration 009 en
   se faisant importer par l'audit des ancres ;
2. **chaque épreuve survit-elle à sa propre interruption ?** Un `finally` ne
   s'exécute pas sur un `SIGKILL` : la mutation reste alors sur le disque, et
   c'est un test, plus tard, qui la découvre. Chaque épreuve doit donc installer
   `mutation_guard.watch(__file__)`, qui répare au démarrage ce qu'une exécution
   précédente a laissé ;
3. **les ancres résolvent-elles encore, et désignent-elles un site ?** Une ancre
   périmée (0 occurrence) ou un motif qui vit deux fois ne cassent plus ce que
   l'épreuve annonce : c'est le mode de panne le plus silencieux du corpus. Les
   entrées que la lecture statique ne sait pas évaluer sont **nommées**, jamais
   comptées vertes, et leur compte est figé ici pour qu'une entrée devenue
   illisible ailleurs se voie tout de suite. Une mutation à **effet** échappe à
   cette lecture tant qu'elle ne **déclare** pas sa cible : depuis que
   `mutate_packages` déclare les siennes, plus aucune entrée du corpus n'est non
   lue. Et là où le site est le fichier lui-même (un déplacement, aucun texte à
   compter), la lecture le dit *cible* — le compte est figé ici aussi, pour qu'une
   ancre ne puisse pas se changer en simple chemin sans que ça se voie ;
4. **reste-t-il une mutation en place ?** Un journal qui protège encore un fichier
   qui n'est plus d'origine est une mutation vivante : `mutation_guard.py
   --repair` la remet en place sans lancer les tests ;
5. **importer le corpus écrit-il quelque chose ?** Non : le dépôt est empreint
   avant et après les imports, comme le fait l'audit lui-même.

La lecture est celle de l'audit (`.pgtest/audit_anchors.py`), **importée** et non
recopiée : deux lecteurs divergents seraient pires que pas de lecteur du tout. Le
garde-fou, lui, est éprouvé sur des fichiers **fabriqués** dans un répertoire
temporaire : installer le patch global (`mutation_guard.watch`) ici journaliserait
les écritures de toute la suite.
"""

import contextlib
import io
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
PGTEST = ROOT / ".pgtest"
HARNESSES = sorted(PGTEST.glob("mutate_*.py")) if PGTEST.is_dir() else []

#: Une garde d'exécution : sans elle, importer l'épreuve l'exécute.
MAIN_GUARD = 'if __name__ == "__main__":'

#: Les mutations que la lecture statique ne peut pas évaluer : à effet (fichiers
#: écrits et déplacés), elles ne sont jamais appelées. Aucune ne doit rester : une
#: mutation à effet qui ne **déclare** pas sa cible est aveugle pour cet audit.
UNREAD_ALLOWED = {}

#: Les mutations dont le site est le **fichier** lui-même — un déplacement, qui ne
#: remplace aucun texte. Autorisé, mais figé : une ancre qui se change en simple
#: chemin rétrécirait la lecture sans rien casser d'autre.
TARGET_ONLY_ALLOWED = {"mutate_packages": 1}


def audit_tool():
    """L'audit des ancres, chargé depuis `.pgtest` — une seule réponse par question.

    Le charger ajoute `.pgtest` à `sys.path` (il y importe son garde-fou) : on
    remet la liste dans son état d'origine ensuite, pour ne rien changer au reste
    de la suite. Le module reste chargé, donc lu une seule fois.
    """
    saved = list(sys.path)
    try:
        sys.path.insert(0, str(PGTEST))
        import audit_anchors
    finally:
        sys.path[:] = saved
    return audit_anchors


@unittest.skipUnless(HARNESSES, "corpus de mutation absent (.pgtest n'est pas versionné)")
class MutationCorpusTest(unittest.TestCase):
    """Ce que chaque épreuve présente doit garantir, vu par la lecture statique."""

    @classmethod
    def setUpClass(cls):
        cls.audit = audit_tool()

    def test_every_harness_has_a_runtime_guard(self):
        """Sans garde `__main__`, un import exécute l'épreuve — donc mute la production."""
        bare = [
            path.stem for path in HARNESSES if MAIN_GUARD not in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(bare, [], f"épreuve(s) sans garde `__main__` : {bare}")

    def test_every_harness_carries_the_interruption_guard(self):
        """Une épreuve tuée en pleine mutation doit se réparer au passage suivant."""
        missing = self.audit.unprotected()
        self.assertEqual(
            missing,
            [],
            "épreuve(s) sans garde-fou d'interruption (`mutation_guard.watch(__file__)`) — "
            f"voir `.pgtest/install_guard.py` : {missing}",
        )

    def test_every_anchor_resolves_and_names_one_site(self):
        """Une ancre périmée ou ambiguë est une épreuve qui ne casse plus rien."""
        total = 0
        broken = []
        unread = {}
        target_only = {}
        for path in HARNESSES:
            module = self.audit.load(path)
            self.assertIsNotNone(module, f"{path.stem} : import impossible")
            entries = list(getattr(module, "MUTATIONS", ()) or ())
            if not entries:
                continue
            total += len(entries)
            noise = io.StringIO()
            with contextlib.redirect_stdout(noise):
                _ok, leftover, failed, unseen, targets = self.audit.audit(module)
            if failed or leftover:
                broken.append(
                    f"{path.stem} : {failed} ancre(s) cassée(s), {leftover} reste(s)\n"
                    f"{noise.getvalue()}"
                )
            if unseen:
                unread[path.stem] = len(unseen)
            if targets:
                target_only[path.stem] = len(targets)
        self.assertGreater(total, 0, "le corpus est là, mais aucune mutation n'est lisible")
        self.assertEqual(broken, [], "\n".join(broken))
        self.assertEqual(
            unread,
            UNREAD_ALLOWED,
            "entrée(s) non lue(s) : à décider, jamais à compter vertes",
        )
        self.assertEqual(
            target_only,
            TARGET_ONLY_ALLOWED,
            "entrée(s) vérifiée(s) par leur seule cible : une ancre a-t-elle disparu ?",
        )

    def test_no_mutation_was_left_in_place(self):
        """Un journal qui protège un fichier non d'origine est une mutation vivante."""
        still = self.audit.mutation_guard.leftovers()
        self.assertEqual(
            still,
            [],
            "mutation(s) laissée(s) en place — `python .pgtest/mutation_guard.py --repair`",
        )

    def test_importing_the_corpus_writes_nothing(self):
        """Importer une épreuve n'a pas le droit de toucher un fichier de production."""
        before = self.audit.snapshot()
        self.assertGreater(len(before), 0, "l'empreinte ne couvre aucun fichier de production")
        for path in HARNESSES:
            with contextlib.redirect_stdout(io.StringIO()):  # les avertissements sont couverts
                self.audit.load(path)
        after = self.audit.snapshot()
        changed = sorted(
            self.audit.show(path) for path, digest in before.items() if after.get(path) != digest
        )
        created = sorted(self.audit.show(path) for path in after if path not in before)
        self.assertEqual(changed, [], "fichier(s) de production modifiés par un import")
        self.assertEqual(created, [], "fichier(s) de production créés par un import")


@unittest.skipUnless(HARNESSES, "garde-fou absent (.pgtest n'est pas versionné)")
class MutationGuardTest(unittest.TestCase):
    """Le garde-fou, éprouvé sur des fichiers **fabriqués** dans un répertoire temporaire.

    `ROOT` est détourné vers ce répertoire : le garde-fou croit protéger le dépôt,
    et le test peut donc le tuer et le réparer sans approcher un seul fichier de
    production. Les deux points d'entrée du patch — journaliser avant la première
    écriture, constater après chaque écriture — sont appelés directement : le patch
    global, lui, appartient aux épreuves (l'installer ici journaliserait toutes les
    écritures de la suite, jusqu'à son `atexit`).
    """

    HARNESS = "mutate_probe.py"

    def setUp(self):
        self.guard = audit_tool().mutation_guard
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="mutation-guard-")).resolve()
        self.journals = self.root / "journals"
        self.patcher = mock.patch.multiple(self.guard, ROOT=self.root, JOURNAL_ROOT=self.journals)
        self.patcher.start()
        self.guard._directory = self.journals / pathlib.Path(self.HARNESS).stem
        self.guard._index = self.guard._directory / "index.json"
        self.guard._entries = {}
        self.addCleanup(self.patcher.stop)  # en dernier : le patch couvre tous les nettoyages
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(setattr, self.guard, "_index", None)
        self.addCleanup(setattr, self.guard, "_directory", None)
        self.addCleanup(self.guard._entries.clear)

    def test_the_original_bytes_are_journaled_before_the_first_write(self):
        """La journalisation précède l'écriture : sinon elle ne sauverait rien."""
        target = self.root / "media.py"
        target.write_bytes(b"original\n")
        self.guard._before_write(target)
        entries = self.guard.read_index(self.guard._directory)
        self.assertEqual([entry["path"] for entry in entries], ["media.py"])
        self.assertEqual((self.guard._directory / entries[0]["blob"]).read_bytes(), b"original\n")

    def test_a_killed_run_is_repaired_at_the_next_start(self):
        """Le cas vécu : tué entre l'écriture et son `finally`, le processus laisse la mutation."""
        target = self.root / "media.py"
        target.write_bytes(b"original\n")
        self.guard._before_write(target)
        target.write_bytes(b"mutated\n")  # l'épreuve est tuée ici : pas de `finally`
        self.assertEqual(self.guard.leftovers(), ["media.py"])
        repaired = self.guard.recover(self.HARNESS)
        self.assertEqual(repaired, ["media.py"])
        self.assertEqual(target.read_bytes(), b"original\n")
        self.assertFalse(self.guard._directory.exists(), "le journal doit disparaître")
        self.assertEqual(self.guard.leftovers(), [])

    def test_a_normal_restore_empties_the_journal(self):
        """Une épreuve qui va au bout ne laisse ni mutation ni journal."""
        target = self.root / "media.py"
        target.write_bytes(b"original\n")
        self.guard._before_write(target)
        target.write_bytes(b"mutated\n")
        self.assertTrue(self.guard._directory.exists(), "la mutation doit être journalisée")
        target.write_bytes(b"original\n")  # le `finally` de l'épreuve
        self.guard._settle(target)
        self.assertFalse(self.guard._directory.exists(), "un journal vide doit disparaître")

    def test_a_file_created_by_the_mutation_is_removed(self):
        """Rien à restaurer : la réparation, c'est la suppression."""
        target = self.root / "appeared.py"
        self.guard._before_write(target)
        target.write_bytes(b"cree par la mutation\n")
        self.assertEqual(self.guard.recover(self.HARNESS), ["appeared.py (supprimé)"])
        self.assertFalse(target.exists())

    def test_a_moved_file_is_moved_back(self):
        """Un déplacement est une mutation comme une autre."""
        source = self.root / "media.py"
        moved = self.root / "theme" / "media.py"
        source.write_bytes(b"original\n")
        self.guard._before_move(source, moved)
        moved.parent.mkdir(parents=True)
        shutil.move(str(source), str(moved))
        self.assertEqual(self.guard.recover(self.HARNESS), ["media.py (remis en place)"])
        self.assertEqual(source.read_bytes(), b"original\n")
        self.assertFalse(moved.exists())

    def test_the_undo_move_does_not_leave_the_journal_open(self):
        """Le déplacement inverse ne se journalise pas : sans cela le journal ne se referme jamais."""
        source = self.root / "media.py"
        moved = self.root / "theme" / "media.py"
        source.write_bytes(b"original\n")
        self.guard._before_move(source, moved)
        moved.parent.mkdir(parents=True)
        shutil.move(str(source), str(moved))
        self.assertTrue(self.guard._is_undo(moved), "le déplacement inverse doit être reconnu")
        self.guard._before_move(moved, source)
        shutil.move(str(moved), str(source))
        self.guard._settle(source)
        self.assertFalse(self.guard._directory.exists(), "l'aller-retour doit refermer le journal")
        self.assertEqual(self.guard.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
