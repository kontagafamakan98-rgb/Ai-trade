"""La veille Supabase périodique, en **lecture seule** (`workers/supabase_watch.py`).

Ce qui est éprouvé ici est une **comparaison entre deux passages**, et c'est tout
le sujet : un module qui alerterait sur l'état courant serait insupportable (une
base cassée alerterait toutes les cinq minutes, indéfiniment, jusqu'à ce qu'on
coupe la veille — le remède pire que le mal), et un module qui n'alerterait
jamais serait inutile. Quatre propriétés sont tenues, et chacune a son lot de cas :

* **on ne parle qu'au changement** : vert→rouge et rouge→vert sont annoncés, un
  état stable ne dit rien, et un échec qui existait déjà au premier passage est
  annoncé comme un **constat**, pas comme une découverte du jour ;
* **une vérification absente d'un rapport garde son verdict connu** : son retour
  se compare à ce qu'elle était, au lieu d'être pris pour une nouveauté ;
* **une alerte qui n'est pas partie n'est pas adoptée** — chat admin absent,
  Telegram en panne : le passage suivant redira le même changement, parce qu'un
  envoi refusé et une absence de changement ne doivent pas se ressembler ;
* **rien n'est écrit, jamais** : le mode qui écrit de la sonde (`roundtrip`) est
  désactivé **dans** le module, pas reçu en paramètre, donc pas contournable par
  distraction chez un appelant.

Une sonde en échec n'est ni un changement ni un état : on ne sait rien, donc rien
n'est adopté et rien n'est annoncé — mais l'échec est **rendu** (`probe_failed`),
pour que le silence ne se lise pas comme une bonne nouvelle.

`auto_loop` n'est pas importable ici (`feedparser` absent), donc son câblage est
vérifié en **relisant sa source** — même méthode que `tests/test_media_reconcile.py`.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import pathlib
import unittest
from unittest import mock

from core import config_runtime
from database import settings
from scripts import check_supabase
from tests import supabase_double
from workers import supabase_watch

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
AUTO_LOOP = REPO_ROOT / "workers" / "auto_loop.py"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
README = REPO_ROOT / "README.md"
WATCH_SOURCE = pathlib.Path(supabase_watch.__file__).read_text(encoding="utf-8")

ADMIN = "42"

#: Deux vérifications nommées comme la sonde les nomme, pour que les cas se lisent
#: comme le rapport réel plutôt que comme des jetons abstraits.
CONNEXION = "client Supabase"
INSIGHTS = "insights"


def _check(name, ok, detail=""):
    """Une vérification telle que `check_supabase.result()` la produit."""
    return {"name": name, "ok": ok, "detail": detail}


def _section(title, checks):
    return {"title": title, "checks": None if checks is None else list(checks)}


def _report(*sections, leftovers=()):
    """Un rapport de sonde : sections, verdict global, restes de l'aller-retour."""
    built = list(sections) or [_section("1. Configuration", [])]
    return {
        "sections": built,
        "ok": all(check["ok"] for section in built for check in (section["checks"] or [])),
        "leftovers": list(leftovers),
    }


def _one(*checks, title="1. Configuration"):
    return _report(_section(title, checks))


class _Probe:
    """Doublure de la sonde : elle retient ses appels, et peut échouer.

    Elle lève au lieu de rendre un rapport d'échec : c'est la panne qui compte
    ici (réseau coupé, bibliothèque absente), pas une base mal configurée — celle
    -là produit un **rapport** en échec, que la veille annonce.
    """

    def __init__(self, report=None, *, fail=None, sequence=None):
        self.report = report if report is not None else _one()
        self.fail = fail
        self.sequence = list(sequence) if sequence is not None else None
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.sequence:
            step = self.sequence.pop(0)
            return step() if callable(step) else step
        if self.fail is not None:
            raise self.fail
        return self.report


class _Recorder:
    """Doublure de l'envoi : ce qui est parti, au chat admin, ou pas du tout."""

    def __init__(self, *, fail=None):
        self.sent = []
        self.fail = fail

    async def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))
        if self.fail is not None:
            raise self.fail
        return True


class _Config:
    """Un `EnvConfig` réduit au seul champ que la veille lit."""

    def __init__(self, chat_id=""):
        self.telegram_admin_chat_id = chat_id


async def _pass(probe, *, watch=None, notify=None, chat_id=ADMIN, client="db", config=None):
    """Un passage : lire, comparer, alerter — et ce qui part au chat admin."""
    notify = notify if notify is not None else _Recorder()
    result = await supabase_watch.watch_once(
        watch=watch if watch is not None else supabase_watch.VerdictWatch(),
        probe=probe,
        notify=notify,
        chat_id=chat_id,
        client=client,
        config=config,
    )
    return result, notify


class FlattenChecksTest(unittest.TestCase):
    """Les sections sont une mise en page ; ce qui se suit est un **verdict**."""

    def test_every_section_is_gathered_in_order(self):
        report = _report(
            _section("1. Configuration", [_check(CONNEXION, True, "construit")]),
            _section("2. Tables", [_check(INSIGHTS, False, "migration non appliquée")]),
        )

        names = [check["name"] for check in supabase_watch.flatten_checks(report)]

        self.assertEqual(names, [CONNEXION, INSIGHTS])

    def test_an_absent_report_is_no_check_at_all(self):
        """Un rapport vide n'est pas une erreur : c'est un passage sans verdict."""
        for report in (None, {}, {"sections": None}, {"sections": []}):
            with self.subTest(report=report):
                self.assertEqual(supabase_watch.flatten_checks(report), [])

    def test_a_section_without_checks_is_skipped(self):
        report = _report(_section("1. Configuration", []), _section("2. Tables", None))

        self.assertEqual(supabase_watch.flatten_checks(report), [])

    def test_an_unnamed_check_is_gathered_here(self):
        """C'est `observe` qui écarte les anonymes, pas le rassemblement."""
        report = _one(_check("", False, "sans nom"), _check(INSIGHTS, True))

        self.assertEqual(len(supabase_watch.flatten_checks(report)), 2)


class VerdictWatchTest(unittest.TestCase):
    """La mémoire des verdicts, passage après passage — sans réseau ni Telegram."""

    def setUp(self) -> None:
        self.watch = supabase_watch.VerdictWatch()

    def _observe(self, report):
        return self.watch.observe(supabase_watch.flatten_checks(report))

    def test_a_first_green_pass_says_nothing(self):
        """Il n'y a rien à raconter d'un état normal : c'est le bruit à éviter."""
        self.assertEqual(self._observe(_one(_check(INSIGHTS, True))), [])

    def test_a_first_failing_pass_is_a_constat(self):
        change = self._observe(_one(_check(INSIGHTS, False, "relation absente")))[0]

        self.assertEqual(change["name"], INSIGHTS)
        self.assertEqual(change["change"], supabase_watch.CHANGE_FAILED)
        self.assertTrue(change["first"])
        self.assertEqual(change["detail"], "relation absente")

    def test_a_green_that_turns_red_is_a_failure(self):
        self._observe(_one(_check(INSIGHTS, True)))
        self.watch.commit()

        change = self._observe(_one(_check(INSIGHTS, False)))[0]

        self.assertEqual(change["change"], supabase_watch.CHANGE_FAILED)
        self.assertFalse(change["first"], "on connaissait son état d'avant")

    def test_a_red_that_turns_green_is_announced(self):
        """Sans ça, personne ne saurait jamais que c'est réglé."""
        self._observe(_one(_check(INSIGHTS, False)))
        self.watch.commit()

        change = self._observe(_one(_check(INSIGHTS, True)))[0]

        self.assertEqual(change["change"], supabase_watch.CHANGE_RECOVERED)

    def test_a_steady_green_does_not_reannounce_itself(self):
        """Un rétablissement repéré deux fois serait un rétablissement inventé."""
        self._observe(_one(_check(INSIGHTS, True)))
        self.watch.commit()

        self.assertEqual(self._observe(_one(_check(INSIGHTS, True))), [])

    def test_a_steady_failure_does_not_repeat_itself(self):
        self._observe(_one(_check(INSIGHTS, False)))
        self.watch.commit()

        changes = [self._observe(_one(_check(INSIGHTS, False))) for _ in range(3)]

        self.assertEqual(changes, [[], [], []], "l'état n'est pas réannoncé")

    def test_observing_alone_changes_nothing(self):
        """La décision est séparée de la mémoire : une alerte non partie se rejoue."""
        self._observe(_one(_check(INSIGHTS, False)))

        self.assertEqual(len(self._observe(_one(_check(INSIGHTS, False)))), 1)
        self.assertEqual(self.watch.verdicts(), {}, "rien n'a été adopté sans commit")

    def test_the_commit_adopts_the_pass(self):
        self._observe(_one(_check(INSIGHTS, False)))
        self.watch.commit()

        self.assertEqual(self.watch.verdicts(), {INSIGHTS: False})

    def test_a_check_absent_from_a_report_keeps_its_verdict(self):
        """Sinon son retour serait annoncé comme une nouveauté.

        C'est le cas d'une section facultative qui disparaît d'un rapport : la
        vérification n'a pas changé d'état, elle n'a pas été mesurée.
        """
        self._observe(_one(_check(INSIGHTS, False)))
        self.watch.commit()
        # Deuxième passage : un autre rapport, qui ne la mesure pas — et qui est
        # adopté, donc qui pourrait l'effacer.
        self._observe(_one(_check(CONNEXION, True)))
        self.watch.commit()

        self.assertEqual(self.watch.verdicts()[INSIGHTS], False)
        self.assertEqual(self._observe(_one(_check(INSIGHTS, False))), [])

    def test_a_check_absent_then_back_green_is_a_recovery_not_a_discovery(self):
        self._observe(_one(_check(INSIGHTS, False)))
        self.watch.commit()
        self._observe(_one(_check(CONNEXION, True)))
        self.watch.commit()

        change = self._observe(_one(_check(INSIGHTS, True)))[0]

        self.assertEqual(change["change"], supabase_watch.CHANGE_RECOVERED)
        self.assertFalse(change["first"])

    def test_a_check_absent_then_back_red_does_not_restate_a_first_pass(self):
        self._observe(_one(_check(INSIGHTS, True)))
        self.watch.commit()
        self._observe(_one(_check(CONNEXION, True)))
        self.watch.commit()

        change = self._observe(_one(_check(INSIGHTS, False)))[0]

        self.assertFalse(change["first"], "« constat du premier passage » serait faux ici")

    def test_a_nameless_check_is_ignored(self):
        """Toutes les entrées anonymes se confondraient sous la clé `\"\"`."""
        changes = self._observe(_one(_check("", False), _check("   ", False)))

        self.assertEqual(changes, [])
        self.assertEqual(self.watch.verdicts(), {})

    def test_every_check_is_followed_on_its_own(self):
        """Une vérification qui tombe ne doit pas signaler les autres."""
        self._observe(_one(_check(INSIGHTS, True), _check(CONNEXION, True)))
        self.watch.commit()

        changes = self._observe(_one(_check(INSIGHTS, False), _check(CONNEXION, True)))

        self.assertEqual([change["name"] for change in changes], [INSIGHTS])

    def test_the_verdicts_returned_are_a_copy(self):
        """`verdicts()` sert à journaliser, pas à décider."""
        self._observe(_one(_check(INSIGHTS, False)))
        self.watch.commit()

        self.watch.verdicts()[INSIGHTS] = True

        self.assertEqual(self.watch.verdicts(), {INSIGHTS: False})

    def test_a_discarded_pass_is_not_adopted(self):
        """Une sonde en échec : on ne sait rien, donc un commit n'adopte rien."""
        self._observe(_one(_check(INSIGHTS, False)))
        self.watch.discard()
        self.watch.commit()

        self.assertEqual(self.watch.verdicts(), {})


class FormatAlertTest(unittest.TestCase):
    """Le message : ce qui a bougé, depuis quand, et quoi en faire."""

    def _changes(self, *pairs):
        return [
            {"name": name, "change": change, "first": first, "detail": detail}
            for name, change, first, detail in pairs
        ]

    def test_the_alert_names_the_failing_check_and_its_detail(self):
        text = supabase_watch.format_alert(
            self._changes((INSIGHTS, supabase_watch.CHANGE_FAILED, False, "relation absente"))
        )

        self.assertIn("❌", text)
        self.assertIn(INSIGHTS, text)
        self.assertIn("relation absente", text)

    def test_the_recovery_is_announced_apart(self):
        text = supabase_watch.format_alert(
            self._changes((INSIGHTS, supabase_watch.CHANGE_RECOVERED, False, "lisible"))
        )

        self.assertIn("✅", text)
        self.assertIn("Rétabli", text)
        self.assertNotIn("❌", text)

    def test_a_first_pass_constat_is_qualified(self):
        """Sinon on croit à une panne du jour là où la base était déjà cassée."""
        text = supabase_watch.format_alert(
            self._changes((INSIGHTS, supabase_watch.CHANGE_FAILED, True, "absente"))
        )

        self.assertIn("Constat du premier passage", text)

    def test_a_transition_is_not_qualified(self):
        text = supabase_watch.format_alert(
            self._changes((INSIGHTS, supabase_watch.CHANGE_FAILED, False, "absente"))
        )

        self.assertNotIn("Constat du premier passage", text)

    def test_the_failures_come_before_the_recoveries(self):
        """C'est ce qu'on lit en premier quand les deux arrivent ensemble."""
        text = supabase_watch.format_alert(
            self._changes(
                (CONNEXION, supabase_watch.CHANGE_RECOVERED, False, "construit"),
                (INSIGHTS, supabase_watch.CHANGE_FAILED, False, "absente"),
            )
        )

        self.assertLess(text.index(INSIGHTS), text.index(CONNEXION))
        self.assertLess(text.index("❌"), text.index("✅"))

    def test_the_message_says_the_watch_never_writes(self):
        """Le lecteur doit pouvoir vérifier la promesse sans relire le code."""
        text = supabase_watch.format_alert(
            self._changes((INSIGHTS, supabase_watch.CHANGE_FAILED, False, "absente"))
        )

        self.assertIn("sans jamais écrire", text)
        self.assertIn("--roundtrip", text)

    def test_the_message_carries_no_markup(self):
        """Le bot envoie sans `parse_mode` : les astérisques s'afficheraient."""
        text = supabase_watch.format_alert(
            self._changes(
                (INSIGHTS, supabase_watch.CHANGE_FAILED, False, "absente"),
                (CONNEXION, supabase_watch.CHANGE_RECOVERED, False, "construit"),
            )
        )

        self.assertNotIn("*", text)
        self.assertNotIn("`", text)
        self.assertLess(len(text), 4096, "un message Telegram en porte 4096")

    def test_a_pass_without_change_still_has_a_sentence(self):
        """Inatteignable depuis `watch_once`, mais un message vide serait pire."""
        self.assertIn("rien de changé", supabase_watch.format_alert([]))


class ReadOnlyProbeTest(unittest.TestCase):
    """La promesse du module : aucune écriture, et elle est **dans** le code."""

    def test_the_probe_switch_is_off_and_not_received(self):
        run = mock.Mock(return_value=_one())
        with mock.patch.object(check_supabase, "run", run):
            supabase_watch._read_only_probe(client="db")

        run.assert_called_once_with(roundtrip=False, as_json=False, client="db")

    def test_the_module_cannot_ask_for_a_roundtrip(self):
        """Un `roundtrip=True` écrit sept tables : il ne doit pas être atteignable."""
        self.assertIn("roundtrip=False", WATCH_SOURCE)
        self.assertNotIn("roundtrip=True", WATCH_SOURCE)

    def test_the_probe_is_the_default(self):
        signature = inspect.signature(supabase_watch.watch_once)

        self.assertIsNone(signature.parameters["probe"].default)
        self.assertIn("probe = probe or _read_only_probe", WATCH_SOURCE)

    def test_the_supabase_stack_is_imported_lazily(self):
        """La veille doit s'importer sans `supabase` ni `dotenv`, comme les workers."""
        top = WATCH_SOURCE.split("def _read_only_probe", 1)[0]

        self.assertNotIn("import supabase", top)
        self.assertNotIn("from scripts import", top)


class WatchOnceTest(unittest.IsolatedAsyncioTestCase):
    """Un passage complet : constat, décision, envoi — et ce qui ne se produit pas."""

    async def test_a_first_failing_pass_alerts_the_admin_chat(self):
        probe = _Probe(_one(_check(INSIGHTS, False, "relation absente")))

        result, recorder = await _pass(probe)

        self.assertTrue(result["alerted"])
        self.assertIsNone(result["reason"])
        self.assertEqual(recorder.sent[0][0], ADMIN)
        self.assertIn(INSIGHTS, recorder.sent[0][1])
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["failing"], 1)

    async def test_a_quiet_pass_sends_nothing_but_is_adopted(self):
        watch = supabase_watch.VerdictWatch()
        probe = _Probe(_one(_check(INSIGHTS, True)))

        result, recorder = await _pass(probe, watch=watch)

        self.assertFalse(result["alerted"])
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["failing"], 0)
        self.assertEqual(recorder.sent, [])
        self.assertEqual(watch.verdicts(), {INSIGHTS: True})

    async def test_the_worker_client_is_handed_to_the_probe(self):
        """Sans ça, la veille interrogerait la base réelle en test comme en prod."""
        probe = _Probe()

        await _pass(probe, client="doublure")

        self.assertEqual(probe.calls, [{"client": "doublure"}])

    async def test_the_probe_runs_off_the_event_loop(self):
        """La sonde est bloquante (réseau, base) : elle ne doit pas geler le reste."""
        probe = _Probe()
        threaded = []
        real = asyncio.to_thread

        async def spy(func, *args, **kwargs):
            threaded.append(func)
            return await real(func, *args, **kwargs)

        with mock.patch.object(asyncio, "to_thread", spy):
            await _pass(probe)

        self.assertEqual(threaded, [probe])

    async def test_a_successful_send_adopts_the_pass(self):
        """Le point de bascule : c'est l'envoi **réussi** qui fait avancer la mémoire."""
        watch = mock.Mock()
        watch.observe.return_value = [
            {"name": INSIGHTS, "change": supabase_watch.CHANGE_FAILED, "first": True, "detail": ""}
        ]

        result, recorder = await _pass(_Probe(), watch=watch)

        self.assertTrue(result["alerted"])
        self.assertEqual(len(recorder.sent), 1)
        watch.commit.assert_called_once_with()
        watch.discard.assert_not_called()

    async def test_a_refused_send_does_not_adopt_the_pass(self):
        """Sinon le changement serait perdu : personne ne l'a lu."""
        watch = mock.Mock()
        watch.observe.return_value = [
            {"name": INSIGHTS, "change": supabase_watch.CHANGE_FAILED, "first": False, "detail": ""}
        ]

        result, _ = await _pass(
            _Probe(), watch=watch, notify=_Recorder(fail=RuntimeError("Telegram down"))
        )

        self.assertEqual(result["reason"], "send_failed")
        watch.commit.assert_not_called()
        watch.discard.assert_not_called()

    async def test_a_probe_failure_clears_the_observed_pass(self):
        """Rien n'a été lu : ni comparaison, ni adoption — le passage est jeté."""
        watch = mock.Mock()
        watch.observe.return_value = []

        result, _ = await _pass(_Probe(fail=RuntimeError("boom")), watch=watch)

        self.assertEqual(result["reason"], "probe_failed")
        watch.discard.assert_called_once_with()
        watch.observe.assert_not_called()
        watch.commit.assert_not_called()

    async def test_the_same_state_does_not_alert_twice(self):
        watch = supabase_watch.VerdictWatch()
        probe = _Probe(_one(_check(INSIGHTS, False)))
        recorder = _Recorder()

        await _pass(probe, watch=watch, notify=recorder)
        await _pass(probe, watch=watch, notify=recorder)

        self.assertEqual(len(recorder.sent), 1, "l'état n'est pas réannoncé")

    async def test_a_transition_alerts_again(self):
        watch = supabase_watch.VerdictWatch()
        recorder = _Recorder()
        failing = _Probe(_one(_check(INSIGHTS, False)))
        working = _Probe(_one(_check(INSIGHTS, True)))

        await _pass(failing, watch=watch, notify=recorder)
        await _pass(working, watch=watch, notify=recorder)

        self.assertEqual(len(recorder.sent), 2)
        self.assertIn("Rétabli", recorder.sent[-1][1])

    async def test_a_probe_failure_is_a_result_not_an_exception(self):
        """Une veille qui tombe emporterait le cycle qui l'appelle."""
        probe = _Probe(fail=RuntimeError("connexion refusée"))

        result, recorder = await _pass(probe)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "probe_failed")
        self.assertIn("connexion refusée", result["error"])
        self.assertEqual(result["changes"], [])
        self.assertEqual(result["checked"], 0)
        self.assertEqual(recorder.sent, [])

    async def test_a_probe_failure_adopts_nothing(self):
        """On ne sait rien : le passage suivant doit pouvoir reparler."""
        watch = supabase_watch.VerdictWatch()
        await _pass(_Probe(fail=RuntimeError("boom")), watch=watch)

        self.assertEqual(watch.verdicts(), {})
        result, recorder = await _pass(
            _Probe(_one(_check(INSIGHTS, False))), watch=watch
        )
        self.assertTrue(result["alerted"], "l'échec n'a rien fait adopter")
        self.assertEqual(len(recorder.sent), 1)

    async def test_the_chat_is_read_from_the_configuration(self):
        probe = _Probe(_one(_check(INSIGHTS, False)))

        _, recorder = await _pass(probe, chat_id=None, config=_Config("777"))

        self.assertEqual(recorder.sent[0][0], "777")

    async def test_without_an_admin_chat_the_silence_is_explained(self):
        """Sans destinataire, l'alerte n'existe pas — et ça ne doit pas se taire."""
        watch = supabase_watch.VerdictWatch()
        probe = _Probe(_one(_check(INSIGHTS, False)))

        result, recorder = await _pass(probe, watch=watch, chat_id="", config=_Config())

        self.assertFalse(result["alerted"])
        self.assertEqual(result["reason"], "no_admin_chat")
        self.assertEqual(len(result["changes"]), 1, "le changement a bien été vu")
        self.assertEqual(recorder.sent, [])
        self.assertEqual(watch.verdicts(), {}, "rien n'a été lu par personne")

    async def test_the_alert_waits_for_a_chat_that_exists(self):
        """Le verdict n'est pas adopté : le changement partira quand il y aura un chat."""
        watch = supabase_watch.VerdictWatch()
        probe = _Probe(_one(_check(INSIGHTS, False)))
        await _pass(probe, watch=watch, chat_id="", config=_Config())

        result, recorder = await _pass(probe, watch=watch)

        self.assertTrue(result["alerted"])
        self.assertEqual(len(recorder.sent), 1)

    async def test_a_failed_send_is_replayed_on_the_next_pass(self):
        """Sinon la veille se tairait sur une panne que personne n'a vue."""
        watch = supabase_watch.VerdictWatch()
        probe = _Probe(_one(_check(INSIGHTS, False)))
        failing = _Recorder(fail=RuntimeError("Telegram down"))

        first, _ = await _pass(probe, watch=watch, notify=failing)

        self.assertFalse(first["alerted"])
        self.assertEqual(first["reason"], "send_failed")
        self.assertEqual(watch.verdicts(), {}, "un envoi refusé n'est pas un passage adopté")

        second, recorder = await _pass(probe, watch=watch)

        self.assertTrue(second["alerted"])
        self.assertEqual(len(recorder.sent), 1)

    async def test_a_send_failure_does_not_repeat_the_recovery_either(self):
        """Le même contrat pour les deux sens : c'est la livraison qui compte."""
        watch = supabase_watch.VerdictWatch()
        await _pass(_Probe(_one(_check(INSIGHTS, False))), watch=watch)
        failing = _Recorder(fail=RuntimeError("Telegram down"))

        failed, _ = await _pass(
            _Probe(_one(_check(INSIGHTS, True))), watch=watch, notify=failing
        )

        self.assertEqual(failed["reason"], "send_failed")
        self.assertFalse(failed["alerted"])

        again, recorder = await _pass(_Probe(_one(_check(INSIGHTS, True))), watch=watch)

        self.assertTrue(again["alerted"])
        self.assertIn("Rétabli", recorder.sent[-1][1])

    async def test_an_empty_report_is_a_pass_without_verdict(self):
        """Aucune vérification nommée : rien à comparer, donc rien à dire."""
        result, recorder = await _pass(_Probe({"sections": []}))

        self.assertTrue(result["ok"])
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["changes"], [])
        self.assertEqual(recorder.sent, [])

    async def test_each_named_check_is_reported_with_its_own_name(self):
        probe = _Probe(
            _report(
                _section("1. Configuration", [_check(CONNEXION, True)]),
                _section("2. Tables", [_check(INSIGHTS, False, "absente")]),
            )
        )

        result, recorder = await _pass(probe)

        self.assertEqual([change["name"] for change in result["changes"]], [INSIGHTS])
        self.assertEqual(result["checked"], 2)
        self.assertIn(INSIGHTS, recorder.sent[0][1])


def _settings_client(store=None):
    """Le client doublé du dossier, `bot_settings` semé comme la base le rendrait.

    `store` est le dictionnaire clé → valeur des surcharges : chaque entrée devient
    une **ligne**, donc la lecture filtrée par `eq("key", …)` la retrouve, et une
    clé absente est une ligne absente. La valeur est relue par `get_setting` — la
    vraie lecture du module — et non par une seconde lecture écrite dans le test.
    """
    client = supabase_double.SupabaseDouble()
    client.store(settings.TABLE).rows = [
        {"key": key, "value": value} for key, value in (store or {}).items()
    ]
    return client


class SettingsTest(unittest.TestCase):
    """La période : bornée, éteignable, jamais en échec."""

    def test_a_plain_value_is_kept(self):
        self.assertEqual(config_runtime.parse_supabase_watch_minutes(720), 720)
        self.assertEqual(config_runtime.parse_supabase_watch_minutes("12"), 12)

    def test_zero_turns_the_watch_off_instead_of_being_raised_to_the_floor(self):
        """Éteindre une veille est une décision, pas une valeur à corriger."""
        self.assertEqual(config_runtime.parse_supabase_watch_minutes(0), 0)
        self.assertEqual(config_runtime.parse_supabase_watch_minutes("0"), 0)
        self.assertEqual(config_runtime.parse_supabase_watch_minutes(-10), 0)

    def test_a_period_below_the_floor_is_raised(self):
        low, _ = config_runtime.SUPABASE_WATCH_MINUTES_BOUNDS

        self.assertEqual(config_runtime.parse_supabase_watch_minutes(1), low)
        self.assertEqual(config_runtime.parse_supabase_watch_minutes(low - 1), low)

    def test_a_period_above_the_ceiling_is_lowered(self):
        _, high = config_runtime.SUPABASE_WATCH_MINUTES_BOUNDS

        self.assertEqual(config_runtime.parse_supabase_watch_minutes(100000), high)

    def test_an_unreadable_value_falls_back_without_raising(self):
        self.assertEqual(
            config_runtime.parse_supabase_watch_minutes("bientot"),
            config_runtime.DEFAULT_SUPABASE_WATCH_MINUTES,
        )
        self.assertEqual(config_runtime.parse_supabase_watch_minutes("bientot", default=45), 45)
        self.assertEqual(config_runtime.parse_supabase_watch_minutes(None, default=45), 45)

    def test_the_default_is_the_project_one(self):
        self.assertEqual(
            config_runtime.parse_supabase_watch_minutes(None),
            config_runtime.DEFAULT_SUPABASE_WATCH_MINUTES,
        )


class WatchSettingsTest(unittest.TestCase):
    """Les valeurs effectives : base, sinon environnement, sinon défaut."""

    KEYS = ("SUPABASE_WATCH_MINUTES",)

    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in self.KEYS}
        os.environ["SUPABASE_WATCH_MINUTES"] = "120"
        config_runtime.reset_env_config()

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_runtime.reset_env_config()

    def _settings(self, store=None):
        return settings.supabase_watch_settings(client=_settings_client(store))

    def test_the_environment_applies_by_default(self):
        self.assertEqual(self._settings(), {"minutes": 120})

    def test_the_database_override_wins(self):
        self.assertEqual(
            self._settings({settings.SETTING_SUPABASE_WATCH_MINUTES: 45}),
            {"minutes": 45},
        )

    def test_zero_in_the_database_turns_the_watch_off(self):
        self.assertEqual(
            self._settings({settings.SETTING_SUPABASE_WATCH_MINUTES: 0})["minutes"], 0
        )

    def test_an_unreadable_override_keeps_the_environment(self):
        """Une faute de frappe ne peut pas éteindre la veille par accident."""
        resolved = self._settings({settings.SETTING_SUPABASE_WATCH_MINUTES: "bientot"})

        self.assertEqual(resolved["minutes"], 120)

    def test_a_broken_database_keeps_the_environment(self):
        client = _settings_client()
        client.table = lambda name: (_ for _ in ()).throw(RuntimeError("db down"))

        self.assertEqual(settings.supabase_watch_settings(client=client), {"minutes": 120})


class PreflightTest(unittest.TestCase):
    """Ce qui est configuré doit se lire quelque part."""

    def setUp(self) -> None:
        self._saved = {
            key: os.environ.get(key)
            for key in ("SUPABASE_WATCH_MINUTES", "TELEGRAM_ADMIN_CHAT_ID")
        }
        for key in self._saved:
            os.environ.pop(key, None)
        config_runtime.reset_env_config()

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_runtime.reset_env_config()

    def test_the_effective_watch_is_published(self):
        os.environ["SUPABASE_WATCH_MINUTES"] = "15"
        config_runtime.reset_env_config()

        published = config_runtime.get_env_config().preflight()["supabase_watch"]

        self.assertEqual(published, {"every_minutes": 15})

    def test_an_off_watch_is_visible(self):
        """« Éteinte » doit se lire, sinon on cherche pourquoi aucune alerte n'arrive."""
        os.environ["SUPABASE_WATCH_MINUTES"] = "0"
        config_runtime.reset_env_config()

        self.assertEqual(
            config_runtime.get_env_config().preflight()["supabase_watch"]["every_minutes"], 0
        )

    def test_a_watch_without_an_admin_chat_is_visible(self):
        health = config_runtime.get_env_config().component_health()["supabase_watch"]

        self.assertFalse(health["admin_chat"])
        self.assertEqual(health["every_minutes"], config_runtime.DEFAULT_SUPABASE_WATCH_MINUTES)

        os.environ["TELEGRAM_ADMIN_CHAT_ID"] = ADMIN
        config_runtime.reset_env_config()

        self.assertTrue(
            config_runtime.get_env_config().component_health()["supabase_watch"]["admin_chat"]
        )


class WiringTest(unittest.TestCase):
    """`auto_loop` n'est pas importable ici (`feedparser` absent) : on le lit."""

    def setUp(self) -> None:
        self.source = AUTO_LOOP.read_text(encoding="utf-8")

    def test_the_watch_is_imported_by_the_loop(self):
        self.assertIn(
            "from workers import media_reconcile, scan_backlog, supabase_watch", self.source
        )

    def test_the_watch_runs_once_at_startup(self):
        """Un déploiement est justement le moment où on veut savoir."""
        startup = self.source.split("while True:", 1)[0]

        self.assertIn("await supabase_watch_and_alert()", startup)

    def test_the_watch_is_a_veille_not_a_cycle(self):
        body = self.source.split("while True:", 1)[1]

        self.assertIn("await supabase_watch_and_alert()", body)

    def test_the_verdict_memory_outlives_a_single_pass(self):
        """L'état est au niveau du module : une comparaison ne tient pas dans un appel."""
        self.assertIn("\n_supabase_watch = supabase_watch.VerdictWatch()", self.source)

    def test_the_period_comes_from_the_settings(self):
        self.assertIn("settings.supabase_watch_settings", self.source)
        self.assertIn('_supabase_watch_settings["minutes"] * 60', self.source)
        self.assertIn(
            "resolved = await asyncio.to_thread(settings.supabase_watch_settings)",
            self.source,
        )

    def test_an_off_watch_is_re_examined_instead_of_staying_off_forever(self):
        """Sinon un `0` posé en base ne serait relu qu'au prochain redémarrage."""
        self.assertIn("DISABLED_RECHECK_EVERY", self.source)

    def test_the_startup_line_says_what_will_run(self):
        body = self.source.split("async def run_forever", 1)[1]

        self.assertIn("Veille Supabase (lecture seule):", body)

    def test_the_worker_does_not_reimplement_the_decision(self):
        """La comparaison des verdicts vit dans le module, pas dans la boucle."""
        self.assertNotIn("CHANGE_FAILED", self.source)
        self.assertNotIn("format_alert", self.source)

    def test_no_period_is_hardcoded_in_the_worker(self):
        for line in self.source.splitlines():
            if line.strip().startswith("#"):
                continue
            self.assertNotIn("SUPABASE_WATCH_MINUTES =", line)

    def test_the_pass_is_journalised_in_every_case(self):
        """Une veille silencieuse doit se distinguer d'une veille qui n'a rien vu."""
        body = self.source.split("async def supabase_watch_and_alert", 1)[1]

        self.assertIn("aucun changement", body)
        self.assertIn("alerte NON envoyée", body)
        self.assertIn("sonde en échec", body)


class DocumentationTest(unittest.TestCase):
    """.env.example et le README disent la même chose que le code."""

    def test_the_variable_is_documented_in_the_env_example(self):
        text = ENV_EXAMPLE.read_text(encoding="utf-8")

        self.assertIn("SUPABASE_WATCH_MINUTES", text)

    def test_the_readme_has_a_section_for_the_watch(self):
        text = README.read_text(encoding="utf-8")

        self.assertIn("workers/supabase_watch.py", text)
        self.assertIn("lecture seule", text)


if __name__ == "__main__":
    unittest.main()
