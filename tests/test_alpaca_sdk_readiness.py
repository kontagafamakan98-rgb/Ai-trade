"""Le préflight dit-il si le SDK Alpaca est **vraiment** installé ?

`alpaca_shared.ready` ne lisait que les clés : sur une machine où `alpaca-py`
n'est pas installé — donc là où `get_alpaca_client` refuse et où **aucun ordre ne
peut partir** — le préflight publiait « prêt ». Les deux causes se réparent à des
endroits différents (`pip install` d'un côté, `.env` de l'autre) : elles sont
maintenant publiées séparément, et `ready` est leur ET.

La sonde est éprouvée ici comme celle du repli local (`tests/test_media_extractor.py`) :
absence, présence, recherche cassée, et une question à laquelle on ne répond pas
en important le paquet. Ce dernier point n'est pas du style : `/health` appelle
cette sonde, et `execution.order_executor` coûte plus de trois cents modules —
c'est précisément pour ça que `execution/alpaca_sdk.py` existe.

Elle est enfin comparée à `ALPACA_OK`, la réponse qui fait foi à l'exécution : le
jour où les deux divergeront, c'est ici que ça se verra.
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import sys
import unittest
from unittest import mock

from core import config_runtime
from execution import alpaca_sdk as asdk
from execution import order_executor as oe

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


class AlpacaSdkProbeTest(unittest.TestCase):
    """La sonde elle-même : `find_spec`, et un non plutôt qu'une exception."""

    def test_an_absent_package_is_unavailable(self):
        with mock.patch.object(asdk.importlib.util, "find_spec", return_value=None):
            self.assertFalse(asdk.alpaca_sdk_available())

    def test_a_present_package_is_available(self):
        with mock.patch.object(asdk.importlib.util, "find_spec", return_value=object()):
            self.assertTrue(asdk.alpaca_sdk_available())

    def test_a_broken_lookup_is_a_no_not_an_exception(self):
        """Paquet parent absent, chemin exotique : deux pannes, un seul verdict."""
        for error in (ImportError("paquet parent absent"), ValueError("chemin exotique")):
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(asdk.importlib.util, "find_spec", side_effect=error):
                    self.assertFalse(asdk.alpaca_sdk_available())

    def test_answering_does_not_import_the_package(self):
        """« Es-tu là ? » ne doit pas charger le SDK — un import se verrait ici."""
        loaded = [
            name
            for name in sys.modules
            if name == asdk.ALPACA_SDK_PACKAGE or name.startswith(asdk.ALPACA_SDK_PACKAGE + ".")
        ]
        if loaded:
            self.skipTest(f"{loaded[0]} est déjà chargé : l'import ne serait pas dû à la sonde")
        before = set(sys.modules)
        with mock.patch.object(asdk.importlib.util, "find_spec", return_value=object()):
            self.assertTrue(asdk.alpaca_sdk_available())
        self.assertEqual(sorted(set(sys.modules) - before), [], "la sonde a importé quelque chose")

    def test_the_probe_looks_for_the_package_the_executor_imports(self):
        """Le nom du paquet vit dans la sonde : un renommage doit la faire bouger."""
        source = (REPO_ROOT / "execution" / "order_executor.py").read_text(encoding="utf-8")
        self.assertIn(f"from {asdk.ALPACA_SDK_PACKAGE}.", source)

    def test_the_light_probe_agrees_with_the_executor(self):
        """Deux façons de poser la question, une seule réponse — par construction.

        La sonde cherche le paquet, `ALPACA_OK` importe les quatre noms du client :
        elles ne peuvent différer que sur un paquet cassé, et ce jour-là il faut le
        savoir plutôt que de publier un préflight et un exécuteur qui se contredisent.
        """
        self.assertEqual(oe.ALPACA_OK, asdk.alpaca_sdk_available())


class AlpacaSharedAccountTest(unittest.TestCase):
    """Ce que publie `/preflight` : deux causes, deux champs, un seul `ready`."""

    KEYS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")

    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in self.KEYS}
        for key in self.KEYS:
            os.environ.pop(key, None)
        config_runtime.reset_env_config()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_runtime.reset_env_config()

    def _config(self):
        config_runtime.reset_env_config()
        return config_runtime.get_env_config()

    def _with_keys(self) -> None:
        for key in self.KEYS:
            os.environ[key] = f"{key.lower()}-partage"

    @contextlib.contextmanager
    def _sdk(self, installed: bool):
        """Le SDK, répondu sans toucher à l'interpréteur qui exécute la suite."""
        with mock.patch.object(config_runtime, "alpaca_sdk_ready", return_value=installed):
            yield

    def _published(self) -> dict:
        return self._config().component_health()["alpaca_shared"]

    def test_the_shared_account_is_not_ready_without_the_sdk(self):
        """Le défaut d'origine : deux clés suffisaient à publier « prêt »."""
        self._with_keys()
        with self._sdk(False):
            component = self._published()
        self.assertFalse(component["ready"], "aucun ordre ne peut partir sans le SDK")
        self.assertTrue(component["keys_present"], "les clés sont là : c'est la *cause* qui change")
        self.assertFalse(component["sdk_installed"])

    def test_the_shared_account_is_not_ready_without_keys(self):
        """L'autre cause, qui n'est pas effacée par un SDK bien installé."""
        with self._sdk(True):
            component = self._published()
        self.assertFalse(component["ready"])
        self.assertFalse(component["keys_present"])
        self.assertTrue(component["sdk_installed"])

    def test_the_two_causes_together_are_the_only_ready(self):
        self._with_keys()
        with self._sdk(True):
            component = self._published()
        self.assertTrue(component["ready"])
        self.assertEqual(component["paper_only"], True, "le compte partagé reste paper")

    def test_the_preflight_publishes_what_the_component_says(self):
        self._with_keys()
        with self._sdk(False):
            published = self._config().preflight()["components"]["alpaca_shared"]
            expected = self._published()
        self.assertEqual(published, expected)

    def test_without_patching_the_verdict_follows_this_interpreter(self):
        """Pas de doublure : le préflight dit l'état réel de **cette** machine."""
        self._with_keys()
        component = self._published()
        self.assertEqual(component["sdk_installed"], asdk.alpaca_sdk_available())
        self.assertEqual(component["ready"], component["keys_present"] and component["sdk_installed"])


if __name__ == "__main__":
    unittest.main()
