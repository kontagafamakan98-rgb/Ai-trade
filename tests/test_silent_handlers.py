"""Le corpus des `except` : aucun ne se tait sans le dire, ni sans l'enregistrer.

Un `except` qui avale une panne sans rien en dire est le mode de panne le plus
cher du projet : il transforme une erreur en succès apparent, et rien ne le
signale ensuite. Le contrôle vit dans ``scripts/check_silent_handlers.py``, et ce
fichier l'éprouve — plus les deux choses que la CI exige : qu'il soit branché, et
que l'inventaire versionné des exemptions soit à jour.

Ce qu'est un signal, au sens de ce contrôle : de quoi être vu par quelqu'un
d'autre que la ligne fautive — un `raise`, un `return`, un `yield`, un `await`,
un `assert`, ou un **appel** (imprimer, journaliser, avertir, noter un motif…).
Deux échappatoires, et deux seulement :

* **nommer la raison dans le corps** : un handler qui capture l'exception
  (`as exc`) et **s'en sert** garde la cause — il n'est donc pas muet, même s'il
  n'imprime rien ;
* **porter une justification déclarée, et l'enregistrer** : un commentaire
  ``# sans signal : <raison>`` nomme la raison **dans le code**, et l'inventaire
  versionné ``tests/silent_exceptions.json`` la **retient**. Le contrôle ne croit
  pas le seul commentaire : une exemption n'existe que si elle est enregistrée, et
  l'enregistrement doit correspondre exactement au code.

Ce que ces tests verrouillent, en quatre familles :

* **le détecteur**, sur des sources fabriquées : `pass`, `continue`, sentinelle
  seule, `as exc` utilisé ou non, marqueur vide, trop court ou hors du handler,
  `except*` ;
* **l'inventaire**, sur des sources fabriquées aussi : ce qu'une exemption
  retient (fichier, symbole, clause, raison), ce qu'elle ne retient pas, et ce
  que la régénération écrit (LF, trié, marqué « ne pas éditer ») ;
* **le contrôle**, sur des arbres jetables : un silencieux non déclaré est
  refusé, un repli déclaré ne passe qu'une fois enregistré, un enregistrement
  périmé est une dérive, un inventaire absent ou illisible est refusé, un arbre
  lu à moitié aussi (fichier illisible, syntaxe cassée, corpus trop pauvre) ;
* **le câblage**, sur le dépôt réel : l'étape CI existe et tourne **avant**
  l'installation des dépendances, l'outil est documenté, l'inventaire est
  versionné, et le dépôt entier passe.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import re
import tempfile
import textwrap
import unittest

from scripts import check_silent_handlers as csh

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
REAL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
INVENTORY = REPO_ROOT / "tests" / "silent_exceptions.json"
DOC = REPO_ROOT / "docs" / "EXCEPTIONS.md"

#: Une ligne de triage : `fichier` :: `symbole` :: `clause` :: pourquoi. Les
#: trois premières valeurs sont vérifiées contre l'inventaire ; la raison est de
#: la prose, et n'est pas comparée.
TRIAGE_LINE = re.compile(r"^- `([^`]+)` :: `([^`]+)` :: `([^`]+)` :: (.+)$", re.MULTILINE)

#: Les quatre régimes tolérés, nommés dans le triage : la frontière se lit là.
TRIAGE_REGIMES = ("Absence publiée", "Repli sans mesure", "Variante essayée", "Entrée écartée")

#: Un arbre fabriqué qui déclare un repli : le socle des tests de l'inventaire.
DECLARED = """
    try:
        import httpx
    except ImportError:  # sans signal : dépendance optionnelle, sondée ailleurs
        httpx = None
    """

#: Un arbre fabriqué qui avale, sans rien dire : ce que le contrôle doit refuser.
UNDECLARED = """
    try:
        import httpx
    except ImportError:
        httpx = None
    """


def _source(body: str) -> str:
    return textwrap.dedent(body).lstrip("\n")


class DetectorTest(unittest.TestCase):
    """Ce que le détecteur voit — sur des sources fabriquées, pas sur le dépôt."""

    def test_a_bare_pass_is_silent(self):
        self.assertEqual(csh.undeclared_silences(_source("""
            try:
                run()
            except Exception:
                pass
            """)), [3])

    def test_an_empty_handler_is_silent(self):
        self.assertEqual(csh.undeclared_silences(_source("""
            try:
                run()
            except Exception:
                continue
            """)), [3])

    def test_a_call_is_a_signal(self):
        self.assertEqual(csh.undeclared_silences(_source("""
            try:
                run()
            except Exception:
                print("échec")
            """)), [])

    def test_a_raise_or_return_is_a_signal(self):
        self.assertEqual(
            csh.undeclared_silences(
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
        self.assertEqual(csh.undeclared_silences(_source("""
            try:
                import alpaca
            except Exception:
                ALPACA_OK = False
            """)), [3])

    def test_using_the_bound_exception_is_a_signal(self):
        self.assertEqual(csh.undeclared_silences(_source("""
            try:
                run()
            except Exception as exc:
                reason = str(exc)
            """)), [])

    def test_binding_the_exception_without_using_it_stays_silent(self):
        self.assertEqual(csh.undeclared_silences(_source("""
            try:
                run()
            except Exception as exc:
                result = None
            """)), [3])

    def test_a_declared_reason_clears_the_handler(self):
        self.assertEqual(csh.undeclared_silences(_source("""
            try:
                import httpx
            except ImportError:  # sans signal : dépendance optionnelle, sondée ailleurs
                httpx = None
            """)), [])

    def test_a_declared_reason_can_live_inside_the_body(self):
        self.assertEqual(csh.undeclared_silences(_source("""
            try:
                run()
            except OSError:
                # sans signal : permission absente sous Windows, sans conséquence
                pass
            """)), [])

    def test_an_empty_reason_does_not_clear_the_handler(self):
        self.assertEqual(csh.undeclared_silences(_source("""
            try:
                run()
            except Exception:
                pass
            # sans signal :
            """)), [3])

    def test_a_reason_too_short_does_not_clear_the_handler(self):
        self.assertEqual(csh.undeclared_silences(_source("""
            try:
                run()
            except Exception:  # sans signal : ok
                pass
            """)), [3])

    def test_a_marker_outside_the_handler_does_not_clear_it(self):
        """La déclaration est locale au handler : elle ne couvre pas le voisin."""
        self.assertEqual(csh.undeclared_silences(_source("""
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
        self.assertEqual(csh.undeclared_silences(_source("""
            try:
                run()
            except* ValueError:
                pass
            """)), [3])

    def test_the_clause_of_a_handler_is_written_canonically(self):
        """Deux façons d'écrire la même clause doivent donner le même libellé."""
        located = list(csh.located_handlers(_source("""
            try:
                first()
            except (TypeError, ValueError):
                pass
            try:
                second()
            except* KeyError:
                pass
            """)))
        self.assertEqual(
            [csh.handler_label(handler, star) for handler, _symbol, star in located],
            ["except (TypeError, ValueError)", "except* KeyError"],
        )

    def test_a_handler_is_located_inside_its_enclosing_symbol(self):
        located = list(csh.located_handlers(_source("""
            class Ring:
                def decrypt(self):
                    try:
                        run()
                    except Exception:
                        pass
            try:
                import httpx
            except ImportError:
                httpx = None
            """)))
        self.assertEqual(
            [(csh.handler_label(handler, star), symbol) for handler, symbol, star in located],
            [
                ("except Exception", "Ring.decrypt"),
                ("except ImportError", "<module>"),
            ],
        )


class InventoryTest(unittest.TestCase):
    """Ce que l'inventaire retient d'une source, et ce qu'il n'en dit pas."""

    def test_an_exemption_names_the_file_the_symbol_and_the_clause(self):
        entries = csh.exemptions(_source(DECLARED), "app.py")
        self.assertEqual(
            entries,
            [
                {
                    "file": "app.py",
                    "symbol": "<module>",
                    "handler": "except ImportError",
                    "reason": "dépendance optionnelle, sondée ailleurs",
                }
            ],
        )

    def test_an_undeclared_silence_is_not_an_exemption(self):
        """L'inventaire ne peut pas absoudre ce que le code ne déclare pas."""
        self.assertEqual(csh.exemptions(_source(UNDECLARED), "app.py"), [])

    def test_a_handler_that_is_not_silent_is_not_an_exemption(self):
        source = _source("""
            try:
                run()
            except Exception as exc:
                return str(exc)
            """)
        self.assertEqual(csh.exemptions(source, "app.py"), [])

    def test_two_identical_exemptions_are_two_entries(self):
        """Compter les exemptions : supprimer l'une d'elles doit se voir."""
        source = _source("""
            try:
                first()
            except OSError:  # sans signal : permission absente, sans conséquence
                pass
            try:
                second()
            except OSError:  # sans signal : permission absente, sans conséquence
                pass
            """)
        entries = csh.exemptions(source, "app.py")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0], entries[1])

    def test_the_entries_read_from_a_tree_are_sorted(self):
        """L'inventaire est déterministe : un arbre parcouru dans le désordre s'ordonne."""
        with tempfile.TemporaryDirectory(prefix="sort-") as directory:
            root = pathlib.Path(directory)
            for name in ("zeta.py", "alpha.py"):
                (root / name).write_text(textwrap.dedent(DECLARED), encoding="utf-8", newline="\n")
            harvest = csh.collect(root)
        self.assertEqual([entry["file"] for entry in harvest.entries], ["alpha.py", "zeta.py"])

    def test_the_rendered_inventory_says_it_must_not_be_edited_by_hand(self):
        text = csh.render([])
        payload = json.loads(text)
        self.assertIn("ne pas éditer à la main", payload["_comment"])
        self.assertIn(csh.UPDATE_COMMAND, payload["_comment"])
        self.assertEqual(payload["exemptions"], [])
        self.assertTrue(text.endswith("\n"))

    def test_reading_a_malformed_inventory_names_the_problem(self):
        with tempfile.TemporaryDirectory(prefix="inventory-") as directory:
            path = pathlib.Path(directory) / "inv.json"
            cases = {
                "absent": None,
                "pas du JSON": "{ ceci n'est pas du JSON",
                "pas de liste": json.dumps({"exemptions": "non"}),
                "clé manquante": json.dumps({"exemptions": [{"file": "a.py"}]}),
            }
            for label, content in cases.items():
                with self.subTest(case=label):
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_text(content, encoding="utf-8", newline="\n")
                    entries, problem = csh.read_recorded(path)
                    self.assertIsNone(entries)
                    self.assertIsNotNone(problem)

    def test_reading_a_valid_inventory_returns_its_entries(self):
        with tempfile.TemporaryDirectory(prefix="inventory-") as directory:
            path = pathlib.Path(directory) / "inv.json"
            entry = {"file": "a.py", "symbol": "<module>", "handler": "except X", "reason": "bonne raison"}
            path.write_text(csh.render([entry]), encoding="utf-8", newline="\n")
            entries, problem = csh.read_recorded(path)
            self.assertIsNone(problem)
            self.assertEqual(entries, [entry])


class GateTest(unittest.TestCase):
    """Le contrôle, vu de la ligne de commande, sur des arbres jetables."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="silent-")
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)

    def write(self, name: str, body: str, newline: str = "\n") -> pathlib.Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8", newline=newline)
        return path

    def inventory(self) -> pathlib.Path:
        return self.root / "inventaire.json"

    def run_tool(self, *arguments: str):
        output, errors = io.StringIO(), io.StringIO()
        argv = [
            "--root",
            str(self.root),
            "--inventory",
            str(self.inventory()),
            "--min-handlers",
            "0",
            *arguments,
        ]
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = csh.main(list(argv))
        return code, output.getvalue(), errors.getvalue()

    def test_a_new_silent_handler_is_refused_and_named(self):
        self.write("app.py", UNDECLARED)
        code, _, errors = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("app.py:4", errors)
        self.assertIn("sans signal", errors)

    def test_a_declared_fallback_passes_once_it_is_recorded(self):
        self.write("app.py", DECLARED)
        # Déclaré mais pas enregistré : le contrôle refuse, et dit quoi faire.
        code, _, errors = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("fichier absent", errors)
        self.assertIn(csh.UPDATE_COMMAND, errors)

        code, output, _ = self.run_tool("--update")
        self.assertEqual(code, 0, output)
        self.assertIn("1 exemption(s)", output)

        code, output, errors = self.run_tool()
        self.assertEqual(code, 0, errors + output)
        self.assertIn("aucun `except` silencieux non déclaré", output)

    def test_update_refuses_to_absolve_an_undeclared_silence(self):
        """Enregistrer ne doit pas rendre verte une panne qu'on n'a pas déclarée."""
        self.write("app.py", UNDECLARED)
        code, _, errors = self.run_tool("--update")
        self.assertEqual(code, 1)
        self.assertIn("app.py:4", errors)
        code, _, _ = self.run_tool()
        self.assertEqual(code, 1)

    def test_the_inventory_tracks_declarations_not_the_handler_body(self):
        """Réécrire le corps d'un repli sans changer sa raison ne dérange pas l'inventaire."""
        self.write("app.py", DECLARED)
        self.assertEqual(self.run_tool("--update")[0], 0)
        self.write("app.py", DECLARED.replace("httpx = None", "httpx, _ = None, 1"))
        code, _, errors = self.run_tool()
        self.assertEqual(code, 0, errors)

    def test_an_exemption_whose_handler_stopped_being_silent_is_a_drift(self):
        """Un repli réparé (il parle enfin) rend son exemption périmée : elle doit partir."""
        self.write("app.py", DECLARED)
        self.assertEqual(self.run_tool("--update")[0], 0)
        self.write("app.py", _source("""
            try:
                import httpx
            except ImportError:  # sans signal : dépendance optionnelle, sondée ailleurs
                print("absent")
            """))
        code, _, errors = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("inventaire", errors)
        self.assertIn("ne correspond plus au code", errors)

    def test_a_changed_reason_is_a_drift(self):
        self.write("app.py", DECLARED)
        self.assertEqual(self.run_tool("--update")[0], 0)
        self.write("app.py", DECLARED.replace("sondée ailleurs", "sondée beaucoup plus loin"))
        code, _, errors = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("inventaire", errors)

    def test_a_malformed_inventory_is_refused_rather_than_ignored(self):
        self.write("app.py", DECLARED)
        self.inventory().write_text("{ pas du JSON", encoding="utf-8", newline="\n")
        code, _, errors = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("JSON valide", errors)

    def test_the_json_report_carries_the_verdict(self):
        self.write("declared.py", DECLARED)
        self.assertEqual(self.run_tool("--update")[0], 0)
        # Un silence nouveau, et une raison modifiée : les deux refus à la fois.
        self.write("broken.py", UNDECLARED)
        self.write("declared.py", DECLARED.replace("sondée ailleurs", "sondée beaucoup plus loin"))
        code, output, _ = self.run_tool("--json")
        self.assertEqual(code, 1)
        report = json.loads(output)
        self.assertFalse(report["ok"])
        self.assertEqual(report["handlers"], 2)
        self.assertEqual([offender["file"] for offender in report["offenders"]], ["broken.py"])
        self.assertTrue(report["drift"])
        self.assertEqual(report["recorded"], 1)
        self.assertEqual(report["computed"], 1)

    def test_a_clean_tree_is_accepted_and_counted(self):
        self.write("app.py", DECLARED)
        self.assertEqual(self.run_tool("--update")[0], 0)
        code, output, errors = self.run_tool("--json")
        self.assertEqual(code, 0, errors)
        report = json.loads(output)
        self.assertTrue(report["ok"])
        self.assertEqual(report["files"], 1)
        self.assertEqual(report["handlers"], 1)
        self.assertEqual(report["recorded"], 1)

    def test_the_list_shows_the_recorded_exemptions(self):
        self.write("app.py", DECLARED)
        self.assertEqual(self.run_tool("--update")[0], 0)
        code, output, _ = self.run_tool("--list")
        self.assertEqual(code, 0)
        self.assertIn("app.py:<module>", output)
        self.assertIn("dépendance optionnelle", output)

    def test_the_floor_refuses_a_corpus_that_was_not_really_read(self):
        """Un parcours cassé passerait à vide : on refuse le vide explicitement."""
        self.write("app.py", DECLARED)
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = csh.main(
                ["--root", str(self.root), "--inventory", str(self.inventory()), "--min-handlers", "100"]
            )
        self.assertEqual(code, 1)
        self.assertIn("trop peu", errors.getvalue())

    def test_an_unreadable_file_is_refused_not_ignored(self):
        path = self.root / "app.py"
        path.write_bytes(b"# \xff\xfe pas de l'UTF-8\n")
        code, _, errors = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("illisible", errors)
        self.assertIn("app.py", errors)

    def test_a_file_that_does_not_parse_is_refused_not_ignored(self):
        self.write("app.py", "def oups(:\n    pass\n")
        code, _, errors = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("app.py", errors)


class WiringTest(unittest.TestCase):
    """Les trois maillons sans lesquels le contrôle ne contrôlerait rien."""

    def test_the_gate_is_wired_into_the_workflow(self) -> None:
        workflow = REAL_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("check_silent_handlers.py", workflow)

    def test_the_step_runs_before_the_dependencies_are_installed(self) -> None:
        """Il ne tient qu'à la bibliothèque standard : il doit tourner avant `pip`."""
        workflow = REAL_WORKFLOW.read_text(encoding="utf-8")
        self.assertLess(
            workflow.index("check_silent_handlers.py"),
            workflow.index("pip install -r requirements.txt"),
        )

    def test_the_gate_is_documented(self) -> None:
        self.assertIn("check_silent_handlers.py", README.read_text(encoding="utf-8"))

    def test_the_inventory_is_versioned_on_disk(self) -> None:
        self.assertTrue(INVENTORY.is_file(), "l'inventaire des exemptions doit être versionné")

    def test_the_inventory_is_not_meant_to_be_edited_by_hand(self) -> None:
        self.assertIn("ne pas éditer à la main", INVENTORY.read_text(encoding="utf-8"))

    def test_the_repository_has_no_undeclared_silence(self) -> None:
        verdict = csh.check(REPO_ROOT, INVENTORY)
        self.assertTrue(verdict.report["ok"], verdict.message)
        self.assertEqual(verdict.status, "ok", verdict.message)
        self.assertEqual(verdict.report["drift"], False, verdict.message)
        self.assertEqual(verdict.report["offenders"], [], verdict.message)

    def test_the_inventory_is_up_to_date(self) -> None:
        """Le code et l'inventaire doivent dire la même chose, dans ce dépôt-ci."""
        harvest = csh.collect(REPO_ROOT)
        entries, problem = csh.read_recorded(INVENTORY)
        self.assertIsNone(problem)
        self.assertEqual(csh._normalise(entries or []), csh._normalise(harvest.entries))


class DocumentationTest(unittest.TestCase):
    """Le triage consigné, et son accord avec l'inventaire. Une frontière qui dérive ne se lit plus."""

    def triaged(self):
        """Les `(fichier, symbole, clause)` du triage, en multiensemble (les doublons comptent)."""
        text = DOC.read_text(encoding="utf-8")
        found = [match.groups()[:3] for match in TRIAGE_LINE.finditer(text)]
        return sorted(found)

    def inventoried(self):
        entries, problem = csh.read_recorded(INVENTORY)
        self.assertIsNone(problem)
        return sorted(
            (entry["file"], entry["symbol"], entry["handler"]) for entry in entries or []
        )

    def test_the_triage_exists_and_is_linked_from_the_readme(self):
        self.assertTrue(DOC.is_file(), "le triage doit être consigné dans un document versionné")
        self.assertIn("docs/EXCEPTIONS.md", README.read_text(encoding="utf-8"))

    def test_every_exemption_is_triaged(self):
        """Une exemption que le document ne range nulle part n'est pas une décision relue."""
        missing = _multiset_difference(self.inventoried(), self.triaged())
        self.assertEqual(missing, [], f"exemption(s) non triée(s) : {missing}")

    def test_no_triage_line_survives_its_exemption(self):
        """Un repli redevenu silencieux ou disparu laisserait un triage qui ne décrit plus rien."""
        stale = _multiset_difference(self.triaged(), self.inventoried())
        self.assertEqual(stale, [], f"ligne(s) de triage sans exemption : {stale}")

    def test_the_triage_states_the_boundary(self):
        """La frontière se lit : mesure fabriquée, les quatre régimes, et la règle d'ajout."""
        text = DOC.read_text(encoding="utf-8")
        self.assertIn("mesure fabriquée", text)
        for regime in TRIAGE_REGIMES:
            with self.subTest(regime=regime):
                self.assertIn(regime, text)
        self.assertIn("check_silent_handlers.py", text)

    def test_a_triage_line_carries_a_real_reason(self):
        reasons = [match.group(4).strip() for match in TRIAGE_LINE.finditer(DOC.read_text(encoding="utf-8"))]
        self.assertTrue(reasons)
        for reason in reasons:
            with self.subTest(reason=reason[:40]):
                self.assertGreaterEqual(len(reason), 20, "une raison de triage doit être écrite")


def _multiset_difference(left, right):
    """Ce qui reste de `left` une fois retiré `right`, occurrence par occurrence."""
    remaining = list(right)
    extra = []
    for item in left:
        if item in remaining:
            remaining.remove(item)
        else:
            extra.append(item)
    return extra


class CorpusTest(unittest.TestCase):
    """Le dépôt entier, lu par le contrôle — pas seulement par le détecteur."""

    @classmethod
    def setUpClass(cls):
        cls.files = csh.source_files(REPO_ROOT)

    def test_the_corpus_reads_a_plausible_repository(self):
        """Un parcours cassé passerait à vide : on refuse le vide explicitement."""
        self.assertIn(REPO_ROOT / "main.py", self.files)
        total = sum(len(list(csh.handlers(path.read_text(encoding="utf-8")))) for path in self.files)
        self.assertGreaterEqual(
            total, csh.MIN_HANDLERS, f"seulement {total} handler(s) lu(s) dans {len(self.files)} fichier(s)"
        )

    def test_no_handler_is_silent_without_a_declared_reason(self):
        offenders = []
        for path in self.files:
            for lineno in csh.undeclared_silences(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
        self.assertEqual(
            offenders,
            [],
            "handler(s) `except` sans signal et sans justification :\n"
            + "\n".join(f"  - {where}" for where in offenders)
            + "\nAjoute un appel qui nomme l'échec, ou un commentaire "
            f"« # {csh.MARKER} : <raison> » sur le `except` ou dans son corps, "
            "puis enregistre l'exemption : "
            f"{csh.UPDATE_COMMAND}",
        )


if __name__ == "__main__":
    unittest.main()
