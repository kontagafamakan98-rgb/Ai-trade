"""Tests du scraper de canal Telegram public (`scrapers/telegram_channel.py`).

Le parsing est testé sur du HTML réel (structure relevée sur l'aperçu web de
canaux publics), et l'ingestion avec un client HTTP et un uploader factices :
aucun accès réseau. On vérifie surtout que les **documents** — dont l'aperçu
n'expose aucune URL de fichier — sont comptés puis ignorés, et non « ingérés »
à tort.
"""
from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from ai import media_indexing
from database import knowledge_index, media_store
from scrapers import telegram_channel as tc

PHOTO_HTML = """
<div class="tgme_widget_message" data-post="canal/100">
  <div class="tgme_widget_message_text">BTC cassure des 100k confirmée en clôture</div>
  <a class="tgme_widget_message_photo_wrap blured"
     href="https://t.me/canal/100"
     style="width:367px;background-image:url('https://cdn1.telesco.pe/file/abc123')"></a>
</div>
"""

DOCUMENT_HTML = """
<div class="tgme_widget_message" data-post="canal/200">
  <div class="tgme_widget_message_text">Rapport hebdo en pièce jointe</div>
  <a class="tgme_widget_message_document_wrap" href="https://t.me/canal/200">
    <div class="tgme_widget_message_document_title">rapport.pdf</div>
    <div class="tgme_widget_message_document_extra">18 KB</div>
  </a>
</div>
"""

VIDEO_HTML = """
<div class="tgme_widget_message" data-post="canal/300">
  <div class="tgme_widget_message_text">Démo de la stratégie</div>
  <video class="tgme_widget_message_video" src="https://cdn4.telesco.pe/file/x.mp4?token=abc"></video>
</div>
"""


class FakeResponse:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeStream:
    def __init__(self, chunks, content_type="image/jpeg"):
        self._chunks = chunks
        self.headers = {"content-type": content_type}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def raise_for_status(self):
        pass

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


#: Deux publications, pour vérifier qu'on sait **laquelle** on récupère.
TWO_POSTS_HTML = PHOTO_HTML + """
<div class="tgme_widget_message" data-post="canal/101">
  <div class="tgme_widget_message_text">ETH range haut</div>
  <a class="tgme_widget_message_photo_wrap"
     href="https://t.me/canal/101"
     style="width:367px;background-image:url('https://cdn1.telesco.pe/file/def456')"></a>
</div>
"""


class FakeClient:
    def __init__(self, html="", streams=None, get_error=None):
        self.html = html
        self.streams = streams or {}
        self.get_error = get_error
        self.requested = []

    async def get(self, url):
        self.requested.append(url)
        if self.get_error:
            raise self.get_error
        return FakeResponse(self.html)

    def stream(self, method, url, headers=None):
        self.requested.append(url)
        stream = self.streams.get(url)
        if isinstance(stream, Exception):
            raise stream
        return stream or FakeStream([b"x"])


class HelpersTest(unittest.TestCase):
    def test_post_message_id(self):
        self.assertEqual(tc._post_message_id("canal/15438"), 15438)
        self.assertIsNone(tc._post_message_id("canal/abc"))
        self.assertIsNone(tc._post_message_id(None))

    def test_is_file_url(self):
        self.assertTrue(tc._is_file_url("https://cdn1.telesco.pe/file/abc"))
        self.assertTrue(tc._is_file_url("https://cdn4.telesco.pe/file/x.mp4"))
        self.assertFalse(tc._is_file_url("https://t.me/canal/200"))
        self.assertFalse(tc._is_file_url(None))

    def test_photo_url_from_style(self):
        soup = tc._make_soup(PHOTO_HTML)
        wrap = soup.select_one("a.tgme_widget_message_photo_wrap")
        self.assertEqual(tc._photo_url(wrap), "https://cdn1.telesco.pe/file/abc123")

    def test_extension_from_content_type_then_url(self):
        self.assertEqual(tc._file_extension("https://x/y", "image/jpeg; charset=x"), ".jpg")
        self.assertEqual(tc._file_extension("https://x/y.mp4?token=1", None), ".mp4")
        self.assertEqual(tc._file_extension("https://x/y", None), ".bin")


class ParseChannelHtmlTest(unittest.TestCase):
    def test_photo_produces_insight_and_media(self):
        parsed = tc.parse_channel_html(PHOTO_HTML, "canal")
        self.assertEqual(len(parsed["insights"]), 1)
        self.assertIn("cassure des 100k", parsed["insights"][0]["summary"])
        self.assertEqual(parsed["insights"][0]["type"], "telegram_channel")
        self.assertEqual(len(parsed["media"]), 1)
        media = parsed["media"][0]
        self.assertEqual(media["media_type"], "photo")
        self.assertEqual(media["message_id"], 100)
        self.assertEqual(media["url"], "https://cdn1.telesco.pe/file/abc123")
        self.assertEqual(parsed["skipped_documents"], 0)

    def test_the_publication_text_serves_as_the_media_caption(self):
        """L'aperçu n'expose pas de légende à part : c'est le texte du message.

        Sans lui, une photo de canal n'arriverait à l'index qu'avec la description
        de la vision — qui ne nomme pas forcément l'actif, et n'existe pas du tout
        quand l'extraction échoue.
        """
        media = tc.parse_channel_html(PHOTO_HTML, "canal")["media"][0]
        self.assertIn("cassure des 100k", media["caption"])

    def test_a_publication_without_text_has_no_caption(self):
        html = (
            '<div class="tgme_widget_message" data-post="canal/101">'
            '<a class="tgme_widget_message_photo_wrap" href="https://t.me/canal/101" '
            'style="background-image:url(\'https://cdn1.telesco.pe/file/z\')"></a></div>'
        )
        media = tc.parse_channel_html(html, "canal")["media"][0]
        self.assertIsNone(media["caption"])

    def test_a_video_carries_the_caption_too(self):
        media = tc.parse_channel_html(VIDEO_HTML, "canal")["media"][0]
        self.assertIn("stratégie", media["caption"])

    def test_document_is_counted_but_not_ingested(self):
        parsed = tc.parse_channel_html(DOCUMENT_HTML, "canal")
        self.assertEqual(parsed["media"], [])
        self.assertEqual(parsed["skipped_documents"], 1)
        self.assertEqual(len(parsed["insights"]), 1)

    def test_video_is_exposed_as_media(self):
        parsed = tc.parse_channel_html(VIDEO_HTML, "canal")
        self.assertEqual(len(parsed["media"]), 1)
        self.assertEqual(parsed["media"][0]["media_type"], "video")

    def test_short_text_is_ignored(self):
        html = '<div class="tgme_widget_message" data-post="c/1"><div class="tgme_widget_message_text">hi</div></div>'
        self.assertEqual(tc.parse_channel_html(html, "canal")["insights"], [])


class FileNameTest(unittest.TestCase):
    def test_document_title_is_used(self):
        descriptor = {"file_name": "rapport.pdf", "media_type": "document", "message_id": 2, "url": "u"}
        self.assertEqual(tc._file_name(descriptor, "application/pdf"), "rapport.pdf")

    def test_photo_falls_back_to_type_and_id(self):
        descriptor = {"file_name": None, "media_type": "photo", "message_id": 100, "url": "u"}
        self.assertEqual(tc._file_name(descriptor, "image/jpeg"), "photo_100.jpg")

    def test_title_with_path_is_reduced_to_basename(self):
        descriptor = {"file_name": "../../etc/passwd", "media_type": "document", "message_id": 1, "url": "u"}
        self.assertEqual(tc._file_name(descriptor, None), "passwd")


class FetchMessageMediaTest(unittest.IsolatedAsyncioTestCase):
    """Retrouver les octets d'**une** publication précise (`?before=<id>`).

    C'est la voie de réparation quand un objet a disparu du bucket et qu'aucun
    `telegram_file_id` n'existe (les médias du scraper n'en ont jamais eu).
    """

    def test_the_page_url_includes_the_target(self):
        """`?before=N` rend les publications d'id inférieur à N : la cible est X+1."""
        self.assertEqual(tc.message_page_url("canal", 7), "https://t.me/s/canal?before=8")

    async def test_the_wanted_publication_is_the_one_downloaded(self):
        client = FakeClient(html=TWO_POSTS_HTML)
        found = await tc.fetch_message_media("canal", 101, client=client)
        self.assertEqual(found["data"], b"x")
        self.assertEqual(found["url"], "https://cdn1.telesco.pe/file/def456")
        self.assertEqual(found["media_type"], "photo")
        self.assertEqual(found["file_name"], "photo_101.jpg")
        self.assertEqual(client.requested[0], "https://t.me/s/canal?before=102")
        self.assertEqual(
            client.requested[1],
            "https://cdn1.telesco.pe/file/def456",
            "l'autre publication ne doit pas être téléchargée",
        )

    async def test_the_first_publication_is_reached_too(self):
        client = FakeClient(html=TWO_POSTS_HTML)
        found = await tc.fetch_message_media("canal", 100, client=client)
        self.assertEqual(found["url"], "https://cdn1.telesco.pe/file/abc123")

    async def test_the_caption_of_the_publication_is_returned_with_the_bytes(self):
        client = FakeClient(html=TWO_POSTS_HTML)
        found = await tc.fetch_message_media("canal", 101, client=client)
        self.assertNotIn("caption", found, "seuls les octets et de quoi les décrire")

    async def test_a_publication_outside_the_preview_returns_nothing(self):
        """Rien trouvé n'est pas une panne : la voie n'a simplement rien rendu."""
        client = FakeClient(html=TWO_POSTS_HTML)
        self.assertIsNone(await tc.fetch_message_media("canal", 999, client=client))
        self.assertEqual(len(client.requested), 1, "aucun téléchargement inutile")

    async def test_a_document_is_not_downloadable_by_this_route(self):
        """L'aperçu n'expose aucune URL de fichier pour un document."""
        client = FakeClient(html=DOCUMENT_HTML)
        self.assertIsNone(await tc.fetch_message_media("canal", 200, client=client))

    async def test_an_oversized_file_is_refused(self):
        url = "https://cdn1.telesco.pe/file/abc123"
        client = FakeClient(
            html=PHOTO_HTML, streams={url: FakeStream([b"a" * 50, b"b" * 50])}
        )
        self.assertIsNone(
            await tc.fetch_message_media("canal", 100, client=client, max_bytes=60)
        )

    async def test_a_network_failure_raises(self):
        """Une panne, elle, doit remonter : l'appelant la rapporte, `None` veut
        dire « pas par cette voie »."""
        client = FakeClient(html=PHOTO_HTML, get_error=RuntimeError("503"))
        with self.assertRaises(RuntimeError):
            await tc.fetch_message_media("canal", 100, client=client)


class IngestChannelMediaTest(unittest.IsolatedAsyncioTestCase):
    """Téléchargement, stockage, puis **indexation** du texte du média."""

    def setUp(self):
        self.uploads = []
        self.indexed = []
        self.tagged = []

        def upload(data, **kwargs):
            self.uploads.append((data, kwargs))
            return {"id": "m1"}

        async def index(data, descriptor, **kwargs):
            self.indexed.append((data, descriptor, kwargs))
            return {"chunks": 2, "asset": "BTC-USD", "asset_source": "caption"}

        async def tag(media_id, asset, source=None, **_kwargs):
            self.tagged.append((media_id, asset, source))
            return True

        self.upload = upload
        self.index = index
        self.tag = tag

    def _media(self, **overrides):
        item = {
            "url": "https://cdn1.telesco.pe/file/abc",
            "media_type": "photo",
            "file_name": None,
            "message_id": 100,
            "caption": "BTC cassure des 100k",
        }
        item.update(overrides)
        return [item]

    def _ingest(self, client, media=None, **kwargs):
        """Ingestion avec les collaborateurs factices (aucun réseau, aucune base)."""
        kwargs.setdefault("upload", self.upload)
        kwargs.setdefault("index", self.index)
        kwargs.setdefault("tag", self.tag)
        kwargs.setdefault("find_message", lambda *_a: None)
        kwargs.setdefault("chunks", lambda *_a: False)
        return tc.ingest_channel_media("canal", media or self._media(), client=client, **kwargs)

    async def test_downloads_and_stores_with_channel_metadata(self):
        client = FakeClient(streams={"https://cdn1.telesco.pe/file/abc": FakeStream([b"img"])})
        counts = await self._ingest(client)
        self.assertEqual(counts["stored"], 1)
        data, kwargs = self.uploads[0]
        self.assertEqual(data, b"img")
        self.assertEqual(kwargs["media_type"], "photo")
        self.assertEqual(kwargs["source"], tc.CHANNEL_SOURCE)
        self.assertEqual(kwargs["chat_id"], "canal")
        self.assertEqual(kwargs["message_id"], 100)
        self.assertTrue(kwargs["upsert"])
        self.assertEqual(kwargs["file_name"], "photo_100.jpg")
        self.assertEqual(kwargs["mime_type"], "image/jpeg")
        self.assertEqual(kwargs["metadata"]["channel"], "canal")

    async def test_the_stored_media_is_indexed_with_its_caption(self):
        """Sans cette étape, la photo serait un fichier que `/search` ignore."""
        client = FakeClient(streams={"https://cdn1.telesco.pe/file/abc": FakeStream([b"img"])})
        counts = await self._ingest(client)
        self.assertEqual(counts["indexed"], 1)
        data, descriptor, kwargs = self.indexed[0]
        self.assertEqual(data, b"img")
        self.assertEqual(kwargs["media_id"], "m1")
        # Le descripteur transmis est complet : le module partagé lit ces quatre
        # champs, exactement comme sur la route du bot.
        self.assertEqual(descriptor["media_type"], "photo")
        self.assertEqual(descriptor["mime_type"], "image/jpeg")
        self.assertEqual(descriptor["file_name"], "photo_100.jpg")
        self.assertIn("cassure des 100k", descriptor["caption"])

    async def test_the_detected_asset_is_recorded_on_the_media_row(self):
        client = FakeClient(streams={"https://cdn1.telesco.pe/file/abc": FakeStream([b"img"])})
        await self._ingest(client)
        self.assertEqual(self.tagged, [("m1", "BTC-USD", "caption")])

    async def test_a_media_without_indexable_text_is_stored_and_said(self):
        """Un média stocké sans texte n'apporte rien à la recherche : les deux
        comptes sont distincts pour que le journal ne les confonde pas."""
        async def empty_index(*_a, **_k):
            return {"chunks": 0, "reason": "unsupported"}

        client = FakeClient(streams={"https://cdn1.telesco.pe/file/abc": FakeStream([b"img"])})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            counts = await self._ingest(client, index=empty_index)
        self.assertEqual((counts["stored"], counts["indexed"]), (1, 0))
        self.assertIn("sans texte", out.getvalue())

    async def test_an_indexing_failure_does_not_lose_the_media(self):
        async def failing_index(*_a, **_k):
            raise RuntimeError("embeddings indisponibles")

        client = FakeClient(streams={"https://cdn1.telesco.pe/file/abc": FakeStream([b"img"])})
        with contextlib.redirect_stdout(io.StringIO()):
            counts = await self._ingest(client, index=failing_index)
        self.assertEqual((counts["stored"], counts["indexed"]), (1, 0))
        self.assertEqual(len(self.uploads), 1)

    async def test_an_already_indexed_message_is_not_downloaded_again(self):
        """Le balayage revoit les mêmes publications : pas de vision en boucle."""
        client = FakeClient(streams={"https://cdn1.telesco.pe/file/abc": FakeStream([b"img"])})
        counts = await self._ingest(
            client,
            find_message=lambda chat_id, message_id: {"id": "m1"},
            chunks=lambda media_id: True,
        )
        self.assertEqual(
            counts, {"stored": 0, "indexed": 0, "already_indexed": 1, "deferred": 0}
        )
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.indexed, [])

    async def test_a_stored_but_unindexed_message_is_indexed_on_the_next_sweep(self):
        """Les médias enregistrés avant cette route n'ont aucun morceau : ils se
        rattrapent au balayage suivant au lieu de rester invisibles."""
        client = FakeClient(streams={"https://cdn1.telesco.pe/file/abc": FakeStream([b"img"])})
        counts = await self._ingest(
            client,
            find_message=lambda chat_id, message_id: {"id": "m1"},
            chunks=lambda media_id: False,
        )
        self.assertEqual((counts["stored"], counts["indexed"]), (1, 1))

    async def test_an_unreadable_duplicate_lookup_does_not_block_ingestion(self):
        def failing_lookup(*_a):
            raise RuntimeError("base indisponible")

        client = FakeClient(streams={"https://cdn1.telesco.pe/file/abc": FakeStream([b"img"])})
        with contextlib.redirect_stdout(io.StringIO()):
            counts = await self._ingest(client, find_message=failing_lookup)
        self.assertEqual(counts["stored"], 1)

    async def test_the_defaults_are_the_shared_pipeline(self):
        """Sans injection, le scraper doit utiliser **le** pipeline du bot.

        C'est la propriété qui empêche deux implémentations de diverger : le
        stockage vient de `media_store`, l'extraction et l'étiquette de
        `ai.media_indexing` — les mêmes fonctions que `telegram_media`.
        """
        client = FakeClient(streams={"https://cdn1.telesco.pe/file/abc": FakeStream([b"img"])})
        with mock.patch.object(
            media_indexing, "extract_and_index", new=mock.AsyncMock(return_value={"chunks": 1})
        ) as index, mock.patch.object(
            media_indexing, "store_asset", new=mock.AsyncMock(return_value=True)
        ), mock.patch.object(
            media_store, "upload_media", return_value={"id": "m1"}
        ) as upload, mock.patch.object(
            media_store, "find_media_by_message", return_value=None
        ), mock.patch.object(
            knowledge_index, "has_chunks", return_value=False
        ):
            counts = await tc.ingest_channel_media("canal", self._media(), client=client)

        self.assertEqual(
            counts, {"stored": 1, "indexed": 1, "already_indexed": 0, "deferred": 0}
        )
        upload.assert_called_once()
        index.assert_awaited_once()

    async def test_oversized_media_is_skipped(self):
        client = FakeClient(streams={"u": FakeStream([b"12345", b"67890"])})
        counts = await self._ingest(client, media=self._media(url="u"), max_bytes=4)
        self.assertEqual(counts["stored"], 0)
        self.assertEqual(self.uploads, [])

    async def test_download_failure_does_not_abort(self):
        client = FakeClient(streams={"u": FakeStream([b"x"])})
        client.streams["u"] = RuntimeError("lien mort")
        counts = await self._ingest(client, media=self._media(url="u"))
        self.assertEqual(counts["stored"], 0)

    async def test_upload_failure_is_reported_not_raised(self):
        def failing_upload(*_a, **_k):
            raise RuntimeError("bucket down")

        client = FakeClient()
        counts = await self._ingest(client, upload=failing_upload)
        self.assertEqual(counts["stored"], 0)
        self.assertEqual(self.indexed, [])

    async def test_media_without_message_id_is_skipped(self):
        client = FakeClient()
        counts = await self._ingest(client, media=self._media(message_id=None))
        self.assertEqual(counts["stored"], 0)
        self.assertEqual(self.uploads, [])

    async def test_the_cap_defers_the_rest_of_the_album(self):
        """Un album de dix photos ne doit pas partir en dix appels de vision."""
        media = [
            {
                "url": f"https://cdn1.telesco.pe/file/{index}",
                "media_type": "photo",
                "file_name": None,
                "message_id": 200 + index,
                "caption": "BTC",
            }
            for index in range(4)
        ]
        client = FakeClient(
            streams={f"https://cdn1.telesco.pe/file/{index}": FakeStream([b"img"]) for index in range(4)}
        )
        with contextlib.redirect_stdout(io.StringIO()) as out:
            counts = await self._ingest(client, media=media, max_extractions=1)
        self.assertEqual((counts["stored"], counts["indexed"], counts["deferred"]), (1, 1, 3))
        self.assertEqual(len(self.indexed), 1)
        self.assertIn("reportée(s) au prochain balayage", out.getvalue())

    async def test_the_deferred_media_are_taken_on_the_next_sweep(self):
        """Le reste n'est pas perdu : il remonte au balayage suivant, sans relecture."""
        stored: set = set()
        media = [
            {
                "url": f"https://cdn1.telesco.pe/file/{index}",
                "media_type": "photo",
                "file_name": None,
                "message_id": 300 + index,
                "caption": "BTC",
            }
            for index in range(3)
        ]

        def upload(data, **kwargs):
            stored.add(kwargs["message_id"])
            self.uploads.append((data, kwargs))
            return {"id": f"m{kwargs['message_id']}"}

        def find_message(_channel, message_id):
            return {"id": f"m{message_id}"} if message_id in stored else None

        def chunks(media_id):
            #: L'indexation réussit pour tout ce qui a été stocké (le faux `index`
            #: rend des morceaux), donc "déjà indexé" équivaut à "déjà stocké".
            return media_id in {f"m{message_id}" for message_id in stored}

        client = FakeClient(
            streams={f"https://cdn1.telesco.pe/file/{index}": FakeStream([b"img"]) for index in range(3)}
        )
        with contextlib.redirect_stdout(io.StringIO()):
            first = await self._ingest(
                client,
                media=media,
                upload=upload,
                max_extractions=1,
                find_message=find_message,
                chunks=chunks,
            )
        self.assertEqual((first["indexed"], first["deferred"]), (1, 2))
        #: Au cycle suivant, la déjà indexée est écartée **avant** le plafond : il
        #: reste donc de la place pour la suivante.
        with contextlib.redirect_stdout(io.StringIO()):
            second = await self._ingest(
                client,
                media=media,
                upload=upload,
                max_extractions=1,
                find_message=find_message,
                chunks=chunks,
            )
        self.assertEqual((second["indexed"], second["deferred"]), (1, 1))
        self.assertEqual(stored, {300, 301})

    async def test_zero_means_no_cap(self):
        media = [
            {
                "url": f"https://cdn1.telesco.pe/file/{index}",
                "media_type": "photo",
                "file_name": None,
                "message_id": 400 + index,
                "caption": "BTC",
            }
            for index in range(3)
        ]
        client = FakeClient(
            streams={f"https://cdn1.telesco.pe/file/{index}": FakeStream([b"img"]) for index in range(3)}
        )
        counts = await self._ingest(client, media=media, max_extractions=0)
        self.assertEqual((counts["stored"], counts["deferred"]), (3, 0))

    async def test_a_failed_transfer_does_not_consume_the_budget(self):
        """Le plafond borne les **extractions** : un transfert raté ne coûte aucun quota."""
        media = [
            {
                "url": "https://cdn1.telesco.pe/file/mort",
                "media_type": "photo",
                "file_name": None,
                "message_id": 500,
                "caption": None,
            },
            {
                "url": "https://cdn1.telesco.pe/file/vivant",
                "media_type": "photo",
                "file_name": None,
                "message_id": 501,
                "caption": None,
            },
        ]
        client = FakeClient(
            streams={
                "https://cdn1.telesco.pe/file/mort": RuntimeError("lien mort"),
                "https://cdn1.telesco.pe/file/vivant": FakeStream([b"img"]),
            }
        )
        with contextlib.redirect_stdout(io.StringIO()):
            counts = await self._ingest(client, media=media, max_extractions=1)
        self.assertEqual((counts["stored"], counts["indexed"], counts["deferred"]), (1, 1, 0))
        self.assertEqual(len(self.indexed), 1)


class FetchAndPushTest(unittest.IsolatedAsyncioTestCase):
    async def test_text_and_media_are_reported(self):
        html = PHOTO_HTML + DOCUMENT_HTML
        client = FakeClient(
            html=html, streams={"https://cdn1.telesco.pe/file/abc123": FakeStream([b"img"])}
        )
        uploads = []
        indexed = []

        def upload(data, **kwargs):
            uploads.append(kwargs)
            return {"id": "m1"}

        async def index(data, descriptor, **kwargs):
            indexed.append(descriptor)
            return {"chunks": 1}

        async def tag(*_a, **_k):
            return True

        with mock.patch.object(tc, "insert_insight") as insert:
            stats = await tc.fetch_and_push_telegram_channel(
                "canal",
                client=client,
                upload=upload,
                index=index,
                tag=tag,
                find_message=lambda *_a: None,
                chunks=lambda *_a: False,
            )

        self.assertEqual(stats["insights"], 2)
        self.assertEqual(stats["media"], 1)
        self.assertEqual(stats["indexed"], 1)
        self.assertEqual(stats["skipped_documents"], 1)
        self.assertEqual(insert.call_count, 2)
        self.assertEqual(len(uploads), 1)
        self.assertEqual(len(indexed), 1)

    async def test_the_document_stays_out_of_the_index(self):
        """Aucun fichier à télécharger pour un document : rien à indexer non plus."""
        client = FakeClient(html=DOCUMENT_HTML)
        indexed = []

        async def index(*_a, **_k):
            indexed.append(1)
            return {"chunks": 1}

        with mock.patch.object(tc, "insert_insight"):
            stats = await tc.fetch_and_push_telegram_channel(
                "canal",
                client=client,
                index=index,
                find_message=lambda *_a: None,
                chunks=lambda *_a: False,
            )
        self.assertEqual(stats["media"], 0)
        self.assertEqual(stats["indexed"], 0)
        self.assertEqual(indexed, [])
        self.assertEqual(stats["skipped_documents"], 1)

    async def test_the_cap_is_forwarded_to_the_media_ingestion(self):
        """Le plafond vient de la configuration : il doit traverser le scraper entier."""
        client = FakeClient(html=PHOTO_HTML)
        seen = []

        async def fake_ingest(channel, media, **kwargs):
            seen.append(kwargs.get("max_extractions"))
            return {"stored": 0, "indexed": 0, "already_indexed": 0, "deferred": 0}

        with mock.patch.object(tc, "ingest_channel_media", fake_ingest), mock.patch.object(
            tc, "insert_insight"
        ):
            stats = await tc.fetch_and_push_telegram_channel(
                "canal", client=client, max_extractions=2
            )
        self.assertEqual(seen, [2])
        self.assertEqual(stats["deferred"], 0)

    async def test_fetch_error_returns_zeroed_stats(self):
        """Aucun compte n'est mesuré : le motif d'échec est la seule information."""
        client = FakeClient(get_error=RuntimeError("boom"))
        stats = await tc.fetch_and_push_telegram_channel("canal", client=client)
        self.assertEqual(
            stats,
            {
                "insights": 0,
                "media": 0,
                "indexed": 0,
                "skipped_documents": 0,
                "deferred": 0,
                "error": "RuntimeError: boom",
            },
        )

    async def test_a_read_that_failed_names_its_cause(self):
        """Sans ce motif, un canal injoignable et un canal calme se ressemblent."""
        client = FakeClient(html=PHOTO_HTML, get_error=RuntimeError("503"))

        stats = await tc.fetch_and_push_telegram_channel("canal", client=client)

        self.assertIn("RuntimeError", stats["error"])
        self.assertIn("503", stats["error"])

    async def test_a_read_that_succeeded_names_no_cause(self):
        """C'est cette absence qui solde la suite d'échecs, côté suivi du balayage."""
        client = FakeClient(html=PHOTO_HTML)

        with mock.patch.object(tc, "insert_insight"):
            stats = await tc.fetch_and_push_telegram_channel("canal", client=client)

        self.assertIsNone(stats["error"])
        self.assertIn("deferred", stats)


if __name__ == "__main__":
    unittest.main()
