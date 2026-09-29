"""Tests du contrôle structurel des fichiers Compose.

Le script `scripts/kotlin_ui_check.py` est le seul filet de sécurité Kotlin de ce
dépôt quand ni Gradle ni le SDK Android ne sont disponibles. Sa valeur tient donc
entièrement à ce qu'il **détecte** ; ces tests vérifient les deux moitiés de
l'affirmation :

* chaque contrôle déclenche bien l'échec (cas volontairement fautifs) — un
  vérificateur qui ne trouve jamais rien passerait au vert sans rien contrôler ;
* chaque contrôle **ne** se déclenche pas sur ce qui est légitime (import étoile,
  usage dans un template, avertissement de taille), sinon la CI deviendrait
  rouge pour de mauvaises raisons et serait désactivée.

Le dernier test s'exécute sur le vrai paquet `ui` : c'est le garde-fou du
découpage des écrans séparés en sections.
"""

import importlib.util
import io
import pathlib
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "kotlin_ui_check.py"
UI_ROOT = REPO_ROOT / "app" / "src" / "main" / "java" / "com" / "aitrade" / "ui"
#: Tout le Kotlin du module : ce que le contrôle de mise en forme couvre.
SOURCES_ROOT = REPO_ROOT / "app" / "src"

SPEC = importlib.util.spec_from_file_location("kotlin_ui_check", SCRIPT_PATH)
assert SPEC and SPEC.loader, SCRIPT_PATH
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class Workspace:
    """Paquet Kotlin jetable : un dossier de fichiers `*.kt`."""

    def __init__(self, test: unittest.TestCase) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)

    def write(self, name: str, content: str) -> pathlib.Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def run(self, max_lines: int = 300) -> int:
        return checker.check(self.root, max_lines, stream=io.StringIO())

    def report(
        self, max_lines: int = 300, sources: pathlib.Path | None = None
    ) -> tuple[int, str]:
        """(code de sortie, rapport) : le rapport porte les verdicts eux-mêmes."""
        stream = io.StringIO()
        code = checker.check(self.root, max_lines, stream=stream, sources=sources)
        return code, stream.getvalue()


def with_editorconfig(test: unittest.TestCase, content: str) -> None:
    """Substitue un `.editorconfig` jetable à celui du dépôt, le temps d'un test."""
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    path = pathlib.Path(folder.name) / ".editorconfig"
    path.write_text(content, encoding="utf-8")
    original = checker.EDITORCONFIG_PATH
    test.addCleanup(setattr, checker, "EDITORCONFIG_PATH", original)
    checker.EDITORCONFIG_PATH = path


class NeutralizerTest(unittest.TestCase):
    """Le socle : ce qui est analysé comme du code, et ce qui ne l'est pas."""

    def test_comment_text_is_not_code(self) -> None:
        self.assertNotIn("Ghost", checker.identifiers("// Ghost\nval x = 1\n"))

    def test_block_comment_text_is_not_code(self) -> None:
        self.assertNotIn("Ghost", checker.identifiers("/* Ghost */ val x = 1"))

    def test_string_literal_text_is_not_code(self) -> None:
        self.assertNotIn("Ghost", checker.identifiers('val x = "Ghost"'))

    def test_string_template_is_code(self) -> None:
        """`"${strings.title}"` référence vraiment `strings` : l'ignorer ferait
        passer un import utilisé pour inutilisé."""
        found = checker.identifiers('val x = "${strings.title}"')
        self.assertIn("strings", found)
        self.assertIn("title", found)

    def test_short_string_template_is_code(self) -> None:
        self.assertIn("prefs", checker.identifiers('val x = "$prefs"'))

    def test_escaped_quote_does_not_end_the_literal_early(self) -> None:
        """Sans cela, la fin de chaîne est mal détectée et le reste du fichier
        est analysé comme du texte (ou l'inverse)."""
        found = checker.identifiers(r'val x = "a\"b" + RealCode')
        self.assertIn("RealCode", found)


class UnusedImportTest(unittest.TestCase):
    def test_unused_named_import_is_detected(self) -> None:
        workspace = Workspace(self)
        path = workspace.write("A.kt", "package p\n\nimport p.Missing\n\nval x = 1\n")
        self.assertEqual(checker.unused_imports(path), ["import p.Missing"])

    def test_used_named_import_is_kept(self) -> None:
        workspace = Workspace(self)
        path = workspace.write("A.kt", "package p\n\nimport p.Used\n\nval x = Used()\n")
        self.assertEqual(checker.unused_imports(path), [])

    def test_wildcard_import_is_never_reported(self) -> None:
        """Sans classpath, les symboles apportés par un `*` sont indéterminables."""
        workspace = Workspace(self)
        path = workspace.write("A.kt", "package p\n\nimport androidx.compose.runtime.*\n\nval x = 1\n")
        self.assertEqual(checker.unused_imports(path), [])

    def test_symbol_used_only_in_a_template_counts_as_used(self) -> None:
        workspace = Workspace(self)
        path = workspace.write("A.kt", 'package p\n\nimport p.Locale\n\nval x = "${Locale.getDefault()}"\n')
        self.assertEqual(checker.unused_imports(path), [])

    def test_symbol_mentioned_only_in_a_comment_is_still_unused(self) -> None:
        workspace = Workspace(self)
        path = workspace.write("A.kt", "package p\n\nimport p.Missing\n\n// Missing is gone\nval x = 1\n")
        self.assertEqual(checker.unused_imports(path), ["import p.Missing"])

    def test_aliased_import_is_checked_under_its_alias(self) -> None:
        workspace = Workspace(self)
        used = workspace.write("A.kt", "package p\n\nimport p.Machine as Moteur\n\nval x = Moteur()\n")
        unused = workspace.write("B.kt", "package p\n\nimport p.Machine as Moteur\n\nval x = 1\n")
        self.assertEqual(checker.unused_imports(used), [])
        self.assertEqual(checker.unused_imports(unused), ["import p.Machine as Moteur"])


class StructuralTest(unittest.TestCase):
    def test_duplicate_top_level_declaration_fails(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\nfun Doublon() {}\n")
        workspace.write("B.kt", "package p\n\nfun Doublon() {}\n")
        self.assertNotEqual(workspace.run(), 0)

    def test_same_name_in_different_kinds_of_declaration_still_fails(self) -> None:
        """`fun X` et `enum class X` ne coexistent pas dans un même paquet."""
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\nfun Doublon() {}\n")
        workspace.write("B.kt", "package p\n\nenum class Doublon { A }\n")
        self.assertNotEqual(workspace.run(), 0)

    def test_nested_declaration_is_not_a_top_level_one(self) -> None:
        """Un composable imbriqué dans un `let` reste local : pas de conflit."""
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\nfun Outer() {}\n")
        workspace.write("B.kt", "package p\n\nfun Other() {\n    val X = 1\n}\n")
        self.assertEqual(workspace.run(), 0)

    def test_unbalanced_braces_fail(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\nfun F() {\n    if (true) {\n}\n")
        self.assertNotEqual(workspace.run(), 0)

    def test_brace_inside_a_string_does_not_count(self) -> None:
        """Une accolade ouvrante dans un littéral ne doit pas casser le compte.

        Le littéral est volontairement un identifiant (`chat_list{0}`) et non une
        phrase : une phrase anglaise en dur est justement ce que le contrôle des
        libellés refuse, et ce test porte sur le comptage d'accolades.
        """
        workspace = Workspace(self)
        workspace.write("A.kt", 'package p\n\nfun F() {\n    val x = "chat_list{0}"\n    val y = "${x} ${x}"\n}\n')
        self.assertEqual(checker.brace_delta(workspace.root / "A.kt"), 0)
        self.assertEqual(workspace.run(), 0)

    def test_brace_block_inside_a_template_does_not_break_the_count(self) -> None:
        """Un `${if (…) { … } else { … }}` étendu sur plusieurs lignes (ce que
        produit le formatage ktlint) doit rester équilibré : la première `}`
        d'un bloc interne ne referme PAS le template. Bug réel : un écran a été
        signalé « accolades déséquilibrées » après un formatage.
        """
        workspace = Workspace(self)
        content = (
            "package p\n"
            "\n"
            "fun F() {\n"
            '    val x =\n'
            '        "${strings.bias}: ${if (score >= 0.58) {\n'
            "            strings.bullish\n"
            "        } else if (score <= 0.42) {\n"
            "            strings.bearish\n"
            "        } else {\n"
            "            strings.neutral\n"
            '        }}"\n'
            "}\n"
        )
        path = workspace.write("A.kt", content)
        self.assertEqual(checker.brace_delta(path), 0)
        self.assertEqual(workspace.run(), 0)
        # Les identifiants du template restent analysés (détection d'usage).
        self.assertIn("bullish", checker.identifiers(content))

    def test_an_extension_function_is_named_by_its_own_name(self) -> None:
        """`fun Context.localizedTo(…)` s'appelle `localizedTo`, pas `Context`.

        Le récepteur n'est pas un nom de déclaration : le lire à sa place cachait
        le vrai nom à la détection de doublons, et rendait l'import d'une
        extension impossible à résoudre (`data/LocalizedContext.kt`).
        """
        workspace = Workspace(self)
        path = workspace.write(
            "A.kt", "package p\n\nfun Context.localizedTo(code: String): Context = this\n"
        )
        self.assertEqual(sorted(checker.declarations(path)), ["localizedTo"])

    def test_an_extension_property_is_named_by_its_own_name(self) -> None:
        workspace = Workspace(self)
        path = workspace.write("A.kt", "package p\n\nval String.words: Int = 1\n")
        self.assertEqual(sorted(checker.declarations(path)), ["words"])

    def test_empty_directory_fails(self) -> None:
        """Un dossier vide ferait passer le contrôle sans rien vérifier."""
        self.assertNotEqual(Workspace(self).run(), 0)


class HardcodedFontSizeTest(unittest.TestCase):
    """L'échelle typographique vit dans `theme/Theme.kt`, pas dans les écrans.

    Ce contrôle verrouille la migration : il a fallu retirer 235 `fontSize` en
    dur de 39 écrans pour que toute la typographie vienne du thème. Sans lui,
    le premier écran ajouté à la main réintroduit la fuite.
    """

    def test_hardcoded_size_is_reported(self) -> None:
        workspace = Workspace(self)
        path = workspace.write("A.kt", 'package p\n\nval x = Text("a", fontSize = 10.sp)\n')
        self.assertEqual(checker.hardcoded_font_sizes(path), ["A.kt:3 -> fontSize = 10.sp"])

    def test_decimal_size_is_reported(self) -> None:
        workspace = Workspace(self)
        path = workspace.write("A.kt", "package p\n\nval x = Text(\"a\", fontSize = 11.5.sp)\n")
        self.assertEqual(len(checker.hardcoded_font_sizes(path)), 1)

    def test_theme_package_is_exempt(self) -> None:
        """Dans `theme/`, un `fontSize` est la **source** de l'échelle."""
        theme = Workspace(self).root / "theme"
        theme.mkdir()
        path = theme / "Theme.kt"
        path.write_text("package p\n\nval Body = TextStyle(fontSize = 14.sp)\n", encoding="utf-8")
        self.assertEqual(checker.hardcoded_font_sizes(path), [])

    def test_line_comment_is_not_a_size(self) -> None:
        workspace = Workspace(self)
        path = workspace.write("A.kt", "package p\n\n// fontSize = 10.sp\nval x = 1\n")
        self.assertEqual(checker.hardcoded_font_sizes(path), [])

    def test_block_comment_is_not_a_size(self) -> None:
        """Le paquet `theme` documente l'ancien style dans un KDoc : une simple
        mention ne doit pas faire échouer le gate."""
        workspace = Workspace(self)
        path = workspace.write(
            "A.kt",
            "package p\n\n/**\n * Remplace les `Text(..., fontSize = 14.sp)` répétés.\n */\nval x = 1\n",
        )
        self.assertEqual(checker.hardcoded_font_sizes(path), [])

    def test_code_after_a_block_comment_is_still_seen(self) -> None:
        workspace = Workspace(self)
        path = workspace.write(
            "A.kt", "package p\n\n/* doc\n */\nval x = Text(\"a\", fontSize = 9.sp)\n"
        )
        self.assertEqual(checker.hardcoded_font_sizes(path), ["A.kt:5 -> fontSize = 9.sp"])

    def test_check_fails_on_a_screen_with_a_hardcoded_size(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", 'package p\n\nval x = Text("a", fontSize = 10.sp)\n')
        stream = io.StringIO()
        code = checker.check(workspace.root, 300, stream=stream)
        self.assertNotEqual(code, 0)
        self.assertIn("taille de police en dur", stream.getvalue())

    def test_check_passes_when_the_size_comes_from_the_theme(self) -> None:
        workspace = Workspace(self)
        workspace.write(
            "A.kt",
            'package p\n\nval x = Text("a", style = MaterialTheme.typography.labelSmall)\n',
        )
        self.assertEqual(workspace.run(), 0)


class HardcodedTextTest(unittest.TestCase):
    """Tout texte affiché vient de `res/values*/strings.xml`.

    Ce contrôle verrouille la migration des libellés : il a fallu déplacer
    ~340 chaînes en dur vers les ressources (et les traduire dans dix langues)
    pour que l'interface cesse d'être anglaise hors de `values-fr`. Sans lui, le
    premier `Text("Save changes")` ajouté à la main rouvre la brèche.
    """

    def test_english_phrase_is_reported(self) -> None:
        workspace = Workspace(self)
        path = workspace.write("A.kt", 'package p\n\nval x = Text("Save changes")\n')
        self.assertEqual(checker.hardcoded_text(path), ["A.kt:3 -> 'Save changes'"])

    def test_parameterised_phrase_is_reported(self) -> None:
        """Le texte fixe d'un gabarit reste un libellé, même avec un argument."""
        workspace = Workspace(self)
        path = workspace.write("A.kt", 'package p\n\nval x = "Score: 0.00"\n')
        self.assertEqual(len(checker.hardcoded_text(path)), 1)

    def test_identifier_and_codes_are_not_reported(self) -> None:
        """`BTC`, `image/jpeg`, `chat_list`, `BLOCKED_RISK` ne sont pas du texte."""
        workspace = Workspace(self)
        path = workspace.write(
            "A.kt",
            'package p\n\nval a = "BTC"\nval b = "image/jpeg"\nval c = "chat_list"\nval d = "BLOCKED_RISK"\n',
        )
        self.assertEqual(checker.hardcoded_text(path), [])

    def test_resource_backed_label_is_not_reported(self) -> None:
        workspace = Workspace(self)
        path = workspace.write("A.kt", "package p\n\nval x = Text(strings.saveButton)\n")
        self.assertEqual(checker.hardcoded_text(path), [])

    def test_allowlisted_text_is_not_reported(self) -> None:
        """Les noms de langue du prompt Gemini ne sont pas affichés."""
        workspace = Workspace(self)
        path = workspace.write("A.kt", 'package p\n\nval x = "French (Français)"\n')
        self.assertEqual(checker.hardcoded_text(path), [])

    def test_comment_text_is_not_reported(self) -> None:
        workspace = Workspace(self)
        path = workspace.write("A.kt", "package p\n\n// Save changes est un libellé\nval x = 1\n")
        self.assertEqual(checker.hardcoded_text(path), [])

    def test_check_fails_on_a_screen_with_hardcoded_text(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", 'package p\n\nval x = Text("Save changes")\n')
        stream = io.StringIO()
        code = checker.check(workspace.root, 300, stream=stream)
        self.assertNotEqual(code, 0)
        self.assertIn("libellé en dur", stream.getvalue())


class AppStringsCoherenceTest(unittest.TestCase):
    """Le modèle et ses usages doivent rester alignés sans compilateur.

    Deux fautes invisibles autrement : un champ déclaré mais jamais projeté
    (donc `null` à l'exécution) et un `strings.champInexistant`, exactement le
    genre de faute de frappe que produit une migration de 350 chaînes.
    """

    def _workspace(self, project_cancel: bool = True) -> Workspace:
        workspace = Workspace(self)
        workspace.write(
            "Localization.kt",
            "package p\n\ndata class AppStrings(\n    val saveButton: String,\n    val cancelButton: String,\n)\n",
        )
        projection = "        saveButton = context.getString(R.string.save_button),\n"
        if project_cancel:
            projection += "        cancelButton = context.getString(R.string.cancel_button),\n"
        workspace.write(
            "StringsResources.kt",
            "package p\n\nfun appStrings(context: Context) =\n    AppStrings(\n" + projection + "    )\n",
        )
        return workspace

    def test_declared_but_unprojected_field_is_detected(self) -> None:
        declared, projected = checker.app_strings_model(self._workspace(project_cancel=False).root)
        self.assertEqual(sorted(declared - projected), ["cancelButton"])

    def test_unknown_field_access_is_detected(self) -> None:
        workspace = self._workspace()
        workspace.write("Screen.kt", "package p\n\nval a = strings.saveButton\nval b = strings.cancelBtn\n")
        declared, _ = checker.app_strings_model(workspace.root)
        self.assertEqual(checker.undeclared_field_accesses(workspace.root, declared), ["Screen.kt:4 -> cancelBtn"])

    def test_qualified_paths_are_not_taken_for_accesses(self) -> None:
        """`androidx.compose.ui.geometry.Offset` contient `ui.` : pas un accès."""
        workspace = self._workspace()
        workspace.write(
            "Screen.kt",
            "package p\n\nimport androidx.compose.ui.geometry.Offset\n\nval a = strings.saveButton\n",
        )
        declared, _ = checker.app_strings_model(workspace.root)
        self.assertEqual(checker.undeclared_field_accesses(workspace.root, declared), [])

    def test_check_passes_when_model_and_usages_agree(self) -> None:
        workspace = self._workspace()
        workspace.write("Screen.kt", "package p\n\nfun screen(strings: AppStrings) = strings.saveButton\n")
        self.assertEqual(workspace.run(), 0)

    def test_access_without_a_declared_receiver_is_detected(self) -> None:
        """Le champ existe, le récepteur non : `ui.x` sans aucune `val ui` en
        portée ne compile pas. C'est la faute produite par une réécriture en
        masse — le champ a bien été ajouté au modèle, la variable a été oubliée
        dans une fonction."""
        workspace = self._workspace()
        workspace.write("Screen.kt", "package p\n\nfun screen() {\n    val x = ui.saveButton\n}\n")
        self.assertEqual(checker.unbound_field_receivers(workspace.root), [
            "Screen.kt: récepteur non déclaré -> ui.<champ>"
        ])
        self.assertNotEqual(workspace.run(), 0)

    def test_declared_receiver_is_accepted(self) -> None:
        """Les trois formes réelles : paramètre, `val`, fonction du ViewModel."""
        workspace = self._workspace()
        workspace.write(
            "Screen.kt",
            "package p\n\nfun a(ui: AppStrings) = ui.saveButton\n\n"
            "fun b() {\n    val ui = appStrings(context)\n    ui.cancelButton\n}\n"
            "\nfun strings(language: String) = ui.saveButton\n",
        )
        self.assertEqual(checker.unbound_field_receivers(workspace.root), [])

    def test_package_name_is_not_a_receiver(self) -> None:
        """`package com.aitrade.ui` fait apparaître `ui` sans être un accès."""
        workspace = self._workspace()
        workspace.write("Screen.kt", "package p\n\nfun a(ui: AppStrings) = ui.saveButton\n")
        self.assertEqual(checker.unbound_field_receivers(workspace.root), [])

    def test_folder_without_the_model_skips_the_check(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\nval x = 1\n")
        self.assertIsNone(checker.app_strings_model(workspace.root))
        self.assertEqual(workspace.run(), 0)


class SizeWarningTest(unittest.TestCase):
    def test_oversized_file_is_a_warning_not_a_failure(self) -> None:
        workspace = Workspace(self)
        body = "\n".join(f"val v{index} = {index}" for index in range(320))
        workspace.write("A.kt", f"package p\n\n{body}\n")
        stream = io.StringIO()
        code = checker.check(workspace.root, 300, stream=stream)
        self.assertEqual(code, 0, "le seuil de taille est un repère, pas une règle")
        self.assertIn("A.kt", stream.getvalue())


class FormattingLimitsTest(unittest.TestCase):
    """Les seuils de mise en forme sont **lus** dans `.editorconfig`, jamais
    recopiés : c'est ce qui empêche le contrôle Python de dériver du gate ktlint
    qu'il reproduit."""

    def test_the_depot_thresholds_are_read(self) -> None:
        self.assertEqual(checker.formatting_limits(), (140, 4, "space"))

    def test_a_section_only_applies_to_the_files_it_matches(self) -> None:
        self.assertTrue(checker._section_matches("*.{kt,kts}", "ui/Foo.kt"))
        self.assertTrue(checker._section_matches("*.{kt,kts}", "Foo.kts"))
        self.assertFalse(checker._section_matches("*.{kt,kts}", "Foo.py"))
        #: Un motif sans barre oblique porte sur le nom du fichier, comme le veut
        #: la spécification `.editorconfig` — pas sur le chemin.
        self.assertTrue(checker._section_matches("*", "a/b/Foo.kt"))

    def test_the_last_matching_section_wins(self) -> None:
        with_editorconfig(
            self,
            "root = true\n\n[*]\nindent_size = 2\nmax_line_length = 100\n\n"
            "[*.{kt,kts}]\nindent_size = 4\n\n[*.md]\nmax_line_length = 20\n",
        )
        self.assertEqual(
            checker.editorconfig_properties(("indent_size", "max_line_length")),
            {"indent_size": "4", "max_line_length": "100"},
        )

    def test_an_absent_property_falls_back_to_the_official_style(self) -> None:
        """Sans `max_line_length`, ktlint applique le défaut de `ktlint_official`."""
        with_editorconfig(self, "[*.{kt,kts}]\nindent_size = 2\n")
        self.assertEqual(checker.formatting_limits(), (140, 2, "space"))

    def test_a_disabled_limit_is_not_replaced_by_a_default(self) -> None:
        """`off` veut dire « ktlint ne mesure rien » : inventer un seuil ferait
        échouer la CI pour une règle que le gate n'applique pas."""
        with_editorconfig(self, "[*]\nmax_line_length = off\n")
        self.assertIsNone(checker.formatting_limits()[0])

    def test_an_illegible_limit_is_not_invented(self) -> None:
        with_editorconfig(self, "[*]\nmax_line_length = beaucoup\n")
        self.assertIsNone(checker.formatting_limits()[0])


class ScanFormattingTest(unittest.TestCase):
    """Le genre d'une ligne décide de ce qui s'y applique. Ces tests portent sur
    le lecteur lui-même, parce que c'est lui qui pourrait se tromper en silence."""

    def kinds(self, text: str) -> dict:
        return {fact.number: fact.kind for fact in checker.scan_formatting(text)}

    def test_a_comment_line_is_not_code(self) -> None:
        self.assertEqual(self.kinds("package p\n\n// note\nval x = 1\n")[3], "comment")

    def test_a_block_comment_interior_is_not_code(self) -> None:
        kinds = self.kinds("package p\n\n/**\n * Texte\n */\nval x = 1\n")
        self.assertEqual(kinds[4], "kdoc")
        self.assertEqual(kinds[5], "kdoc")
        self.assertEqual(kinds[6], "code")

    def test_a_raw_string_body_is_string(self) -> None:
        kinds = self.kinds('package p\n\nval x = """\n   corps\n"""\n')
        self.assertEqual(kinds[4], "string")

    def test_a_line_holding_only_a_literal_is_string(self) -> None:
        kinds = self.kinds('package p\n\nval x =\n    "texte",\n')
        self.assertEqual(kinds[4], "string")

    def test_the_tail_of_a_closing_raw_quote_is_code(self) -> None:
        """Le `trimIndent()` qui suit la fermeture d'une chaîne brute est du
        code, pas du texte : une telle ligne obéit aux règles."""
        kinds = self.kinds('package p\n\nval x = """\ncorps\n""".trimIndent()\n')
        self.assertEqual(kinds[5], "code")

    def test_a_template_spanning_lines_does_not_swallow_the_following_code(self) -> None:
        """Le cas qui piège un lecteur naïf : `"${String.format(` ouvre un
        gabarit sur plusieurs lignes, et le `"` d'un `"%.2f"` imbriqué ne
        referme pas la chaîne englobante. Sans ce suivi, les lignes suivantes
        seraient lues comme du texte."""
        text = (
            "package p\n\n"
            "fun f() {\n"
            '    val x = "a${String.format(\n'
            '        "%.2f",\n'
            "        y,\n"
            '    )}b"\n'
            "    val z = 1\n"
            "}\n"
        )
        kinds = self.kinds(text)
        self.assertEqual(kinds[4], "code")
        self.assertEqual(kinds[6], "code")
        self.assertEqual(kinds[7], "code")
        self.assertEqual(kinds[8], "code")


class LineLengthTest(unittest.TestCase):
    """`max-line-length` : la limite, et **les mêmes exceptions** que la règle."""

    def test_a_code_line_over_the_limit_fails(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\nfun f() {\n    val x = " + "a" * 200 + "\n}\n")
        code, report = workspace.report()
        self.assertEqual(code, 1, report)
        self.assertIn("A.kt:4", report)
        self.assertIn("limite 140", report)

    def test_a_line_at_the_limit_passes(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\nfun f() {\n    " + "a" * 136 + "\n}\n")
        code, report = workspace.report()
        self.assertEqual(code, 0, report)

    def test_a_line_one_character_over_the_limit_fails(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\nfun f() {\n    " + "a" * 137 + "\n}\n")
        code, report = workspace.report()
        self.assertEqual(code, 1, report)

    def test_the_length_includes_the_indentation(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\nfun f() {\n" + " " * 8 + "val x = " + "a" * 128 + "\n}\n")
        code, report = workspace.report()
        self.assertEqual(code, 1, report)

    def test_a_comment_alone_on_its_line_is_exempt(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\n// " + "mot " * 60 + "\nval x = 1\n")
        code, report = workspace.report()
        self.assertEqual(code, 0, report)

    def test_a_comment_after_code_is_not_exempt(self) -> None:
        """C'est la moitié de la règle qui compte : seul un commentaire **seul**
        sur sa ligne échappe à la limite."""
        workspace = Workspace(self)
        workspace.write(
            "A.kt", "package p\n\nval x = " + "a" * 100 + " // " + "mot " * 20 + "\n"
        )
        code, report = workspace.report()
        self.assertEqual(code, 1, report)
        self.assertIn("A.kt:3", report)

    def test_a_raw_string_body_is_exempt(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", 'package p\n\nval x = """\n' + "a" * 200 + '\n"""\n')
        code, report = workspace.report()
        self.assertEqual(code, 0, report)

    def test_a_literal_alone_on_its_line_is_exempt(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", 'package p\n\nval x =\n    "' + "a" * 200 + '",\n')
        code, report = workspace.report()
        self.assertEqual(code, 0, report)

    def test_a_crlf_file_is_measured_without_its_carriage_return(self) -> None:
        """Une fin de ligne CRLF ne doit pas compter pour un caractère de plus :
        sinon un fichier de 140 caractères par ligne échouerait pour 141."""
        workspace = Workspace(self)
        path = workspace.root / "A.kt"
        path.write_text(
            "package p\r\n\r\nfun f() {\r\n    " + "a" * 136 + "\r\n}\r\n",
            encoding="utf-8",
            newline="",
        )
        code, report = workspace.report()
        self.assertEqual(code, 0, report)

    def test_a_long_import_is_exempt(self) -> None:
        """Un import ne se coupe pas : ktlint l'exempte, et l'exempter évite un
        faux positif que rien ne pourrait corriger."""
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\nimport a." + "b" * 200 + ".*\n")
        code, report = workspace.report()
        self.assertEqual(code, 0, report)


class IndentTest(unittest.TestCase):
    """La part d'`indent` qui se décide sans parseur : espaces uniquement, et
    multiples de `indent_size`."""

    def test_a_tab_in_the_indentation_fails(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\nfun f() {\n\tval x = 1\n}\n")
        code, report = workspace.report()
        self.assertEqual(code, 1, report)
        self.assertIn("tabulation", report)

    def test_an_indentation_that_is_not_a_multiple_of_four_fails(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\nfun f() {\n      val x = 1\n}\n")
        code, report = workspace.report()
        self.assertEqual(code, 1, report)
        self.assertIn("indentation de 6 espaces", report)

    def test_four_and_eight_spaces_pass(self) -> None:
        workspace = Workspace(self)
        workspace.write(
            "A.kt",
            "package p\n\nfun f() {\n    if (true) {\n        val x = 1\n    }\n}\n",
        )
        code, report = workspace.report()
        self.assertEqual(code, 0, report)

    def test_a_kdoc_star_line_is_not_indented_code(self) -> None:
        """Un KDoc aligne ses astérisques sous le `/**` : elles tombent à 4n + 1,
        ce qui n'est pas de l'indentation de code."""
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\n/**\n * Un mot.\n */\nfun f() {\n    val x = 1\n}\n")
        code, report = workspace.report()
        self.assertEqual(code, 0, report)

    def test_a_raw_string_body_is_not_indented_code(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", 'package p\n\nval x = """\n   corps\n     fin\n"""\n')
        code, report = workspace.report()
        self.assertEqual(code, 0, report)

    def test_code_after_a_closed_block_comment_is_still_indented_code(self) -> None:
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\nfun f() {\n      /* note */ val x = 1\n}\n")
        code, report = workspace.report()
        self.assertEqual(code, 1, report)
        self.assertIn("indentation de 6 espaces", report)


class FormattingScopeTest(unittest.TestCase):
    """La mise en forme couvre tout le Kotlin du module, pas seulement les écrans
    inspectés par les contrôles structurels : `--root` et `--sources` sont deux
    périmètres, et le confondre laisserait `api/`, `data/` et `engine/` de côté."""

    def test_sources_are_scanned_recursively(self) -> None:
        """Deux périmètres distincts : `--root` borne les contrôles structurels,
        `--sources` la mise en forme, qui descend dans les sous-paquets."""
        ui = Workspace(self)
        ui.write("A.kt", "package p\n\nval a = 1\n")
        module = Workspace(self)
        module.write("engine/deep/B.kt", "package p\n\nfun f() {\n      val b = 2\n}\n")
        code, report = ui.report(sources=module.root)
        self.assertEqual(code, 1, report)
        self.assertIn("B.kt:4", report)

    def test_an_empty_sources_directory_fails(self) -> None:
        """Un périmètre vide rendrait le contrôle vert sans rien inspecter."""
        workspace = Workspace(self)
        workspace.write("A.kt", "package p\n\nval a = 1\n")
        code, report = workspace.report(sources=workspace.root / "absent")
        self.assertEqual(code, 1, report)
        self.assertIn("aucun fichier Kotlin", report)


class RealKotlinTreeTest(unittest.TestCase):
    """Le contrôle de mise en forme est le seul à couvrir **tout** le Kotlin du
    module, hors `ui/` : c'est donc ici que se verrait une ligne trop longue
    ajoutée dans `engine/` ou `data/`."""

    def test_the_whole_module_passes_formatting(self) -> None:
        stream = io.StringIO()
        code = checker.check(UI_ROOT, 300, stream=stream, sources=SOURCES_ROOT)
        report = stream.getvalue()
        self.assertEqual(code, 0, report)
        self.assertIn("aucune ligne de plus de 140 caractères", report)
        self.assertIn("multiple de 4", report)

    def test_every_line_over_the_limit_is_one_ktlint_exempts(self) -> None:
        files = sorted(SOURCES_ROOT.rglob("*.kt"))
        self.assertGreater(len(files), 60, "les sources Kotlin ont disparu")
        offenders, long_lines = [], 0
        for path in files:
            for fact in checker.scan_formatting(path.read_text(encoding="utf-8")):
                if fact.length <= 140:
                    continue
                long_lines += 1
                if fact.kind != checker.CODE or fact.body.startswith(("package", "import")):
                    continue
                offenders.append(f"{path.name}:{fact.number}")
        self.assertEqual(offenders, [], "ligne de code au-delà de 140 caractères")
        #: Garde-fou inverse : si plus aucune ligne n'était longue, l'exemption
        #: ne serait plus exercée et le contrôle pourrait être faux en silence.
        self.assertGreater(long_lines, 0, "aucune ligne longue : exemption non exercée")


class CiWiringTest(unittest.TestCase):
    """Le contrôle doit tourner en CI, sinon il ne sert qu'à la main."""

    CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

    def test_ci_runs_the_checker(self) -> None:
        text = self.CI_PATH.read_text(encoding="utf-8")
        self.assertIn("python scripts/kotlin_ui_check.py", text)
        self.assertIn("kotlin-lint:", text)

    def test_ci_references_an_existing_script(self) -> None:
        """Un chemin renommé ne casserait qu'en CI, au pire moment."""
        self.assertTrue(SCRIPT_PATH.is_file(), f"script absent : {SCRIPT_PATH}")
        self.assertIn(SCRIPT_PATH.name, self.CI_PATH.read_text(encoding="utf-8"))


class RealPackageTest(unittest.TestCase):
    """Garde-fou du découpage réel : il doit rester vert sur le vrai paquet."""

    def test_ui_package_has_files_and_named_imports(self) -> None:
        """Sans cette garde, le test suivant passerait sans rien inspecter."""
        files = sorted(UI_ROOT.glob("*.kt"))
        self.assertGreater(len(files), 20, "le paquet ui semble avoir disparu")
        named = [line for path in files for line, _ in checker.named_imports(path)]
        self.assertGreater(len(named), 100, "aucun import nommé lu : le contrôle serait vide")
        declarations = [name for path in files for name in checker.declarations(path)]
        self.assertGreater(len(declarations), 40, "aucune déclaration lue")

    def test_ui_package_passes_every_check(self) -> None:
        stream = io.StringIO()
        code = checker.check(UI_ROOT, 300, stream=stream)
        report = stream.getvalue()
        self.assertEqual(code, 0, report)
        self.assertIn("[OK] aucun import nommé inutilisé", report)
        self.assertIn("aucune taille de police en dur", report)
        self.assertIn("toutes uniques", report)
        self.assertIn("aucun libellé en dur", report)
        self.assertIn("champs AppStrings", report)
        self.assertIn("aucune ligne de plus de 140 caractères", report)
        self.assertIn("multiple de 4", report)


if __name__ == "__main__":
    unittest.main()
