"""L'endpoint de réconciliation média (`api/admin_router.py`).

Ce fichier mesure ce qu'une réponse **heureuse** ne montre pas :

* une panne du Storage répond **502**, jamais une liste vide. Une liste vide
  signifie « bucket et base cohérents » : confondre les deux ferait passer une
  panne pour une bonne nouvelle, et c'est précisément ce que le code de sortie
  `1` du script distingue ;
* l'endpoint **ne peut pas supprimer** : le module n'importe pas `delete_objects`
  — ce n'est pas une convention de style, c'est la seule chose qui empêche un
  `GET` rejoué par un cache d'effacer des octets ;
* `limit` borne la **réponse**, pas le travail : `count` reste le total exact, et
  une réponse tronquée le dit au lieu de laisser croire qu'il n'y en a pas plus.

L'endpoint est appelé **directement** (c'est une fonction), sans HTTP :
l'authentification, l'enregistrement de la route et la validation des bornes de
`limit` sont tenus ailleurs, dans `tests/test_api_auth_integration.py` et par le
golden de surface HTTP (`tests/goldens/api_routes.json`). Ce fichier ne mesure
donc que le métier.

FastAPI est une dépendance de l'application, pas de ce module : dans un
environnement de développement minimal où il manque, les tests sont **sautés**
plutôt que de faire échouer la collecte — même règle que
`tests/test_media_router.py`.
"""
from __future__ import annotations

import pathlib
import unittest
from unittest import mock

try:
    from fastapi import HTTPException

    from api import admin_router
    from core import media_repair as media_repair_module
    from database import media_store
    from scripts import check_supabase as check_supabase_module

    _FASTAPI_AVAILABLE = True
except Exception as exc:  # pragma: no cover - dépend de l'environnement
    _FASTAPI_AVAILABLE = False
    _IMPORT_ERROR = exc


@unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI non installé")
class OrphanEndpointTest(unittest.TestCase):
    """Le comportement de la route, doublure à la seule frontière de stockage."""

    def setUp(self) -> None:
        self.orphans: list = []
        self.calls: list = []
        self.failure: Exception | None = None
        self._patch("list_orphan_objects", self._list_orphans)

    def _patch(self, name, replacement):
        patcher = mock.patch.object(admin_router, name, side_effect=replacement)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _list_orphans(self, prefix=""):
        """Doublure qui **retient** le préfixe demandé."""
        self.calls.append(prefix)
        if self.failure is not None:
            raise self.failure
        return list(self.orphans)

    def _call(self, prefix="", limit=admin_router.DEFAULT_LIMIT, offset=0):
        return admin_router.media_orphans(prefix=prefix, limit=limit, offset=offset)

    # -- le cas nominal ---------------------------------------------------- #

    def test_the_orphans_are_returned_with_their_bucket(self):
        self.orphans = ["telegram/chan/a.jpg", "telegram/chan/b.jpg"]
        body = self._call()
        self.assertEqual(body["orphans"], self.orphans)
        self.assertEqual(body["bucket"], media_store.BUCKET)
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["returned"], 2)
        self.assertFalse(body["truncated"])

    def test_a_coherent_bucket_is_an_empty_answer_not_an_error(self):
        """Zéro orphelin est une **réponse** : le bucket et la base concordent."""
        body = self._call()
        self.assertEqual(body["orphans"], [])
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["returned"], 0)
        self.assertFalse(body["truncated"])

    # -- la borne ---------------------------------------------------------- #

    def test_the_limit_bounds_the_payload_not_the_count(self):
        """`count` est le total, `orphans` la tranche : le diagnostic reste exact."""
        self.orphans = [f"telegram/orphelin-{index}.jpg" for index in range(5)]
        body = self._call(limit=2)
        self.assertEqual(body["count"], 5, "le total doit rester exact")
        self.assertEqual(body["returned"], 2)
        self.assertTrue(body["truncated"])
        self.assertEqual(body["orphans"], self.orphans[:2])

    def test_a_list_shorter_than_the_limit_is_not_truncated(self):
        self.orphans = ["un.jpg"]
        body = self._call(limit=1)
        self.assertFalse(body["truncated"])
        self.assertIsNone(body["next_offset"])

    def test_a_first_page_is_not_promised_a_follower(self):
        """`next_offset` absent veut dire « fin », pas « je ne sais pas » :
        l'annoncer à tort ferait cliquer vers une page vide."""
        self.orphans = ["a.jpg", "b.jpg"]
        body = self._call(limit=2)
        self.assertEqual(body["returned"], 2)
        self.assertIsNone(body["next_offset"])

    def test_an_offset_past_the_end_is_an_empty_page_not_an_error(self):
        """Fin de liste et « aucun orphelin » ne se ressemblent pas : `count` le dit."""
        self.orphans = ["a.jpg", "b.jpg"]
        body = self._call(offset=5)
        self.assertEqual(body["orphans"], [])
        self.assertEqual(body["returned"], 0)
        self.assertFalse(body["truncated"])
        self.assertIsNone(body["next_offset"])
        self.assertEqual(body["count"], 2, "le bucket n'est pas devenu vide pour autant")

    def test_the_scan_is_never_bound_by_the_limit(self):
        """Borner le travail demanderait un `limit` à `list_orphan_objects`.

        S'il l'acceptait un jour, `count` deviendrait un total **faux** — le genre
        d'erreur qui fait croire à un bucket presque propre.
        """
        self.orphans = [f"o-{index}.jpg" for index in range(4)]
        self._call(limit=1)
        self.assertEqual(self.calls, [""], "le parcours reçoit le préfixe, pas la borne")

    def test_an_offset_does_not_turn_the_walk_into_a_cheap_one(self):
        """Paginer ne doit pas devenir « je ne regarde que ma page ».

        `count` est le total **entier** : le parcours doit rester complet sur une
        page avancée, sinon un opérateur lirait « 37 orphelins » sans que rien
        n'ait été compté au-delà de sa tranche.
        """
        self.orphans = [f"o-{index}.jpg" for index in range(4)]
        body = self._call(limit=1, offset=3)
        self.assertEqual(self.calls, [""], "le parcours ne reçoit pas la tranche")
        self.assertEqual(body["count"], 4, "le total reste celui du bucket entier")

    # -- la pagination ------------------------------------------------------ #

    def test_the_whole_list_is_reachable_without_raising_the_limit(self):
        """La question que la page 1 pose : « et la suite ? ».

        On parcourt le listing page par page, `next_offset` en main, et on vérifie
        que rien n'est caché **ni répété** — c'est la seule façon de savoir qu'une
        pagination est complète plutôt que plausible.
        """
        self.orphans = [f"o-{index:02d}.jpg" for index in range(7)]
        seen: list = []
        offset = 0
        pages = 0
        # La borne du `while` n'est pas décorative : une pagination qui renvoie
        # toujours la même page ferait tourner ce test **indéfiniment** au lieu
        # d'échouer, et un test qui pend ne dit rien.
        while offset is not None and pages < 10:
            body = self._call(limit=3, offset=offset)
            self.assertEqual(body["offset"], offset, "le rang est renvoyé tel quel")
            self.assertEqual(body["limit"], 3)
            self.assertEqual(
                body["truncated"], body["next_offset"] is not None, "les deux le disent"
            )
            seen.extend(body["orphans"])
            offset = body["next_offset"]
            pages += 1
        self.assertIsNone(offset, "la pagination s'arrête d'elle-même")
        self.assertEqual(seen, self.orphans, "parcours complet, dans l'ordre, sans doublon")
        self.assertEqual(pages, 3, "3 + 3 + 1")

    def test_the_last_page_announces_the_end(self):
        self.orphans = [f"o-{index}.jpg" for index in range(4)]
        body = self._call(limit=2, offset=2)
        self.assertEqual(body["returned"], 2)
        self.assertFalse(body["truncated"])
        self.assertIsNone(body["next_offset"], "rien à demander après")

    def test_the_default_limit_is_the_advertised_one(self):
        self.assertEqual(admin_router.DEFAULT_LIMIT, 100)

    # -- le périmètre ------------------------------------------------------ #

    def test_the_prefix_is_normalized_and_transmitted(self):
        """Le préfixe part **normalisé** (`iter_storage_paths` retire les `/`)."""
        self._call(prefix="/telegram/thehalalwinningteam/")
        self.assertEqual(self.calls, ["telegram/thehalalwinningteam"])

    def test_the_prefix_is_echoed_as_scanned(self):
        """Ce qui est relu dans la réponse est ce qui a été parcouru."""
        body = self._call(prefix="/telegram/chan/")
        self.assertEqual(body["prefix"], "telegram/chan")

    def test_no_prefix_means_the_whole_bucket(self):
        self.assertEqual(self._call()["prefix"], "")

    # -- les pannes -------------------------------------------------------- #

    def test_a_storage_failure_is_a_502_not_an_empty_list(self):
        """Le point qui compte : une panne ne doit pas ressembler à la cohérence."""
        self.failure = RuntimeError("Storage injoignable")
        with self.assertRaises(HTTPException) as caught:
            self._call()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("Storage injoignable", caught.exception.detail)

    def test_a_missing_database_row_read_fails_loudly_too(self):
        self.failure = KeyError("storage_path")
        with self.assertRaises(HTTPException) as caught:
            self._call()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("KeyError", caught.exception.detail)

    # -- lecture seule ----------------------------------------------------- #

    def test_the_endpoint_cannot_delete_anything(self):
        """Le module n'importe **pas** la suppression : elle reste au script.

        Un `GET` qui supprime est rejoué par un navigateur, un cache ou une sonde
        de disponibilité — et efface des octets sans trace.
        """
        self.assertFalse(hasattr(admin_router, "delete_objects"))
        self.assertNotIn("delete_objects", pathlib.Path(admin_router.__file__).read_text(encoding="utf-8"))

    def test_the_response_names_where_deletion_lives(self):
        body = self._call()
        self.assertIn("reconcile_media.py", body["deletion"])
        self.assertIn("--delete", body["deletion"])

    # -- la surface -------------------------------------------------------- #

    def test_the_routes_are_the_advertised_ones(self):
        """Les chemins demandés, ici dans leur composition (préfixe compris)."""
        self.assertEqual(admin_router.router.prefix, "/admin")
        paths = {route.path for route in admin_router.router.routes}
        self.assertEqual(
            paths,
            {
                "/admin/media/orphans",
                "/admin/media/missing",
                "/admin/media/missing/repair",
                "/admin/supabase/check",
                "/admin/supabase/roundtrip",
            },
        )
        methods = {method for route in admin_router.router.routes for method in route.methods}
        self.assertEqual(methods, {"GET", "POST"})

    def test_the_consultations_are_gets_and_the_two_writes_are_posts(self):
        """Un `GET` qui re-télécharge ou qui écrit se déclencherait depuis un cache
        ou un préchargement de lien : les deux se demandent explicitement."""
        by_path = {
            route.path: route.methods for route in admin_router.router.routes
        }
        self.assertEqual(by_path["/admin/media/missing"], {"GET"})
        self.assertEqual(by_path["/admin/media/orphans"], {"GET"})
        self.assertEqual(by_path["/admin/media/missing/repair"], {"POST"})
        self.assertEqual(by_path["/admin/supabase/check"], {"GET"})
        self.assertEqual(by_path["/admin/supabase/roundtrip"], {"POST"})


ROW = {
    "id": "m1",
    "storage_path": "telegram/chan/2-b.jpg",
    "media_type": "photo",
    "source": "telegram_channel",
    "chat_id": "chan",
    "message_id": 2,
    "file_size": 6,
    "created_at": "2026-09-27T10:00:00Z",
    "telegram_file_id": "AgAC-2",
    "metadata": {
        media_store.ASSET_KEY: {"value": "BTC-USD", "source": "caption"},
        # L'empreinte des octets que `_download` rend à la réparation.
        media_store.FINGERPRINT_KEY: media_store.fingerprint(b"octets"),
    },
}


@unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI non installé")
class MissingEndpointTest(unittest.TestCase):
    """Le sens inverse : des lignes décrites dont les octets ont disparu."""

    def setUp(self) -> None:
        self.rows: list = [dict(ROW)]
        self.calls: list = []
        self.failure: Exception | None = None
        self._patch("list_missing_objects", self._list_missing)

    def _patch(self, name, replacement):
        patcher = mock.patch.object(admin_router, name, side_effect=replacement)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _list_missing(self, prefix=""):
        self.calls.append(prefix)
        if self.failure is not None:
            raise self.failure
        return [dict(row) for row in self.rows]

    def _call(self, prefix="", limit=admin_router.DEFAULT_LIMIT):
        return admin_router.media_missing(prefix=prefix, limit=limit)

    def test_a_missing_media_is_described_with_its_repair_route(self):
        """Sans la voie possible, la liste oblige à tout tenter pour le savoir."""
        body = self._call()
        entry = body["missing"][0]
        self.assertEqual(entry["media_id"], "m1")
        self.assertEqual(entry["storage_path"], "telegram/chan/2-b.jpg")
        self.assertEqual(entry["route"], media_repair_module.ROUTE_FILE_ID)
        self.assertEqual(entry["asset"], "BTC-USD")
        self.assertTrue(entry["fingerprint"], "la restauration pourra être prouvée")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["repairable"], 1)
        self.assertEqual(body["repair"], "POST /admin/media/missing/repair")

    def test_a_row_without_a_recorded_fingerprint_says_so(self):
        """`false` ne condamne pas la ligne : on ne saura dire que la taille."""
        self.rows = [{**ROW, "metadata": {}}]
        body = self._call()
        self.assertFalse(body["missing"][0]["fingerprint"])
        self.assertEqual(body["missing"][0]["route"], media_repair_module.ROUTE_FILE_ID)

    def test_a_row_without_any_route_is_announced_as_such(self):
        """Un `chat_id` numérique et aucun `file_id` : rien à tenter."""
        self.rows = [{**ROW, "telegram_file_id": None, "chat_id": "-100"}]
        body = self._call()
        self.assertIsNone(body["missing"][0]["route"])
        self.assertEqual(body["repairable"], 0)

    def test_the_limit_truncates_but_the_count_stays_exact(self):
        self.rows = [{**ROW, "id": f"m{index}"} for index in range(4)]
        body = self._call(limit=2)
        self.assertEqual(body["count"], 4)
        self.assertEqual(body["returned"], 2)
        self.assertTrue(body["truncated"])

    def test_the_prefix_is_normalized_and_transmitted(self):
        self._call(prefix="/telegram/chan/")
        self.assertEqual(self.calls, ["telegram/chan"])
        self.assertEqual(self._call(prefix="/telegram/chan/")["prefix"], "telegram/chan")

    def test_a_storage_failure_is_a_502_not_an_empty_list(self):
        """Une panne ne doit pas ressembler à « rien ne manque »."""
        self.failure = RuntimeError("Storage injoignable")
        with self.assertRaises(HTTPException) as caught:
            self._call()
        self.assertEqual(caught.exception.status_code, 502)

    def test_the_listing_does_not_delete_or_repair_anything(self):
        """Elle annonce la voie possible, elle ne l'emprunte pas."""
        source = pathlib.Path(admin_router.__file__).read_text(encoding="utf-8")
        listing = source.split("def media_missing", 1)[1].split("@router.post", 1)[0]
        for forbidden in ("repair_missing", "restore_object", "delete_objects"):
            self.assertNotIn(forbidden, listing)


@unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI non installé")
class RepairEndpointTest(unittest.IsolatedAsyncioTestCase):
    """La réparation : nommée, bornée, et sans jamais écraser un objet présent."""

    def setUp(self) -> None:
        self.rows = {"m1": dict(ROW)}
        self.present = False
        self.restored: list = []
        self.downloads: list = []
        self._patch(admin_router, "get_media", lambda media_id: self.rows.get(media_id))
        self._patch(media_store, "object_exists", lambda path: self.present)
        self._patch(media_store, "restore_object", self._restore)
        self._patch(admin_router, "download_by_file_id", self._download)
        # Le seul chemin qui réécrirait la ligne : s'il est emprunté, on le sait.
        self._patch(media_store, "upload_media", self._forbidden)

    def _patch(self, module, name, replacement):
        patcher = mock.patch.object(module, name, replacement)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _forbidden(self, *args, **kwargs):
        raise AssertionError("la réparation ne doit pas réécrire la ligne")

    async def _download(self, file_id):
        self.downloads.append(file_id)
        return b"octets"

    def _restore(self, path, data, *, mime_type=None, upsert=False):
        self.restored.append((path, bytes(data), mime_type))
        return len(data)

    async def _request(self, media_ids):
        return await admin_router.repair_missing_media(
            admin_router.RepairRequest(media_ids=media_ids)
        )

    async def test_a_missing_media_comes_back_at_its_own_path(self):
        report = await self._request(["m1"])
        self.assertEqual(report["requested"], 1)
        self.assertEqual(report["restored"], 1)
        self.assertEqual(report["failed"], 0)
        self.assertEqual(report["unknown"], [])
        self.assertEqual(report["results"][0]["route"], media_repair_module.ROUTE_FILE_ID)
        self.assertEqual(self.downloads, ["AgAC-2"])
        self.assertEqual(self.restored[0][0], "telegram/chan/2-b.jpg")

    async def test_an_object_still_present_is_not_overwritten(self):
        """La voie du scraper rend la copie de l'aperçu, possiblement réduite."""
        self.present = True
        report = await self._request(["m1"])
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(report["restored"], 0)
        self.assertEqual(self.downloads, [], "aucun téléchargement pour un objet présent")
        self.assertEqual(self.restored, [])
        self.assertIn("déjà présent", report["results"][0]["reason"])

    async def test_a_repair_proves_the_bytes_are_those_of_the_ingestion(self):
        """`_download` rend exactement ce que l'ingestion avait empreinté."""
        report = await self._request(["m1"])
        self.assertTrue(report["results"][0]["fingerprint_matches"])
        self.assertEqual(report["verified"], 1)
        self.assertEqual(report["diverged"], 0)

    async def test_a_preview_copy_is_named_as_a_different_file(self):
        """L'aperçu public rend une copie réduite : même si le compte tombe
        juste, l'empreinte dit que ce n'est pas le fichier d'origine."""
        self.rows["m1"] = {**ROW, "telegram_file_id": None, "file_size": 6}

        async def _preview(channel, message_id, *, max_bytes=None):
            return {"data": b"autre!"}

        self._patch(media_repair_module.telegram_channel, "fetch_message_media", _preview)
        report = await self._request(["m1"])
        self.assertEqual(report["restored"], 1)
        self.assertEqual(report["verified"], 0)
        self.assertEqual(report["diverged"], 1)
        self.assertTrue(report["results"][0]["size_matches"])
        self.assertFalse(report["results"][0]["fingerprint_matches"])

    async def test_an_unknown_id_is_reported_without_interrupting_the_rest(self):
        report = await self._request(["inconnu", "m1"])
        self.assertEqual(report["unknown"], ["inconnu"])
        self.assertEqual(report["requested"], 1)
        self.assertEqual(report["restored"], 1)

    async def test_the_line_is_never_rewritten(self):
        """Le rapport ne porte pas la ligne, et `upload_media` n'est pas appelé :
        un verdict de revue, un actif ou une note d'extraction ne peuvent pas être
        effacés par une réparation de fichier."""
        report = await self._request(["m1"])
        self.assertTrue(report["results"][0]["ok"])
        self.assertNotIn("metadata", report["results"][0])


@unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI non installé")
class SupabaseProbeEndpointTest(unittest.IsolatedAsyncioTestCase):
    """La sonde Supabase derrière la surface : ce qui écrit, et ce qui refuse.

    Ce fichier mesure ce qu'une réponse heureuse ne montre pas :

    * l'aller-retour est un **`POST` à sélection explicite** — un nom inconnu est
      refusé **avant** la première écriture, jamais replié sur « toutes les
      tables ». C'est la seule façon dont cette route pourrait faire des dégâts :
      un repli silencieux écrirait précisément là où on a demandé de ne pas aller ;
    * le verdict du script reste **dans la réponse** (`ok`, `writes`), fût-il
      mauvais : une base en échec est un 200 qui le dit. Un 502 est réservé à la
      sonde qui **tombe**, où l'appel n'a rien rendu de lisible ;
    * la requête ne fait **pas** lire le `.env` de la machine : le serveur a déjà
      sa configuration, et une requête ne doit pas pouvoir désigner une autre base
      que la sienne.

    La sonde elle-même (`run`) est doublée : ce qui est mesuré ici est la route.
    `tests/test_supabase_config.py` tient l'autre moitié — ce que la sonde
    vérifie, et dans quel ordre elle nettoie.
    """

    def setUp(self) -> None:
        self.calls: list = []
        self.report: dict = {"sections": [], "ok": True}
        self.failure: Exception | None = None
        self._patch(check_supabase_module, "run", self._run)
        #: S'il est appelé, c'est un échec : la route ne doit rien charger.
        self._patch(check_supabase_module, "load_project_env", self._forbidden_env)

    def _patch(self, module, name, replacement):
        patcher = mock.patch.object(module, name, replacement)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _forbidden_env(self):
        raise AssertionError("une requête ne doit pas lire le .env de la machine")

    def _forbidden_alert(self, *args, **kwargs):
        raise AssertionError("aucune alerte ne doit partir sans reste en base")

    def _run(self, **kwargs):
        """Doublure qui **retient** comment elle a été appelée."""
        self.calls.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return dict(self.report)

    async def _roundtrip(self, tables):
        return await admin_router.supabase_roundtrip(
            admin_router.SupabaseProbeRequest(tables=tables)
        )

    async def _check(self, only=None):
        return await admin_router.supabase_check(only=only)

    # -- ce que la route transmet à la sonde ------------------------------ #

    async def test_the_roundtrip_writes_and_the_check_does_not(self):
        """Le drapeau qui décide de tout, et ce que la réponse en dit."""
        written = await self._roundtrip(["core", "engine"])
        read = await self._check(only=["core", "engine"])
        self.assertEqual([call["roundtrip"] for call in self.calls], [True, False])
        self.assertEqual(written["writes"], list(check_supabase_module.ROUNDTRIP_TABLES))
        self.assertIsNone(read["writes"], "une consultation n'annonce aucune écriture")

    async def test_the_tables_asked_for_are_the_tables_transmitted(self):
        await self._roundtrip(["economic_events", "users"])
        self.assertEqual(self.calls[0]["only"], ["economic_events", "users"])

    async def test_the_selection_is_resolved_by_the_script_itself(self):
        """Un groupe se résout comme en ligne de commande, pas ici."""
        body = await self._roundtrip(["engine"])
        self.assertEqual(body["selection"]["requested"], ["engine"])
        self.assertEqual(
            set(body["selection"]["tables"]),
            set(check_supabase_module.TABLE_GROUPS["engine"]),
        )

    async def test_the_response_names_the_equivalent_command(self):
        """Ce que l'opérateur taperait pour rejouer la même chose à la main."""
        body = await self._roundtrip(["core", "engine"])
        self.assertIn("check_supabase.py", body["command"])
        self.assertIn("--roundtrip", body["command"])
        self.assertIn("--only core,engine", body["command"])

    async def test_a_consultation_command_carries_no_roundtrip(self):
        body = await self._check(only=["users"])
        self.assertNotIn("--roundtrip", body["command"])

    # -- le refus --------------------------------------------------------- #

    async def test_an_unknown_name_is_refused_before_any_write(self):
        with self.assertRaises(HTTPException) as caught:
            await self._roundtrip(["economic_event"])
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("economic_event", caught.exception.detail)
        self.assertEqual(self.calls, [], "rien ne doit être éprouvé sur un nom inconnu")

    async def test_the_refusal_lists_the_choices(self):
        """Après un refus, ce qu'on cherche est précisément quoi taper."""
        with self.assertRaises(HTTPException) as caught:
            await self._roundtrip(["nawak"])
        for choice in (*check_supabase_module.TABLE_GROUPS, *check_supabase_module.REQUIRED_TABLES):
            with self.subTest(choice=choice):
                self.assertIn(choice, caught.exception.detail)
        self.assertIn("--only", caught.exception.detail)

    async def test_the_read_only_check_refuses_the_same_names(self):
        with self.assertRaises(HTTPException) as caught:
            await self._check(only=["nawak"])
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(self.calls, [])

    # -- le verdict ------------------------------------------------------- #

    async def test_a_failing_base_is_a_report_not_a_transport_error(self):
        """Le rapport porte `ok` : une base en échec se lit, elle ne se devine pas."""
        self.report = {
            "sections": [
                {
                    "title": "1. Configuration",
                    "checks": [{"name": "SUPABASE_URL", "ok": False, "detail": "vide"}],
                }
            ],
            "ok": False,
        }
        body = await self._check()
        self.assertFalse(body["ok"])
        self.assertEqual(body["sections"], self.report["sections"])

    async def test_the_sections_of_the_script_are_returned_as_they_are(self):
        """La route ne reformate pas : elle enrichit, elle ne réécrit pas."""
        self.report = {"sections": [{"title": "2. Tables", "checks": []}], "ok": True}
        body = await self._roundtrip(["core"])
        self.assertEqual(body["sections"], self.report["sections"])
        self.assertIn("selection", body)

    async def test_a_probe_that_blows_up_is_a_502(self):
        """La sonde qui **tombe** n'a rien rendu de lisible : là, c'est une panne."""
        self.failure = RuntimeError("client Supabase absent")
        with self.assertRaises(HTTPException) as caught:
            await self._roundtrip(["core"])
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("RuntimeError", caught.exception.detail)

    async def test_a_selection_the_probe_cannot_exercise_says_so(self):
        """`knowledge` n'est pas éprouvée par l'aller-retour : `writes` est vide.

        Un aller-retour qui n'écrit rien doit se lire comme tel, pas comme un
        succès silencieux.
        """
        body = await self._roundtrip(["knowledge"])
        self.assertEqual(body["writes"], [])

    # -- le nettoyage incomplet ------------------------------------------- #

    async def test_a_leftover_row_is_relayed_and_the_operator_is_alerted(self):
        """Un reste en base ne doit pas dépendre de qui a la page ouverte."""
        self.report = {
            "sections": [],
            "ok": False,
            "leftovers": ["insights.asset=PROBE (TypeError: refus)"],
        }
        alerts: list = []
        self._patch(
            check_supabase_module,
            "alert_leftovers",
            lambda rows: alerts.append(list(rows)) or {"sent": True, "reason": None},
        )
        body = await self._roundtrip(["core"])
        self.assertEqual(body["leftovers"], self.report["leftovers"])
        self.assertEqual(alerts, [self.report["leftovers"]])
        self.assertEqual(body["alert"], {"sent": True, "reason": None})

    async def test_without_a_leftover_no_alert_is_attempted(self):
        """Alerter sur un nettoyage réussi ferait du bruit là où rien ne cloche."""
        self._patch(check_supabase_module, "alert_leftovers", self._forbidden_alert)
        body = await self._roundtrip(["core"])
        self.assertEqual(body["leftovers"], [])
        self.assertIsNone(body["alert"])

    # -- ce que la route ne fait pas -------------------------------------- #

    async def test_the_request_never_reads_the_dot_env(self):
        """Le serveur a déjà sa configuration : une requête ne désigne pas une base."""
        source = pathlib.Path(admin_router.__file__).read_text(encoding="utf-8")
        probe = source.split("def supabase_probe", 1)[1].split("__all__", 1)[0]
        self.assertNotIn("load_project_env", probe)
        await self._check()
        await self._roundtrip(["core"])  # la doublure lève si la route le charge

    async def test_the_probe_module_is_imported_at_call_time(self):
        """La surface protégée doit s'importer sans le paquet `supabase`."""
        source = pathlib.Path(admin_router.__file__).read_text(encoding="utf-8")
        head = source.split("def supabase_probe", 1)[0]
        self.assertNotIn("import scripts", head)
        self.assertNotIn("from scripts", head)


if __name__ == "__main__":
    unittest.main()
