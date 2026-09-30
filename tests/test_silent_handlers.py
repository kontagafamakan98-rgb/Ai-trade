"""Le corpus des `except` : aucun ne se tait sans le dire.

Un `except` qui avale une panne sans rien en dire est le mode de panne le plus
cher du projet : il transforme une erreur en succès apparent, et rien ne le
signale ensuite. Ce test lit **tout** le code versionné — application *et*
tests — et refuse tout handler qui n'émet **aucun** signal.

Ce qu'est un signal, au sens de ce corpus : de quoi être vu par quelqu'un
d'autre que la ligne fautive — un `raise`, un `return`, un `yield`, un `await`,
un `assert`, ou un **appel** (imprimer, journaliser, avertir, noter un motif…).
Deux échappatoires, et deux seulement :

* **nommer la raison dans le corps** : un handler qui capture l'exception
  (`as exc`) et **s'en sert** garde la cause — il n'est donc pas muet, même s'il
  n'imprime rien ;
* **porter une justification déclarée** : un commentaire `# sans signal : <raison>`
  sur la ligne du `except` ou dans son corps. La raison doit être écrite : un
  marqueur vide ne vaut pas mieux qu'un silence.

Le corpus lit l'arbre réel (`.venv`, `.pgtest` et les caches exclus) et refuse
d'être vide : un fichier illisible, ou un parcours qui ne trouverait plus rien,
fait échouer le test plutôt que de le faire passer à vide. Un corpus qui ne lit
pas tout ne prouve rien sur ce qu'il n'a pas lu.
"""

from __future__ import annotations

import ast
import pathlib
import textwrap
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: Répertoires jamais lus : dépendances, caches, artefacts, et `.pgtest` (non
#: versionné, absent en CI). Le corpus doit lire le même arbre partout.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".pgtest",
        ".gradle",
        "build",
        "dist",
        "out",
        "target",
        "node_modules",
        "artifacts",
        ".freebuff",
        ".idea",
        ".vscode",
    }
)

#: Le mot qu'un handler sans signal doit porter, suivi de sa raison **sur la même
#: ligne**. Choisi pour être greppable et ne pas apparaître par accident.
MARKER = "sans signal"

#: Longueur minimale de la raison : « sans signal : ok » n'est pas une raison.
MIN_REASON = 10

#: Plancher de lecture : si le parcours se casse, le corpus doit échouer, pas
#: passer à vide. Constaté : plus de 300 handlers sur le dépôt entier.
MIN_HANDLERS = 100


def source_files() -> list[pathlib.Path]:
    """Tous les `.py` versionnés, caches et dépendances exclus."""
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        parts = path.relative_to(ROOT).parts[:-1]
        if any(part in SKIP_DIRS for part in parts):
            continue
        found.append(path)
    return found


def handlers(source: str):
    """Les handlers `except` d'une source, `except*` compris."""
    tree = ast.parse(source)
    try_star = getattr(ast, "TryStar", ast.Try)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Try, try_star)):
            yield from node.handlers


def _observable(stmt: ast.stmt) -> bool:
    """Vrai si l'instruction est de quoi être vu par quelqu'un d'autre."""
    if isinstance(stmt, (ast.Raise, ast.Return, ast.Assert)):
        return True
    return any(
        isinstance(node, (ast.Call, ast.Yield, ast.YieldFrom, ast.Await))
        for node in ast.walk(stmt)
    )


def _uses_bound_name(handler: ast.ExceptHandler) -> bool:
    """Vrai si le handler nomme l'exception (`as exc`) et s'en sert."""
    name = handler.name
    if not name:
        return False
    return any(
        isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
        for node in ast.walk(handler)
    )


def is_silent(handler: ast.ExceptHandler) -> bool:
    """Un handler qui n'émet aucun signal et ne garde même pas la cause capturée."""
    if any(_observable(stmt) for stmt in handler.body):
        return False
    return not _uses_bound_name(handler)


def declared_reason(lines: list[str], handler: ast.ExceptHandler) -> bool:
    """Vrai si le handler porte `# sans signal : <raison>` dans son corps."""
    end = getattr(handler, "end_lineno", handler.lineno)
    for raw in lines[handler.lineno - 1 : end]:
        index = raw.lower().find(MARKER)
        if index == -1:
            continue
        reason = raw[index + len(MARKER) :].strip(" \t:-—")
        if len(reason) >= MIN_REASON:
            return True
    return False


def undeclared_silences(source: str) -> list[int]:
    """Les lignes des handlers sans signal qui ne déclarent aucune raison."""
    lines = source.splitlines()
    return [
        handler.lineno
        for handler in handlers(source)
        if is_silent(handler) and not declared_reason(lines, handler)
    ]


def _source(body: str) -> str:
    return textwrap.dedent(body).lstrip("\n")


class DetectorTest(unittest.TestCase):
    """Ce que le détecteur voit — sur des sources fabriquées, pas sur le dépôt."""

    def test_a_bare_pass_is_silent(self):
        self.assertEqual(undeclared_silences(_source("""
            try:
                run()
            except Exception:
                pass
            """)), [3])

    def test_an_empty_handler_is_silent(self):
        self.assertEqual(undeclared_silences(_source("""
            try:
                run()
            except Exception:
                continue
            """)), [3])

    def test_a_call_is_a_signal(self):
        self.assertEqual(undeclared_silences(_source("""
            try:
                run()
            except Exception:
                print("échec")
            """)), [])

    def test_a_raise_or_return_is_a_signal(self):
        self.assertEqual(
            undeclared_silences(
                _source("""
                    try:
                        run()
                    except ValueError:
                        raise
                    except KeyError:
                        return None
                    """)
            ),
            [],
        )

    def test_a_sentinel_assignment_alone_is_still_silent(self):
        """Poser `ALPACA_OK = False` ne nomme pas la cause : ça se déclare."""
        self.assertEqual(undeclared_silences(_source("""
            try:
                import alpaca
            except Exception:
                ALPACA_OK = False
            """)), [3])

    def test_using_the_bound_exception_is_a_signal(self):
        self.assertEqual(undeclared_silences(_source("""
            try:
                run()
            except Exception as exc:
                reason = str(exc)
            """)), [])

    def test_binding_the_exception_without_using_it_stays_silent(self):
        self.assertEqual(undeclared_silences(_source("""
            try:
                run()
            except Exception as exc:
                result = None
            """)), [3])

    def test_a_declared_reason_clears_the_handler(self):
        self.assertEqual(undeclared_silences(_source("""
            try:
                import httpx
            except ImportError:  # sans signal : dépendance optionnelle, sondée ailleurs
                httpx = None
            """)), [])

    def test_a_declared_reason_can_live_inside_the_body(self):
        self.assertEqual(undeclared_silences(_source("""
            try:
                run()
            except OSError:
                # sans signal : permission absente sous Windows, sans conséquence
                pass
            """)), [])

    def test_an_empty_reason_does_not_clear_the_handler(self):
        self.assertEqual(undeclared_silences(_source("""
            try:
                run()
            except Exception:
                pass
            # sans signal :
            """)), [3])

    def test_a_reason_too_short_does_not_clear_the_handler(self):
        self.assertEqual(undeclared_silences(_source("""
            try:
                run()
            except Exception:  # sans signal : ok
                pass
            """)), [3])

    def test_a_marker_outside_the_handler_does_not_clear_it(self):
        """La déclaration est locale au handler : elle ne couvre pas le voisin."""
        self.assertEqual(undeclared_silences(_source("""
            # sans signal : garde d'import optionnelle, annoncée ailleurs
            try:
                first()
            except ImportError:
                pass
            try:
                second()
            except ImportError:
                pass
            """)), [4, 8])

    def test_except_star_handlers_are_read_too(self):
        self.assertEqual(undeclared_silences(_source("""
            try:
                run()
            except* ValueError:
                pass
            """)), [3])


class CorpusTest(unittest.TestCase):
    """Le dépôt entier : aucun handler muet qui ne se déclare pas."""

    @classmethod
    def setUpClass(cls):
        cls.files = source_files()

    def test_the_corpus_reads_a_plausible_repository(self):
        """Un parcours cassé passerait à vide : on refuse le vide explicitement."""
        self.assertIn(ROOT / "main.py", self.files)
        total = sum(len(list(handlers(path.read_text(encoding="utf-8")))) for path in self.files)
        self.assertGreaterEqual(
            total, MIN_HANDLERS, f"seulement {total} handler(s) lu(s) dans {len(self.files)} fichier(s)"
        )

    def test_no_handler_is_silent_without_a_declared_reason(self):
        offenders = []
        for path in self.files:
            for lineno in undeclared_silences(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
        self.assertEqual(
            offenders,
            [],
            "handler(s) `except` sans signal et sans justification :\n"
            + "\n".join(f"  - {where}" for where in offenders)
            + "\nAjoute un appel qui nomme l'échec, ou un commentaire "
            f"« # {MARKER} : <raison> » sur le `except` ou dans son corps.",
        )


if __name__ == "__main__":
    unittest.main()
