"""Garde-fous sur les ressources de chaînes Android.

Ces tests remplacent ce qu'un `aapt2`/`lint` vérifierait à la compilation, qui
n'est pas disponible partout (pas de JDK/SDK Android requis ici) :

* le jeu de clés est **identique** dans toutes les langues ;
* les apostrophes sont échappées (`\\'`) et `&` est encodé (`&amp;`) — sinon la
  compilation échoue ;
* les chaînes contenant `%` déclarent `formatted="false"` ;
* chaque `R.string.<clé>` du code Kotlin existe réellement, et inversement
  aucune clé n'est orpheline (le manifeste compte comme référence) ;
* le prix affiché et la remise annoncée disent la même chose : la fiche
  tarifaire annonçait « -60 % » écrit en dur pour un écart réel de 58,3 % ;
* aucun emoji décoratif ni tiret cadratin dans un texte affiché.
"""

from __future__ import annotations

import pathlib
import re
import unittest
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
RES_DIR = ROOT / "app" / "src" / "main" / "res"
APP_SRC = ROOT / "app" / "src"
DEFAULT_FILE = RES_DIR / "values" / "strings.xml"
LOCALES = ("fr", "es", "de", "zh", "ar", "ja", "pt", "ru", "hi")
# Référencé depuis le manifeste, pas depuis Kotlin.
MANIFEST_ONLY_KEYS = {"app_name"}

#: Emoji « décoratifs » : symboles, dingbats et pictogrammes. Les flèches sont
#: exclues (ce ne sont pas des emoji), et l'ensemble reste volontairement étroit
#: pour ne jamais confondre un caractère plein d'une écriture (arabe, hindi,
#: français) avec un pictogramme.
EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0001F1E6-\U0001F1FF\u2139\u24C2\uFE0F\u20E3]"
)
#: Tirets cadratin et demi-cadratin : une ponctuation de prose.
EM_DASH = re.compile("[\u2014\u2013]")

STRING_TAG = re.compile(r"<string\b[^>]*>(.*?)</string>", re.S)
R_STRING_REF = re.compile(r"R\.string\.(\w+)")
MANIFEST_REF = re.compile(r"@string/(\w+)")
#: Argument de format, indexé (`%1$s`, `%2$d`) ou positionnel (`%s`). Un `%%`
#: littéral ne correspond pas : ce n'est pas un argument.
FORMAT_ARG = re.compile(r"%(?:\d+\$)?[sd]")


def _locale_files() -> dict[str, pathlib.Path]:
    files = {"values": DEFAULT_FILE}
    for code in LOCALES:
        files[f"values-{code}"] = RES_DIR / f"values-{code}" / "strings.xml"
    return files


def _keys(path: pathlib.Path) -> list[str]:
    return [node.get("name") for node in ET.parse(path).getroot().findall("string")]


def _texts(path: pathlib.Path) -> dict[str, str]:
    return {node.get("name"): (node.text or "") for node in ET.parse(path).getroot().findall("string")}


def _non_translatable(path: pathlib.Path) -> set[str]:
    """Clés marquées `translatable="false"` (noms propres, codes).

    Elles n'ont **pas** à figurer dans les autres langues : c'est précisément ce
    que déclare l'attribut. Les y dupliquer recréerait un endroit de plus à
    corriger le jour où une marque change.
    """
    return {
        node.get("name")
        for node in ET.parse(path).getroot().findall("string")
        if node.get("translatable") == "false"
    }


class StringResourcesTest(unittest.TestCase):
    def test_every_translated_locale_exists(self) -> None:
        missing = [str(p) for p in _locale_files().values() if not p.exists()]
        self.assertEqual([], missing, f"traductions manquantes : {missing}")

    def test_key_sets_are_identical_across_languages(self) -> None:
        default_keys = set(_keys(DEFAULT_FILE))
        exempt = MANIFEST_ONLY_KEYS | _non_translatable(DEFAULT_FILE)
        for label, path in _locale_files().items():
            expected = default_keys - (exempt if label != "values" else set())
            actual = set(_keys(path))
            self.assertEqual(set(), expected - actual, f"{label} : clés manquantes {sorted(expected - actual)}")
            self.assertEqual(set(), actual - expected, f"{label} : clés en trop {sorted(actual - expected)}")

    def test_non_translatable_keys_are_not_duplicated_in_locales(self) -> None:
        """Un nom propre n'a rien à faire dans `values-fr` : il se traduirait."""
        untranslatable = _non_translatable(DEFAULT_FILE)
        self.assertGreater(len(untranslatable), 0, "aucune clé non traduisible : le test ne vérifierait rien")
        for label, path in _locale_files().items():
            if label == "values":
                continue
            duplicated = sorted(set(_keys(path)) & untranslatable)
            self.assertEqual([], duplicated, f"{label} : clés non traduisibles dupliquées {duplicated}")

    def test_no_duplicate_keys(self) -> None:
        for label, path in _locale_files().items():
            keys = _keys(path)
            duplicates = sorted({key for key in keys if keys.count(key) > 1})
            self.assertEqual([], duplicates, f"{label} : clés dupliquées {duplicates}")

    def test_apostrophes_are_escaped(self) -> None:
        """aapt refuse une apostrophe non échappée (sauf valeur entre guillemets)."""
        offenders: list[str] = []
        for label, path in _locale_files().items():
            raw = path.read_text(encoding="utf-8")
            for match in STRING_TAG.finditer(raw):
                value = match.group(1)
                if value.startswith('"') and value.endswith('"'):
                    continue
                for index, char in enumerate(value):
                    if char == "'" and (index == 0 or value[index - 1] != "\\"):
                        offenders.append(f"{label}: {value[:60]!r}")
                        break
        self.assertEqual([], offenders, f"apostrophes non échappées : {offenders}")

    def test_percent_strings_are_declared_unformatted(self) -> None:
        offenders: list[str] = []
        for label, path in _locale_files().items():
            for node in ET.parse(path).getroot().findall("string"):
                if "%" in (node.text or "") and node.get("formatted") != "false":
                    offenders.append(f"{label}: {node.get('name')}")
        self.assertEqual([], offenders, f"chaînes avec %% sans formatted=\"false\" : {offenders}")

    def test_format_arguments_match_the_english_value(self) -> None:
        """Une traduction doit garder exactement les arguments de l'anglais.

        C'est le défaut le plus coûteux d'une traduction : un `%2$s` oublié ou
        renommé fait lever `MissingFormatArgumentException` au moment où
        `String.format` est appelé, donc **à l'écran**, chez l'utilisateur de
        cette langue, et seulement sur le chemin de code concerné.
        """
        english = _texts(DEFAULT_FILE)
        offenders: list[str] = []
        for label, path in _locale_files().items():
            if label == "values":
                continue
            for key, value in _texts(path).items():
                if key not in english:
                    continue
                expected = set(FORMAT_ARG.findall(english[key]))
                found = set(FORMAT_ARG.findall(value))
                if expected != found:
                    offenders.append(f"{label}: {key} -> {sorted(found)} au lieu de {sorted(expected)}")
        self.assertEqual([], offenders, f"arguments de format divergents : {offenders}")

    def test_kotlin_references_exist(self) -> None:
        known = set(_keys(DEFAULT_FILE))
        unknown: list[str] = []
        for kotlin in APP_SRC.rglob("*.kt"):
            for reference in R_STRING_REF.findall(kotlin.read_text(encoding="utf-8")):
                if reference not in known:
                    unknown.append(f"{kotlin.relative_to(ROOT)}: R.string.{reference}")
        self.assertEqual([], sorted(set(unknown)), "références R.string inconnues")

    def test_no_orphan_keys(self) -> None:
        referenced = set(MANIFEST_ONLY_KEYS)
        for kotlin in APP_SRC.rglob("*.kt"):
            referenced.update(R_STRING_REF.findall(kotlin.read_text(encoding="utf-8")))
        for manifest in APP_SRC.rglob("AndroidManifest.xml"):
            referenced.update(MANIFEST_REF.findall(manifest.read_text(encoding="utf-8")))
        orphans = sorted(set(_keys(DEFAULT_FILE)) - referenced)
        self.assertEqual([], orphans, f"clés déclarées mais jamais utilisées : {orphans}")

    def test_no_missing_english_value(self) -> None:
        empty = sorted(name for name, text in _texts(DEFAULT_FILE).items() if not text.strip())
        self.assertEqual([], empty, f"valeurs anglaises vides : {empty}")


class DemoContentResourcesTest(unittest.TestCase):
    """Les données de démonstration sont des ressources, plus un `if` par langue.

    C'est cette migration qui a fait cesser le repli anglais : le choix de la
    langue ne distinguaient que le français, l'espagnol, l'allemand et le
    chinois, et l'arabe, le japonais, le portugais, le russe et l'hindi lisaient
    la branche `else` anglaise. Un test de valeur ne verrait pas la régression
    revenir (une traduction recopiée reste une traduction) ; ces trois
    contrôles portent donc sur le câblage lui-même : chaque clé de démonstration
    est lue par le dépôt, et le branchement par code de langue a disparu.
    """

    REPOSITORY = APP_SRC / "main" / "java" / "com" / "aitrade" / "data" / "TradingRepository.kt"
    PREFIXES = ("demo_insight_", "demo_signal_")
    #: Trois analyses (titre + contenu) et sept signaux (quatre champs).
    EXPECTED_KEYS = 3 * 2 + 7 * 4

    def _demo_keys(self) -> set[str]:
        return {name for name in _keys(DEFAULT_FILE) if name.startswith(self.PREFIXES)}

    def test_the_demo_content_is_actually_resourced(self) -> None:
        keys = self._demo_keys()
        self.assertEqual(
            len(keys), self.EXPECTED_KEYS, f"{len(keys)} clés de démonstration au lieu de {self.EXPECTED_KEYS}"
        )
        self.assertEqual(
            len(self._demo_keys() & _non_translatable(DEFAULT_FILE)),
            0,
            "une clé de démonstration est déclarée non traduisible",
        )

    def test_the_repository_reads_every_demo_key(self) -> None:
        text = self.REPOSITORY.read_text(encoding="utf-8")
        referenced = set(R_STRING_REF.findall(text))
        self.assertEqual(
            sorted(self._demo_keys() - referenced),
            [],
            "clé de démonstration jamais lue par le dépôt",
        )
        #: Garde-fou : sans référence de démonstration du tout, le contrôle
        #: précédent serait vrai par vacuité.
        self.assertGreaterEqual(len(referenced & self._demo_keys()), self.EXPECTED_KEYS)

    def test_the_repository_no_longer_branches_on_the_language_code(self) -> None:
        text = self.REPOSITORY.read_text(encoding="utf-8")
        for fragment in ("startsWith(", "isFr", "languageCode.lowercase()"):
            with self.subTest(fragment=fragment):
                self.assertNotIn(
                    fragment, text, f"le dépôt rebranche sur la langue ({fragment!r})"
                )
        self.assertIn("localizedTo(", text, "le dépôt ne lit plus de contexte localisé")


class PresentationRulesTest(unittest.TestCase):
    """Deux règles de présentation, tenues par un test et non par la mémoire.

    * **Aucun emoji décoratif**, ni dans un libellé ni dans les sources Kotlin.
      Un emoji ne remplace pas une icône : il ne s'aligne pas sur la ligne, ne
      se teinte pas avec le thème, et son dessin dépend de la police du système.
      La sévérité d'une ligne de journal est portée par sa **couleur** (voir
      `LiveLogFeedCard`), pas par un pictogramme.
    * **Aucun tiret cadratin** dans un texte affiché : c'est une ponctuation de
      prose, pas un libellé d'interface. Les commentaires du code ne suivent pas
      cette règle, ils ne sont jamais affichés.
    """

    def _offenders(self, pattern: re.Pattern) -> list[str]:
        found: list[str] = []
        for label, path in _locale_files().items():
            for name, value in _texts(path).items():
                if pattern.search(value):
                    found.append(f"{label}: {name} {value[:40]!r}")
        return found

    def test_no_emoji_in_user_facing_strings(self) -> None:
        self.assertEqual([], self._offenders(EMOJI), "emoji dans un libellé")

    def test_no_em_dash_in_user_facing_strings(self) -> None:
        self.assertEqual([], self._offenders(EM_DASH), "tiret cadratin dans un libellé")

    def test_no_emoji_in_the_android_sources(self) -> None:
        offenders: list[str] = []
        for kotlin in APP_SRC.rglob("*.kt"):
            for number, line in enumerate(kotlin.read_text(encoding="utf-8").splitlines(), 1):
                if EMOJI.search(line):
                    offenders.append(f"{kotlin.relative_to(ROOT)}:{number}")
        self.assertEqual([], offenders, "emoji dans le code Kotlin")


class PricingClaimTest(unittest.TestCase):
    """Le prix affiché et la remise annoncée ne peuvent pas diverger.

    La fiche tarifaire affichait « 60 % » écrit en dur alors que l'écart réel
    vaut 58,3 % : un chiffre qui ment, et qui serait devenu franchement faux au
    premier changement de prix. Le pourcentage est désormais **calculé** depuis
    `VipPlan.amountUsd`. Ce qui reste à verrouiller, c'est que les montants
    affichés dans les dix langues soient bien ceux de ce calcul — sinon la
    remise redevient une décoration.
    """

    UI_DIR = APP_SRC / "main" / "java" / "com" / "aitrade" / "ui"
    MAPPINGS = UI_DIR / "StringMappings.kt"
    PAYWALL = UI_DIR / "VipPaywallCard.kt"
    PRICE_KEYS = {
        "MONTHLY": "vip_price_monthly",
        "ANNUAL": "vip_price_annual",
        "LIFETIME": "vip_price_lifetime",
    }

    def _plan_amounts(self) -> dict[str, float]:
        found = dict(
            re.findall(
                r'\b(MONTHLY|ANNUAL|LIFETIME)\("([\d.]+)"\)',
                self.MAPPINGS.read_text(encoding="utf-8"),
            )
        )
        # Garde-fou : sans lui, une extraction cassée ferait passer le test au
        # vert sans rien vérifier.
        self.assertEqual(set(found), set(self.PRICE_KEYS), "montants VipPlan illisibles")
        return {name: float(value) for name, value in found.items()}

    @staticmethod
    def _amount(text: str) -> float:
        """« 19,99 $ » et « $19.99 » désignent le même montant."""
        return float(re.sub(r"[^\d,.]", "", text).replace(",", "."))

    def test_the_displayed_prices_match_the_plan_amounts(self) -> None:
        amounts = self._plan_amounts()
        for label, path in _locale_files().items():
            texts = _texts(path)
            for plan, key in self.PRICE_KEYS.items():
                with self.subTest(locale=label, plan=plan):
                    self.assertIn(key, texts, f"{label}: {key} manquante")
                    self.assertAlmostEqual(
                        self._amount(texts[key]),
                        amounts[plan],
                        places=2,
                        msg=f"{label}: {key} = {texts[key]!r}, mais VipPlan.{plan} = {amounts[plan]}",
                    )

    def test_the_saving_is_a_computed_template(self) -> None:
        """Aucun pourcentage en dur : la valeur porte `%1$d` et rien d'autre."""
        for label, path in _locale_files().items():
            value = _texts(path).get("vip_save_annual", "")
            with self.subTest(locale=label):
                self.assertIn("%1$d", value, f"{label}: la remise doit être calculée : {value!r}")
                without_token = re.sub(r"%\d+\$[sd]", "", value)
                self.assertNotRegex(
                    without_token, r"\d", f"{label}: chiffre écrit en dur : {value!r}"
                )

    def test_the_percentage_is_derived_from_the_plan_amounts(self) -> None:
        body = self.MAPPINGS.read_text(encoding="utf-8")
        self.assertIn("fun annualSavingPercent()", body, "le pourcentage n'est plus calculé")
        function = body.split("fun annualSavingPercent()", 1)[1]
        for source in ("VipPlan.MONTHLY.amountUsd", "VipPlan.ANNUAL.amountUsd"):
            with self.subTest(source=source):
                self.assertIn(source, function)

    def test_the_paywall_shows_the_computed_percentage(self) -> None:
        text = self.PAYWALL.read_text(encoding="utf-8")
        self.assertIn("strings.vipSaveAnnual", text)
        self.assertIn("annualSavingPercent()", text)
        self.assertNotIn("vipSave60", text, "l'ancien libellé écrit en dur subsiste")
        # La ressource doit rester projetée : le champ affiché vient de la
        # ressource, pas d'un texte composé à la main.
        projection = (self.UI_DIR / "StringsResources.kt").read_text(encoding="utf-8")
        self.assertIn("R.string.vip_save_annual", projection)


if __name__ == "__main__":
    unittest.main()
