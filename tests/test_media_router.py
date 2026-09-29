"""Les endpoints des médias (`api/media_router.py`) : la liste, puis son texte.

Ce qui se teste ici est ce qu'une réponse **heureuse** ne montre pas :

* un média **introuvable** répond 404 — pas une liste vide, qui laisserait croire
  à un média sans texte ;
* un média **sans texte** répond 200 : un média rejeté en revue a zéro morceau et
  reste parfaitement valide, c'est son verdict qui l'explique. Confondre les deux
  cas rendrait indiscernable ce que la revue cherchait justement à distinguer ;
* la lecture **ne demande jamais** l'`embedding` (768 flottants par morceau) : la
  projection part avec la requête, donc le vecteur ne quitte pas Postgres — le
  filtrer après coup ne réglerait que l'apparence ;
* la **liste** échoue (502) quand les comptes de morceaux sont illisibles : elle
  existe pour dire quel identifiant a du texte, et un `null` par ligne laisserait
  le premier client qui affiche `0` faire croire à un média vide ;
* et le compteur est **compté pour de vrai** : le dernier test de ce fichier
  traverse le routeur, `media_store` et `knowledge_index` sur la doublure
  partagée (`tests/supabase_double.py`), parce qu'un comptage tronqué à une page
  annoncerait « 2 » pour un média qui en a 5.

Les endpoints sont appelés **directement** (ce sont des fonctions), sans passer
par HTTP : l'authentification et l'enregistrement des routes sont tenus ailleurs,
dans `tests/test_api_auth_integration.py` et par le golden de surface HTTP
(`tests/goldens/api_routes.json`). Ce fichier ne mesure donc que le métier.

FastAPI est une dépendance de l'application, pas de ce module : dans un
environnement de développement minimal où il manque, les tests sont **sautés**
plutôt que de faire échouer la collecte — même règle que
`tests/test_api_auth_integration.py`.
"""
from __future__ import annotations

import unittest
from unittest import mock

try:
    from fastapi import HTTPException

    from api import media_router
    from database import knowledge_index, media_store
    from tests import supabase_double

    _FASTAPI_AVAILABLE = True
except Exception as exc:  # pragma: no cover - dépend de l'environnement
    _FASTAPI_AVAILABLE = False
    _IMPORT_ERROR = exc


def _row(asset=None, asset_source=None, review=None):
    """Une ligne `knowledge_media` réduite à ce que l'endpoint en lit."""
    metadata = {}
    if asset:
        metadata[media_store.ASSET_KEY] = (
            {"value": asset, "source": asset_source} if asset_source else asset
        )
    if review:
        metadata[media_store.REVIEW_KEY] = {"status": review}
    return {"id": "media-1", "metadata": metadata}


def _chunk(index, content, tokens=1):
    return {"chunk_index": index, "content": content, "token_count": tokens}


@unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI non installé")
class MediaTextTest(unittest.TestCase):
    """Le comportement de la route, doublure aux deux seules frontières de base."""

    def setUp(self) -> None:
        self.row = _row()
        self.chunks = []
        self.calls = []
        self._patch("get_media", lambda media_id: self.row)
        self._patch("list_media_chunks", self._list_chunks)

    def _patch(self, name, replacement):
        patcher = mock.patch.object(media_router, name, side_effect=replacement)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _list_chunks(self, media_id, **kwargs):
        """Doublure qui **retient** ce qu'on lui demande, projection comprise."""
        self.calls.append((media_id, kwargs))
        return list(self.chunks)

    def test_an_unknown_media_is_a_404(self):
        self.row = None
        with self.assertRaises(HTTPException) as caught:
            media_router.media_text("media-1")
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIn("media-1", caught.exception.detail)
        # Rien n'est lu dans l'index : un média inconnu n'a pas de morceaux à lire.
        self.assertEqual(self.calls, [])

    def test_the_chunks_are_returned_in_the_order_the_index_gives_them(self):
        self.chunks = [_chunk(0, "premier"), _chunk(1, "second")]
        body = media_router.media_text("media-1")
        self.assertEqual(body["chunks"], self.chunks)
        self.assertEqual(body["media_id"], "media-1")
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["chars"], len("premier") + len("second"))

    def test_the_projection_is_explicit(self):
        """Le routeur demande des colonnes **nommées** : `select("*")` ramène tout.

        C'est la première moitié du contrat ; la seconde — que cette projection
        traverse les trois maillons et atteigne Postgres — se prouve plus bas,
        sur la requête elle-même.
        """
        self.chunks = [_chunk(0, "texte")]
        media_router.media_text("media-1")
        self.assertEqual(len(self.calls), 1)
        _, kwargs = self.calls[0]
        columns = tuple(kwargs.get("columns") or ())
        self.assertTrue(columns, "une projection vide veut dire `select(\"*\")`")
        self.assertNotIn("*", columns, "le joker ramène l'`embedding` avec le reste")
        self.assertNotIn("embedding", columns, "le vecteur de 768 flottants ne doit pas être lu")
        self.assertIn("content", columns, "sans `content`, il n'y a plus de texte à relire")
        self.assertIn("chunk_index", columns, "sans l'index, l'ordre du document est perdu")

    def test_a_media_without_text_is_not_an_error(self):
        """Zéro morceau est une **réponse**, pas une panne.

        C'est le cas d'un média rejeté (`❌`) : le fichier est toujours là, le
        texte a été retiré de l'index. Le verdict dit lequel des deux cas on lit.
        """
        self.row = _row(review="rejected")
        self.chunks = []
        body = media_router.media_text("media-1")
        self.assertEqual(body["count"], 0)
        self.assertEqual(body["chars"], 0)
        self.assertEqual(body["chunks"], [])
        self.assertEqual(body["review_status"], "rejected")

    def test_a_media_never_reviewed_says_so_with_a_null_verdict(self):
        self.chunks = [_chunk(0, "texte")]
        self.assertIsNone(media_router.media_text("media-1")["review_status"])

    def test_the_validated_verdict_is_carried(self):
        self.row = _row(review="validated")
        self.chunks = [_chunk(0, "texte")]
        self.assertEqual(media_router.media_text("media-1")["review_status"], "validated")

    def test_the_asset_and_its_source_are_carried(self):
        """L'actif décide du filtre préférentiel de la recherche : il s'affiche."""
        self.row = _row(asset="BTC-USD", asset_source="caption")
        self.chunks = [_chunk(0, "zone d'achat")]
        body = media_router.media_text("media-1")
        self.assertEqual(body["asset"], "BTC-USD")
        self.assertEqual(body["asset_source"], "caption")

    def test_an_untagged_media_reports_no_asset(self):
        self.chunks = [_chunk(0, "texte")]
        body = media_router.media_text("media-1")
        self.assertIsNone(body["asset"])
        self.assertIsNone(body["asset_source"])

    def test_a_missing_content_does_not_break_the_count(self):
        """Une ligne sans `content` (colonne absente) ne doit pas lever."""
        self.chunks = [{"chunk_index": 0, "token_count": 3}]
        body = media_router.media_text("media-1")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["chars"], 0)


def _listing(**overrides):
    """Appelle `GET /media` avec des valeurs **résolues**.

    FastAPI n'est pas dans la boucle ici : appelée directement, la fonction
    recevrait ses marqueurs `Query(...)` en guise de défauts, et non des entiers.
    """
    options = {
        "limit": media_router.DEFAULT_PAGE,
        "offset": 0,
        "source": None,
        "media_type": None,
    }
    options.update(overrides)
    return media_router.media_listing(**options)


def _media_row(media_id="media-1", **overrides):
    """Une ligne `knowledge_media` telle que `list_media` la rend."""
    row = {
        "id": media_id,
        "file_name": f"{media_id}.jpg",
        "media_type": "photo",
        "file_size": 1024,
        "created_at": "2026-01-02T03:04:05+00:00",
        "source": "telegram",
        "chat_id": "@signals",
        "message_id": 42,
        "metadata": {},
    }
    row.update(overrides)
    return row


@unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI non installé")
class MediaListingTest(unittest.TestCase):
    """`GET /media` : ce qu'une liste doit dire pour qu'on choisisse une ligne.

    Les deux frontières de base (la table et l'index) sont doublées : ce qui est
    mesuré ici est la **charge** rendue, pas la lecture. Le dernier test du
    fichier, lui, ne double que le client Supabase.
    """

    def setUp(self) -> None:
        self.rows = [_media_row()]
        self.counts = {"media-1": 3}
        self.list_calls: list = []
        self.count_calls: list = []
        self.list_error = None
        self.count_error = None
        self._patch("list_media", self._list)
        self._patch("chunk_counts", self._counts)

    def _patch(self, name, replacement):
        patcher = mock.patch.object(media_router, name, side_effect=replacement)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _list(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.list_error:
            raise self.list_error
        return list(self.rows)

    def _counts(self, media_ids, **_kwargs):
        self.count_calls.append(list(media_ids))
        if self.count_error:
            raise self.count_error
        return {
            media_id: count
            for media_id, count in self.counts.items()
            if media_id in media_ids
        }

    # -- ce qu'une ligne porte --------------------------------------------- #

    def test_a_row_identifies_the_media_and_says_what_lies_behind(self):
        entry = _listing()["media"][0]

        self.assertEqual(entry["media_id"], "media-1")
        self.assertEqual(entry["file_name"], "media-1.jpg")
        self.assertEqual(entry["media_type"], "photo")
        self.assertEqual(entry["file_size"], 1024)
        self.assertEqual(entry["source"], "telegram")
        self.assertEqual(entry["chat_id"], "@signals")
        self.assertEqual(entry["chunks"], 3)

    def test_the_verdict_and_the_extraction_result_are_carried(self):
        """Les deux explications d'une ligne à zéro morceau : humain, machine."""
        self.rows = [
            _media_row(
                metadata={
                    media_store.REVIEW_KEY: {"status": "rejected"},
                    media_store.OUTCOME_KEY: {"ok": False, "reason": "vision absente"},
                }
            )
        ]
        entry = _listing()["media"][0]
        self.assertEqual(entry["review_status"], "rejected")
        self.assertFalse(entry["extracted"])

    def test_no_verdict_and_no_outcome_are_not_invented(self):
        entry = _listing()["media"][0]
        self.assertIsNone(entry["review_status"])
        self.assertIsNone(entry["extracted"], "sans note, on ne dit ni lu ni à rattraper")

    def test_the_asset_and_its_source_are_carried(self):
        self.rows = [
            _media_row(metadata={media_store.ASSET_KEY: {"value": "BTC-USD", "source": "caption"}})
        ]
        entry = _listing()["media"][0]
        self.assertEqual(entry["asset"], "BTC-USD")
        self.assertEqual(entry["asset_source"], "caption")

    def test_the_caption_never_leaves_the_base(self):
        """La charge est reconstruite champ par champ : pas de `**row`."""
        self.rows = [_media_row(caption="légende privée", telegram_file_id="AgAC-x")]
        self.assertNotIn("légende privée", str(_listing()))

    def test_a_media_without_text_stays_in_the_list(self):
        self.counts = {}
        body = _listing()
        self.assertEqual(len(body["media"]), 1)
        self.assertEqual(body["media"][0]["chunks"], 0)

    # -- ce qui est demandé à la base -------------------------------------- #

    def test_the_counts_are_asked_once_for_the_whole_page(self):
        """Une requête pour la page, pas une par média."""
        self.rows = [_media_row("media-1"), _media_row("media-2")]
        _listing()
        self.assertEqual(self.count_calls, [["media-1", "media-2"]])

    def test_an_empty_page_does_not_read_the_index(self):
        """Le routeur demande le comptage pour la page, **même sans identifiant** :
        c'est `chunk_counts` qui décide qu'il n'y a rien à lire (et ne lit rien),
        plutôt qu'une seconde garde ici qui finirait par diverger."""
        self.rows = []
        body = _listing()
        self.assertEqual(body["media"], [])
        self.assertEqual(self.count_calls, [[]])

    def test_the_page_asks_for_one_row_more_than_it_shows(self):
        """La ligne de trop dit qu'il en reste, sans compter la table."""
        _listing(limit=5, offset=10, source="telegram", media_type="photo")
        self.assertEqual(
            self.list_calls,
            [{"source": "telegram", "media_type": "photo", "limit": 6, "offset": 10}],
        )

    def test_a_full_page_announces_the_next_one(self):
        self.rows = [_media_row("media-1"), _media_row("media-2"), _media_row("media-3")]
        body = _listing(limit=2, offset=4)

        self.assertEqual(body["returned"], 2)
        self.assertEqual([entry["media_id"] for entry in body["media"]], ["media-1", "media-2"])
        self.assertTrue(body["truncated"])
        self.assertEqual(body["next_offset"], 6)

    def test_a_last_page_announces_nothing_after_it(self):
        body = _listing(limit=2)
        self.assertEqual(body["returned"], 1)
        self.assertFalse(body["truncated"])
        self.assertIsNone(body["next_offset"])

    def test_the_response_says_where_the_text_lives(self):
        self.assertEqual(_listing()["text"], "GET /media/{media_id}/text")

    # -- les pannes --------------------------------------------------------- #

    def test_an_unreadable_table_is_a_502(self):
        """Une liste vide dirait « aucun média » : c'est la panne, pas la réponse."""
        self.list_error = RuntimeError("db down")
        with self.assertRaises(HTTPException) as caught:
            _listing()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("db down", caught.exception.detail)

    def test_an_unreadable_index_is_a_502(self):
        """Sans les comptes, la liste ne répond plus à sa propre question."""
        self.count_error = RuntimeError("index down")
        with self.assertRaises(HTTPException) as caught:
            _listing()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("index down", caught.exception.detail)


@unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI non installé")
class ProjectionReachesTheDatabaseTest(unittest.TestCase):
    """Ce que la **requête** demande à Postgres — les trois maillons, pour de vrai.

    Vérifier la constante du routeur ne suffit pas : les deux maillons qui la
    transportent (la délégation média, puis la construction de `select`) peuvent
    la perdre ou la remplacer par un joker sans que rien ne le dise, et un
    `("*",)` passerait même le contrôle des colonnes nommées. Ici, seul le
    client Supabase est doublé : routeur, `media_store` et `knowledge_index`
    sont le code réel, et c'est la chaîne envoyée à la base qui est lue.
    """

    def setUp(self) -> None:
        self.rows = [_chunk(0, "premier")]
        self.client = supabase_double.SupabaseDouble()
        # La lecture filtre sur `media_id`, donc la ligne semée le porte — et la
        # réponse ne le rend pas : la projection demandée ne le nomme pas.
        self.client.store(knowledge_index.TABLE).rows = [
            {"media_id": "media-1", **row} for row in self.rows
        ]
        supabase_double.use_supabase(self, self.client, knowledge_index)
        media_patcher = mock.patch.object(media_router, "get_media", lambda media_id: _row())
        media_patcher.start()
        self.addCleanup(media_patcher.stop)

    def selections(self):
        """Les projections demandées, dans l'ordre où elles sont parties."""
        return [entry[1][0] for entry in self.client.operations("select")]

    def test_the_database_is_asked_for_the_text_only(self):
        body = media_router.media_text("media-1")
        self.assertEqual(
            self.selections(),
            ["chunk_index,content,token_count"],
            "la requête doit nommer les colonnes lues, jamais les ramener toutes",
        )
        self.assertEqual(body["chunks"], self.rows)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["chars"], len("premier"))

    def test_the_embedding_cannot_slip_into_the_payload(self):
        """La preuve par la réponse : aucune colonne lue ne porte de vecteur."""
        body = media_router.media_text("media-1")
        self.assertNotIn("embedding", "".join(self.selections()))
        self.assertNotIn("embedding", str(body["chunks"]))


@unittest.skipUnless(_FASTAPI_AVAILABLE, "FastAPI non installé")
class ListingReachesTheDatabaseTest(unittest.TestCase):
    """La liste, bout en bout : routeur, `media_store` et `knowledge_index` réels.

    Seul le client Supabase est doublé, donc ce qui se prouve ici est ce qu'une
    doublure de frontière ne peut pas montrer : que la lecture des morceaux est
    **paginée**. Un comptage qui ne lirait qu'une page annoncerait « 2 morceaux »
    pour un média qui en a 5 — le tableau de bord croirait avoir tout lu, et
    relirait un texte tronqué.
    """

    def setUp(self) -> None:
        self.client = supabase_double.SupabaseDouble()
        self.media = self.client.store(media_store.TABLE)
        self.chunks = self.client.store(knowledge_index.TABLE)
        supabase_double.use_supabase(self, self.client, media_store, knowledge_index)

    def _seed(self, media_id, chunks, *, created_at="2026-01-01T00:00:00+00:00"):
        self.media.rows.append(
            {
                "id": media_id,
                "file_name": f"{media_id}.jpg",
                "created_at": created_at,
                "metadata": {},
            }
        )
        for index in range(chunks):
            self.chunks.rows.append({"media_id": media_id, "chunk_index": index})

    def test_the_count_is_the_number_of_chunks_really_stored(self):
        self._seed("m1", 3)
        self._seed("m2", 0)
        body = _listing()

        self.assertEqual(
            {entry["media_id"]: entry["chunks"] for entry in body["media"]},
            {"m1": 3, "m2": 0},
        )

    def test_a_count_spanning_several_pages_is_not_truncated(self):
        """Cinq morceaux lus par pages de deux : le compte reste cinq."""
        self._seed("m1", 5)
        with mock.patch.object(knowledge_index, "COUNT_PAGE", 2):
            body = _listing()

        self.assertEqual(body["media"][0]["chunks"], 5)
        # 2 + 2 + 1 : une page courte signale la fin du comptage.
        self.assertEqual(len(self.chunks.operations("range")), 3)

    def test_the_page_follows_the_most_recent_media(self):
        self._seed("ancien", 1, created_at="2026-01-01T00:00:00+00:00")
        self._seed("recent", 1, created_at="2026-02-01T00:00:00+00:00")
        body = _listing(limit=1)

        self.assertEqual([entry["media_id"] for entry in body["media"]], ["recent"])
        self.assertTrue(body["truncated"])

        second = _listing(limit=1, offset=body["next_offset"])
        self.assertEqual([entry["media_id"] for entry in second["media"]], ["ancien"])
        self.assertFalse(second["truncated"], "la seconde page est la dernière")


if __name__ == "__main__":
    unittest.main()
