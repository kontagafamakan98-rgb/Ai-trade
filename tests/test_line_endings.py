"""Tests du contrôle des fins de ligne (`scripts/check_line_endings.py`).

Trois questions, et aucune ne se déduit des deux autres :

1. le **dépôt** est-il conforme à ce que `.editorconfig` déclare ? C'est le gate
   lui-même — il a de quoi échouer : 71 fichiers étaient en CRLF avant que ce
   script n'existe, dont tout le Kotlin de l'interface ;
2. le **détecteur** trouve-t-il vraiment ce qu'il annonce ? Un contrôle qui ne
   détecte rien passe au vert sans rien contrôler, donc chaque règle est éprouvée
   sur des fichiers fabriqués (CRLF, fins de ligne hétérogènes, fin de ligne
   finale absente, octets non-UTF-8) ;
3. la règle vient-elle de la **configuration** ? `end_of_line = crlf` doit
   retourner le contrôle au lieu d'être combattu — sinon ce test serait un
   deuxième `.editorconfig`, en désaccord possible avec le premier. Et elle en
   vient **par fichier** : `[*.{bat,cmd}] end_of_line = crlf` est honorée, pas
   contournée, et `[*.kt]` qui ne dit rien ne déplace rien.

Et une quatrième, sur la **correction** : `--fix` normalise, mais refuse de
trancher un fichier à fins de ligne hétérogènes (le réécrire effacerait la trace
de ce qui l'a produit).
"""

import contextlib
import io
import pathlib
import sys
import tempfile
import types
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_line_endings.py"
EDITORCONFIG = REPO_ROOT / ".editorconfig"

# Chargé depuis la **source**, sans passer par le cache de bytecode. Deux versions
# du script de la même longueur, écrites dans la même seconde, portent la même
# empreinte (taille, date) : Python servirait alors le `.pyc` de la précédente, et
# ce test jugerait un autre code que celui du dépôt. Le harnais de mutation
# (`.pgtest/mutate_line_endings.py`) l'a fait apparaître — il annonçait « non
# détectée » une mutation qui l'était.
#
# Le module est enregistré dans `sys.modules` **avant** d'exécuter son code : une
# classe se cherche elle-même dans `sys.modules` au moment de sa définition.
checker = types.ModuleType("check_line_endings")
checker.__file__ = str(SCRIPT_PATH)
sys.modules["check_line_endings"] = checker
exec(compile(SCRIPT_PATH.read_bytes(), str(SCRIPT_PATH), "exec"), checker.__dict__)

LF_RULES = checker.Rules(end_of_line="lf", insert_final_newline=True, charset="utf-8")


class Workspace:
    """Arborescence jetable, écrite **en octets** (c'est tout le sujet)."""

    def __init__(self, test: unittest.TestCase) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def write(self, name: str, data: bytes) -> pathlib.Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def config(self, text: str) -> pathlib.Path:
        return self.write(".editorconfig", text.encode("utf-8"))

    def violations(self, rules=LF_RULES) -> list:
        return checker.find_violations(self.root, rules)

    def check(self, rules=LF_RULES) -> tuple[int, str]:
        stream = io.StringIO()
        code = checker.check(self.root, rules, stream=stream)
        return code, stream.getvalue()

    def fix(self, rules=LF_RULES) -> tuple[int, str]:
        stream = io.StringIO()
        code = checker.fix(self.root, rules, stream=stream)
        return code, stream.getvalue()

    def cli(self, *argv: str) -> tuple[int, str]:
        """La ligne de commande, sortie capturée : le vrai chemin d'un utilisateur."""
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            code = checker.main(["--root", str(self.root), *argv])
        return code, stream.getvalue()


class RepositoryTest(unittest.TestCase):
    """Le dépôt réel : c'est ce test qui échoue quand une fin de ligne revient."""

    def test_the_declared_rules_are_the_ones_this_gate_defends(self) -> None:
        rules = checker.declared_rules(EDITORCONFIG)
        self.assertEqual(rules.end_of_line, "lf")
        self.assertTrue(rules.insert_final_newline)
        self.assertEqual(rules.charset, "utf-8")

    def test_no_text_file_contradicts_the_editorconfig(self) -> None:
        violations = checker.find_violations(REPO_ROOT, checker.declared_rules(EDITORCONFIG))
        self.assertEqual(
            violations,
            [],
            "fins de ligne en dérive — corriger avec : python scripts/check_line_endings.py --fix",
        )

    def _walked(self) -> set:
        """Les noms du parcours, en séparateurs POSIX quelle que soit la machine."""
        return {
            checker.relative_name(path, REPO_ROOT) for path in checker.text_files(REPO_ROOT)
        }

    def test_the_walk_covers_the_repository(self) -> None:
        """Un parcours vide ou trop filtré passerait le test sans rien regarder."""
        relative = self._walked()
        self.assertGreater(len(relative), 200, "le dépôt compte bien plus de fichiers texte")
        for name in (
            ".editorconfig",
            ".gitignore",
            ".githooks/pre-push",
            "README.md",
            "main.py",
            "database/media_store.py",
            "app/src/main/java/com/aitrade/ui/Localization.kt",
            "app/src/main/res/values-fr/strings.xml",
            "requirements.txt",
        ):
            with self.subTest(name=name):
                self.assertIn(name, relative)

    def test_the_sandbox_is_not_part_of_the_repository(self) -> None:
        """`.pgtest` est ignoré par git : ses scripts y écrivent ce qu'ils veulent."""
        self.assertFalse(
            [name for name in self._walked() if name.startswith(".pgtest/")]
        )

    def test_the_windows_launcher_is_declared_apart(self) -> None:
        """`gradlew.bat` (six `goto`) est le seul fichier déclaré à part, et lu comme tel."""
        sections = checker.declared_sections(EDITORCONFIG)

        self.assertEqual(checker.rules_for_file("gradlew.bat", sections).end_of_line, "crlf")
        self.assertEqual(checker.rules_for_file("main.py", sections).end_of_line, "lf")

    def test_the_repository_is_reported_as_conforming(self) -> None:
        stream = io.StringIO()
        code = checker.check(REPO_ROOT, checker.declared_rules(EDITORCONFIG), stream=stream)

        self.assertEqual(code, 0, stream.getvalue())
        self.assertIn("conforme", stream.getvalue())


class DetectorTest(unittest.TestCase):
    """Ce que le détecteur trouve — et ce qu'il laisse tranquille."""

    def setUp(self) -> None:
        self.workspace = Workspace(self)

    def test_a_crlf_file_is_named_with_its_count(self) -> None:
        self.workspace.write("module.py", b"un\r\ndeux\r\ntrois\r\n")
        violations = self.workspace.violations()

        self.assertEqual([violation.path for violation in violations], ["module.py"])
        self.assertIn("3 fin(s) de ligne CRLF", violations[0].problem)

    def test_a_lf_file_is_left_alone(self) -> None:
        self.workspace.write("module.py", b"un\ndeux\ntrois\n")
        self.assertEqual(self.workspace.violations(), [])

    def test_heterogeneous_endings_are_named_apart(self) -> None:
        """Mélanger les deux styles n'est pas une conversion oubliée.

        C'est le signe qu'un outil a tronqué ou recollé le fichier : le réécrire
        mécaniquement effacerait la trace de ce qui l'a produit.
        """
        self.workspace.write("moitie.py", b"un\r\ndeux\n")
        problems = [violation.problem for violation in self.workspace.violations()]

        self.assertEqual(len(problems), 1)
        self.assertIn("hétérogènes", problems[0])
        self.assertNotIn("CRLF là où", problems[0])

    def test_a_missing_final_newline_is_named(self) -> None:
        self.workspace.write("court.py", b"une seule ligne")
        problems = [violation.problem for violation in self.workspace.violations()]

        self.assertEqual(len(problems), 1)
        self.assertIn("fin de ligne finale", problems[0])

    def test_a_file_that_ends_well_is_not_reported_twice(self) -> None:
        """La fin de ligne finale manquante ne doit pas se lire aussi comme un CRLF."""
        self.workspace.write("propre.py", b"ligne\n")
        self.assertEqual(self.workspace.violations(), [])

    def test_an_empty_file_is_not_a_violation(self) -> None:
        self.workspace.write("vide.py", b"")
        self.assertEqual(self.workspace.violations(), [])

    def test_bytes_that_are_not_utf8_are_named(self) -> None:
        self.workspace.write("latin.py", "café = 1\n".encode("latin-1"))
        problems = [violation.problem for violation in self.workspace.violations()]

        self.assertEqual(len(problems), 1)
        self.assertIn("UTF-8", problems[0])

    def test_a_binary_file_is_not_judged(self) -> None:
        self.workspace.write("blob.py", b"\x00\x01\x02\r\n\x00")
        self.assertEqual(self.workspace.violations(), [])

    def test_an_excluded_suffix_is_not_judged(self) -> None:
        """Le vocabulaire des suffixes vient de l'audit des secrets, pas d'ici."""
        self.workspace.write("image.png", b"pas du png\r\n")
        self.assertEqual(self.workspace.violations(), [])

    def test_an_excluded_directory_is_not_judged(self) -> None:
        for name in (".venv/lib/module.py", "build/output.txt", "__pycache__/x.py"):
            self.workspace.write(name, b"un\r\ndeux\r\n")
        self.assertEqual(self.workspace.violations(), [])

    # -- la règle vient de la configuration, pas du test -------------------- #

    def test_the_rule_follows_the_declared_end_of_line(self) -> None:
        """`end_of_line = crlf` : le contrôle suit la configuration au lieu de la combattre."""
        crlf_rules = checker.Rules(end_of_line="crlf", insert_final_newline=True, charset="utf-8")
        self.workspace.write("module.py", b"un\r\ndeux\r\n")

        self.assertEqual(self.workspace.violations(crlf_rules), [])

        self.workspace.write("module.py", b"un\ndeux\n")
        problems = [violation.problem for violation in self.workspace.violations(crlf_rules)]
        self.assertEqual(len(problems), 1)
        self.assertIn("LF là où", problems[0])

    def test_the_final_newline_rule_follows_the_configuration(self) -> None:
        relaxed = checker.Rules(end_of_line="lf", insert_final_newline=False, charset="utf-8")
        self.workspace.write("court.py", b"une seule ligne")
        self.assertEqual(self.workspace.violations(relaxed), [])

    def test_the_repository_default_is_the_root_section(self) -> None:
        """`declared_rules` est le **défaut** : une section par langue ne le déplace pas."""
        self.workspace.config("[*]\nend_of_line = lf\n\n[*.kt]\nend_of_line = crlf\n")
        rules = checker.declared_rules(self.workspace.root / ".editorconfig")

        self.assertEqual(rules.end_of_line, "lf")

    # -- une section décide des fichiers qu'elle nomme ---------------------- #

    def test_a_section_decides_for_the_files_it_names(self) -> None:
        """Le fichier est jugé selon SA règle : un `.bat` déclaré CRLF reste conforme."""
        self.workspace.config(
            "[*]\nend_of_line = lf\n\n[*.{bat,cmd}]\nend_of_line = crlf\n"
        )
        self.workspace.write("gradlew.bat", b"un\r\ndeux\r\n")
        self.workspace.write("outil.cmd", b"un\r\ndeux\r\n")
        self.workspace.write("module.py", b"un\r\ndeux\r\n")

        self.assertEqual(
            [violation.path for violation in self.workspace.violations()], ["module.py"]
        )

    def test_a_section_without_slash_matches_at_any_depth(self) -> None:
        """EditorConfig : `[*.bat]` vise le **nom**, donc à n'importe quelle profondeur."""
        self.workspace.config("[*]\nend_of_line = lf\n\n[*.bat]\nend_of_line = crlf\n")
        self.workspace.write("tools/inner/outil.bat", b"un\r\ndeux\r\n")

        self.assertEqual(self.workspace.violations(), [])

    def test_the_last_section_that_speaks_wins(self) -> None:
        """Deux sections pour le même motif : la dernière déclaration l'emporte."""
        self.workspace.config(
            "[*]\nend_of_line = lf\n\n[*.bat]\nend_of_line = crlf\n\n[*.bat]\nend_of_line = lf\n"
        )
        sections = checker.declared_sections(self.workspace.root / ".editorconfig")

        self.assertEqual(checker.rules_for_file("gradlew.bat", sections).end_of_line, "lf")

    def test_a_section_that_says_nothing_leaves_the_default_alone(self) -> None:
        """`[*.kt]` porte les réglages de ktlint : ne rien dire n'est pas dire `lf`."""
        self.workspace.config("[*]\nend_of_line = crlf\n\n[*.kt]\nmax_line_length = 140\n")
        sections = checker.declared_sections(self.workspace.root / ".editorconfig")

        self.assertEqual(checker.rules_for_file("Main.kt", sections).end_of_line, "crlf")

    def test_an_unsupported_end_of_line_is_refused(self) -> None:
        """`end_of_line = cr` n'est pas une règle : mieux vaut le dire que deviner."""
        self.workspace.config("[*]\nend_of_line = cr\n")
        with self.assertRaises(ValueError):
            checker.declared_rules(self.workspace.root / ".editorconfig")

    def test_an_unsupported_end_of_line_is_refused_in_any_section(self) -> None:
        """Une règle qu'on ne sait pas appliquer ne doit pas pouvoir passer pour appliquée."""
        self.workspace.config("[*]\nend_of_line = lf\n\n[*.bat]\nend_of_line = cr\n")
        with self.assertRaises(ValueError):
            checker.declared_sections(self.workspace.root / ".editorconfig")


class FixTest(unittest.TestCase):
    """`--fix` : ce qu'il normalise, et ce qu'il refuse de trancher."""

    def setUp(self) -> None:
        self.workspace = Workspace(self)

    def test_a_crlf_file_is_normalized_and_then_conforms(self) -> None:
        path = self.workspace.write("module.py", b"un\r\ndeux\r\n")
        code, report = self.workspace.fix()

        self.assertEqual(code, 0, report)
        self.assertEqual(path.read_bytes(), b"un\ndeux\n")
        self.assertEqual(self.workspace.check()[0], 0, "la vérification après coup doit passer")

    def test_the_final_newline_is_added_without_touching_the_rest(self) -> None:
        path = self.workspace.write("court.py", "café = 1  ".encode("utf-8"))
        code, report = self.workspace.fix()

        self.assertEqual(code, 0, report)
        self.assertEqual(path.read_bytes(), "café = 1  \n".encode("utf-8"))

    def test_the_content_is_preserved_byte_for_byte(self) -> None:
        """La normalisation ne change que des `\\r` : rien d'autre ne bouge."""
        body = "ligne accentuée — 🎯\r\n\ttabulation  \r\n\r\n"
        path = self.workspace.write("riche.py", body.encode("utf-8"))
        self.workspace.fix()

        self.assertEqual(path.read_bytes(), body.replace("\r\n", "\n").encode("utf-8"))

    def test_fixing_twice_changes_nothing_the_second_time(self) -> None:
        self.workspace.write("module.py", b"un\r\ndeux\r\n")
        self.workspace.fix()
        code, report = self.workspace.fix()

        self.assertEqual(code, 0, report)
        self.assertIn("0 fichier(s) normalisé(s)", report)

    def test_a_heterogeneous_file_is_left_untouched(self) -> None:
        """Le script ne devine pas ce qu'un outil a voulu faire : il le dit."""
        path = self.workspace.write("moitie.py", b"un\r\ndeux\n")
        code, report = self.workspace.fix()

        self.assertEqual(code, 1)
        self.assertIn("à reprendre à la main", report)
        self.assertEqual(path.read_bytes(), b"un\r\ndeux\n", "aucune écriture")

    def test_the_fix_follows_a_crlf_configuration_too(self) -> None:
        """Si `.editorconfig` demandait `crlf`, `--fix` devrait convertir dans l'autre sens."""
        crlf_rules = checker.Rules(end_of_line="crlf", insert_final_newline=True, charset="utf-8")
        path = self.workspace.write("module.py", b"un\ndeux\n")
        code, report = self.workspace.fix(crlf_rules)

        self.assertEqual(code, 0, report)
        self.assertEqual(path.read_bytes(), b"un\r\ndeux\r\n")
        self.assertEqual(self.workspace.fix(crlf_rules)[0], 0, "idempotent")

    def test_the_fix_applies_the_section_that_covers_the_file(self) -> None:
        """`--fix` suit la section aussi : un `.bat` en LF devient CRLF, l'inverse du défaut."""
        self.workspace.config("[*]\nend_of_line = lf\n\n[*.{bat,cmd}]\nend_of_line = crlf\n")
        launcher = self.workspace.write("gradlew.bat", b"un\ndeux\n")
        module = self.workspace.write("module.py", b"un\r\ndeux\r\n")

        code, report = self.workspace.fix()

        self.assertEqual(code, 0, report)
        self.assertEqual(launcher.read_bytes(), b"un\r\ndeux\r\n")
        self.assertEqual(module.read_bytes(), b"un\ndeux\n")
        self.assertEqual(self.workspace.check()[0], 0, "la vérification après coup doit passer")

    def test_a_binary_file_is_not_rewritten(self) -> None:
        path = self.workspace.write("blob.py", b"\x00\x01\r\n\x00")
        self.workspace.fix()

        self.assertEqual(path.read_bytes(), b"\x00\x01\r\n\x00")


class CommandLineTest(unittest.TestCase):
    """Les codes de sortie : 0 conforme, 1 dérive, 2 configuration illisible."""

    def setUp(self) -> None:
        self.workspace = Workspace(self)

    def test_a_clean_tree_exits_zero(self) -> None:
        self.workspace.config("[*]\nend_of_line = lf\n")
        self.workspace.write("module.py", b"ligne\n")
        code, report = self.workspace.cli()

        self.assertEqual(code, 0, report)

    def test_a_drifting_tree_exits_one_and_names_the_fix(self) -> None:
        self.workspace.config("[*]\nend_of_line = lf\n")
        self.workspace.write("module.py", b"un\r\ndeux\r\n")
        code, report = self.workspace.cli()

        self.assertEqual(code, 1)
        self.assertIn("module.py", report)
        self.assertIn("--fix", report, "un contrôle doit dire comment se corriger")

    def test_the_fix_flag_normalizes_the_tree(self) -> None:
        self.workspace.config("[*]\nend_of_line = lf\n")

        path = self.workspace.write("module.py", b"un\r\ndeux\r\n")
        code, report = self.workspace.cli("--fix")

        self.assertEqual(code, 0, report)
        self.assertEqual(path.read_bytes(), b"un\ndeux\n")

    def test_a_missing_editorconfig_is_a_usage_error(self) -> None:
        """Pas de configuration, pas de règle : dire 0 ferait croire à un contrôle."""
        self.workspace.write("module.py", b"un\r\ndeux\r\n")
        self.assertEqual(self.workspace.cli()[0], 2)

    def test_an_unsupported_rule_is_a_usage_error(self) -> None:
        self.workspace.config("[*]\nend_of_line = cr\n")
        self.assertEqual(self.workspace.cli()[0], 2)

    def test_the_default_root_is_the_repository(self) -> None:
        self.assertEqual(REPO_ROOT, pathlib.Path(checker.REPO_ROOT).resolve())


class GitAttributesTest(unittest.TestCase):
    """Le geste qui supprime la **cause**, pas seulement le symptôme.

    `check_line_endings.py` répare et surveille ; il ne peut rien contre un
    checkout qui réécrit tout en CRLF. C'est `.gitattributes` qui neutralise
    `core.autocrlf` (le défaut de Git for Windows) : sa disparition ramènerait la
    dérive au clone suivant, sans qu'aucun diff ne l'ait montrée.
    """

    PATH = REPO_ROOT / ".gitattributes"

    def _rules(self) -> list:
        """Les couples (motif, attributs) du fichier, commentaires et vides écartés."""
        pairs = []
        for raw in self.PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            pattern, _, attributes = line.partition(" ")
            pairs.append((pattern, attributes.replace(" ", "")))
        return pairs

    def test_every_text_file_is_normalized_to_lf_on_checkout(self) -> None:
        rule = dict(self._rules()).get("*", "")

        self.assertIn("text=auto", rule)
        self.assertIn("eol=lf", rule)

    def test_crlf_is_granted_only_to_the_windows_commands(self) -> None:
        """Une exception de plus passerait ici sans être déclarée nulle part ailleurs."""
        crlf = [pattern for pattern, attributes in self._rules() if "eol=crlf" in attributes]

        self.assertEqual(sorted(crlf), ["*.bat", "*.cmd"])

    def test_the_exception_is_the_one_the_editorconfig_declares(self) -> None:
        """Les deux fichiers doivent dire la même chose : sinon le gate échoue au checkout."""
        sections = checker.declared_sections(EDITORCONFIG)

        self.assertEqual(checker.rules_for_file("gradlew.bat", sections).end_of_line, "crlf")
        self.assertEqual(checker.rules_for_file("script.cmd", sections).end_of_line, "crlf")


if __name__ == "__main__":
    unittest.main()
