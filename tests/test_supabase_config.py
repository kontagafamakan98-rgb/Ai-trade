"""Contrat de la vérification Supabase : configuration, tables, aller-retour.

Ce qui est testé ici ne peut pas dépendre d'une base réelle — celle du projet
n'est pas joignable depuis l'environnement de test (pas de `.env`, pas de client
`supabase` installé). On vérifie donc **ce qui décide** du verdict :

* la lecture de la configuration (`SUPABASE_URL` est-elle l'URL de l'API ?
  la clé porte-t-elle le rôle `service_role` ?) — c'est le contrôle qui attrape la
  confusion anon/service_role, fatale ici puisque la RLS est en deny-by-default
  sans aucune policy : avec une clé publique, l'application ne lit **rien** et
  n'écrit rien, sans jamais lever ;
* le comportement de l'outil `scripts/check_supabase.py` sur un client simulé :
  tables lisibles, aller-retour écriture → relecture → suppression, et surtout
  **nettoyage** (une sonde qui laisse des lignes derrière elle dans une base de
  production serait pire que pas de sonde du tout) ;
* le fait que l'aller-retour passe par les **fonctions de l'application**
  (`insert_insight`, `create_pending_signal`, `update_signal_status`), pas par des
  requêtes réécrites dans le script.

Une vérification contre la vraie base existe, mais elle est **ignorée** sans
identifiants : `tests/test_supabase_live.py`.

Le client simulé est **celui du dossier** (`tests/supabase_double.py`), et pas une
douzaine de doublures locales. Ce fichier en portait trois (`_Table`, `_Client`,
`_RecordingClient`) dont un enregistreur d'ordre, écrites avant que la doublure
partagée n'existe. Elles sont parties avec le reste : la doublure commune applique
les filtres, persiste les écritures et tient le journal des écritures, donc elle
**dit vrai** sur ce que la sonde laisse en base — ce dont ce fichier parle.

Le chemin qui ne demande pas `--roundtrip` est monté en **lecture seule**
(`SupabaseDouble(read_only=True)`) : un client qui refuse toute écriture, plutôt
qu'une relecture du script. C'est la propriété la plus importante de l'outil — il
se lance sur une base de **production** — et elle se vérifie en laissant échouer la
doublure si elle est fausse (voir `NeverWritesTest`).
"""
from __future__ import annotations

import base64
import contextlib
import importlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest import mock

from core import adaptive_learning, config_runtime

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(REPO_ROOT))

from scripts import check_supabase  # noqa: E402
from database import supabase_client  # noqa: E402
from tests import supabase_double  # noqa: E402


#: Même forme que `check_supabase.result`, sans dépendre de son nom interne.
result_check = check_supabase.result


def _jwt(role: str) -> str:
    """Jeton au format Supabase, avec le rôle demandé (signature factice)."""

    def part(payload: dict) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part({'role': role, 'iss': 'supabase'})}.sig"


class SupabaseKeyRoleTest(unittest.TestCase):
    """Une clé publique à la place de la clé service_role doit être vue."""

    def test_a_service_role_jwt_is_recognized(self):
        self.assertEqual(config_runtime.supabase_key_role(_jwt("service_role")), "service_role")

    def test_an_anon_jwt_is_recognized(self):
        self.assertEqual(config_runtime.supabase_key_role(_jwt("anon")), "anon")

    def test_the_new_key_format_is_recognized(self):
        self.assertEqual(config_runtime.supabase_key_role("sb_secret_abc123"), "service_role")
        self.assertEqual(config_runtime.supabase_key_role("sb_publishable_abc123"), "anon")

    def test_an_unreadable_key_is_not_libelled(self):
        """Un format inconnu n'est pas déclaré faux : on ne peut que ne rien dire."""
        for key in ("", None, "nimportequoi", "a.b", "a.b.c.d", "eyJ.broken!!.sig"):
            with self.subTest(key=key):
                self.assertEqual(config_runtime.supabase_key_role(key), "")


class SupabaseUrlIssueTest(unittest.TestCase):
    """L'URL de l'API, pas celle de Postgres ni un `/rest/v1` collé du navigateur."""

    def test_a_project_url_is_accepted(self):
        self.assertEqual(
            config_runtime.supabase_url_issue("https://abcdefghijklmnopqrst.supabase.co"), ""
        )

    def test_a_trailing_slash_is_accepted(self):
        self.assertEqual(
            config_runtime.supabase_url_issue("https://abcdefghijklmnopqrst.supabase.co/"), ""
        )

    def test_a_self_hosted_domain_is_not_refused(self):
        """Un projet auto-hébergé n'a pas à finir par `.supabase.co`."""
        self.assertEqual(config_runtime.supabase_url_issue("https://supabase.interne.local"), "")

    def test_common_mistakes_are_named(self):
        cases = {
            "": "manquante",
            "http://abcdefghijklmnopqrst.supabase.co": "https://",
            "https://abcdefghijklmnopqrst.supabase.co/rest/v1": "chemin",
            "https://db.abcdefghijklmnopqrst.supabase.co": "Postgres",
            "https://xxxxx.supabase.co": "exemple",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertIn(expected, config_runtime.supabase_url_issue(url))


class SupabaseConfigIssuesTest(unittest.TestCase):
    """Ce que voit le démarrage (`required_issues`) et `/preflight`."""

    KEYS = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY")

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

    def test_an_anon_key_is_a_blocking_issue(self):
        os.environ["SUPABASE_URL"] = "https://abcdefghijklmnopqrst.supabase.co"
        os.environ["SUPABASE_SERVICE_KEY"] = _jwt("anon")
        issues = self._config().required_issues()
        self.assertTrue(
            any("clé publique" in issue for issue in issues), f"issues : {issues}"
        )

    def test_a_service_role_key_is_not_flagged(self):
        os.environ["SUPABASE_URL"] = "https://abcdefghijklmnopqrst.supabase.co"
        os.environ["SUPABASE_SERVICE_KEY"] = _jwt("service_role")
        cfg = self._config()
        self.assertEqual(cfg.supabase_issues(), [])
        self.assertNotIn("supabase", " ".join(cfg.required_issues()).lower())

    def test_a_database_url_is_flagged(self):
        os.environ["SUPABASE_URL"] = "https://db.abcdefghijklmnopqrst.supabase.co"
        os.environ["SUPABASE_SERVICE_KEY"] = _jwt("service_role")
        self.assertTrue(any("Postgres" in i for i in self._config().supabase_issues()))

    def test_the_health_reports_the_role_never_the_key(self):
        os.environ["SUPABASE_URL"] = "https://abcdefghijklmnopqrst.supabase.co"
        os.environ["SUPABASE_SERVICE_KEY"] = _jwt("anon")
        component = self._config().component_health()["supabase"]
        self.assertEqual(component["key_role"], "anon")
        self.assertEqual(component["url_issue"], "")
        self.assertTrue(component["ready"], "l'URL et une clé sont renseignées")
        self.assertNotIn("eyJ", json.dumps(component), "la clé ne doit jamais être publiée")


def _client(*, read_only: bool = False) -> supabase_double.SupabaseDouble:
    """Le client doublé, en mémoire — seul le régime change d'un test à l'autre.

    Une **fonction**, pas une doublure : elle rend celle du dossier
    (`tests/supabase_double.py`), qui applique les filtres, persiste les écritures
    et tient le journal. Les lignes se sèment par `client.store(nom).rows`, les
    pannes se programment par `client.fail(table, opération, erreur)` — deux
    gestes qui ont remplacé les trois classes locales.
    """
    return supabase_double.SupabaseDouble(read_only=read_only)


def _probe_failure(table: str, operation: str) -> RuntimeError:
    """Le refus qu'une panne programmée doit simuler — nommé comme la table."""
    return RuntimeError(f"{table}: opération refusée ({operation})")


def _foreign_rows() -> Dict[str, List[Dict[str, Any]]]:
    """Une ligne par table éprouvée, qui **n'est pas** celle de la sonde.

    Elles portent les colonnes sur lesquelles la sonde filtre, avec des valeurs
    qui ne sont pas les siennes : c'est ce qui rend « nettoyé » distinguable de
    « balayé » dans une table où il n'y aurait rien d'autre.
    """
    return {
        "users": [
            {
                "id": "user-reel-1",
                "username": "client",
                "first_name": "Client",
                "telegram_chat_id": 42,
                "paper_mode": True,
            }
        ],
        "insights": [
            {
                "id": "insight-reel-1",
                "asset": "EURUSD",
                "type": "analysis",
                "title": "Note réelle",
                "summary": "Une note qui n'appartient pas à la sonde.",
                "source": "application",
                "confidence": 0.5,
            }
        ],
        "pending_signals": [
            {
                "id": "signal-reel-1",
                "user_id": "user-reel-1",
                "signal": {"asset": "EURUSD", "direction": "BUY"},
                "status": "executed",
            }
        ],
        "economic_events": [
            {
                "id": "event-reel-1",
                "event_id": "event-reel-1",
                "title": "CPI réel",
                "country": "US",
                "currency": "USD",
                "event_date": "2026-01-01T00:00:00+00:00",
                "impact": "High",
            }
        ],
        "macro_bias_logs": [
            {
                "id": "macro-reel-1",
                "symbol": "EURUSD",
                "bias": "BULLISH",
                "confidence": 0.6,
                "impact": "high",
                "decision": "BUY",
                "reason": "décision réelle",
            }
        ],
        "trade_post_mortems": [
            {
                "id": "pm-reel-1",
                "signal_id": "signal-reel-1",
                "asset": "EURUSD",
                "outcome": "won",
                "learned_lesson": "leçon réelle",
            }
        ],
        "adaptive_model_weights": [
            {
                "asset": "EURUSD",
                "ta_weight": 0.4,
                "sentiment_weight": 0.3,
                "macro_weight": 0.3,
                "consecutive_losses": 0,
                "win_rate_pct": 50.0,
                "total_trades": 12,
            }
        ],
    }


@contextlib.contextmanager
def _no_real_wait():
    """Neutralise l'attente entre deux tentatives de nettoyage : les reprises
    restent, l'attente non.

    `check_supabase.time` est remplacé **dans ce module seulement** — le vrai
    `time.sleep` n'est pas touché. Sans cela, chaque test qui éprouve un reste en
    base ferait patienter la suite d'une seconde et demie par ligne restée, pour
    une temporisation qu'un client simulé ne mesure pas. Utilisable en `with`
    (pour lire l'attente demandée) comme en décorateur.
    """
    with mock.patch.object(check_supabase, "time") as fake_time:
        yield fake_time


class TableChecksTest(unittest.TestCase):
    def test_readable_tables_pass(self):
        client = _client()
        checks = check_supabase.table_checks(client, ("users", "insights"))
        self.assertEqual([check["ok"] for check in checks], [True, True])

    def test_an_unreadable_table_names_the_reason(self):
        client = _client()
        client.fail("insights", "select", _probe_failure("insights", "select"))
        checks = check_supabase.table_checks(client, ("insights",))
        self.assertFalse(checks[0]["ok"])
        self.assertIn("migration non appliquée", checks[0]["detail"])


class RoundtripTest(unittest.TestCase):
    """Écrit, relit, supprime — et ne laisse rien derrière."""

    #: Les sept tables de l'application, dans l'ordre où l'outil les éprouve.
    TABLES = (
        "users (écriture → relecture)",
        "insights (écriture → relecture)",
        "pending_signals (écriture → verdict → relecture)",
        "economic_events (écriture → relecture)",
        "macro_bias_logs (écriture → relecture)",
        "trade_post_mortems (écriture → relecture)",
        "adaptive_model_weights (écriture → relecture)",
    )

    def test_the_seven_tables_are_exercised_and_cleaned(self):
        client = _client()
        checks = check_supabase.roundtrip_checks(client)
        self.assertEqual([check["name"] for check in checks], [*self.TABLES, "nettoyage"])
        self.assertTrue(all(check["ok"] for check in checks), f"échecs : {checks}")
        for table in (
            "users",
            "insights",
            "pending_signals",
            "economic_events",
            "macro_bias_logs",
            "trade_post_mortems",
            "adaptive_model_weights",
        ):
            with self.subTest(table=table):
                self.assertEqual(client.store(table).rows, [], f"sonde restée dans {table}")

    def test_the_report_shows_what_happened(self):
        checks = {check["name"]: check for check in check_supabase.roundtrip_checks(_client())}
        self.assertIn("probe-", checks["users (écriture → relecture)"]["detail"])
        self.assertIn("get_recent_insights", checks["insights (écriture → relecture)"]["detail"])
        self.assertIn("statut=executed", checks["pending_signals (écriture → verdict → relecture)"]["detail"])
        self.assertIn(
            "get_economic_events", checks["economic_events (écriture → relecture)"]["detail"]
        )
        self.assertIn("symbol=PROBE-", checks["macro_bias_logs (écriture → relecture)"]["detail"])
        self.assertIn(
            "get_learning_summary", checks["trade_post_mortems (écriture → relecture)"]["detail"]
        )
        self.assertIn(
            "get_adaptive_parameters",
            checks["adaptive_model_weights (écriture → relecture)"]["detail"],
        )

    def test_a_failed_write_is_reported_and_still_cleaned(self):
        client = _client()
        client.fail("pending_signals", "insert", _probe_failure("pending_signals", "insert"))
        checks = {check["name"]: check for check in check_supabase.roundtrip_checks(client)}
        self.assertFalse(checks["aller-retour"]["ok"], "l'échec doit être visible")
        self.assertTrue(checks["nettoyage"]["ok"], "le nettoyage doit avoir lieu malgré l'échec")
        self.assertEqual(client.store("users").rows, [])

    def test_the_engine_tables_are_checked_even_after_an_earlier_failure(self):
        """Un échec sur `users` ne dit rien des quatre tables des migrations 005/006."""
        client = _client()
        client.fail("users", "upsert", _probe_failure("users", "upsert"))
        checks = {check["name"]: check for check in check_supabase.roundtrip_checks(client)}
        self.assertFalse(checks["aller-retour"]["ok"])
        for name in self.TABLES[3:]:
            with self.subTest(table=name):
                self.assertTrue(checks[name]["ok"], checks[name]["detail"])

    def test_a_failing_engine_table_does_not_hide_the_others(self):
        """Chaque table a son verdict : sinon le rapport ne dit pas où ça casse."""
        client = _client()
        client.fail("macro_bias_logs", "select", _probe_failure("macro_bias_logs", "select"))
        checks = {check["name"]: check for check in check_supabase.roundtrip_checks(client)}
        self.assertFalse(checks["macro_bias_logs (écriture → relecture)"]["ok"])
        for name in ("economic_events (écriture → relecture)",
                     "trade_post_mortems (écriture → relecture)",
                     "adaptive_model_weights (écriture → relecture)"):
            with self.subTest(table=name):
                self.assertTrue(checks[name]["ok"], checks[name]["detail"])

    @_no_real_wait()
    def test_the_cleanup_names_the_engine_tables_it_could_not_empty(self):
        client = _client()
        for table in ("economic_events", "adaptive_model_weights"):
            client.fail(table, "delete", _probe_failure(table, "delete"))
        checks = {check["name"]: check for check in check_supabase.roundtrip_checks(client)}
        cleanup = checks["nettoyage"]
        self.assertFalse(cleanup["ok"])
        self.assertIn("economic_events.id=ff_probe_", cleanup["detail"])
        self.assertIn("adaptive_model_weights.asset=PROBE", cleanup["detail"])
        self.assertIn("à la main", cleanup["detail"])

    def test_the_learning_tables_are_not_written_by_the_heavy_function(self):
        """`record_trade_settlement_and_learn` réécrit aussi une note du savoir."""
        with mock.patch.object(
            adaptive_learning, "record_trade_settlement_and_learn"
        ) as heavy:
            checks = check_supabase.roundtrip_checks(_client())
        self.assertTrue(all(check["ok"] for check in checks), f"échecs : {checks}")
        self.assertFalse(heavy.called, "une sonde n'a pas à réécrire une note du savoir")

    def test_the_referencing_table_is_deleted_before_the_referenced_one(self):
        """`pending_signals.user_id` référence `users.id` : l'ordre n'est pas cosmétique.

        La doublure note chaque écriture **réellement exécutée** avec sa table :
        l'ordre se lit donc dans le journal des écritures, et non dans un
        enregistreur de plus.
        """
        client = _client()
        checks = check_supabase.roundtrip_checks(client)
        self.assertTrue(all(check["ok"] for check in checks), f"échecs : {checks}")
        deleted = client.write_order("delete")
        self.assertIn("pending_signals", deleted)
        self.assertIn("users", deleted)
        self.assertLess(
            deleted.index("pending_signals"),
            deleted.index("users"),
            f"suppressions dans l'ordre : {deleted}",
        )

    @_no_real_wait()
    def test_a_failed_cleanup_says_what_to_remove_by_hand(self):
        client = _client()
        client.fail("insights", "delete", _probe_failure("insights", "delete"))
        checks = check_supabase.roundtrip_checks(client)
        cleanup = checks[-1]
        self.assertFalse(cleanup["ok"])
        self.assertIn("insights.asset=PROBE", cleanup["detail"])
        self.assertIn("à la main", cleanup["detail"])

    @_no_real_wait()
    def test_the_leftovers_are_published_as_data_not_as_a_sentence(self):
        """Le code de sortie, l'alerte et la route lisent une **liste**.

        Uniquement dans le détail écrit en français obligerait chacun d'eux à
        relire une phrase pour savoir s'il reste quelque chose en base.
        """
        client = _client()
        client.fail("insights", "delete", _probe_failure("insights", "delete"))
        cleanup = check_supabase.roundtrip_checks(client)[-1]
        self.assertEqual(cleanup["name"], check_supabase.CLEANUP_CHECK)
        self.assertEqual(len(cleanup["leftovers"]), 1)
        self.assertIn("insights.asset=PROBE", cleanup["leftovers"][0])

    def test_a_clean_cleanup_publishes_an_empty_list(self):
        """Vide, jamais absent : le nettoyage dit **toujours** ce qu'il a laissé."""
        cleanup = check_supabase.roundtrip_checks(_client())[-1]
        self.assertTrue(cleanup["ok"])
        self.assertEqual(cleanup["leftovers"], [])

    def test_a_transient_delete_failure_does_not_leave_a_row_behind(self):
        """Déclarer un reste sur un hoquet coûte deux fois : le code `3`, qu'on ne
        rejoue pas, et l'opérateur envoyé supprimer une ligne déjà partie."""
        client = _client()
        #: Une panne **transitoire** : la liste est consommée une tentative par
        #: élément, donc la reprise de la sonde repasse — et c'est ce qu'on éprouve.
        client.fail("insights", "delete", [_probe_failure("insights", "delete")])
        with _no_real_wait() as tick:
            cleanup = check_supabase.roundtrip_checks(client)[-1]
        self.assertTrue(cleanup["ok"], cleanup["detail"])
        self.assertEqual(cleanup["leftovers"], [])
        self.assertEqual(client.store("insights").rows, [], "la ligne doit être partie")
        self.assertEqual(tick.sleep.call_count, 1, "une reprise a suffi")

    def test_the_wait_between_attempts_grows_and_stays_short(self):
        """Croissante : le premier refus est un hoquet, le suivant n'en est plus un."""
        client = _client()
        client.fail("insights", "delete", _probe_failure("insights", "delete"))
        with _no_real_wait() as tick:
            check_supabase.roundtrip_checks(client)
        waits = [call.args[0] for call in tick.sleep.call_args_list]
        self.assertTrue(waits, "aucune reprise n'est configurée : un hoquet laisserait une ligne")
        self.assertEqual(waits, list(check_supabase.CLEANUP_RETRY_DELAYS))
        self.assertEqual(waits, sorted(waits), "l'attente doit croître")
        self.assertTrue(all(wait > 0 for wait in waits))
        self.assertLess(sum(waits), 5, "une sonde ne doit pas s'endormir")

    def test_the_leftover_is_declared_only_after_the_last_attempt(self):
        """Un refus **définitif** reste un reste : réessayer ne doit rien masquer."""
        client = _client()
        client.fail("insights", "delete", _probe_failure("insights", "delete"))
        with _no_real_wait() as tick:
            cleanup = check_supabase.roundtrip_checks(client)[-1]
        self.assertFalse(cleanup["ok"])
        self.assertEqual(len(cleanup["leftovers"]), 1)
        self.assertIn("insights.asset=PROBE", cleanup["leftovers"][0])
        #: Les **tentatives**, pas les effets : une suppression refusée n'entre pas
        #: dans le journal des écritures (`write_order`), mais elle a bel et bien
        #: été demandée — c'est le journal des requêtes de la table qui les compte.
        self.assertEqual(
            len(client.store("insights").operations("delete")),
            len(check_supabase.CLEANUP_RETRY_DELAYS) + 1,
            "une tentative, puis une par délai d'attente",
        )
        self.assertEqual(
            tick.sleep.call_count,
            len(check_supabase.CLEANUP_RETRY_DELAYS),
            "une attente par reprise, et pas une de plus",
        )

    def test_the_cleanup_of_a_persistent_failure_says_so(self):
        """Le motif rapporté est celui de la **dernière** tentative."""
        client = _client()
        client.fail("insights", "delete", _probe_failure("insights", "delete"))
        with _no_real_wait():
            cleanup = check_supabase.roundtrip_checks(client)[-1]
        self.assertIn("RuntimeError", cleanup["leftovers"][0])
        self.assertIn("à la main", cleanup["detail"])

    def test_the_probe_uses_the_application_functions(self):
        """Sinon l'outil vérifierait ses propres requêtes, pas l'application."""
        # Les espions enveloppent les fonctions réelles : l'aller-retour doit
        # rester vert tout en passant par elles (sinon la liste est vide).
        sources = (
            ("insert_insight", supabase_client),
            ("get_recent_insights", supabase_client),
            ("create_pending_signal", supabase_client),
            ("update_signal_status", supabase_client),
            ("upsert_economic_events", supabase_client),
            ("get_economic_events", supabase_client),
            ("log_macro_decision", supabase_client),
            ("get_learning_summary", adaptive_learning),
            ("get_adaptive_parameters", adaptive_learning),
        )
        with contextlib.ExitStack() as stack:
            spies = {
                name: stack.enter_context(
                    mock.patch.object(check_supabase, name, wraps=getattr(source, name))
                )
                for name, source in sources
            }
            checks = check_supabase.roundtrip_checks(_client())
        self.assertTrue(all(check["ok"] for check in checks), f"échecs : {checks}")
        for name, spy in spies.items():
            self.assertTrue(spy.called, f"{name} doit être celle utilisée par l'aller-retour")

    def test_an_error_inside_an_application_function_is_visible(self):
        """L'échec de la fonction applicative doit remonter dans le rapport."""
        with mock.patch.object(
            check_supabase, "insert_insight", side_effect=RuntimeError("insights refusée")
        ):
            checks = {check["name"]: check for check in check_supabase.roundtrip_checks(_client())}
        self.assertFalse(checks["aller-retour"]["ok"], f"échecs attendus : {checks}")
        self.assertIn("insights refusée", checks["aller-retour"]["detail"])
        self.assertTrue(checks["nettoyage"]["ok"], "le nettoyage doit avoir lieu malgré l'échec")


class RoundtripLeakTest(unittest.TestCase):
    """Aucune ligne de sonde ne survit au nettoyage — même quand une étape échoue.

    Ces tests regardent la **base**, pas le rapport : « il ne reste rien » se lit
    dans les lignes vivantes de la doublure (`client.store(table).rows`). Un
    rapport peut se tromper, et c'est exactement ce qu'un outil lancé sur une base
    de production ne doit pas faire — le code de sortie `3` n'existe que parce
    qu'une ligne restée doit se dire.

    Les points d'injection ne sont pas recopiés à la main : ils sont **lus dans le
    journal d'un passage propre** (`client.store(table).operations(...)`). Une étape
    qui disparaît du script disparaît de cette épreuve, une étape qui s'ajoute y
    entre sans qu'on touche à ce fichier — une liste écrite à la main serait
    périmée au premier remaniement, et personne ne le verrait.

    Deux moitiés, parce que la propriété en a deux : une écriture ou une relecture
    qui échoue ne laisse **rien**, et une suppression refusée ne laisse que la
    ligne qu'elle **nomme** — ni ligne cachée, ni ligne annoncée à tort.
    """

    #: Les sept tables de l'aller-retour, nommées : une table oubliée se voit.
    TABLES = (
        "users",
        "insights",
        "pending_signals",
        "economic_events",
        "macro_bias_logs",
        "trade_post_mortems",
        "adaptive_model_weights",
    )
    #: Les écritures qu'une panne peut viser, avec `select` et `delete` : ce sont
    #: les opérations que la doublure sait nommer (voir `_OPERATION_ERRORS`).
    WRITES = ("insert", "upsert", "update")

    def _points(self):
        """Les couples (table, opération) qu'un passage propre exerce vraiment.

        Le passage de référence doit être vert : sinon les points d'injection
        décriraient un aller-retour déjà cassé, et l'épreuve ne dirait plus rien.
        """
        client = _client()
        checks = check_supabase.roundtrip_checks(client)
        self.assertTrue(all(check["ok"] for check in checks), f"référence cassée : {checks}")
        self.assertEqual(
            sorted(client.tables()), sorted(self.TABLES), "le tour des tables a changé"
        )
        return [
            (table, operation)
            for table in self.TABLES
            for operation in ("select", *self.WRITES, "delete")
            if client.store(table).operations(operation)
        ]

    def _in_base(self, client):
        """Les lignes encore en base, table par table (les tables vides sont omises)."""
        return {
            table: client.store(table).rows
            for table in self.TABLES
            if client.store(table).rows
        }

    def test_a_failed_write_or_read_never_leaves_a_probe_row(self):
        """Un échec en cours de route ne doit pas laisser de ligne derrière lui.

        C'est la moitié qui se perd le plus facilement : une relecture qui échoue
        après une écriture réussie, par exemple — si la cible de nettoyage n'était
        posée qu'après le verdict, la ligne resterait en base sans que le rapport
        le dise.
        """
        points = [point for point in self._points() if point[1] != "delete"]
        self.assertGreaterEqual(len(points), 10, f"trop peu de points d'injection : {points}")
        for table, operation in points:
            with self.subTest(table=table, operation=operation):
                client = _client()
                client.fail(table, operation, _probe_failure(table, operation))
                cleanup = check_supabase.roundtrip_checks(client)[-1]
                self.assertEqual(self._in_base(client), {}, "ligne de sonde restée en base")
                self.assertTrue(cleanup["ok"], cleanup["detail"])
                self.assertEqual(cleanup["leftovers"], [])

    @_no_real_wait()
    def test_a_refused_delete_leaves_exactly_the_row_it_names(self):
        """Une suppression refusée est le seul reste possible — et il est nommé.

        L'égalité est tenue dans les deux sens : ce qui reste en base est
        exactement ce que `leftovers` nomme, et rien n'est nommé à tort. Une ligne
        annoncée fait envoyer quelqu'un supprimer une ligne déjà partie ; une ligne
        restée et non annoncée est un dégât silencieux.
        """
        for table, operation in self._points():
            if operation != "delete":
                continue
            with self.subTest(table=table):
                client = _client()
                client.fail(table, "delete", _probe_failure(table, "delete"))
                cleanup = check_supabase.roundtrip_checks(client)[-1]

                stayed = {name: len(rows) for name, rows in self._in_base(client).items()}
                named: Dict[str, int] = {}
                for row in cleanup["leftovers"]:
                    name = row.split(".", 1)[0]
                    named[name] = named.get(name, 0) + 1
                self.assertEqual(
                    stayed, named, "ce qui reste en base et ce qui est nommé doivent coïncider"
                )
                self.assertEqual(named, {table: 1}, f"seule {table} devait rester")
                self.assertFalse(cleanup["ok"])

    def test_a_row_that_is_not_the_probe_survives_the_roundtrip(self):
        """La sonde écrit et supprime **ses** lignes, et rien d'autre.

        Sans ce test, un nettoyage qui viderait les tables passerait aussi le
        premier : « il ne reste aucune ligne de sonde » ne distingue pas
        « nettoyé » de « balayé » tant qu'il n'y a rien d'autre dans la table. Or
        l'outil tourne sur la base de production.
        """
        client = _client()
        strangers = _foreign_rows()
        for table, rows in strangers.items():
            client.store(table).rows = [dict(row) for row in rows]
        checks = check_supabase.roundtrip_checks(client)
        self.assertTrue(all(check["ok"] for check in checks), f"échecs : {checks}")
        for table, rows in strangers.items():
            with self.subTest(table=table):
                self.assertEqual(
                    client.store(table).rows, rows, f"{table} : ligne étrangère perdue"
                )


class SimultaneousRunTest(unittest.TestCase):
    """Deux vérifications en parallèle ne se prennent pas leurs lignes.

    Le scénario n'est pas théorique : deux branches en CI lancent la sonde en même
    temps, un opérateur la relance pendant qu'elle tourne — et les deux visent la
    **même** base, celle de production, c'est la raison d'être de l'outil. Tant que
    les lignes de sonde portaient un actif **constant** (`PROBE`), la première
    exécution à nettoyer emportait la ligne de l'autre : celle-ci relisait un actif
    disparu et se déclarait en échec alors qu'aucune sonde n'était cassée, pendant
    que `total_trades` se lisait sur la ligne de la voisine.

    L'entrelacement est **réel**, pas simulé sur une liste d'appels : une seconde
    sonde complète — écriture, relecture, nettoyage — s'exécute pendant la première,
    au moment précis où le doublon se voit, c'est-à-dire entre l'écriture d'une ligne
    et sa relecture. Les trois lectures qui portent sur un actif de sonde passent par
    l'application (`get_recent_insights`, `get_learning_summary`,
    `get_adaptive_parameters`) : c'est donc là que la seconde se glisse, une fois par
    lecture.
    """

    #: Les sept tables de l'aller-retour (mêmes noms que `RoundtripLeakTest`).
    TABLES = (
        "users",
        "insights",
        "pending_signals",
        "economic_events",
        "macro_bias_logs",
        "trade_post_mortems",
        "adaptive_model_weights",
    )
    #: Les lectures qui touchent la ligne d'une sonde : chacune doit retrouver la
    #: sienne, même quand une autre exécution vient d'écrire et de nettoyer à côté.
    READINGS = ("get_recent_insights", "get_learning_summary", "get_adaptive_parameters")

    def test_a_second_run_does_not_take_the_first_ones_rows(self):
        client = _client()
        runs: List[List[Dict[str, Any]]] = []
        inside = False

        def with_other_run(name: str):
            """Encadre une lecture : la première fois, une autre sonde tourne entièrement."""
            real = getattr(check_supabase, name)

            def wrapper(*args, **kwargs):
                nonlocal inside
                if not inside:
                    inside = True
                    try:
                        runs.append(check_supabase.roundtrip_checks(client))
                    finally:
                        inside = False
                return real(*args, **kwargs)

            return mock.patch.object(check_supabase, name, wrapper)

        with contextlib.ExitStack() as stack:
            for name in self.READINGS:
                stack.enter_context(with_other_run(name))
            checks = check_supabase.roundtrip_checks(client)

        self.assertEqual(len(runs), len(self.READINGS), "une sonde concurrente par lecture")
        for index, run in enumerate(runs):
            failed = [check for check in run if not check["ok"]]
            self.assertEqual(failed, [], f"sonde concurrente {index} : {failed}")
        failed = [check for check in checks if not check["ok"]]
        self.assertEqual(failed, [], f"la première sonde a subi les autres : {failed}")
        in_base = {
            table: client.store(table).rows
            for table in self.TABLES
            if client.store(table).rows
        }
        self.assertEqual(in_base, {}, "ligne de sonde restée en base")


class NeverWritesTest(unittest.TestCase):
    """Le chemin qui n'a pas demandé `--roundtrip` n'écrit rien — et il le prouve.

    C'est la propriété la plus sérieuse de l'outil : `scripts/check_supabase.py` se
    lance sur la base de **production**, sans demander la permission à personne. La
    vérifier en relisant le script ne prouve rien (une fonction appelée plus loin
    peut écrire) ; elle se vérifie en montant le client en **lecture seule** : toute
    écriture y lève, donc un seul `insert` oublié dans le chemin de lecture ferait
    échouer ces tests — en nommant la table, l'opération et le test fautif.

    Les deux moitiés comptent, et dans cet ordre : que le chemin de lecture **lise**
    (sinon « rien écrit » ne dirait rien), et qu'une écriture soit **bel et bien**
    refusée (sinon le garde-fou serait décoratif).
    """

    def _read_only_run(self, **kwargs):
        client = _client(read_only=True)
        with mock.patch.object(check_supabase, "configuration_checks", return_value=[]):
            report = check_supabase.run(
                roundtrip=False, as_json=False, client=client, **kwargs
            )
        return client, report

    def test_the_default_run_reads_tables_without_writing_anything(self):
        client, report = self._read_only_run()

        self.assertTrue(report["ok"], f"rapport : {report}")
        self.assertTrue(client.operations("select"), "aucune lecture : le test ne prouve rien")
        self.assertIn("users", client.tables(), "les tables de l'application sont lues")
        self.assertEqual(client.writes, [], "la sonde a écrit sur une base en lecture seule")
        titles = [section["title"] for section in report["sections"]]
        self.assertNotIn("4. Aller-retour écriture/lecture", titles, "sans `--roundtrip`")

    def test_no_probe_row_is_left_in_the_base(self):
        client, _report = self._read_only_run()
        for table in ("users", "insights", "pending_signals", "economic_events"):
            with self.subTest(table=table):
                self.assertEqual(client.store(table).rows, [], f"une ligne est apparue dans {table}")

    def test_a_restricted_run_reads_only_what_it_names(self):
        """`--only` restreint ce qui est **lu** — il ne restreint aucune écriture."""
        client, report = self._read_only_run(only=["economic_events"])

        self.assertTrue(report["ok"], f"rapport : {report}")
        self.assertEqual(client.tables(), ["economic_events"], "tables lues")
        self.assertEqual(client.writes, [])

    @_no_real_wait()
    def test_a_write_would_be_refused_and_named(self):
        """Sans cette moitié, les tests ci-dessus ne prouveraient rien du tout."""
        client = _client(read_only=True)
        with mock.patch.object(check_supabase, "configuration_checks", return_value=[]):
            report = check_supabase.run(roundtrip=True, as_json=False, client=client)

        self.assertFalse(report["ok"], "un aller-retour en lecture seule ne peut être vert")
        details = " ".join(
            check["detail"] for section in report["sections"] for check in section["checks"]
        )
        self.assertIn("ReadOnlyError", details, "le refus doit nommer ce qui a été tenté")
        self.assertEqual(client.writes, [], "rien n'est écrit, même quand l'aller-retour est demandé")

    @_no_real_wait()
    def test_the_report_over_states_what_a_refused_write_left_behind(self):
        """Un faux reste plutôt qu'un reste tu : le sens de l'erreur, et ses limites.

        La sonde pose la **cible de nettoyage avant l'écriture** (pour qu'une
        écriture à moitié réussie soit nettoyée quand même) : sur un client qui
        refuse tout, elle annonce donc un reste qu'elle n'a pas créé. C'est le bon
        sens — un vrai reste tu coûterait une base polluée —, et c'est la doublure
        qui montre les deux moitiés : le rapport alerte, et `rows` reste vide.
        """
        client = _client(read_only=True)
        with mock.patch.object(check_supabase, "configuration_checks", return_value=[]):
            report = check_supabase.run(roundtrip=True, as_json=False, client=client)

        for table in client.tables():
            with self.subTest(table=table):
                self.assertEqual(client.store(table).rows, [])
        self.assertTrue(report["leftovers"], "elle préfère annoncer un reste possible")
        self.assertEqual(client.writes, [], "et rien n'existe : aucune écriture n'a eu lieu")


class ResolveOnlyTest(unittest.TestCase):
    """`--only` : ce qui est visé, et le refus **franc** d'un nom inconnu."""

    def test_no_selection_means_no_filter(self):
        selection = check_supabase.resolve_only(None)
        self.assertTrue(selection["ok"], selection["problem"])
        self.assertEqual(selection["requested"], [])
        self.assertEqual(selection["required"], list(supabase_client.REQUIRED_TABLES))
        self.assertEqual(selection["optional"], list(supabase_client.OPTIONAL_TABLES))

    def test_a_table_name_selects_exactly_it(self):
        selection = check_supabase.resolve_only(["economic_events"])
        self.assertTrue(selection["ok"], selection["problem"])
        self.assertEqual(selection["required"], ["economic_events"])

    def test_an_optional_table_is_selected_in_its_own_section(self):
        selection = check_supabase.resolve_only(["bot_settings"])
        self.assertEqual(selection["required"], [])
        self.assertEqual(selection["optional"], ["bot_settings"])

    def test_a_group_resolves_to_its_tables_in_the_canonical_order(self):
        """L'ordre de `REQUIRED_TABLES`, pas l'ordre de frappe."""
        selection = check_supabase.resolve_only(["engine"])
        self.assertEqual(
            selection["required"],
            [
                "economic_events",
                "macro_bias_logs",
                "trade_post_mortems",
                "adaptive_model_weights",
            ],
        )

    def test_the_case_of_a_name_does_not_decide_whether_it_is_known(self):
        """Postgres ne connaît que les minuscules ; la casse tapée n'est pas une intention."""
        selection = check_supabase.resolve_only(["ECONOMIC_EVENTS", "Engine"])
        self.assertTrue(selection["ok"], selection["problem"])
        self.assertEqual(
            selection["required"],
            [
                "economic_events",
                "macro_bias_logs",
                "trade_post_mortems",
                "adaptive_model_weights",
            ],
        )

    def test_comma_separated_and_repeated_values_are_both_accepted(self):
        selection = check_supabase.resolve_only(["users, insights", "pending_signals"])
        #: L'ordre est celui de `REQUIRED_TABLES` (`pending_signals` y précède
        #: `insights`), pas celui de la frappe : un rapport filtré se lit comme un
        #: rapport complet.
        self.assertEqual(selection["required"], ["users", "pending_signals", "insights"])

    def test_an_unknown_name_is_refused_and_named(self):
        selection = check_supabase.resolve_only(["economic_events", "economic_event"])
        self.assertFalse(selection["ok"])
        self.assertEqual(selection["unknown"], ["economic_event"])
        self.assertIn("economic_event", selection["problem"])

    def test_the_refusal_message_says_what_can_be_typed(self):
        """Après un refus, ce qu'on cherche est précisément **quoi taper**."""
        message = check_supabase.selection_problem(check_supabase.resolve_only(["nope"]))
        for group in check_supabase.TABLE_GROUPS:
            with self.subTest(group=group):
                self.assertIn(group, message)
        for table in supabase_client.REQUIRED_TABLES:
            with self.subTest(table=table):
                self.assertIn(table, message)
        self.assertIn("--only economic_events", message)

    def test_an_option_without_a_name_is_refused(self):
        """`--only ""` est une option **tapée** : elle ne vaut pas « tout »."""
        for names in ([""], [","]):
            with self.subTest(names=names):
                selection = check_supabase.resolve_only(names)
                self.assertFalse(selection["ok"])
                self.assertIn("au moins un nom", selection["problem"])

    def test_an_empty_list_is_not_a_selection_either_way(self):
        """Une liste vide (appel programmatique) veut dire « tout », pas « rien »."""
        selection = check_supabase.resolve_only([])
        self.assertTrue(selection["ok"], selection["problem"])
        self.assertEqual(selection["required"], list(supabase_client.REQUIRED_TABLES))


    def test_the_selection_detail_names_the_tables_not_only_the_group(self):
        """Afficher `engine` seul laisserait croire à une table nommée `engine`."""
        detail = check_supabase.selection_detail(check_supabase.resolve_only(["engine"]))
        self.assertIn("economic_events", detail)
        self.assertIn("engine", detail)


class RoundtripSelectionTest(unittest.TestCase):
    """L'aller-retour restreint : ce qui n'est pas visé n'est **pas écrit**."""

    def test_a_single_table_is_the_only_one_probed(self):
        client = _client()
        checks = check_supabase.roundtrip_checks(client, ["economic_events"])
        self.assertEqual(
            [check["name"] for check in checks],
            ["economic_events (écriture → relecture)", "nettoyage"],
        )
        for table in ("users", "insights", "pending_signals"):
            with self.subTest(table=table):
                self.assertEqual(client.store(table).rows, [], f"{table} a été écrite")

    def test_a_group_probes_its_four_tables(self):
        """Le groupe est résolu en amont : `roundtrip_checks` ne voit que des tables."""
        resolved = check_supabase.resolve_only(["engine"])["required"]
        checks = check_supabase.roundtrip_checks(_client(), resolved)
        self.assertEqual(
            [check["name"] for check in checks],
            [
                "economic_events (écriture → relecture)",
                "macro_bias_logs (écriture → relecture)",
                "trade_post_mortems (écriture → relecture)",
                "adaptive_model_weights (écriture → relecture)",
                "nettoyage",
            ],
        )

    def test_targeting_pending_signals_still_writes_the_user_it_needs(self):
        """La clé étrangère n'est pas négociable : on écrit, mais on ne rapporte pas."""
        client = _client()
        checks = check_supabase.roundtrip_checks(client, ["pending_signals"])
        self.assertEqual(
            [check["name"] for check in checks],
            ["pending_signals (écriture → verdict → relecture)", "nettoyage"],
        )
        self.assertTrue(all(check["ok"] for check in checks), f"échecs : {checks}")
        self.assertEqual(client.store("users").rows, [], "la ligne utilitaire reste")
        self.assertEqual(client.store("insights").rows, [])

    def test_a_selection_outside_the_roundtrip_says_so_instead_of_being_empty(self):
        client = _client()
        checks = check_supabase.roundtrip_checks(client, ["knowledge_base"])
        self.assertEqual([check["name"] for check in checks], ["aller-retour"])
        self.assertTrue(checks[0]["ok"], "rien à faire n'est pas un échec")
        self.assertIn("knowledge_base", checks[0]["detail"])
        self.assertEqual(client.tables(), [], "aucune table ne doit avoir été touchée")

    def test_everything_is_still_probed_without_a_selection(self):
        self.assertEqual(len(check_supabase.roundtrip_checks(_client())), 8)


class RunAndExitCodeTest(unittest.TestCase):
    """Le verdict global et le code de sortie, sans toucher à la base."""

    def test_no_client_means_no_table_check_and_a_clear_reason(self):
        with mock.patch.object(check_supabase, "configuration_checks", return_value=[]), \
             mock.patch.object(
                 check_supabase, "client_status", return_value={"ready": False, "error": "boom"}
             ):
            report = check_supabase.run(roundtrip=True, as_json=True)
        self.assertFalse(report["ok"])
        self.assertEqual(report["sections"][-1]["title"], "2. Base")
        detail = report["sections"][-1]["checks"][0]["detail"]
        self.assertIn("boom", detail, "la raison du client absent doit être rapportée")
        self.assertNotIn("insights", detail.lower(), "le rapport ne doit pas parler de tables")

    def test_a_whole_run_with_a_client_is_green(self):
        with mock.patch.object(check_supabase, "configuration_checks", return_value=[]):
            report = check_supabase.run(roundtrip=True, as_json=True, client=_client())
        self.assertTrue(report["ok"], f"rapport : {report}")
        titles = [section["title"] for section in report["sections"]]
        self.assertIn("4. Aller-retour écriture/lecture", titles)

    def test_the_default_run_still_checks_every_table(self):
        """Sans `--only`, la sélection vaut **toutes** les tables — jamais aucune."""
        with mock.patch.object(check_supabase, "configuration_checks", return_value=[]):
            report = check_supabase.run(roundtrip=False, as_json=False, client=_client())
        tables = report["sections"][1]["checks"]
        self.assertEqual(
            [check["name"] for check in tables], list(supabase_client.REQUIRED_TABLES)
        )

    def test_an_optional_table_missing_does_not_fail_the_run(self):
        client = _client()
        client.fail("bot_settings", "select", _probe_failure("bot_settings", "select"))
        with mock.patch.object(check_supabase, "configuration_checks", return_value=[]):
            report = check_supabase.run(roundtrip=False, as_json=True, client=client)
        self.assertTrue(report["ok"], f"rapport : {report}")

    def test_exit_codes(self):
        silence = contextlib.redirect_stdout(io.StringIO())
        with mock.patch.object(check_supabase, "configuration_checks", return_value=[]), \
             mock.patch.object(
                 check_supabase, "client_status", return_value={"ready": False, "error": "boom"}
             ), silence:
            self.assertEqual(check_supabase.main(["--json"]), 1, "sans base joignable")
        with mock.patch.object(check_supabase, "configuration_checks", return_value=[]), \
             mock.patch.object(
                 check_supabase, "client_status", return_value={"ready": True, "error": None}
             ), mock.patch.object(check_supabase, "table_checks", return_value=[]), \
             mock.patch.object(check_supabase, "roundtrip_checks", return_value=[]), silence:
            self.assertEqual(check_supabase.main([]), 0)

    def test_bad_usage_exits_with_two(self):
        with self.assertRaises(SystemExit) as raised:
            check_supabase.main(["--inconnu"])
        self.assertEqual(raised.exception.code, 2)

    def test_only_restricts_the_report_to_the_selected_tables(self):
        with mock.patch.object(check_supabase, "configuration_checks", return_value=[]):
            report = check_supabase.run(
                roundtrip=False, as_json=False, client=_client(), only=["economic_events"]
            )
        titles = [section["title"] for section in report["sections"]]
        self.assertIn("2. Tables — sélection : economic_events", titles)
        self.assertNotIn("3. Tables facultatives", titles, "rien à y vérifier")
        checks = report["sections"][1]["checks"]
        self.assertEqual([check["name"] for check in checks], ["--only", "economic_events"])
        self.assertTrue(checks[0]["ok"], checks[0]["detail"])

    def test_an_unknown_name_fails_the_run_without_touching_the_base(self):
        """Un repli silencieux sur « tout » écrirait les sept tables avec `--roundtrip`."""
        client = _client()
        with mock.patch.object(check_supabase, "configuration_checks", return_value=[]):
            report = check_supabase.run(
                roundtrip=True, as_json=False, client=client, only=["economic_event"]
            )
        self.assertFalse(report["ok"])
        self.assertEqual(len(report["sections"]), 2, "le rapport s'arrête à la sélection")
        self.assertIn("Nom inconnu", report["sections"][1]["checks"][0]["detail"])
        self.assertEqual(client.tables(), [], "aucune écriture pour un nom inconnu")

    def test_the_option_is_validated_before_anything_else_runs(self):
        """Une faute de frappe ne doit ni lire `.env`, ni commencer une écriture."""
        with mock.patch.object(check_supabase, "run") as run, mock.patch.object(
            check_supabase, "load_project_env"
        ) as load, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(check_supabase.main(["--only", "nope", "--roundtrip"]), 2)
        self.assertFalse(run.called)
        self.assertFalse(load.called)
        self.assertIn("nope", err.getvalue())
        self.assertIn("--only economic_events", err.getvalue())

    def test_the_selection_is_forwarded_to_run(self):
        with mock.patch.object(
            check_supabase, "run", return_value={"sections": [], "ok": True}
        ) as run, mock.patch.object(
            check_supabase, "load_project_env", return_value=False
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(check_supabase.main(["--json", "--only", "engine"]), 0)
        run.assert_called_once_with(roundtrip=False, as_json=True, only=["engine"])


class LeftoverContractTest(unittest.TestCase):
    """Un nettoyage incomplet n'est pas un échec comme les autres.

    Tous les autres se corrigent et se rejouent ; celui-là a **laissé des lignes
    dans la base** — la base partagée, avec l'actif fictif `PROBE`. Il a donc ses
    signaux propres : une liste structurée dans le rapport, un journal sur
    `stderr` (pour que `--json` reste lisible sur `stdout`), un code de sortie
    distinct, et une alerte au chat d'exploitation.
    """

    LEFTOVER = "insights.asset=PROBE (TypeError: refus)"

    def setUp(self) -> None:
        self.report = {
            "sections": [
                {
                    "title": "4. Aller-retour écriture/lecture",
                    "checks": [
                        {
                            "name": check_supabase.CLEANUP_CHECK,
                            "ok": False,
                            "detail": "à supprimer à la main",
                            "leftovers": [self.LEFTOVER],
                        }
                    ],
                }
            ],
            "ok": False,
            "leftovers": [self.LEFTOVER],
        }

    def _main(self, argv):
        """`main()` avec la sonde doublée, l'alerte muette et **sans** `.env`.

        Rien n'est interrogé et rien n'est envoyé : ce qui est mesuré est la
        décision de sortie et ce qui part sur les deux flux.
        """
        with mock.patch.object(check_supabase, "run", return_value=dict(self.report)), \
             mock.patch.object(check_supabase, "load_project_env", return_value=False), \
             mock.patch.object(check_supabase, "alert_leftovers") as alert:
            code = check_supabase.main(argv)
        return code, alert

    def _silent(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code, alert = self._main(argv)
        return code, out.getvalue(), err.getvalue(), alert

    # -- ce que le rapport porte ------------------------------------------ #

    @_no_real_wait()
    def test_the_run_gathers_the_leftovers_from_the_report(self):
        """Rassemblés là où le nettoyage les constate, pas devinés ailleurs."""
        client = _client()
        client.fail("insights", "delete", _probe_failure("insights", "delete"))
        with mock.patch.object(check_supabase, "configuration_checks", return_value=[]):
            report = check_supabase.run(
                roundtrip=True, as_json=False, client=client
            )
        self.assertFalse(report["ok"])
        self.assertIn("insights.asset=PROBE", report["leftovers"][0])

    def test_the_key_exists_even_when_nothing_is_left(self):
        """« aucun reste » et « clé absente » ne doivent pas se confondre."""
        with mock.patch.object(check_supabase, "configuration_checks", return_value=[]):
            report = check_supabase.run(roundtrip=False, as_json=False, client=_client())
        self.assertEqual(report["leftovers"], [])

    # -- le code de sortie ------------------------------------------------- #

    def test_the_leftover_exit_code_is_its_own(self):
        code, _, _, alert = self._silent([])
        self.assertEqual(code, check_supabase.EXIT_LEFTOVER)
        self.assertNotEqual(
            check_supabase.EXIT_LEFTOVER,
            check_supabase.EXIT_FAILED,
            "un reste en base doit se distinguer d'un échec ordinaire",
        )
        self.assertEqual(alert.call_args.args[0], [self.LEFTOVER])

    def test_the_same_report_without_a_leftover_exits_one(self):
        """La distinction tient à la **liste des restes**, pas au `ok`."""
        self.report["leftovers"] = []
        code, _, _, alert = self._silent([])
        self.assertEqual(code, check_supabase.EXIT_FAILED)
        self.assertFalse(alert.called, "rien à alerter : le reste est déjà corrigé")

    def test_a_green_report_exits_zero(self):
        self.report = {"sections": [], "ok": True, "leftovers": []}
        code, _, _, _ = self._silent([])
        self.assertEqual(code, check_supabase.EXIT_OK)

    # -- le journal -------------------------------------------------------- #

    def test_the_journal_goes_to_stderr_and_json_stays_readable(self):
        """Un consommateur machine ne doit pas filtrer un avertissement."""
        code, out, err, _ = self._silent(["--json"])
        self.assertEqual(code, check_supabase.EXIT_LEFTOVER)
        self.assertIn(self.LEFTOVER, err)
        self.assertIn(check_supabase.JOURNAL_TAG, err)
        self.assertEqual(json.loads(out)["leftovers"], [self.LEFTOVER])

    def test_the_human_report_says_the_cleanup_is_incomplete(self):
        _, out, err, _ = self._silent([])
        self.assertIn("Nettoyage incomplet", out)
        self.assertIn("voir le journal", out)
        self.assertIn(self.LEFTOVER, err)

    def test_the_journal_names_every_row_and_where_to_act(self):
        journal = check_supabase.format_leftover_journal([self.LEFTOVER, "users.id=probe-1"])
        self.assertIn("INCOMPLET", journal)
        self.assertIn(self.LEFTOVER, journal)
        self.assertIn("users.id=probe-1", journal)
        self.assertIn("à la main", journal)

    # -- l'alerte ---------------------------------------------------------- #

    def test_the_alert_goes_to_the_admin_chat(self):
        sent: list = []

        async def notify(chat_id, text):
            sent.append((chat_id, text))

        with contextlib.redirect_stderr(io.StringIO()):
            outcome = check_supabase.alert_leftovers(
                [self.LEFTOVER], notify=notify, chat_id="42"
            )
        self.assertEqual(outcome, {"sent": True, "reason": None})
        self.assertEqual(sent[0][0], "42")
        self.assertIn(self.LEFTOVER, sent[0][1])

    def test_the_chat_defaults_to_the_configured_admin(self):
        sent: list = []

        async def notify(chat_id, text):
            sent.append(chat_id)

        with contextlib.redirect_stderr(io.StringIO()):
            check_supabase.alert_leftovers(
                [self.LEFTOVER],
                notify=notify,
                config=SimpleNamespace(telegram_admin_chat_id="99"),
            )
        self.assertEqual(sent, ["99"])

    def test_without_an_admin_chat_the_silence_is_explained(self):
        """Sinon aucune alerte n'arriverait, et rien ne l'expliquerait."""
        sent: list = []
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            outcome = check_supabase.alert_leftovers(
                [self.LEFTOVER], notify=lambda *args: sent.append(args), chat_id=""
            )
        self.assertEqual(outcome, {"sent": False, "reason": "no_admin_chat"})
        self.assertEqual(sent, [])
        self.assertIn("TELEGRAM_ADMIN_CHAT_ID", err.getvalue())

    def test_a_refused_send_is_reported_and_never_raises(self):
        """Le reste en base est déjà l'incident : l'alerte ne doit pas le cacher."""

        async def notify(chat_id, text):
            raise RuntimeError("Telegram a refusé")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            outcome = check_supabase.alert_leftovers(
                [self.LEFTOVER], notify=notify, chat_id="42"
            )
        self.assertEqual(outcome, {"sent": False, "reason": "send_failed"})
        self.assertIn("Telegram a refusé", err.getvalue())

    def test_nothing_left_means_no_alert_at_all(self):
        sent: list = []
        outcome = check_supabase.alert_leftovers(
            [], notify=lambda *args: sent.append(args), chat_id="42"
        )
        self.assertEqual(outcome, {"sent": False, "reason": "nothing_left"})
        self.assertEqual(sent, [])

    def test_the_alert_message_names_every_row(self):
        text = check_supabase.format_leftover_alert([self.LEFTOVER])
        self.assertIn(self.LEFTOVER, text)
        self.assertIn("check_supabase.py", text)


class LiveFileContractTest(unittest.TestCase):
    """La vérification live tourne partout, mais n'écrit que sur demande."""

    @classmethod
    def setUpClass(cls):
        from tests import test_supabase_live as live

        cls.live = live

    def test_the_whole_file_stays_asleep_without_the_opt_in(self):
        """Polluer `os.environ` ne doit pas réveiller la vérification live.

        C'est arrivé pour de vrai : `tests/test_api_auth_integration.py` injecte
        une fausse config Supabase au moment de son import, et la laisse derrière
        lui. Un fichier live qui se croit configuré à cause de ça écrirait dans
        une base au hasard.
        """
        live = self.live
        self.addCleanup(importlib.reload, live)
        self.addCleanup(config_runtime.reset_env_config)
        polluted = {
            "SUPABASE_URL": "https://test-project.supabase.co",
            "SUPABASE_SERVICE_KEY": "test-service-role-key-0123456789",
            "SUPABASE_LIVE": "",
            "SUPABASE_LIVE_ROUNDTRIP": "",
        }
        with mock.patch.dict(os.environ, polluted):
            # La config est mise en cache : la reconstruire depuis l'environnement
            # pollué reproduit ce que voit le fichier live importé sous `discover`.
            config_runtime.reset_env_config()
            reloaded = importlib.reload(live)
            self.assertTrue(
                reloaded.URL and reloaded.KEY,
                "environnement de test mal posé : le fichier live doit se croire configuré",
            )
            for name in ("LiveConfigurationTest", "LiveReadOnlyTest", "LiveRoundtripTest"):
                with self.subTest(name=name):
                    self.assertTrue(
                        getattr(reloaded, name).__unittest_skip__,
                        f"{name} s'est activé tout seul",
                    )

    def test_the_write_test_refuses_even_when_called_directly(self):
        """Sans opt-in, l'aller-retour ne doit pas se lancer."""
        if not self.live.blocker(client=True, write=True):
            self.skipTest("opt-in et identifiants actifs : le blocage n'a rien à bloquer")
        case = self.live.LiveRoundtripTest("test_the_roundtrip_succeeds_and_leaves_nothing")
        with self.assertRaises(unittest.SkipTest) as raised:
            case.setUp()
        self.assertIn("SUPABASE_LIVE", str(raised.exception))

    def test_a_failure_names_the_public_key_trap(self):
        """Quand la base ne répond rien, le message doit dire pourquoi."""
        with mock.patch.object(self.live, "KEY", _jwt("anon")):
            note = self.live.role_note()
        self.assertIn("publique", note)
        self.assertIn("service_role", note)

    def test_a_failure_lists_the_checks_and_not_the_key(self):
        report = {
            "sections": [
                {
                    "title": "2. Tables",
                    "checks": [
                        result_check("users", True, "lisible"),
                        result_check("insights", False, "permission denied"),
                    ],
                }
            ]
        }
        rendered = self.live.failures(report)
        self.assertIn("insights", rendered)
        self.assertIn("permission denied", rendered)
        self.assertNotIn("users", rendered)


class ClientStatusTest(unittest.TestCase):
    """« La base est vide » et « le client n'existe pas » ne se ressemblent pas."""

    def test_the_reason_is_kept(self):
        with mock.patch.object(supabase_client, "supabase", None), \
             mock.patch.object(supabase_client, "SUPABASE_ERROR", "ModuleNotFoundError: supabase"):
            status = supabase_client.client_status()
        self.assertFalse(status["ready"])
        self.assertIn("supabase", status["error"])

    def test_a_built_client_has_no_error(self):
        with mock.patch.object(supabase_client, "supabase", object()), \
             mock.patch.object(supabase_client, "SUPABASE_ERROR", None):
            self.assertEqual(
                supabase_client.client_status(), {"ready": True, "error": None}
            )

    def test_the_required_tables_cover_the_application(self):
        """La liste vérifiée doit couvrir ce que le code écrit : elle vit ici."""
        for table in (
            "users",
            "insights",
            "pending_signals",
            "knowledge_chunks",
            # Les quatre tables de l'aller-retour (migrations 005 et 006) sont
            # aussi lues par l'application : elles doivent être vérifiées au
            # niveau « la table répond », pas seulement à l'aller-retour.
            "economic_events",
            "macro_bias_logs",
            "trade_post_mortems",
            "adaptive_model_weights",
        ):
            with self.subTest(table=table):
                self.assertIn(table, supabase_client.REQUIRED_TABLES)
        self.assertNotIn("bot_settings", supabase_client.REQUIRED_TABLES)


class ClientInjectionTest(unittest.TestCase):
    """La substitution doit atteindre les modules qui ont lié le client à l'import.

    `core/adaptive_learning.py` écrit `from database.supabase_client import
    supabase` : la valeur est figée à l'import. Substituer le client dans son seul
    module d'origine laissait donc la relecture des tables d'apprentissage
    pointer sur **un autre client** que celui de l'écriture. En production les
    deux sont le même objet, donc personne ne le verrait ; en test, on
    interrogerait la vraie base au lieu de la doublure.
    """

    def test_the_injection_reaches_a_module_that_bound_the_client(self):
        client = _client()
        original = adaptive_learning.supabase
        with check_supabase.client_in_place(client):
            self.assertIs(adaptive_learning.supabase, client)
            self.assertIs(supabase_client.supabase, client)
        self.assertIs(adaptive_learning.supabase, original)
        self.assertIs(supabase_client.supabase, original)

    def test_it_only_rewrites_bindings_that_point_at_the_client(self):
        """Un attribut `supabase` sans rapport ne doit pas être touché."""
        sentinel = object()
        with mock.patch.object(adaptive_learning, "supabase", sentinel):
            with check_supabase.client_in_place(_client()):
                self.assertIs(adaptive_learning.supabase, sentinel)


class EnvFileLoadingTest(unittest.TestCase):
    """Le `.env` que l'application lit doit aussi être lu par l'outil.

    Sans ce chargement, `scripts/check_supabase.py` ne verrait que
    l'environnement du processus : renseigner `.env` ferait tourner le bot mais
    pas la vérification — et l'écart est le pire des cas, puisque tout paraîtrait
    configuré d'un côté et vide de l'autre.
    """

    def setUp(self) -> None:
        if importlib.util.find_spec("dotenv") is None:
            self.skipTest("python-dotenv absent : le `.env` n'est pas lu du tout")
        self.temp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.temp, ignore_errors=True)
        self.env_file = self.temp / ".env"
        self.env_file.write_text(
            'SUPABASE_URL="https://probe.supabase.co"\n', encoding="utf-8"
        )

    def test_it_reads_the_file_the_application_reads(self):
        with mock.patch.object(check_supabase, "PROJECT_ENV", self.env_file), \
             mock.patch.dict(os.environ):
            # `tests/test_api_auth_integration.py` exporte une **fausse** config
            # Supabase à son import, sans la restaurer (le module fige ses
            # constantes). On part donc d'un environnement où la variable est
            # absente : c'est le cas visé — rien d'exporté, tout dans `.env`.
            os.environ.pop("SUPABASE_URL", None)
            self.assertTrue(check_supabase.load_project_env())
            self.assertEqual(os.environ.get("SUPABASE_URL"), "https://probe.supabase.co")

    def test_a_variable_already_exported_wins(self):
        """Visiter une autre base doit rester possible sans toucher au fichier."""
        with mock.patch.object(check_supabase, "PROJECT_ENV", self.env_file), \
             mock.patch.dict(os.environ, {"SUPABASE_URL": "https://autre.supabase.co"}):
            check_supabase.load_project_env()
            self.assertEqual(os.environ.get("SUPABASE_URL"), "https://autre.supabase.co")

    def test_a_missing_file_is_not_an_error(self):
        absent = self.temp / "inexistant.env"
        with mock.patch.object(check_supabase, "PROJECT_ENV", absent), \
             mock.patch.dict(os.environ):
            self.assertFalse(check_supabase.load_project_env())

    def test_main_loads_it_before_running_the_checks(self):
        """Le point de câblage : charger le fichier **avant** de lire la config."""
        seen: dict = {}

        def spy_run(**kwargs):
            seen.update(os.environ)
            return {"sections": [], "ok": True}

        with mock.patch.object(check_supabase, "PROJECT_ENV", self.env_file), \
             mock.patch.dict(os.environ), \
             mock.patch.object(check_supabase, "run", side_effect=spy_run):
            os.environ.pop("SUPABASE_URL", None)  # voir le commentaire ci-dessus
            self.assertEqual(check_supabase.main(["--json"]), 0)
        self.assertEqual(seen.get("SUPABASE_URL"), "https://probe.supabase.co")


if __name__ == "__main__":
    unittest.main()
