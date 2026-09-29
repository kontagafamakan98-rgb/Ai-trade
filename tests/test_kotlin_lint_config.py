"""Cohérence de la configuration de build Kotlin (wrapper + lint).

Ces vérifications ne remplacent pas l'exécution de Gradle : elles contrôlent la
part que Gradle ne signalerait qu'à la **configuration** du build, c'est-à-dire
tard et parfois seulement en CI.

Ce qui est verrouillé ici :

1. le wrapper Gradle est complet et utilisable (jar présent et valide, scripts,
   version épinglée, checksum de distribution au format attendu) ;
2. ktlint est bien l'**unique gate bloquant** : appliqué au module `:app`,
   `ignoreFailures` à `false`, moteur épinglé, et les deux exceptions du ruleset
   (imports étoile, fonctions Compose) écrites explicitement dans
   `.editorconfig` — et detekt a bel et bien disparu ;
3. chaque *accessor* du catalogue de versions utilisé dans un `*.gradle.kts`
   existe réellement dans `gradle/libs.versions.toml` — une faute de frappe sur
   un alias ne se voit qu'à l'exécution de Gradle, jamais à la relecture ;
4. aucune version de plugin n'est dynamique (`+`, `latest.release`) : un gate de
   lint ne doit pas changer de comportement tout seul.
"""

import hashlib
import re
import tomllib
import unittest
import zipfile
from pathlib import Path

#: `tomllib` est dans la bibliothèque standard à partir de Python 3.11 (voir
#: `requires-python` dans pyproject.toml) : aucun parseur YAML/TOML externe n'est
#: ajouté au projet pour ces vérifications.

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = REPO_ROOT / "gradle" / "libs.versions.toml"
WRAPPER_DIR = REPO_ROOT / "gradle" / "wrapper"
PROPS_PATH = WRAPPER_DIR / "gradle-wrapper.properties"
JAR_PATH = WRAPPER_DIR / "gradle-wrapper.jar"
ROOT_BUILD = REPO_ROOT / "build.gradle.kts"
APP_BUILD = REPO_ROOT / "app" / "build.gradle.kts"
EDITORCONFIG = REPO_ROOT / ".editorconfig"
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

GRADLE_SCRIPTS = (ROOT_BUILD, APP_BUILD)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _wrapper_properties() -> dict:
    props = {}
    for raw in _read(PROPS_PATH).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key.strip()] = value.strip()
    return props


def _catalog() -> dict:
    with CATALOG_PATH.open("rb") as handle:
        return tomllib.load(handle)


#: Méthodes Gradle qui suivent un accessor (`libs.versions.ktlint.get()`).
#: Sans ce nettoyage, le dernier segment est pris pour une clé du catalogue.
_ACCESSOR_METHODS = frozenset({"get", "getOrNull", "getOrElse", "orNull", "map"})


def _library_accessors(script: Path):
    """`libs.<a>.<b>` → (catégorie, clé attendue dans le catalogue)."""
    found = set()
    for match in re.finditer(r"\blibs\.([A-Za-z0-9_.]+)", _read(script)):
        parts = [p for p in match.group(1).split(".") if p]
        while parts and parts[-1] in _ACCESSOR_METHODS:
            parts.pop()
        if not parts:
            continue
        if parts[0] in ("plugins", "versions"):
            if len(parts) < 2:
                continue
            found.add((parts[0], "-".join(parts[1:])))
        else:
            found.add(("libraries", "-".join(parts)))
    return found


class GradleWrapperTest(unittest.TestCase):
    def test_wrapper_files_are_present(self):
        for path in (REPO_ROOT / "gradlew", REPO_ROOT / "gradlew.bat", PROPS_PATH, JAR_PATH):
            self.assertTrue(path.is_file(), f"fichier de wrapper manquant : {path}")

    def test_wrapper_jar_is_a_valid_jar_with_the_launcher(self):
        """Un jar tronqué ne se voit qu'au premier `./gradlew`."""
        self.assertTrue(zipfile.is_zipfile(JAR_PATH), "gradle-wrapper.jar n'est pas une archive ZIP")
        with zipfile.ZipFile(JAR_PATH) as archive:
            names = archive.namelist()
        self.assertIn("org/gradle/wrapper/GradleWrapperMain.class", names)

    #: SHA-256 du jar du wrapper **publié par Gradle pour la version 8.9**.
    #: Source : `sources/src/wrapper-validation/wrapper-checksums.json` du dépôt
    #: `gradle/actions`, où cette empreinte est listée pour `8.9`, `8.9-rc-1` et
    #: `8.9-rc-2`. Verrouiller les octets a une raison précise : le jar est un
    #: binaire, et une récupération en mode *texte* (traduction des fins de ligne)
    #: le corrompt sans que rien ne se voie à la lecture — Gradle refuse alors de
    #: démarrer, et la validation de wrapper de `gradle/actions` refuse de laisser
    #: la CI continuer. En cas de montée de version de Gradle : mettre à jour
    #: l'`distributionUrl`, le `distributionSha256Sum` **et** cette empreinte, à
    #: partir de la même source.
    GRADLE_89_WRAPPER_JAR_SHA256 = (
        "498495120a03b9a6ab5d155f5de3c8f0d986a449153702fb80fc80e134484f17"
    )

    def test_wrapper_jar_is_the_official_gradle_8_9_jar(self):
        self.assertEqual(
            hashlib.sha256(JAR_PATH.read_bytes()).hexdigest(),
            self.GRADLE_89_WRAPPER_JAR_SHA256,
            "le jar du wrapper n'est pas celui publié par Gradle 8.9 "
            "(binaire altéré, ou montée de version de Gradle non propagée ici)",
        )

    def test_gradlew_is_executable_on_posix(self):
        import os

        self.assertTrue(
            os.access(REPO_ROOT / "gradlew", os.X_OK),
            "gradlew doit être exécutable (chmod +x gradlew)",
        )

    def test_wrapper_pins_a_version_and_its_checksum(self):
        props = _wrapper_properties()
        url = props.get("distributionUrl", "")
        self.assertRegex(url, r"gradle-\d+\.\d+(\.\d+)?-bin\.zip$")
        checksum = props.get("distributionSha256Sum", "")
        self.assertRegex(
            checksum,
            r"^[0-9a-f]{64}$",
            "distributionSha256Sum doit être le SHA-256 officiel (64 hex) : sans lui, "
            "une distribution substituée serait exécutée sans vérification.",
        )

    def test_gradle_version_is_compatible_with_the_declared_agp(self):
        """AGP 8.5.x exige Gradle 8.7+ et ne supporte pas Gradle 9.

        Sans cette vérification, épingler une version de Gradle incompatible ne se
        voit qu'au moment d'un build — donc potentiellement jamais sur cette machine.
        """
        props = _wrapper_properties()
        url = props["distributionUrl"]
        gradle_version = re.search(r"gradle-(\d+)\.(\d+)", url)
        self.assertIsNotNone(gradle_version, url)
        major, minor = int(gradle_version.group(1)), int(gradle_version.group(2))

        agp = _catalog()["versions"]["agp"]
        agp_major, agp_minor = (int(part) for part in agp.split(".")[:2])

        self.assertEqual(
            (agp_major, agp_minor),
            (8, 5),
            "l'AGP a changé : revérifie la compatibilité Gradle avant de garder cet épinglage",
        )
        self.assertEqual(major, 8, "Gradle 9.x exige AGP 8.11+ (ou plus récent)")
        self.assertGreaterEqual(
            minor, 7, "AGP 8.5 exige Gradle 8.7 minimum"
        )


class NoDetektRemainsTest(unittest.TestCase):
    """« Un seul gate » n'est vrai que si detekt a réellement disparu.

    Le moindre reste — plugin, bloc de configuration, tâche CI — recréerait un
    second gate, ou casserait la configuration si un fichier référencé n'existe
    plus. Ces tests rendent la suppression vérifiable plutôt que déclarative.
    """

    def test_config_directory_is_gone(self):
        self.assertFalse(
            (REPO_ROOT / "config" / "detekt").exists(),
            "config/detekt a été supprimé avec detekt : il ne doit pas revenir",
        )

    def test_no_build_script_mentions_detekt(self):
        for script in GRADLE_SCRIPTS:
            with self.subTest(script=script.name):
                self.assertNotIn("detekt", _read(script).lower())

    def test_catalog_no_longer_declares_detekt(self):
        catalog = _catalog()
        self.assertNotIn("detekt", catalog.get("plugins", {}))
        self.assertNotIn("detekt", catalog.get("versions", {}))

    def test_ci_does_not_run_detekt(self):
        self.assertNotIn("detekt", _read(CI_PATH).lower())


class KtlintConfigTest(unittest.TestCase):
    def test_editorconfig_enables_unused_import_detection(self):
        text = _read(EDITORCONFIG)
        self.assertIn("ktlint_standard_no-unused-imports = enabled", text)

    def test_editorconfig_disables_the_wildcard_import_rule(self):
        """Les imports étoile sont volontaires : la règle doit rester désactivée.

        Elle ne peut l'être que dans `.editorconfig` — `disabledRules` n'est
        plus supporté par ktlint-gradle depuis le moteur ktlint 0.48.
        """
        self.assertIn("ktlint_standard_no-wildcard-imports = disabled", _read(EDITORCONFIG))

    def test_editorconfig_exempts_composable_functions_from_function_naming(self):
        """Compose nomme ses fonctions `@Composable` en PascalCase : la règle
        `function-naming` doit les ignorer, sinon tout écran échoue le gate."""
        self.assertIn(
            "ktlint_function_naming_ignore_when_annotated_with = Composable",
            _read(EDITORCONFIG),
        )

    def test_ktlint_is_applied_to_the_android_module_only(self):
        self.assertIn("alias(libs.plugins.ktlint)", _read(APP_BUILD))
        # Le projet racine se contente de déclarer le plugin.
        self.assertIn("alias(libs.plugins.ktlint) apply false", _read(ROOT_BUILD))

    def test_ktlint_engine_version_comes_from_the_catalog(self):
        self.assertIn("version.set(libs.versions.ktlint.get())", _read(APP_BUILD))

    def test_no_op_android_flag_is_not_set(self):
        """`android.set(true)` est un no-op avec ktlint 1.x : le plugin l'injecte
        comme propriété `.editorconfig`, que le moteur ignore. Le laisser donne
        l'illusion d'un réglage actif ; on ne le remet pas."""
        self.assertNotIn("android.set(true)", _read(APP_BUILD))

    def test_the_gate_is_blocking(self):
        text = _read(APP_BUILD)
        self.assertIn("ignoreFailures.set(false)", text)
        self.assertNotIn(
            "ignoreFailures.set(true)",
            text,
            "`ignoreFailures = true` remettrait ktlint en rapport seul",
        )


class LintGateWiringTest(unittest.TestCase):
    """Le gate n'existe que s'il est réellement exécuté par la CI. Ces
    vérifications portent sur le workflow lui-même, pas sur une intention."""

    def test_ci_runs_the_ktlint_check(self):
        self.assertIn(":app:ktlintCheck", _read(CI_PATH))

    def test_ci_installs_the_android_sdk_the_lint_task_needs(self):
        """ktlint est appliqué à `:app` : configurer sa tâche exige l'AGP, donc
        le SDK Android. Sans cette installation, le job échoue avant tout lint."""
        self.assertIn("android-actions/setup-android", _read(CI_PATH))

    def test_the_fast_python_preflight_still_runs(self):
        self.assertIn("scripts/kotlin_ui_check.py", _read(CI_PATH))


class VersionCatalogConsistencyTest(unittest.TestCase):
    def test_every_accessor_used_in_build_scripts_exists(self):
        catalog = _catalog()
        tables = {
            "plugins": catalog.get("plugins", {}),
            "libraries": catalog.get("libraries", {}),
            "versions": catalog.get("versions", {}),
        }
        missing = set()
        checked = 0
        for script in GRADLE_SCRIPTS:
            for kind, key in _library_accessors(script):
                checked += 1
                if key not in tables[kind]:
                    missing.add(f"{script.relative_to(REPO_ROOT)}: libs.{kind}.{key}")
        # Garde-fou du test lui-même : si l'extraction ne trouvait plus rien, le
        # test passerait au vert sans avoir rien vérifié.
        self.assertGreater(checked, 10, "aucun accessor extrait des scripts de build")
        self.assertEqual(missing, set(), f"alias absents du catalogue : {sorted(missing)}")

    def test_plugin_version_refs_resolve(self):
        catalog = _catalog()
        versions = catalog.get("versions", {})
        refs = {
            name: entry.get("version", {}).get("ref")
            for name, entry in catalog.get("plugins", {}).items()
        }
        # Garde-fou du test lui-même : si la lecture des refs se cassait, tout
        # passerait sans rien vérifier (c'est arrivé une fois).
        self.assertTrue(all(refs.values()), f"aucune version.ref lue : {refs}")
        for name, ref in refs.items():
            with self.subTest(plugin=name):
                self.assertIn(ref, versions, f"{name} référence une version inconnue : {ref}")

    def test_declared_versions_are_not_dynamic(self):
        """Un gate de lint doit être reproductible : pas de `+` ni de plages."""
        for name, value in _catalog().get("versions", {}).items():
            with self.subTest(version=name):
                self.assertNotIn("+", value, f"{name} doit être épinglé")
                self.assertNotIn("latest", value.lower(), f"{name} doit être épinglé")

    def test_lint_versions_are_pinned_in_the_expected_place(self):
        versions = _catalog()["versions"]
        for key in ("ktlintGradlePlugin", "ktlint"):
            with self.subTest(key=key):
                self.assertRegex(versions.get(key, ""), r"^\d+\.\d+(\.\d+)?$")


if __name__ == "__main__":
    unittest.main()
