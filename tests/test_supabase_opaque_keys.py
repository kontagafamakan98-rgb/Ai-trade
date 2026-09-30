"""Le client Supabase accepte les clés **opaques** (`sb_secret_…`), sans upgrade.

Les clés de la nouvelle génération ne sont pas des JWT, et supabase-py 2.7.4
refuse tout ce qui n'a pas la forme `a.b.c` (`SupabaseException: Invalid API
key`) : le client ne se construisait donc **pas du tout**, et comme
`database/supabase_client.py` avale l'exception en `SUPABASE_ERROR`, la base
paraissait vide — le pire des symptômes, puisqu'il ressemble à « rien à lire ».

Ce que ces tests prouvent : la clé part dans l'en-tête **`apikey`**, et **aucun**
`Authorization: Bearer <clé>` ne l'accompagne — la passerelle y attend un JWT
d'**utilisateur**, pas une clé opaque.

La mesure porte sur la **requête préparée pour l'envoi** : le transport HTTP est
doublé (`httpx.MockTransport`, le point d'entrée que toute requête traverse), pas
le client — les en-têtes vérifiés sont donc ceux que le client compose, fusion
d'en-têtes de session comprise, et non un dictionnaire que le test aurait monté.
C'est le transport qui est remplacé, et lui seul : la requête, l'URL, les
paramètres et les en-têtes restent le fait du client. (Une vraie socket locale a
été essayée d'abord : la session de `postgrest` demande `http2=True` et la
connexion abandonnée fait échouer un test sur cinq, sous Windows. Un test à
flotteur ne prouve rien.)

Le chemin des clés JWT — celui de la production d'aujourd'hui — est vérifié juste
à côté : sa place dans `Authorization` est ce qui le fait fonctionner, et rien de
cette correction ne doit la lui retirer.
"""
from __future__ import annotations

import importlib.util
import json
import unittest

import httpx

from database import supabase_client

#: Une clé de la nouvelle génération : un préfixe, du matériau, et **aucun
#: point** — c'est précisément ce que le contrôle de forme prenait pour une clé
#: invalide.
OPAQUE_KEY = "sb_secret_" + "3f9c2a7b" * 5
PUBLISHABLE_KEY = "sb_publishable_" + "a1b2c3d4" * 5

#: Une clé **JWT factice**, de la forme de l'ancienne génération (rôle lisible,
#: signature sans valeur) : elle sert à prouver que ce chemin-là n'a pas bougé.
LEGACY_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJyb2xlIjoic2VydmljZV9yb2xlIn0"
    ".signature-factice"
)


class OpaqueKeyTest(unittest.TestCase):
    """Ce que le client envoie, vu depuis le transport."""

    URL = "https://exemple.supabase.co"

    def setUp(self) -> None:
        if importlib.util.find_spec("supabase") is None:
            self.skipTest("supabase absent : aucun client ne peut être construit")
        self.sent: list[httpx.Request] = []

    def _send(self, key: str):
        """Une vraie lecture REST, et les en-têtes de la requête **envoyée**.

        Les noms sont rendus en minuscules : HTTP ne distingue pas la casse, et
        c'est sous cette forme qu'on peut affirmer ce que la passerelle lira.
        """
        client = supabase_client.build_supabase_client(self.URL, key)

        def recorder(request: httpx.Request) -> httpx.Response:
            self.sent.append(request)
            return httpx.Response(200, json=[])

        # Le **transport**, pas le client : la requête est celle que le client
        # prépare (URL, paramètres, en-têtes fusionnés avec ceux de la session).
        client.postgrest.session._transport = httpx.MockTransport(recorder)
        client.table("pending_signals").select("id").limit(1).execute()

        self.assertEqual(len(self.sent), 1, "une requête, une seule")
        request = self.sent[0]
        headers = {name.lower(): value for name, value in request.headers.items()}
        return client, request, headers

    # -- pourquoi cette correction existe ---------------------------------- #

    def test_the_library_alone_refuses_an_opaque_key(self):
        """Le défaut reproduit : sans ce module, aucune clé opaque n'entre.

        Si une version future l'acceptait nativement, la correction deviendrait
        inutile : ce test le **dit** (`skipped`) au lieu de rougir pour un progrès.
        """
        from supabase import create_client

        try:
            create_client(self.URL, OPAQUE_KEY)
        except Exception as error:  # la classe vit dans un module privé de la bibliothèque
            self.assertIn("Invalid API key", str(error))
        else:
            self.skipTest("supabase-py accepte désormais les clés opaques : correction inutile")

    # -- ce qui part sur le fil -------------------------------------------- #

    def test_the_opaque_key_travels_in_the_apikey_header(self):
        _client, request, headers = self._send(OPAQUE_KEY)

        self.assertEqual(request.method, "GET")
        self.assertTrue(request.url.path.startswith("/rest/v1/pending_signals"), request.url)
        self.assertEqual(headers.get("apikey"), OPAQUE_KEY)

    def test_no_jwt_authorization_header_accompanies_it(self):
        """La passerelle attend un JWT d'utilisateur là : une clé opaque la fait échouer."""
        _client, _request, headers = self._send(OPAQUE_KEY)

        self.assertNotIn("authorization", headers)

    def test_a_publishable_key_travels_the_same_way(self):
        _client, _request, headers = self._send(PUBLISHABLE_KEY)

        self.assertEqual(headers.get("apikey"), PUBLISHABLE_KEY)
        self.assertNotIn("authorization", headers)

    def test_the_temporary_shape_never_leaves_the_process(self):
        """La forme `….sig` n'existe que le temps du contrôle de la bibliothèque."""
        client, _request, headers = self._send(OPAQUE_KEY)

        self.assertEqual(client.supabase_key, OPAQUE_KEY)
        self.assertEqual(client.options.headers["apiKey"], OPAQUE_KEY)
        placeholder = f"{OPAQUE_KEY}.{supabase_client.JWT_SHAPED_SUFFIX}"
        self.assertNotIn(placeholder, json.dumps(headers))
        self.assertEqual(
            [value for value in headers.values() if value.endswith(".sig")],
            [],
            "aucun en-tête ne doit porter la forme temporaire",
        )

    def test_a_legacy_jwt_key_keeps_its_authorization_header(self):
        """La production d'aujourd'hui : la clé JWT voyage **aussi** en Bearer."""
        _client, _request, headers = self._send(LEGACY_KEY)

        self.assertEqual(headers.get("apikey"), LEGACY_KEY)
        self.assertEqual(headers.get("authorization"), f"Bearer {LEGACY_KEY}")

    # -- la correction atteint-elle tous les sous-clients ? ---------------- #

    def test_the_repair_reaches_the_other_sub_clients(self):
        """`options.headers` est le dictionnaire **partagé** : c'est tout le pari.

        Les sous-clients le lisent à leur construction paresseuse (`storage`,
        `functions`) ou à chaque appel (`auth`). Si l'un d'eux se mettait à en
        garder une copie, la correction ne vaudrait que pour `postgrest` — et
        cette copie apparaîtrait ici.
        """
        client = supabase_client.build_supabase_client(self.URL, OPAQUE_KEY)

        storage = {name.lower(): value for name, value in client.storage.session.headers.items()}
        self.assertEqual(storage.get("apikey"), OPAQUE_KEY)
        self.assertNotIn("authorization", storage)
        self.assertIs(
            client.auth._headers,
            client.options.headers,
            "le client d'authentification lit le dictionnaire partagé",
        )

    # -- la reconnaissance des deux générations ---------------------------- #

    def test_the_two_key_generations_are_recognised(self):
        self.assertTrue(supabase_client.is_opaque_key(OPAQUE_KEY))
        self.assertTrue(supabase_client.is_opaque_key(PUBLISHABLE_KEY))
        self.assertFalse(supabase_client.is_opaque_key(LEGACY_KEY))
        self.assertFalse(supabase_client.is_opaque_key(""))
        self.assertFalse(supabase_client.is_opaque_key(None))


if __name__ == "__main__":
    unittest.main()
