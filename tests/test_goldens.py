"""Tests du harnais de valeurs de référence (`tests/goldens/*.json`).

Le gate lui-même mérite des tests, sinon il pourrait ne rien contrôler : un
vérificateur qui compare toujours à lui-même passe au vert indéfiniment. On
couvre donc les deux moitiés :

* **la dérive est détectée** — un golden modifié, absent, illisible ou mal
  étiqueté fait échouer la vérification, avec la commande de régénération ;
* **le vert est réel** — quand le golden correspond au code, l'échec n'a pas
  lieu, et régénérer deux fois produit des octets identiques (sinon le gate
  deviendrait instable de lui-même).

Le dernier groupe de tests vérifie que la CI exécute bien le contrôle et que les
fichiers sont **versionnés** : un golden non commité ne référence rien.
"""

from __future__ import annotations

import contextlib
import fnmatch
import importlib.util
import io
import json
import pathlib
import os
import tempfile
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "goldens.py"
GOLDENS_DIR = REPO_ROOT / "tests" / "goldens"
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
GITIGNORE = REPO_ROOT / ".gitignore"

SPEC = importlib.util.spec_from_file_location("goldens", SCRIPT_PATH)
assert SPEC and SPEC.loader, SCRIPT_PATH
goldens = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(goldens)

from core import alert_engine as ae  # noqa: E402  (après l'insertion de la racine dans sys.path)


def _synthetic(value=1, name="synthetic"):
    """Jeu de valeurs jetable : la dérive se teste sans toucher aux vrais goldens."""
    return goldens.Dataset(name, "jeu synthétique de test", lambda: {"value": value})


class RepositoryGoldensTest(unittest.TestCase):
    """Les valeurs de référence du dépôt doivent correspondre au code."""

    def test_every_golden_is_up_to_date(self) -> None:
        """Le contrat du gate est `Outcome.ok` : « à jour », ou « ignoré » **annoncé**.

        Un jeu non recalculable ici — `postgres_schema` sans serveur PostgreSQL —
        n'est pas une dérive : le script le dit et sort en `0` (voir `main`).
        Exiger « ok » à tout prix ferait rougir la suite sur une machine sans base,
        là où il n'y a rien à voir — et l'existence du fichier, elle, est vérifiée
        juste en dessous, donc un `skip` ne peut pas couvrir un golden absent.
        """
        for outcome in goldens.check_all():
            with self.subTest(golden=outcome.name):
                self.assertTrue(outcome.ok, outcome.message)

    def test_golden_files_exist_for_every_dataset(self) -> None:
        """Même ignoré, un jeu garde son fichier : c'est la seule référence qui reste."""
        for dataset in goldens.DATASETS:
            with self.subTest(golden=dataset.name):
                self.assertTrue(goldens.golden_path(dataset).is_file())

    def test_a_skipped_golden_says_what_it_needs(self) -> None:
        """Un `skip` sans condition nommée serait un gate éteint en silence.

        `Dataset.requires` est ce qui sépare « pas exécutable ici, et voici quoi
        faire » d'une erreur avalée : tolérer le `skip` n'est acceptable que pour
        un jeu qui **déclare** ce qu'il lui faut.
        """
        for outcome in goldens.check_all():
            if outcome.status != "skip":
                continue
            with self.subTest(golden=outcome.name):
                self.assertTrue(
                    goldens.dataset_by_name(outcome.name).requires,
                    f"{outcome.name} est ignoré sans dire ce qu'il lui faut",
                )

    def test_the_schema_golden_is_compared_where_a_database_exists(self) -> None:
        """La contrepartie du `skip` : là où une base répond, on **compare**.

        Sans cette exigence, `postgres_schema` resterait ignoré partout — versionné,
        mais jamais relu, donc libre de dériver sans que personne ne le voie.
        """
        if not os.environ.get(goldens.MIGRATION_ENV_VAR):
            self.skipTest(f"{goldens.MIGRATION_ENV_VAR} absent : le jeu n'est pas exécutable ici")
        outcome = goldens.check(goldens.dataset_by_name("postgres_schema"))
        self.assertNotEqual("skip", outcome.status, outcome.message)
        self.assertTrue(outcome.ok, outcome.message)

    def test_goldens_are_not_vacuous(self) -> None:
        """Un golden vide passerait au vert sans rien verrouiller."""
        recorded, problem = goldens.read_recorded(goldens.dataset_by_name("android_localization"))
        self.assertIsNone(problem)
        self.assertGreater(len(recorded["default_keys"]), 200)
        self.assertGreater(len(recorded["default_format_args"]), 10)
        self.assertGreaterEqual(len(recorded["non_translatable_keys"]), 5)
        self.assertGreaterEqual(len(recorded["locales"]), 5)
        for label, locale in recorded["locales"].items():
            with self.subTest(locale=label):
                self.assertGreater(locale["count"], 200)

        engine, problem = goldens.read_recorded(goldens.dataset_by_name("alert_engine"))
        self.assertIsNone(problem)
        self.assertGreater(len(engine["ema_fast"]), 20)
        self.assertGreater(len(engine["ema_slow"]), 5)
        self.assertEqual("buy", engine["crosses"]["hand_check_bullish"]["action"])

        routes, problem = goldens.read_recorded(goldens.dataset_by_name("api_routes"))
        self.assertIsNone(problem)
        self.assertGreaterEqual(len(routes["routes"]), 10)
        self.assertIn("GET /health", routes["routes"])

        floor, problem = goldens.read_recorded(
            goldens.dataset_by_name("similarity_calibration")
        )
        self.assertIsNone(problem)
        self.assertGreater(len(floor["constants"]["probes"]), 2, "sondes absentes")
        self.assertGreaterEqual(
            len(floor["media_selection"]["with_measured_floor"]["kept"]),
            2,
            "règle de sélection des médias non verrouillée",
        )
        for name in ("mid_noise_base", "low_noise_base", "high_noise_base"):
            with self.subTest(base=name):
                self.assertEqual(
                    floor[name]["pairs"],
                    floor[name]["documents"] * floor[name]["probes"],
                )

    def test_the_media_floor_follows_the_measured_noise(self) -> None:
        """Le seuil des médias n'est plus une constante : il suit le bruit mesuré.

        Deux propriétés à la fois, parce que la constante 0,5 se trompait dans les
        deux sens : un corpus dont le bruit dépasse 0,5 doit donner un seuil **plus
        strict** (sinon du bruit entre dans le prompt), et une base discriminante un
        seuil **plus permissif** (sinon des correspondances utiles sont jetées).
        """
        recorded, problem = goldens.read_recorded(
            goldens.dataset_by_name("similarity_calibration")
        )
        self.assertIsNone(problem)
        mid = recorded["mid_noise_base"]
        low = recorded["low_noise_base"]
        high = recorded["high_noise_base"]
        low_bound, high_bound = recorded["constants"]["FLOOR_BOUNDS"]
        margin = recorded["constants"]["NOISE_MARGIN"]

        self.assertGreater(mid["floor"], 0.5, "le bruit de la base n'est pas pris en compte")
        self.assertLess(low["floor"], 0.5, "la constante reste appliquée telle quelle")
        self.assertGreaterEqual(low["floor"], low_bound)
        self.assertAlmostEqual(high["floor"], high_bound, places=6)

        for name, record in (("mid", mid), ("low", low), ("high", high)):
            with self.subTest(base=name):
                expected = record["noise_at_percentile"] + margin
                clamped = min(max(expected, low_bound), high_bound)
                self.assertAlmostEqual(record["floor"], clamped, places=6)

        # Le percentile est un vrai paramètre : le durcir durcit le seuil.
        self.assertLess(mid["floor_at_p50"], mid["floor_at_p90"])
        self.assertLessEqual(mid["floor_at_p90"], mid["floor_at_p100"])
        self.assertLessEqual(mid["floor_at_p90"], mid["floor"])

    def test_the_media_selection_rule_lets_a_tag_pass_the_floor(self) -> None:
        """Une étiquette d'actif n'est pas une similarité de plus.

        Le seuil mesure le bruit du corpus ; un média que l'utilisateur a désigné
        pour cet actif n'en fait pas partie. Le golden fige les deux moitiés de la
        règle — ce que l'étiquette fait passer, et ce qu'elle ne fait pas passer (un
        contenu sans rapport, sous `TAGGED_HARD_FLOOR` : l'erreur de saisie) — ainsi
        que l'ordre, l'étiquette primant sur la similarité.
        """
        recorded, problem = goldens.read_recorded(
            goldens.dataset_by_name("similarity_calibration")
        )
        self.assertIsNone(problem)
        selection = recorded["media_selection"]
        measured = selection["with_measured_floor"]
        floor = measured["floor"]
        hard = selection["tagged_hard_floor"]
        candidates = {row["media_id"]: row for row in selection["candidates"]}
        kept = [row["media_id"] for row in measured["kept"]]

        self.assertLess(hard, floor, "le plancher d'étiquette ne borne plus rien")
        self.assertGreater(floor, 0.5, "le seuil n'est pas celui du bruit mesuré")

        # Retenu **sous** le seuil : sa similarité ne l'aurait pas fait passer.
        self.assertIn("tagged-far", kept)
        self.assertLess(candidates["tagged-far"]["similarity"], floor)
        # Écarté bien qu'étiqueté : contenu sans rapport avec l'actif.
        self.assertNotIn("tagged-off-topic", kept)
        self.assertLess(candidates["tagged-off-topic"]["similarity"], hard)
        # Écartés faute d'étiquette, pourtant plus proches que `tagged-far` : sans
        # cette règle le classement par similarité les aurait préférés.
        for name in ("weak-untagged", "noise-untagged", "other-asset"):
            with self.subTest(media=name):
                self.assertNotIn(name, kept)
        self.assertGreater(
            candidates["weak-untagged"]["similarity"], candidates["tagged-far"]["similarity"]
        )
        # L'étiquette passe devant : les étiquetés d'abord, puis la similarité.
        self.assertEqual(["tagged-close", "tagged-far", "close-untagged"], kept)

        # Base très homogène (seuil sur sa borne haute) : seules les étiquettes
        # passent encore — une base aveugle à ses propres étiquettes serait un
        # défaut, pas une prudence.
        self.assertEqual(
            ["tagged-close", "tagged-far"],
            [row["media_id"] for row in selection["with_strict_floor"]["kept"]],
        )

        # Sans seuil, ce qu'il écartait revient : c'est bien lui qui filtre.
        without = [row["media_id"] for row in selection["without_floor"]["kept"]]
        self.assertEqual(len(candidates), len(without))
        self.assertIn("tagged-off-topic", without)

    def test_cross_records_freeze_the_whole_alert(self) -> None:
        """Le stop-loss, le take-profit et le message sont figés, pas seulement l'ATR.

        Ce sont les nombres qui engagent de l'argent : les figer sur la série
        documentée — au lieu du seul cas dégénéré `high = low = close` — est ce
        qui interdit de déplacer la logique de croisement sans le voir.
        """
        recorded, problem = goldens.read_recorded(goldens.dataset_by_name("alert_engine"))
        self.assertIsNone(problem)
        crosses = recorded["crosses"]

        # La série de niveau ne croise pas (dérive positive : EMA20 reste au-dessus
        # d'EMA50) : l'absence d'alerte est elle-même une propriété verrouillée.
        self.assertIsNone(crosses["none_on_level_series"])
        self.assertIsNone(crosses["flat_market"])

        expectations = (("documented_bullish", "buy", True), ("documented_bearish", "sell", False))
        for name, action, entry_is_above_stop in expectations:
            with self.subTest(cross=name):
                alert = crosses[name]
                self.assertEqual(action, alert["action"])
                for field in ("price", "stop_loss", "take_profit", "ema_fast", "ema_slow", "atr", "message"):
                    self.assertIn(field, alert)
                self.assertGreater(alert["atr"], 0, "ATR nul : le stop et l'objectif ne veulent rien dire")
                if entry_is_above_stop:
                    self.assertLess(alert["stop_loss"], alert["price"])
                    self.assertGreater(alert["take_profit"], alert["price"])
                else:
                    self.assertGreater(alert["stop_loss"], alert["price"])
                    self.assertLess(alert["take_profit"], alert["price"])

        # Les multiplicateurs sont **appliqués** à l'ATR (1,5 x / 3,0 x par
        # défaut, 1,0 x / 2,0 x dans la variante) : un `1.5` figé en dur dans le
        # moteur rendrait ces deux enregistrements identiques.
        default = crosses["documented_bullish"]
        displaced = crosses["documented_bullish_displaced_multipliers"]
        self.assertEqual(default["atr"], displaced["atr"])
        self.assertEqual(default["price"], displaced["price"])
        self.assertNotEqual(default["stop_loss"], displaced["stop_loss"])
        self.assertNotEqual(default["take_profit"], displaced["take_profit"])
        self.assertAlmostEqual(
            default["price"] - 1.5 * default["atr"], default["stop_loss"], places=5
        )
        self.assertAlmostEqual(
            default["price"] + 3.0 * default["atr"], default["take_profit"], places=5
        )
        self.assertAlmostEqual(
            displaced["price"] - 1.0 * displaced["atr"], displaced["stop_loss"], places=5
        )
        self.assertAlmostEqual(
            displaced["price"] + 2.0 * displaced["atr"], displaced["take_profit"], places=5
        )
        # Le message suit les périodes passées en paramètres, et reste en français
        # (c'est le texte réellement journalisé).
        self.assertIn("EMA20 crossover EMA50", default["message"])
        self.assertIn("(haussier)", default["message"])
        self.assertIn("EMA5 crossover EMA10", crosses["documented_bullish_custom_periods"]["message"])
        self.assertIn("crossunder", crosses["documented_bearish"]["message"])

    def test_rebuilding_is_deterministic(self) -> None:
        """Deux calculs du même code doivent donner exactement la même valeur.

        Sinon le gate signalerait une dérive à chaque exécution, et serait
        désactivé à la première occasion.
        """
        for name in ("alert_engine", "android_localization", "similarity_calibration"):
            dataset = goldens.dataset_by_name(name)
            with self.subTest(golden=name):
                self.assertEqual(dataset.build(), dataset.build())


class BreakoutSeriesTest(unittest.TestCase):
    """La série de croisement doit croiser **là où** `detect_cross` regarde.

    `detect_cross` n'examine que la dernière chandelle close : une série qui
    croise ailleurs ne verrouillerait rien, et le golden enregistrerait `None`.
    """

    def test_bullish_series_crosses_up_on_the_last_candle(self) -> None:
        alert = ae.detect_cross(goldens.documented_breakout_candles("up"))
        self.assertIsNotNone(alert)
        self.assertEqual("buy", alert["action"])

    def test_bearish_series_crosses_down_on_the_last_candle(self) -> None:
        alert = ae.detect_cross(goldens.documented_breakout_candles("down"))
        self.assertIsNotNone(alert)
        self.assertEqual("sell", alert["action"])

    def test_the_level_series_does_not_cross(self) -> None:
        """Sinon la série de croisement ne serait pas nécessaire."""
        self.assertIsNone(ae.detect_cross(goldens.documented_candles()))

    def test_last_candle_carries_the_amplitude_and_a_real_spread(self) -> None:
        candles = goldens.documented_breakout_candles("up")
        closes = [c["close"] for c in candles]
        self.assertEqual(100.0, closes[-2])
        self.assertEqual(100.0 + goldens.BREAKOUT_AMPLITUDE, closes[-1])
        self.assertTrue(all(c["high"] > c["low"] for c in candles), "ATR dégénéré")

    def test_unknown_direction_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            goldens.documented_breakout_candles("sideways")


class DriftDetectionTest(unittest.TestCase):
    """Le cœur : distinguer « à jour » de « en dérive », et le dire."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = pathlib.Path(self._tmp.name)

    def test_matching_value_passes(self) -> None:
        dataset = _synthetic(value=42)
        goldens.write_golden(dataset, self.directory)
        outcome = goldens.check(dataset, self.directory)
        self.assertEqual("ok", outcome.status, outcome.message)
        self.assertTrue(outcome.ok)

    def test_changed_value_is_reported_with_a_diff(self) -> None:
        goldens.write_golden(_synthetic(value=1), self.directory)
        outcome = goldens.check(_synthetic(value=2), self.directory)
        self.assertEqual("changed", outcome.status)
        self.assertFalse(outcome.ok)
        self.assertIn("la valeur de référence a changé", outcome.message)
        self.assertIn("+++", outcome.message)
        self.assertIn('+    "value": 2', outcome.message)
        self.assertIn('-    "value": 1', outcome.message)

    def test_message_explains_how_to_regenerate(self) -> None:
        goldens.write_golden(_synthetic(value=1), self.directory)
        message = goldens.check(_synthetic(value=2), self.directory).message
        self.assertIn("python scripts/goldens.py --update synthetic", message)
        self.assertIn("synthetic.json", message)
        self.assertIn("MÊME commit", message)

    def test_missing_file_fails_and_explains(self) -> None:
        """Un golden absent ne doit pas passer au vert « faute de référence »."""
        outcome = goldens.check(_synthetic(), self.directory)
        self.assertEqual("error", outcome.status)
        self.assertIn("fichier absent", outcome.message)
        self.assertIn("python scripts/goldens.py --update synthetic", outcome.message)

    def test_unreadable_file_fails(self) -> None:
        path = self.directory / "synthetic.json"
        path.write_text("{ pas du JSON", encoding="utf-8")
        outcome = goldens.check(_synthetic(), self.directory)
        self.assertEqual("error", outcome.status)
        self.assertIn("illisible", outcome.message)

    def test_file_without_values_fails(self) -> None:
        path = self.directory / "synthetic.json"
        path.write_text(json.dumps({"dataset": "synthetic"}), encoding="utf-8")
        outcome = goldens.check(_synthetic(), self.directory)
        self.assertEqual("error", outcome.status)
        self.assertIn("values", outcome.message)

    def test_file_for_another_dataset_fails(self) -> None:
        """Un fichier copié sous le mauvais nom comparerait deux jeux différents."""
        path = self.directory / "synthetic.json"
        path.write_text(
            goldens.render(_synthetic(name="autre"), {"value": 1}), encoding="utf-8"
        )
        outcome = goldens.check(_synthetic(), self.directory)
        self.assertEqual("error", outcome.status)
        self.assertIn("autre", outcome.message)

    def test_unavailable_dataset_is_skipped_loudly(self) -> None:
        """FastAPI absent : contrôle non exécuté, annoncé — jamais un vert muet."""

        def build():
            raise goldens.Unavailable("dépendance absente")

        unavailable = goldens.Dataset("absent", "jeu indisponible", build)
        outcome = goldens.check(unavailable, self.directory)
        self.assertEqual("skip", outcome.status)
        self.assertTrue(outcome.ok)
        self.assertIn("ignoré", outcome.message)

    def test_failing_extraction_is_not_a_pass(self) -> None:
        def build():
            raise RuntimeError("boum")

        dataset = goldens.Dataset("casse", "jeu qui échoue", build)
        goldens.write_golden(_synthetic(value=1), self.directory)  # crée le dossier
        (self.directory / "casse.json").write_text(
            goldens.render(dataset, {"value": 1}), encoding="utf-8"
        )
        outcome = goldens.check(dataset, self.directory)
        self.assertEqual("error", outcome.status)
        self.assertIn("boum", outcome.message)
        self.assertIn("--update casse", outcome.message)

    def test_update_twice_writes_the_same_bytes(self) -> None:
        dataset = _synthetic(value=7)
        first = goldens.write_golden(dataset, self.directory).read_bytes()
        second = goldens.write_golden(dataset, self.directory).read_bytes()
        self.assertEqual(first, second)
        self.assertNotIn(b"\r\n", first, "les goldens doivent rester en LF")

    def test_update_is_written_with_a_trailing_newline(self) -> None:
        raw = goldens.write_golden(_synthetic(), self.directory).read_bytes()
        self.assertTrue(raw.endswith(b"\n"))


class CliTest(unittest.TestCase):
    """La ligne de commande : ce que la CI et un développeur exécutent."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = pathlib.Path(self._tmp.name)
        self.dataset = _synthetic(value=5)
        patcher = mock.patch.object(goldens, "DATASETS", (self.dataset,))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = goldens.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_check_fails_on_a_missing_golden(self) -> None:
        code, output, _ = self._run(["--dir", str(self.directory)])
        self.assertEqual(1, code)
        self.assertIn("python scripts/goldens.py --update synthetic", output)

    def test_update_then_check_passes(self) -> None:
        code, output, _ = self._run(["--update", "--dir", str(self.directory)])
        self.assertEqual(0, code)
        self.assertIn("synthetic.json", output)
        code, output, _ = self._run(["--dir", str(self.directory)])
        self.assertEqual(0, code)
        self.assertIn("[OK] synthetic", output)

    def test_unknown_dataset_is_a_usage_error_without_touching_the_disk(self) -> None:
        code, _, err = self._run(["--update", "inconnu", "--dir", str(self.directory)])
        self.assertEqual(2, code)
        self.assertIn("inconnu", err)
        self.assertEqual([], list(self.directory.iterdir()))

    def test_list_names_every_dataset(self) -> None:
        code, output, _ = self._run(["--list"])
        self.assertEqual(0, code)
        self.assertIn("synthetic", output)


class CiWiringTest(unittest.TestCase):
    """Un contrôle que la CI n'exécute pas ne protège personne."""

    def test_ci_runs_the_golden_check(self) -> None:
        text = CI_PATH.read_text(encoding="utf-8")
        self.assertIn("python scripts/goldens.py", text)
        self.assertIn("golden", text.lower())

    def test_ci_references_an_existing_script(self) -> None:
        self.assertTrue(SCRIPT_PATH.is_file(), f"script absent : {SCRIPT_PATH}")
        self.assertIn(SCRIPT_PATH.name, CI_PATH.read_text(encoding="utf-8"))

    def test_golden_files_are_versioned(self) -> None:
        """Un golden exclu par `.gitignore` ne serait jamais commité ni comparé."""
        patterns = [
            line.strip()
            for line in GITIGNORE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith(("#", "!"))
        ]
        for dataset in goldens.DATASETS:
            relative = goldens.golden_path(dataset).relative_to(REPO_ROOT).as_posix()
            with self.subTest(golden=relative):
                self.assertTrue(goldens.golden_path(dataset).is_file())
                self.assertFalse(_gitignored(relative, patterns), f"{relative} est ignoré par git")


def _gitignored(relative: str, patterns) -> bool:
    parts = pathlib.PurePosixPath(relative).parts
    candidates = ["/".join(parts[: index + 1]) for index in range(len(parts))]
    candidates.append(pathlib.PurePosixPath(relative).name)
    for raw in patterns:
        pattern = raw.lstrip("/").rstrip("/")
        if any(fnmatch.fnmatch(candidate, pattern) for candidate in candidates):
            return True
    return False


if __name__ == "__main__":
    unittest.main()
