"""Suivi du report d'extractions (`workers/scan_backlog.py`).

Ce qui est éprouvé ici est une **suite** d'observations, et c'est tout le sujet :
le report est normal, c'est sa répétition qui ne l'est pas. Un module qui
n'alerterait qu'au premier report serait donc aussi faux qu'un module qui
n'alerterait jamais — les deux se lisent pourtant « ça marche » sur un seul cas.
Quatre propriétés sont tenues, et chacune a son lot de cas :

* **le seuil se compte en balayages consécutifs** : un report isolé est le
  mécanisme du plafond qui travaille, et la séquence repart de zéro dès qu'un
  balayage ne reporte plus ;
* **on ne répète pas la même alerte**, mais on reparle si ça empire, et un blocage
  qui ne bouge pas est rappelé à intervalle large — une alerte unique peut se
  perdre dans une nuit ;
* **le retour à la normale est annoncé**, en une fois, avec la durée du blocage :
  sans lui, une alerte ne se referme jamais ;
* **une lecture qui ne revient pas finit par être abandonnée** : les rappels d'un
  canal illisible s'espacent (le double à chaque fois), puis s'arrêtent sur un
  dernier message. C'est la différence entre une veille et une plainte : au bout de
  quelques jours, le même message ne dit plus rien de neuf. L'abandon ne porte que
  sur la **lecture** — un canal qui reporte est vivant — et il tombe à la première
  lecture qui aboutit, annoncée comme telle ;
* **une alerte qui n'est pas partie n'est pas notée comme envoyée** — chat admin
  absent, Telegram en panne : le balayage suivant doit pouvoir réessayer.

Un balayage en erreur (`None`) n'est ni un report ni une résolution : compter l'un
pour l'autre ferait taire l'alerte d'un canal dont le balayage vient justement
d'échouer.

Et il y a une seconde famille, au symptôme opposé : un balayage qui **n'a rien
lu** — l'aperçu `t.me/s` du canal injoignable. Ses comptes sont à zéro, mais ce ne
sont pas des mesures : sans le motif rendu par le scraper, un canal injoignable et
un canal calme se présentent exactement pareil. Sa suite se compte donc à part
(un échec ne dit rien du report), elle a ses propres messages, et elle se solde par
la première lecture qui aboutit.

`auto_loop` n'est pas importable ici (`feedparser` absent), donc son câblage est
vérifié en **relisant sa source** — même méthode que `tests/test_media_reconcile.py`.
"""
from __future__ import annotations

import pathlib
import unittest

from workers import scan_backlog

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
AUTO_LOOP = REPO_ROOT / "workers" / "auto_loop.py"

CHANNEL = "canal_charge"
ADMIN = "42"
#: Un motif tel que le scraper en rend un : le type de l'exception, puis son texte.
CAUSE = "ConnectError: [Errno 11001] getaddrinfo failed"


class _Recorder:
    """Doublure de l'envoi : elle retient ce qui est parti, et peut échouer."""

    def __init__(self, *, fail: bool = False, fail_from: int = 0) -> None:
        self.sent = []
        self.fail = fail
        #: Échoue à partir du n-ième envoi (0 = jamais). Un envoi qui échoue **après**
        #: des envois réussis est le cas qui compte pour un abandon : la décision est
        #: prise, mais personne ne l'a lue.
        self.fail_from = fail_from

    async def __call__(self, chat_id, text):
        if self.fail or (self.fail_from and len(self.sent) + 1 >= self.fail_from):
            raise RuntimeError("Telegram down")
        self.sent.append((chat_id, text))
        return True


def _watch(**kwargs) -> scan_backlog.ScanBacklog:
    return scan_backlog.ScanBacklog(**kwargs)


async def _sweep(counts, *, watch=None, recorder=None, chat_id=ADMIN, cap=10, errors=None):
    """Un balayage : les comptes par canal, les motifs d'échec, et ce qui part."""
    recorder = recorder if recorder is not None else _Recorder()
    result = await scan_backlog.observe_sweep(
        counts,
        errors=errors,
        watch=watch if watch is not None else _watch(),
        notify=recorder,
        chat_id=chat_id,
        cap=cap,
    )
    return result, recorder


class BacklogDecisionTest(unittest.TestCase):
    """Ce qui décide, balayage après balayage — sans réseau ni Telegram."""

    def test_a_report_below_the_threshold_is_silent(self) -> None:
        watch = _watch(threshold=3)

        self.assertEqual(watch.observe(CHANNEL, deferred=7), scan_backlog.ACTION_SILENT)
        self.assertEqual(watch.observe(CHANNEL, deferred=9), scan_backlog.ACTION_SILENT)

    def test_the_threshold_raises_the_alert(self) -> None:
        """« Plusieurs balayages d'affilée » : c'est la troisième fois qu'on parle."""
        watch = _watch(threshold=3)
        watch.observe(CHANNEL, deferred=7)
        watch.observe(CHANNEL, deferred=7)

        self.assertEqual(watch.observe(CHANNEL, deferred=7), scan_backlog.ACTION_BLOCKED)
        self.assertEqual(watch.consecutive(CHANNEL), 3)
        self.assertEqual(watch.peak(CHANNEL), 7)

    def test_a_settled_blockage_does_not_repeat_itself(self) -> None:
        watch = _watch(threshold=3)
        for _ in range(2):
            watch.observe(CHANNEL, deferred=7)
        self.assertEqual(watch.observe(CHANNEL, deferred=7), scan_backlog.ACTION_BLOCKED)
        watch.recorded(CHANNEL, scan_backlog.ACTION_BLOCKED)

        self.assertEqual(watch.observe(CHANNEL, deferred=7), scan_backlog.ACTION_SILENT)
        self.assertEqual(watch.observe(CHANNEL, deferred=7), scan_backlog.ACTION_SILENT)
        self.assertEqual(watch.alerted, [CHANNEL])

    def test_a_worsening_speaks_again(self) -> None:
        """Le pic de référence est celui de la **dernière** alerte, pas du début."""
        watch = _watch(threshold=2)
        watch.observe(CHANNEL, deferred=5)
        watch.recorded(CHANNEL, watch.observe(CHANNEL, deferred=5))

        self.assertEqual(watch.observe(CHANNEL, deferred=12), scan_backlog.ACTION_WORSENED)
        watch.recorded(CHANNEL, scan_backlog.ACTION_WORSENED)
        self.assertEqual(watch.observe(CHANNEL, deferred=12), scan_backlog.ACTION_SILENT)

    def test_a_blockage_that_does_not_move_is_reminded(self) -> None:
        """Un rappel à intervalle large : dire une fois ce n'est pas assez."""
        watch = _watch(threshold=2, reminder_every=3)
        watch.observe(CHANNEL, deferred=5)
        watch.recorded(CHANNEL, watch.observe(CHANNEL, deferred=5))

        seen = [watch.observe(CHANNEL, deferred=5) for _ in range(3)]

        self.assertEqual(seen, [scan_backlog.ACTION_SILENT] * 2 + [scan_backlog.ACTION_REMINDER])

    def test_the_report_disappearing_closes_the_alert_once(self) -> None:
        watch = _watch(threshold=2)
        watch.observe(CHANNEL, deferred=5)
        watch.recorded(CHANNEL, watch.observe(CHANNEL, deferred=5))

        self.assertEqual(watch.observe(CHANNEL, deferred=0), scan_backlog.ACTION_RESOLVED)
        watch.recorded(CHANNEL, scan_backlog.ACTION_RESOLVED)
        self.assertEqual(watch.observe(CHANNEL, deferred=0), scan_backlog.ACTION_SILENT)
        self.assertEqual(watch.alerted, [])

    def test_a_report_that_never_alerted_resolves_to_nothing(self) -> None:
        """Sans alerte ouverte, « plus de report » n'est pas une nouvelle."""
        watch = _watch(threshold=3)
        watch.observe(CHANNEL, deferred=4)

        self.assertEqual(watch.observe(CHANNEL, deferred=0), scan_backlog.ACTION_SILENT)

    def test_the_sequence_restarts_from_zero(self) -> None:
        """Un balayage sans report solde la séquence : le suivant repart à un."""
        watch = _watch(threshold=3)
        watch.observe(CHANNEL, deferred=4)
        watch.observe(CHANNEL, deferred=4)
        watch.observe(CHANNEL, deferred=0)
        watch.recorded(CHANNEL, scan_backlog.ACTION_SILENT)

        self.assertEqual(watch.consecutive(CHANNEL), 0)
        self.assertEqual(watch.observe(CHANNEL, deferred=4), scan_backlog.ACTION_SILENT)

    def test_each_channel_is_counted_apart(self) -> None:
        """Le compte est par canal : un canal chargé ne doit pas signaler les autres."""
        watch = _watch(threshold=2)
        watch.observe(CHANNEL, deferred=9)
        watch.observe("calme", deferred=1)
        watch.observe(CHANNEL, deferred=9)

        self.assertEqual(watch.alerted, [])
        self.assertEqual(watch.consecutive("calme"), 1)
        self.assertEqual(watch.consecutive(CHANNEL), 2)

    def test_a_channel_that_is_no_longer_scanned_is_forgotten(self) -> None:
        """Sinon un canal retiré puis remis serait annoncé bloqué sur un seul report."""
        watch = _watch(threshold=3)
        watch.observe(CHANNEL, deferred=4)
        watch.observe(CHANNEL, deferred=4)

        self.assertEqual(watch.retain(["autre_canal"]), [CHANNEL])
        self.assertEqual(watch.consecutive(CHANNEL), 0)
        self.assertEqual(watch.observe(CHANNEL, deferred=4), scan_backlog.ACTION_SILENT)

    def test_the_threshold_cannot_make_one_report_a_symptom(self) -> None:
        """`1` serait un seuil qui alerte sur le fonctionnement normal du plafond."""
        self.assertEqual(_watch(threshold=1).threshold, 2)
        self.assertEqual(_watch(threshold=0).threshold, 2)


class ScanFailureDecisionTest(unittest.TestCase):
    """La lecture qui échoue : une seconde suite, qui ne se compte pas avec la première."""

    def test_a_single_failed_read_is_not_a_symptom(self) -> None:
        """`t.me/s` limite les requêtes : un échec isolé est un hoquet réseau."""
        watch = _watch(threshold=3)

        self.assertEqual(watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SILENT)
        self.assertEqual(watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SILENT)
        self.assertEqual(watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SCAN_FAILING)
        self.assertEqual(watch.failures(CHANNEL), 3)

    def test_a_settled_failure_does_not_repeat_itself(self) -> None:
        watch = _watch(threshold=2)
        watch.observe(CHANNEL, error=CAUSE)
        watch.recorded(CHANNEL, watch.observe(CHANNEL, error=CAUSE))

        self.assertEqual(watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SILENT)
        self.assertEqual(watch.failing, [CHANNEL], "l'alerte reste ouverte")

    def test_a_read_that_about_soldes_the_failure_sequence(self) -> None:
        """Sinon des échecs séparés par une lecture se compteraient à la suite."""
        watch = _watch(threshold=3)
        watch.observe(CHANNEL, error=CAUSE)
        watch.observe(CHANNEL, error=CAUSE)

        watch.observe(CHANNEL, deferred=0)

        self.assertEqual(watch.failures(CHANNEL), 0)
        self.assertEqual(
            watch.observe(CHANNEL, error=CAUSE),
            scan_backlog.ACTION_SILENT,
            "la suite repart de un",
        )

    def test_a_failure_that_does_not_move_is_reminded(self) -> None:
        """Une panne qui dure ne doit pas se perdre dans une nuit."""
        watch = _watch(threshold=2, reminder_every=3)
        watch.observe(CHANNEL, error=CAUSE)
        watch.recorded(CHANNEL, watch.observe(CHANNEL, error=CAUSE))
        for _ in range(2):
            self.assertEqual(
                watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SILENT
            )

        self.assertEqual(watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SCAN_REMINDER)

    def test_the_first_read_that_succeeds_closes_the_alert_once(self) -> None:
        """Sans annonce de reprise, l'alerte ne se referme jamais."""
        watch = _watch(threshold=2)
        watch.observe(CHANNEL, error=CAUSE)
        watch.recorded(CHANNEL, watch.observe(CHANNEL, error=CAUSE))

        self.assertEqual(watch.observe(CHANNEL, deferred=0), scan_backlog.ACTION_SCAN_RECOVERED)
        watch.recorded(CHANNEL, scan_backlog.ACTION_SCAN_RECOVERED)

        self.assertEqual(watch.observe(CHANNEL, deferred=0), scan_backlog.ACTION_SILENT)
        self.assertEqual(watch.failing, [])
        self.assertEqual(watch.failures(CHANNEL), 0)

    def test_a_recovery_that_could_not_be_sent_is_redited_with_its_duration(self) -> None:
        """Personne ne l'a lue : la reprise doit pouvoir être annoncée telle quelle."""
        watch = _watch(threshold=3)
        for _ in range(3):
            watch.recorded(CHANNEL, watch.observe(CHANNEL, error=CAUSE))
        self.assertEqual(watch.failures(CHANNEL), 3)

        # Deux lectures qui aboutissent n'ont rien annoncé (l'envoi a échoué chaque
        # fois) : la durée annoncée reste celle des échecs, elle ne se prolonge pas.
        self.assertEqual(watch.observe(CHANNEL, deferred=0), scan_backlog.ACTION_SCAN_RECOVERED)
        self.assertEqual(watch.observe(CHANNEL, deferred=0), scan_backlog.ACTION_SCAN_RECOVERED)

        self.assertEqual(watch.failures(CHANNEL), 3)

    def test_a_failure_does_not_touch_the_report_sequence(self) -> None:
        """Rien n'a été mesuré : ce n'est pas « plus de report », ni un report."""
        watch = _watch(threshold=2)
        watch.observe(CHANNEL, deferred=4)
        watch.recorded(CHANNEL, watch.observe(CHANNEL, deferred=4))
        self.assertEqual(watch.alerted, [CHANNEL])

        watch.observe(CHANNEL, error=CAUSE)
        watch.observe(CHANNEL, error=CAUSE)

        self.assertEqual(watch.consecutive(CHANNEL), 2, "la séquence de reports tient")
        self.assertEqual(watch.alerted, [CHANNEL])
        self.assertEqual(
            watch.observe(CHANNEL, deferred=4),
            scan_backlog.ACTION_SILENT,
            "l'alerte de report est déjà ouverte",
        )

    def test_both_alerts_can_be_open_on_the_same_channel(self) -> None:
        """Un canal peut publier plus vite que son budget **et** être injoignable."""
        watch = _watch(threshold=2)
        watch.observe(CHANNEL, deferred=4)
        watch.recorded(CHANNEL, watch.observe(CHANNEL, deferred=4))
        watch.observe(CHANNEL, error=CAUSE)
        watch.recorded(CHANNEL, watch.observe(CHANNEL, error=CAUSE))

        self.assertEqual(watch.alerted, [CHANNEL], "signalé une fois, pas deux")
        self.assertEqual(watch.failing, [CHANNEL])

    def test_each_channel_fails_apart(self) -> None:
        watch = _watch(threshold=2)
        watch.observe(CHANNEL, error=CAUSE)
        watch.observe("autre_canal", error=CAUSE)
        watch.observe(CHANNEL, error=CAUSE)

        self.assertEqual(watch.failing, [])
        self.assertEqual(watch.failures(CHANNEL), 2)
        self.assertEqual(watch.failures("autre_canal"), 1)

    def test_a_channel_that_is_no_longer_scanned_forgets_its_failures(self) -> None:
        watch = _watch(threshold=2)
        watch.observe(CHANNEL, error=CAUSE)
        watch.observe(CHANNEL, error=CAUSE)

        self.assertEqual(watch.retain(["autre_canal"]), [CHANNEL])
        self.assertEqual(watch.failures(CHANNEL), 0)
        self.assertEqual(watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SILENT)

    def test_a_sweep_that_measured_nothing_touches_neither_sequence(self) -> None:
        """Ni report ni échec : `None` n'est pas une observation."""
        watch = _watch(threshold=2)
        watch.observe(CHANNEL, deferred=4)

        self.assertEqual(watch.observe(CHANNEL), scan_backlog.ACTION_SILENT)

        self.assertEqual(watch.consecutive(CHANNEL), 1)
        self.assertEqual(watch.failures(CHANNEL), 0)

    def test_the_threshold_cannot_make_one_failure_a_symptom(self) -> None:
        watch = _watch(threshold=1)

        self.assertEqual(watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SILENT)
        self.assertEqual(watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SCAN_FAILING)

    def test_the_reminders_space_out_instead_of_repeating(self) -> None:
        """Un rappel à cadence fixe est une alerte qui ne s'arrête jamais."""
        watch = _watch(threshold=2, reminder_every=3, abandon_after_reminders=9)
        for _ in range(2):
            watch.recorded(CHANNEL, watch.observe(CHANNEL, error=CAUSE))
        self.assertEqual(watch.failing, [CHANNEL], "l'alerte est ouverte")

        # Le premier rappel arrive après `reminder_every` balayages, comme avant.
        self.assertEqual(watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SILENT)
        self.assertEqual(watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SILENT)
        self.assertEqual(
            watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SCAN_REMINDER
        )
        watch.recorded(CHANNEL, scan_backlog.ACTION_SCAN_REMINDER)

        seen = [watch.observe(CHANNEL, error=CAUSE) for _ in range(6)]

        self.assertEqual(
            seen[:5],
            [scan_backlog.ACTION_SILENT] * 5,
            "le même échec, trois balayages plus tard, n'est plus un rappel",
        )
        self.assertEqual(seen[5], scan_backlog.ACTION_SCAN_REMINDER)

    def test_an_abandoned_channel_stops_being_alerted_about(self) -> None:
        """Le même message au dixième jour ne dit plus rien de neuf : on se tait."""
        watch = _watch(threshold=2, reminder_every=2, abandon_after_reminders=2)

        sent = []
        for _ in range(80):
            action = watch.observe(CHANNEL, error=CAUSE)
            watch.recorded(CHANNEL, action)
            if action != scan_backlog.ACTION_SILENT:
                sent.append(action)

        self.assertEqual(
            sent,
            [
                scan_backlog.ACTION_SCAN_FAILING,
                scan_backlog.ACTION_SCAN_REMINDER,
                scan_backlog.ACTION_SCAN_REMINDER,
                scan_backlog.ACTION_SCAN_ABANDONED,
            ],
            "deux rappels espacés, un dernier message, puis le silence",
        )
        self.assertEqual(watch.abandoned, [CHANNEL])
        self.assertEqual(watch.alerted, [], "on ne le signale plus")
        self.assertEqual(watch.failing, [])

    def test_a_reminder_that_did_not_leave_does_not_shorten_the_trial(self) -> None:
        """Un rappel que personne n'a reçu ne fait pas avancer l'épreuve."""
        watch = _watch(threshold=2, reminder_every=2, abandon_after_reminders=1)
        for _ in range(2):
            watch.recorded(CHANNEL, watch.observe(CHANNEL, error=CAUSE))

        self.assertEqual(watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SILENT)
        self.assertEqual(
            watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SCAN_REMINDER
        )
        # L'envoi a échoué : le balayage suivant redit le même rappel — et l'abandon
        # n'est pas plus proche pour autant.
        self.assertEqual(
            watch.observe(CHANNEL, error=CAUSE),
            scan_backlog.ACTION_SCAN_REMINDER,
            "toujours un rappel, pas encore un abandon",
        )
        self.assertEqual(watch.abandoned, [])

    def test_only_the_reading_is_abandoned_never_the_report(self) -> None:
        """Un canal qui reporte est **vivant** : c'est le budget qui ne suit pas."""
        watch = _watch(threshold=2, reminder_every=2, abandon_after_reminders=1)

        sent = []
        for _ in range(40):
            action = watch.observe(CHANNEL, deferred=5)
            watch.recorded(CHANNEL, action)
            if action != scan_backlog.ACTION_SILENT:
                sent.append(action)

        self.assertNotIn(scan_backlog.ACTION_SCAN_ABANDONED, sent)
        self.assertNotIn(scan_backlog.ACTION_SCAN_REVIVED, sent)
        self.assertGreater(
            sent.count(scan_backlog.ACTION_REMINDER), 1, "on continue de rappeler"
        )

    def test_a_read_after_an_abandon_lifts_it_and_the_watch_restarts(self) -> None:
        """Le silence n'est pas une condamnation : un canal qui repart est suivi."""
        watch = _watch(threshold=2, reminder_every=2, abandon_after_reminders=1)
        for _ in range(12):
            watch.recorded(CHANNEL, watch.observe(CHANNEL, error=CAUSE))
        self.assertEqual(watch.abandoned, [CHANNEL])
        self.assertEqual(
            watch.failures(CHANNEL), 12, "la durée des échecs continue de s'allonger"
        )

        self.assertEqual(watch.observe(CHANNEL, deferred=0), scan_backlog.ACTION_SCAN_REVIVED)
        watch.recorded(CHANNEL, scan_backlog.ACTION_SCAN_REVIVED)

        self.assertEqual(watch.abandoned, [])
        self.assertEqual(watch.failures(CHANNEL), 0)
        self.assertEqual(
            watch.observe(CHANNEL, deferred=0),
            scan_backlog.ACTION_SILENT,
            "la relance s'annonce une fois",
        )
        # La veille repart de zéro : un nouvel échec se signale au seuil, pas avant.
        self.assertEqual(watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SILENT)
        self.assertEqual(watch.observe(CHANNEL, error=CAUSE), scan_backlog.ACTION_SCAN_FAILING)

    def test_a_revival_that_could_not_be_sent_is_redited(self) -> None:
        """Personne ne l'a lue : l'abandon reste lisible, avec sa durée vraie."""
        watch = _watch(threshold=2, reminder_every=2, abandon_after_reminders=1)
        for _ in range(12):
            watch.recorded(CHANNEL, watch.observe(CHANNEL, error=CAUSE))

        self.assertEqual(watch.observe(CHANNEL, deferred=0), scan_backlog.ACTION_SCAN_REVIVED)
        self.assertEqual(watch.observe(CHANNEL, deferred=0), scan_backlog.ACTION_SCAN_REVIVED)

        self.assertEqual(watch.failures(CHANNEL), 12, "la durée ne se prolonge pas")
        self.assertEqual(watch.abandoned, [CHANNEL], "rien n'a été lu")

    def test_a_channel_that_is_no_longer_scanned_forgets_its_abandon(self) -> None:
        watch = _watch(threshold=2, reminder_every=2, abandon_after_reminders=1)
        for _ in range(12):
            watch.recorded(CHANNEL, watch.observe(CHANNEL, error=CAUSE))
        self.assertEqual(watch.abandoned, [CHANNEL])

        self.assertEqual(watch.retain(["autre_canal"]), [CHANNEL])

        self.assertEqual(watch.abandoned, [])


class FormatTest(unittest.TestCase):
    """Le message : ce qui est constaté, depuis quand, et où agir."""

    def _alert(self, action=scan_backlog.ACTION_BLOCKED, cap=10):
        return scan_backlog.format_report(
            channel=CHANNEL,
            action=action,
            deferred=12,
            sweeps=4,
            peak=14,
            cap=cap,
        )

    def _scan(self, action=scan_backlog.ACTION_SCAN_FAILING, error=CAUSE, sweeps=4):
        return scan_backlog.format_report(
            channel=CHANNEL,
            action=action,
            sweeps=sweeps,
            error=error,
            cap=10,
        )

    def test_the_alert_names_the_channel_and_the_observations(self) -> None:
        text = self._alert()

        self.assertIn(f"@{CHANNEL}", text)
        self.assertIn("4 balayage(s) d'affilée", text)
        self.assertIn("reportées au dernier balayage : 12", text)
        self.assertIn("plus haut vu depuis le début du blocage : 14", text)
        self.assertIn("plafond effectif : 10 extraction(s)", text)

    def test_the_alert_says_what_to_do(self) -> None:
        """Une alerte sans issue se lit comme du bruit."""
        text = self._alert()

        self.assertIn("/channels", text)
        self.assertIn("/channels max", text)
        self.assertIn("/transcribe", text)

    def test_the_alert_explains_why_a_report_is_not_always_a_failure(self) -> None:
        """Sinon l'opérateur cherche une panne là où il y a une file d'attente."""
        self.assertIn("Un report isolé est normal", self._alert())

    def test_the_message_carries_no_markup(self) -> None:
        """Le bot envoie sans `parse_mode` : les astérisques s'afficheraient."""
        text = (
            self._alert()
            + scan_backlog.format_report(
                channel=CHANNEL,
                action=scan_backlog.ACTION_RESOLVED,
                deferred=0,
                sweeps=4,
                peak=14,
                cap=10,
            )
            + self._scan()
            + self._scan(scan_backlog.ACTION_SCAN_REMINDER)
            + self._scan(scan_backlog.ACTION_SCAN_RECOVERED, error=None)
            + self._scan(scan_backlog.ACTION_SCAN_ABANDONED, error=None)
            + self._scan(scan_backlog.ACTION_SCAN_REVIVED, error=None)
        )

        self.assertNotIn("*", text)
        self.assertNotIn("`", text)
        self.assertLess(len(text), 4096, "un message Telegram en porte 4096")

    def test_the_worsening_and_the_reminder_say_which_they_are(self) -> None:
        self.assertIn("ça s'aggrave", self._alert(scan_backlog.ACTION_WORSENED))
        self.assertIn("toujours bloqué", self._alert(scan_backlog.ACTION_REMINDER))

    def test_without_a_cap_the_line_disappears_instead_of_lying(self) -> None:
        """« Plafond : sans plafond » ne peut pas reporter : c'est incohérent."""
        text = self._alert(cap=0)

        self.assertNotIn("plafond effectif", text)
        self.assertIn(f"@{CHANNEL}", text)

    def test_the_resolution_closes_the_alert_with_its_duration(self) -> None:
        text = scan_backlog.format_report(
            channel=CHANNEL,
            action=scan_backlog.ACTION_RESOLVED,
            deferred=0,
            sweeps=4,
            peak=14,
            cap=10,
        )

        self.assertIn("✅", text)
        self.assertIn("résorbé", text)
        self.assertIn("4 balayage(s) d'affilée", text)
        self.assertIn("jusqu'à 14", text)
        self.assertNotIn("⚠️", text)

    def test_the_failing_alert_names_the_channel_the_duration_and_the_cause(self) -> None:
        """Le motif est la seule chose qu'un zéro ne peut pas dire."""
        text = self._scan()

        self.assertIn(f"@{CHANNEL}", text)
        self.assertIn("4 balayage(s) d'affilée", text)
        self.assertIn(CAUSE, text)
        self.assertIn("🛑", text)

    def test_the_failing_alert_says_the_channel_is_not_silent(self) -> None:
        """Sinon l'opérateur cherche une panne côté canal, et non côté lecture."""
        text = self._scan()

        self.assertIn("n'est pas forcément calme", text)
        self.assertIn("/channels", text)
        self.assertIn("/refresh_data", text)
        self.assertIn(f"https://t.me/s/{CHANNEL}", text)

    def test_the_failing_alert_does_not_borrow_the_report_counters(self) -> None:
        """« Reportées : 0 » dans une alerte de lecture serait un chiffre inventé."""
        text = self._scan()

        self.assertNotIn("reportée", text)
        self.assertNotIn("plafond", text)

    def test_the_reminder_says_which_it_is(self) -> None:
        self.assertIn("toujours en échec", self._scan(scan_backlog.ACTION_SCAN_REMINDER))

    def test_the_recovery_says_the_channel_reads_again(self) -> None:
        text = self._scan(scan_backlog.ACTION_SCAN_RECOVERED, error=None)

        self.assertIn("✅", text)
        self.assertIn(f"@{CHANNEL}", text)
        self.assertIn("4 balayage(s) d'affilée", text)
        self.assertIn("rien à faire", text)
        self.assertNotIn("🛑", text)

    def test_the_abandon_says_the_alerting_stops_and_how_to_come_back(self) -> None:
        """Un abandon muet serait indiscernable d'une panne du bot."""
        text = self._scan(scan_backlog.ACTION_SCAN_ABANDONED)

        self.assertIn("🪦", text)
        self.assertIn("tenu pour mort", text)
        self.assertIn(f"@{CHANNEL}", text)
        self.assertIn("4 balayage(s) d'affilée", text)
        self.assertIn(CAUSE, text)
        self.assertIn("/channels", text)
        self.assertIn(f"https://t.me/s/{CHANNEL}", text)
        self.assertIn("lèvera l'abandon", text, "l'abandon doit dire comment il tombe")
        self.assertNotIn("reportée", text, "il n'emprunte pas les compteurs du report")

    def test_the_revival_says_the_watch_is_back(self) -> None:
        text = self._scan(scan_backlog.ACTION_SCAN_REVIVED, error=None)

        self.assertIn("✅", text)
        self.assertIn(f"@{CHANNEL}", text)
        self.assertIn("L'abandon est levé", text)
        self.assertIn("de nouveau signalés", text)
        self.assertNotIn("🪦", text)

    def test_the_cause_is_flattened_to_one_line(self) -> None:
        """Une exception multiligne casserait la puce et le reste du message."""
        text = self._scan(
            error="ConnectError:\n  [Errno 11001]\n  getaddrinfo failed"
        )

        self.assertIn("ConnectError: [Errno 11001] getaddrinfo failed", text)
        self.assertEqual(text.count("dernier motif"), 1)

    def test_an_absent_cause_removes_the_line_instead_of_lying(self) -> None:
        """Une alerte qui dirait « motif : » sans motif n'aide personne."""
        text = self._scan(error=None)

        self.assertNotIn("dernier motif", text)
        self.assertIn(f"@{CHANNEL}", text)

    def test_an_endless_cause_is_truncated(self) -> None:
        """Un motif de dix mille caractères noierait ce qu'il faut retenir."""
        text = self._scan(error="boom " * 4000)

        self.assertLess(len(text), 4096, "un message Telegram en porte 4096")
        self.assertIn("…", text)


class SweepTest(unittest.IsolatedAsyncioTestCase):
    """Un balayage complet : constat, décision, envoi — et ce qui n'est pas envoyé."""

    async def test_a_blocked_channel_alerts_the_admin_chat(self) -> None:
        watch = _watch(threshold=3)
        for _ in range(2):
            await _sweep({CHANNEL: 12}, watch=watch)

        result, recorder = await _sweep({CHANNEL: 12}, watch=watch)

        self.assertEqual(result["alerted"], [CHANNEL])
        self.assertEqual(recorder.sent[0][0], ADMIN)
        self.assertIn(f"@{CHANNEL}", recorder.sent[0][1])

    async def test_a_sweep_that_reports_nothing_sends_nothing(self) -> None:
        result, recorder = await _sweep({CHANNEL: 0})

        self.assertEqual(result["actions"], {CHANNEL: scan_backlog.ACTION_SILENT})
        self.assertEqual(recorder.sent, [])

    async def test_a_quiet_sweep_between_two_reports_breaks_the_sequence(self) -> None:
        """C'est la consécution qui compte, pas le nombre de reports cumulés."""
        watch = _watch(threshold=2)
        await _sweep({CHANNEL: 5}, watch=watch)
        await _sweep({CHANNEL: 0}, watch=watch)
        result, recorder = await _sweep({CHANNEL: 5}, watch=watch)

        self.assertEqual(result["alerted"], [])
        self.assertEqual(recorder.sent, [])

    async def test_the_resolution_is_sent_once(self) -> None:
        watch = _watch(threshold=2)
        recorder = _Recorder()
        await _sweep({CHANNEL: 6}, watch=watch, recorder=recorder)
        await _sweep({CHANNEL: 6}, watch=watch, recorder=recorder)

        result, _ = await _sweep({CHANNEL: 0}, watch=watch, recorder=recorder)

        self.assertEqual(result["resolved"], [CHANNEL])
        self.assertEqual(result["alerted"], [CHANNEL], "une résolution est un envoi")
        self.assertIn("résorbé", recorder.sent[-1][1])
        self.assertEqual(len(recorder.sent), 2, "l'alerte, puis sa fermeture")
        await _sweep({CHANNEL: 0}, watch=watch, recorder=recorder)
        self.assertEqual(len(recorder.sent), 2, "la fermeture ne se répète pas")

    async def test_a_reminder_comes_back_after_the_interval(self) -> None:
        watch = _watch(threshold=2, reminder_every=2)
        recorder = _Recorder()
        await _sweep({CHANNEL: 4}, watch=watch, recorder=recorder)
        await _sweep({CHANNEL: 4}, watch=watch, recorder=recorder)

        for _ in range(2):
            await _sweep({CHANNEL: 4}, watch=watch, recorder=recorder)

        self.assertEqual(len(recorder.sent), 2)
        self.assertIn("toujours bloqué", recorder.sent[-1][1])

    async def test_two_blocked_channels_get_one_message_each(self) -> None:
        """Un message par canal : chacun est actionnable, et rien n'atteint 4096."""
        watch = _watch(threshold=2)
        counts = {"canal_a": 8, "canal_b": 5}

        await _sweep(counts, watch=watch)
        result, recorder = await _sweep(counts, watch=watch)

        self.assertEqual(result["alerted"], ["canal_a", "canal_b"])
        self.assertEqual(len(recorder.sent), 2)
        self.assertIn("canal_a", recorder.sent[0][1])
        self.assertIn("canal_b", recorder.sent[1][1])

    async def test_without_an_admin_chat_the_silence_is_explained(self) -> None:
        """Rien n'est envoyé, et rien n'est mémorisé : personne n'a rien lu."""
        watch = _watch(threshold=2)
        await _sweep({CHANNEL: 4}, watch=watch, chat_id="")

        result, recorder = await _sweep({CHANNEL: 4}, watch=watch, chat_id="")

        self.assertEqual(recorder.sent, [])
        self.assertEqual(result["reason"], "no_admin_chat")
        self.assertEqual(result["alerted"], [])
        self.assertEqual(watch.alerted, [], "l'anomalie n'a été vue par personne")

    async def test_the_alert_waits_for_a_chat_that_exists(self) -> None:
        """Sans destinataire, l'alerte n'est pas perdue : elle part dès qu'il y en a un."""
        watch = _watch(threshold=2)
        await _sweep({CHANNEL: 4}, watch=watch, chat_id="")
        await _sweep({CHANNEL: 4}, watch=watch, chat_id="")

        result, recorder = await _sweep({CHANNEL: 4}, watch=watch)

        self.assertEqual(result["alerted"], [CHANNEL])
        self.assertEqual(len(recorder.sent), 1)

    async def test_a_failed_send_is_retried_on_the_next_sweep(self) -> None:
        """Sinon la veille se tairait sur un blocage que personne n'a vu."""
        watch = _watch(threshold=2)
        failing = _Recorder(fail=True)
        await _sweep({CHANNEL: 4}, watch=watch, recorder=failing)
        first, _ = await _sweep({CHANNEL: 4}, watch=watch, recorder=failing)

        self.assertEqual(first["reason"], "send_failed")
        self.assertEqual(first["alerted"], [])
        self.assertEqual(watch.alerted, [])

        second, recorder = await _sweep({CHANNEL: 4}, watch=watch)

        self.assertEqual(second["alerted"], [CHANNEL])
        self.assertEqual(len(recorder.sent), 1)

    async def test_a_failed_channel_does_not_close_an_open_alert(self) -> None:
        """`None` : rien n'a été mesuré — ce n'est pas « plus de report »."""
        watch = _watch(threshold=2)
        await _sweep({CHANNEL: 4}, watch=watch)
        await _sweep({CHANNEL: 4}, watch=watch)
        self.assertEqual(watch.alerted, [CHANNEL])

        result, recorder = await _sweep({CHANNEL: None}, watch=watch)

        self.assertEqual(recorder.sent, [])
        self.assertEqual(result["actions"], {})
        self.assertEqual(watch.alerted, [CHANNEL], "l'alerte reste ouverte")
        self.assertEqual(watch.consecutive(CHANNEL), 2, "et la séquence est intacte")

    async def test_a_channel_dropped_from_the_sweep_is_forgotten(self) -> None:
        watch = _watch(threshold=2)
        await _sweep({CHANNEL: 4}, watch=watch)
        await _sweep({CHANNEL: 4}, watch=watch)

        await _sweep({"autre_canal": 0}, watch=watch)

        self.assertEqual(watch.alerted, [])
        result, recorder = await _sweep({CHANNEL: 4}, watch=watch)
        self.assertEqual(result["alerted"], [], "une nouvelle séquence commence")
        self.assertEqual(recorder.sent, [])

    async def test_a_resolution_that_could_not_be_sent_does_not_carry_into_the_next(self) -> None:
        """Sinon la séquence suivante hériterait de la durée et du seuil de la précédente."""
        watch = _watch(threshold=2)
        # L'alerte doit d'abord **partir** : sans destinataire, il n'y a pas de
        # blocage ouvert, donc rien à fermer.
        await _sweep({CHANNEL: 5}, watch=watch)
        await _sweep({CHANNEL: 5}, watch=watch)
        failing = _Recorder(fail=True)

        resolution, _ = await _sweep({CHANNEL: 0}, watch=watch, recorder=failing)

        self.assertEqual(resolution["resolved"], [CHANNEL])
        self.assertEqual(resolution["alerted"], [])
        self.assertEqual(resolution["reason"], "send_failed")
        self.assertEqual(watch.alerted, [CHANNEL], "rien n'a été lu : l'alerte reste ouverte")

        _, recorder = await _sweep({CHANNEL: 4}, watch=watch)

        self.assertEqual(watch.consecutive(CHANNEL), 1, "une nouvelle séquence commence")
        self.assertEqual(recorder.sent, [], "le seuil ne s'hérite pas")

    async def test_the_cap_travels_with_the_alert(self) -> None:
        """Le message dit le plafond effectif : c'est lui qu'on va relever."""
        watch = _watch(threshold=2)
        await _sweep({CHANNEL: 4}, watch=watch, cap=7)

        _, recorder = await _sweep({CHANNEL: 4}, watch=watch, cap=7)

        self.assertIn("plafond effectif : 7", recorder.sent[0][1])


class ScanFailureSweepTest(unittest.IsolatedAsyncioTestCase):
    """Le balayage d'un canal qui ne se lit plus : ce qui part, et ce qui est noté."""

    async def test_a_channel_whose_read_fails_alerts_the_admin_chat(self) -> None:
        watch = _watch(threshold=2)
        await _sweep({CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch)

        result, recorder = await _sweep({CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch)

        self.assertEqual(result["alerted"], [CHANNEL])
        self.assertEqual(result["failing"], [CHANNEL])
        self.assertEqual(watch.alerted, [CHANNEL], "la lecture compte parmi les alertes")
        self.assertEqual(recorder.sent[0][0], ADMIN)
        self.assertIn(CAUSE, recorder.sent[0][1])
        self.assertIn(
            "2 balayage(s) d'affilée",
            recorder.sent[0][1],
            "la durée annoncée est celle des échecs, pas des reports",
        )

    async def test_a_failed_read_is_not_a_missing_measurement(self) -> None:
        """`errors` distingue l'échec du silence : sans lui, rien n'est observé."""
        watch = _watch(threshold=2)

        result, recorder = await _sweep({CHANNEL: None}, watch=watch)

        self.assertEqual(result["actions"], {})
        self.assertEqual(recorder.sent, [])
        self.assertEqual(watch.failures(CHANNEL), 0)

    async def test_a_failed_read_does_not_close_an_open_report_alert(self) -> None:
        """L'inverse est le piège : un échec tôt ferait croire à un canal calmé."""
        watch = _watch(threshold=2)
        await _sweep({CHANNEL: 6}, watch=watch)
        _, recorder = await _sweep({CHANNEL: 6}, watch=watch)
        self.assertEqual(len(recorder.sent), 1)

        result, recorder = await _sweep({CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch)

        self.assertEqual(recorder.sent, [], "rien à dire : l'échec est encore sous le seuil")
        self.assertEqual(result["resolved"], [])
        self.assertEqual(watch.alerted, [CHANNEL])
        self.assertEqual(watch.consecutive(CHANNEL), 2)

    async def test_the_recovery_is_sent_and_closes_the_alert(self) -> None:
        watch = _watch(threshold=2)
        await _sweep({CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch)
        await _sweep({CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch)
        self.assertEqual(watch.failing, [CHANNEL])

        result, recorder = await _sweep({CHANNEL: 0}, watch=watch)

        self.assertEqual(result["resolved"], [CHANNEL])
        self.assertIn("✅", recorder.sent[0][1])
        self.assertEqual(watch.failing, [])

        _, recorder = await _sweep({CHANNEL: 0}, watch=watch)
        self.assertEqual(recorder.sent, [], "la reprise s'annonce une fois")

    async def test_without_an_admin_chat_the_failure_is_not_memorized(self) -> None:
        """Aucune alerte lue : le balayage suivant doit pouvoir réessayer."""
        watch = _watch(threshold=2)
        await _sweep({CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch)

        result, recorder = await _sweep(
            {CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch, chat_id=None
        )

        self.assertEqual(recorder.sent, [])
        self.assertEqual(result["reason"], "no_admin_chat")
        self.assertEqual(watch.failing, [], "rien n'a été signalé à personne")

    async def test_a_failed_send_of_the_failure_alert_is_retried(self) -> None:
        watch = _watch(threshold=2)
        await _sweep({CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch)

        first, _ = await _sweep(
            {CHANNEL: None},
            errors={CHANNEL: CAUSE},
            watch=watch,
            recorder=_Recorder(fail=True),
        )
        self.assertEqual(first["reason"], "send_failed")
        self.assertEqual(watch.failing, [])

        second, recorder = await _sweep(
            {CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch
        )

        self.assertEqual(second["alerted"], [CHANNEL], "le même message repart")
        self.assertEqual(len(recorder.sent), 1)

    async def test_two_unreadable_channels_get_one_message_each(self) -> None:
        watch = _watch(threshold=2)
        errors = {"canal_a": CAUSE, "canal_b": CAUSE}
        await _sweep({"canal_a": None, "canal_b": None}, errors=errors, watch=watch)

        result, recorder = await _sweep(
            {"canal_a": None, "canal_b": None}, errors=errors, watch=watch
        )

        self.assertEqual(result["failing"], ["canal_a", "canal_b"])
        self.assertEqual(len(recorder.sent), 2)
        self.assertIn("@canal_a", recorder.sent[0][1])
        self.assertIn("@canal_b", recorder.sent[1][1])

    async def test_a_report_that_breaks_off_the_failure_sequence_wins_the_message(self) -> None:
        """Un seul message par canal : la reprise prime, le report se redit ensuite."""
        watch = _watch(threshold=2)
        await _sweep({CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch)
        await _sweep({CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch)

        result, recorder = await _sweep({CHANNEL: 9}, watch=watch)

        self.assertEqual(len(recorder.sent), 1)
        self.assertIn("✅", recorder.sent[0][1])
        self.assertEqual(result["failing"], [])
        self.assertEqual(watch.consecutive(CHANNEL), 1, "le report, lui, est bien observé")

    async def test_an_abandoned_channel_stops_producing_messages(self) -> None:
        """C'est l'objet du mécanisme : la veille se tait, et le dit une fois."""
        watch = _watch(threshold=2, reminder_every=2, abandon_after_reminders=1)
        announcements = 0
        last = None
        for _ in range(30):
            result, recorder = await _sweep(
                {CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch
            )
            if result["abandoned"]:
                announcements += 1
                last = (result, recorder)

        self.assertEqual(announcements, 1, "l'abandon s'annonce une fois")
        result, recorder = last
        self.assertEqual(result["abandoned"], [CHANNEL])
        self.assertEqual(result["failing"], [], "il n'est plus signalé en échec")
        self.assertEqual(len(recorder.sent), 1)
        self.assertIn("tenu pour mort", recorder.sent[0][1])

        result, recorder = await _sweep({CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch)

        self.assertEqual(recorder.sent, [], "plus rien ne part : l'alerte est finie")
        self.assertEqual(result["actions"][CHANNEL], scan_backlog.ACTION_SILENT)
        self.assertEqual(result["abandoned"], [])

    async def test_a_read_after_an_abandon_is_announced_as_a_revival(self) -> None:
        watch = _watch(threshold=2, reminder_every=2, abandon_after_reminders=1)
        for _ in range(12):
            await _sweep({CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch)
        self.assertEqual(watch.abandoned, [CHANNEL])

        result, recorder = await _sweep({CHANNEL: 3}, watch=watch)

        self.assertEqual(
            result["resolved"], [CHANNEL], "une reprise est un retour à la normale"
        )
        self.assertEqual(result["abandoned"], [])
        self.assertIn("L'abandon est levé", recorder.sent[0][1])
        self.assertEqual(watch.abandoned, [])

    async def test_an_abandon_that_could_not_leave_is_redited(self) -> None:
        """Personne n'a lu l'abandon : il n'est pas acquis, et se redit tel quel."""
        watch = _watch(threshold=2, reminder_every=2, abandon_after_reminders=1)
        recorder = _Recorder(fail_from=3)  # le signalement et son rappel passent, pas l'abandon
        for _ in range(12):
            result, _ = await _sweep(
                {CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch, recorder=recorder
            )

        self.assertEqual(result["reason"], "send_failed")
        self.assertEqual(len(recorder.sent), 2)
        self.assertEqual(watch.abandoned, [], "rien n'a été lu : l'abandon n'est pas acquis")

        result, recorder = await _sweep({CHANNEL: None}, errors={CHANNEL: CAUSE}, watch=watch)

        self.assertEqual(result["alerted"], [CHANNEL])
        self.assertIn("tenu pour mort", recorder.sent[0][1])
        self.assertEqual(watch.abandoned, [CHANNEL])


class AutoLoopWiringTest(unittest.TestCase):
    """`auto_loop` n'est pas importable ici (`feedparser` absent) : on le lit."""

    def _source(self) -> str:
        return AUTO_LOOP.read_text(encoding="utf-8")

    def test_the_sweep_feeds_the_backlog_with_every_channel_count(self) -> None:
        source = self._source()

        self.assertIn("deferred_by_channel[channel] = deferred", source)
        self.assertIn("scan_backlog.observe_sweep(", source)
        self.assertIn("deferred_by_channel,", source)
        self.assertIn("errors=errors_by_channel,", source)
        self.assertIn("watch=_scan_backlog,", source)

    def test_a_read_that_failed_is_passed_as_a_cause(self) -> None:
        """Sans le motif, un canal injoignable se lit comme un canal calme."""
        source = self._source()

        self.assertIn("errors_by_channel[channel] = error", source)
        self.assertIn('stats.get("error")', source)

    def test_a_channel_that_raises_is_named_too(self) -> None:
        """Le scraper rend le motif ; une exception qui remonte doit laisser le sien."""
        source = self._source()

        self.assertIn('errors_by_channel[channel] = f"{type(e).__name__}: {e}"', source)

    def test_a_read_that_failed_is_not_logged_as_a_quiet_channel(self) -> None:
        """La ligne de journal ne doit pas annoncer « 0 messages » sur une panne."""
        source = self._source()

        self.assertLess(
            source.index("errors_by_channel[channel] = error"),
            source.index("canal Telegram @{channel}: {stats['insights']}"),
        )
        self.assertIn("aucune lecture", source)

    def test_the_worker_does_not_decide_when_an_alert_is_due(self) -> None:
        """Le motif est transmis, pas jugé : le seuil vit dans le module."""
        source = self._source()

        self.assertNotIn("failures", source)

    def test_a_channel_in_error_is_reported_as_unmeasured(self) -> None:
        """Un balayage qui lève ne dit rien du retard : ni report, ni résolution."""
        source = self._source()
        body = source.split("for channel in channels:", 1)[1].split("_last_telegram_scan", 1)[0]

        self.assertIn("deferred_by_channel[channel] = None", body)

    def test_the_backlog_outlives_a_single_sweep(self) -> None:
        """L'état est au niveau du module : une suite ne tient pas dans une fonction."""
        self.assertIn("\n_scan_backlog = scan_backlog.ScanBacklog()", self._source())

    def test_the_worker_does_not_reimplement_the_decision(self) -> None:
        """Le compte des balayages d'affilée vit dans le module, pas dans la boucle."""
        source = self._source()

        self.assertNotIn("consecutive", source)
        self.assertNotIn("sweeps_since_alert", source)

    def test_the_backlog_is_documented(self) -> None:
        """Une alerte qu'on ne sait pas d'où vient ne se règle pas."""
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn("scan_backlog", readme)
        self.assertIn("n'a rien lu", readme, "l'alerte de lecture doit être documentée")
        self.assertIn("error", readme, "le motif rendu par le scraper aussi")
        self.assertIn("l'abandon", readme, "l'abandon progressif doit être documenté")


if __name__ == "__main__":
    unittest.main()
