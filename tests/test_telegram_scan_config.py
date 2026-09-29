"""Configuration du balayage des canaux Telegram : liste et période.

Ce qui est testé ici est ce qui a **quitté le code** : la liste des canaux et la
période de balayage étaient des constantes de `workers/auto_loop.py`, elles sont
maintenant lues dans l'environnement (`core.config_runtime`) et surchargeables en
base (`database/settings.py`, migration 010).

Trois propriétés comptent, et chacune a son lot de cas :

* **une seule validation** pour les deux sources — une valeur acceptée dans
  l'environnement doit l'être en base, et inversement ;
* **rien d'ambigu n'est deviné** : ce qui ne peut pas être un canal public est
  écarté *en le disant*, jamais transformé en pseudo fantôme ;
* **aucune panne de réglage n'arrête la collecte** : table absente, base
  injoignable ou valeur illisible retombent sur l'environnement.
"""
from __future__ import annotations

import os
import pathlib
import unittest
from types import SimpleNamespace
from unittest import mock

from core import config_runtime
from database import settings
from notifications import telegram_channels
from tests import supabase_double

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATION = REPO_ROOT / "database" / "migrations" / "010_bot_settings.sql"
README = REPO_ROOT / "README.md"
AUTO_LOOP = REPO_ROOT / "workers" / "auto_loop.py"
MAIN = REPO_ROOT / "main.py"


class SplitTelegramChannelsTest(unittest.TestCase):
    """Ce qui est accepté comme canal, et ce qui est écarté (et rendu à part)."""

    def test_a_single_channel_is_kept(self):
        self.assertEqual(
            config_runtime.split_telegram_channels("thehalalwinningteam"),
            (["thehalalwinningteam"], []),
        )

    def test_several_channels_are_separated_by_commas_semicolons_or_newlines(self):
        for value in (
            "canal_un,canal_deux",
            "canal_un; canal_deux",
            "canal_un\ncanal_deux",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    config_runtime.split_telegram_channels(value),
                    (["canal_un", "canal_deux"], []),
                )

    def test_at_underscore_and_case_are_normalized(self):
        self.assertEqual(
            config_runtime.split_telegram_channels("@Mon_Canal"),
            (["mon_canal"], []),
        )

    def test_urls_are_accepted_including_public_preview_and_message_links(self):
        self.assertEqual(
            config_runtime.split_telegram_channels(
                "https://t.me/canal_un, t.me/s/canal_deux, telegram.me/canal_trois, "
                "https://t.me/canal_quatre/42"
            ),
            (["canal_un", "canal_deux", "canal_trois", "canal_quatre"], []),
        )

    def test_duplicates_are_removed(self):
        """Un canal écrit deux fois serait balayé deux fois par cycle."""
        self.assertEqual(
            config_runtime.split_telegram_channels("canal_un, @canal_un"),
            (["canal_un"], []),
        )

    def test_a_list_is_accepted_as_is(self):
        """Une valeur de base (`jsonb`) arrive déjà découpée."""
        self.assertEqual(
            config_runtime.split_telegram_channels(["canal_un", "@canal_deux"]),
            (["canal_un", "canal_deux"], []),
        )

    def test_a_title_is_rejected_instead_of_becoming_two_channels(self):
        """Un espace n'est pas un séparateur : un pseudo Telegram n'en contient pas."""
        self.assertEqual(
            config_runtime.split_telegram_channels("Crypto Signals"),
            ([], ["Crypto Signals"]),
        )

    def test_what_cannot_be_a_public_channel_is_rejected(self):
        for value in ("42", "-1001234567890", "ab", "a" * 33, "mon.canal", "@"):
            with self.subTest(value=value):
                channels, rejected = config_runtime.split_telegram_channels(value)
                self.assertEqual(channels, [])
                self.assertEqual(rejected, [value])

    def test_a_valid_value_next_to_an_invalid_one_keeps_the_valid_one(self):
        channels, rejected = config_runtime.split_telegram_channels(
            "https://t.me/bon_canal, Crypto Signals"
        )
        self.assertEqual(channels, ["bon_canal"])
        self.assertEqual(rejected, ["Crypto Signals"])

    def test_nothing_configured_yields_nothing_and_no_complaint(self):
        self.assertEqual(config_runtime.split_telegram_channels(None), ([], []))
        self.assertEqual(config_runtime.split_telegram_channels("  "), ([], []))


class ParseScanMinutesTest(unittest.TestCase):
    """La période de balayage : bornée, et jamais en échec."""

    def test_a_plain_number_is_kept(self):
        self.assertEqual(config_runtime.parse_scan_minutes("45"), 45)
        self.assertEqual(config_runtime.parse_scan_minutes(90), 90)
        self.assertEqual(config_runtime.parse_scan_minutes("12.0"), 12)

    def test_below_the_floor_is_raised_to_the_floor(self):
        """En dessous, on martèle l'aperçu public `t.me/s/<canal>`."""
        low, _ = config_runtime.TELEGRAM_SCAN_MINUTES_BOUNDS
        self.assertEqual(config_runtime.parse_scan_minutes(1), low)
        self.assertEqual(config_runtime.parse_scan_minutes("0"), low)
        self.assertEqual(config_runtime.parse_scan_minutes(-30), low)

    def test_above_the_ceiling_is_lowered_to_the_ceiling(self):
        _, high = config_runtime.TELEGRAM_SCAN_MINUTES_BOUNDS
        self.assertEqual(config_runtime.parse_scan_minutes(100000), high)

    def test_an_unreadable_value_falls_back_without_raising(self):
        """Une faute de frappe dans un intervalle ne doit pas rendre la config illisible."""
        self.assertEqual(
            config_runtime.parse_scan_minutes("bientot"), config_runtime.DEFAULT_TELEGRAM_SCAN_MINUTES
        )
        self.assertEqual(config_runtime.parse_scan_minutes(None), config_runtime.DEFAULT_TELEGRAM_SCAN_MINUTES)
        self.assertEqual(config_runtime.parse_scan_minutes("bientot", default=17), 17)

    def test_the_interactive_reader_uses_the_same_bounds(self):
        """`clamp_scan_minutes` est la validation ; `parse_scan_minutes` s'en sert.

        Une seconde table de bornes finirait par diverger : la commande
        accepterait alors une période que le balayage refuse (ou l'inverse).
        Cette version rend `None` sur une valeur illisible, parce qu'une commande
        doit pouvoir répondre à quelqu'un qui attend.
        """
        low, high = config_runtime.TELEGRAM_SCAN_MINUTES_BOUNDS
        self.assertEqual(config_runtime.clamp_scan_minutes("45"), 45)
        self.assertEqual(config_runtime.clamp_scan_minutes(1), low)
        self.assertEqual(config_runtime.clamp_scan_minutes(100000), high)
        self.assertIsNone(config_runtime.clamp_scan_minutes("bientot"))
        self.assertIsNone(config_runtime.clamp_scan_minutes(None))

    def test_the_two_readers_agree_on_every_value(self):
        """Propriété de partage : l'un ne peut pas dériver de l'autre en silence."""
        for value in (1, "0", -30, 45, "12.0", 100000, "bientot", None, "", "1e3"):
            with self.subTest(value=value):
                clamped = config_runtime.clamp_scan_minutes(value)
                expected = (
                    config_runtime.DEFAULT_TELEGRAM_SCAN_MINUTES if clamped is None else clamped
                )
                self.assertEqual(config_runtime.parse_scan_minutes(value), expected)


class ParseScanMaxExtractionsTest(unittest.TestCase):
    """Le plafond d'extractions : borné, `0` = sans plafond, jamais en échec."""

    def test_a_plain_number_is_kept(self):
        self.assertEqual(config_runtime.parse_scan_max_extractions("5"), 5)
        self.assertEqual(config_runtime.parse_scan_max_extractions(40), 40)
        self.assertEqual(config_runtime.parse_scan_max_extractions("12.0"), 12)

    def test_zero_means_no_cap_and_is_kept(self):
        """`0` n'est pas « aucune extraction » : c'est un cran d'arrêt à retirer."""
        low, _ = config_runtime.TELEGRAM_SCAN_MAX_EXTRACTIONS_BOUNDS
        self.assertEqual(low, 0)
        self.assertEqual(config_runtime.parse_scan_max_extractions(0), 0)
        self.assertEqual(config_runtime.parse_scan_max_extractions("0"), 0)

    def test_a_negative_value_is_raised_to_no_cap(self):
        self.assertEqual(config_runtime.parse_scan_max_extractions(-5), 0)

    def test_above_the_ceiling_is_lowered_to_the_ceiling(self):
        _, high = config_runtime.TELEGRAM_SCAN_MAX_EXTRACTIONS_BOUNDS
        self.assertEqual(config_runtime.parse_scan_max_extractions(100000), high)

    def test_an_unreadable_value_falls_back_without_raising(self):
        self.assertEqual(
            config_runtime.parse_scan_max_extractions("bientot"),
            config_runtime.DEFAULT_TELEGRAM_SCAN_MAX_EXTRACTIONS,
        )
        self.assertEqual(
            config_runtime.parse_scan_max_extractions(None),
            config_runtime.DEFAULT_TELEGRAM_SCAN_MAX_EXTRACTIONS,
        )
        self.assertEqual(config_runtime.parse_scan_max_extractions(None, default=3), 3)

    def test_the_interactive_reader_uses_the_same_bounds(self):
        """`clamp_scan_max_extractions` est la validation ; `max` s'en sert."""
        _, high = config_runtime.TELEGRAM_SCAN_MAX_EXTRACTIONS_BOUNDS
        self.assertEqual(config_runtime.clamp_scan_max_extractions("5"), 5)
        self.assertEqual(config_runtime.clamp_scan_max_extractions(100000), high)
        self.assertIsNone(config_runtime.clamp_scan_max_extractions("bientot"))
        self.assertIsNone(config_runtime.clamp_scan_max_extractions(None))

    def test_the_two_readers_agree_on_every_value(self):
        """Propriété de partage : l'un ne peut pas dériver de l'autre en silence."""
        for value in (-5, "0", 1, 10, "12.0", 100000, "bientot", None, "", "1e3"):
            with self.subTest(value=value):
                clamped = config_runtime.clamp_scan_max_extractions(value)
                expected = (
                    config_runtime.DEFAULT_TELEGRAM_SCAN_MAX_EXTRACTIONS
                    if clamped is None
                    else clamped
                )
                self.assertEqual(config_runtime.parse_scan_max_extractions(value), expected)


class ChooseTelegramChannelsTest(unittest.TestCase):
    """Base > environnement > défaut, avec les deux cas limites qui comptent."""

    FALLBACK = ["canal_env"]

    def test_no_override_keeps_the_fallback(self):
        self.assertEqual(
            config_runtime.choose_telegram_channels(override=None, fallback=self.FALLBACK),
            (self.FALLBACK, []),
        )

    def test_a_usable_override_wins(self):
        self.assertEqual(
            config_runtime.choose_telegram_channels(
                override="canal_base", fallback=self.FALLBACK
            ),
            (["canal_base"], []),
        )

    def test_an_explicitly_empty_override_turns_the_scan_off(self):
        """C'est la seule façon d'éteindre la collecte sans toucher au code."""
        self.assertEqual(
            config_runtime.choose_telegram_channels(override="", fallback=self.FALLBACK),
            ([], []),
        )
        self.assertEqual(
            config_runtime.choose_telegram_channels(override=[], fallback=self.FALLBACK),
            ([], []),
        )

    def test_an_unreadable_override_does_not_silence_the_scan(self):
        """Une faute de frappe n'est pas une décision : le repli reprend, et le dit."""
        channels, rejected = config_runtime.choose_telegram_channels(
            override="Crypto Signals", fallback=self.FALLBACK
        )
        self.assertEqual(channels, self.FALLBACK)
        self.assertEqual(rejected, ["Crypto Signals"])


class EnvConfigTelegramTest(unittest.TestCase):
    """Le passage par l'environnement : lecture, normalisation, preflight."""

    KEYS = (
        "TELEGRAM_CHANNELS",
        "TELEGRAM_SCAN_MINUTES",
        "TELEGRAM_SCAN_MAX_EXTRACTIONS",
    )

    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in self.KEYS}
        for key in self.KEYS:
            os.environ.pop(key, None)
        config_runtime.reset_env_config()

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_runtime.reset_env_config()

    def _config(self):
        config_runtime.reset_env_config()
        return config_runtime.get_env_config()

    def test_unset_env_keeps_the_project_default(self):
        cfg = self._config()
        self.assertEqual(list(cfg.telegram_channels), list(config_runtime.DEFAULT_TELEGRAM_CHANNELS))
        self.assertEqual(
            cfg.telegram_scan_max_extractions,
            config_runtime.DEFAULT_TELEGRAM_SCAN_MAX_EXTRACTIONS,
        )

    def test_env_channels_are_normalized(self):
        os.environ["TELEGRAM_CHANNELS"] = "@Premier, https://t.me/second_canal"
        cfg = self._config()
        self.assertEqual(cfg.telegram_channels, ["premier", "second_canal"])
        self.assertEqual(cfg.telegram_channels_invalid, [])

    def test_an_empty_env_value_disables_the_scan(self):
        """`TELEGRAM_CHANNELS=""` doit vraiment tout éteindre, pas remettre le défaut."""
        os.environ["TELEGRAM_CHANNELS"] = ""
        self.assertEqual(self._config().telegram_channels, [])

    def test_invalid_env_values_are_reported_not_guessed(self):
        os.environ["TELEGRAM_CHANNELS"] = "Crypto Signals"
        cfg = self._config()
        self.assertEqual(cfg.telegram_channels, [])
        self.assertEqual(cfg.telegram_channels_invalid, ["Crypto Signals"])

    def test_scan_minutes_are_read_and_clamped(self):
        os.environ["TELEGRAM_SCAN_MINUTES"] = "70000"
        self.assertEqual(
            self._config().telegram_scan_minutes, config_runtime.TELEGRAM_SCAN_MINUTES_BOUNDS[1]
        )
        os.environ["TELEGRAM_SCAN_MINUTES"] = "bientot"
        self.assertEqual(
            self._config().telegram_scan_minutes, config_runtime.DEFAULT_TELEGRAM_SCAN_MINUTES
        )

    def test_the_extraction_cap_is_read_and_clamped(self):
        os.environ["TELEGRAM_SCAN_MAX_EXTRACTIONS"] = "3"
        self.assertEqual(self._config().telegram_scan_max_extractions, 3)
        os.environ["TELEGRAM_SCAN_MAX_EXTRACTIONS"] = "100000"
        self.assertEqual(
            self._config().telegram_scan_max_extractions,
            config_runtime.TELEGRAM_SCAN_MAX_EXTRACTIONS_BOUNDS[1],
        )
        os.environ["TELEGRAM_SCAN_MAX_EXTRACTIONS"] = "bientot"
        self.assertEqual(
            self._config().telegram_scan_max_extractions,
            config_runtime.DEFAULT_TELEGRAM_SCAN_MAX_EXTRACTIONS,
        )

    def test_preflight_publishes_the_effective_scan_configuration(self):
        os.environ["TELEGRAM_CHANNELS"] = "canal_un, Crypto Signals"
        os.environ["TELEGRAM_SCAN_MINUTES"] = "45"
        os.environ["TELEGRAM_SCAN_MAX_EXTRACTIONS"] = "4"
        published = self._config().preflight()["telegram_channels"]
        self.assertEqual(published["channels"], ["canal_un"])
        self.assertEqual(published["invalid"], ["Crypto Signals"])
        self.assertEqual(published["scan_minutes"], 45)
        self.assertEqual(published["max_extractions"], 4)

    def test_a_channel_mistake_does_not_make_the_service_unready(self):
        """Un pseudo mal orthographié se voit, mais ne bloque pas le démarrage.

        Il n'entre ni dans les manques fonctionnels ni dans les défauts de
        sécurité : il est publié dans le preflight, ce qui est une information,
        pas un refus de démarrer.
        """
        os.environ["TELEGRAM_CHANNELS"] = "pas un canal"
        cfg = self._config()
        issues = cfg.required_issues() + cfg.security_issues()
        self.assertEqual([issue for issue in issues if "canal" in issue], [])
        self.assertEqual(cfg.preflight()["telegram_channels"]["invalid"], ["pas un canal"])


def _client(store=None, error=None):
    """Le client doublé du dossier, `bot_settings` semé comme la base le rendrait.

    `store` est le dictionnaire clé → valeur des surcharges : chaque entrée devient
    une **ligne**, donc la lecture filtrée par `eq("key", …)` la retrouve, et une
    clé absente est une ligne absente. `error` est la panne que **toutes** les
    opérations rencontrent — lecture comme écriture —, ce qui couvre d'un coup les
    trois replis du module : lecture ratée, écriture ratée, table manquante.
    """
    client = supabase_double.SupabaseDouble()
    table = client.store(settings.TABLE)
    table.rows = [{"key": key, "value": value} for key, value in (store or {}).items()]
    if error is not None:
        table.read_error = table.upsert_error = table.delete_error = error
    return client


def _stored(client, key):
    """La valeur que la base porte pour un réglage (`None` si la ligne est absente).

    Relue par `get_setting` — la vraie lecture du module, filtrée sur la clé — et
    non par un dictionnaire tenu à part : une écriture qui ne s'est pas faite ne
    peut donc pas passer pour une réussite.
    """
    return settings.get_setting(key, client=client)


class GetSettingTest(unittest.TestCase):
    """Lecture d'un réglage : trois façons d'être absent, une seule conduite."""

    def test_a_stored_value_is_returned(self):
        client = _client({settings.SETTING_TELEGRAM_CHANNELS: ["canal_base"]})
        self.assertEqual(
            settings.get_setting(settings.SETTING_TELEGRAM_CHANNELS, client=client),
            ["canal_base"],
        )
        self.assertIn(settings.TABLE, client.tables())

    def test_an_absent_key_reads_as_none(self):
        self.assertIsNone(settings.get_setting("inconnu", client=_client()))

    def test_a_missing_table_reads_as_none_instead_of_raising(self):
        """La migration 010 n'est peut-être pas appliquée : l'environnement reprend."""
        client = _client(error=RuntimeError("relation \"bot_settings\" does not exist"))
        self.assertIsNone(settings.get_setting(settings.SETTING_TELEGRAM_CHANNELS, client=client))

    def test_an_error_carried_by_the_response_is_not_taken_for_a_success(self):
        """Le seul client écrit à la main qui reste ici, et pour une raison précise.

        `supabase-py` a deux façons de signaler un échec : lever, ou rendre un
        objet portant `error`. La doublure partagée ne sait faire que la première
        (`read_error`), et son `Result` n'a que `data` — c'est donc la **forme de
        la réponse** qu'on fabrique ici, pas un client.
        """
        response = SimpleNamespace(data=[], error="boom", count=None)
        client = SimpleNamespace(table=lambda name: SimpleNamespace(
            select=lambda *a: SimpleNamespace(
                eq=lambda *a: SimpleNamespace(limit=lambda *a: SimpleNamespace(
                    execute=lambda: response
                ))
            )
        ))
        self.assertIsNone(settings.get_setting("canal", client=client))

    def test_without_a_client_nothing_is_queried(self):
        with mock.patch.object(settings, "supabase", None):
            self.assertIsNone(settings.get_setting(settings.SETTING_TELEGRAM_CHANNELS))
        self.assertIsNone(settings.get_setting("", client=_client()))


class SetSettingTest(unittest.TestCase):
    """Écriture d'un réglage, et le retour à l'environnement."""

    def test_a_value_is_written(self):
        client = _client()
        result = settings.set_setting(settings.SETTING_TELEGRAM_SCAN_MINUTES, 45, client=client)
        self.assertTrue(result["ok"])
        self.assertEqual(_stored(client, settings.SETTING_TELEGRAM_SCAN_MINUTES), 45)

    def test_none_removes_the_row_instead_of_writing_null(self):
        """Deux façons de dire « pas de surcharge » finiraient par diverger."""
        client = _client({settings.SETTING_TELEGRAM_SCAN_MINUTES: 45})
        result = settings.set_setting(settings.SETTING_TELEGRAM_SCAN_MINUTES, None, client=client)
        self.assertTrue(result["ok"])
        self.assertIsNone(_stored(client, settings.SETTING_TELEGRAM_SCAN_MINUTES))
        self.assertIn(("delete",), client.calls)

    def test_a_write_failure_is_announced_not_raised(self):
        client = _client(error=RuntimeError("db down"))
        result = settings.set_setting(settings.SETTING_TELEGRAM_SCAN_MINUTES, 45, client=client)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "write_failed")

    def test_without_a_client_the_write_is_refused(self):
        with mock.patch.object(settings, "supabase", None):
            result = settings.set_setting(settings.SETTING_TELEGRAM_SCAN_MINUTES, 45)
        self.assertEqual(result["reason"], "no_client")


class TelegramScanSettingsTest(unittest.TestCase):
    """Les réglages effectifs que lit l'auto-loop : base, sinon environnement."""

    KEYS = (
        "TELEGRAM_CHANNELS",
        "TELEGRAM_SCAN_MINUTES",
        "TELEGRAM_SCAN_MAX_EXTRACTIONS",
    )

    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in self.KEYS}
        os.environ["TELEGRAM_CHANNELS"] = "canal_env"
        os.environ["TELEGRAM_SCAN_MINUTES"] = "45"
        os.environ["TELEGRAM_SCAN_MAX_EXTRACTIONS"] = "7"
        config_runtime.reset_env_config()

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_runtime.reset_env_config()

    def _settings(self, store=None, error=None):
        return settings.telegram_scan_settings(client=_client(store, error=error))

    def test_without_any_override_the_environment_applies(self):
        self.assertEqual(
            self._settings(),
            {
                "channels": ["canal_env"],
                "rejected": [],
                "scan_minutes": 45,
                "max_extractions": 7,
            },
        )

    def test_the_database_override_wins(self):
        resolved = self._settings(
            {
                settings.SETTING_TELEGRAM_CHANNELS: ["canal_base"],
                settings.SETTING_TELEGRAM_SCAN_MINUTES: 90,
                settings.SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS: 2,
            }
        )
        self.assertEqual(resolved["channels"], ["canal_base"])
        self.assertEqual(resolved["scan_minutes"], 90)
        self.assertEqual(resolved["max_extractions"], 2)

    def test_an_unreadable_cap_falls_back_on_the_environment_value(self):
        resolved = self._settings({settings.SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS: "bientot"})
        self.assertEqual(resolved["max_extractions"], 7)

    def test_a_cap_from_the_database_is_clamped_like_the_environment(self):
        resolved = self._settings({settings.SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS: 100000})
        self.assertEqual(
            resolved["max_extractions"],
            config_runtime.TELEGRAM_SCAN_MAX_EXTRACTIONS_BOUNDS[1],
        )

    def test_an_empty_override_in_the_database_turns_the_scan_off(self):
        self.assertEqual(self._settings({settings.SETTING_TELEGRAM_CHANNELS: []})["channels"], [])

    def test_an_unreadable_override_falls_back_and_is_reported(self):
        resolved = self._settings({settings.SETTING_TELEGRAM_CHANNELS: "Crypto Signals"})
        self.assertEqual(resolved["channels"], ["canal_env"])
        self.assertEqual(resolved["rejected"], ["Crypto Signals"])

    def test_an_unreadable_interval_falls_back_on_the_environment_value(self):
        resolved = self._settings({settings.SETTING_TELEGRAM_SCAN_MINUTES: "bientot"})
        self.assertEqual(resolved["scan_minutes"], 45)

    def test_an_interval_from_the_database_is_clamped_like_the_environment(self):
        resolved = self._settings({settings.SETTING_TELEGRAM_SCAN_MINUTES: 1})
        self.assertEqual(
            resolved["scan_minutes"], config_runtime.TELEGRAM_SCAN_MINUTES_BOUNDS[0]
        )

    def test_a_broken_database_keeps_the_environment_configuration(self):
        """Une base injoignable ne doit pas arrêter la collecte."""
        resolved = self._settings(error=RuntimeError("db down"))
        self.assertEqual(resolved["channels"], ["canal_env"])
        self.assertEqual(resolved["scan_minutes"], 45)
        self.assertEqual(resolved["max_extractions"], 7)


class ChannelsCommandArgsTest(unittest.TestCase):
    """L'analyse des arguments : un verbe, ses valeurs, et rien de deviné."""

    def test_no_argument_reads(self):
        parsed = telegram_channels.parse_channels_args([])
        self.assertEqual(parsed["action"], "list")
        self.assertTrue(parsed["ok"], parsed["problem"])
        self.assertEqual(parsed["values"], [])

    def test_add_and_remove_take_their_values(self):
        add = telegram_channels.parse_channels_args(["add", "@canal_un", "@canal_deux"])
        self.assertEqual(add["action"], "add")
        self.assertEqual(add["values"], ["@canal_un", "@canal_deux"])
        remove = telegram_channels.parse_channels_args(["remove", "canal_un"])
        self.assertEqual((remove["action"], remove["values"]), ("remove", ["canal_un"]))

    def test_a_verb_without_a_value_is_a_problem_not_an_empty_write(self):
        for args in (["add"], ["remove"]):
            with self.subTest(args=args):
                parsed = telegram_channels.parse_channels_args(args)
                self.assertFalse(parsed["ok"])
                self.assertIn("au moins un canal", parsed["problem"])

    def test_the_period_takes_exactly_one_value(self):
        self.assertEqual(telegram_channels.parse_channels_args(["every", "30"])["values"], ["30"])
        for args in (["every"], ["every", "30", "45"]):
            with self.subTest(args=args):
                self.assertFalse(telegram_channels.parse_channels_args(args)["ok"])

    def test_the_cap_takes_exactly_one_value(self):
        self.assertEqual(telegram_channels.parse_channels_args(["max", "10"])["action"], "max")
        self.assertEqual(telegram_channels.parse_channels_args(["max", "0"])["values"], ["0"])
        for args in (["max"], ["max", "10", "5"]):
            with self.subTest(args=args):
                self.assertFalse(telegram_channels.parse_channels_args(args)["ok"])

    def test_the_help_the_read_and_the_reset_are_reachable(self):
        for token in ("--help", "-h", "help", "aide"):
            with self.subTest(token=token):
                self.assertEqual(telegram_channels.parse_channels_args([token])["action"], "help")
        for token in ("list", "ls", "show"):
            with self.subTest(token=token):
                self.assertEqual(telegram_channels.parse_channels_args([token])["action"], "list")
        self.assertEqual(telegram_channels.parse_channels_args(["reset"])["action"], "reset")

    def test_an_unknown_verb_is_not_taken_for_a_channel(self):
        """`/channels canaux` ne doit pas ajouter « canaux » à la liste balayée."""
        parsed = telegram_channels.parse_channels_args(["canaux"])
        self.assertFalse(parsed["ok"])
        self.assertNotIn(parsed["action"], telegram_channels.ACTIONS)
        self.assertIn("canaux", telegram_channels.format_channels_help(parsed))

    def test_the_help_names_every_action_and_the_bounds(self):
        text = telegram_channels.format_channels_help()
        for action in ("add", "remove", "every", "max", "reset"):
            with self.subTest(action=action):
                self.assertIn(action, text)
        low, high = config_runtime.TELEGRAM_SCAN_MINUTES_BOUNDS
        self.assertIn(f"{low}..{high}", text)
        cap_low, cap_high = config_runtime.TELEGRAM_SCAN_MAX_EXTRACTIONS_BOUNDS
        self.assertIn(f"{cap_low}..{cap_high}", text)
        self.assertIn("sans plafond", text)
        #: Le bot envoie sans `parse_mode` : un balisage s'afficherait en clair.
        self.assertNotIn("**", text)
        self.assertNotIn("`", text)


class _ChannelsCommandCase(unittest.TestCase):
    """Socle commun : un environnement connu, un chat admin, une base en mémoire."""

    ADMIN = "-1009988776655"
    KEYS = (
        "TELEGRAM_CHANNELS",
        "TELEGRAM_SCAN_MINUTES",
        "TELEGRAM_SCAN_MAX_EXTRACTIONS",
    )

    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in self.KEYS}
        os.environ["TELEGRAM_CHANNELS"] = "canal_env"
        os.environ["TELEGRAM_SCAN_MINUTES"] = "45"
        os.environ["TELEGRAM_SCAN_MAX_EXTRACTIONS"] = "7"
        config_runtime.reset_env_config()

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        config_runtime.reset_env_config()

    def run_command(self, args, *, client, as_admin: bool = True):
        return telegram_channels.channels_command(
            telegram_channels.parse_channels_args(args),
            chat_id=self.ADMIN if as_admin else "42",
            admin_chat_id=self.ADMIN,
            client=client,
        )

    @staticmethod
    def writes(client):
        """Les écritures réellement tentées sur `bot_settings`."""
        return [call for call in client.calls if call[0] in ("upsert", "delete")]


class ChannelsCommandReadTest(_ChannelsCommandCase):
    """/channels — ce qui est balayé, et d'où vient le réglage."""

    def test_the_effective_configuration_comes_from_the_environment(self):
        result = self.run_command([], client=_client())
        self.assertTrue(result["ok"])
        self.assertFalse(result["changed"])
        self.assertIn("@canal_env", result["text"])
        self.assertIn("45 min", result["text"])
        self.assertIn("environnement (TELEGRAM_CHANNELS)", result["text"])
        #: Le plafond se lit aussi : c'est le réglage qui explique pourquoi un
        #: canal chargé n'est pas entièrement traité dans un balayage.
        self.assertIn("Plafond : 7 extraction(s)", result["text"])
        self.assertIn("TELEGRAM_SCAN_MAX_EXTRACTIONS", result["text"])

    def test_a_cap_without_a_limit_is_said_in_plain_words(self):
        """`0` n'est pas un plafond nul : l'afficher « 0 extraction(s) » mentirait."""
        os.environ["TELEGRAM_SCAN_MAX_EXTRACTIONS"] = "0"
        config_runtime.reset_env_config()
        text = self.run_command([], client=_client())["text"]
        self.assertIn("Plafond : sans plafond", text)
        self.assertNotIn("0 extraction(s)", text)

    def test_a_database_override_is_named_as_such(self):
        client = _client({settings.SETTING_TELEGRAM_CHANNELS: ["canal_base"]})
        text = self.run_command([], client=client)["text"]
        self.assertIn("@canal_base", text)
        self.assertNotIn("@canal_env", text)
        self.assertIn("surcharge en base (bot_settings)", text)

    def test_what_was_rejected_is_shown(self):
        client = _client({settings.SETTING_TELEGRAM_CHANNELS: ["Crypto Signals"]})
        text = self.run_command([], client=client)["text"]
        self.assertIn("Crypto Signals", text)
        self.assertIn("Écarté", text)

    def test_reading_never_writes(self):
        client = _client()
        self.run_command([], client=client)
        self.assertEqual(self.writes(client), [])

    def test_an_explicit_read_works_too(self):
        for args in (["list"], ["show"]):
            with self.subTest(args=args):
                result = self.run_command(args, client=_client())
                self.assertTrue(result["ok"])
                self.assertIn("@canal_env", result["text"])

    def test_reading_does_not_require_the_admin_chat(self):
        """Lire n'est pas régler : l'état doit se voir même sans chat admin."""
        result = telegram_channels.channels_command(
            telegram_channels.parse_channels_args([]),
            chat_id="42",
            admin_chat_id="",
            client=_client(),
        )
        self.assertTrue(result["ok"])
        self.assertIn("@canal_env", result["text"])


class ChannelsCommandGuardTest(_ChannelsCommandCase):
    """Qui a le droit d'écrire un réglage **global**."""

    def test_without_an_admin_chat_the_write_is_refused(self):
        client = _client()
        result = telegram_channels.channels_command(
            telegram_channels.parse_channels_args(["add", "nouveau_canal"]),
            chat_id=self.ADMIN,
            admin_chat_id="",
            client=client,
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["changed"])
        self.assertIn("TELEGRAM_ADMIN_CHAT_ID", result["text"])
        self.assertEqual(self.writes(client), [])

    def test_another_chat_cannot_change_the_setting(self):
        client = _client()
        result = self.run_command(["add", "nouveau_canal"], client=client, as_admin=False)
        self.assertFalse(result["ok"])
        self.assertIn("chat admin", result["text"])
        self.assertEqual(self.writes(client), [])

    def test_the_admin_chat_can(self):
        result = self.run_command(["add", "nouveau_canal"], client=_client())
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])


class ChannelsCommandAddTest(_ChannelsCommandCase):
    """Ajouter — sans faire disparaître ce qui est déjà balayé."""

    def test_adding_keeps_what_is_already_scanned(self):
        """Sinon, ajouter un canal ferait disparaître ceux de l'environnement."""
        client = _client()
        result = self.run_command(["add", "nouveau_canal"], client=client)
        self.assertEqual(
            _stored(client, settings.SETTING_TELEGRAM_CHANNELS), ["canal_env", "nouveau_canal"]
        )
        self.assertIn("Ajouté", result["text"])

    def test_an_already_scanned_channel_is_not_written_again(self):
        client = _client()
        result = self.run_command(["add", "@canal_env"], client=client)
        self.assertFalse(result["changed"])
        self.assertIn("Déjà balayé", result["text"])
        self.assertEqual(self.writes(client), [])

    def test_a_valid_value_is_kept_next_to_a_rejected_one(self):
        """Écarter n'est pas deviner, et n'annule pas les autres valeurs."""
        client = _client()
        result = self.run_command(["add", "nouveau_canal, Crypto Signals"], client=client)
        self.assertEqual(
            _stored(client, settings.SETTING_TELEGRAM_CHANNELS), ["canal_env", "nouveau_canal"]
        )
        self.assertIn("Crypto Signals", result["text"])

    def test_nothing_usable_writes_nothing(self):
        client = _client()
        result = self.run_command(["add", "Crypto Signals"], client=client)
        self.assertFalse(result["ok"])
        self.assertEqual(self.writes(client), [])
        self.assertIn("pseudo public", result["text"])

    def test_the_answer_shows_what_the_worker_will_read(self):
        """La liste affichée vient de `telegram_scan_settings`, pas d'un calcul local."""
        client = _client()
        text = self.run_command(["add", "nouveau_canal"], client=client)["text"]
        self.assertEqual(
            settings.telegram_scan_settings(client=client)["channels"],
            ["canal_env", "nouveau_canal"],
        )
        self.assertIn("• @canal_env", text)
        self.assertIn("• @nouveau_canal", text)
        self.assertIn("surcharge en base (bot_settings)", text)

    def test_a_write_failure_is_never_announced_as_a_success(self):
        client = _client(error=RuntimeError("db down"))
        result = self.run_command(["add", "nouveau_canal"], client=client)
        self.assertFalse(result["ok"])
        self.assertFalse(result["changed"])
        self.assertIn("NON appliqué", result["text"])
        self.assertIn("précédente", result["text"])


class ChannelsCommandRemoveTest(_ChannelsCommandCase):
    """Retirer — et le dire quand la collecte s'éteint."""

    def test_removing_the_last_channel_turns_the_collecte_off(self):
        client = _client()
        result = self.run_command(["remove", "canal_env"], client=client)
        self.assertEqual(_stored(client, settings.SETTING_TELEGRAM_CHANNELS), [])
        self.assertIn("Retiré", result["text"])
        self.assertIn("collecte est éteinte", result["text"])

    def test_removing_one_of_two_keeps_the_other(self):
        client = _client(
            {settings.SETTING_TELEGRAM_CHANNELS: ["canal_un", "canal_deux"]}
        )
        self.run_command(["remove", "canal_un"], client=client)
        self.assertEqual(_stored(client, settings.SETTING_TELEGRAM_CHANNELS), ["canal_deux"])

    def test_removing_a_channel_that_is_not_scanned_writes_nothing(self):
        client = _client()
        result = self.run_command(["remove", "inconnu_xyz"], client=client)
        self.assertFalse(result["changed"])
        self.assertIn("Pas balayé", result["text"])
        self.assertEqual(self.writes(client), [])

    def test_the_environment_list_is_the_starting_point(self):
        """Retirer un canal de l'environnement écrit le reste, explicitement."""
        os.environ["TELEGRAM_CHANNELS"] = "canal_un, canal_deux"
        config_runtime.reset_env_config()
        client = _client()
        self.run_command(["remove", "canal_un"], client=client)
        self.assertEqual(_stored(client, settings.SETTING_TELEGRAM_CHANNELS), ["canal_deux"])


class ChannelsCommandEveryTest(_ChannelsCommandCase):
    """La période : bornée comme le balayage, mais jamais avalée en silence."""

    def test_a_valid_period_is_written(self):
        client = _client()
        result = self.run_command(["every", "90"], client=client)
        self.assertEqual(_stored(client, settings.SETTING_TELEGRAM_SCAN_MINUTES), 90)
        self.assertIn("Période : 90 min", result["text"])

    def test_the_period_does_not_touch_the_channel_list(self):
        client = _client()
        self.run_command(["every", "90"], client=client)
        self.assertIsNone(_stored(client, settings.SETTING_TELEGRAM_CHANNELS))

    def test_a_period_out_of_bounds_is_clamped_to_the_scan_bounds(self):
        low, high = config_runtime.TELEGRAM_SCAN_MINUTES_BOUNDS
        for raw, expected in (("2", low), ("100000", high)):
            with self.subTest(raw=raw):
                client = _client()
                result = self.run_command(["every", raw], client=client)
                self.assertEqual(
                    _stored(client, settings.SETTING_TELEGRAM_SCAN_MINUTES), expected
                )
                self.assertIn("hors bornes", result["text"])

    def test_an_illegible_period_is_refused_instead_of_being_kept_silently(self):
        """La lecture de configuration retombe en silence ; la commande répond."""
        client = _client()
        result = self.run_command(["every", "bientot"], client=client)
        self.assertFalse(result["ok"])
        self.assertIn("illisible", result["text"])
        self.assertEqual(self.writes(client), [])


class ChannelsCommandMaxTest(_ChannelsCommandCase):
    """Le plafond d'extractions : borné, mais `0` y est une **valeur**, pas un refus."""

    def test_a_valid_cap_is_written(self):
        client = _client()
        result = self.run_command(["max", "4"], client=client)
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(_stored(client, settings.SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS), 4)
        self.assertIn("Plafond : 4 extraction(s)", result["text"])

    def test_zero_removes_the_cap_instead_of_setting_it_to_zero(self):
        """`0` = « sans plafond » : le borner en silence rendrait le cran d'arrêt
        impossible à retirer depuis Telegram."""
        client = _client({settings.SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS: 3})
        result = self.run_command(["max", "0"], client=client)
        self.assertEqual(_stored(client, settings.SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS), 0)
        self.assertIn("Plafond retiré", result["text"])

    def test_a_cap_out_of_bounds_is_clamped_to_the_bounds(self):
        _, high = config_runtime.TELEGRAM_SCAN_MAX_EXTRACTIONS_BOUNDS
        for raw, expected in (("-3", 0), ("100000", high)):
            with self.subTest(raw=raw):
                client = _client()
                result = self.run_command(["max", raw], client=client)
                self.assertEqual(
                    _stored(client, settings.SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS), expected
                )
                self.assertIn("hors bornes", result["text"])

    def test_an_illegible_cap_is_refused_instead_of_being_kept_silently(self):
        client = _client()
        result = self.run_command(["max", "beaucoup"], client=client)
        self.assertFalse(result["ok"])
        self.assertIn("illisible", result["text"])
        self.assertEqual(self.writes(client), [])

    def test_the_cap_does_not_touch_the_other_settings(self):
        client = _client()
        self.run_command(["max", "4"], client=client)
        self.assertIsNone(_stored(client, settings.SETTING_TELEGRAM_CHANNELS))
        self.assertIsNone(_stored(client, settings.SETTING_TELEGRAM_SCAN_MINUTES))

    def test_a_write_failure_is_never_announced_as_a_success(self):
        client = _client(error=RuntimeError("db down"))
        result = self.run_command(["max", "4"], client=client)
        self.assertFalse(result["ok"])
        self.assertFalse(result["changed"])
        self.assertIn("NON appliqué", result["text"])


class ChannelsCommandResetTest(_ChannelsCommandCase):
    """Revenir à l'environnement, sans laisser de surcharge derrière soi."""

    def test_reset_removes_every_override(self):
        """Un réglage oublié ici survivrait à « revenir à l'environnement »."""
        client = _client(
            {
                settings.SETTING_TELEGRAM_CHANNELS: ["canal_base"],
                settings.SETTING_TELEGRAM_SCAN_MINUTES: 90,
                settings.SETTING_TELEGRAM_SCAN_MAX_EXTRACTIONS: 3,
            }
        )
        result = self.run_command(["reset"], client=client)
        self.assertEqual(client.store(settings.TABLE).rows, [])
        self.assertIn("Retour à l'environnement", result["text"])
        self.assertIn("@canal_env", result["text"])
        self.assertIn("45 min", result["text"])
        self.assertIn("Plafond : 7 extraction(s)", result["text"])

    def test_reset_without_an_override_is_not_an_error(self):
        """Supprimer une ligne absente est idempotent : il n'y a rien à signaler."""
        result = self.run_command(["reset"], client=_client())
        self.assertTrue(result["ok"])

    def test_a_failed_reset_says_the_setting_is_unchanged(self):
        client = _client(error=RuntimeError("db down"))
        result = self.run_command(["reset"], client=client)
        self.assertFalse(result["ok"])
        self.assertIn("NON appliqué", result["text"])


class ChannelsCommandWiringTest(unittest.TestCase):
    """`main.py` n'est pas importable ici : on lit sa table de handlers."""

    def _source(self) -> str:
        return MAIN.read_text(encoding="utf-8")

    def test_the_handler_is_registered(self):
        self.assertIn('CommandHandler("channels", channels_cmd)', self._source())

    def test_the_handler_calls_the_module(self):
        source = self._source()
        self.assertIn("telegram_channels.parse_channels_args", source)
        self.assertIn("telegram_channels.channels_command", source)

    def test_the_write_is_gated_on_the_admin_chat(self):
        self.assertIn("admin_chat_id=TELEGRAM_ADMIN_CHAT_ID", self._source())

    def test_the_start_help_lists_the_command(self):
        body = self._source().split("async def start", 1)[1].split("async def refresh_data", 1)[0]
        self.assertIn("/channels", body)

    def test_the_validation_stays_in_one_place(self):
        """Un canal accepté par la commande et refusé par le worker serait deux vérités.

        `main.py` ne doit donc contenir ni la validation, ni une seconde lecture
        des bornes : il passe par le module, qui passe par `core.config_runtime`.
        """
        source = self._source()
        for fragment in (
            "split_telegram_channels",
            "clamp_scan_minutes",
            "parse_scan_minutes",
            "clamp_scan_max_extractions",
        ):
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, source)


class BotSettingsMigrationTest(unittest.TestCase):
    """Contrat de la migration 010, qui ne se compile pas dans ce dépôt."""

    def setUp(self) -> None:
        self.text = MIGRATION.read_text(encoding="utf-8")

    def test_the_migration_exists_and_is_documented(self):
        """Une migration non documentée est une migration qu'on oublie d'appliquer."""
        self.assertIn(MIGRATION.name, README.read_text(encoding="utf-8"))

    def test_the_table_is_a_key_value_store_of_json(self):
        self.assertIn("create table if not exists bot_settings", self.text)
        self.assertIn("key text primary key", self.text)
        self.assertIn("value jsonb", self.text)

    def test_the_migration_is_idempotent(self):
        """Elle doit pouvoir être relancée sans erreur."""
        self.assertIn("create table if not exists", self.text)
        self.assertIn("drop trigger if exists trg_bot_settings_updated_at", self.text)
        self.assertIn("alter table if exists bot_settings enable row level security", self.text)

    def test_it_follows_deny_by_default(self):
        self.assertIn("enable row level security", self.text)
        self.assertIn("revoke all on bot_settings from anon, authenticated", self.text)


class AutoLoopWiringTest(unittest.TestCase):
    """`auto_loop` n'est pas importable ici (`feedparser` absent) : on le lit."""

    def _source(self) -> str:
        return AUTO_LOOP.read_text(encoding="utf-8")

    def test_the_channel_list_is_no_longer_a_constant_of_the_worker(self):
        self.assertNotIn('TELEGRAM_CHANNELS = ["', self._source())
        self.assertNotIn("TELEGRAM_SCAN_EVERY =", self._source())

    def test_the_loop_reads_the_effective_settings(self):
        self.assertIn("settings.telegram_scan_settings", self._source())

    def test_the_settings_are_read_off_the_event_loop(self):
        """La lecture passe par la base : bloquante, donc hors de l'event loop."""
        self.assertIn(
            "scan_settings = await asyncio.to_thread(settings.telegram_scan_settings)",
            self._source(),
        )

    def test_the_period_comes_from_the_settings(self):
        self.assertIn('scan_settings["scan_minutes"] * 60', self._source())

    def test_the_channels_scanned_are_the_configured_ones(self):
        self.assertIn("for channel in channels:", self._source())

    def test_rejected_values_are_announced(self):
        """Un canal écarté doit se voir : sinon il ne remonte juste jamais."""
        self.assertIn('for raw in scan_settings["rejected"]:', self._source())

    def test_the_startup_line_shows_what_will_be_scanned(self):
        body = self._source().split("async def run_forever", 1)[1]
        self.assertIn("Canaux Telegram:", body)

    def test_the_startup_line_shows_the_extraction_cap(self):
        """Sinon un balayage plafonné se lirait comme un balayage incomplet."""
        body = self._source().split("async def run_forever", 1)[1]
        self.assertIn("max_extractions", body)

    def test_the_cap_is_passed_to_the_channel_sweep(self):
        source = self._source()
        self.assertIn('scan_settings["max_extractions"]', source)
        self.assertIn("max_extractions=cap", source)

    def test_deferred_extractions_are_announced(self):
        """Un balayage partiel doit se lire comme partiel, jamais comme un échec."""
        source = self._source()
        self.assertIn('stats.get("deferred", 0)', source)
        self.assertIn("reporté(s) au prochain balayage", source)


class EnvironmentExampleTest(unittest.TestCase):
    """`.env.example` et le README disent la même chose que le code."""

    def test_the_scan_settings_are_documented_in_the_env_example(self):
        text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        for name in (
            "TELEGRAM_CHANNELS",
            "TELEGRAM_SCAN_MINUTES",
            "TELEGRAM_SCAN_MAX_EXTRACTIONS",
        ):
            with self.subTest(name=name):
                self.assertIn(name, text)

    def test_the_readme_documents_the_extraction_cap(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn("TELEGRAM_SCAN_MAX_EXTRACTIONS", text)
        self.assertIn("max_extractions", text)


if __name__ == "__main__":
    unittest.main()
