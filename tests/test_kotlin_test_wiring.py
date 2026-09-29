"""Vérifications « les tests Compose peuvent réellement tourner en CI ».

Cette machine n'a ni JDK, ni SDK Android, ni Gradle installé : rien de ce qui
suit ne se manifesterait avant la CI, et souvent sous la forme d'un message
tardif (« Failed to find Build Tools revision… », « Unable to find
androidTest… »). Les éléments verrouillés ici sont exactement ceux dont l'absence
rend la suite Kotlin inexécutable :

1. `isIncludeAndroidResources = true` — sans lui, Robolectric ne voit aucune
   ressource et chaque test Compose échoue ;
2. les dépendances de test (Robolectric, `androidx.test`, `ui-test-junit4`) et
   `ui-test-manifest` en `debugImplementation` — sans ce dernier,
   `createComposeRule()` ne peut pas démarrer d'activité ;
3. la version de Robolectric doit couvrir l'API utilisée par `@Config(sdk = …)` ;
4. les tests d'écran doivent être des tests **JVM** (`src/test`), pas des tests
   instrumentés (`src/androidTest`) : le job n'a aucun émulateur ;
5. le job CI doit installer la plateforme correspondant au `compileSdk` déclaré,
   la version de build-tools réclamée par l'AGP, et un JDK conforme à la cible de
   compilation du module ;
6. la signature de debug ne doit pas dépendre d'un fichier non versionné
   (`debug.keystore` est exclu par `.gitignore`, donc absent de tout clone) ;
7. le job CI **construit** réellement l'APK (`:app:assembleDebug`) en plus de
   lancer les tests : `testDebugUnitTest` compile le module mais n'assemble
   aucun livrable, donc sans cette tâche le job ne « build » pas l'application.

Le fichier de workflow est lu sans parseur YAML, comme les autres tests de
configuration du dépôt (voir `tests/test_kotlin_lint_config.py`) : ajouter une
dépendance pour lire trois noms de jobs serait disproportionné.
"""

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_BUILD = REPO_ROOT / "app" / "build.gradle.kts"
CATALOG = REPO_ROOT / "gradle" / "libs.versions.toml"
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
GITIGNORE = REPO_ROOT / ".gitignore"
TEST_DIR = REPO_ROOT / "app" / "src" / "test" / "java"
INSTRUMENTED_DIR = REPO_ROOT / "app" / "src" / "androidTest"

#: Version de Robolectric → API Android la plus élevée qu'elle sait émuler.
#: Au-delà, Robolectric refuse de démarrer (« Robolectric does not support SDK … ») :
#: cela ne se voit qu'à l'exécution des tests. Toute nouvelle version doit être
#: ajoutée ici sciemment.
ROBOLECTRIC_MAX_SDK = {
    "4.11": 34,
    "4.12": 34,
    "4.13": 35,
    "4.14": 35,
    "4.15": 36,
    "4.16": 36,
}


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def catalog_versions() -> dict:
    """Versions de la section `[versions]` (fin de section incluse)."""
    found = {}
    for line in read(CATALOG).splitlines():
        if line.startswith("["):
            if found:
                break
            continue
        match = re.match(r'^([A-Za-z0-9_]+)\s*=\s*"([^"]+)"', line)
        if match:
            found[match.group(1)] = match.group(2)
    return found


def ci_jobs() -> dict:
    """Découpe `ci.yml` en {nom de job: corps du job}, sans parseur YAML.

    Les gardes-fous de `CiJobTest` vérifient que cette lecture ne renvoie pas une
    carte vide : sans cela, tous les tests qui s'appuient dessus passeraient au
    vert sans rien contrôler.
    """
    lines = read(CI_PATH).splitlines()
    start = next((index for index, line in enumerate(lines) if line.rstrip() == "jobs:"), None)
    if start is None:
        raise AssertionError("section `jobs:` introuvable dans le workflow")
    jobs: dict = {}
    current = None
    for line in lines[start + 1:]:
        if re.match(r"^  \S", line) and line.rstrip().endswith(":"):
            current = line.strip().rstrip(":")
            jobs[current] = []
        elif current is not None and (line.startswith("    ") or not line.strip()):
            jobs[current].append(line)
        elif line.strip() and not line.startswith(" "):
            break
    return {name: "\n".join(body) for name, body in jobs.items()}


def without_comments(text: str) -> str:
    """Retire les commentaires : une explication ne doit pas valider un test.

    Écrire « ce job lance `:app:testDebugUnitTest` » en commentaire satisfaisait
    la recherche de chaîne — le test passait alors que rien n'était câblé.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


class UnitTestConfigurationTest(unittest.TestCase):
    def test_android_resources_are_enabled_for_unit_tests(self) -> None:
        self.assertIn(
            "isIncludeAndroidResources = true",
            read(APP_BUILD),
            "sans cette option, Robolectric ne charge aucune ressource et les tests Compose échouent",
        )

    def test_test_dependencies_are_declared(self) -> None:
        text = read(APP_BUILD)
        expected = {
            "testImplementation(libs.junit)": "JUnit 4 : runner des tests Robolectric",
            "testImplementation(libs.robolectric)": "Robolectric : Android sur la JVM",
            "testImplementation(libs.androidx.test.core)": "ApplicationProvider",
            "testImplementation(libs.androidx.test.ext.junit)": "runner androidx.test",
            "testImplementation(libs.androidx.compose.ui.test.junit4)": "createComposeRule",
            "debugImplementation(libs.androidx.compose.ui.test.manifest)": (
                "manifeste de test : sans lui, createComposeRule() ne démarre aucune activité"
            ),
        }
        for declaration, reason in expected.items():
            with self.subTest(declaration=declaration):
                self.assertIn(declaration, text, reason)


class RobolectricVersionTest(unittest.TestCase):
    def test_pinned_version_is_known(self) -> None:
        version = catalog_versions().get("robolectric", "")
        self.assertIn(
            version,
            ROBOLECTRIC_MAX_SDK,
            f"Robolectric {version} n'est pas dans la table ROBOLECTRIC_MAX_SDK : "
            "vérifie l'API maximale qu'elle supporte avant de l'ajouter",
        )

    def test_screen_tests_use_a_supported_sdk(self) -> None:
        version = catalog_versions()["robolectric"]
        maximum = ROBOLECTRIC_MAX_SDK[version]
        configs = 0
        for path in sorted(TEST_DIR.rglob("*.kt")):
            for match in re.finditer(r"@Config\([^)]*sdk\s*=\s*\[([^\]]*)\]", read(path)):
                for value in re.findall(r"\d+", match.group(1)):
                    configs += 1
                    self.assertLessEqual(
                        int(value),
                        maximum,
                        f"{path.name} : @Config(sdk = [{value}]) dépasse l'API {maximum} "
                        f"supportée par Robolectric {version}",
                    )
        # Garde-fou : si l'expression régulière ne trouvait plus rien, le test
        # passerait sans avoir vérifié une seule configuration.
        self.assertGreater(configs, 3, "aucun `@Config(sdk = …)` lu dans les tests")


class ScreenTestPlacementTest(unittest.TestCase):
    """Sans émulateur en CI, les tests Compose doivent être des tests JVM."""

    def test_compose_screen_tests_exist_and_are_jvm_tests(self) -> None:
        jvm_tests = [
            path
            for path in sorted(TEST_DIR.rglob("*.kt"))
            if "createComposeRule()" in read(path)
        ]
        self.assertGreaterEqual(len(jvm_tests), 5, "aucun test Compose JVM trouvé")
        for path in jvm_tests:
            with self.subTest(path=path.name):
                self.assertIn(
                    "RobolectricTestRunner",
                    read(path),
                    "un test Compose sans runner Robolectric réclame un appareil",
                )

    def test_no_instrumented_compose_test_requires_a_device(self) -> None:
        if not INSTRUMENTED_DIR.exists():
            return
        offenders = [
            path.name
            for path in sorted(INSTRUMENTED_DIR.rglob("*.kt"))
            if "createAndroidComposeRule" in read(path)
        ]
        self.assertEqual(
            offenders,
            [],
            f"tests instrumentés incompatibles avec un job sans émulateur : {offenders}",
        )


class CiJobTest(unittest.TestCase):
    def setUp(self) -> None:
        self.jobs = ci_jobs()

    def test_jobs_are_read(self) -> None:
        self.assertGreaterEqual(len(self.jobs), 3, f"jobs lus : {sorted(self.jobs)}")

    def unit_test_job(self) -> str:
        matching = [
            body
            for body in self.jobs.values()
            if ":app:testDebugUnitTest" in without_comments(body)
        ]
        self.assertEqual(
            len(matching), 1, "exactement un job doit lancer les tests unitaires Kotlin"
        )
        return matching[0]

    def test_a_job_runs_the_unit_tests(self) -> None:
        self.assertIn(":app:testDebugUnitTest", without_comments(self.unit_test_job()))

    def test_the_job_installs_the_sdk_packages_the_build_requires(self) -> None:
        job = without_comments(self.unit_test_job())
        compile_sdk = re.search(r"compileSdk\s*=\s*(\d+)", read(APP_BUILD))
        self.assertIsNotNone(compile_sdk, "compileSdk introuvable dans app/build.gradle.kts")
        self.assertIn(
            f"platforms;android-{compile_sdk.group(1)}",
            job,
            "la plateforme installée doit être celle du compileSdk déclaré",
        )
        self.assertRegex(
            job,
            r"build-tools;\d+\.\d+\.\d+",
            "l'AGP réclame une version précise de build-tools (34.0.0 pour l'AGP 8.5.2)",
        )
        self.assertIn("android-actions/setup-android", job)

    def test_the_job_uses_a_jdk_matching_the_compile_target(self) -> None:
        job = without_comments(self.unit_test_job())
        target = re.search(r"JavaVersion\.VERSION_(\d+)", read(APP_BUILD))
        self.assertIsNotNone(target, "cible Java introuvable dans app/build.gradle.kts")
        self.assertRegex(
            job,
            rf'java-version:\s*"?{target.group(1)}"?',
            "le JDK de la CI doit correspondre à la cible de compilation du module",
        )

    def test_the_job_uses_the_wrapper_and_keeps_the_reports(self) -> None:
        job = without_comments(self.unit_test_job())
        self.assertIn("gradle/actions/setup-gradle", job)
        self.assertIn("actions/upload-artifact", job)
        # Le wrapper est le seul moyen d'obtenir la version de Gradle épinglée :
        # aucun `gradle-version` ne doit être demandé à l'action.
        self.assertNotIn("gradle-version:", job)

    def test_the_job_also_builds_the_debug_apk(self) -> None:
        """`testDebugUnitTest` compile mais n'assemble rien : sans
        `assembleDebug`, le job ne produit aucun APK — il ne « construit » donc
        pas l'application au sens d'un livrable installable."""
        self.assertIn(":app:assembleDebug", without_comments(self.unit_test_job()))

    def test_the_debug_apk_is_published_as_an_artifact(self) -> None:
        """Un APK construit mais jeté à la fin du job ne sert à personne."""
        job = without_comments(self.unit_test_job())
        self.assertIn("app/build/outputs/apk/debug/*.apk", job)
        self.assertIn("apk-debug", job)


class SecretsAvailableInCiTest(unittest.TestCase):
    """La CI compile `:app` sans `.env` : le plugin secrets retombe alors sur
    `.env.example`. Un `BuildConfig.X` absent des deux fait échouer la
    compilation du module — et donc tous les tests Compose avec elle."""

    def test_every_build_config_key_used_by_kotlin_is_generatable(self):
        used = set(
            re.findall(
                r"BuildConfig\.([A-Z_][A-Z0-9_]*)",
                "\n".join(read(path) for path in (REPO_ROOT / "app" / "src" / "main").rglob("*.kt")),
            )
        )
        # Garde-fou : si aucun `BuildConfig.*` n'était trouvé, le test passerait
        # sans rien vérifier (et il ne servirait alors à rien).
        self.assertGreater(len(used), 0, "aucun `BuildConfig.*` lu dans les sources Kotlin")
        declared = set(
            re.findall(r"^([A-Z_][A-Z0-9_]*)=", read(REPO_ROOT / ".env.example"), flags=re.M)
        )
        self.assertEqual(
            sorted(used - declared),
            [],
            "des clés sont absentes de .env.example : la CI compile sans `.env`, "
            "donc `BuildConfig` ne les contiendrait pas",
        )

    def test_the_secrets_plugin_declares_the_fallback_file(self):
        text = read(APP_BUILD)
        self.assertIn('propertiesFileName = ".env"', text)
        self.assertIn(
            'defaultPropertiesFileName = ".env.example"',
            text,
            "sans ce repli, un clone sans `.env` (la CI) ne génère aucun secret",
        )


class DebugSigningTest(unittest.TestCase):
    """La CI part d'un clone : aucun fichier ignoré n'y existe."""

    def test_keystore_and_local_properties_are_not_versioned(self) -> None:
        ignored = read(GITIGNORE)
        for pattern in ("*.keystore", "local.properties"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, ignored)

    def test_debug_signing_is_conditional(self) -> None:
        """`debug.keystore` étant ignoré par git, le référencer sans condition
        casse toute construction sur un clone neuf — donc la CI."""
        text = read(APP_BUILD)
        self.assertIn("debug.keystore", text)
        self.assertRegex(
            text,
            r"debugKeystore\.exists\(\)",
            "le keystore de debug doit être testé avant d'être utilisé",
        )
        self.assertRegex(
            text,
            r'signingConfigs\.findByName\("debugConfig"\)',
            "la surcharge de signature ne doit être appliquée que si elle existe",
        )


if __name__ == "__main__":
    unittest.main()
