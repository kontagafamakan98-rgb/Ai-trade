"""Le contrat de l'écran d'administration Android, relu dans les sources Kotlin.

Aucun compilateur n'est disponible partout (ni JDK, ni SDK Android), et ce que cet
écran peut faire de pire ne se voit ni à l'écran ni à la compilation :

* **écrire dans la base que le serveur utilise sans confirmation** — l'écran doit
  nommer les tables avant d'envoyer l'aller-retour, et rien d'autre ne doit
  pouvoir le déclencher ;
* **porter la clé interne dans l'APK** — elle se saisit sur l'appareil et s'y
  conserve chiffrée ; compiler `INTERNAL_API_KEY` la donnerait à quiconque
  décompile l'application, pour un endpoint qui sait écrire en production ;
* **inventer un rapport** — l'application simule ses prix, ses actualités et son
  backtest, mais un rapport de vérification simulé ressemblerait à une base en bon
  état : c'est exactement le silence que la sonde existe pour rompre.

Ces trois propriétés-là ne se vérifient pas à l'œil ; elles se vérifient dans le
code, et c'est ce que fait ce fichier — même méthode que
`tests/test_android_strings.py` et `tests/test_kotlin_ui_check.py`.
"""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP_SRC = ROOT / "app" / "src"
MAIN = APP_SRC / "main" / "java" / "com" / "aitrade"
UI = MAIN / "ui"
DATA = MAIN / "data"
API = MAIN / "api"

ADMIN_SCREEN = UI / "AdminSupabaseScreen.kt"
ADMIN_OUTCOME = UI / "AdminProbeOutcomeCard.kt"
ADMIN_API = API / "AdminProbeApi.kt"
DASHBOARD = UI / "TerminalDashboard.kt"
TERMINAL_SCREEN = UI / "TerminalScreen.kt"
HEALTH_CARD = UI / "SystemHealthObservabilityCard.kt"
DATABASE = DATA / "Database.kt"
REPOSITORY = DATA / "TradingRepository.kt"
VIEW_MODEL = UI / "TradingViewModel.kt"
README = ROOT / "README.md"


def read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def without_comments(text: str) -> str:
    """Le code seul : un commentaire qui cite `@POST` ne doit pas compter."""
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        lines.append(line.split("//", 1)[0])
    return "\n".join(lines)


def kotlin_sources() -> list[pathlib.Path]:
    return sorted(APP_SRC.rglob("*.kt"))


class ReachabilityTest(unittest.TestCase):
    """L'écran est atteignable, et pas par un sixième onglet."""

    def setUp(self) -> None:
        self.dashboard = read(DASHBOARD)
        self.card = read(HEALTH_CARD)
        self.terminal = read(TERMINAL_SCREEN)

    def test_the_dashboard_routes_to_the_admin_screen(self) -> None:
        self.assertIn("AdminSupabaseScreen(", self.dashboard)
        self.assertIn("viewModel.adminProbe", self.dashboard)
        self.assertIn("viewModel.adminEndpoint", self.dashboard)

    def test_the_route_is_left_by_a_close_button_and_the_back_key(self) -> None:
        """Un écran plein écran sans sortie serait un piège, une fois ouvert."""
        self.assertIn("onClose = { adminOpen = false }", self.dashboard)
        self.assertIn("BackHandler { adminOpen = false }", self.dashboard)

    def test_the_observability_card_opens_it(self) -> None:
        """C'est le bouton de la carte « santé système » qui y mène."""
        self.assertIn("onOpenAdmin: () -> Unit", self.card)
        self.assertIn("onClick = onOpenAdmin", self.card)
        self.assertIn('testTag("admin_open_button")', self.card)
        self.assertIn("onOpenAdmin = onOpenAdmin", self.terminal)

    def test_the_navigation_bar_keeps_five_destinations(self) -> None:
        """Material en recommande cinq au plus : l'administration n'en ajoute pas."""
        tabs = read(DASHBOARD).split("val tabs =", 1)[1].split("val icons =", 1)[0]
        entries = re.findall(r"strings\.\w+,", tabs)
        self.assertEqual(5, len(entries), f"{len(entries)} onglet(s) : {entries}")


class RoundtripConfirmationTest(unittest.TestCase):
    """Le seul chemin qui écrit passe par une confirmation qui nomme les tables."""

    def setUp(self) -> None:
        self.screen = read(ADMIN_SCREEN)

    def test_the_screen_asks_for_the_tables_before_writing(self) -> None:
        self.assertIn("AdminRoundtripConfirmDialog(", self.screen)
        self.assertIn('testTag("admin_confirm_dialog")', self.screen)

    def test_the_network_call_happens_only_in_the_confirmation(self) -> None:
        """Compter les appels : deux chemins vers l'écriture seraient un de trop."""
        self.assertEqual(
            1,
            self.screen.count("onRunRoundtrip("),
            "l'aller-retour doit être déclenché à un seul endroit : la confirmation",
        )
        confirm = self.screen.split("AdminRoundtripConfirmDialog(", 1)[1]
        body = confirm.split("\n    )", 1)[0]
        self.assertIn("onRunRoundtrip(tables)", body)

    def test_the_button_only_opens_the_dialog(self) -> None:
        """Le bouton prépare la liste ; il n'envoie rien lui-même."""
        button = self.screen.split('testTag("admin_roundtrip_button")', 1)[0]
        call = button.rsplit("onClick =", 1)[1]
        self.assertIn("pendingTables =", call)
        self.assertNotIn("onRunRoundtrip", call)

    def test_the_dialog_names_the_tables(self) -> None:
        self.assertIn("adminRoundtripConfirmBody", self.screen)
        self.assertIn('tables.joinToString(", ")', self.screen)

    def test_an_empty_selection_cannot_be_sent(self) -> None:
        """Le backend refuse une liste vide : on ne l'envoie pas pour l'apprendre."""
        self.assertIn("adminTableNames(tablesText).isNotEmpty()", self.screen)
        self.assertIn("if (tables.isEmpty()) return", read(VIEW_MODEL))


class BackendCallsTest(unittest.TestCase):
    """Les deux appels du backend, avec la bonne méthode et la clé au bon endroit."""

    def setUp(self) -> None:
        self.api = read(ADMIN_API)

    def test_the_check_is_a_get_and_a_roundtrip_a_post(self) -> None:
        self.assertIn('@GET("admin/supabase/check")', self.api)
        self.assertIn('@POST("admin/supabase/roundtrip")', self.api)

    def test_the_roundtrip_is_never_a_get(self) -> None:
        """Un GET se rejoue par un cache ou un préchargement de lien — et il écrit."""
        self.assertNotIn('@GET("admin/supabase/roundtrip")', self.api)
        self.assertNotIn("GET(\"admin/supabase/roundtrip", self.api)

    def test_the_written_tables_are_the_only_body(self) -> None:
        self.assertIn("data class AdminRoundtripRequest(", self.api)
        self.assertIn('@Body request: AdminRoundtripRequest', self.api)

    def test_the_key_travels_in_a_header_and_never_in_the_url(self) -> None:
        """Les **deux** appels, pas seulement l'un : les deux sont protégés."""
        self.assertEqual(
            2,
            self.api.count('@Header("X-API-Key") apiKey: String'),
            "chaque appel doit porter la clé en en-tête",
        )
        for forbidden in ('@Query("key")', '@Query("api_key")', '@Query("apikey")'):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.api)

    def test_each_failure_has_its_own_name(self) -> None:
        """« Injoignable » et « refusé » n'appellent pas la même conduite."""
        for kind in ("NOT_CONFIGURED", "BAD_URL", "REFUSED", "UNREACHABLE"):
            with self.subTest(kind=kind):
                self.assertIn(kind, self.api)


class SecretTest(unittest.TestCase):
    """La clé interne n'est jamais compilée, et se conserve chiffrée."""

    def test_no_kotlin_source_reads_the_internal_key_from_build_config(self) -> None:
        offenders = [
            str(path.relative_to(ROOT))
            for path in kotlin_sources()
            if "BuildConfig.INTERNAL_API_KEY" in read(path)
        ]
        self.assertEqual([], offenders, "la clé interne serait compilée dans l'APK")

    def test_the_admin_client_has_no_backend_url_of_its_own(self) -> None:
        """L'URL se saisit : une adresse en dur désignerait la base d'un autre."""
        self.assertNotIn("BASE_URL", read(ADMIN_API))
        self.assertNotIn("http://", read(ADMIN_API))
        self.assertNotIn("https://", read(ADMIN_API))

    def test_the_key_is_encrypted_at_rest_like_the_broker_ones(self) -> None:
        repository = read(REPOSITORY)
        self.assertIn("Crypto.encrypt(endpoint.apiKey)", repository)
        self.assertIn("Crypto.decrypt(endpoint.apiKey)", repository)
        self.assertIn("saveAdminEndpoint", repository)

    def test_the_endpoint_is_its_own_row_and_the_schema_version_moved(self) -> None:
        database = read(DATABASE)
        self.assertIn('@Entity(tableName = "admin_endpoint")', database)
        self.assertIn("AdminEndpoint::class,", database)
        version = re.search(r"version = (\d+),", database)
        self.assertIsNotNone(version, "version de base illisible")
        self.assertGreaterEqual(int(version.group(1)), 6)

    def test_the_key_is_typed_on_the_device_not_read_from_the_environment(self) -> None:
        self.assertIn("saveAdminEndpoint(baseUrl, apiKey)", read(DASHBOARD))
        self.assertIn("fun saveAdminEndpoint(", read(VIEW_MODEL))


class NoFabricatedReportTest(unittest.TestCase):
    """L'application ne fabrique jamais un rapport : elle affiche celui du backend."""

    def test_no_source_constructs_a_report(self) -> None:
        """La déclaration du modèle compte, sa construction non.

        Un test peut légitimement fabriquer un rapport comme fixture ; c'est le
        **code de l'application** qui ne doit jamais en inventer un.
        """
        construction = re.compile(r"(?<!class )AdminProbeReport\(")
        offenders = [
            str(path.relative_to(ROOT))
            for path in sorted(MAIN.rglob("*.kt"))
            if construction.search(read(path))
        ]
        self.assertEqual([], offenders, "un rapport est construit sur l'appareil")
        # Garde-fou : le contrôle n'est ni vide, ni aveugle.
        self.assertIsNone(
            construction.search("data class AdminProbeReport("),
            "la déclaration ne doit pas être prise pour une construction",
        )
        self.assertIsNotNone(
            construction.search("val invente = AdminProbeReport("),
            "une construction doit être vue",
        )

    def test_the_screen_says_when_there_is_no_backend(self) -> None:
        self.assertIn("adminNotConfigured", read(ADMIN_SCREEN))
        self.assertIn("AdminProbeFailure.NOT_CONFIGURED", read(VIEW_MODEL))

    def test_the_report_shown_is_the_payload_of_the_answer(self) -> None:
        """Le corps du rapport vient de la réponse, pas d'un modèle recopié."""
        self.assertIn("AdminProbeOutcome.Report(block())", read(ADMIN_API))
        self.assertIn("report.sections.forEach", read(ADMIN_OUTCOME))

    def test_the_backend_still_exposes_exactly_these_two_calls(self) -> None:
        """Le contrat que l'application vise existe bien côté serveur."""
        router = read(ROOT / "api" / "admin_router.py")
        self.assertIn('@router.get("/supabase/check")', router)
        self.assertIn('@router.post("/supabase/roundtrip")', router)
        self.assertIn("Depends(require_api_key)", router)


class DocumentationTest(unittest.TestCase):
    """Le README dit ce que l'écran fait, et ce qu'il ne fait pas."""

    def setUp(self) -> None:
        self.text = read(README)

    def test_the_admin_screen_is_documented(self) -> None:
        self.assertIn("écran d'administration", self.text)

    def test_the_key_is_documented_as_never_compiled(self) -> None:
        self.assertRegex(self.text, r"jamais\s+compilée|n'est jamais compilée")

    def test_the_confirmation_is_documented(self) -> None:
        self.assertIn("confirmation", self.text)


if __name__ == "__main__":
    unittest.main()
