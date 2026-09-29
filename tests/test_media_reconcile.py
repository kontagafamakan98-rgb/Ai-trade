"""La veille média de l'auto-loop (`workers/media_reconcile.py`).

Ce qui se teste ici ne se voit pas dans un cycle heureux :

* **rien n'est supprimé, jamais** — le module ne connaît même pas la fonction de
  suppression : un parcours peut se tromper (un import en cours ressemble à un
  orphelin), et une veille qui supprime peut détruire la seule copie d'un média ;
* **on ne répète pas la même alerte** à chaque cycle, mais on reparle si ça
  empire, et on annonce le retour à la normale — sans quoi une alerte ne se
  referme jamais ;
* **une alerte qui n'est pas partie n'est pas notée comme envoyée** : sinon la
  veille se tairait sur une anomalie que personne n'a vue ;
* **un média manquant alerte dès le premier**, alors qu'un orphelin isolé est
  banal : le seuil ne porte que sur les orphelins.

`auto_loop` n'est pas importable dans cet environnement (`feedparser` absent), donc
son câblage est vérifié en **relisant sa source** — même méthode que
`tests/test_telegram_scan_config.py`.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import unittest
from unittest import mock

from core import config_runtime
from database import media_store, settings
from notifications import notify
from tests import supabase_double
from workers import media_reconcile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
AUTO_LOOP = REPO_ROOT / "workers" / "auto_loop.py"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
README = REPO_ROOT / "README.md"

ROW = {
    "id": "m1",
    "storage_path": "telegram/@signals/1-a.jpg",
    "telegram_file_id": "AgAC-1",
    "chat_id": "@signals",
    "message_id": 1,
}


class _Recorder:
    """Doublures du parcours et de l'envoi."""

    def __init__(self, *, orphans=(), missing=(), fail_scan=None, fail_send=None):
        self.orphans = list(orphans)
        self.missing = [dict(row) for row in missing]
        self.fail_scan = fail_scan
        self.fail_send = fail_send
        self.scans = 0
        self.sent: list = []

    def store(self, prefix=""):
        self.scans += 1
        if self.fail_scan is not None:
            raise self.fail_scan
        return {"orphans": list(self.orphans), "missing": [dict(r) for r in self.missing]}

    async def notify(self, chat_id, text):
        self.sent.append((chat_id, text))
        if self.fail_send is not None:
            raise self.fail_send
        return True


async def _cycle(recorder, **kwargs):
    params = {"threshold": 2, "chat_id": "42"}
    params.update(kwargs)
    return await media_reconcile.reconcile_once(
        store=recorder.store, notify=recorder.notify, **params
    )


class ThresholdTest(unittest.TestCase):
    """Qui alerte, et à partir de quand."""

    def test_an_isolated_orphan_stays_under_the_threshold(self):
        self.assertFalse(media_reconcile._above_threshold(2, 0, 2))
        self.assertTrue(media_reconcile._above_threshold(3, 0, 2))

    def test_a_threshold_of_zero_alerts_on_the_first_orphan(self):
        """Le seuil ne peut pas servir à rendre une anomalie muette."""
        self.assertFalse(media_reconcile._above_threshold(0, 0, 0))
        self.assertTrue(media_reconcile._above_threshold(1, 0, 0))

    def test_one_missing_media_alerts_whatever_the_threshold(self):
        """C'est l'application qui est cassée, pas seulement du stockage occupé."""
        for threshold in (0, 10, 1000):
            with self.subTest(threshold=threshold):
                self.assertTrue(media_reconcile._above_threshold(0, 1, threshold))


class WatchStateTest(unittest.TestCase):
    """L'état entre deux cycles : parler, se taire, ou refermer."""

    def setUp(self) -> None:
        self.watch = media_reconcile.ReconcileWatch(threshold=3)

    def _observe(self, orphans, missing=0):
        return self.watch.observe(orphans=orphans, missing=missing)

    def test_the_first_crossing_alerts(self):
        self.assertEqual(self._observe(4), media_reconcile.ACTION_ALERT)

    def test_a_steady_anomaly_stays_silent(self):
        """Répéter la même alerte chaque heure est du bruit, pas une information."""
        self.watch.sent(media_reconcile.ACTION_ALERT, orphans=4, missing=0)
        self.assertEqual(self._observe(4), media_reconcile.ACTION_SILENT, "même compte")
        # Moins mauvais, mais toujours au-dessus du seuil : rien de neuf à dire.
        self.watch.sent(media_reconcile.ACTION_ALERT, orphans=5, missing=0)
        self.assertEqual(self._observe(4), media_reconcile.ACTION_SILENT, "en baisse, mais cassé")

    def test_a_worsening_anomaly_speaks_again(self):
        self.watch.sent(media_reconcile.ACTION_ALERT, orphans=4, missing=0)
        self.assertEqual(self._observe(5), media_reconcile.ACTION_WORSENED)

    def test_a_recovery_is_announced_once(self):
        """Sans cette annonce, personne ne saurait jamais que c'est réglé."""
        self.watch.sent(media_reconcile.ACTION_ALERT, orphans=4, missing=0)
        self.assertEqual(self._observe(0), media_reconcile.ACTION_RECOVERED)
        self.watch.sent(media_reconcile.ACTION_RECOVERED, orphans=0, missing=0)
        self.assertEqual(self._observe(0), media_reconcile.ACTION_SILENT)

    def test_a_quiet_watch_says_nothing(self):
        self.assertEqual(self._observe(0), media_reconcile.ACTION_SILENT)

    def test_a_second_crossing_alerts_again(self):
        self.watch.sent(media_reconcile.ACTION_ALERT, orphans=4, missing=0)
        self.watch.sent(media_reconcile.ACTION_RECOVERED, orphans=0, missing=0)
        self.assertEqual(self._observe(9), media_reconcile.ACTION_ALERT)

    def test_observing_alone_changes_nothing(self):
        """La décision est séparée de la mémoire : une alerte non partie se rejoue."""
        self.assertEqual(self._observe(4), media_reconcile.ACTION_ALERT)
        self.assertEqual(self._observe(4), media_reconcile.ACTION_ALERT)


class MessageTest(unittest.TestCase):
    """Ce que l'opérateur lit : de quoi agir, et par quel moyen."""

    def test_the_alert_names_both_senses_and_their_remedies(self):
        text = media_reconcile.format_alert(
            orphans=["scraper/a.jpg", "scraper/b.jpg"],
            missing=[ROW],
            threshold=1,
        )
        self.assertIn("🚨", text)
        self.assertIn("scraper/a.jpg", text)
        self.assertIn("telegram/@signals/1-a.jpg", text)
        self.assertIn("scripts/reconcile_media.py --delete", text)
        self.assertIn("/admin/media/missing/repair", text)
        self.assertLess(len(text), 4096, "un message Telegram en porte 4096")

    def test_a_missing_media_without_file_id_says_so(self):
        """Sans `file_id`, la réparation dépend de l'aperçu public : ça se dit."""
        row = {**ROW, "telegram_file_id": None}
        text = media_reconcile.format_alert(orphans=[], missing=[row], threshold=0)
        self.assertIn("sans `file_id`", text)

    def test_the_details_are_bounded_but_never_silently(self):
        """Une liste coupée net ferait croire qu'il n'y en a pas plus."""
        text = media_reconcile.format_alert(
            orphans=[f"scraper/objet-{index}.jpg" for index in range(9)],
            missing=[],
            threshold=0,
        )
        shown = [line for line in text.splitlines() if line.startswith("   • scraper/")]
        self.assertEqual(len(shown), media_reconcile.ALERT_DETAIL_LINES)
        self.assertIn("… et 4 autre(s)", text)

    def test_a_worsening_message_recalls_the_previous_count(self):
        text = media_reconcile.format_alert(
            orphans=["a", "b", "c"],
            missing=[],
            threshold=1,
            action=media_reconcile.ACTION_WORSENED,
            previous=(2, 0),
        )
        self.assertIn("ça empire", text)
        self.assertIn("contre 2 à la dernière alerte", text)

    def test_a_count_that_did_not_move_is_not_compared(self):
        """C'est l'autre sens qui a empiré : comparer celui-ci serait faux."""
        text = media_reconcile.format_alert(
            orphans=["a", "b", "c"],
            missing=[ROW],
            threshold=1,
            action=media_reconcile.ACTION_WORSENED,
            previous=(3, 0),
        )
        orphans_section = text.split("🗂️", 1)[0]
        self.assertNotIn("contre", orphans_section)
        self.assertIn("contre 0 à la dernière alerte", text)

    def test_the_recovery_message_closes_the_alert(self):
        text = media_reconcile.format_recovery(orphans=0, missing=0, threshold=5)
        self.assertIn("✅", text)
        self.assertIn("retour à la normale", text)
        self.assertIn("seuil 5", text)


class CycleTest(unittest.IsolatedAsyncioTestCase):
    """Un cycle : constat, décision, envoi — et ce qui ne se produit pas."""

    async def test_an_anomaly_is_alerted_to_the_admin_chat(self):
        recorder = _Recorder(orphans=["a", "b", "c"], missing=[ROW])
        result = await _cycle(recorder)
        self.assertTrue(result["alerted"])
        self.assertEqual(result["orphans"], 3)
        self.assertEqual(recorder.sent[0][0], "42")
        self.assertIn("🚨", recorder.sent[0][1])

    async def test_a_quiet_cycle_sends_nothing(self):
        recorder = _Recorder(orphans=["a", "b"])
        result = await _cycle(recorder)
        self.assertEqual(result["action"], media_reconcile.ACTION_SILENT)
        self.assertEqual(recorder.sent, [])

    async def test_the_same_anomaly_does_not_alert_twice(self):
        recorder = _Recorder(orphans=["a", "b", "c"])
        watch = media_reconcile.ReconcileWatch(2)
        await _cycle(recorder, watch=watch)
        await _cycle(recorder, watch=watch)
        self.assertEqual(len(recorder.sent), 1)

    async def test_a_missing_media_alone_raises_the_alert(self):
        """Un seul média manquant suffit : l'application affiche un fichier mort."""
        recorder = _Recorder(missing=[ROW])
        result = await _cycle(recorder)
        self.assertTrue(result["alerted"])

    async def test_a_recovery_is_announced(self):
        recorder = _Recorder(orphans=["a", "b", "c"])
        watch = media_reconcile.ReconcileWatch(2)
        await _cycle(recorder, watch=watch)
        recorder.orphans = []
        result = await _cycle(recorder, watch=watch)
        self.assertEqual(result["action"], media_reconcile.ACTION_RECOVERED)
        self.assertIn("retour à la normale", recorder.sent[-1][1])

    async def test_without_an_admin_chat_the_alert_is_not_lost_silently(self):
        """Une veille sans destinataire ne peut rien signaler : elle le dit."""
        recorder = _Recorder(orphans=["a", "b", "c"])
        result = await _cycle(recorder, chat_id="")
        self.assertFalse(result["alerted"])
        self.assertEqual(result["reason"], "no_admin_chat")
        self.assertEqual(recorder.sent, [])

    async def test_a_failed_send_is_retried_next_cycle(self):
        """Sinon la veille se tairait sur une anomalie que personne n'a vue."""
        recorder = _Recorder(orphans=["a", "b", "c"], fail_send=RuntimeError("Telegram down"))
        watch = media_reconcile.ReconcileWatch(2)
        first = await _cycle(recorder, watch=watch)
        self.assertFalse(first["alerted"])
        self.assertEqual(first["reason"], "send_failed")
        recorder.fail_send = None
        second = await _cycle(recorder, watch=watch)
        self.assertTrue(second["alerted"], "le cycle suivant doit réessayer")

    async def test_a_failed_scan_does_not_raise(self):
        """La veille ne doit pas emporter l'auto-loop avec elle."""
        recorder = _Recorder(fail_scan=RuntimeError("Storage injoignable"))
        result = await _cycle(recorder)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "scan_failed")
        self.assertEqual(recorder.sent, [])

    async def test_the_scan_happens_off_the_event_loop(self):
        """Le parcours est bloquant (réseau/DB) : il ne doit pas geler les autres."""
        recorder = _Recorder()
        threaded: list = []
        real = asyncio.to_thread

        async def spy(func, *args, **kwargs):
            threaded.append(getattr(func, "__name__", str(func)))
            return await real(func, *args, **kwargs)

        with mock.patch.object(media_reconcile.asyncio, "to_thread", spy):
            await _cycle(recorder)
        self.assertIn("store", threaded)

    async def test_the_module_cannot_delete_anything(self):
        """Le seul chemin qui supprime des octets n'est même pas connu de la veille."""
        source = pathlib.Path(media_reconcile.__file__).read_text(encoding="utf-8")
        for forbidden in ("delete_objects", "delete_media", "remove("):
            self.assertNotIn(forbidden, source)

    async def test_the_defaults_are_the_shared_ones(self):
        """Sans injection, la veille parle au chat admin et parcourt le vrai bucket."""
        import inspect

        signature = inspect.signature(media_reconcile.reconcile_once)
        self.assertIsNone(signature.parameters["store"].default)
        self.assertIsNone(signature.parameters["notify"].default)
        source = pathlib.Path(media_reconcile.__file__).read_text(encoding="utf-8")
        self.assertIn("store = store or media_store.reconcile_bucket", source)
        self.assertIn("notify = notify or send_admin_message", source)


class SettingsTest(unittest.TestCase):
    """Période et seuil : bornés, éteignables, jamais en échec."""

    def test_a_plain_value_is_kept(self):
        self.assertEqual(config_runtime.parse_reconcile_minutes(720), 720)
        self.assertEqual(config_runtime.parse_orphan_threshold("12"), 12)

    def test_zero_turns_the_watch_off_instead_of_being_raised_to_the_floor(self):
        """Éteindre une veille coûteuse est une décision, pas une valeur à corriger."""
        self.assertEqual(config_runtime.parse_reconcile_minutes(0), 0)
        self.assertEqual(config_runtime.parse_reconcile_minutes("0"), 0)
        self.assertEqual(config_runtime.parse_reconcile_minutes(-10), 0)

    def test_a_period_below_the_floor_is_raised(self):
        low, _ = config_runtime.RECONCILE_MINUTES_BOUNDS
        self.assertEqual(config_runtime.parse_reconcile_minutes(1), low)
        self.assertEqual(config_runtime.parse_reconcile_minutes(29), low)

    def test_a_period_above_the_ceiling_is_lowered(self):
        _, high = config_runtime.RECONCILE_MINUTES_BOUNDS
        self.assertEqual(config_runtime.parse_reconcile_minutes(100000), high)

    def test_an_unreadable_value_falls_back_without_raising(self):
        self.assertEqual(
            config_runtime.parse_reconcile_minutes("bientot"),
            config_runtime.DEFAULT_RECONCILE_MINUTES,
        )
        self.assertEqual(config_runtime.parse_reconcile_minutes("bientot", default=45), 45)
        self.assertEqual(
            config_runtime.parse_orphan_threshold("bientot"),
            config_runtime.DEFAULT_ORPHAN_ALERT_THRESHOLD,
        )

    def test_a_negative_threshold_falls_back_instead_of_being_guessed(self):
        """Aucun compte ne peut être inférieur à un seuil négatif."""
        self.assertEqual(
            config_runtime.parse_orphan_threshold(-1),
            config_runtime.DEFAULT_ORPHAN_ALERT_THRESHOLD,
        )

    def test_a_threshold_of_zero_is_a_value_not_an_absence(self):
        self.assertEqual(config_runtime.parse_orphan_threshold(0), 0)


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


class ReconcileSettingsTest(unittest.TestCase):
    """Les valeurs effectives : base, sinon environnement, sinon défaut."""

    KEYS = ("MEDIA_RECONCILE_MINUTES", "MEDIA_ORPHAN_ALERT_THRESHOLD")

    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in self.KEYS}
        os.environ["MEDIA_RECONCILE_MINUTES"] = "120"
        os.environ["MEDIA_ORPHAN_ALERT_THRESHOLD"] = "7"
        config_runtime.reset_env_config()

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_runtime.reset_env_config()

    def _settings(self, store=None):
        return settings.media_reconcile_settings(client=_settings_client(store))

    def test_the_environment_applies_by_default(self):
        self.assertEqual(self._settings(), {"minutes": 120, "orphan_threshold": 7})

    def test_the_database_override_wins(self):
        resolved = self._settings(
            {
                settings.SETTING_MEDIA_RECONCILE_MINUTES: 720,
                settings.SETTING_MEDIA_ORPHAN_ALERT_THRESHOLD: 0,
            }
        )
        self.assertEqual(resolved, {"minutes": 720, "orphan_threshold": 0})

    def test_zero_in_the_database_turns_the_watch_off(self):
        resolved = self._settings({settings.SETTING_MEDIA_RECONCILE_MINUTES: 0})
        self.assertEqual(resolved["minutes"], 0)

    def test_an_unreadable_override_keeps_the_environment(self):
        resolved = self._settings({settings.SETTING_MEDIA_RECONCILE_MINUTES: "bientot"})
        self.assertEqual(resolved["minutes"], 120)

    def test_a_broken_database_keeps_the_environment(self):
        client = _settings_client()
        client.table = lambda name: (_ for _ in ()).throw(RuntimeError("db down"))
        resolved = settings.media_reconcile_settings(client=client)
        self.assertEqual(resolved, {"minutes": 120, "orphan_threshold": 7})


class PreflightTest(unittest.TestCase):
    """Ce qui est configuré doit se lire quelque part."""

    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in ("TELEGRAM_ADMIN_CHAT_ID",)}
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
        os.environ["MEDIA_RECONCILE_MINUTES"] = "60"
        os.environ["MEDIA_ORPHAN_ALERT_THRESHOLD"] = "3"
        config_runtime.reset_env_config()
        published = config_runtime.get_env_config().preflight()["media_reconciliation"]
        self.assertEqual(published, {"every_minutes": 60, "orphan_threshold": 3})

    def test_a_watch_without_an_admin_chat_is_visible(self):
        """Sinon on cherche pourquoi aucune alerte n'arrive."""
        published = (
            config_runtime.get_env_config().component_health()["media_reconciliation"]
        )
        self.assertFalse(published["admin_chat"])
        os.environ["TELEGRAM_ADMIN_CHAT_ID"] = "42"
        config_runtime.reset_env_config()
        self.assertTrue(
            config_runtime.get_env_config().component_health()["media_reconciliation"]["admin_chat"]
        )


class AdminMessageTest(unittest.IsolatedAsyncioTestCase):
    """Le message d'exploitation : au chat admin, sans clavier."""

    async def test_the_message_goes_to_the_admin_chat(self):
        bot = mock.MagicMock()
        bot.send_message = mock.AsyncMock()
        with mock.patch.object(notify, "get_bot", lambda: bot):
            self.assertTrue(await notify.send_admin_message(42, "texte"))
        bot.send_message.assert_awaited_once_with(chat_id=42, text="texte")

    async def test_an_unusable_bot_raises_to_the_caller(self):
        """C'est à l'appelant de décider ce qu'une alerte non partie implique."""
        with mock.patch.object(notify, "get_bot", lambda: (_ for _ in ()).throw(RuntimeError("token"))):
            with self.assertRaises(RuntimeError):
                await notify.send_admin_message(42, "texte")


class WiringTest(unittest.TestCase):
    """`auto_loop` n'est pas importable ici : on relit sa source."""

    def setUp(self) -> None:
        self.source = AUTO_LOOP.read_text(encoding="utf-8")

    def test_the_watch_is_scheduled_by_the_loop(self):
        self.assertIn("from workers import media_reconcile", self.source)

    def test_the_watch_runs_on_every_pass_of_the_loop(self):
        """Un cycle au démarrage ne suffit pas : c'est une **veille**."""
        body = self.source.split("while True:", 1)[1]
        self.assertIn("await reconcile_media_and_alert()", body)

    def test_the_watch_also_runs_once_at_startup(self):
        """Un déploiement est justement le moment où on veut savoir."""
        startup = self.source.split("while True:", 1)[0]
        self.assertIn("await reconcile_media_and_alert()", startup)

    def test_the_period_and_the_threshold_come_from_the_settings(self):
        self.assertIn("settings.media_reconcile_settings", self.source)
        self.assertIn('resolved["orphan_threshold"]', self.source)
        self.assertIn('_reconcile_settings["minutes"] * 60', self.source)

    def test_the_settings_are_read_off_the_event_loop(self):
        self.assertIn(
            "resolved = await asyncio.to_thread(settings.media_reconcile_settings)",
            self.source,
        )

    def test_an_off_watch_is_re_examined_instead_of_staying_off_forever(self):
        """Sinon un `0` posé en base ne serait relu qu'au prochain redémarrage."""
        self.assertIn("DISABLED_RECHECK_EVERY", self.source)

    def test_the_startup_line_says_what_will_run(self):
        body = self.source.split("async def run_forever", 1)[1]
        self.assertIn("Réconciliation média:", body)

    def test_no_period_or_threshold_is_hardcoded_in_the_worker(self):
        """Des constantes recopiées ici divergeraient de la configuration."""
        for line in self.source.splitlines():
            if line.strip().startswith("#"):
                continue
            self.assertNotIn("ORPHAN_ALERT_THRESHOLD =", line)
            self.assertNotIn("RECONCILE_EVERY =", line)


class DocumentationTest(unittest.TestCase):
    """.env.example et le README disent la même chose que le code."""

    def test_both_variables_are_documented_in_the_env_example(self):
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        for name in ("MEDIA_RECONCILE_MINUTES", "MEDIA_ORPHAN_ALERT_THRESHOLD"):
            self.assertIn(name, text)

    def test_the_readme_has_a_section_for_the_watch(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("workers/media_reconcile.py", text)
        self.assertIn("Aucune suppression automatique", text)


class StorageTest(unittest.TestCase):
    """Le parcours partagé : un seul instantané pour les deux sens."""

    def test_the_two_senses_come_from_the_same_scan(self):
        paths = ["telegram/@signals/1-a.jpg", "telegram/@signals/9-orphan.jpg"]
        rows = [{"storage_path": "telegram/@signals/1-a.jpg"}, {"storage_path": "hors/périmètre.jpg"}]
        with mock.patch.object(media_store, "iter_storage_paths", lambda prefix="": list(paths)), (
            mock.patch.object(media_store, "_read_media", lambda columns, **kwargs: [dict(r) for r in rows])
        ):
            report = media_store.reconcile_bucket()
        self.assertEqual(report["orphans"], ["telegram/@signals/9-orphan.jpg"])
        self.assertEqual([row["storage_path"] for row in report["missing"]], ["hors/périmètre.jpg"])
        self.assertEqual(report["objects"], 2)
        self.assertEqual(report["rows"], 2)

    def test_the_delegations_use_that_single_scan(self):
        """Deux implémentations pourraient déclarer le même objet orphelin et référencé."""
        calls: list = []

        def fake(prefix=""):
            calls.append(prefix)
            return {"orphans": ["o"], "missing": [ROW]}

        with mock.patch.object(media_store, "reconcile_bucket", fake):
            self.assertEqual(media_store.list_orphan_objects(), ["o"])
            self.assertEqual(media_store.list_missing_objects(), [ROW])
        self.assertEqual(calls, ["", ""])


if __name__ == "__main__":
    unittest.main()
