"""Vérification de la **production** par la barrière pre-deploy.

Ce que ces tests protègent, et pourquoi ils existent : la barrière ne lisait que
le `.env` de la machine qui la lance. Elle pouvait donc écrire « déploiement
autorisé » pour des valeurs que la production n'avait jamais reçues — variable
jamais posée sur la plateforme, rotation faite d'un seul côté, ou service jamais
redémarré. Le feu vert portait alors sur autre chose que ce qui tourne en ligne.

Trois niveaux sont vérifiés ici :

* la **comparaison** (pure) : même empreinte = même valeur, empreinte différente
  = refus, secret absent d'un côté = refus ou avertissement, et jamais une valeur
  en clair dans un message ;
* le **transport** (opener injecté) : ce qui part sur le réseau (la clé interne
  en en-tête, jamais dans l'URL), et ce que chaque panne signifie (401 = la
  production n'a pas cette clé, 503 = elle n'en a aucune, corps illisible =
  mauvaise URL) ;
* la **chaîne complète**, sur de vraies sockets : un serveur HTTP qui publie le
  rapport de production (calculé par le vrai code) et la barrière lancée en
  sous-processus, avec un `.env` en répertoire jetable. Le cas qui compte est
  celui où les deux côtés diffèrent : la barrière doit refuser, et nommer la
  différence sans jamais afficher les valeurs.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from core.secrets_audit import (
    DEFAULT_SPECS,
    DEPLOYED_FINGERPRINTS_PATH,
    DEPLOYED_REPORT_KIND,
    DEPLOYED_REPORT_VERSION,
    DEPLOYED_URL_ENV,
    ERROR,
    FINGERPRINT_SALT,
    WARNING,
    AuditResult,
    Issue,
    DeployedUnreachable,
    apply_deployed_check,
    compare_with_deployed,
    deployed_fingerprints_url,
    fetch_deployed_fingerprints,
    fingerprints_for_values,
    redact_url,
    remote_url_issues,
)
from core.secrets_audit import (
    secret_fingerprint as _fingerprint,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPT = REPO_ROOT / "scripts" / "verify_secrets.py"

#: Une clé Fernet valide (base64 urlsafe de 32 octets) — celle des `.env.example`.
FERNET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="

#: Valeurs jouées côté **machine locale** dans la chaîne de bout en bout. Elles
#: sont robustes et distinctes : l'audit local doit passer, sinon un refus ne
#: prouverait rien de la vérification distante.
LOCAL_ENV = {
    "WEBHOOK_SECRET": "local-webhook-secret-0123456789",
    "INTERNAL_API_KEY": "local-internal-key-0123456789",
    "ENCRYPTION_KEY": FERNET,
    "SUPABASE_SERVICE_KEY": "local-service-role-key-0123456789",
    "TELEGRAM_BOT_TOKEN": "123456789:AAF7c3d2e1b0a9f8e7d6c5b4a3f2e1d0",
}

#: Valeurs jouées côté **production** : les mêmes, sauf celles qu'un cas de test
#: fait diverger volontairement.
PRODUCTION_ENV = dict(LOCAL_ENV)


def _payload(values, extra=None, **overrides):
    """Un rapport de production valide, construit comme le service le publie.

    `extra` simule une **révision plus récente** du code déployé : ses specs
    contiennent un secret que ce dépôt ne connaît pas encore, donc son rapport
    publie une empreinte de plus (`fingerprints_for_values` ne parcourt que les
    specs de l'émetteur — c'est ainsi qu'un décalage de révision devient visible).
    """
    fingerprints = fingerprints_for_values(values)
    for name, value in (extra or {}).items():
        fingerprints[name] = _fingerprint(name, value)
    payload = {
        "kind": DEPLOYED_REPORT_KIND,
        "version": DEPLOYED_REPORT_VERSION,
        "fingerprint_salt": FINGERPRINT_SALT,
        "fingerprints": fingerprints,
    }
    payload.update(overrides)
    return payload


def _opener(body=b"{}", *, error=None, calls=None):
    """Un `opener` injectable : aucun réseau, et on garde trace de la requête."""

    def open_url(request, timeout=None):
        if calls is not None:
            calls.append((request, timeout))
        if error is not None:
            raise error
        return io.BytesIO(body)

    return open_url


def _http_error(code):
    return urllib.error.HTTPError("http://svc/secrets/fingerprints", code, "boom", None, None)


class DeployedUrlTest(unittest.TestCase):
    """L'URL accepte les deux formes, et une URL douteuse est dite telle."""

    def test_a_base_url_gets_the_endpoint_path(self):
        self.assertEqual(
            deployed_fingerprints_url("https://svc.onrender.com"),
            f"https://svc.onrender.com{DEPLOYED_FINGERPRINTS_PATH}",
        )

    def test_a_trailing_slash_does_not_double_the_separator(self):
        self.assertEqual(
            deployed_fingerprints_url("https://svc.onrender.com/"),
            f"https://svc.onrender.com{DEPLOYED_FINGERPRINTS_PATH}",
        )

    def test_a_complete_endpoint_url_is_not_suffixed_twice(self):
        complete = f"https://svc.onrender.com{DEPLOYED_FINGERPRINTS_PATH}"
        self.assertEqual(deployed_fingerprints_url(complete), complete)

    def test_an_empty_url_is_refused_not_guessed(self):
        for empty in ("", "   ", None):
            with self.subTest(url=repr(empty)):
                with self.assertRaises(DeployedUnreachable):
                    deployed_fingerprints_url(empty)

    def test_a_public_cleartext_url_is_signalled_but_allowed(self):
        """La clé interne y voyage en clair : on le dit, on ne bloque pas pour autant."""
        issues = remote_url_issues("http://svc.example.com")
        self.assertEqual([issue.severity for issue in issues], [WARNING])
        self.assertIn("clair", issues[0].message)

    def test_the_loopback_in_cleartext_is_not_signalled(self):
        for url in ("http://127.0.0.1:8000", "http://localhost:8000", "https://svc.example.com"):
            with self.subTest(url=url):
                self.assertEqual(remote_url_issues(url), [])

    def test_a_url_that_cannot_be_a_service_is_an_error(self):
        for url in ("ftp://svc.example.com", "svc.onrender.com", "https://", "http://[::1"):
            with self.subTest(url=url):
                issues = remote_url_issues(url)
                self.assertEqual([issue.severity for issue in issues], [ERROR], url)

    def test_credentials_in_the_url_are_refused_and_never_repeated(self):
        """Le rapport publie l'URL : des identifiants y seraient recopiés dans les journaux."""
        issues = remote_url_issues("https://user:motdepasse@svc.example.com")
        self.assertEqual([issue.severity for issue in issues], [ERROR])
        self.assertNotIn("motdepasse", issues[0].message)
        self.assertIn("svc.example.com", issues[0].message)

    def test_a_redacted_url_keeps_the_host_and_drops_the_credentials(self):
        self.assertEqual(
            redact_url("https://user:motdepasse@svc.example.com/x?y=1"),
            "https://***@svc.example.com/x?y=1",
        )
        # Sans identifiants, l'URL est rendue telle quelle (aucune surprise).
        self.assertEqual(
            redact_url("https://svc.example.com/secrets/fingerprints"),
            "https://svc.example.com/secrets/fingerprints",
        )


class CompareWithDeployedTest(unittest.TestCase):
    """Même empreinte = même valeur ; toute autre combinaison est dite explicitement."""

    def _compare(self, local=None, deployed=None, extra=None, **overrides):
        return compare_with_deployed(
            LOCAL_ENV if local is None else local,
            _payload(PRODUCTION_ENV if deployed is None else deployed, extra=extra, **overrides),
        )

    def test_identical_values_are_matched_without_any_issue(self):
        comparison = self._compare()
        self.assertTrue(comparison.ok)
        self.assertEqual(comparison.issues, [])
        self.assertEqual(sorted(comparison.matched), sorted(LOCAL_ENV))
        self.assertEqual(comparison.mismatched, [])

    def test_a_different_value_is_an_error_naming_the_secret(self):
        production = dict(PRODUCTION_ENV, WEBHOOK_SECRET="autre-secret-de-production-0123")
        comparison = self._compare(deployed=production)
        self.assertFalse(comparison.ok)
        self.assertEqual(comparison.mismatched, ["WEBHOOK_SECRET"])
        issue = next(i for i in comparison.issues if i.name == "WEBHOOK_SECRET")
        self.assertEqual(issue.severity, ERROR)
        self.assertIn("DIFFÉRENTE", issue.message)
        # Le message porte les deux empreintes (comparables, non réversibles)…
        self.assertIn(_fingerprint("WEBHOOK_SECRET", LOCAL_ENV["WEBHOOK_SECRET"]), issue.message)
        self.assertIn(
            _fingerprint("WEBHOOK_SECRET", "autre-secret-de-production-0123"), issue.message
        )
        # …et surtout AUCUNE des deux valeurs en clair.
        for value in (LOCAL_ENV["WEBHOOK_SECRET"], "autre-secret-de-production-0123"):
            self.assertNotIn(value, issue.message)

    def test_a_required_secret_absent_from_production_is_an_error(self):
        production = {k: v for k, v in PRODUCTION_ENV.items() if k != "TELEGRAM_BOT_TOKEN"}
        comparison = self._compare(deployed=production)
        self.assertFalse(comparison.ok)
        self.assertEqual(comparison.missing_in_production, ["TELEGRAM_BOT_TOKEN"])
        issue = next(i for i in comparison.issues if i.name == "TELEGRAM_BOT_TOKEN")
        self.assertEqual(issue.severity, ERROR)
        self.assertIn("ABSENT", issue.message)

    def test_an_optional_secret_absent_from_production_is_only_a_warning(self):
        local = dict(LOCAL_ENV, GEMINI_API_KEY="local-gemini-key-0123456789")
        comparison = self._compare(local=local)
        self.assertTrue(comparison.ok)
        self.assertEqual(comparison.missing_in_production, ["GEMINI_API_KEY"])
        self.assertEqual([i.severity for i in comparison.issues], [WARNING])

    def test_a_required_secret_only_present_in_production_is_an_error(self):
        """Rien ici ne peut juger une valeur qu'on ne détient pas : on le dit."""
        local = {k: v for k, v in LOCAL_ENV.items() if k != "SUPABASE_SERVICE_KEY"}
        comparison = self._compare(local=local)
        self.assertFalse(comparison.ok)
        issue = next(i for i in comparison.issues if i.name == "SUPABASE_SERVICE_KEY")
        self.assertEqual(issue.severity, ERROR)
        self.assertIn("cette machine non", issue.message)

    def test_an_optional_secret_only_present_in_production_is_a_warning(self):
        production = dict(PRODUCTION_ENV, GROQ_API_KEY="production-groq-key-0123456789")
        comparison = self._compare(deployed=production)
        self.assertTrue(comparison.ok)
        self.assertEqual(comparison.unknown_here, [])
        issue = next(i for i in comparison.issues if i.name == "GROQ_API_KEY")
        self.assertEqual(issue.severity, WARNING)

    def test_a_report_from_another_service_is_refused(self):
        comparison = self._compare(kind="autre-service")
        self.assertFalse(comparison.ok)
        self.assertIn("mauvaise URL", comparison.issues[0].message)
        self.assertEqual(comparison.matched, [])
        self.assertEqual(len(comparison.issues), 1)

    def test_another_fingerprint_salt_makes_the_comparison_impossible(self):
        """Un autre sel, c'est un autre code : comparer les empreintes ne voudrait rien dire."""
        comparison = self._compare(fingerprint_salt="ai-trade-secret-ledger:v9")
        self.assertFalse(comparison.ok)
        self.assertIn("sel", comparison.issues[0].message)
        self.assertEqual(comparison.matched, [])

    def test_another_schema_version_is_refused(self):
        comparison = self._compare(version=DEPLOYED_REPORT_VERSION + 1)
        self.assertFalse(comparison.ok)
        self.assertIn("schéma", comparison.issues[0].message)
        self.assertEqual(comparison.matched, [])

    def test_a_report_without_fingerprints_is_refused(self):
        payload = _payload(PRODUCTION_ENV)
        payload.pop("fingerprints")
        comparison = compare_with_deployed(LOCAL_ENV, payload)
        self.assertFalse(comparison.ok)
        self.assertIn("fingerprints", comparison.issues[0].message)

    def test_a_secret_unknown_here_is_signalled_not_ignored(self):
        """Le code déployé est plus récent que ce dépôt : ça se voit dans le rapport."""
        comparison = self._compare(extra={"NOUVEAU_SECRET": "une-valeur-qu-on-ne-connait-pas"})
        self.assertTrue(comparison.ok)
        self.assertEqual(comparison.unknown_here, ["NOUVEAU_SECRET"])
        self.assertIn("plus récent", comparison.issues[0].message)

    def test_nothing_on_either_side_is_not_an_issue(self):
        comparison = compare_with_deployed({}, _payload({}))
        self.assertTrue(comparison.ok)
        self.assertEqual(comparison.issues, [])
        self.assertEqual(comparison.matched, [])

    def test_every_compared_secret_comes_from_the_specs(self):
        """Un nom absent des specs ne peut pas être comparé : il est signalé, jamais jugé."""
        comparison = self._compare(extra={"AUTRE": "valeur-inconnue-de-l-audit"})
        self.assertEqual(comparison.unknown_here, ["AUTRE"])
        self.assertIn("AUTRE", [issue.name for issue in comparison.issues])
        # Il n'est ni « apparié » ni « manquant » : il n'appartient pas à cet audit.
        self.assertNotIn("AUTRE", comparison.matched)
        self.assertNotIn("AUTRE", comparison.missing_in_production)


class FetchDeployedFingerprintsTest(unittest.TestCase):
    """Le transport : ce qui part, ce qui revient, et ce que chaque panne signifie."""

    def test_the_request_carries_the_key_in_a_header_and_never_in_the_url(self):
        calls = []
        payload = _payload(PRODUCTION_ENV)
        body = json.dumps(payload).encode("utf-8")
        result = fetch_deployed_fingerprints(
            "https://svc.onrender.com",
            "la-cle-interne",
            timeout=7,
            opener=_opener(body, calls=calls),
        )
        self.assertEqual(result, payload)
        request, timeout = calls[0]
        self.assertEqual(timeout, 7)
        self.assertEqual(request.get_header("X-api-key"), "la-cle-interne")
        self.assertEqual(request.full_url, f"https://svc.onrender.com{DEPLOYED_FINGERPRINTS_PATH}")
        self.assertNotIn("la-cle-interne", request.full_url)

    def test_a_401_says_the_production_does_not_hold_this_key(self):
        with self.assertRaises(DeployedUnreachable) as ctx:
            fetch_deployed_fingerprints("https://svc", "k", opener=_opener(error=_http_error(401)))
        self.assertIn("INTERNAL_API_KEY", str(ctx.exception))

    def test_a_503_says_the_production_has_no_internal_key(self):
        with self.assertRaises(DeployedUnreachable) as ctx:
            fetch_deployed_fingerprints("https://svc", "k", opener=_opener(error=_http_error(503)))
        self.assertIn("INTERNAL_API_KEY", str(ctx.exception))

    def test_any_other_status_names_the_url(self):
        with self.assertRaises(DeployedUnreachable) as ctx:
            fetch_deployed_fingerprints("https://svc", "k", opener=_opener(error=_http_error(500)))
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_an_unreachable_service_says_that_nothing_was_verified(self):
        error = urllib.error.URLError("connexion refusée")
        with self.assertRaises(DeployedUnreachable) as ctx:
            fetch_deployed_fingerprints("https://svc", "k", opener=_opener(error=error))
        self.assertIn("injoignable", str(ctx.exception))
        self.assertIn("rien n'est vérifié", str(ctx.exception))

    def test_a_timeout_is_reported_as_such(self):
        with self.assertRaises(DeployedUnreachable) as ctx:
            fetch_deployed_fingerprints(
                "https://svc", "k", timeout=2, opener=_opener(error=TimeoutError("timeout"))
            )
        self.assertIn("2", str(ctx.exception))

    def test_a_body_that_is_not_json_is_refused(self):
        body = b"<!doctype html><html>404</html>"
        with self.assertRaises(DeployedUnreachable) as ctx:
            fetch_deployed_fingerprints("https://svc", "k", opener=_opener(body))
        self.assertIn("JSON", str(ctx.exception))

    def test_a_json_payload_that_is_not_an_object_is_refused(self):
        with self.assertRaises(DeployedUnreachable) as ctx:
            fetch_deployed_fingerprints("https://svc", "k", opener=_opener(b"[1, 2, 3]"))
        self.assertIn("objet JSON", str(ctx.exception))

    def test_an_oversized_body_is_refused_without_being_read_whole(self):
        body = b"x" * 2_000_000
        with self.assertRaises(DeployedUnreachable) as ctx:
            fetch_deployed_fingerprints("https://svc", "k", opener=_opener(body))
        self.assertIn("octets", str(ctx.exception))


class ApplyDeployedCheckTest(unittest.TestCase):
    """L'absence de mesure et l'échec de la mesure ne se confondent pas avec un succès."""

    def _local_result(self, values=None):
        """Un audit local **conforme**, pour isoler l'effet de la vérification distante."""
        return AuditResult(ok=True, checked=sorted((values or LOCAL_ENV).keys()))

    def test_without_a_url_the_green_light_says_what_it_did_not_measure(self):
        result = apply_deployed_check(self._local_result(), LOCAL_ENV)
        self.assertTrue(result.ok)
        self.assertEqual(result.deployed, {"checked": False, "reason": "no_url", "required": False})
        issue = next(i for i in result.issues if i.name == "DEPLOYED")
        self.assertEqual(issue.severity, WARNING)
        self.assertIn("PAS été vérifiée", issue.message)

    def test_requiring_the_remote_turns_that_absence_into_a_refusal(self):
        result = apply_deployed_check(self._local_result(), LOCAL_ENV, require_remote=True)
        self.assertFalse(result.ok)
        issue = next(i for i in result.issues if i.name == "DEPLOYED")
        self.assertEqual(issue.severity, ERROR)
        self.assertIn("exigée", issue.message)

    def test_a_url_with_credentials_is_refused_before_any_request(self):
        calls = []
        result = apply_deployed_check(
            self._local_result(),
            LOCAL_ENV,
            "https://user:motdepasse@svc.example.com",
            api_key=LOCAL_ENV["INTERNAL_API_KEY"],
            opener=_opener(calls=calls),
        )
        self.assertFalse(result.ok)
        self.assertEqual(calls, [])
        report = json.dumps(result.as_dict(), ensure_ascii=False)
        self.assertNotIn("motdepasse", report)
        self.assertNotIn("user@", report)
        self.assertEqual(result.deployed["url"], "https://***@svc.example.com")

    def test_an_invalid_url_is_an_error_and_no_request_is_made(self):
        calls = []
        result = apply_deployed_check(
            self._local_result(), LOCAL_ENV, "pas-une-url", opener=_opener(calls=calls)
        )
        self.assertFalse(result.ok)
        self.assertEqual(calls, [])
        self.assertEqual(result.deployed["reason"], "invalid_url")

    def test_without_a_local_internal_key_nothing_can_be_verified(self):
        """Sans clé interne ici, impossible de s'authentifier : on refuse, on ne devine pas."""
        local = {k: v for k, v in LOCAL_ENV.items() if k != "INTERNAL_API_KEY"}
        calls = []
        result = apply_deployed_check(
            self._local_result(local), local, "https://svc", opener=_opener(calls=calls)
        )
        self.assertFalse(result.ok)
        self.assertEqual(calls, [])
        self.assertEqual(result.deployed["reason"], "no_api_key")

    def test_an_unreachable_production_refuses_instead_of_falling_back_to_local(self):
        result = apply_deployed_check(
            self._local_result(),
            LOCAL_ENV,
            "https://svc",
            api_key=LOCAL_ENV["INTERNAL_API_KEY"],
            opener=_opener(error=urllib.error.URLError("réseau coupé")),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.deployed["reason"], "unreachable")
        self.assertIn("error", result.deployed)

    def test_a_matching_production_is_reported_as_measured(self):
        body = json.dumps(_payload(PRODUCTION_ENV)).encode("utf-8")
        result = apply_deployed_check(
            self._local_result(),
            LOCAL_ENV,
            "https://svc",
            api_key=LOCAL_ENV["INTERNAL_API_KEY"],
            opener=_opener(body),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.issues, [])
        self.assertTrue(result.deployed["checked"])
        self.assertEqual(sorted(result.deployed["matched"]), sorted(LOCAL_ENV))
        self.assertEqual(result.deployed["url"], "https://svc")

    def test_a_different_production_refuses_the_deployment(self):
        body = json.dumps(
            _payload(dict(PRODUCTION_ENV, WEBHOOK_SECRET="production-whsec-0123456789"))
        ).encode("utf-8")
        result = apply_deployed_check(
            self._local_result(),
            LOCAL_ENV,
            "https://svc",
            api_key=LOCAL_ENV["INTERNAL_API_KEY"],
            opener=_opener(body),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.deployed["mismatched"], ["WEBHOOK_SECRET"])

    def test_the_verdict_is_recomputed_from_all_issues(self):
        """Un audit local déjà en échec le reste, même si la production correspond."""
        body = json.dumps(_payload(PRODUCTION_ENV)).encode("utf-8")
        local_result = AuditResult(
            ok=False, issues=[Issue("TELEGRAM_BOT_TOKEN", ERROR, "secret absent ou vide")]
        )
        result = apply_deployed_check(
            local_result,
            LOCAL_ENV,
            "https://svc",
            api_key=LOCAL_ENV["INTERNAL_API_KEY"],
            opener=_opener(body),
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.deployed["checked"])
        self.assertEqual([issue.name for issue in result.errors], ["TELEGRAM_BOT_TOKEN"])


class SecretConfigBridgeTest(unittest.TestCase):
    """Le pont entre l'audit et la configuration que le processus utilise vraiment."""

    def test_every_audited_secret_has_a_config_field(self):
        """Un secret audité mais non relayé disparaîtrait du rapport de production.

        La barrière conclurait alors « absent de la production » pour un secret
        qui y est — un faux verdict exactement du genre qu'on supprime.
        """
        from core.config_runtime import SECRET_CONFIG_FIELDS, EnvConfig

        self.assertEqual(
            {name for name, _field in SECRET_CONFIG_FIELDS},
            {spec.name for spec in DEFAULT_SPECS},
        )
        config = EnvConfig()
        for name, field in SECRET_CONFIG_FIELDS:
            with self.subTest(secret=name):
                self.assertTrue(hasattr(config, field), f"champ inconnu : {field}")
                self.assertIsInstance(getattr(config, field), str)

    def test_the_report_describes_what_the_process_loaded(self):
        """Le rapport est calculé depuis la configuration effective, et jamais depuis un fichier."""
        from core import config_runtime

        with mock.patch.dict(os.environ, LOCAL_ENV, clear=False):
            config_runtime.reset_env_config()
            try:
                values = config_runtime.configured_secret_values()
                report = config_runtime.deployed_secret_report()
            finally:
                config_runtime.reset_env_config()

        self.assertEqual(values["WEBHOOK_SECRET"], LOCAL_ENV["WEBHOOK_SECRET"])
        self.assertEqual(report["kind"], DEPLOYED_REPORT_KIND)
        self.assertEqual(report["fingerprint_salt"], FINGERPRINT_SALT)
        self.assertEqual(
            report["fingerprints"]["SUPABASE_SERVICE_KEY"],
            _fingerprint("SUPABASE_SERVICE_KEY", LOCAL_ENV["SUPABASE_SERVICE_KEY"]),
        )
        self.assertEqual(report["key_ring"]["primary_version"], 1)
        serialized = json.dumps(report, ensure_ascii=False)
        for value in LOCAL_ENV.values():
            self.assertNotIn(value, serialized)


class _FingerprintsHandler(BaseHTTPRequestHandler):
    """Un service déployé minimal : la même réponse, et la même clé exigée."""

    def do_GET(self):  # noqa: N802 - nom imposé par BaseHTTPRequestHandler
        if self.path != DEPLOYED_FINGERPRINTS_PATH:
            self.send_error(404)
            return
        if self.headers.get("X-API-Key") != self.server.api_key:  # type: ignore[attr-defined]
            self.send_error(401)
            return
        body = json.dumps(self.server.payload()).encode("utf-8")  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # pragma: no cover - bruit de serveur
        pass


class EndToEndDeployedCheckTest(unittest.TestCase):
    """La chaîne entière : vraies sockets, vrai rapport, barrière en sous-processus."""

    def _serve(self, payload, api_key):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _FingerprintsHandler)
        server.daemon_threads = True
        server.payload = payload  # type: ignore[attr-defined]
        server.api_key = api_key  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return f"http://127.0.0.1:{server.server_port}"

    def _production_payload(self, values):
        """Le rapport tel que ce service le publierait, calculé par le **vrai** code.

        L'environnement est injecté autour de la mesure, puis oublié : le rapport
        décrit la configuration que le processus aurait chargée, pas celle du test.
        """
        from core import config_runtime

        with mock.patch.dict(os.environ, values):
            config_runtime.reset_env_config()
            try:
                return config_runtime.deployed_secret_report()
            finally:
                config_runtime.reset_env_config()

    def _run(self, env_file, *args):
        """Lance la barrière comme un pipeline le ferait, sans hériter d'un secret.

        L'environnement du sous-processus est **nettoyé** de tous les secrets
        connus : `load_effective_env` fait gagner l'environnement sur le fichier,
        donc un secret hérité du processus de test rendrait le `.env` jetable
        inopérant — et le test mesurerait autre chose que ce qu'il écrit.
        """
        env = {
            name: value
            for name, value in os.environ.items()
            if name not in {spec.name for spec in DEFAULT_SPECS} and name != DEPLOYED_URL_ENV
        }
        env["PYTHONIOENCODING"] = "utf-8"
        command = [
            sys.executable,
            "-B",
            str(AUDIT_SCRIPT),
            "--env-file",
            str(env_file),
            "--ledger",
            str(env_file.parent / "ledger.json"),
            "--no-scan-repo",
            "--allow-missing-rotation",
            *args,
        ]
        return subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", env=env, cwd=env_file.parent
        )

    def _env_file(self, values):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / ".env"
        path.write_text(
            "".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8"
        )
        return path

    def test_a_production_that_matches_is_verified_not_assumed(self):
        env_file = self._env_file(LOCAL_ENV)
        url = self._serve(lambda: self._production_payload(PRODUCTION_ENV), LOCAL_ENV["INTERNAL_API_KEY"])
        done = self._run(env_file, "--remote", url, "--require-remote", "--json")
        report = json.loads(done.stdout)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(report["deployed"]["checked"])
        self.assertEqual(sorted(report["deployed"]["matched"]), sorted(LOCAL_ENV))
        self.assertEqual(report["deployed"]["mismatched"], [])

    def test_a_production_that_differs_refuses_and_shows_no_value(self):
        env_file = self._env_file(LOCAL_ENV)
        production = dict(PRODUCTION_ENV, ENCRYPTION_KEY="Zm9vYmFyYmF6cXV1eDEyMzQ1Njc4OTBhYmNkZWY=")
        url = self._serve(lambda: self._production_payload(production), LOCAL_ENV["INTERNAL_API_KEY"])
        done = self._run(env_file, "--remote", url, "--require-remote")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("ENCRYPTION_KEY", done.stdout)
        self.assertIn("DIFFÉRENTE", done.stdout)
        # Ni la valeur locale, ni celle de la production ne sont affichées.
        for value in (LOCAL_ENV["ENCRYPTION_KEY"], production["ENCRYPTION_KEY"]):
            self.assertNotIn(value, done.stdout + done.stderr)

    def test_a_production_that_refuses_the_key_names_the_cause(self):
        env_file = self._env_file(LOCAL_ENV)
        url = self._serve(lambda: self._production_payload(PRODUCTION_ENV), "une-autre-cle-interne-0")
        done = self._run(env_file, "--remote", url)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("INTERNAL_API_KEY", done.stdout)

    def test_a_service_that_is_not_the_expected_one_is_refused(self):
        env_file = self._env_file(LOCAL_ENV)
        url = self._serve(lambda: {"ok": True}, LOCAL_ENV["INTERNAL_API_KEY"])
        done = self._run(env_file, "--remote", url)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("ne vient pas de ce service", done.stdout + done.stderr)

    def test_requiring_the_remote_without_a_url_refuses(self):
        env_file = self._env_file(LOCAL_ENV)
        done = self._run(env_file, "--require-remote")
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("exigée", done.stdout)

    def test_without_any_url_the_green_light_still_says_what_it_ignored(self):
        env_file = self._env_file(LOCAL_ENV)
        done = self._run(env_file)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("NON VÉRIFIÉE", done.stdout)
        self.assertIn("LA PRODUCTION N'A PAS ÉTÉ VÉRIFIÉE", done.stdout)

    def test_the_deployed_url_variable_is_used_when_no_option_is_given(self):
        """`DEPLOYED_URL` du `.env` suffit : le pipeline n'a pas à la répéter."""
        env_file = self._env_file(LOCAL_ENV)
        url = self._serve(lambda: self._production_payload(PRODUCTION_ENV), LOCAL_ENV["INTERNAL_API_KEY"])
        with env_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{DEPLOYED_URL_ENV}={url}\n")
        done = self._run(env_file, "--require-remote", "--json")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(json.loads(done.stdout)["deployed"]["checked"])

    def test_no_remote_opt_out_is_explicit_and_visible(self):
        env_file = self._env_file(LOCAL_ENV)
        url = self._serve(lambda: self._production_payload(PRODUCTION_ENV), LOCAL_ENV["INTERNAL_API_KEY"])
        with env_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{DEPLOYED_URL_ENV}={url}\n")
        done = self._run(env_file, "--no-remote")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("NON VÉRIFIÉE", done.stdout)


if __name__ == "__main__":  # pragma: no cover - exécution directe
    unittest.main()
