"""Sortie console des scripts de contrôle (`core/console.py`).

Le défaut réparé ici n'est pas esthétique, et il n'arrive pas au hasard : il tombe
au **verdict**. Sur une sortie `cp1252` — le cas de tout terminal Windows qui n'est
pas une console Win32 native (Git Bash, mintty, un tube, une redirection) — écrire
« ✅ » lève un `UnicodeEncodeError`, le contrôle a travaillé pour rien, et le message
affiché parle d'encodage au lieu de parler du dépôt.

Ce qui est éprouvé tient donc en deux parties :

* **le rapport reste intact** : après l'appel, un flux `cp1252` accepte « ✅ », « → »
  et « é » et ce qui sort se relit en UTF-8 — pas un « ? » à la place du symbole, ce
  qui serait un rapport silencieusement amputé ;
* **rien ne peut échouer** : un flux sans `reconfigure` (capture de test), un flux
  refusant la reconfiguration (détaché), deux appels d'affilée — aucun de ces cas ne
  laisse remonter d'exception, sinon le remède deviendrait la panne.

Le second volet est un **contrat sur les scripts eux-mêmes** : chaque script de
`scripts/` qui écrit un rapport règle sa sortie en tête de son `main`, et aucun ne
recopie le pansement. Trois scripts l'avaient recopié, sous trois formes différentes
— c'est ce qui a motivé `core/console.py`, et c'est ce que ce contrat empêche de
recommencer. Il lit l'**arbre** du module, pas des sous-chaînes : `reconfigure(`
apparaît dans une docstring, un `main` peut appeler une fonction d'un autre nom.
"""
from __future__ import annotations

import ast
import io
import pathlib
import sys
import unittest
from unittest import mock

from core import console

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

#: Sans `main` : c'est un module de la sonde, importé par `check_schema_drift.py`.
#: Une exemption se justifie, et se vérifie juste en dessous (il n'écrit rien).
LIBRARY_ONLY = {"schema_snapshot.py"}

#: Le symbole qui a fait lever le premier rapport, et les deux qui suivent partout.
VERDICT = "✅ verdict → é"


def _console_stream() -> "tuple[io.TextIOWrapper, io.BytesIO]":
    """Un `sys.stdout` tel qu'on le trouve hors console native, et son tampon."""
    sink = io.BytesIO()
    return io.TextIOWrapper(sink, encoding="cp1252", errors="surrogateescape"), sink


class StreamsTest(unittest.TestCase):
    """Ce que `make_streams_utf8` garantit à l'appelant, et rien de plus."""

    def test_the_report_survives_a_cp1252_stream(self) -> None:
        """Sans l'appel, le verdict lèverait : c'est le défaut, reproduit ici."""
        broken, _sink = _console_stream()
        with mock.patch.object(sys, "stdout", broken):
            with self.assertRaises(UnicodeEncodeError):
                print(VERDICT)

        stdout, sink = _console_stream()
        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(console.make_streams_utf8(), 1)
            print(VERDICT)
            stdout.flush()  # un `TextIOWrapper` garde l'écriture en tampon

        self.assertEqual(sink.getvalue().decode("utf-8").strip(), VERDICT)

    def test_both_streams_are_repaired(self) -> None:
        """Le verdict peut sortir sur `stderr` (erreur d'usage) : les deux comptent."""
        stdout, out_sink = _console_stream()
        stderr, err_sink = _console_stream()

        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr):
            self.assertEqual(console.make_streams_utf8(), 2)
            print(VERDICT)
            print(VERDICT, file=sys.stderr)
            stdout.flush()
            stderr.flush()

        self.assertEqual(stdout.encoding, console.OUTPUT_ENCODING)
        self.assertEqual(stderr.errors, console.OUTPUT_ERRORS)
        self.assertEqual(out_sink.getvalue(), err_sink.getvalue())

    def test_a_stream_without_reconfigure_is_left_alone(self) -> None:
        """La capture d'un test, un objet maison : on ne les remplace pas."""
        with mock.patch.object(sys, "stdout", io.StringIO()):
            with mock.patch.object(sys, "stderr", io.StringIO()):
                self.assertEqual(console.make_streams_utf8(), 0)

    def test_a_detached_stream_does_not_break_the_report(self) -> None:
        """Un flux qui refuse la reconfiguration ne fait pas échouer le script."""
        detached, _sink = _console_stream()
        detached.detach()
        with mock.patch.object(sys, "stdout", detached):
            with mock.patch.object(sys, "stderr", io.StringIO()):
                self.assertEqual(console.make_streams_utf8(), 0)

    def test_calling_it_twice_is_harmless(self) -> None:
        """Les scripts l'appellent une fois, mais un import partagé peut la doubler."""
        stdout, _sink = _console_stream()
        stderr, _sink2 = _console_stream()

        with mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr):
            self.assertEqual(console.make_streams_utf8(), 2)
            self.assertEqual(console.make_streams_utf8(), 2)

    def test_the_encoding_is_utf8_and_the_errors_are_replaced(self) -> None:
        """Deux filets distincts : l'encodage fait le travail, le remplacement pardonne."""
        self.assertEqual(console.OUTPUT_ENCODING, "utf-8")
        self.assertEqual(console.OUTPUT_ERRORS, "replace")


def _mains(path: pathlib.Path) -> "list[ast.FunctionDef]":
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    ]


def _body(main: ast.FunctionDef) -> "list[ast.stmt]":
    body = list(main.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        return body[1:]  # la docstring ne compte pas
    return body


def _calls(node: ast.AST, name: str) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and getattr(child.func, "attr", None) == name:
            return True
    return False


def _writes(node: ast.AST) -> bool:
    """Un `print`, un `write` ou un `flush` : de la sortie, quelle qu'elle soit."""
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name) and func.id == "print":
            return True
        if isinstance(func, ast.Attribute) and func.attr in {"write", "flush"}:
            return True
    return False


class ScriptContractTest(unittest.TestCase):
    """Chaque script de contrôle répare sa sortie **avant** d'écrire son verdict."""

    def _scripts(self) -> "list[pathlib.Path]":
        return sorted(SCRIPTS.glob("*.py"))

    def test_every_reporting_script_repairs_its_output_first(self) -> None:
        for path in self._scripts():
            mains = _mains(path)
            if not mains:
                continue
            with self.subTest(script=path.name):
                body = _body(mains[0])
                repairs = [i for i, stmt in enumerate(body) if _calls(stmt, "make_streams_utf8")]
                self.assertTrue(
                    repairs,
                    f"{path.name} écrit un rapport sans régler sa sortie "
                    "(`console.make_streams_utf8()` manque)",
                )
                self.assertLessEqual(repairs[0], 1, f"{path.name} : l'appel doit être en tête de `main`")
                writes = [i for i, stmt in enumerate(body) if _writes(stmt)]
                self.assertTrue(
                    not writes or repairs[0] < writes[0],
                    f"{path.name} écrit avant d'avoir réglé sa sortie",
                )

    def test_a_script_without_main_is_exempted_explicitly(self) -> None:
        """Une exemption est un mot dans une liste, pas un silence — et elle se paie."""
        for path in self._scripts():
            if _mains(path):
                continue
            with self.subTest(script=path.name):
                self.assertIn(path.name, LIBRARY_ONLY, f"{path.name} : script sans `main`, hors exemption")
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("print(", source, "un module importé ne compose pas de rapport")
                self.assertNotIn("sys.stdout.write(", source)

    def test_no_script_copies_the_repair(self) -> None:
        """Trois scripts l'avaient recopié : c'est `core/console.py` qui le porte."""
        for path in self._scripts():
            source = path.read_text(encoding="utf-8")
            with self.subTest(script=path.name):
                self.assertNotIn("sys.stdout.reconfigure(", source)
                self.assertNotIn("sys.stderr.reconfigure(", source)
                self.assertNotIn("_make_streams_utf8", source)

    def test_the_scripts_that_carried_the_bandage_now_share_it(self) -> None:
        """Ces trois-là l'avaient recopié sous trois formes : qu'ils appellent le module."""
        for name in ("verify_secrets.py", "pre_commit_secrets.py", "calibrate_similarity.py"):
            source = (SCRIPTS / name).read_text(encoding="utf-8")
            with self.subTest(script=name):
                self.assertIn("console.make_streams_utf8()", source)


if __name__ == "__main__":
    unittest.main()
