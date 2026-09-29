"""Contrat d'identité de l'application Android : elle appartient à ce projet.

L'application a été exportée d'un atelier externe, et son identité le disait :
`com.aistudio.aitrade.kyzomw` (`com.aistudio.*`, nom de projet `kyzomw`) et un
`metadata.json` déclarant `MAJOR_CAPABILITY_SERVER_SIDE_GEMINI_API`. Ces deux
traces sont parties ; ces tests existent pour qu'elles ne reviennent pas —
typiquement au prochain export du gabarit, qui les réécrirait à l'identique.

Trois propriétés, chacune avec sa conséquence :

* l'`applicationId` est **valide** et **propre** (aucun `aistudio`, `example`,
  `google`) : c'est l'identité que le système et les magasins connaissent, et
  elle ne se change plus après publication ;
* `metadata.json` ne déclare **aucune** capacité d'atelier, et son `name` est
  celui de l'application installée (`@string/app_name`) : l'étiquette sur
  l'appareil et l'identité exportée disent la même chose ;
* l'`applicationId` **n'est pas** le paquet des sources (`namespace`) : les
  confondre est tentant, et c'est justement ce que l'export du gabarit faisait.
  Renommer le paquet relève d'un autre changement, sans effet sur une
  installation existante.
"""

import json
import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD = REPO_ROOT / "app" / "build.gradle.kts"
METADATA = REPO_ROOT / "metadata.json"
STRINGS = REPO_ROOT / "app" / "src" / "main" / "res" / "values" / "strings.xml"

#: Ce qu'un identifiant d'application Android peut porter : au moins deux
#: segments, chacun commençant par une lettre, lettres/chiffres/`_` ensuite.
APPLICATION_ID = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$")

#: Ce dont l'identité ne doit plus porter la trace : l'atelier qui l'a générée,
#: et les espaces de noms réservés (`example`, `google`) qu'un projet ne possède
#: pas — les publier, c'est publier sous une identité qu'on ne peut pas défendre.
FORBIDDEN = ("aistudio", "ai_studio", "studio", "example", "google")


def _gradle_value(name: str) -> str:
    """La valeur d'une affectation `name = "…"` du module Android."""
    text = BUILD.read_text(encoding="utf-8")
    found = re.search(rf'^\s*{name}\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert found is not None, f"{name} introuvable dans {BUILD}"
    return found.group(1)


class ApplicationIdTest(unittest.TestCase):
    """L'identité que le système connaît : elle décide des mises à jour."""

    def test_it_is_a_valid_android_application_id(self) -> None:
        self.assertRegex(_gradle_value("applicationId"), APPLICATION_ID)

    def test_it_carries_no_trace_of_the_generating_workshop(self) -> None:
        identifier = _gradle_value("applicationId")
        for word in FORBIDDEN:
            self.assertNotIn(word, identifier.lower(), f"« {word} » reste dans {identifier}")

    def test_it_is_not_the_source_package(self) -> None:
        """`namespace` est le paquet des sources Kotlin ; l'identité, autre chose."""
        self.assertNotEqual(_gradle_value("applicationId"), _gradle_value("namespace"))

    def test_the_source_package_is_still_the_one_the_manifest_names(self) -> None:
        """Renommer l'identité ne touche pas aux sources — et ce test le vérifie.

        Le manifeste nomme son activité par son paquet : si le paquet des sources
        change un jour, cette ligne et les fichiers doivent changer ensemble, et
        c'est ici que ça se voit avant la compilation.
        """
        namespace = _gradle_value("namespace")
        manifest = (REPO_ROOT / "app" / "src" / "main" / "AndroidManifest.xml").read_text(
            encoding="utf-8"
        )
        # `android:name` apparaît d'abord sur `<uses-permission>` : c'est
        # l'**activité** qui doit porter le paquet des sources.
        activity = re.search(r'<activity\b.*?android:name="([^"]+)"', manifest, re.S)
        assert activity is not None, "aucune activité nommée dans le manifeste"
        self.assertTrue(activity.group(1).startswith(namespace + "."))
        path = REPO_ROOT / "app" / "src" / "main" / "java" / pathlib.Path(
            *activity.group(1).split(".")
        ).with_suffix(".kt")
        self.assertTrue(path.is_file(), f"{path} attendu par le manifeste")


class MetadataTest(unittest.TestCase):
    """`metadata.json` est la fiche de l'application pour l'atelier d'origine."""

    def setUp(self) -> None:
        self.metadata = json.loads(METADATA.read_text(encoding="utf-8"))

    def test_it_declares_no_workshop_capability(self) -> None:
        """Une capacité `MAJOR_CAPABILITY_*` est la signature de l'atelier.

        L'application appelle l'API qu'elle utilise **elle-même**, avec sa propre
        clé : rien à déclarer ici, donc rien qui puisse réclamer un service côté
        atelier.
        """
        capabilities = self.metadata.get("majorCapabilities", [])
        self.assertEqual(capabilities, [], "aucune capacité d'atelier ne doit être déclarée")

    def test_no_key_of_the_metadata_names_a_workshop_capability(self) -> None:
        text = METADATA.read_text(encoding="utf-8")
        self.assertNotIn("MAJOR_CAPABILITY", text)
        self.assertNotIn("aistudio", text.lower())

    def test_the_name_is_the_installed_application_name(self) -> None:
        """L'étiquette sur l'appareil et l'identité exportée disent la même chose."""
        label = re.search(r'<string name="app_name">([^<]+)</string>', STRINGS.read_text(encoding="utf-8"))
        assert label is not None, "app_name introuvable dans values/strings.xml"
        self.assertEqual(self.metadata["name"], label.group(1))

    def test_it_still_describes_the_application(self) -> None:
        """Retirer la trace de l'atelier ne doit pas vider la fiche."""
        self.assertTrue(self.metadata["description"].strip())
        self.assertIsInstance(self.metadata.get("requestFramePermissions"), list)


class NoTraceInTheRepositoryTest(unittest.TestCase):
    """La trace ne doit pas non plus survivre ailleurs dans les sources.

    Ce fichier **se cite lui-même** : c'est lui qui nomme la trace à interdire,
    et il porte donc la chaîne qu'il refuse. Il s'exclut de sa propre lecture —
    le taire serait le prix d'un faux positif permanent.
    """

    SKIP = {".git", ".venv", ".pgtest", ".gradle", "build", ".freebuff", "__pycache__"}
    SELF = pathlib.Path(__file__).resolve()

    def tracked_texts(self):
        for path in sorted(REPO_ROOT.rglob("*")):
            if not path.is_file() or set(path.parts) & self.SKIP:
                continue
            if path.resolve() == self.SELF:
                continue
            if path.suffix in {".png", ".jar", ".webp", ".apk", ".zip"}:
                continue
            try:
                yield path, path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue

    def test_no_source_file_mentions_the_generating_workshop(self) -> None:
        leftovers = [
            f"{path.relative_to(REPO_ROOT)}:{line}"
            for path, text in self.tracked_texts()
            for line, content in enumerate(text.splitlines(), 1)
            if "aistudio" in content.lower() or "MAJOR_CAPABILITY" in content
        ]
        self.assertEqual(leftovers, [], "trace de l'atelier d'origine encore présente")

    def test_the_previous_identifier_is_gone(self) -> None:
        leftovers = [
            str(path.relative_to(REPO_ROOT))
            for path, text in self.tracked_texts()
            if "com.aistudio" in text
        ]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
