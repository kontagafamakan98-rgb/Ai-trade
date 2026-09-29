"""Test d'intégration FastAPI : authentification des endpoints internes.

Il est volontairement **niveau intégration** : on importe la vraie application
(`api.webhook.app`, qui inclut les routeurs `/consensus`, `/learning`, `/macro`,
`/media`, `/admin` et porte les routes `/reports/*`), on injecte une **fausse
config de test** (clé interne factice), puis on vérifie pour chaque endpoint :

* **sans en-tête d'authentification → 401** (refus) ;
* **avec `X-API-Key` valide → 200** (accès autorisé).

Les dépendances externes (Supabase, LLM, exécution d'ordres) sont *stubbées*
sur les modules de routeurs pour rendre les réponses déterministes : le test
prouve le comportement d'authentification, pas l'exécution métier.

Si FastAPI n'est pas installé (environnement de dev minimal), les tests sont
**ignorés** proprement au lieu d'échouer.
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from tests import supabase_double

REPO_ROOT = Path(__file__).resolve().parents[1]

# --------------------------------------------------------------------------- #
# Fausse config de test : injectée AVANT tout import de l'application, car
# `config.py` et `notifications/notify.py` figent leurs valeurs à l'import.
# --------------------------------------------------------------------------- #
FAKE_INTERNAL_KEY = "test-internal-key-9f3a2c7b"
FAKE_WEBHOOK_SECRET = "test-webhook-secret-5b8e2d1c"
FAKE_TELEGRAM_TOKEN = "123456789:AAF7c3d2e1b0a9f8e7d6c5b4a3f2e1d0"

FAKE_ENV = {
    "INTERNAL_API_KEY": FAKE_INTERNAL_KEY,
    "WEBHOOK_SECRET": FAKE_WEBHOOK_SECRET,
    "TELEGRAM_BOT_TOKEN": FAKE_TELEGRAM_TOKEN,
    "SUPABASE_URL": "https://test-project.supabase.co",
    "SUPABASE_SERVICE_KEY": "test-service-role-key-0123456789",
    "ENCRYPTION_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
    "PAPER_TRADING": "true",
    "HEADLESS": "true",
}

# Import du client de test : retombe sur un skip si FastAPI/httpx absents.
try:
    from fastapi.testclient import TestClient  # noqa: F401

    _FASTAPI_AVAILABLE = True
    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover - dépend de l'environnement
    _FASTAPI_AVAILABLE = False
    _IMPORT_ERROR = exc

_APP = None
_IMPORT_ERROR = _IMPORT_ERROR


def _inject_fake_config() -> None:
    os.environ.update(FAKE_ENV)
    from core import config_runtime

    config_runtime.reset_env_config()
    # `config.py` fige ses constantes à l'import (dont `WEBHOOK_SECRET`, lié par
    # `api/webhook`). Le singleton de `config_runtime` suffit pour les lectures
    # dynamiques, mais PAS pour les modules qui ont déjà fait `from config import
    # X`. On recharge donc `config` après l'injection : le test reste valable
    # quel que soit l'ordre d'import des modules de test (avant, il ne passait
    # que parce que ce fichier était importé en premier).
    import config

    importlib.reload(config)


def _load_app():
    """Charge `api.webhook.app` avec la fausse config, une seule fois."""
    global _APP, _IMPORT_ERROR
    if _APP is not None:
        return _APP
    _inject_fake_config()
    try:
        from api.webhook import app

        _APP = app
    except Exception as exc:  # pragma: no cover - dépend de l'environnement
        _IMPORT_ERROR = exc
    return _APP


if _FASTAPI_AVAILABLE:
    _load_app()


def _async_stub(*_args, **_kwargs):
    async def _coro():
        return {"ok": True, "stub": True}

    return _coro()


#: Remplacements des dépendances métier (module, nom, valeur de remplacement).
STUBS = (
    ("api.consensus_router", "generate_consensus_report", lambda *_a, **_k: {"ok": True, "stub": True}),
    ("api.consensus_router", "inject_consensus_signal_into_pipeline", _async_stub),
    ("api.learning_router", "get_learning_summary", lambda *_a, **_k: {"ok": True, "stub": True}),
    ("api.learning_router", "get_adaptive_parameters", lambda *_a, **_k: {"ok": True, "stub": True}),
    ("api.learning_router", "record_trade_settlement_and_learn", lambda *_a, **_k: {"ok": True}),
    ("api.learning_router", "_refresh_knowledge_base_lessons", lambda *_a, **_k: None),
    ("api.macro_router", "get_economic_events", lambda *_a, **_k: []),
    ("api.macro_router", "get_currencies_for_symbol", lambda *_a, **_k: ["EUR", "USD"]),
    ("api.macro_router", "calculate_symbol_macro_bias", lambda *_a, **_k: (0.1, "Neutral", ["stub"])),
    (
        "api.macro_router",
        "check_news_risk",
        lambda *_a, **_k: {
            "risk_level": "Low",
            "allowed": True,
            "reason": "stub",
            "penalty": 0.0,
            "active_event": None,
        },
    ),
    ("api.macro_router", "fetch_and_push_forex_factory_events", _async_stub),
    ("api.reports_router", "build_performance_summary", lambda *_a, **_k: {"ok": True, "stub": True}),
    ("api.reports_router", "export_run_card", lambda *_a, **_k: {"ok": True, "stub": True}),
    # Le texte d'un média lit la base : sans doublure, l'endpoint répondrait 500
    # (Supabase n'est pas configuré ici) et le test d'authentification mesurerait
    # cette panne au lieu de mesurer la clé.
    ("api.media_router", "get_media", lambda *_a, **_k: {"id": "media-1", "metadata": {}}),
    (
        "api.media_router",
        "list_media_chunks",
        lambda *_a, **_k: [{"chunk_index": 0, "content": "texte", "token_count": 2}],
    ),
    # La **liste** des médias lit la table puis compte les morceaux : même raison,
    # et sans ligne aucun morceau n'est demandé (`chunk_counts` ne lit rien quand
    # la page est vide).
    ("api.media_router", "list_media", lambda *_a, **_k: []),
    # La réconciliation parcourt le bucket entier (deux listings complets) : sans
    # doublure l'endpoint répondrait 502 (Storage non configuré) et le test
    # d'authentification mesurerait cette panne au lieu de mesurer la clé.
    ("api.admin_router", "list_orphan_objects", lambda *_a, **_k: []),
    ("api.admin_router", "list_missing_objects", lambda *_a, **_k: []),
    (
        "api.admin_router",
        "get_media",
        lambda media_id, *_a, **_k: (
            {"id": media_id, "storage_path": f"telegram/chan/{media_id}.jpg"}
            if media_id == "media-1"
            else None
        ),
    ),
    # Objet déclaré présent : la réparation n'a alors rien à télécharger ni à
    # déposer, donc la clé est mesurée sans qu'aucun réseau ne soit touché.
    ("database.media_store", "object_exists", lambda *_a, **_k: True),
    # La sonde Supabase **écrit** : sans doublure, mesurer la clé reviendrait à
    # écrire sept lignes de sonde dans la base pointée par l'environnement du test.
    ("scripts.check_supabase", "run", lambda *_a, **_k: {"sections": [], "ok": True}),
)

#: (méthode, chemin, corps JSON optionnel)
ENDPOINTS = (
    ("GET", "/consensus/report/BTC", None),
    ("POST", "/consensus/inject", {"user_id": "u-test", "asset": "BTC"}),
    ("GET", "/learning/summary", None),
    ("GET", "/learning/parameters/BTC", None),
    ("POST", "/learning/feedback", {"signal_id": "s-1", "asset": "BTC", "outcome": "won"}),
    ("POST", "/learning/recalibrate", None),
    ("GET", "/macro/calendar", None),
    ("GET", "/macro/bias/EURUSD", None),
    ("GET", "/macro/news-risk/EURUSD", None),
    ("POST", "/macro/refresh", None),
    ("GET", "/reports/summary", None),
    ("GET", "/reports/summary/u-test", None),
    ("POST", "/reports/export", None),
    ("GET", "/preflight", None),
    ("GET", "/secrets/fingerprints", None),
    ("GET", "/media", None),
    ("GET", "/media/media-1/text", None),
    ("GET", "/admin/media/orphans", None),
    ("GET", "/admin/media/missing", None),
    ("POST", "/admin/media/missing/repair", {"media_ids": ["media-1"]}),
    ("GET", "/admin/supabase/check", None),
    ("POST", "/admin/supabase/roundtrip", {"tables": ["core"]}),
)

ROUTE_PREFIXES = (
    "/consensus",
    "/learning",
    "/macro",
    "/reports",
    "/secrets",
    "/media",
    "/admin",
)


@unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI/httpx non installés")
class ApiAuthIntegrationTest(unittest.TestCase):
    """401 sans clé, 200 avec clé, pour chaque endpoint interne."""

    def setUp(self):
        # Réinjecte la config à chaque test : d'autres tests peuvent avoir
        # réinitialisé le singleton de configuration.
        _inject_fake_config()
        self.app = _load_app()
        if self.app is None:
            self.skipTest(f"import de api.webhook impossible : {_IMPORT_ERROR!r}")
        self.client = TestClient(self.app)
        self._stack = ExitStack()
        for module_name, attr, replacement in STUBS:
            module = importlib.import_module(module_name)
            self._stack.enter_context(mock.patch.object(module, attr, replacement))
        self._stack.__enter__()

    def tearDown(self):
        self._stack.close()

    # -- helpers ---------------------------------------------------------- #

    def _request(self, method, path, body=None, headers=None):
        kwargs = {"headers": headers or {}}
        if body is not None:
            kwargs["json"] = body
        return self.client.request(method, path, **kwargs)

    # -- structure -------------------------------------------------------- #

    def test_all_internal_routes_are_registered(self):
        # Les schéma OpenAPI liste tous les gabarits de chemin, y compris ceux
        # des routeurs inclus (indépendant de la version de Starlette).
        paths = set(self.app.openapi().get("paths", {}).keys())
        for prefix in ROUTE_PREFIXES:
            self.assertTrue(
                any(p.startswith(prefix) for p in paths),
                f"aucune route enregistrée pour le préfixe {prefix}",
            )

    # -- 401 sans clé ----------------------------------------------------- #

    def test_every_endpoint_rejects_without_key(self):
        for method, path, body in ENDPOINTS:
            with self.subTest(endpoint=f"{method} {path}"):
                response = self._request(method, path, body)
                self.assertEqual(
                    response.status_code,
                    401,
                    f"{method} {path} devrait refuser sans clé (reçu {response.status_code})",
                )

    def test_wrong_key_is_rejected(self):
        response = self._request(
            "GET", "/learning/summary", headers={"X-API-Key": "wrong-key"}
        )
        self.assertEqual(response.status_code, 401)

    # -- 200 avec clé ----------------------------------------------------- #

    def test_every_endpoint_accepts_with_valid_key(self):
        headers = {"X-API-Key": FAKE_INTERNAL_KEY}
        for method, path, body in ENDPOINTS:
            with self.subTest(endpoint=f"{method} {path}"):
                response = self._request(method, path, body, headers=headers)
                self.assertEqual(
                    response.status_code,
                    200,
                    f"{method} {path} devrait accepter la clé (reçu "
                    f"{response.status_code} : {response.text[:200]})",
                )

    def test_the_fingerprint_endpoint_publishes_no_secret_at_all(self):
        """Le seul contenu publiable : des noms, des empreintes salées, des versions.

        C'est ce qui rend la vérification de la production possible sans faire
        circuler un secret : la barrière compare des empreintes. Le test lit donc
        la réponse **en cherchant les valeurs** : si l'une d'elles y apparaît, la
        fonctionnalité est devenue une fuite.
        """
        from core.secrets_audit import (
            DEPLOYED_REPORT_KIND,
            DEPLOYED_REPORT_VERSION,
            FINGERPRINT_SALT,
            secret_fingerprint,
        )

        response = self._request(
            "GET", "/secrets/fingerprints", headers={"X-API-Key": FAKE_INTERNAL_KEY}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["kind"], DEPLOYED_REPORT_KIND)
        self.assertEqual(body["version"], DEPLOYED_REPORT_VERSION)
        self.assertEqual(body["fingerprint_salt"], FINGERPRINT_SALT)
        # Les empreintes sont celles des valeurs réellement chargées par le
        # processus : deux services qui les calculent pareil peuvent comparer
        # leur configuration sans échanger une valeur.
        self.assertEqual(
            body["fingerprints"]["INTERNAL_API_KEY"],
            secret_fingerprint("INTERNAL_API_KEY", FAKE_INTERNAL_KEY),
        )
        self.assertEqual(
            body["fingerprints"]["WEBHOOK_SECRET"],
            secret_fingerprint("WEBHOOK_SECRET", FAKE_WEBHOOK_SECRET),
        )
        self.assertEqual(body["key_ring"]["primary_version"], 1)
        for value in (FAKE_INTERNAL_KEY, FAKE_WEBHOOK_SECRET, FAKE_TELEGRAM_TOKEN):
            with self.subTest(value=value[:8]):
                self.assertNotIn(value, response.text)

    def test_authorization_bearer_is_accepted(self):
        response = self._request(
            "GET",
            "/macro/calendar",
            headers={"Authorization": f"Bearer {FAKE_INTERNAL_KEY}"},
        )
        self.assertEqual(response.status_code, 200)

    # -- bornes de la requête --------------------------------------------- #

    def test_the_orphan_limit_bounds_are_enforced_by_the_framework(self):
        """Un `limit` hors bornes est refusé (422) — pas silencieusement corrigé.

        Borner côté routeur seulement laisserait `limit=0` renvoyer une réponse
        vide, c'est-à-dire la même chose qu'un bucket cohérent.
        """
        headers = {"X-API-Key": FAKE_INTERNAL_KEY}
        for limit in (0, -1, 1001):
            with self.subTest(limit=limit):
                response = self._request(
                    "GET", f"/admin/media/orphans?limit={limit}", headers=headers
                )
                self.assertEqual(response.status_code, 422, response.text)

    def test_the_orphan_offset_bounds_are_enforced_by_the_framework(self):
        """`offset=-1` n'est pas « 0 » : un rang négatif n'existe pas."""
        response = self._request(
            "GET", "/admin/media/orphans?offset=-1", headers={"X-API-Key": FAKE_INTERNAL_KEY}
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_the_orphan_endpoint_accepts_a_bounded_limit(self):
        response = self._request(
            "GET", "/admin/media/orphans?limit=5&prefix=/telegram/chan/",
            headers={"X-API-Key": FAKE_INTERNAL_KEY},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["prefix"], "telegram/chan")

    def test_the_orphan_endpoint_can_be_asked_past_the_first_slice(self):
        """Le rang demandé est celui qui est rendu — sans relever `limit`."""
        response = self._request(
            "GET", "/admin/media/orphans?limit=5&offset=10",
            headers={"X-API-Key": FAKE_INTERNAL_KEY},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["offset"], 10)
        self.assertEqual(body["limit"], 5)

    def test_the_media_page_bounds_are_enforced_by_the_framework(self):
        """`limit=0` rendrait une page vide — soit la même réponse qu'un stock vide."""
        headers = {"X-API-Key": FAKE_INTERNAL_KEY}
        for query in ("limit=0", "limit=101", "offset=-1"):
            with self.subTest(query=query):
                response = self._request("GET", f"/media?{query}", headers=headers)
                self.assertEqual(response.status_code, 422, response.text)

    def test_the_media_endpoint_accepts_a_bounded_page(self):
        from api import media_router

        response = self._request(
            "GET",
            f"/media?limit={media_router.MAX_PAGE}&offset=10&source=telegram&media_type=photo",
            headers={"X-API-Key": FAKE_INTERNAL_KEY},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["offset"], 10)
        self.assertEqual(body["limit"], media_router.MAX_PAGE)

    def test_the_repair_body_is_bounded(self):
        """Une réparation se demande sur une liste **nommée** et bornée.

        Une liste vide (ou un « tout » implicite) déclencherait des
        téléchargements non voulus ; au-delà de la borne, il faut découper — un
        lot trop long se termine en timeout au milieu, sans dire lesquels sont
        passés.
        """
        from api import admin_router

        headers = {"X-API-Key": FAKE_INTERNAL_KEY}
        for body in (
            {"media_ids": []},
            {"media_ids": [str(index) for index in range(admin_router.MAX_REPAIR_IDS + 1)]},
            {},
        ):
            with self.subTest(body=str(body)[:60]):
                response = self._request(
                    "POST", "/admin/media/missing/repair", body, headers=headers
                )
                self.assertEqual(response.status_code, 422, response.text)

    def test_the_repair_reports_an_unknown_media_without_failing(self):
        response = self._request(
            "POST",
            "/admin/media/missing/repair",
            {"media_ids": ["inconnu-1", "media-1"]},
            headers={"X-API-Key": FAKE_INTERNAL_KEY},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["unknown"], ["inconnu-1"])
        self.assertEqual(body["requested"], 1)

    def test_the_roundtrip_body_is_bounded_and_named(self):
        """Un `POST` qui **écrit** ne déduit pas son périmètre.

        Une liste vide (ou un « tout » implicite) écrirait sept lignes de sonde
        non voulues : le périmètre se nomme, et au-delà de la borne on découpe.
        """
        from api import admin_router

        headers = {"X-API-Key": FAKE_INTERNAL_KEY}
        for body in (
            {"tables": []},
            {},
            {"tables": ["core"] * (admin_router.MAX_PROBE_TABLES + 1)},
        ):
            with self.subTest(body=str(body)[:40]):
                response = self._request(
                    "POST", "/admin/supabase/roundtrip", body, headers=headers
                )
                self.assertEqual(response.status_code, 422, response.text)

    def test_an_unknown_table_name_is_refused_not_widened(self):
        """Le refus est **franc** : avec l'aller-retour, un repli écrirait ailleurs."""
        headers = {"X-API-Key": FAKE_INTERNAL_KEY}
        post = self._request(
            "POST",
            "/admin/supabase/roundtrip",
            {"tables": ["economic_event"]},
            headers=headers,
        )
        self.assertEqual(post.status_code, 400, post.text)
        self.assertIn("economic_event", post.json()["detail"])
        self.assertIn("economic_events", post.json()["detail"])

        read = self._request("GET", "/admin/supabase/check?only=nawak", headers=headers)
        self.assertEqual(read.status_code, 400, read.text)
        self.assertIn("core", read.json()["detail"])

    def test_the_roundtrip_says_which_tables_it_writes(self):
        response = self._request(
            "POST",
            "/admin/supabase/roundtrip",
            {"tables": ["core", "engine"]},
            headers={"X-API-Key": FAKE_INTERNAL_KEY},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(len(body["writes"]), 7, body["writes"])
        self.assertIn("--roundtrip", body["command"])

    def test_the_read_only_check_announces_no_write(self):
        response = self._request(
            "GET", "/admin/supabase/check?only=core", headers={"X-API-Key": FAKE_INTERNAL_KEY}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["writes"])

    # -- découplage Telegram ---------------------------------------------- #

    def test_protected_surface_imports_without_telegram(self):
        """La surface protégée ne doit PAS dépendre de `python-telegram-bot`.

        On bloque l'import de `telegram` dans un sous-processus puis on importe
        uniquement les routeurs protégés : si l'un d'eux tirait encore
        `notifications.notify`, l'import échouerait.
        """
        code = (
            "import sys; sys.modules['telegram'] = None\n"
            "import api.reports_router\n"
            "import api.consensus_router, api.learning_router, api.macro_router\n"
            "print('ok')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(
            proc.returncode, 0, (proc.stderr or proc.stdout)[-2000:]
        )

    def test_webhook_module_loads_without_telegram_and_with_bad_token(self):
        """`api/webhook` doit être importable sans Telegram et avec un token invalide.

        C'est la propriété qui garantit qu'une panne ou une mauvaise configuration
        du bot ne peut plus empêcher le serveur web de démarrer (`run.py` importe
        `api.webhook` au niveau module). On bloque `telegram` **et** on force un
        token syntaxiquement invalide : l'import doit tout de même réussir, et
        `notifications.notify` doit rester importable sans valider ce token.
        """
        code = (
            "import sys; sys.modules['telegram'] = None\n"
            "import api.webhook\n"
            "import notifications.notify as notify\n"
            "# Le Bot ne doit pas être construit à l'import (sinon le token serait validé)\n"
            "assert notify._bot is None, 'bot should stay lazy'\n"
            "print('ok')\n"
        )
        env = dict(os.environ)
        env["TELEGRAM_BOT_TOKEN"] = "pas-un-token-valide"
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
        )
        self.assertEqual(
            proc.returncode, 0, (proc.stderr or proc.stdout)[-2000:]
        )
        self.assertIn("ok", proc.stdout)

    def test_webhook_answers_200_when_telegram_token_is_unusable(self):
        """Un token inutilisable laisse le webhook debout (200, 0 notification).

        On simule la construction impossible du `Bot` (token invalide) : la
        boucle de notification doit absorber l'erreur utilisateur par
        utilisateur, sans transformer l'endpoint en 500.
        """
        import notifications.notify as notify

        from api import webhook as webhook_module
        from core import alert_engine as alert_engine_module

        # Un utilisateur paper actif, avec les colonnes que le webhook demande
        # (`select("id, telegram_chat_id").eq("paper_mode", True)`). La doublure
        # partagée filtre pour de bon : semer la ligne sans `paper_mode` la
        # rendrait invisible et le test ne notifierait plus personne.
        client = supabase_double.SupabaseDouble()
        client.store("users").rows = [
            {"id": "u-1", "telegram_chat_id": 42, "paper_mode": True}
        ]

        def _unusable_bot():
            raise RuntimeError("TELEGRAM_BOT_TOKEN inutilisable : InvalidToken")

        payload = {
            "secret": FAKE_WEBHOOK_SECRET,
            "ticker": "BTCUSD",
            "action": "buy",
            "price": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
        }
        with mock.patch.object(webhook_module, "supabase", client), mock.patch.object(
            webhook_module, "recently_sent", lambda *a, **k: False
        ), mock.patch.object(
            webhook_module, "create_pending_signal", lambda *a, **k: "sig-1"
        ), mock.patch.object(
            alert_engine_module, "get_recent_insights", lambda *a, **k: []
        ), mock.patch.object(
            notify, "get_bot", _unusable_bot
        ):
            response = self.client.post("/alert", json=payload)

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("notifications_sent"), 0)

    # -- fail-closed ------------------------------------------------------ #

    def test_missing_configured_key_fails_closed_with_503(self):
        """Sans clé configurée côté serveur, l'endpoint doit être désactivé."""
        from core import config_runtime

        saved = os.environ.pop("INTERNAL_API_KEY", None)
        config_runtime.reset_env_config()
        try:
            response = self._request("GET", "/learning/summary")
            self.assertEqual(response.status_code, 503)
        finally:
            if saved is not None:
                os.environ["INTERNAL_API_KEY"] = saved
            config_runtime.reset_env_config()


if __name__ == "__main__":
    unittest.main()
