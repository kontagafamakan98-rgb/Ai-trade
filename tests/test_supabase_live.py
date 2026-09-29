"""Vérification de bout en bout contre la **vraie** base Supabase.

C'est le seul fichier de test qui parle à un vrai projet. Il est donc **dormant
par défaut** : rien ne s'exécute sans un opt-in explicite, quel que soit
l'environnement.

    SUPABASE_LIVE=1              python -m unittest tests.test_supabase_live   # lecture seule
    SUPABASE_LIVE_ROUNDTRIP=1    python -m unittest tests.test_supabase_live   # + écriture

L'opt-in n'est pas une coquetterie. `tests/test_api_auth_integration.py` injecte
une fausse configuration dans `os.environ` au moment de son import (et ne la
restaure pas : c'est délibéré, `config.py` fige ses constantes à l'import). Sous
`unittest discover`, un module qui lirait l'environnement à son propre import
verrait donc cette fausse config et se croirait configuré. Une vérification qui
écrit dans la base ne peut pas dépendre de ce genre de hasard.

Ce qui est vérifié ici ne peut pas l'être ailleurs — que la clé configurée soit
vraiment la clé `service_role`, que les tables soient vraiment joignables, et que
l'écriture soit vraiment visible en relecture. C'est exactement ce que masque la
RLS de ce projet : elle est en « deny by default » sans aucune policy, donc une
clé publique ne lit **rien** et n'écrit rien sans jamais lever — « clé publique »
et « base vide » ont le même symptôme.

L'aller-retour **écrit dans la base pointée par `.env`**, y compris en
production. Il supprime ses lignes de sonde ; l'opt-in est là pour que ça reste
un geste volontaire.
"""
from __future__ import annotations

import os
import pathlib
import sys
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402,F401  (charge `.env` avant toute lecture d'environnement)
from core import config_runtime  # noqa: E402
from database import supabase_client  # noqa: E402
from scripts import check_supabase  # noqa: E402

CFG = config_runtime.get_env_config()
URL = CFG.supabase_url
KEY = CFG.supabase_service_key
CLIENT_STATUS = supabase_client.client_status()

#: Demande explicite de vérifier la vraie base (lecture).
LIVE = config_runtime.parse_env_bool(os.environ.get("SUPABASE_LIVE"))
#: Demande explicite d'**écrire** dans la vraie base (`check_supabase --roundtrip`).
WRITE = config_runtime.parse_env_bool(os.environ.get("SUPABASE_LIVE_ROUNDTRIP"))

SKIP_NOT_REQUESTED = (
    "vérification live non demandée : passe SUPABASE_LIVE=1 pour lire la vraie base, "
    "ou SUPABASE_LIVE_ROUNDTRIP=1 pour écrire et relire"
)
SKIP_NO_CREDENTIALS = (
    "aucun identifiant Supabase : renseigne SUPABASE_URL et SUPABASE_SERVICE_KEY dans .env"
)
SKIP_WRITE_NOT_REQUESTED = (
    "écriture non demandée : passe SUPABASE_LIVE_ROUNDTRIP=1 pour exercer l'aller-retour "
    "(il écrit dans la base pointée par .env)"
)


def blocker(*, client: bool = False, write: bool = False) -> str:
    """Pourquoi ces tests ne peuvent pas tourner ici — `\"\"` s'ils le peuvent."""
    if not LIVE:
        return SKIP_NOT_REQUESTED
    if not (URL and KEY):
        return SKIP_NO_CREDENTIALS
    if client and not CLIENT_STATUS["ready"]:
        return f"client Supabase non construit : {CLIENT_STATUS['error']}"
    if write and not WRITE:
        return SKIP_WRITE_NOT_REQUESTED
    return ""


def live_only(*, client: bool = False, write: bool = False):
    """Décorateur de classe : dormant tant que rien ne le demande."""
    reason = blocker(client=client, write=write)
    return unittest.skipIf(bool(reason), reason)


def role_note() -> str:
    """Ce qu'il faut savoir quand la base ne répond rien d'utile."""
    role = config_runtime.supabase_key_role(KEY)
    if role == "anon":
        return (
            " La clé configurée est une clé **publique** : la RLS de ce projet est en "
            "deny-by-default sans aucune policy, donc elle ne lit rien et n'écrit rien — "
            "sans jamais lever. Utilise la clé `service_role`."
        )
    if not role:
        return " Le rôle de la clé n'a pas pu être lu (format inconnu)."
    return ""


def failures(report: dict) -> str:
    """Rend lisibles les seuls contrôles en échec d'un rapport."""
    lines = [
        f"  ❌ {section['title']} → {check['name']}: {check['detail']}"
        for section in report["sections"]
        for check in section["checks"]
        if not check["ok"]
    ]
    return "\n".join(lines) or "  (aucun détail)"


@live_only()
class LiveConfigurationTest(unittest.TestCase):
    """Ce que porte le `.env` du poste, tel quel."""

    def test_the_url_is_the_api_url(self):
        issue = config_runtime.supabase_url_issue(URL)
        self.assertEqual(issue, "", f"SUPABASE_URL={URL!r} : {issue}")

    def test_the_key_has_the_service_role(self):
        role = config_runtime.supabase_key_role(KEY)
        self.assertEqual(role, "service_role", f"rôle lu : {role!r}.{role_note()}")

    def test_the_client_is_built(self):
        self.assertTrue(CLIENT_STATUS["ready"], CLIENT_STATUS["error"])


@live_only(client=True)
class LiveReadOnlyTest(unittest.TestCase):
    """Aucune écriture : les tables répondent-elles à la clé configurée ?"""

    def test_every_required_table_answers(self):
        checks = check_supabase.table_checks(supabase_client.supabase)
        missing = [check for check in checks if not check["ok"]]
        self.assertEqual(
            [check["name"] for check in missing],
            [],
            "tables injoignables :\n"
            + "\n".join(f"  ❌ {c['name']}: {c['detail']}" for c in missing)
            + role_note(),
        )


@live_only(client=True, write=True)
class LiveRoundtripTest(unittest.TestCase):
    """Écrit, relit, supprime — sur la vraie base."""

    def setUp(self) -> None:
        # Ceinture et bretelles : le décorateur suffit à l'exécution normale, mais
        # un appel direct ne doit pas pouvoir écrire sans la demande explicite.
        reason = blocker(client=True, write=True)
        if reason:
            self.skipTest(reason)

    def test_the_roundtrip_succeeds_and_leaves_nothing(self):
        report = check_supabase.run(roundtrip=True, as_json=True)
        self.assertTrue(report["ok"], "l'aller-retour a échoué :\n" + failures(report) + role_note())

        cleanup = report["sections"][-1]["checks"][-1]
        self.assertEqual(cleanup["name"], "nettoyage")
        self.assertTrue(cleanup["ok"], f"nettoyage incomplet : {cleanup['detail']}")
        self.assertEqual(cleanup["detail"], "lignes de sonde supprimées")

    def test_no_probe_row_survives_an_independent_read(self):
        """La suppression doit être constatable sans croire l'outil sur parole.

        Les sept tables de l'aller-retour sont relues ici **sans passer par
        l'outil** : le rapport d'un outil ne peut pas être sa propre preuve que
        la base est propre.
        """
        check_supabase.run(roundtrip=True, as_json=True)
        client = supabase_client.supabase

        #: Les trois tables d'apprentissage sont filtrées sur un **préfixe**, pas
        #: sur `PROBE` : leur clé de sonde est unique à l'exécution
        #: (`PROBE-<suffixe>`), donc une égalité sur la constante ne trouverait plus
        #: rien — le contrôle se viderait en silence, ce qu'une vérification
        #: indépendante n'a pas le droit de faire. Le préfixe attrape aussi les
        #: restes laissés par les versions où la clé était partagée.
        leftovers = {
            # Migration 011.
            "users": client.table("users").select("id").like("id", "probe-%"),
            "insights": client.table("insights").select("id").like("asset", "PROBE%"),
            "pending_signals": client.table("pending_signals")
            .select("id")
            .eq("signal->>asset", "PROBE-USD"),
            # Migrations 005 et 006.
            "economic_events": client.table("economic_events")
            .select("id")
            .like("id", "ff_probe_%"),
            "macro_bias_logs": client.table("macro_bias_logs")
            .select("id")
            .like("symbol", "PROBE-%"),
            "trade_post_mortems": client.table("trade_post_mortems")
            .select("id")
            .like("asset", "PROBE%"),
            "adaptive_model_weights": client.table("adaptive_model_weights")
            .select("asset")
            .like("asset", "PROBE%"),
        }
        for table, query in leftovers.items():
            with self.subTest(table=table):
                self.assertEqual(
                    query.execute().data or [],
                    [],
                    f"des lignes {table} de sonde sont restées",
                )


if __name__ == "__main__":
    unittest.main()
