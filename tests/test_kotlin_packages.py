"""Le paquet des sources Kotlin porte le nom du projet — et le répertoire le dit.

Les sources venaient d'un gabarit externe : elles vivaient dans `com.example`,
c'est-à-dire dans un espace de noms que personne ne possède — celui que les
outils réservent aux exemples — et qui ne disait rien du projet. Elles vivent
désormais dans `com.aitrade`, celui du projet. Ce fichier tient ce contrat.

Rien de ce qui suit n'est vérifié par le compilateur avant la CI : Kotlin, à la
différence de Java, **n'exige pas** qu'une déclaration de paquet corresponde à
l'emplacement du fichier. Un fichier déplacé sans que sa déclaration suive (ou
l'inverse) compile donc parfaitement, et les seuls dégâts sont ailleurs : le
manifeste nomme son activité par son paquet, les scripts d'analyse prennent le
dossier du paquet en argument, et un renommage à moitié fait laisse deux espaces
de noms cohabiter. Ce sont exactement les points contrôlés ici :

1. **déclaration ↔ répertoire** : pour chaque `.kt`, le paquet déclaré est le
   chemin sous `java/`, segment pour segment ;
2. **déclaration ↔ projet** : chaque paquet est sous le `namespace` déclaré par
   la construction, lui-même nommé d'après le projet ;
3. **déclaration ↔ manifeste** : chaque composant nommé par le manifeste est un
   fichier réel du paquet ;
4. **aucun reste** : l'ancien espace de noms n'apparaît plus nulle part —
   sources, scripts, documentation ou configuration — sous **aucune** de ses
   trois écritures : la déclaration pointée, le chemin d'import, et la chaîne de
   segments assemblée dans un test ou un script (`"com" / … / "ui"`). La
   troisième est celle qui piège : elle ne contient les deux autres, et c'est
   par elle que le renommage a d'abord échoué ailleurs dans ce dépôt ;
5. **imports résolus** : chaque `import com.aitrade.…` désigne une déclaration
   qui existe vraiment. C'est la part du compilateur qu'on peut reprendre ici, et
   c'est précisément ce qu'un renommage mécanique casse sans le dire : toutes les
   déclarations de paquet peuvent être justes, et une référence ne plus viser
   personne.

Les fichiers sont lus sans parseur Kotlin ni JDK : la forme contrôlée
(`^package …` en tête de fichier) est exactement celle qu'impose la mise en
forme du projet, donc une lecture ligne à ligne suffit. Le manifeste, lui, est
du XML réel : il est analysé avec `xml.etree`, pas avec une expression
régulière.
"""

import importlib.util
import json
import pathlib
import re
import unittest
import xml.etree.ElementTree as ElementTree

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_BUILD = REPO_ROOT / "app" / "build.gradle.kts"
MANIFEST = REPO_ROOT / "app" / "src" / "main" / "AndroidManifest.xml"
METADATA = REPO_ROOT / "metadata.json"
UI_CHECK = REPO_ROOT / "scripts" / "kotlin_ui_check.py"

#: Le vérificateur des écrans sait déjà lire une déclaration de premier niveau et
#: un import nommé (`DECLARATION`, `IMPORT`) : réutiliser sa grammaire plutôt que
#: d'en écrire une seconde, qui divergerait. Il est chargé comme dans
#: `tests/test_kotlin_ui_check.py`, sans l'installer dans `sys.path`.
_SPEC = importlib.util.spec_from_file_location("kotlin_ui_check", UI_CHECK)
assert _SPEC and _SPEC.loader, UI_CHECK
CHECKER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(CHECKER)

#: Les racines de sources de la construction (les valeurs par défaut d'AGP :
#: `app/build.gradle.kts` ne les redéfinit pas, et un test le vérifie plus bas).
JAVA_ROOTS = (
    REPO_ROOT / "app" / "src" / "main" / "java",
    REPO_ROOT / "app" / "src" / "test" / "java",
)

#: La déclaration de paquet, en tête de ligne — sans indentation : c'est la seule
#: forme acceptée par la mise en forme du projet (ktlint la refuserait sinon).
PACKAGE_DECLARATION = re.compile(r"^package\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*$", re.MULTILINE)

#: Un segment de paquet doit être un identifiant : lettres, chiffres, `_`, et
#: commencer par une lettre ou `_`. `com/example-app/` ne serait pas un paquet.
SEGMENT = re.compile(r"^[A-Za-z_]\w*$")

#: Espaces de noms que le projet ne possède pas : `example` et `google` sont
#: réservés par les outils, et `studio` est le mot que porte le nom de l'atelier
#: d'origine, avec ou sans séparateur. Les publier, c'est publier sous une
#: identité qu'on ne peut pas défendre.
#:
#: Le mot est choisi pour tenir dans **un** terme, sans l'écrire en clair :
#: `tests/test_android_identity.py` refuse que le nom de l'atelier apparaisse
#: ailleurs que dans ses propres assertions, et ce fichier doit y satisfaire
#: comme n'importe quel autre.
FORBIDDEN = ("example", "studio", "google")

#: L'ancien espace de noms, dans ses deux écritures textuelles : celle des
#: sources (pointée) et celle des chemins d'import.
STRAY_POINTED = "com.example"  # ce que les sources déclaraient
STRAY_PATH = "com/example"  # sa forme en chemin d'import

#: La troisième écriture : un chemin assemblé à partir de segments littéraux,
#: joints par `/` (`"com" / "aitrade"`) ou passés en arguments (`"com",
#: "aitrade"`). Elle ne contient ni l'une ni l'autre des deux précédentes, donc
#: aucun de leurs contrôles ne la voit — c'est pourtant elle que deux tests du
#: dépôt portaient encore après le renommage.
#:
#: Le séparateur virgule n'introduit pas de faux positif : un simple groupe de
#: chaînes ne fait qu'**élargir** la chaîne lue, et le contrôle ne flagge que
#: celle qui contient le premier segment du `namespace` suivi d'autre chose.
CHAIN = re.compile(r"""["']([A-Za-z_]\w*)["'](?:\s*[/,]\s*["'][A-Za-z_]\w*["'])+""")
CHAIN_PARTS = re.compile(r"""["']([A-Za-z_]\w*)["']""")

#: Ce fichier **s'exclut** du contrôle de reste : il décrit la trace à interdire,
#: et porte donc les deux chaînes qu'il refuse. L'exclusion est sans risque — il
#: est en Python, il ne peut pas porter une déclaration de paquet Kotlin, et il
#: ne cache aucun fichier que le contrôle ne lirait pas par ailleurs. Les écrire
#: en clair le rend au contraire trouvable par qui cherche la trace à retirer.
SELF = pathlib.Path(__file__).resolve()

SKIP_DIRS = {".git", ".venv", ".pgtest", ".gradle", "build", ".freebuff", "__pycache__",
             ".ruff_cache", ".idea", "artifacts"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".jar", ".apk", ".zip",
                 ".ttf", ".otf", ".woff", ".woff2", ".keystore", ".jks", ".so", ".dll"}


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def gradle_value(name: str) -> str:
    """La valeur d'une affectation `name = "…"` du module Android."""
    found = re.search(rf'^\s*{name}\s*=\s*"([^"]+)"', read(APP_BUILD), re.MULTILINE)
    assert found is not None, f"{name} introuvable dans {APP_BUILD}"
    return found.group(1)


def declared_packages(path: pathlib.Path) -> list:
    """Les déclarations de paquet d'un fichier Kotlin, en clair."""
    return PACKAGE_DECLARATION.findall(read(path))


def kotlin_sources() -> list:
    """Chaque `.kt` des racines de sources, avec son répertoire d'appartenance."""
    found = []
    for root in JAVA_ROOTS:
        for path in sorted(root.rglob("*.kt")):
            found.append((path, root))
    return found


def repository_texts():
    """Les fichiers texte du dépôt — le contrôle 4 lit la documentation aussi."""
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or set(path.parts) & SKIP_DIRS:
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            yield path, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


class SourceLayoutTest(unittest.TestCase):
    """Les fichiers sous contrôle sont bien ceux que la construction compile."""

    def test_the_sources_are_found_where_they_are_expected(self) -> None:
        """Garde-fou : sans sources, tous les contrôles ci-dessous seraient vides."""
        for root in JAVA_ROOTS:
            with self.subTest(root=str(root)):
                self.assertTrue(root.is_dir(), f"{root} : racine de sources absente")
                self.assertGreater(
                    len(list(root.rglob("*.kt"))), 0, f"{root} ne contient aucun `.kt`"
                )
        self.assertGreater(
            len(kotlin_sources()), 50, "trop peu de sources Kotlin trouvées pour un contrôle utile"
        )

    def test_the_build_does_not_relocate_the_source_sets(self) -> None:
        """Le contrat porte sur `src/{main,test}/java` : il faut qu'ils soient compilés.

        Redéfinir les `sourceSets` déplacerait les sources ailleurs, et ce fichier
        vérifierait alors des fichiers que la construction ignore — ou manquerait
        ceux qu'elle compile. Le cas n'existe pas aujourd'hui ; s'il apparaît, ce
        test doit être étendu pour le lire plutôt que contourné.
        """
        self.assertNotIn(
            "sourceSets",
            read(APP_BUILD),
            "les racines de sources sont redéfinies : étendre ce test aux nouveaux dossiers",
        )


class PackageDeclarationTest(unittest.TestCase):
    """Chaque déclaration dit exactement où le fichier se trouve."""

    def test_every_source_declares_exactly_one_package(self) -> None:
        sources = kotlin_sources()
        self.assertGreater(len(sources), 0, "aucune source Kotlin trouvée")
        for path, _ in sources:
            with self.subTest(path=path.relative_to(REPO_ROOT).as_posix()):
                declarations = declared_packages(path)
                self.assertEqual(
                    len(declarations),
                    1,
                    f"{path.name} : {len(declarations)} déclaration(s) de paquet, "
                    "un fichier Kotlin en a exactement une",
                )

    def test_the_declaration_is_the_first_thing_in_the_file(self) -> None:
        """La forme vérifiée (`^package …`) est celle que la mise en forme impose.

        Seules les lignes de commentaire et les annotations de fichier
        (`@file:Suppress(…)`) peuvent la précéder.
        """
        for path, _ in kotlin_sources():
            with self.subTest(path=path.relative_to(REPO_ROOT).as_posix()):
                leading = []
                for line in read(path).splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("//") or stripped.startswith("*"):
                        continue
                    if stripped.startswith("/*") or stripped.startswith("*/"):
                        continue
                    if stripped.startswith("@file:"):
                        continue
                    leading.append(line)
                    break
                self.assertTrue(leading, f"{path.name} : fichier vide")
                self.assertTrue(
                    leading[0].startswith("package "),
                    f"{path.name} : la première instruction est « {leading[0].strip()} », "
                    "pas une déclaration de paquet",
                )

    def test_the_declaration_matches_the_directory(self) -> None:
        """Le contrôle central : `com/aitrade/ui/Foo.kt` déclare `com.aitrade.ui`."""
        for path, root in kotlin_sources():
            with self.subTest(path=path.relative_to(REPO_ROOT).as_posix()):
                declarations = declared_packages(path)
                self.assertEqual(len(declarations), 1, f"{path.name} : déclaration de paquet")
                expected = ".".join(path.parent.relative_to(root).parts)
                self.assertEqual(
                    declarations[0],
                    expected,
                    f"{path.relative_to(REPO_ROOT).as_posix()} déclare « {declarations[0]} » "
                    f"mais se trouve dans « {expected} »",
                )

    def test_every_directory_is_a_valid_package_path(self) -> None:
        """Un paquet n'est fait que d'identifiants : ni tiret, ni espace, ni point."""
        for path, root in kotlin_sources():
            for part in path.parent.relative_to(root).parts:
                with self.subTest(path=path.relative_to(REPO_ROOT).as_posix(), part=part):
                    self.assertRegex(part, SEGMENT, f"« {part} » n'est pas un nom de paquet")


class NamespaceTest(unittest.TestCase):
    """Le paquet des sources est celui du projet, pas celui d'un outil."""

    def setUp(self) -> None:
        self.namespace = gradle_value("namespace")

    def test_the_namespace_is_read(self) -> None:
        self.assertRegex(self.namespace, r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")

    def test_it_carries_no_name_the_project_does_not_own(self) -> None:
        for word in FORBIDDEN:
            with self.subTest(word=word):
                self.assertNotIn(
                    word,
                    self.namespace.lower(),
                    f"« {word} » : espace de noms que le projet ne possède pas",
                )

    def test_it_carries_the_project_name(self) -> None:
        """Le nom du projet, réduit à un identifiant, ouvre le paquet.

        `com.aitrade` se lit « le projet *AI Trade* » ; c'est ce qu'on vient
        chercher ici. La comparaison porte sur ce qui suit le domaine (`aitrade`)
        et non sur le paquet entier : le domaine (`com`) ne dit rien du projet, et
        un qualificatif ajouté après le nom resterait légitime.
        """
        name = json.loads(read(METADATA))["name"]
        project = re.sub(r"[^a-z0-9]", "", name.lower())
        self.assertGreater(len(project), 3, f"nom de projet illisible : {name!r}")
        own = ".".join(self.namespace.split(".")[1:])
        self.assertTrue(
            own.startswith(project),
            f"le paquet « {self.namespace} » ne porte pas le nom du projet "
            f"« {name} » (attendu : suite au domaine commençant par « {project} »)",
        )

    def test_every_declared_package_lives_under_it(self) -> None:
        for path, _ in kotlin_sources():
            with self.subTest(path=path.relative_to(REPO_ROOT).as_posix()):
                package = declared_packages(path)[0]
                self.assertTrue(
                    package == self.namespace or package.startswith(self.namespace + "."),
                    f"{package} n'est pas sous le paquet des sources ({self.namespace})",
                )

    def test_nothing_lives_beside_the_package(self) -> None:
        """Un espace de noms à moitié renommé laisse deux arbres cohabiter.

        Sous chaque racine de sources il ne doit donc y avoir **qu'un** premier
        segment, celui du paquet — et aucun `.kt` posé à la racine, hors paquet.
        """
        head = self.namespace.split(".")[0]
        for root in JAVA_ROOTS:
            with self.subTest(root=str(root.relative_to(REPO_ROOT))):
                self.assertEqual(
                    sorted(entry.name for entry in root.iterdir() if entry.is_dir()),
                    [head],
                    f"{root} : plusieurs arbres de paquets cohabitent",
                )
                self.assertEqual(
                    sorted(entry.name for entry in root.iterdir() if entry.suffix == ".kt"),
                    [],
                    f"{root} : des sources sont hors de tout paquet",
                )

    def test_the_application_id_lives_inside_it(self) -> None:
        """L'identité de l'application est distincte du paquet, et pourtant voisine.

        Elles ne doivent pas être confondues (voir `test_android_identity.py`) :
        celle-ci nomme l'application pour le système, celui-là nomme le code. Mais
        un `applicationId` hors du paquet du projet signalerait qu'un des deux a
        été renommé sans l'autre.
        """
        application_id = gradle_value("applicationId")
        self.assertTrue(
            application_id.startswith(self.namespace + "."),
            f"l'identité « {application_id} » n'est pas sous le paquet « {self.namespace} »",
        )


class ManifestAlignmentTest(unittest.TestCase):
    """Le manifeste nomme ses composants par leur paquet : ils doivent exister."""

    @staticmethod
    def named_classes() -> list:
        """Les `android:name` des composants déclarés par le manifeste."""
        android = "{http://schemas.android.com/apk/res/android}name"
        root = ElementTree.parse(MANIFEST).getroot()
        application = root.find("application")
        assert application is not None, "aucun `<application>` dans le manifeste"
        found = []
        for component in application:
            # `activity`, `service`, `receiver`, `provider` — pas `meta-data` ni
            # `uses-library`, dont le `android:name` n'est pas un nom de classe.
            if component.tag in {"activity", "service", "receiver", "provider"}:
                name = component.get(android)
                if name is not None:
                    found.append((component.tag, name))
        return found

    def test_the_manifest_declares_at_least_one_class(self) -> None:
        self.assertGreaterEqual(
            len(self.named_classes()), 1, "aucun composant nommé dans le manifeste"
        )

    def test_each_named_class_exists_in_the_source_package(self) -> None:
        namespace = gradle_value("namespace")
        main_root = JAVA_ROOTS[0]
        for tag, name in self.named_classes():
            with self.subTest(component=tag, name=name):
                self.assertTrue(
                    name.startswith(namespace + "."),
                    f"{tag} « {name} » n'est pas sous le paquet des sources ({namespace})",
                )
                # Une classe imbriquée (`…$Inner`) vit dans le fichier de sa classe
                # hôte : c'est le `$` qu'il faut retirer pour trouver le fichier.
                qualified = name.split("$")[0]
                expected = main_root.joinpath(*qualified.split(".")).with_suffix(".kt")
                self.assertTrue(
                    expected.is_file(),
                    f"le manifeste nomme « {name} », mais "
                    f"{expected.relative_to(REPO_ROOT).as_posix()} n'existe pas",
                )

    def test_the_label_stays_a_resource(self) -> None:
        """Le nom affiché n'est pas le paquet : il reste une ressource traduisible."""
        application = ElementTree.parse(MANIFEST).getroot().find("application")
        self.assertEqual(application.get("{http://schemas.android.com/apk/res/android}label"),
                         "@string/app_name")


class InternalImportTest(unittest.TestCase):
    """Le renommage a réécrit 78 fichiers d'un coup : chaque import doit viser quelque chose.

    Ni le JDK ni le SDK Android ne sont présents sur toutes les machines, et le
    compilateur ne passera donc qu'en CI. En attendant, ce contrôle fait ce que la
    compilation aurait fait de plus utile ici : il résout chaque `import
    com.aitrade.…` dans les sources réelles. C'est très exactement ce qu'un
    renommage mécanique casse — un paquet réécrit un caractère trop loin, un
    fichier déplacé dont la déclaration n'a pas suivi —, et sans lui la première
    machine à le savoir serait la CI.

    Les imports « étoile » sont écartés (leurs symboles ne sont pas déterminables
    sans classpath, ici comme ailleurs) et les imports externes aussi : seuls ceux
    du paquet du projet sont résolus.
    """

    #: Ce que la construction **génère** dans le paquet du projet. Ni l'un ni
    #: l'autre n'a de fichier source : `R` naît des ressources, `BuildConfig` des
    #: champs déclarés et du module. Les chercher serait les manquer tous deux et
    #: faire échouer un contrôle juste.
    GENERATED = {"R", "BuildConfig"}

    @staticmethod
    def declarations_by_package(root: pathlib.Path) -> dict:
        """{paquet: {nom de premier niveau: fichier}} pour une racine de sources.

        Le fichier où la déclaration vit est conservé : c'est lui qu'on veut voir
        cité quand un import ne résout pas, plutôt qu'un « introuvable » muet.
        """
        found: dict = {}
        for path in sorted(root.rglob("*.kt")):
            package = declared_packages(path)[0]
            names = found.setdefault(package, {})
            for name in CHECKER.declarations(path):
                names.setdefault(name, path)
        return found

    def unresolved(self):
        """Les imports internes qui ne désignent aucune déclaration."""
        main = self.declarations_by_package(JAVA_ROOTS[0])
        test = self.declarations_by_package(JAVA_ROOTS[1])
        namespace = gradle_value("namespace")
        found = []
        for path, root in kotlin_sources():
            # Un test peut s'appuyer sur les sources principales **et** sur les
            # autres tests ; une source principale ne peut s'appuyer que sur
            # elle-même — importer un test depuis `main` ne compilerait pas.
            available = {**test, **main} if root != JAVA_ROOTS[0] else main
            for number, line in enumerate(read(path).splitlines(), start=1):
                match = CHECKER.IMPORT.match(line)
                if not match:
                    continue
                target = match.group(1)
                if not target.startswith(namespace + "."):
                    continue
                package, _, name = target.rpartition(".")
                if package == namespace and name in self.GENERATED:
                    continue
                if name not in available.get(package, {}):
                    found.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()}:{number} ne résout pas « {target} »"
                    )
        return found

    def test_every_internal_import_resolves(self) -> None:
        self.assertEqual(self.unresolved(), [])

    def test_the_control_has_something_to_resolve(self) -> None:
        """Garde-fou : sans import interne lu, le contrôle précédent serait vide."""
        namespace = gradle_value("namespace")
        internal = [
            line
            for path, _ in kotlin_sources()
            for line in read(path).splitlines()
            if (match := CHECKER.IMPORT.match(line)) and match.group(1).startswith(namespace + ".")
        ]
        self.assertGreater(
            len(internal), 20, "aucun import interne lu : la lecture des imports serait cassée"
        )

    def test_the_generated_names_are_the_only_exemptions(self) -> None:
        """L'exemption ne doit pas devenir un refuge.

        Si `R` ou `BuildConfig` étaient soudain déclarés quelque part, l'exemption
        serait de trop : ce test le dit, et le nom reste alors vérifié comme les
        autres.
        """
        namespace = gradle_value("namespace")
        main = self.declarations_by_package(JAVA_ROOTS[0])
        self.assertEqual(
            sorted(self.GENERATED & set(main.get(namespace, {}))),
            [],
            "un nom exempté est en fait déclaré dans les sources : retirer l'exemption",
        )


class StrayNamespaceTest(unittest.TestCase):
    """L'ancien espace de noms ne doit survivre nulle part.

    Ni dans les sources qu'on vient de renommer, ni dans les scripts d'analyse qui
    prennent le dossier du paquet en argument, ni dans la documentation qui
    l'explique : un renommage à moitié fait laisse exactement ce genre de reste,
    et il ne se voit qu'en le cherchant.
    """

    def test_the_old_namespace_is_gone_from_the_repository(self) -> None:
        leftovers = [
            f"{path.relative_to(REPO_ROOT).as_posix()}:{line}"
            for path, text in repository_texts()
            if path.resolve() != SELF
            for line, content in enumerate(text.splitlines(), 1)
            if STRAY_POINTED in content or STRAY_PATH in content
        ]
        self.assertEqual(leftovers, [], "l'ancien espace de noms est encore écrit")

    def test_no_path_is_assembled_outside_the_package(self) -> None:
        """Toute chaîne de segments qui passe par le domaine mène au paquet du projet.

        Un test qui écrit `« app » / « src » / … / « com » / <autre chose>` décrit un
        dossier qui n'existe pas : il ne se plaint pas au moment du renommage, il
        échoue plus tard, à l'exécution, en annonçant un paquet « disparu ». Le
        contrôle est volontairement restreint — **après** le segment du domaine, la
        suite doit être celle du `namespace` —, ce qui laisse libres les chaînes qui
        nomment autre chose (un dossier temporaire, un chemin réseau…).
        """
        segments = gradle_value("namespace").split(".")
        head, own = segments[0], segments[1:]
        offenders = []
        for path, text in repository_texts():
            for chain in CHAIN.finditer(text):
                parts = CHAIN_PARTS.findall(chain.group(0))
                for index, part in enumerate(parts):
                    if part != head:
                        continue
                    if parts[index + 1: index + 1 + len(own)] != own:
                        line = text.count("\n", 0, chain.start()) + 1
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT).as_posix()}:{line} -> {'/'.join(parts)}"
                        )
        self.assertEqual(offenders, [], "chemin de paquet assemblé hors du projet")

    def test_the_control_reads_the_repository(self) -> None:
        """Garde-fou : un parcours cassé rendrait le contrôle précédent vide."""
        texts = list(repository_texts())
        self.assertGreater(len(texts), 50, "trop peu de fichiers lus pour un contrôle utile")
        self.assertTrue(
            any(path.name == "build.gradle.kts" for path, _ in texts),
            "le parcours du dépôt n'a pas atteint les fichiers de construction",
        )

    def test_the_analysis_scripts_point_into_the_package(self) -> None:
        """Un script pointé sur un dossier disparu réussit sans rien analyser."""
        root = re.search(r'^DEFAULT_ROOT\s*=\s*"([^"]+)"', read(UI_CHECK), re.MULTILINE)
        assert root is not None, "DEFAULT_ROOT introuvable dans scripts/kotlin_ui_check.py"
        directory = REPO_ROOT / root.group(1)
        self.assertTrue(directory.is_dir(), f"{root.group(1)} : dossier par défaut absent")
        self.assertGreater(len(list(directory.rglob("*.kt"))), 0, f"{root.group(1)} est vide")
        namespace = gradle_value("namespace")
        self.assertTrue(
            root.group(1).startswith("app/src/main/java/" + namespace.replace(".", "/")),
            f"le dossier analysé ({root.group(1)}) n'est pas dans le paquet du projet",
        )


if __name__ == "__main__":
    unittest.main()
