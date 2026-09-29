"""« La CI peut réellement collecter les tests. »

Le job CI lance `pytest` — le **script console** — et non `python -m pytest`. Or
le script console n'ajoute PAS le répertoire courant à `sys.path` : les modules
de test qui font `from core import …` (et `from tests.hook_support import …`)
échouent alors à l'import par `ModuleNotFoundError: No module named 'core'` —
erreur de collecte constatée en pratique, qui rend tout le job rouge. D'où
`pythonpath = ["."]` dans `pyproject.toml`, verrouillé ici.
"""

import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
TESTS_DIR = REPO_ROOT / "tests"


class PytestCollectionConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))

    def ini_options(self) -> dict:
        return self.config["tool"]["pytest"]["ini_options"]

    def test_repo_root_is_on_the_pythonpath(self) -> None:
        self.assertIn(
            ".",
            self.ini_options().get("pythonpath", []),
            'sans `pythonpath = ["."]`, le script console `pytest` ne peut pas '
            "importer `core`/`tests` et la collecte échoue",
        )

    def test_testpaths_points_at_tests(self) -> None:
        self.assertEqual(self.ini_options().get("testpaths"), ["tests"])

    def test_the_requirement_is_not_vacuous(self) -> None:
        """Au moins un test importe un paquet racine : sinon `pythonpath` serait inutile."""
        importers = [
            path.name
            for path in TESTS_DIR.glob("test_*.py")
            if "from core import" in path.read_text(encoding="utf-8")
        ]
        self.assertTrue(importers, "aucun test n'importe un paquet racine (`from core import`)")


if __name__ == "__main__":
    unittest.main()
