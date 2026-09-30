"""Tests de l'ingestion des médias Telegram (sans `python-telegram-bot`).

`notifications/telegram_media.py` ne manipule que des objets duck-typés et des
fonctions injectables : on peut donc tester l'extraction, l'orchestration du
téléchargement et l'appel à `media_store` avec de simples `SimpleNamespace` et
des faux `download`/`upload`.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import pathlib
import unittest
from types import SimpleNamespace
from unittest import mock

from notifications import telegram_media


def _called_name(call: ast.Call) -> str:
    """Nom appelé par un nœud `ast.Call` (`f()` ou `obj.f()`), sinon chaîne vide.

    Sert aux contrats qui lisent l'**arbre** d'un module : chercher un nom dans le
    texte confond `review` avec `preview` et `bulk_preview`.
    """
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _message(**overrides):
    base = {"message_id": 42, "chat_id": 12345, "caption": None}
    base.update(overrides)
    return SimpleNamespace(**base)


def _recorder(calls=None):
    """Doublure du **notaire** du pipeline : elle n'écrit rien, mais retient ce qu'elle voit.

    `extract_and_index` note le résultat de l'extraction sur la ligne média ; les
    tests qui injectent `store` doivent donc aussi injecter celle-là, sinon la
    vraie fonction part vers un Supabase absent et la suite se remplit d'un
    avertissement qui noie les vraies erreurs.
    """

    def record(media_id, summary):
        if calls is not None:
            calls.append({"media_id": media_id, "summary": summary})
        return {}

    return record


class ExtractMediaTest(unittest.TestCase):
    def test_photo_uses_largest_resolution(self):
        message = _message(
            photo=[
                SimpleNamespace(file_id="small", file_size=100, width=90, height=60),
                SimpleNamespace(file_id="large", file_size=900, width=1280, height=720),
            ]
        )
        descriptor = telegram_media.extract_media(message)
        self.assertEqual(descriptor["media_type"], "photo")
        self.assertEqual(descriptor["file_id"], "large")
        self.assertEqual(descriptor["mime_type"], "image/jpeg")
        self.assertEqual((descriptor["width"], descriptor["height"]), (1280, 720))

    def test_video_keeps_metadata(self):
        message = _message(
            video=SimpleNamespace(
                file_id="v1",
                file_size=2000,
                file_name="clip.mp4",
                mime_type="video/mp4",
                width=640,
                height=480,
                duration=12,
            )
        )
        descriptor = telegram_media.extract_media(message)
        self.assertEqual(descriptor["media_type"], "video")
        self.assertEqual(descriptor["file_name"], "clip.mp4")
        self.assertEqual(descriptor["duration"], 12)

    def test_document_defaults_name_and_mime(self):
        message = _message(document=SimpleNamespace(file_id="d1", file_size=10))
        descriptor = telegram_media.extract_media(message)
        self.assertEqual(descriptor["media_type"], "document")
        self.assertEqual(descriptor["file_name"], "document_42.bin")
        self.assertEqual(descriptor["mime_type"], "application/octet-stream")

    def test_voice_defaults_to_ogg(self):
        message = _message(voice=SimpleNamespace(file_id="vo", file_size=5, duration=3))
        descriptor = telegram_media.extract_media(message)
        self.assertEqual(descriptor["media_type"], "voice")
        self.assertEqual(descriptor["file_name"], "voice_42.ogg")
        self.assertEqual(descriptor["mime_type"], "audio/ogg")

    def test_caption_is_carried(self):
        message = _message(
            caption="BTC support",
            photo=[SimpleNamespace(file_id="p", file_size=1, width=1, height=1)],
        )
        self.assertEqual(telegram_media.extract_media(message)["caption"], "BTC support")

    def test_audio_defaults_to_mp3(self):
        # Telegram réserve `audio` aux fichiers joints (mp3, m4a…) ; sans ce cas,
        # un mp3 envoyé par l'utilisateur serait silencieusement ignoré.
        message = _message(audio=SimpleNamespace(file_id="au", file_size=7, duration=9))
        descriptor = telegram_media.extract_media(message)
        self.assertEqual(descriptor["media_type"], "audio")
        self.assertEqual(descriptor["file_name"], "audio_42.mp3")
        self.assertEqual(descriptor["mime_type"], "audio/mpeg")
        self.assertEqual(descriptor["duration"], 9)

    def test_audio_keeps_the_original_file_name(self):
        message = _message(
            audio=SimpleNamespace(
                file_id="au", file_size=7, file_name="briefing.m4a", mime_type="audio/mp4"
            )
        )
        descriptor = telegram_media.extract_media(message)
        self.assertEqual(descriptor["file_name"], "briefing.m4a")
        self.assertEqual(descriptor["mime_type"], "audio/mp4")

    def test_voice_has_priority_over_audio(self):
        # Un message ne porte qu'un média, mais si les deux attributs étaient
        # présents, la note vocale (plus spécifique) doit gagner.
        message = _message(
            voice=SimpleNamespace(file_id="vo", file_size=5, duration=3),
            audio=SimpleNamespace(file_id="au", file_size=7),
        )
        self.assertEqual(telegram_media.extract_media(message)["media_type"], "voice")

    def test_album_id_is_carried(self):
        message = _message(
            media_group_id="grp-1",
            photo=[SimpleNamespace(file_id="p", file_size=1, width=1, height=1)],
        )
        self.assertEqual(telegram_media.extract_media(message)["media_group_id"], "grp-1")

    def test_album_id_is_absent_outside_an_album(self):
        message = _message(photo=[SimpleNamespace(file_id="p", file_size=1, width=1, height=1)])
        self.assertIsNone(telegram_media.extract_media(message)["media_group_id"])

    def test_plain_message_has_no_media(self):
        self.assertIsNone(telegram_media.extract_media(_message()))


class IngestMediaTest(unittest.IsolatedAsyncioTestCase):
    async def test_success_uploads_then_indexes_extracted_text(self):
        message = _message(
            photo=[SimpleNamespace(file_id="fid", file_size=10, width=1, height=1)]
        )
        calls = {}
        stored = {}
        notes = []

        async def download(file_id):
            calls["file_id"] = file_id
            return b"bytes"

        def upload(data, **kwargs):
            calls["upload"] = (data, kwargs)
            return {"id": "media-1", "mime_type": "image/jpeg"}

        def extract(data, **kwargs):
            calls["extract"] = (data, kwargs)
            return {"ok": True, "text": "graphique BTC", "method": "gemini_vision", "reason": None}

        def store(media_id, text, asset=None):
            stored["call"] = (media_id, text)
            return 2

        result = await telegram_media.ingest_media(
            message,
            download=download,
            upload=upload,
            extract=extract,
            store=store,
            record=_recorder(notes),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["media"]["id"], "media-1")
        self.assertEqual(calls["file_id"], "fid")
        data, kwargs = calls["upload"]
        self.assertEqual(data, b"bytes")
        self.assertEqual(kwargs["media_type"], "photo")
        self.assertEqual(kwargs["chat_id"], "12345")
        self.assertEqual(kwargs["message_id"], 42)
        self.assertEqual(kwargs["telegram_file_id"], "fid")
        self.assertEqual(kwargs["source"], "telegram")
        self.assertTrue(kwargs["upsert"])

        # Le texte extrait est indexé sous le média créé.
        self.assertEqual(stored["call"], ("media-1", "graphique BTC"))
        self.assertEqual(result["extraction"]["method"], "gemini_vision")
        self.assertEqual(result["extraction"]["chunks"], 2)
        self.assertTrue(result["extraction"]["ok"])
        # L'aperçu reprend le texte extrait pour vérification côté utilisateur.
        self.assertEqual(result["extraction"]["excerpt"], "graphique BTC")
        # Le **résultat** est noté sur la ligne média : c'est la trace qui permet
        # plus tard de distinguer « lu » de « la clef manquait » — un média non lu
        # a des morceaux indexés dès qu'il porte une légende. `/transcribe` vit de
        # cette note, et rien d'autre ne l'écrit.
        self.assertEqual(notes[0]["media_id"], "media-1")
        self.assertTrue(notes[0]["summary"]["ok"])
        self.assertEqual(notes[0]["summary"]["chunks"], 2)

    async def test_extraction_failure_does_not_fail_ingestion(self):
        message = _message(
            document=SimpleNamespace(
                file_id="d", file_size=5, file_name="scan.pdf", mime_type="application/pdf"
            )
        )

        async def download(file_id):
            return b"%PDF-1.4"

        def upload(data, **kwargs):
            return {"id": "m-9", "mime_type": "application/pdf"}

        def extract(data, **kwargs):
            return {
                "ok": False,
                "text": "",
                "method": "pypdf",
                "reason": "PDF sans texte extractible (probablement scanné)",
            }

        def store(*_a, **_k):
            raise AssertionError("store ne doit pas être appelé sans texte")

        result = await telegram_media.ingest_media(
            message,
            download=download,
            upload=upload,
            extract=extract,
            store=store,
            record=_recorder(),
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["extraction"]["ok"])
        self.assertIn("scanné", result["extraction"]["reason"])

    async def test_indexing_failure_is_reported_without_failing_ingestion(self):
        message = _message(
            photo=[SimpleNamespace(file_id="p", file_size=1, width=1, height=1)]
        )

        async def download(file_id):
            return b"img"

        def upload(data, **kwargs):
            return {"id": "m-1"}

        def extract(data, **kwargs):
            return {"ok": True, "text": "texte utile", "method": "fake", "reason": None}

        def store(media_id, text, asset=None):
            raise RuntimeError("db down")

        result = await telegram_media.ingest_media(
            message,
            download=download,
            upload=upload,
            extract=extract,
            store=store,
            record=_recorder(),
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["extraction"]["ok"])
        self.assertIn("indexation échouée", result["extraction"]["reason"])

    async def test_missing_media_id_skips_extraction(self):
        message = _message(
            photo=[SimpleNamespace(file_id="p", file_size=1, width=1, height=1)]
        )

        async def download(file_id):
            return b"img"

        def upload(data, **kwargs):
            return {}  # la base n'a pas renvoyé d'id

        def extract(*_a, **_k):
            raise AssertionError("extract ne doit pas être appelé sans media_id")

        result = await telegram_media.ingest_media(
            message,
            download=download,
            upload=upload,
            extract=extract,
            store=lambda *a, **k: 0,
            record=_recorder(),
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["extraction"]["ok"])
        self.assertIn("media_id absent", result["extraction"]["reason"])

    async def test_too_large_is_refused_before_downloading(self):
        message = _message(
            video=SimpleNamespace(
                file_id="big",
                file_size=telegram_media.MAX_MEDIA_BYTES + 1,
            )
        )
        called = {"download": False}

        async def download(file_id):
            called["download"] = True
            return b"x"

        def upload(*_a, **_k):
            raise AssertionError("upload ne doit pas être appelé")

        result = await telegram_media.ingest_media(message, download=download, upload=upload)
        self.assertEqual(result["reason"], "too_large")
        self.assertFalse(called["download"])

    async def test_download_failure_is_reported(self):
        message = _message(
            photo=[SimpleNamespace(file_id="f", file_size=1, width=1, height=1)]
        )

        async def download(file_id):
            raise RuntimeError("lien expiré")

        def upload(*_a, **_k):
            raise AssertionError("upload ne doit pas être appelé")

        result = await telegram_media.ingest_media(message, download=download, upload=upload)
        self.assertEqual(result["reason"], "download_failed")
        self.assertIn("expiré", result["error"])

    async def test_upload_failure_is_reported(self):
        message = _message(
            document=SimpleNamespace(file_id="f", file_size=1, file_name="x.pdf")
        )

        async def download(file_id):
            return b"x"

        def upload(*_a, **_k):
            raise RuntimeError("Supabase indisponible")

        result = await telegram_media.ingest_media(message, download=download, upload=upload)
        self.assertEqual(result["reason"], "upload_failed")
        self.assertIn("Supabase indisponible", result["error"])

    async def test_message_without_media_does_not_download(self):
        async def download(file_id):  # pragma: no cover - ne doit pas être appelé
            raise AssertionError("download ne doit pas être appelé")

        result = await telegram_media.ingest_media(
            _message(), download=download, upload=lambda *a, **k: {}
        )
        self.assertEqual(result["reason"], "no_media")


class DeduplicationTest(unittest.IsolatedAsyncioTestCase):
    """Un média déjà stocké sous le même `telegram_file_id` ne l'est pas deux fois."""

    async def test_duplicate_is_detected_before_downloading(self):
        message = _message(
            photo=[SimpleNamespace(file_id="fid", file_size=10, width=1, height=1)]
        )
        row = {"id": "media-1", "file_name": "btc.png", "media_type": "photo"}

        async def download(file_id):  # pragma: no cover - ne doit pas être appelé
            raise AssertionError("un doublon ne doit pas être re-téléchargé")

        def upload(*args, **kwargs):  # pragma: no cover
            raise AssertionError("un doublon ne doit pas être réécrit")

        with mock.patch.object(
            telegram_media.media_store,
            "find_media_by_telegram_file_id",
            return_value=row,
        ) as finder:
            result = await telegram_media.ingest_media(
                message, download=download, upload=upload
            )

        finder.assert_called_once_with("fid")
        self.assertTrue(result["ok"])
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["media"]["id"], "media-1")
        self.assertEqual(result["media_type"], "photo")
        # Pas d'extraction : rien n'a été (re)téléchargé, donc rien à indexer.
        self.assertNotIn("extraction", result)
        self.assertIsNone(result["caption_ignored"])

    async def test_duplicate_reports_the_caption_it_could_not_index(self):
        message = _message(
            caption="nouvelle analyse",
            photo=[SimpleNamespace(file_id="fid", file_size=1, width=1, height=1)],
        )
        with mock.patch.object(
            telegram_media.media_store,
            "find_media_by_telegram_file_id",
            return_value={"id": "media-1"},
        ):
            result = await telegram_media.ingest_media(
                message, download=mock.AsyncMock()
            )
        self.assertEqual(result["caption_ignored"], "nouvelle analyse")

    async def test_same_file_sent_under_another_name_is_still_a_duplicate(self):
        # La déduplication porte sur l'empreinte du fichier côté Telegram, pas
        # sur son nom : renvoyer le même PDF sous un autre nom ne le duplique pas.
        message = _message(
            document=SimpleNamespace(file_id="same", file_size=10, file_name="autre.pdf")
        )
        with mock.patch.object(
            telegram_media.media_store,
            "find_media_by_telegram_file_id",
            return_value={"id": "media-9"},
        ):
            # `download` est un faux jamais appelé : le doublon court-circuite
            # le téléchargement avant même de le solliciter.
            result = await telegram_media.ingest_media(message, download=mock.AsyncMock())
        self.assertTrue(result["duplicate"])

    async def test_unknown_file_is_ingested_normally(self):
        message = _message(
            photo=[SimpleNamespace(file_id="new", file_size=4, width=1, height=1)]
        )
        uploads = []

        async def download(file_id):
            return b"data"

        def upload(data, **kwargs):
            uploads.append(kwargs)
            return {"id": "media-new"}

        with mock.patch.object(
            telegram_media.media_store, "find_media_by_telegram_file_id", return_value=None
        ):
            result = await telegram_media.ingest_media(
                message,
                download=download,
                upload=upload,
                extract=lambda *a, **k: {"ok": False},
                record=_recorder(),
            )

        self.assertTrue(result["ok"])
        self.assertNotIn("duplicate", result)
        self.assertEqual(len(uploads), 1)

    async def test_a_failing_duplicate_lookup_does_not_block_ingestion(self):
        # Supabase non configuré / en panne : on laisse passer et l'upload
        # signalera l'erreur réelle plutôt que de perdre le média.
        message = _message(
            photo=[SimpleNamespace(file_id="fid", file_size=4, width=1, height=1)]
        )
        uploads = []

        async def download(file_id):
            return b"data"

        def upload(data, **kwargs):
            uploads.append(kwargs)
            return {"id": "media-1"}

        with mock.patch.object(
            telegram_media.media_store,
            "find_media_by_telegram_file_id",
            side_effect=RuntimeError("Supabase non configuré"),
        ):
            result = await telegram_media.ingest_media(
                message,
                download=download,
                upload=upload,
                extract=lambda *a, **k: {"ok": False},
                record=_recorder(),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(len(uploads), 1)

    async def test_album_id_lands_in_metadata(self):
        message = _message(
            media_group_id="grp-7",
            photo=[SimpleNamespace(file_id="p", file_size=4, width=1, height=1)],
        )
        uploads = []

        async def download(file_id):
            return b"data"

        def upload(data, **kwargs):
            uploads.append(kwargs)
            return {"id": "media-1"}

        with mock.patch.object(
            telegram_media.media_store, "find_media_by_telegram_file_id", return_value=None
        ):
            await telegram_media.ingest_media(
                message,
                download=download,
                upload=upload,
                extract=lambda *a, **k: {"ok": False},
                record=_recorder(),
            )

        self.assertEqual(uploads[0]["metadata"], {"telegram": True, "media_group_id": "grp-7"})

    async def test_metadata_has_no_album_key_outside_an_album(self):
        message = _message(
            photo=[SimpleNamespace(file_id="p", file_size=4, width=1, height=1)]
        )
        uploads = []

        async def download(file_id):
            return b"data"

        def upload(data, **kwargs):
            uploads.append(kwargs)
            return {"id": "media-1"}

        with mock.patch.object(
            telegram_media.media_store, "find_media_by_telegram_file_id", return_value=None
        ):
            await telegram_media.ingest_media(
                message,
                download=download,
                upload=upload,
                extract=lambda *a, **k: {"ok": False},
                record=_recorder(),
            )

        self.assertEqual(uploads[0]["metadata"], {"telegram": True})

    async def test_audio_is_uploaded_with_its_own_type(self):
        message = _message(audio=SimpleNamespace(file_id="au", file_size=4, duration=2))
        uploads = []

        async def download(file_id):
            return b"sound"

        def upload(data, **kwargs):
            uploads.append(kwargs)
            return {"id": "media-au"}

        with mock.patch.object(
            telegram_media.media_store, "find_media_by_telegram_file_id", return_value=None
        ):
            result = await telegram_media.ingest_media(
                message,
                download=download,
                upload=upload,
                extract=lambda *a, **k: {"ok": False},
                record=_recorder(),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(uploads[0]["media_type"], "audio")
        self.assertEqual(uploads[0]["duration_seconds"], 2)


class AssetTaggingOnIngestTest(unittest.IsolatedAsyncioTestCase):
    """L'étiquette d'actif naît de la légende (sinon du texte extrait) et part
    **avec les morceaux** — c'est `knowledge_chunks.asset`, donc le filtre SQL."""

    def _message(self, caption=None):
        return _message(
            caption=caption,
            photo=[SimpleNamespace(file_id="p", file_size=1, width=1, height=1)],
        )

    async def _ingest(self, *, caption=None, extracted="", tag_store=None):
        stored = {}

        async def download(file_id):
            return b"img"

        def store(media_id, text, asset=None):
            stored["call"] = (media_id, text, asset)
            return 2

        def tag(media_id, asset, **kwargs):
            stored["tag"] = (media_id, asset, kwargs)
            return {"id": media_id}

        kwargs = {"tag_store": tag_store if tag_store is not None else tag}
        result = await telegram_media.ingest_media(
            self._message(caption),
            download=download,
            upload=lambda data, **kw: {"id": "m1", "mime_type": "image/jpeg"},
            extract=lambda data, **kw: {
                "ok": True,
                "text": extracted,
                "method": "gemini_vision",
                "reason": None,
            },
            store=store,
            record=_recorder(),
            **kwargs,
        )
        return result, stored

    async def test_the_caption_tags_the_chunks(self):
        result, stored = await self._ingest(caption="BTC support 64k")
        self.assertEqual(stored["call"][2], "BTC-USD")
        self.assertEqual(result["extraction"]["asset"], "BTC-USD")
        self.assertEqual(result["extraction"]["asset_source"], "caption")

    async def test_a_lowercase_caption_tags_the_media_too(self):
        """Le chemin complet : légende minuscule → morceaux **et** ligne média.

        C'est la forme ordinaire d'une légende Telegram ; l'exiger en majuscules
        laissait `asset` NULL, donc le média joker dans `match_knowledge_chunks`.
        """
        result, stored = await self._ingest(caption="btc support 64k")
        self.assertEqual(stored["call"][2], "BTC-USD")
        self.assertEqual(stored["tag"][1], "BTC-USD")
        self.assertEqual(result["extraction"]["asset_source"], "caption")

    async def test_the_labels_are_persisted_on_the_media_row_too(self):
        """Sans ça, un `↩️ Réindexer` perdrait un choix manuel."""
        result, stored = await self._ingest(caption="$ETH cassure")
        self.assertEqual(
            stored["tag"], ("m1", "ETH-USD", {"source": "caption", "reviewer": None})
        )
        self.assertTrue(result["asset_stored"])

    async def test_the_extracted_text_serves_when_the_caption_says_nothing(self):
        result, stored = await self._ingest(extracted="graphique de bitcoin en range")
        self.assertEqual(stored["call"][2], "BTC-USD")
        self.assertEqual(result["extraction"]["asset_source"], "extraction")

    async def test_the_caption_wins_over_the_extracted_text(self):
        result, stored = await self._ingest(
            caption="ETH range", extracted="graphique de bitcoin"
        )
        self.assertEqual(stored["call"][2], "ETH-USD")
        self.assertEqual(result["extraction"]["asset_source"], "caption")

    async def test_no_recognizable_asset_stays_unlabelled(self):
        """Non étiqueté n'est pas un échec : c'est le joker du filtre SQL."""
        result, stored = await self._ingest(caption="analyse du range 64k")
        self.assertIsNone(stored["call"][2])
        self.assertIsNone(result["extraction"]["asset"])
        self.assertIsNone(result["extraction"]["asset_source"])
        self.assertNotIn("tag", stored, "rien à enregistrer sur la ligne média")
        self.assertFalse(result["asset_stored"])

    async def test_an_ambiguous_word_is_not_an_asset(self):
        """« le sol du graphique » ne doit pas étiqueter SOL-USD."""
        result, stored = await self._ingest(caption="le sol du graphique tient")
        self.assertIsNone(stored["call"][2])
        self.assertIsNone(result["extraction"]["asset"])

    async def test_a_failed_tag_write_does_not_fail_the_ingestion(self):
        """Les morceaux portent déjà l'étiquette : la recherche la respecte."""
        def broken(media_id, asset, **kwargs):
            raise RuntimeError("db down")

        with contextlib.redirect_stdout(io.StringIO()) as out:
            result, stored = await self._ingest(caption="BTC range", tag_store=broken)
        self.assertTrue(result["ok"])
        self.assertEqual(stored["call"][2], "BTC-USD")
        self.assertFalse(result["asset_stored"])
        self.assertIn("db down", out.getvalue())

    async def test_the_report_names_the_asset_and_its_origin(self):
        result, _ = await self._ingest(caption="BTC support 64k")
        report = telegram_media.format_report(result)
        self.assertIn("Actif associé : BTC-USD (légende)", report)

    async def test_the_report_explains_how_to_tag_when_detection_fails(self):
        result, _ = await self._ingest(caption="aucun actif ici")
        report = telegram_media.format_report(result)
        self.assertIn("non reconnu", report)
        self.assertIn("/tag BTC-USD", report)

    async def test_the_report_warns_when_the_label_is_not_recorded(self):
        def broken(media_id, asset, **kwargs):
            raise RuntimeError("db down")

        with contextlib.redirect_stdout(io.StringIO()):
            result, _ = await self._ingest(caption="BTC range", tag_store=broken)
        self.assertIn("non enregistrée", telegram_media.format_report(result))


class ResolveAssetTest(unittest.TestCase):
    def test_a_forced_asset_is_never_overridden(self):
        self.assertEqual(
            telegram_media._resolve_asset("AAPL", "manual", "BTC range", "bitcoin"),
            ("AAPL", "manual"),
        )

    def test_a_forced_asset_without_source_counts_as_manual(self):
        self.assertEqual(telegram_media._resolve_asset("AAPL", None, "", ""), ("AAPL", "manual"))

    def test_detection_order_is_caption_then_content(self):
        self.assertEqual(
            telegram_media._resolve_asset(None, None, "ETH", "bitcoin"), ("ETH-USD", "caption")
        )
        self.assertEqual(
            telegram_media._resolve_asset(None, None, "", "bitcoin"),
            ("BTC-USD", "extraction"),
        )

    def test_nothing_detected_is_a_value_not_a_failure(self):
        self.assertEqual(telegram_media._resolve_asset(None, None, "", ""), (None, None))


class TagArgsTest(unittest.TestCase):
    """`/tag <ACTIF> [référence]` et `/tag --clear` : deux formes, pas trois."""

    def test_an_asset_alone_is_enough(self):
        parsed = telegram_media.parse_tag_args(["btc"])
        self.assertEqual(
            parsed, {"ok": True, "asset": "btc", "clear": False, "reference": None}
        )

    def test_a_reference_can_follow(self):
        parsed = telegram_media.parse_tag_args(["btc", "8f14e45f-ceea"]) 
        self.assertEqual(parsed["reference"], "8f14e45f-ceea")

    def test_clear_flags_remove_the_label(self):
        for flag in telegram_media.TAG_CLEAR_FLAGS:
            with self.subTest(flag=flag):
                parsed = telegram_media.parse_tag_args([flag])
                self.assertTrue(parsed["clear"])
                self.assertIsNone(parsed["asset"])

    def test_help_is_asked_for_without_arguments(self):
        for args in ([], ["--help"], ["-h"], ["help"]):
            with self.subTest(args=args):
                self.assertEqual(telegram_media.parse_tag_args(args)["reason"], "help")

    def test_extra_arguments_are_refused_rather_than_ignored(self):
        self.assertEqual(telegram_media.parse_tag_args(["btc", "ref", "extra"])["reason"], "too_many")

    def test_the_help_names_every_form(self):
        help_text = telegram_media.format_tag_help()
        self.assertIn("/tag BTC-USD", help_text)
        self.assertIn("réponse", help_text)
        self.assertIn("--clear", help_text)
        self.assertIn("préférentiel", help_text)

    def test_a_problem_is_announced_before_the_help(self):
        self.assertTrue(telegram_media.format_tag_help({"reason": "no_reference"}).startswith("⚠️"))
        self.assertIn(
            "pas de média", telegram_media.format_tag_help({"reason": "no_media_in_reply"})
        )


class FindMediaTest(unittest.IsolatedAsyncioTestCase):
    """Désigner un média stocké — question commune à `/tag` et `/transcribe`."""

    def _photo(self):
        return _message(photo=[SimpleNamespace(file_id="fid", file_size=1, width=1, height=1)])

    async def test_a_reference_is_taken_as_is(self):
        resolved = await telegram_media.find_media(reference=" 8f14e45f ")
        self.assertEqual(resolved["media_id"], "8f14e45f")

    async def test_a_replied_media_is_resolved_by_its_file_id(self):
        seen = {}

        def lookup(file_id):
            seen["file_id"] = file_id
            return {"id": "media-1"}

        resolved = await telegram_media.find_media(replied=self._photo(), lookup=lookup)
        self.assertEqual(resolved["media_id"], "media-1")
        self.assertEqual(seen["file_id"], "fid")

    async def test_a_reply_to_a_plain_message_is_explained(self):
        resolved = await telegram_media.find_media(replied=_message())
        self.assertEqual(resolved["reason"], "no_media_in_reply")

    async def test_without_reference_nor_reply_the_help_says_what_to_do(self):
        self.assertEqual(
            (await telegram_media.find_media())["reason"], "no_reference"
        )

    async def test_an_unknown_media_is_reported(self):
        resolved = await telegram_media.find_media(replied=self._photo(), lookup=lambda f: None)
        self.assertEqual(resolved["reason"], "not_found")

    async def test_a_failing_lookup_is_reported(self):
        def boom(file_id):
            raise RuntimeError("timeout")

        resolved = await telegram_media.find_media(replied=self._photo(), lookup=boom)
        self.assertEqual(resolved["reason"], "lookup_failed")
        self.assertIn("timeout", resolved["error"])



class TagMediaTest(unittest.IsolatedAsyncioTestCase):
    """L'étiquetage manuel écrit sur les **morceaux** (le filtre SQL) et sur la ligne."""

    ROW = {
        "id": "m1",
        "media_type": "photo",
        "file_name": "chart.jpg",
        "metadata": {"telegram": True},
    }

    def _harness(self, **overrides):
        calls = {}
        row = overrides.get("row", dict(self.ROW))

        def fetch(media_id):
            calls["fetch"] = media_id
            return row

        def set_chunks(media_id, asset):
            calls["set_chunks"] = (media_id, asset)
            if overrides.get("chunks_error"):
                raise overrides["chunks_error"]
            return overrides.get("chunks", 3)

        def tag(media_id, asset, **kwargs):
            calls["tag"] = (media_id, asset, kwargs)
            if overrides.get("tag_error"):
                raise overrides["tag_error"]
            return {"id": media_id}

        return calls, {"fetch": fetch, "set_chunks": set_chunks, "tag_store": tag}

    async def _tag(self, asset, **overrides):
        calls, deps = self._harness(**overrides)
        result = await telegram_media.tag_media("m1", asset, reviewer="42", **deps)
        return calls, result

    async def test_tagging_writes_both_the_chunks_and_the_media_line(self):
        calls, result = await self._tag("bitcoin")
        self.assertTrue(result["ok"])
        self.assertEqual(calls["set_chunks"], ("m1", "BTC-USD"), "actif non normalisé")
        self.assertEqual(
            calls["tag"], ("m1", "BTC-USD", {"source": "manual", "reviewer": "42"})
        )
        self.assertTrue(result["stored"])

    async def test_clearing_restores_the_wildcard(self):
        """`None` est la valeur du joker : le média redevient candidat partout."""
        calls, result = await self._tag(None)
        self.assertEqual(calls["set_chunks"], ("m1", None))
        self.assertEqual(calls["tag"], ("m1", None, {"source": None, "reviewer": None}))
        self.assertIsNone(result["asset"])

    async def test_the_previous_label_is_reported(self):
        row = {**self.ROW, "metadata": {"asset": {"value": "ETH-USD", "source": "caption"}}}
        _, result = await self._tag("BTC-USD", row=row)
        self.assertEqual(result["previous"], "ETH-USD")

    async def test_an_unusable_asset_is_refused_before_any_write(self):
        calls, result = await self._tag("pas un actif")
        self.assertEqual(result["reason"], "bad_asset")
        self.assertEqual(calls, {})

    async def test_an_unknown_media_is_refused(self):
        calls, result = await self._tag("BTC-USD", row=None)
        self.assertEqual(result["reason"], "not_found")
        self.assertNotIn("set_chunks", calls)

    async def test_a_failing_chunk_write_is_reported(self):
        """Sans les morceaux, rien n'est étiqueté : ne pas prétendre le contraire."""
        calls, result = await self._tag("BTC-USD", chunks_error=RuntimeError("rls"))
        self.assertEqual(result["reason"], "chunks_failed")
        self.assertNotIn("tag", calls)

    async def test_a_failing_label_write_still_leaves_the_chunks_tagged(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            _, result = await self._tag("BTC-USD", tag_error=RuntimeError("db down"))
        self.assertTrue(result["ok"])
        self.assertFalse(result["stored"])
        self.assertIn("db down", out.getvalue())

    async def test_a_rejected_extraction_can_still_be_tagged(self):
        """Zéro morceau : l'étiquette vaudra pour la prochaine extraction."""
        _, result = await self._tag("BTC-USD", chunks=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["chunks"], 0)

    def test_the_report_explains_the_effect_on_searches(self):
        text = telegram_media.format_tag_report(
            {"ok": True, "asset": "BTC-USD", "chunks": 3, "previous": "ETH-USD", "stored": True}
        )
        self.assertIn("BTC-USD", text)
        self.assertIn("Remplace : ETH-USD", text)
        self.assertIn("autres actifs", text)

    def test_the_report_says_when_nothing_is_indexed_yet(self):
        text = telegram_media.format_tag_report(
            {"ok": True, "asset": "BTC-USD", "chunks": 0, "stored": True}
        )
        self.assertIn("Aucun morceau", text)

    def test_the_report_of_an_erasure_explains_the_wildcard(self):
        text = telegram_media.format_tag_report(
            {"ok": True, "asset": None, "chunks": 2, "previous": "BTC-USD", "stored": True}
        )
        self.assertIn("non étiqueté", text)
        self.assertIn("BTC-USD", text)

    def test_every_failure_is_named(self):
        for reason in ("not_found", "bad_asset", "chunks_failed", "lookup_failed"):
            with self.subTest(reason=reason):
                text = telegram_media.format_tag_report(
                    {"ok": False, "reason": reason, "asset": "??", "error": "détail"}
                )
                self.assertTrue(text)
                self.assertNotIn("None", text)

    async def test_the_reindex_keeps_the_stored_label(self):
        """Un choix manuel ne doit pas être perdu par `↩️ Réindexer`."""
        calls = {}

        def store(media_id, text, asset=None):
            calls["store"] = asset
            return 2

        row = {
            **self.ROW,
            "storage_path": "telegram/c/1-chart.jpg",
            "mime_type": "image/jpeg",
            "caption": "analyse du range",
            "metadata": {"asset": {"value": "BTC-USD", "source": "manual"}},
        }
        result = await telegram_media.review_media(
            "m1",
            "re",
            fetch=lambda media_id: row,
            download=lambda path: b"img",
            extract=lambda data, **kw: {"ok": True, "text": "range 64k", "method": "vision", "reason": None},
            store=store,
            record=_recorder(),
            mark=lambda *a, **k: {"id": "m1"},
            tag_store=lambda media_id, asset, **kw: {"id": media_id},
        )
        self.assertEqual(calls["store"], "BTC-USD")
        self.assertTrue(result["asset_stored"])
        self.assertIn("Actif associé : BTC-USD (choix manuel)", telegram_media.format_review_report(result))

    async def test_an_untagged_media_is_retagged_by_the_reindex(self):
        """Une légende ajoutée plus tard doit pouvoir étiqueter le média."""
        calls = {}
        written = {}

        def store(media_id, text, asset=None):
            calls["store"] = asset
            return 2

        def tag(media_id, asset, **kwargs):
            written["tag"] = (asset, kwargs)
            return {"id": media_id}

        row = {
            **self.ROW,
            "storage_path": "telegram/c/1-chart.jpg",
            "mime_type": "image/jpeg",
            "caption": "$ETH cassure",
        }
        result = await telegram_media.review_media(
            "m1",
            "re",
            fetch=lambda media_id: row,
            download=lambda path: b"img",
            extract=lambda data, **kw: {"ok": True, "text": "range", "method": "vision", "reason": None},
            store=store,
            record=_recorder(),
            mark=lambda *a, **k: {"id": "m1"},
            tag_store=tag,
        )
        self.assertEqual(calls["store"], "ETH-USD")
        self.assertEqual(written["tag"], ("ETH-USD", {"source": "caption", "reviewer": None}))
        self.assertTrue(result["asset_stored"])


class TagCommandWiringTest(unittest.TestCase):
    """`/tag`, vu depuis `main.py` (non importable : dépendances lourdes)."""

    MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"

    def _body(self) -> str:
        text = self.MAIN.read_text(encoding="utf-8")
        return text.split("async def tag_cmd", 1)[1].split("\nasync def ", 1)[0]

    def test_the_command_is_registered(self):
        self.assertIn('CommandHandler("tag", tag_cmd)', self.MAIN.read_text(encoding="utf-8"))

    def test_the_arguments_are_parsed_by_the_module(self):
        self.assertIn("telegram_media.parse_tag_args(context.args", self._body())

    def test_the_replied_media_is_handed_over(self):
        """Sans la réponse, l'utilisateur devrait copier un UUID à la main."""
        body = self._body()
        self.assertIn("replied=update.message.reply_to_message", body)
        self.assertIn("reference=parsed[\"reference\"]", body)

    def test_a_bad_invocation_shows_the_help(self):
        body = self._body()
        self.assertIn("telegram_media.format_tag_help(parsed)", body)
        self.assertIn("telegram_media.format_tag_help(resolved)", body)

    def test_the_label_is_attributed_to_the_requester(self):
        body = self._body()
        self.assertIn("telegram_media.tag_media(", body)
        self.assertIn("reviewer=str(update.effective_user.id)", body)
        self.assertIn("telegram_media.format_tag_report(result)", body)

    def test_the_start_message_advertises_the_command(self):
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertIn("/tag BTC-USD", text)
        self.assertIn("/tag --help", text)


class ExtractionAttachmentTest(unittest.TestCase):
    """Texte trop long pour l'aperçu → pièce jointe `.txt` (et pas un fragment)."""

    LIMIT = telegram_media.EXCERPT_CHARS

    def _result(self, text, **overrides):
        media = {"id": "8f14e45f-ceea-467a-9c1c-1a2b3c4d5e6f", "file_name": "photo_42.jpg"}
        media.update(overrides.pop("media", {}))
        extraction = {
            "ok": True,
            "method": "whisper",
            "chars": len(text),
            "chunks": 2,
            "excerpt": telegram_media._excerpt(text),
            "text": text,
        }
        extraction.update(overrides.pop("extraction", {}))
        return {"ok": True, "media": media, "extraction": extraction, **overrides}

    def test_full_text_is_read_from_the_extraction(self):
        self.assertEqual(telegram_media.full_text(self._result("abc")), "abc")
        self.assertEqual(telegram_media.full_text({"extraction": None}), "")
        self.assertEqual(telegram_media.full_text({}), "")

    def test_the_attachment_arrives_exactly_when_the_excerpt_truncates(self):
        """Les deux critères sont le même : pas de divergence possible."""
        at_limit = "mot " * (self.LIMIT // 4)
        over_limit = at_limit + "encore"
        for text in ("", "court", at_limit):
            with self.subTest(text=len(text)):
                self.assertFalse(telegram_media.attachment_needed(text))
                self.assertNotIn("…", telegram_media._excerpt(text), "aperçu tronqué sans joint")
        self.assertTrue(telegram_media.attachment_needed(over_limit))
        self.assertIn("…", telegram_media._excerpt(over_limit))

    def test_whitespace_does_not_forge_a_long_text(self):
        self.assertFalse(telegram_media.attachment_needed(" " * (self.LIMIT * 2)))

    def test_no_attachment_when_the_excerpt_says_everything(self):
        self.assertIsNone(telegram_media.attachment_for(self._result("extraction courte")))

    def test_no_attachment_without_any_text(self):
        result = self._result("", extraction={"ok": False, "reason": "scanné", "text": ""})
        self.assertIsNone(telegram_media.attachment_for(result))

    def test_the_file_carries_the_indexed_text_byte_for_byte(self):
        """Le .txt doit être ce qui est indexé — pas un résumé, pas un nettoyage."""
        text = "ligne 1\n\n" + "bloc " * 200 + "\nfin"
        attachment = telegram_media.attachment_for(self._result(text))

        self.assertEqual(attachment["content"].decode("utf-8"), text)
        self.assertEqual(attachment["chars"], len(text))
        self.assertEqual(attachment["filename"], "photo_42-extraction.txt")
        self.assertIn(str(len(text)), attachment["caption"])
        self.assertIn("indexé", attachment["caption"])

    def test_the_caption_stays_small_enough_for_telegram(self):
        attachment = telegram_media.attachment_for(self._result("x" * 5000))
        self.assertLessEqual(len(attachment["caption"]), 1024)

    def test_the_name_is_sanitized_without_losing_the_original(self):
        cases = {
            "rapport trimestriel.pdf": "rapport-trimestriel-extraction.txt",
            "a/b\\c.pdf": "c-extraction.txt",
            "graphique.PNG": "graphique-extraction.txt",
            "éàç.pdf": "éàç-extraction.txt",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                result = self._result("y" * 500, media={"file_name": name})
                self.assertEqual(telegram_media.attachment_for(result)["filename"], expected)

    def test_a_useless_name_falls_back_to_the_media_id(self):
        for name in ("", ".pdf", "   ", "///", None):
            with self.subTest(name=name):
                result = self._result("y" * 500, media={"file_name": name})
                self.assertEqual(
                    telegram_media.attachment_for(result)["filename"],
                    "extraction-8f14e45f-extraction.txt",
                )

    def test_a_very_long_name_is_bounded(self):
        result = self._result("y" * 500, media={"file_name": "a" * 300 + ".pdf"})
        name = telegram_media.attachment_for(result)["filename"]
        self.assertLessEqual(len(name), 90)
        self.assertTrue(name.endswith("-extraction.txt"))

    def test_a_media_without_a_name_still_gets_a_usable_file(self):
        result = self._result("y" * 500, media={"id": None, "file_name": None})
        self.assertEqual(
            telegram_media.attachment_for(result)["filename"], "extraction-media-extraction.txt"
        )


class LongExtractionEndToEndTest(unittest.IsolatedAsyncioTestCase):
    """De l'ingestion au fichier : le `.txt` contient ce qui a été indexé."""

    def _message(self):
        return _message(
            document=SimpleNamespace(
                file_id="d", file_size=10, file_name="transcription.m4a", mime_type="audio/mp4"
            ),
            caption="BTC range 64k-68k",
        )

    async def _ingest(self, extracted):
        stored = {}

        async def download(file_id):
            return b"audio"

        def store(media_id, text, asset=None):
            stored["text"] = text
            return 4

        result = await telegram_media.ingest_media(
            self._message(),
            download=download,
            upload=lambda data, **kw: {"id": "m1", "mime_type": "audio/mp4"},
            extract=lambda data, **kw: {
                "ok": True,
                "text": extracted,
                "method": "whisper-large-v3",
                "reason": None,
            },
            store=store,
            record=_recorder(),
        )
        return result, stored

    async def test_a_long_transcription_is_attached_in_full(self):
        extracted = "le narrateur décrit la cassure " * 40
        result, stored = await self._ingest(extracted)

        attachment = telegram_media.attachment_for(result)
        self.assertIsNotNone(attachment)
        # Ce qui part en pièce jointe est **exactement** ce qui a été indexé.
        self.assertEqual(attachment["content"].decode("utf-8"), stored["text"])
        self.assertTrue(stored["text"].startswith("BTC range 64k-68k"))

        report = telegram_media.format_report(result)
        self.assertIn("pièce jointe", report)
        self.assertIn(attachment["filename"], report)
        self.assertNotIn("…", report, "plus d'aperçu tronqué")

    async def test_a_short_extraction_keeps_the_inline_preview(self):
        result, stored = await self._ingest("cassure confirmée sous 68k")
        self.assertIsNone(telegram_media.attachment_for(result))
        report = telegram_media.format_report(result)
        # L'aperçu montre le texte indexé **entier** (légende comprise), sans « … »
        # (seuls les espaces sont normalisés : les mots sont tous là).
        self.assertIn(f"Aperçu : {' '.join(stored['text'].split())}", report)
        self.assertNotIn("…", report)
        self.assertNotIn("pièce jointe", report)

    async def test_a_long_caption_alone_goes_to_the_attachment(self):
        """Même quand l'extraction échoue : le texte indexé peut déjà être long."""
        message = _message(
            caption="règle " * 200,
            photo=[SimpleNamespace(file_id="p", file_size=1, width=1, height=1)],
        )

        async def download(file_id):
            return b"img"

        result = await telegram_media.ingest_media(
            message,
            download=download,
            upload=lambda data, **kw: {"id": "m2", "mime_type": "image/jpeg"},
            extract=lambda data, **kw: {
                "ok": False,
                "text": "",
                "method": "gemini_vision",
                "reason": "clé absente",
            },
            store=lambda media_id, text, asset=None: 3,
            record=_recorder(),
        )
        self.assertFalse(result["extraction"]["ok"])
        self.assertIsNotNone(telegram_media.attachment_for(result))
        self.assertIn("clé absente", telegram_media.format_report(result))

    async def test_an_unreadable_media_without_text_sends_nothing(self):
        message = _message(
            photo=[SimpleNamespace(file_id="p", file_size=1, width=1, height=1)]
        )

        async def download(file_id):
            return b"img"

        result = await telegram_media.ingest_media(
            message,
            download=download,
            upload=lambda data, **kw: {"id": "m3", "mime_type": "image/jpeg"},
            extract=lambda data, **kw: {
                "ok": False,
                "text": "",
                "method": "gemini_vision",
                "reason": "scanné",
            },
            store=lambda media_id, text, asset=None: 0,
            record=_recorder(),
        )
        self.assertIsNone(telegram_media.attachment_for(result))
        self.assertNotIn("pièce jointe", telegram_media.format_report(result))


class FormatReportTest(unittest.TestCase):
    def test_duplicate_report_says_it_is_already_stored(self):
        report = telegram_media.format_report(
            {
                "ok": True,
                "duplicate": True,
                "media_type": "photo",
                "media": {"id": "media-1", "file_name": "btc.png"},
            }
        )
        self.assertIn("Déjà stocké", report)
        self.assertIn("btc.png", report)
        self.assertIn("media-1", report)
        self.assertIn("telegram_file_id", report)

    def test_duplicate_report_warns_when_a_new_caption_is_dropped(self):
        report = telegram_media.format_report(
            {
                "ok": True,
                "duplicate": True,
                "media_type": "photo",
                "media": {"id": "media-1"},
                "caption_ignored": "BTC support 64k",
            }
        )
        self.assertIn("n'a pas été indexée", report)

    def test_duplicate_report_is_quiet_without_a_caption(self):
        report = telegram_media.format_report(
            {"ok": True, "duplicate": True, "media_type": "photo", "media": {"id": "m"}}
        )
        self.assertNotIn("n'a pas été indexée", report)

    def test_duplicate_report_survives_a_media_without_name(self):
        report = telegram_media.format_report(
            {"ok": True, "duplicate": True, "media_type": "document", "media": {"id": "m2"}}
        )
        self.assertIn("m2", report)

    def test_success_report_mentions_reference(self):
        text = telegram_media.format_report(
            {
                "ok": True,
                "media_type": "photo",
                "bytes": 2048,
                "media": {"id": "m1", "mime_type": "image/jpeg"},
            }
        )
        self.assertIn("m1", text)
        self.assertIn("image/jpeg", text)

    def test_too_large_report_names_the_limit(self):
        text = telegram_media.format_report(
            {"ok": False, "reason": "too_large", "size": 30_000_000}
        )
        self.assertIn("20 Mo", text)

    def test_upload_failure_report_includes_error(self):
        text = telegram_media.format_report(
            {"ok": False, "reason": "upload_failed", "error": "boom"}
        )
        self.assertIn("boom", text)

    def test_success_report_mentions_indexed_content(self):
        text = telegram_media.format_report(
            {
                "ok": True,
                "media_type": "photo",
                "bytes": 2048,
                "media": {"id": "m1", "mime_type": "image/jpeg"},
                "extraction": {
                    "ok": True,
                    "method": "gemini_vision",
                    "chars": 120,
                    "chunks": 1,
                },
            }
        )
        self.assertIn("gemini_vision", text)
        self.assertIn("m1", text)

    def test_success_report_shows_the_excerpt(self):
        text = telegram_media.format_report(
            {
                "ok": True,
                "media_type": "photo",
                "bytes": 2048,
                "media": {"id": "m1", "mime_type": "image/jpeg"},
                "extraction": {
                    "ok": True,
                    "method": "gemini_vision",
                    "chars": 120,
                    "chunks": 1,
                    "excerpt": "cassure des 100k, RSI 62",
                },
            }
        )
        self.assertIn("Aperçu : cassure des 100k, RSI 62", text)

    def test_excerpt_is_shown_even_when_indexing_failed(self):
        text = telegram_media.format_report(
            {
                "ok": True,
                "media_type": "voice",
                "bytes": 10,
                "media": {"id": "m3", "mime_type": "audio/ogg"},
                "extraction": {
                    "ok": False,
                    "method": "groq_whisper",
                    "chars": 40,
                    "chunks": 0,
                    "reason": "indexation échouée : db down",
                    "excerpt": "note vocale sur le support",
                },
            }
        )
        self.assertIn("indexation échouée", text)
        self.assertIn("note vocale sur le support", text)

    def test_success_report_surfaces_extraction_failure(self):
        text = telegram_media.format_report(
            {
                "ok": True,
                "media_type": "document",
                "bytes": 10,
                "media": {"id": "m2", "mime_type": "application/pdf"},
                "extraction": {
                    "ok": False,
                    "method": "pypdf",
                    "chars": 0,
                    "chunks": 0,
                    "reason": "PDF scanné",
                },
            }
        )
        self.assertIn("PDF scanné", text)


class ExcerptTest(unittest.TestCase):
    def test_short_text_is_kept_as_is(self):
        self.assertEqual(telegram_media._excerpt("  texte  utile "), "texte utile")

    def test_whitespace_is_collapsed(self):
        self.assertEqual(telegram_media._excerpt("a\n\nb\t c"), "a b c")

    def test_long_text_is_truncated_with_a_marker(self):
        excerpt = telegram_media._excerpt("x" * 1000)
        self.assertEqual(len(excerpt), telegram_media.EXCERPT_CHARS + 1)
        self.assertTrue(excerpt.endswith("…"))

    def test_empty_text_yields_empty_excerpt(self):
        self.assertEqual(telegram_media._excerpt(""), "")
        self.assertEqual(telegram_media._excerpt(None), "")


class FormatMediaListTest(unittest.TestCase):
    def test_empty_list(self):
        self.assertIn("Aucun média", telegram_media.format_media_list([]))

    def test_the_first_page_without_a_next_one_keeps_its_header(self):
        """Le cas courant ne parle pas de rangs : il n'en a pas besoin."""
        text = telegram_media.format_media_list(self._entries(3), expires_in=3600)
        self.assertIn("3 dernier(s) média(s)", text)
        self.assertIn("60 min", text)
        self.assertNotIn("lignes", text)

    def test_a_later_page_names_the_lines_it_holds(self):
        """Taire le rang ferait passer la troisième page pour le début de la liste."""
        text = telegram_media.format_media_list(self._entries(5), offset=20)
        self.assertIn("lignes 21–25", text)

    def test_the_amount_left_behind_is_not_invented(self):
        """Lecture bornée : on sait qu'une page suit, pas combien de médias elle porte."""
        text = telegram_media.format_media_list(self._entries(10), more=True)
        self.assertIn("d'autres suivent", text)
        self.assertIn("« ▶️ Suivants »", text)
        for digit in "0123456789":
            self.assertNotIn(f"{digit} autre", text)

    def test_a_rank_beyond_the_end_says_the_list_moved(self):
        """« Aucun média » serait faux : la table n'a pas été vidée, la page a glissé."""
        text = telegram_media.format_media_list([], offset=30)
        self.assertIn("Rien à ces rangs", text)
        self.assertIn("a changé", text)
        self.assertIn("◀️ Précédents", text)
        self.assertNotIn("Aucun média", text)

    def _entries(self, count):
        return [
            {"row": {"media_type": "photo", "file_name": f"{i}.jpg"}, "url": "u"}
            for i in range(count)
        ]

    def test_entries_show_label_and_link(self):
        entries = [
            {
                "row": {
                    "media_type": "photo",
                    "file_name": "btc.png",
                    "file_size": 2048,
                    "created_at": "2026-09-27T10:00:00Z",
                },
                "url": "https://storage/signed",
            }
        ]
        text = telegram_media.format_media_list(entries, expires_in=3600)
        self.assertIn("photo · btc.png · 2.0 Ko · 2026-09-27", text)
        self.assertIn("https://storage/signed", text)
        self.assertIn("60 min", text)

    def test_missing_link_is_flagged_without_hiding_the_others(self):
        entries = [
            {"row": {"media_type": "photo", "file_name": "a.png"}, "url": None},
            {"row": {"media_type": "video", "file_name": "b.mp4"}, "url": "https://ok/b"},
        ]
        text = telegram_media.format_media_list(entries)
        self.assertIn("lien indisponible", text)
        self.assertIn("https://ok/b", text)

    def test_size_is_robust_to_missing_values(self):
        text = telegram_media.format_media_list([{"row": {"file_size": None}, "url": "u"}])
        self.assertIn("taille inconnue", text)


class MediaPageDataTest(unittest.TestCase):
    """Le rang porté par un bouton de navigation de `/media` — et rien d'autre."""

    def test_the_prefix_is_disjoint_from_the_review_ones(self):
        """`^medl:` exige un deux-points juste après : `medlg:` reste à part."""
        for prefix in (
            telegram_media.REVIEW_PREFIX,
            telegram_media.LIST_REVIEW_PREFIX,
            telegram_media.PENDING_REVIEW_PREFIX,
            telegram_media.PENDING_PAGE_PREFIX,
            telegram_media.TRANSCRIBE_PAGE_PREFIX,
        ):
            with self.subTest(prefix=prefix):
                self.assertNotEqual(prefix, telegram_media.MEDIA_PAGE_PREFIX)
                self.assertFalse(telegram_media.media_page_data(3).startswith(prefix + ":"))

    def test_a_round_trip_gives_the_rank_back(self):
        for offset in (0, 1, 10, 1000):
            with self.subTest(offset=offset):
                self.assertEqual(
                    telegram_media.parse_media_page(telegram_media.media_page_data(offset)),
                    offset,
                )

    def test_a_negative_rank_is_flattened_rather_than_refused(self):
        """`media_page_data` borne à gauche : un rang négatif n'existe pas."""
        self.assertEqual(telegram_media.media_page_data(-5), "medlg:0")

    def test_a_foreign_or_broken_payload_is_refused(self):
        """Refuser plutôt que ramener à zéro : un bouton illisible ne doit pas
        réafficher la première page en donnant l'impression d'avoir été compris."""
        for data in (
            None,
            "",
            "med:0",
            "medl:ok:x",
            "medlg",
            "medlg:",
            "medlg:x",
            "medlg:-1",
            "medlg:1:2",
            "medp:0",
            "medpg:20",
            "medt:10",
            "rag:3",
        ):
            with self.subTest(data=data):
                self.assertIsNone(telegram_media.parse_media_page(data))

    def test_nothing_to_go_to_means_no_keyboard(self):
        """Un clavier qui ne mène nulle part laisserait croire qu'il reste des lignes."""
        self.assertEqual(telegram_media.media_page_buttons(offset=0, more=False), [])

    def test_the_first_page_only_goes_forward(self):
        rows = telegram_media.media_page_buttons(offset=0, more=True, page=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual([label for label, _ in rows[0]], ["▶️ Suivants"])
        self.assertEqual([data for _, data in rows[0]], ["medlg:10"])

    def test_the_last_page_only_goes_back(self):
        rows = telegram_media.media_page_buttons(offset=10, more=False, page=10)
        self.assertEqual([label for label, _ in rows[0]], ["◀️ Précédents"])
        self.assertEqual([data for _, data in rows[0]], ["medlg:0"])

    def test_both_directions_share_one_row(self):
        """Ce ne sont pas des lignes de la liste : ils ne s'alignent sur aucune ligne."""
        rows = telegram_media.media_page_buttons(offset=10, more=True, page=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            [label for label, _ in rows[0]], ["◀️ Précédents", "▶️ Suivants"]
        )
        self.assertEqual([data for _, data in rows[0]], ["medlg:0", "medlg:20"])

    def test_the_step_is_the_page_size(self):
        """Le rang avance de ce qu'une page tient, pas d'un pas inventé ici."""
        rows = telegram_media.media_page_buttons(offset=20, more=True, page=25)
        self.assertEqual([data for _, data in rows[0]], ["medlg:0", "medlg:45"])

    def test_a_page_that_would_start_before_the_first_line_is_flattened(self):
        """Un rang de 5 ne recule pas de 10 : il recule au plus haut, à la ligne 0."""
        rows = telegram_media.media_page_buttons(offset=5, more=True, page=10)
        self.assertEqual([data for _, data in rows[0]], ["medlg:0", "medlg:15"])


class MediaListReportTest(unittest.TestCase):
    def test_each_media_gets_a_signed_link(self):
        rows = [
            {
                "id": "m1",
                "storage_path": "telegram/canal/1-a.jpg",
                "media_type": "photo",
                "file_name": "a.jpg",
                "file_size": 100,
            }
        ]
        with mock.patch.object(
            telegram_media.media_store, "list_media", return_value=rows
        ) as listing, mock.patch.object(
            telegram_media.media_store, "create_signed_url", return_value="https://signed"
        ) as sign:
            report = telegram_media.media_list_report()
        self.assertIn("https://signed", report)
        listing.assert_called_once()
        sign.assert_called_once_with("telegram/canal/1-a.jpg", expires_in=telegram_media.SIGNED_URL_TTL)

    def test_limit_is_clamped_to_the_maximum(self):
        """La **page** est bornée à `MAX_MEDIA_PAGE` ; la lecture demande, elle, une
        ligne de plus — c'est elle qui dit s'il reste quelque chose après
        (`more`), et elle n'est ni affichée ni signée."""
        with mock.patch.object(
            telegram_media.media_store, "list_media", return_value=[]
        ) as listing:
            telegram_media.media_list_report(limit=10_000)
        self.assertEqual(
            listing.call_args.kwargs["limit"], telegram_media.MAX_MEDIA_PAGE + 1
        )

    def test_read_failure_becomes_a_message_not_an_exception(self):
        with mock.patch.object(
            telegram_media.media_store, "list_media", side_effect=RuntimeError("db down")
        ):
            report = telegram_media.media_list_report()
        self.assertIn("db down", report)

    def test_one_broken_link_does_not_break_the_others(self):
        rows = [
            {"storage_path": "p1", "media_type": "photo", "file_name": "a"},
            {"storage_path": "p2", "media_type": "photo", "file_name": "b"},
        ]

        def sign(path, *, expires_in=0):
            if path == "p1":
                raise RuntimeError("lien refusé")
            return "https://ok/b"

        with mock.patch.object(
            telegram_media.media_store, "list_media", return_value=rows
        ), mock.patch.object(telegram_media.media_store, "create_signed_url", side_effect=sign):
            report = telegram_media.media_list_report()
        self.assertIn("lien indisponible", report)
        self.assertIn("https://ok/b", report)


class MediaOriginTest(unittest.TestCase):
    """`/media` doit dire d'où vient un média — canal, et quel genre d'aperçu."""

    def test_a_bot_channel_post_names_the_channel_and_the_message(self):
        origin = telegram_media.media_origin(
            {"metadata": {"channel_post": {"channel": "crypto_signals", "message_id": 42}}}
        )
        self.assertEqual(origin, "📡 @crypto_signals · msg 42")

    def test_a_private_channel_is_shown_by_its_title(self):
        """Un titre n'est pas un pseudo : `@Crypto Signals` ne désignerait rien."""
        origin = telegram_media.media_origin(
            {"metadata": {"channel_post": {"channel": "Crypto Signals", "message_id": 7}}}
        )
        self.assertEqual(origin, "📡 Crypto Signals · msg 7")

    def test_a_numeric_channel_id_is_left_alone(self):
        origin = telegram_media.media_origin(
            {"metadata": {"channel_post": {"channel": "-1001234567890"}}}
        )
        self.assertEqual(origin, "📡 -1001234567890")

    def test_a_scraper_preview_is_flagged_as_such(self):
        """L'aperçu public n'est pas le fichier d'origine : le dire évite de le croire complet."""
        origin = telegram_media.media_origin(
            {"metadata": {"channel": "crypto_signals", "web_preview": True}}
        )
        self.assertEqual(origin, "📡 @crypto_signals · aperçu web")

    def test_a_direct_message_has_no_origin(self):
        self.assertIsNone(telegram_media.media_origin({"metadata": {"telegram": True}}))
        self.assertIsNone(telegram_media.media_origin({"metadata": None}))
        self.assertIsNone(telegram_media.media_origin({"metadata": ["bricolé"]}))
        self.assertIsNone(telegram_media.media_origin(None))

    def test_the_origin_appears_in_the_media_line(self):
        row = {
            "id": "m1",
            "media_type": "photo",
            "file_name": "a.jpg",
            "file_size": 1,
            "metadata": {"channel_post": {"channel": "canal", "message_id": 3}},
        }
        text = telegram_media.format_media_list([{"row": row, "url": "u"}])
        self.assertIn("photo · a.jpg · 📡 @canal · msg 3", text)

    def test_a_direct_media_line_has_no_origin(self):
        row = {"id": "m1", "media_type": "photo", "file_name": "a.jpg", "file_size": 1}
        text = telegram_media.format_media_list([{"row": row, "url": "u"}])
        self.assertNotIn("📡", text)


class IndexableTextTest(unittest.TestCase):
    def test_caption_comes_first_then_the_extracted_content(self):
        self.assertEqual(
            telegram_media._indexable_text("BTC support 64k", "cassure confirmée"),
            "BTC support 64k\n\ncassure confirmée",
        )

    def test_empty_parts_are_ignored(self):
        self.assertEqual(telegram_media._indexable_text("", "texte"), "texte")
        self.assertEqual(telegram_media._indexable_text("  caption  ", ""), "caption")
        self.assertEqual(telegram_media._indexable_text("", ""), "")
        self.assertEqual(telegram_media._indexable_text(None, None), "")


class CaptionIndexingTest(unittest.IsolatedAsyncioTestCase):
    """La légende est indexée avec le contenu, et même sans extraction réussie."""

    def _photo(self, caption):
        return _message(
            caption=caption,
            photo=[SimpleNamespace(file_id="p", file_size=1, width=1, height=1)],
        )

    async def _ingest(self, message, extract, stored):
        async def download(file_id):
            return b"img"

        def upload(data, **kwargs):
            return {"id": "m-1"}

        def store(media_id, text, asset=None):
            stored["text"] = text
            return 2

        return await telegram_media.ingest_media(
            message,
            download=download,
            upload=upload,
            extract=extract,
            store=store,
            record=_recorder(),
        )

    async def test_caption_is_indexed_with_the_extracted_text(self):
        def extract(data, **kwargs):
            return {"ok": True, "text": "cassure confirmée", "method": "gemini_vision", "reason": None}

        stored = {}
        result = await self._ingest(self._photo("BTC support 64k"), extract, stored)
        self.assertTrue(stored["text"].startswith("BTC support 64k"))
        self.assertIn("cassure confirmée", stored["text"])
        self.assertTrue(result["extraction"]["has_caption"])
        self.assertEqual(result["extraction"]["chars"], len(stored["text"]))

    async def test_caption_is_indexed_even_when_extraction_fails(self):
        def extract(data, **kwargs):
            return {"ok": False, "text": "", "method": "gemini_vision", "reason": "clé absente"}

        stored = {}
        result = await self._ingest(self._photo("BTC range 64k-68k"), extract, stored)
        self.assertEqual(stored["text"], "BTC range 64k-68k")
        self.assertFalse(result["extraction"]["ok"])
        self.assertEqual(result["extraction"]["chunks"], 2)
        self.assertEqual(result["extraction"]["reason"], "clé absente")

    async def test_without_caption_a_failed_extraction_indexes_nothing(self):
        def extract(data, **kwargs):
            return {"ok": False, "text": "", "method": "pypdf", "reason": "scanné"}

        def store(media_id, text, asset=None):
            raise AssertionError("rien ne doit être indexé sans texte")

        async def download(file_id):
            return b"img"

        message = _message(photo=[SimpleNamespace(file_id="p", file_size=1, width=1, height=1)])
        result = await telegram_media.ingest_media(
            message,
            download=download,
            upload=lambda data, **kw: {"id": "m-1"},
            extract=extract,
            store=store,
            record=_recorder(),
        )
        self.assertEqual(result["extraction"]["chunks"], 0)
        self.assertFalse(result["extraction"]["has_caption"])

    def test_report_labels_the_caption_alongside_the_method(self):
        text = telegram_media.format_report(
            {
                "ok": True,
                "media_type": "photo",
                "bytes": 10,
                "media": {"id": "m1", "mime_type": "image/jpeg"},
                "extraction": {
                    "ok": True,
                    "method": "gemini_vision",
                    "chars": 120,
                    "chunks": 2,
                    "has_caption": True,
                },
            }
        )
        self.assertIn("gemini_vision + légende", text)

    def test_report_says_when_only_the_caption_was_indexed(self):
        text = telegram_media.format_report(
            {
                "ok": True,
                "media_type": "photo",
                "bytes": 10,
                "media": {"id": "m2", "mime_type": "image/jpeg"},
                "extraction": {
                    "ok": False,
                    "method": "gemini_vision",
                    "chars": 14,
                    "chunks": 1,
                    "reason": "clé absente",
                    "has_caption": True,
                    "excerpt": "BTC range 64k",
                },
            }
        )
        self.assertIn("clé absente", text)
        self.assertIn("Légende indexée seule", text)


class ReviewCallbackTest(unittest.TestCase):
    """Encodage des boutons de revue — Telegram plafonne un `callback_data` à 64 octets."""

    MEDIA_ID = "8f14e45f-ceea-467a-9c1c-1a2b3c4d5e6f"

    def test_round_trip(self):
        data = telegram_media.review_callback_data(self.MEDIA_ID, "no")
        self.assertEqual(telegram_media.parse_review_callback(data), ("no", self.MEDIA_ID))

    def test_every_verdict_fits_in_the_telegram_limit(self):
        """Un identifiant réel (~36 caractères) doit laisser la place au verdict."""
        for verdict in telegram_media.REVIEW_VERDICTS:
            data = telegram_media.review_callback_data(self.MEDIA_ID, verdict)
            self.assertLessEqual(len(data.encode()), 64, f"{verdict} ne tient pas")

    def test_unknown_verdict_is_refused_when_building(self):
        with self.assertRaises(ValueError):
            telegram_media.review_callback_data(self.MEDIA_ID, "maybe")

    def test_foreign_callbacks_are_not_ours(self):
        """Sinon le handler de revue capterait les boutons de signal ou RAG."""
        for data in (None, "", "approve:12", "reject:12", "rag:run", "rag:new:BTC-USD"):
            self.assertIsNone(telegram_media.parse_review_callback(data), data)

    def test_malformed_callbacks_are_refused(self):
        for data in ("med", "med:ok", "med:maybe:id", "med:ok:", "xmed:ok:id", "med:ok:   "):
            self.assertIsNone(telegram_media.parse_review_callback(data), data)


class ReviewTargetTest(unittest.TestCase):
    """On ne propose des boutons que s'il y a **quelque chose à relire**."""

    def _result(self, **overrides):
        base = {"ok": True, "media": {"id": "m1"}, "extraction": {"chunks": 2}}
        base.update(overrides)
        return base

    def test_a_successful_extraction_is_reviewable(self):
        self.assertEqual(telegram_media.review_target(self._result()), "m1")

    def test_a_caption_only_indexation_is_reviewable(self):
        """L'extraction a échoué mais des morceaux existent : il y a à relire."""
        result = self._result(extraction={"ok": False, "chunks": 1, "reason": "clé absente"})
        self.assertEqual(telegram_media.review_target(result), "m1")

    def test_nothing_indexed_is_not_reviewable(self):
        self.assertIsNone(telegram_media.review_target(self._result(extraction={"chunks": 0})))

    def test_a_duplicate_is_not_reviewable(self):
        """Déjà stocké : il n'y a ni extraction neuve, ni morceau à retirer."""
        self.assertIsNone(telegram_media.review_target(self._result(duplicate=True)))

    def test_a_failed_ingestion_is_not_reviewable(self):
        self.assertIsNone(
            telegram_media.review_target({"ok": False, "reason": "too_large", "size": 1})
        )

    def test_a_missing_media_id_is_not_reviewable(self):
        self.assertIsNone(telegram_media.review_target(self._result(media=None)))
        self.assertIsNone(telegram_media.review_target(self._result(media={"id": None})))


class ReviewButtonsTest(unittest.TestCase):
    def test_every_status_has_a_label(self):
        """Sinon `/media` afficherait un verdict sans mot pour le dire."""
        for status in telegram_media.media_store.REVIEW_STATUSES:
            self.assertIn(status, telegram_media.REVIEW_LABELS, status)
        self.assertEqual(
            set(telegram_media.REVIEW_LABELS), set(telegram_media.media_store.REVIEW_STATUSES)
        )

    def test_a_button_leads_back_to_the_media_it_came_from(self):
        """Le tour complet : ingestion → bouton → verdict sur le bon média."""
        result = {"ok": True, "media": {"id": "m1"}, "extraction": {"chunks": 2}}
        media_id = telegram_media.review_target(result)
        for _, data in telegram_media.review_buttons(media_id):
            self.assertEqual(telegram_media.parse_review_callback(data)[1], media_id)

    def test_reject_announces_what_it_destroys(self):
        """Le seul bouton destructeur du bot ne doit pas se découvrir après le clic."""
        labels = {data: label for label, data in telegram_media.review_buttons("m1")}
        reject = labels[telegram_media.review_callback_data("m1", "no")]
        self.assertIn("index", reject)
        self.assertIn("Rejeter", reject)

    def test_restore_button_offers_the_way_back(self):
        buttons = telegram_media.restore_buttons("m1")
        self.assertEqual(len(buttons), 1)
        self.assertEqual(
            telegram_media.parse_review_callback(buttons[0][1]), ("re", "m1")
        )

    def test_follow_up_buttons_depend_on_what_the_verdict_did(self):
        """La politique de clavier : chaque cas a une sortie cohérente."""
        rejected = {"ok": True, "verdict": "no", "media_id": "m1", "removed": 3}
        validated = {"ok": True, "verdict": "ok", "media_id": "m1"}
        reindexed = {"ok": True, "verdict": "re", "media_id": "m1", "chunks": 2}
        empty = {"ok": True, "verdict": "re", "media_id": "m1", "chunks": 0}
        failed = {"ok": False, "reason": "delete_failed", "media_id": "m1", "verdict": "no"}
        gone = {"ok": False, "reason": "not_found", "media_id": "m1", "verdict": "no"}

        def verdicts(result):
            return [
                telegram_media.parse_review_callback(data)[0]
                for _, data in telegram_media.follow_up_buttons(result)
            ]

        self.assertEqual(verdicts(rejected), ["re"], "un rejet doit pouvoir être défait")
        self.assertEqual(verdicts(validated), ["no"], "valider reste réversible")
        self.assertEqual(
            verdicts(reindexed), ["ok", "no"], "une extraction neuve doit être relue"
        )
        self.assertEqual(verdicts(empty), ["re"], "rien n'a été indexé : à retenter")
        self.assertEqual(verdicts(failed), ["ok", "no"], "l'action a échoué : réessayer")
        self.assertEqual(verdicts(gone), [], "aucun verdict ne pourrait aboutir")

    def test_follow_up_buttons_are_empty_without_a_media_id(self):
        self.assertEqual(
            telegram_media.follow_up_buttons({"ok": False, "reason": "bad_verdict"}), []
        )


class ReviewMediaTest(unittest.IsolatedAsyncioTestCase):
    """Le verdict : valider, **retirer de l'index**, ou refaire l'extraction."""

    ROW = {
        "id": "m1",
        "storage_path": "telegram/canal/42-photo.jpg",
        "media_type": "photo",
        "file_name": "photo_42.jpg",
        "mime_type": "image/jpeg",
        "caption": "BTC support 64k",
        "width": 800,
        "height": 600,
        "duration_seconds": None,
        "telegram_file_id": "fid",
        "metadata": {"telegram": True},
    }

    def _harness(self, **overrides):
        """Doublures de toutes les dépendances injectables de `review_media`."""
        row = overrides.pop("row", dict(self.ROW))
        text = overrides.pop("text", "cassure confirmée")
        calls = {}

        def fetch(media_id):
            calls["fetch"] = media_id
            if isinstance(row, Exception):
                raise row
            return row

        def remove(media_id):
            calls["remove"] = media_id
            if overrides.get("delete_error"):
                raise overrides["delete_error"]
            return overrides.get("removed", 3)

        def download(path):
            calls["download"] = path
            if overrides.get("download_error"):
                raise overrides["download_error"]
            return b"img"

        def extract(data, **kwargs):
            calls["extract"] = (data, kwargs)
            if overrides.get("extract") is not None:
                return overrides["extract"]
            return {"ok": True, "text": text, "method": "gemini_vision", "reason": None}

        def store(media_id, content, asset=None):
            calls["store"] = (media_id, content)
            return overrides.get("stored", 2)

        def mark(media_id, status, **kwargs):
            calls["mark"] = (media_id, status, kwargs)
            if overrides.get("mark_error"):
                raise overrides["mark_error"]
            return {"id": media_id}

        def record(media_id, summary):
            calls["record"] = (media_id, summary)
            return {}

        return calls, {
            "fetch": fetch,
            "download": download,
            "extract": extract,
            "store": store,
            "remove": remove,
            "mark": mark,
            "record": record,
        }

    async def _review(self, verdict, **overrides):
        calls, deps = self._harness(**overrides)
        result = await telegram_media.review_media(
            "m1", verdict, reviewer="42", **deps
        )
        return calls, deps, result

    async def test_validation_keeps_the_chunks_and_records_the_verdict(self):
        calls, _, result = await self._review("ok")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "validated")
        self.assertNotIn("remove", calls, "valider ne doit rien supprimer")
        self.assertEqual(calls["mark"][1], "validated")
        self.assertEqual(calls["mark"][2]["reviewer"], "42")

    async def test_rejection_removes_the_chunks_then_records_the_verdict(self):
        calls, _, result = await self._review("no")
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], 3)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(calls["remove"], "m1")
        self.assertEqual(calls["mark"], ("m1", "rejected", {"reviewer": "42", "chunks": 0}))
        self.assertNotIn("download", calls, "rejeter ne relit pas le fichier")

    async def test_a_rejection_still_worked_when_the_verdict_cannot_be_saved(self):
        """Les morceaux sont partis : l'annotation ratée ne doit pas dire le contraire."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            _, _, result = await self._review("no", mark_error=RuntimeError("db down"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], 3)
        self.assertIn("db down", out.getvalue())

    async def test_a_failed_deletion_is_reported_without_a_verdict(self):
        calls, _, result = await self._review("no", delete_error=RuntimeError("rls"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "delete_failed")
        self.assertEqual(result["media_id"], "m1")
        self.assertNotIn("mark", calls, "rien n'a été retiré : ne pas l'annoncer validé")

    async def test_rejection_removing_nothing_is_still_a_rejection(self):
        """Idempotent : re-cliquer sur `no` ne doit pas échouer."""
        _, _, result = await self._review("no", removed=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], 0)

    async def test_reindexing_rebuilds_the_extraction_from_the_stored_file(self):
        calls, _, result = await self._review("re")
        self.assertTrue(result["ok"])
        self.assertEqual(result["chunks"], 2)
        self.assertEqual(calls["download"], "telegram/canal/42-photo.jpg")
        _, kwargs = calls["extract"]
        self.assertEqual(kwargs["media_type"], "photo")
        self.assertEqual(kwargs["mime_type"], "image/jpeg")
        self.assertEqual(kwargs["file_name"], "photo_42.jpg")
        media_id, content = calls["store"]
        self.assertEqual(media_id, "m1")
        # La légende est réindexée **avec** le contenu : la perdre rendrait
        # « BTC support 64k » introuvable alors qu'elle nomme l'actif.
        self.assertTrue(content.startswith("BTC support 64k"))
        self.assertIn("cassure confirmée", content)
        self.assertEqual(calls["mark"][1], "validated")
        self.assertEqual(calls["mark"][2]["chunks"], 2)

    async def test_reindexing_notes_the_result_of_the_new_attempt(self):
        """Sans cette note, l'échec de la tentative ne survivrait pas au message."""
        calls, _, _ = await self._review("re")
        media_id, summary = calls["record"]
        self.assertEqual(media_id, "m1")
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["chunks"], 2)
        self.assertEqual(summary["method"], "gemini_vision")

    async def test_a_rejected_extraction_is_not_reindexed(self):
        """Rejeter ne relit pas l'objet : c'est le bouton « ↩️ » qui le fera."""
        calls, _, _ = await self._review("no")
        self.assertNotIn("record", calls)

    async def test_reindexing_without_a_stored_object_is_refused(self):
        row = {**self.ROW, "storage_path": None}
        calls, _, result = await self._review("re", row=row)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_object")
        self.assertNotIn("download", calls)

    async def test_a_failed_download_is_reported(self):
        _, _, result = await self._review("re", download_error=RuntimeError("404"))
        self.assertEqual(result["reason"], "download_failed")
        self.assertIn("404", result["error"])

    async def test_reindexing_indexes_the_caption_alone_when_nothing_is_readable(self):
        """Un PDF scanné garde une légende cherchable — c'est elle qui nomme l'actif."""
        _, _, result = await self._review(
            "re", extract={"ok": False, "text": "", "method": "pypdf", "reason": "scanné"}
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["chunks"], 2)
        self.assertFalse(result["extraction"]["ok"])
        self.assertEqual(result["extraction"]["reason"], "scanné")

    async def test_reindexing_that_reads_nothing_at_all_is_offered_again(self):
        """Sans légende ni texte, rien n'est indexé : le seul recours est de retenter."""
        row = {**self.ROW, "caption": None}
        _, _, result = await self._review(
            "re",
            row=row,
            extract={"ok": False, "text": "", "method": "pypdf", "reason": "scanné"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(
            [
                telegram_media.parse_review_callback(data)[0]
                for _, data in telegram_media.follow_up_buttons(result)
            ],
            ["re"],
        )

    async def test_unknown_media_is_reported(self):
        _, _, result = await self._review("no", row=None)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not_found")

    async def test_a_failed_lookup_is_reported(self):
        _, _, result = await self._review("ok", row=RuntimeError("timeout"))
        self.assertEqual(result["reason"], "lookup_failed")
        self.assertIn("timeout", result["error"])

    async def test_unknown_verdict_is_refused_before_any_io(self):
        calls, _, result = await self._review("peut-être")
        self.assertEqual(result["reason"], "bad_verdict")
        self.assertEqual(calls, {})


class _ReviewClient:
    """Client Supabase minimal pour la revue : chaîne PostgREST + journal des appels.

    Une seule table suffit : les deux lectures se distinguent par leurs colonnes
    (`select("*")` est le média, `select("id")` sont ses morceaux) — c'est
    exactement ce que fait le code réel, et l'assertion porte donc sur un détail
    significatif plutôt que sur un compteur.
    """

    def __init__(self, row, *, chunks=2):
        self.row = dict(row)
        self.chunk_count = chunks
        self.tables = []
        self.selections = []
        self.filters = []
        self.updates = []
        self.deletions = []
        self._columns = None
        self._pending_update = None

    def table(self, name):
        self.tables.append(name)
        return self

    def select(self, *columns):
        self._columns = columns
        self._pending_update = None
        self.selections.append(columns)
        return self

    def update(self, payload):
        self._columns = None
        self._pending_update = payload
        self.updates.append(payload)
        return self

    def delete(self):
        self.deletions.append(self._columns)
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def limit(self, count):
        return self

    def execute(self):
        if self._pending_update is not None:
            return SimpleNamespace(data=[{**self.row, **self._pending_update}])
        if self._columns == ("*",):
            return SimpleNamespace(data=[self.row])
        return SimpleNamespace(data=[{"id": f"c{i}"} for i in range(self.chunk_count)])


class ReviewMediaIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """Le chemin **réel** jusqu'à Supabase — seul le client est remplacé.

    Les tests ci-dessus injectent chaque dépendance une à une : ici, on laisse les
    fonctions réelles (`media_store.get_media`, `set_review_status`,
    `knowledge_index.delete_chunks`) s'enchaîner. C'est précisément là que se
    perdent les arguments d'un module à l'autre — et ce que le rejet doit
    réellement supprimer, ce sont les morceaux, pas la ligne média.
    """

    ROW = {
        "id": "m1",
        "media_type": "photo",
        "storage_path": "telegram/canal/1-a.jpg",
        "metadata": {"telegram": True, "media_group_id": "7"},
    }

    def _patches(self, client):
        return (
            mock.patch.object(telegram_media.media_store, "supabase", client),
            mock.patch.object(telegram_media.knowledge_index, "supabase", client),
        )

    async def test_a_rejection_deletes_the_indexed_chunks_of_that_media(self):
        client = _ReviewClient(self.ROW, chunks=3)
        get, delete = self._patches(client)
        with get, delete:
            result = await telegram_media.review_media("m1", "no", reviewer="42")

        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], 3)
        self.assertEqual(set(client.tables), {"knowledge_media", "knowledge_chunks"})
        self.assertIn(("media_id", "m1"), client.filters, "suppression non filtrée")
        self.assertEqual(client.deletions, [("id",)])

        metadata = client.updates[0]["metadata"]
        self.assertEqual(metadata[telegram_media.media_store.REVIEW_KEY]["status"], "rejected")
        self.assertTrue(metadata["telegram"], "l'origine du média a été écrasée")
        self.assertEqual(metadata["media_group_id"], "7")
        self.assertEqual(telegram_media.media_store.review_status(result["media"]), "rejected")

    async def test_a_validation_touches_no_chunk_and_no_object(self):
        client = _ReviewClient(self.ROW)
        get, delete = self._patches(client)
        with get, delete:
            result = await telegram_media.review_media("m1", "ok", reviewer="42")

        self.assertTrue(result["ok"])
        self.assertEqual(client.deletions, [], "valider ne supprime rien")
        self.assertEqual(
            set(client.tables), {"knowledge_media"}, "valider ne touche jamais les morceaux"
        )
        status = client.updates[0]["metadata"][telegram_media.media_store.REVIEW_KEY]["status"]
        self.assertEqual(status, "validated")
        # Le média rendu porte le verdict : la ligne d'avant le clic ne le dirait pas.
        self.assertEqual(telegram_media.media_store.review_status(result["media"]), "validated")


class FormatReviewReportTest(unittest.TestCase):
    """Ce que l'utilisateur lit après un clic : ce qui a changé, et le chemin retour."""

    def test_rejection_states_the_count_and_the_way_back(self):
        text = telegram_media.format_review_report(
            {"ok": True, "verdict": "no", "media_id": "m1", "removed": 3}
        )
        self.assertIn("3 morceau(x) retiré(s)", text)
        self.assertIn("/search", text, "il faut dire où le contenu disparaît")
        self.assertIn("reste dans le stockage", text)
        self.assertIn("déjà stocké", text, "renvoyer le fichier ne réindexerait rien")

    def test_validation_says_the_content_stays_indexed(self):
        text = telegram_media.format_review_report(
            {"ok": True, "verdict": "ok", "media_id": "m1"}
        )
        self.assertIn("validée", text)
        self.assertIn("/media", text)

    def test_reindexing_reports_the_fresh_extraction(self):
        text = telegram_media.format_review_report(
            {
                "ok": True,
                "verdict": "re",
                "media_id": "m1",
                "chunks": 2,
                "extraction": {
                    "ok": True,
                    "method": "whisper",
                    "chars": 120,
                    "has_caption": True,
                    "excerpt": "cassure confirmée",
                },
            }
        )
        self.assertIn("whisper + légende", text)
        self.assertIn("cassure confirmée", text)

    def test_a_long_reindexed_extraction_also_goes_to_the_attachment(self):
        """Une extraction refaite peut être longue à son tour : même traitement."""
        text = "transcription " * 100
        result = {
            "ok": True,
            "verdict": "re",
            "media_id": "m1",
            "media": {"id": "m1", "file_name": "vocal.ogg"},
            "chunks": 4,
            "extraction": {
                "ok": True,
                "method": "whisper",
                "chars": len(text),
                "chunks": 4,
                "text": text,
                "excerpt": telegram_media._excerpt(text),
            },
        }
        attachment = telegram_media.attachment_for(result)
        self.assertEqual(attachment["filename"], "vocal-extraction.txt")
        self.assertEqual(attachment["content"].decode("utf-8"), text)
        report = telegram_media.format_review_report(result)
        self.assertIn("pièce jointe", report)
        self.assertIn("vocal-extraction.txt", report)
        self.assertNotIn("…", report)

    def test_reindexing_that_reads_nothing_says_so(self):
        text = telegram_media.format_review_report(
            {
                "ok": True,
                "verdict": "re",
                "media_id": "m1",
                "chunks": 0,
                "extraction": {"ok": False, "reason": "scanné", "chunks": 0},
            }
        )
        self.assertIn("scanné", text)
        self.assertIn("aucun texte", text)

    def test_every_failure_has_its_own_message(self):
        """Chaque panne a son remède : les confondre ferait perdre du temps."""
        cases = {
            "not_found": "introuvable",
            "no_object": "stockage",
            "delete_failed": "Suppression",
            "download_failed": "Relecture",
            "lookup_failed": "Lecture du média",
            "mark_failed": "verdict",
        }
        texts = {
            reason: telegram_media.format_review_report(
                {"ok": False, "reason": reason, "error": "détail", "media_id": "m1"}
            )
            for reason in cases
        }
        for reason, needle in cases.items():
            with self.subTest(reason=reason):
                self.assertIn(needle, texts[reason])
        self.assertEqual(len(set(texts.values())), len(cases), "deux pannes se confondent")

    def test_a_failure_names_the_underlying_error_when_there_is_one(self):
        """Sans le message d'origine, un échec Supabase ne se diagnostique pas."""
        for reason in ("delete_failed", "download_failed", "lookup_failed", "mark_failed"):
            with self.subTest(reason=reason):
                text = telegram_media.format_review_report(
                    {"ok": False, "reason": reason, "error": "détail technique"}
                )
                self.assertIn("détail technique", text)

    def test_an_unexpected_failure_still_answers(self):
        text = telegram_media.format_review_report({"ok": False, "reason": "boom"})
        self.assertIn("boom", text)


class TranscribeArgsTest(unittest.TestCase):
    """`/transcribe` accepte une référence — ou rien, s'il y a une réponse."""

    def test_a_reference_alone_is_enough(self):
        self.assertEqual(
            telegram_media.parse_transcribe_args([" 8f14 "])["reference"], " 8f14 "
        )

    def test_no_argument_is_not_an_error(self):
        """Contrairement à `/tag`, qui exige un actif : la réponse peut suffire.

        C'est le **handler** qui décide quand l'absence d'argument devient une
        demande d'aide — quand il n'y a rien à quoi répondre non plus. Le
        décider ici refuserait `/transcribe` en réponse à un média.
        """
        parsed = telegram_media.parse_transcribe_args([])
        self.assertTrue(parsed["ok"])
        self.assertIsNone(parsed["reference"])

    def test_help_is_asked_for_without_a_media(self):
        for token in ("--help", "-h", "help"):
            with self.subTest(token=token):
                self.assertEqual(
                    telegram_media.parse_transcribe_args([token])["reason"], "help"
                )

    def test_extra_arguments_are_refused_rather_than_ignored(self):
        """Une référence avalée en silence relancerait le mauvais média."""
        parsed = telegram_media.parse_transcribe_args(["a", "b"])
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["reason"], "too_many")

    def test_the_help_names_every_form_and_the_reason_to_use_it(self):
        help_text = telegram_media.format_transcribe_help()
        self.assertIn("/transcribe <référence>", help_text)
        self.assertIn("réponse", help_text)
        self.assertIn("GROQ_API_KEY", help_text)
        self.assertIn("GEMINI_API_KEY", help_text)
        self.assertIn("déjà stocké", help_text)

    def test_a_problem_is_announced_before_the_help(self):
        self.assertTrue(
            telegram_media.format_transcribe_help({"reason": "no_reference"}).startswith(
                "⚠️"
            )
        )
        self.assertTrue(
            telegram_media.format_transcribe_help({"reason": "too_many"}).startswith("⚠️")
        )
        self.assertIn(
            "pas de média",
            telegram_media.format_transcribe_help({"reason": "no_media_in_reply"}),
        )

    def test_the_same_problem_reads_differently_in_each_command(self):
        """Les échecs sont communs ; la façon de désigner le média, non."""
        tag = telegram_media.format_tag_help({"reason": "no_reference"})
        transcribe = telegram_media.format_transcribe_help({"reason": "no_reference"})
        self.assertIn("/tag BTC-USD", tag)
        self.assertIn("/transcribe", transcribe)
        self.assertNotIn("/tag", transcribe)


class RetranscribeMediaTest(unittest.IsolatedAsyncioTestCase):
    """`/transcribe` : vérifier les ingrédients **avant** de refaire le travail."""

    ROW = {
        "id": "m1",
        "storage_path": "telegram/chat/42-voice.ogg",
        "media_type": "voice",
        "file_name": "voice_42.ogg",
        "mime_type": "audio/ogg",
        "caption": "point BTC",
        "metadata": {"telegram": True},
    }

    def _harness(self, **overrides):
        row = overrides.pop("row", dict(self.ROW))
        calls = []

        def fetch(media_id):
            calls.append("fetch")
            if isinstance(row, Exception):
                raise row
            return row

        def download(path):
            calls.append("download")
            return b"audio"

        def extract(data, **kwargs):
            calls.append("extract")
            return {"ok": True, "text": "le bitcoin casse les 64k", "method": "groq_whisper"}

        def store(media_id, content, asset=None):
            calls.append("store")
            return 2

        def mark(media_id, status, **kwargs):
            calls.append("mark")
            return {"id": media_id, "metadata": {}}

        def record(media_id, summary):
            calls.append("record")
            return {}

        def tag_store(media_id, asset, **kwargs):
            calls.append("tag")
            return {}

        deps = {
            "fetch": fetch,
            "download": download,
            "extract": extract,
            "store": store,
            "mark": mark,
            "record": record,
            "tag_store": tag_store,
        }
        deps.update(overrides)
        return calls, deps

    async def test_it_reads_the_line_once_and_hands_it_to_the_shared_work(self):
        calls, deps = self._harness()
        with mock.patch.object(telegram_media.media_extractor, "GROQ_API_KEY", "cle"):
            result = await telegram_media.retranscribe_media("m1", reviewer="42", **deps)

        self.assertTrue(result["ok"])
        self.assertEqual(calls.count("fetch"), 1, "la ligne ne doit être lue qu'une fois")
        # L'ordre compte : on indexe **puis** on note (le nombre de morceaux en fait
        # partie), et l'étiquette est reposée avant le verdict.
        self.assertEqual(
            calls[1:], ["download", "extract", "store", "record", "tag", "mark"]
        )
        self.assertEqual(result["chunks"], 2)
        self.assertEqual(result["status"], "validated")

    async def test_the_result_looks_like_a_reindex_for_the_follow_up_keyboard(self):
        """`re` dit exactement ce qui vient d'être fait : l'extraction est refaite."""
        _, deps = self._harness()
        with mock.patch.object(telegram_media.media_extractor, "GROQ_API_KEY", "cle"):
            result = await telegram_media.retranscribe_media("m1", reviewer="42", **deps)

        self.assertEqual(result["verdict"], "re")
        self.assertEqual(
            [
                telegram_media.parse_review_callback(data)[0]
                for _, data in telegram_media.follow_up_buttons(result)
            ],
            ["ok", "no"],
            "une extraction neuve se relit",
        )

    async def test_a_missing_key_refuses_before_touching_the_object(self):
        """Le cas de la commande : la clef manque **encore**, on ne relit rien."""
        calls, deps = self._harness(
            row={**self.ROW, "media_type": "photo", "mime_type": "image/jpeg"}
        )
        with mock.patch.object(telegram_media.media_extractor, "GEMINI_API_KEY", ""):
            result = await telegram_media.retranscribe_media("m1", reviewer="42", **deps)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not_ready")
        self.assertEqual(calls, ["fetch"], "rien n'a été lu ni écrit")
        self.assertEqual(result["readiness"]["missing"], ["GEMINI_API_KEY"])
        self.assertEqual(result["media"]["id"], "m1", "le refus décrit le média")
        self.assertEqual(
            telegram_media.follow_up_buttons(result), [],
            "un refus avant tout travail n'a rien à faire relire",
        )

    async def test_without_a_key_the_local_fallback_makes_it_possible(self):
        """Sans Groq, le repli local suffit : on ne refuse pas, on prévient."""
        _, deps = self._harness()
        with mock.patch.object(telegram_media.media_extractor, "GROQ_API_KEY", ""), mock.patch.object(
            telegram_media.media_extractor, "local_transcription_available", return_value=True
        ):
            result = await telegram_media.retranscribe_media("m1", reviewer="42", **deps)

        self.assertTrue(result["ok"])
        self.assertEqual(result["readiness"]["voices"], {"groq": False, "local": True})
        self.assertIn("plus lent", result["readiness"]["hint"])

    async def test_the_same_work_as_the_reindex_button(self):
        """Un seul travail, deux portes : les appels produits doivent être identiques.

        C'est la seule façon de vérifier qu'une reprise depuis la commande et une
        reprise depuis le bouton ne se mettent pas à diverger (type, légende,
        étiquette, verdict) sans que rien ne le signale.
        """
        button_calls, button_deps = self._harness()
        command_calls, command_deps = self._harness()
        with mock.patch.object(telegram_media.media_extractor, "GROQ_API_KEY", "cle"):
            from_button = await telegram_media.review_media("m1", "re", **button_deps)
            from_command = await telegram_media.retranscribe_media("m1", **command_deps)

        self.assertEqual(button_calls, command_calls)
        for key in ("ok", "media_id", "chunks", "status", "asset_stored"):
            self.assertEqual(from_button[key], from_command[key], key)
        self.assertEqual(from_button["extraction"]["method"], from_command["extraction"]["method"])

    async def test_the_readiness_travels_with_the_result(self):
        """Le rapport dit dans quelles conditions l'extraction a tourné."""
        _, deps = self._harness()
        with mock.patch.object(telegram_media.media_extractor, "GROQ_API_KEY", "cle"):
            result = await telegram_media.retranscribe_media("m1", **deps)
        self.assertEqual(result["readiness"]["kind"], "audio")
        self.assertTrue(result["readiness"]["voices"]["groq"])

    async def test_an_unknown_media_is_reported(self):
        _, deps = self._harness(row=None)
        result = await telegram_media.retranscribe_media("m1", **deps)
        self.assertEqual(result["reason"], "not_found")

    async def test_a_failing_lookup_is_reported(self):
        _, deps = self._harness(row=RuntimeError("timeout"))
        result = await telegram_media.retranscribe_media("m1", **deps)
        self.assertEqual(result["reason"], "lookup_failed")
        self.assertIn("timeout", result["error"])

    async def test_a_media_without_an_object_is_reported(self):
        _, deps = self._harness(row={**self.ROW, "storage_path": None})
        with mock.patch.object(telegram_media.media_extractor, "GROQ_API_KEY", "cle"):
            result = await telegram_media.retranscribe_media("m1", **deps)
        self.assertEqual(result["reason"], "no_object")

    async def test_an_unsupported_type_is_refused_without_pretending_a_key_is_missing(self):
        _, deps = self._harness(
            row={**self.ROW, "media_type": "document", "mime_type": "application/zip", "file_name": "a.zip"}
        )
        result = await telegram_media.retranscribe_media("m1", **deps)
        self.assertEqual(result["reason"], "not_ready")
        self.assertEqual(result["readiness"]["missing"], [])
        self.assertIn("non pris en charge", result["readiness"]["hint"])


class TranscribeCandidatesTest(unittest.TestCase):
    """`/transcribe` sans argument : quoi rattraper, et ce qui manque."""

    FAILED = {
        "id": "m1",
        "media_type": "voice",
        "file_name": "voice.ogg",
        "mime_type": "audio/ogg",
        "file_size": 2048,
        "created_at": "2026-09-27T10:00:00Z",
        "metadata": {
            "telegram": True,
            "extraction": {"ok": False, "reason": "GROQ_API_KEY absente"},
        },
    }

    def _candidates(self, rows, indexed=(), readiness=None, **kwargs):
        """La vue pour des lignes **injectées** : le balayage de la table est remplacé."""
        kwargs["rows"] = rows
        kwargs["indexed"] = set(indexed)
        if readiness is not None:
            kwargs["readiness"] = readiness
        return telegram_media.transcribe_candidates(**kwargs)

    def _ready(self, **kwargs):
        return {"kind": "audio", "ready": True, "missing": [], "hint": ""}

    def test_a_failed_extraction_is_listed_even_though_chunks_exist(self):
        """Une légende indexée n'est pas du contenu lu : c'est le cas à rattraper."""
        view = self._candidates([self.FAILED], indexed={"m1"}, readiness=self._ready)
        self.assertEqual(view["count"], 1)
        self.assertIn("Référence : m1", view["text"])
        self.assertIn("GROQ_API_KEY absente", view["text"], "le motif dit quoi réparer")

    def test_a_successful_extraction_is_not_proposed(self):
        """Relire ce qui a été lu est une relecture, pas un rattrapage."""
        row = {
            "id": "m2",
            "media_type": "voice",
            "metadata": {"extraction": {"ok": True, "method": "groq_whisper"}},
        }
        view = self._candidates([row], indexed={"m2"}, readiness=self._ready)
        self.assertEqual(view["count"], 0)

    def test_without_a_recorded_outcome_only_an_empty_index_is_listed(self):
        """On ne devine pas : sans note, seul l'index vide est un fait."""
        legacy = {"id": "m3", "media_type": "voice", "metadata": {"telegram": True}}
        self.assertEqual(
            self._candidates([legacy], indexed={"m3"}, readiness=self._ready)["count"], 0
        )
        self.assertEqual(
            self._candidates([legacy], indexed=set(), readiness=self._ready)["count"], 1
        )

    def test_a_media_without_an_identifier_is_skipped(self):
        view = self._candidates(
            [{**self.FAILED, "id": None}], indexed=set(), readiness=self._ready
        )
        self.assertEqual(view["count"], 0)

    def test_a_type_nothing_can_extract_is_not_proposed(self):
        """Le proposer promettrait une réparation que rien ne peut faire."""
        row = {
            **self.FAILED,
            "media_type": "document",
            "mime_type": "application/zip",
            "file_name": "a.zip",
        }
        view = self._candidates([row], indexed=set())
        self.assertEqual(view["count"], 0)
        self.assertIn("Rien à rattraper", view["text"])

    def test_each_line_says_whether_a_retry_can_work(self):
        rows = [self.FAILED, {**self.FAILED, "id": "m4", "media_type": "photo", "mime_type": "image/jpeg"}]

        def readiness(media_type=None, mime_type=None, file_name=None):
            if media_type == "photo":
                return {
                    "kind": "image",
                    "ready": False,
                    "missing": ["GEMINI_API_KEY"],
                    "hint": "…",
                }
            return {
                "kind": "audio",
                "ready": True,
                "missing": [],
                "hint": "repli local",
            }

        text = self._candidates(rows, indexed=set(), readiness=readiness)["text"]
        self.assertIn("⚠️ GEMINI_API_KEY", text)
        self.assertIn("🕒 repli local", text)
        self.assertNotIn("✅", text)

    def test_the_whole_table_is_scanned_not_just_the_last_media_page(self):
        """Un rattrapage ancien est trouvé : borner la lecture le rendait invisible.

        Le cas réel : trente ingestions réussies après un vocal jamais transcrit, et
        `/media` n'en montre que les dix dernières. La liste répondait alors « rien à
        rattraper » sur un média qui en avait besoin.
        """
        old = {**self.FAILED, "id": "vieux"}
        done = [
            {**self.FAILED, "id": f"m{i}", "metadata": {"extraction": {"ok": True}}}
            for i in range(30)
        ]
        view = self._candidates(done + [old], indexed=set(), readiness=self._ready)
        self.assertEqual(view["total"], 1)
        self.assertEqual([entry["row"]["id"] for entry in view["entries"]], ["vieux"])
        self.assertEqual(view["scanned"], 31)

    def test_the_bounded_media_page_is_not_used(self):
        """`list_media` borne sa lecture : c'est exactement le défaut à ne pas reprendre."""
        with mock.patch.object(
            telegram_media.media_store, "list_all_media", return_value=[]
        ), mock.patch.object(
            telegram_media.media_store,
            "list_media",
            side_effect=AssertionError("lecture bornée"),
        ):
            view = telegram_media.transcribe_candidates()
        self.assertIn("Rien à rattraper", view["text"])

    def test_nothing_to_do_says_how_much_was_scanned(self):
        """Sans rattrapage, ce qu'on veut connaître est l'étendue du contrôle.

        Et un stockage **vide** ne se dit pas « les 0 média(s) » : l'étendue du
        contrôle n'a plus rien à mesurer, c'est le stockage qui est vide.
        """
        vide = self._candidates([], indexed=set())
        self.assertIn("Rien à rattraper", vide["text"])
        self.assertIn("/transcribe", vide["text"])
        self.assertIn("aucun média n'est stocké", vide["text"])
        self.assertEqual(vide["scanned"], 0)
        self.assertEqual(vide["keyboard"], [], "rien à parcourir, aucun bouton")

        sans_rattrapage = self._candidates(
            [
                {
                    **self.FAILED,
                    "metadata": {"extraction": {"ok": True}},
                }
            ],
            indexed=set(),
            readiness=self._ready,
        )
        self.assertIn("les 1 média(s) stocké(s)", sans_rattrapage["text"])

    def test_a_listing_failure_becomes_a_message_not_an_exception(self):
        def boom(**kwargs):
            raise RuntimeError("supabase down")

        with mock.patch.object(telegram_media.media_store, "list_all_media", side_effect=boom):
            view = telegram_media.transcribe_candidates()
        self.assertIn("supabase down", view["text"])
        self.assertEqual(view["count"], 0)
        self.assertEqual(view["keyboard"], [])

    def _many(self, count):
        return [{**self.FAILED, "id": f"m{i}"} for i in range(count)]

    def test_a_page_holds_the_configured_number_and_announces_the_rest(self):
        view = self._candidates(self._many(25), indexed=set(), readiness=self._ready)
        self.assertEqual(view["total"], 25)
        self.assertEqual(view["shown"], telegram_media.TRANSCRIBE_PAGE)
        self.assertEqual(len(view["entries"]), telegram_media.TRANSCRIBE_PAGE)
        self.assertIn("25 média(s) à rattraper", view["text"])
        self.assertIn("1–10", view["text"])
        self.assertIn("15 autre(s)", view["text"])

    def test_the_ranks_follow_the_page(self):
        """Le 11e de la liste porte le numéro 11, pas 1 : le rang situe dans l'ensemble."""
        view = self._candidates(
            self._many(25), indexed=set(), readiness=self._ready, offset=10
        )
        self.assertIn("11–20", view["text"])
        numbered = [
            line
            for line in view["text"].splitlines()
            if line[:1].isdigit() and ". " in line
        ]
        self.assertEqual(len(numbered), 10)
        self.assertTrue(numbered[0].startswith("11. "), numbered[0])
        self.assertTrue(numbered[-1].startswith("20. "), numbered[-1])

    def test_the_next_button_carries_the_rank(self):
        view = self._candidates(self._many(25), indexed=set(), readiness=self._ready)
        self.assertEqual(
            [(label, data) for row in view["keyboard"] for label, data in row],
            [("▶️ Suivants", "medt:10")],
        )

    def test_a_middle_page_offers_both_directions_on_one_row(self):
        view = self._candidates(
            self._many(25), indexed=set(), readiness=self._ready, offset=10
        )
        self.assertEqual(len(view["keyboard"]), 1)
        self.assertEqual(
            [label for label, _ in view["keyboard"][0]], ["◀️ Précédents", "▶️ Suivants"]
        )
        self.assertEqual(
            [data for _, data in view["keyboard"][0]], ["medt:0", "medt:20"]
        )

    def test_the_last_page_has_no_next_button_and_no_remainder(self):
        view = self._candidates(
            self._many(25), indexed=set(), readiness=self._ready, offset=20
        )
        self.assertEqual(view["shown"], 5)
        self.assertIn("21–25", view["text"])
        self.assertNotIn("autre(s)", view["text"])
        self.assertEqual(
            [label for row in view["keyboard"] for label, _ in row], ["◀️ Précédents"]
        )

    def test_a_single_page_needs_no_keyboard(self):
        view = self._candidates([self.FAILED], indexed=set(), readiness=self._ready)
        self.assertIn("1 média(s) à rattraper :", view["text"])
        self.assertEqual(view["keyboard"], [], "un clavier qui ne mène nulle part")

    def test_the_index_is_only_consulted_for_rows_without_a_recorded_outcome(self):
        """Un résultat noté tranche seul : interroger l'index pour toute la table
        ferait un `in (…)` de tous les identifiants, pour rien."""
        rows = [
            {**self.FAILED, "id": "noté"},
            {"id": "jamais-noté", "media_type": "voice", "mime_type": "audio/ogg", "metadata": {}},
        ]
        asked = []

        def media_with_chunks(ids):
            asked.extend(ids)
            return set()

        with mock.patch.object(
            telegram_media.knowledge_index, "media_with_chunks", side_effect=media_with_chunks
        ):
            view = telegram_media.transcribe_candidates(rows=rows, readiness=self._ready)
        self.assertEqual(asked, ["jamais-noté"])
        self.assertEqual(view["total"], 2, "les deux lignes restent proposées")


class TranscribePageDataTest(unittest.TestCase):
    """Le rang porté par un bouton de navigation — et rien d'autre."""

    def test_the_prefix_is_disjoint_from_the_review_ones(self):
        """`^med:` ne peut pas suivre le `t` de `medt:` : les deux claviers restent séparés."""
        for prefix in (
            telegram_media.REVIEW_PREFIX,
            telegram_media.LIST_REVIEW_PREFIX,
            telegram_media.PENDING_REVIEW_PREFIX,
        ):
            with self.subTest(prefix=prefix):
                self.assertNotEqual(prefix, telegram_media.TRANSCRIBE_PAGE_PREFIX)
                self.assertFalse(
                    telegram_media.transcribe_page_data(3).startswith(prefix + ":")
                )

    def test_a_round_trip_gives_the_rank_back(self):
        for offset in (0, 1, 25, 1000):
            with self.subTest(offset=offset):
                self.assertEqual(
                    telegram_media.parse_transcribe_page(
                        telegram_media.transcribe_page_data(offset)
                    ),
                    offset,
                )

    def test_a_negative_rank_is_flattened_rather_than_refused(self):
        """`transcribe_page_data` borne à gauche : un rang négatif n'existe pas."""
        self.assertEqual(telegram_media.transcribe_page_data(-5), "medt:0")

    def test_a_foreign_or_broken_payload_is_refused(self):
        """Refuser plutôt que ramener à zéro : un bouton illisible ne doit pas
        réafficher la première page en donnant l'impression d'avoir été compris."""
        for data in (
            None,
            "",
            "med:0",
            "medl:ok:x",
            "medp:0",
            "medt",
            "medt:",
            "medt:x",
            "medt:-3",
            "medt:1:2",
            "rag:3",
        ):
            with self.subTest(data=data):
                self.assertIsNone(telegram_media.parse_transcribe_page(data))

    def test_nothing_to_go_to_means_no_keyboard(self):
        self.assertEqual(telegram_media.transcribe_buttons(offset=0, shown=1, total=1), [])
        self.assertEqual(telegram_media.transcribe_buttons(offset=0, shown=0, total=0), [])

    def test_both_directions_share_one_row(self):
        """Ce ne sont pas des lignes de la liste : ils ne s'alignent sur aucune ligne."""
        rows = telegram_media.transcribe_buttons(offset=10, shown=10, total=40, page=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            [label for label, _ in rows[0]], ["◀️ Précédents", "▶️ Suivants"]
        )


class FormatTranscribeReportTest(unittest.TestCase):
    """Ce que `/transcribe` répond — dans les trois cas qu'il distingue."""

    REFUSED = {
        "ok": False,
        "reason": "not_ready",
        "media_id": "m1",
        "media": {"id": "m1", "media_type": "photo", "file_name": "chart.jpg"},
        "readiness": {
            "kind": "image",
            "ready": False,
            "missing": ["GEMINI_API_KEY"],
            "hint": "Renseigne GEMINI_API_KEY dans `.env`.",
        },
    }

    def test_a_refusal_says_nothing_was_tried_and_what_is_missing(self):
        text = telegram_media.format_transcribe_report(self.REFUSED)
        self.assertIn("Rien n'a été tenté", text)
        self.assertIn("GEMINI_API_KEY", text)
        self.assertIn("Renseigne GEMINI_API_KEY", text)
        self.assertIn("chart.jpg", text, "on doit savoir de quel média on parle")
        self.assertIn("/transcribe <référence>", text)

    def test_a_refusal_for_an_unsupported_type_does_not_invent_a_missing_key(self):
        result = {
            **self.REFUSED,
            "readiness": {
                "kind": None,
                "ready": False,
                "missing": [],
                "hint": "Type non pris en charge (document / application/zip).",
            },
        }
        text = telegram_media.format_transcribe_report(result)
        self.assertIn("n'est pas extractible", text)
        self.assertNotIn("Il manque", text)

    def test_a_reindex_reads_like_the_review_but_says_it_is_reindexed(self):
        result = {
            "ok": True,
            "verdict": "re",
            "media_id": "m1",
            "chunks": 3,
            "asset_stored": True,
            "extraction": {
                "ok": True,
                "method": "groq_whisper",
                "chars": 120,
                "asset": "BTC-USD",
                "asset_source": "caption",
                "excerpt": "le bitcoin casse les 64k",
            },
        }
        text = telegram_media.format_transcribe_report(result)
        review = telegram_media.format_review_report(result)
        self.assertIn("♻️ Extraction refaite et réindexée", text)
        self.assertIn("120 car. (groq_whisper, 3 morceau(x))", text)
        self.assertIn("BTC-USD", text)
        # Le fond est le même que celui de la revue : une seule écriture des faits.
        self.assertIn(
            "120 car. (groq_whisper, 3 morceau(x))", review, "corps partagé avec la revue"
        )

    def test_a_reindex_that_read_nothing_says_so(self):
        result = {
            "ok": True,
            "verdict": "re",
            "media_id": "m1",
            "chunks": 0,
            "extraction": {"ok": False, "reason": "scanné"},
        }
        text = telegram_media.format_transcribe_report(result)
        self.assertIn("♻️ Extraction relancée", text)
        self.assertIn("scanné", text)

    def test_the_other_failures_reuse_the_review_phrases(self):
        """Ce sont les mêmes causes : en écrire une seconde version les ferait diverger."""
        for reason in ("not_found", "no_object", "download_failed", "lookup_failed"):
            with self.subTest(reason=reason):
                result = {"ok": False, "reason": reason, "error": "détail", "media_id": "m1"}
                self.assertEqual(
                    telegram_media.format_transcribe_report(result),
                    telegram_media.format_review_report(result),
                )


class TranscribeCommandWiringTest(unittest.TestCase):
    """`/transcribe`, vu depuis `main.py` (non importable : dépendances lourdes)."""

    MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"

    def _body(self) -> str:
        text = self.MAIN.read_text(encoding="utf-8")
        return text.split("async def transcribe_cmd", 1)[1].split("\nasync def ", 1)[0]

    def test_the_command_is_registered(self):
        self.assertIn(
            'CommandHandler("transcribe", transcribe_cmd)',
            self.MAIN.read_text(encoding="utf-8"),
        )

    def test_the_arguments_are_parsed_by_the_module(self):
        self.assertIn("telegram_media.parse_transcribe_args(context.args", self._body())

    def test_the_replied_media_is_handed_over(self):
        body = self._body()
        self.assertIn("replied=replied", body)
        self.assertIn('reference=parsed["reference"]', body)
        self.assertIn("telegram_media.find_media(", body)

    def test_nothing_designated_lists_what_can_be_repaired(self):
        """Sans référence ni réponse, la question n'est plus « comment » mais « lesquels »."""
        body = self._body()
        self.assertIn("telegram_media.transcribe_candidates", body)
        self.assertIn("parsed[\"reference\"] is None and replied is None", body)

    def test_the_list_is_sent_with_its_pagination_keyboard(self):
        """La liste balaie toute la table : sans clavier, ses pages au-delà de la
        première seraient aussi invisibles que l'ancienne borne."""
        body = self._body()
        self.assertIn('reply_markup=_list_keyboard(view["keyboard"])', body)

    def test_a_bad_invocation_shows_the_help(self):
        body = self._body()
        self.assertIn("telegram_media.format_transcribe_help(parsed)", body)
        self.assertIn("telegram_media.format_transcribe_help(resolved)", body)

    def test_the_retry_is_attributed_to_the_requester(self):
        body = self._body()
        self.assertIn("telegram_media.retranscribe_media(", body)
        self.assertIn("reviewer=str(update.effective_user.id)", body)

    def test_the_report_comes_with_its_keyboard_and_the_full_text(self):
        body = self._body()
        self.assertIn("telegram_media.format_transcribe_report(result)", body)
        self.assertIn("_follow_up_keyboard(result)", body)
        self.assertIn("_send_extracted_text(context.bot, update.message.chat_id, result)", body)

    def test_the_start_message_advertises_the_command(self):
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertIn("/transcribe", text)
        self.assertIn("/transcribe --help", text)


class TranscribePageCallbackWiringTest(unittest.TestCase):
    """Le bouton « ▶️ Suivants » vu depuis `main.py` (non importable : dépendances lourdes)."""

    MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"

    def _body(self, name="media_transcribe_callback") -> str:
        text = self.MAIN.read_text(encoding="utf-8")
        return text.split(f"async def {name}", 1)[1].split("\nasync def ", 1)[0]

    def test_the_callback_is_registered_before_the_generic_handler(self):
        """Sinon `button_handler` verrait `medt:` avant la navigation."""
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertLess(
            text.index(
                'CallbackQueryHandler(media_transcribe_callback, pattern=r"^medt:")'
            ),
            text.index("CallbackQueryHandler(button_handler)"),
        )

    def test_the_rank_is_parsed_by_the_module(self):
        self.assertIn(
            "telegram_media.parse_transcribe_page(query.data)", self._body()
        )

    def test_an_unreadable_button_answers_without_touching_the_message(self):
        body = self._body()
        refusal = body.split("if offset is None:", 1)[1].split("try:", 1)[0]
        self.assertIn("await query.answer(", refusal)
        self.assertNotIn("edit_message_text", refusal)

    def test_the_page_is_recomputed_from_the_rank(self):
        """Aucun état mémorisé : la liste change dès qu'une extraction est relancée."""
        body = self._body()
        self.assertIn("telegram_media.transcribe_candidates, offset=offset", body)
        self.assertIn("asyncio.to_thread", body, "lecture réseau : hors de l'event loop")

    def test_the_reply_replaces_the_page_and_its_keyboard(self):
        body = self._body()
        self.assertIn('reply_markup=_list_keyboard(view["keyboard"])', body)
        self.assertIn("await query.edit_message_text(", body)


class MediaListVerdictTest(unittest.TestCase):
    """`/media` doit montrer ce qui a été relu — sinon un rejet est invisible."""

    def _row(self, **overrides):
        row = {"id": "m1", "media_type": "photo", "file_name": "a.jpg", "file_size": 1}
        row.update(overrides)
        return row

    def test_no_verdict_is_shown_for_a_media_never_reviewed(self):
        text = telegram_media.format_media_list([{"row": self._row(), "url": "u"}])
        self.assertNotIn("extraction", text)

    def test_reviewed_medias_are_labelled(self):
        entries = [
            {
                "row": self._row(
                    metadata={telegram_media.media_store.REVIEW_KEY: {"status": "validated"}}
                ),
                "url": "u",
            },
            {
                "row": self._row(
                    metadata={telegram_media.media_store.REVIEW_KEY: {"status": "rejected"}}
                ),
                "url": "u",
            },
        ]
        text = telegram_media.format_media_list(entries)
        self.assertIn(telegram_media.REVIEW_LABELS["validated"], text)
        self.assertIn(telegram_media.REVIEW_LABELS["rejected"], text)

    def test_the_asset_label_is_shown(self):
        """Un média étiqueté n'est pas « neutre » : il est exclu des autres actifs."""
        row = self._row(metadata={"asset": {"value": "BTC-USD", "source": "manual"}})
        text = telegram_media.format_media_list([{"row": row, "url": "u"}])
        self.assertIn("🏷️ BTC-USD", text)

    def test_an_untagged_media_shows_no_label(self):
        text = telegram_media.format_media_list([{"row": self._row(), "url": "u"}])
        self.assertNotIn("🏷️", text)

    def test_an_unknown_status_is_not_displayed(self):
        """Un `metadata` bricolé à la main ne doit pas inventer un verdict."""
        row = self._row(metadata={"extraction_review": {"status": "bof"}})
        text = telegram_media.format_media_list([{"row": row, "url": "u"}])
        self.assertNotIn("bof", text)

    def test_an_extraction_that_read_nothing_is_visible(self):
        """Sinon la ligne est identique à un média lu dès qu'il porte une légende."""
        row = self._row(
            metadata={
                telegram_media.media_store.OUTCOME_KEY: {
                    "ok": False,
                    "reason": "GEMINI_API_KEY absente : vision indisponible",
                }
            }
        )
        text = telegram_media.format_media_list([{"row": row, "url": "u"}])
        self.assertIn("⚠️ texte non extrait", text)

    def test_a_successful_extraction_does_not_clutter_the_line(self):
        row = self._row(
            metadata={telegram_media.media_store.OUTCOME_KEY: {"ok": True, "method": "pypdf"}}
        )
        text = telegram_media.format_media_list([{"row": row, "url": "u"}])
        self.assertNotIn("⚠️", text)


class ListReviewButtonsTest(unittest.TestCase):
    """Boutons par ligne de `/media` : quoi relire, et sur quelle ligne.

    C'est le rattrapage d'une revue non délivrée : un compte-rendu jamais ouvert
    (canal, message noyé) ne laisse sinon aucune action possible, et l'extraction
    reste dans les prompts sans avoir été relue.
    """

    def _entry(self, *, media_id="m1", verdict=None, indexed=True, metadata=None, **row):
        meta = metadata
        if verdict:
            meta = {telegram_media.media_store.REVIEW_KEY: {"status": verdict}}
        values = {"id": media_id, "media_type": "photo", "file_name": "a.jpg"}
        values.update(row)
        if meta is not None:
            values["metadata"] = meta
        return {"row": values, "url": "u", "indexed": indexed}

    def test_an_unreviewed_indexed_media_can_be_validated_or_rejected(self):
        buttons = telegram_media.list_buttons([self._entry()])
        self.assertEqual(
            buttons,
            [
                [
                    ("✅ 1", telegram_media.list_callback_data("m1", "ok")),
                    ("❌ 1", telegram_media.list_callback_data("m1", "no")),
                ]
            ],
        )

    def test_a_validated_media_keeps_only_the_reject(self):
        """Valider n'est pas irréversible : on garde de quoi défaire un clic de trop."""
        buttons = telegram_media.list_buttons([self._entry(verdict="validated")])
        self.assertEqual(buttons, [[("❌ 1", telegram_media.list_callback_data("m1", "no"))]])

    def test_a_rejected_media_offers_only_the_reindex(self):
        buttons = telegram_media.list_buttons([self._entry(verdict="rejected", indexed=False)])
        self.assertEqual(buttons, [[("↩️ 1", telegram_media.list_callback_data("m1", "re"))]])

    def test_a_rejected_media_is_reindexable_even_if_the_index_read_failed(self):
        """Le verdict dit « rejeté » ; des morceaux restants ne changeraient rien."""
        buttons = telegram_media.list_buttons([self._entry(verdict="rejected", indexed=True)])
        self.assertEqual(buttons, [[("↩️ 1", telegram_media.list_callback_data("m1", "re"))]])

    def test_nothing_indexed_is_not_validatable(self):
        """Il n'y a rien à valider : enregistrer une relecture de rien est le mensonge à éviter."""
        buttons = telegram_media.list_buttons([self._entry(indexed=False)])
        self.assertEqual(buttons, [[("↩️ 1", telegram_media.list_callback_data("m1", "re"))]])

    def test_buttons_are_numbered_like_the_lines_they_change(self):
        entries = [self._entry(media_id=f"m{i}") for i in (1, 2, 3)]
        text = telegram_media.format_media_list(entries)
        ranks_in_text = [line.split(".", 1)[0] for line in text.splitlines() if line[:1].isdigit()]
        ranks_in_buttons = [row[0][0].split()[-1] for row in telegram_media.list_buttons(entries)]
        self.assertEqual(ranks_in_text, ranks_in_buttons)

    def test_a_media_without_id_gets_no_button_without_shifting_the_others(self):
        entries = [self._entry(media_id=None), self._entry(media_id="m2")]
        buttons = telegram_media.list_buttons(entries)
        self.assertEqual(len(buttons), 1)
        self.assertTrue(buttons[0][0][0].endswith(" 2"))

    def test_an_empty_list_has_no_buttons(self):
        self.assertEqual(telegram_media.list_buttons([]), [])

    def test_the_list_prefix_is_refused_by_the_report_parser_and_the_reverse(self):
        """Deux claviers, deux messages à réécrire : un handler ne prend que les siens."""
        data = telegram_media.list_callback_data("m1", "ok")
        self.assertTrue(data.startswith(telegram_media.LIST_REVIEW_PREFIX))
        self.assertIsNone(telegram_media.parse_review_callback(data))
        report_data = telegram_media.review_callback_data("m1", "ok")
        self.assertIsNone(
            telegram_media.parse_review_callback(
                report_data, prefix=telegram_media.LIST_REVIEW_PREFIX
            )
        )
        self.assertEqual(
            telegram_media.parse_review_callback(
                data, prefix=telegram_media.LIST_REVIEW_PREFIX
            ),
            ("ok", "m1"),
        )

    def test_the_prefix_decides_which_list_will_be_rerendered(self):
        """Même politique, mais `medp:` doit ramener `/pending`, pas `/media`."""
        pending = telegram_media.PENDING_REVIEW_PREFIX
        buttons = telegram_media.list_buttons([self._entry()], prefix=pending)
        self.assertEqual(
            buttons,
            [
                [
                    ("✅ 1", telegram_media.list_callback_data("m1", "ok", prefix=pending)),
                    ("❌ 1", telegram_media.list_callback_data("m1", "no", prefix=pending)),
                ]
            ],
        )

    def test_the_three_review_prefixes_are_disjoint(self):
        """Un handler ne doit accepter que ses propres boutons : aucun préfixe ne recouvre un autre."""
        prefixes = (
            telegram_media.REVIEW_PREFIX,
            telegram_media.LIST_REVIEW_PREFIX,
            telegram_media.PENDING_REVIEW_PREFIX,
        )
        self.assertEqual(len(set(prefixes)), 3)
        for prefix in prefixes:
            data = telegram_media.review_callback_data("m1", "ok", prefix=prefix)
            for other in prefixes:
                if other != prefix:
                    self.assertIsNone(telegram_media.parse_review_callback(data, prefix=other))


class ReviewToastTest(unittest.TestCase):
    """Un clic sur une ligne répond court : la liste, elle, est réaffichée."""

    def test_a_validation_is_confirmed(self):
        self.assertIn("validée", telegram_media.review_toast({"ok": True, "verdict": "ok"}))

    def test_a_rejection_says_how_much_left_the_index(self):
        toast = telegram_media.review_toast({"ok": True, "verdict": "no", "removed": 3})
        self.assertIn("3", toast)
        self.assertIn("retiré", toast)

    def test_a_reindex_that_extracted_nothing_says_so(self):
        toast = telegram_media.review_toast({"ok": True, "verdict": "re", "chunks": 0})
        self.assertIn("aucun texte", toast)

    def test_a_reindex_is_confirmed_with_its_chunks(self):
        toast = telegram_media.review_toast({"ok": True, "verdict": "re", "chunks": 4})
        self.assertIn("4", toast)

    def test_a_failure_is_announced_not_hidden(self):
        toast = telegram_media.review_toast({"ok": False, "reason": "delete_failed"})
        self.assertIn("delete_failed", toast)

    def test_the_toast_fits_telegram_banner(self):
        for verdict in ("ok", "no", "re"):
            toast = telegram_media.review_toast(
                {"ok": True, "verdict": verdict, "removed": 99, "chunks": 99}
            )
            self.assertLessEqual(len(toast), 200)


class MediaListViewTest(unittest.TestCase):
    """`media_list_view` : le texte **et** le clavier d'une seule lecture."""

    def _rows(self):
        return [
            {"id": "m1", "storage_path": "p1", "media_type": "photo", "file_name": "a.jpg"},
            {"id": "m2", "storage_path": "p2", "media_type": "document", "file_name": "b.pdf"},
        ]

    def _view(self, *, indexed=(), rows=None, list_error=None, link=None):
        patches = [
            mock.patch.object(
                telegram_media.media_store,
                "list_media",
                return_value=self._rows() if rows is None else rows,
                **({"side_effect": list_error} if list_error else {}),
            ),
            mock.patch.object(
                telegram_media.media_store, "create_signed_url", return_value=link or "https://signed"
            ),
            mock.patch.object(
                telegram_media.knowledge_index, "media_with_chunks", return_value=set(indexed)
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return telegram_media.media_list_view()

    def test_the_view_carries_text_keyboard_and_entries(self):
        view = self._view(indexed=("m1",))
        self.assertIn("a.jpg", view["text"])
        self.assertEqual(len(view["entries"]), 2)
        self.assertEqual(len(view["keyboard"]), 2)

    def test_indexed_media_are_marked_as_such(self):
        """« Il y a du texte à valider » : c'est ce qui décide des boutons proposés."""
        view = self._view(indexed=("m1",))
        self.assertEqual(
            [entry["indexed"] for entry in view["entries"]], [True, False]
        )
        self.assertEqual(view["keyboard"][0][0][0], "✅ 1")
        self.assertEqual(view["keyboard"][1][0][0], "↩️ 2")

    def test_the_button_numbers_are_explained_once(self):
        view = self._view(indexed=("m1",))
        self.assertIn(telegram_media.LIST_REVIEW_HINT, view["text"])

    def test_an_empty_list_has_neither_buttons_nor_hint(self):
        view = self._view(rows=[])
        self.assertIn("Aucun média", view["text"])
        self.assertNotIn(telegram_media.LIST_REVIEW_HINT, view["text"])
        self.assertEqual(view["keyboard"], [])

    def test_a_read_failure_gives_an_error_and_no_buttons(self):
        view = self._view(list_error=RuntimeError("db down"))
        self.assertIn("db down", view["text"])
        self.assertEqual(view["keyboard"], [])
        self.assertEqual(view["entries"], [])

    def test_an_unreadable_index_is_treated_as_nothing_indexed(self):
        """Le doute doit coûter une réindexation, jamais une relecture crue à tort."""
        with mock.patch.object(
            telegram_media.media_store, "list_media", return_value=self._rows()
        ), mock.patch.object(
            telegram_media.media_store, "create_signed_url", return_value="https://signed"
        ), mock.patch.object(
            telegram_media.knowledge_index,
            "media_with_chunks",
            side_effect=RuntimeError("index down"),
        ):
            view = telegram_media.media_list_view()
        self.assertEqual(view["keyboard"][0][0][0], "↩️ 1")

    def test_a_broken_link_does_not_cost_the_review(self):
        with mock.patch.object(
            telegram_media.media_store, "list_media", return_value=self._rows()
        ), mock.patch.object(
            telegram_media.media_store,
            "create_signed_url",
            side_effect=RuntimeError("lien refusé"),
        ), mock.patch.object(
            telegram_media.knowledge_index, "media_with_chunks", return_value={"m1"}
        ):
            view = telegram_media.media_list_view()
        self.assertIn("lien indisponible", view["text"])
        self.assertTrue(view["keyboard"])

    def test_the_text_only_report_still_works(self):
        """`media_list_report` reste le texte seul (appelants sans boutons)."""
        with mock.patch.object(
            telegram_media.media_store, "list_media", return_value=self._rows()
        ), mock.patch.object(
            telegram_media.media_store, "create_signed_url", return_value="https://signed"
        ), mock.patch.object(
            telegram_media.knowledge_index, "media_with_chunks", return_value={"m1"}
        ):
            report = telegram_media.media_list_report()
        self.assertIsInstance(report, str)
        self.assertIn("a.jpg", report)
        self.assertIn(telegram_media.LIST_REVIEW_HINT, report)

    def _many(self, count):
        return [
            {
                "id": f"m{i}",
                "storage_path": f"p{i}",
                "media_type": "photo",
                "file_name": f"{i}.jpg",
                "created_at": "2026-03-01",
            }
            for i in range(count)
        ]

    def _paged(self, rows, **kwargs):
        """Vue paginée : la doublure **respecte** `limit`/`offset`, comme la vraie lecture.

        C'est ce qui permet de vérifier ce que la vue **demande** — le rang, la ligne
        de trop —, et non ce qu'un `return_value` veut bien rendre : une doublure qui
        rend toujours la même page validerait n'importe quel rang.
        """
        calls = []

        def fake_list(**params):
            calls.append(params)
            start = int(params.get("offset") or 0)
            limit = int(params.get("limit") or 0)
            return [dict(row) for row in rows[start : start + limit]]

        with mock.patch.object(
            telegram_media.media_store, "list_media", side_effect=fake_list
        ), mock.patch.object(
            telegram_media.media_store, "create_signed_url", return_value="https://signed"
        ) as sign, mock.patch.object(
            telegram_media.knowledge_index, "media_with_chunks", return_value=set()
        ):
            view = telegram_media.media_list_view(**kwargs)
        return view, calls, sign

    def test_a_page_after_the_first_shows_the_lines_beyond(self):
        """Parcourir la liste : le rang demandé est celui de la première ligne affichée."""
        view, calls, _ = self._paged(self._many(25), limit=10, offset=20)
        self.assertEqual(view["offset"], 20)
        self.assertEqual(
            [entry["row"]["id"] for entry in view["entries"]],
            [f"m{i}" for i in range(20, 25)],
        )
        self.assertIn("lignes 21–25", view["text"])
        self.assertEqual(calls[0]["offset"], 20)
        self.assertEqual(calls[0]["limit"], 11, "une ligne de plus que la page")

    def test_the_line_that_says_there_is_a_next_page_is_not_shown_nor_signed(self):
        """Elle sert à répondre « y a-t-il une suite ? », pas à être lue."""
        view, _, sign = self._paged(self._many(25), limit=10)
        self.assertTrue(view["more"])
        self.assertEqual(len(view["entries"]), 10)
        self.assertEqual(sign.call_count, 10, "un lien signé par ligne affichée")
        self.assertNotIn("m10.jpg", view["text"])

    def test_the_next_page_is_offered_only_when_one_exists(self):
        """Sinon le bouton mènerait une fois sur deux sur une page vide."""
        exact, _, _ = self._paged(self._many(10), limit=10)
        self.assertFalse(exact["more"])
        for row in exact["keyboard"]:
            for _, data in row:
                self.assertFalse(data.startswith(telegram_media.MEDIA_PAGE_PREFIX + ":"))
        following, _, _ = self._paged(self._many(11), limit=10)
        self.assertTrue(following["more"])
        self.assertEqual(
            following["keyboard"][-1], [("▶️ Suivants", telegram_media.media_page_data(10))]
        )

    def test_the_navigation_row_comes_after_the_review_rows(self):
        """Ce ne sont pas des lignes de la liste : ils se lisent après elle."""
        view, _, _ = self._paged(self._many(25), limit=10, offset=10)
        for row in view["keyboard"][:-1]:
            for _, data in row:
                self.assertTrue(data.startswith(telegram_media.LIST_REVIEW_PREFIX + ":"))
        self.assertEqual(
            [label for label, _ in view["keyboard"][-1]],
            ["◀️ Précédents", "▶️ Suivants"],
        )
        self.assertEqual(
            [data for _, data in view["keyboard"][-1]],
            [telegram_media.media_page_data(0), telegram_media.media_page_data(20)],
        )

    def test_the_first_page_with_a_next_one_keeps_its_numbers(self):
        """La numérotation repart à 1 à chaque page : les boutons désignent **ses** lignes."""
        view, _, _ = self._paged(self._many(25), limit=10, offset=10)
        self.assertIn("1. photo · 10.jpg", view["text"])
        self.assertEqual(view["keyboard"][0][0][0], "↩️ 1")

    def test_a_rank_beyond_the_end_leaves_a_way_back(self):
        """La liste change sous les doigts : une page vide serait exacte et muette.

        La vue ne peut pas calculer la dernière page réelle comme le fait
        `_pending_start` — la lecture est bornée, donc le total est inconnu —, mais
        elle ne laisse jamais l'opérateur sans bouton.
        """
        view, calls, _ = self._paged(self._many(3), limit=10, offset=30)
        self.assertEqual(view["entries"], [])
        self.assertEqual(view["offset"], 30)
        self.assertIn("Rien à ces rangs", view["text"])
        self.assertEqual(view["keyboard"], [[("◀️ Précédents", "medlg:20")]])
        self.assertEqual(calls[0]["offset"], 30, "le rang demandé est bien celui-là")

    def test_a_read_failure_on_a_later_page_is_still_a_message(self):
        with mock.patch.object(
            telegram_media.media_store, "list_media", side_effect=RuntimeError("db down")
        ):
            view = telegram_media.media_list_view(offset=10)
        self.assertIn("db down", view["text"])
        self.assertEqual(view["keyboard"], [])
        self.assertEqual(view["offset"], 10, "le rang demandé reste lisible")


class MediaListWiringTest(unittest.TestCase):
    """Le câblage de `/media` et `/pending` dans `main.py` (module non importable ici)."""

    MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"

    def _body(self, name: str) -> str:
        text = self.MAIN.read_text(encoding="utf-8")
        return text.split(f"async def {name}", 1)[1].split("\nasync def ", 1)[0]

    def test_the_list_command_sends_the_keyboard(self):
        body = self._body("media_cmd")
        self.assertIn("telegram_media.media_list_view", body)
        self.assertIn('reply_markup=_list_keyboard(view["keyboard"])', body)

    def test_the_pending_command_sends_the_keyboard(self):
        """`/pending` est une **liste** revue, pas un compte-rendu : mêmes boutons."""
        body = self._body("pending_cmd")
        self.assertIn("telegram_media.pending_review_view", body)
        self.assertIn('reply_markup=_list_keyboard(view["keyboard"])', body)

    def test_the_pending_command_is_registered_and_documented(self):
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertIn('CommandHandler("pending", pending_cmd)', text)
        self.assertIn("/pending", self._body("start"))

    def test_each_list_callback_accepts_only_its_own_prefix(self):
        """`medl:` et `medp:` désignent le même geste : un handler n'en prend qu'un."""
        self.assertIn(
            "prefix=telegram_media.LIST_REVIEW_PREFIX", self._body("media_list_callback")
        )
        self.assertIn(
            "prefix=telegram_media.PENDING_REVIEW_PREFIX",
            self._body("media_pending_callback"),
        )

    def test_the_verdict_body_is_shared_by_the_two_lists(self):
        """Un seul corps : deux copies finiraient par diverger sur le repli."""
        self.assertIn("telegram_media.review_media(", self._body("_review_from_list"))
        for name in ("media_list_callback", "media_pending_callback"):
            self.assertIn("_review_from_list(", self._body(name))

    def test_the_shared_body_rerenders_a_list_instead_of_replacing_it(self):
        """Remplacer la liste ferait perdre les boutons des lignes pas encore relues."""
        body = self._body("_review_from_list")
        self.assertIn('reply_markup=_list_keyboard(rendered["keyboard"])', body)
        self.assertIn("telegram_media.review_toast(result)", body)

    def test_each_list_callback_rerenders_its_own_view(self):
        self.assertIn("view=telegram_media.media_list_view", self._body("media_list_callback"))
        self.assertIn(
            "view=telegram_media.pending_review_view",
            self._body("media_pending_callback"),
        )

    def test_a_failed_rerender_still_reports_the_verdict(self):
        body = self._body("_review_from_list")
        self.assertIn("except Exception", body)
        self.assertIn("telegram_media.format_review_report(result)", body)

    def test_a_reindex_from_the_list_gets_the_same_attachment(self):
        body = self._body("_review_from_list")
        self.assertIn("_send_extracted_text(context.bot, update.effective_chat.id, result)", body)

    def test_each_list_callback_precedes_the_generic_handler(self):
        """Sinon `button_handler` verrait `medl:`/`medp:` avant les revues de liste."""
        text = self.MAIN.read_text(encoding="utf-8")
        generic = text.index("CallbackQueryHandler(button_handler)")
        for line in (
            'CallbackQueryHandler(media_list_callback, pattern=r"^medl:")',
            'CallbackQueryHandler(media_pending_callback, pattern=r"^medp:")',
        ):
            self.assertLess(text.index(line), generic)


class MediaPageCallbackWiringTest(unittest.TestCase):
    """La navigation de `/media` vue depuis `main.py` (module non importable ici)."""

    MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"

    def _body(self, name="media_list_page_callback") -> str:
        text = self.MAIN.read_text(encoding="utf-8")
        return text.split(f"async def {name}", 1)[1].split("\nasync def ", 1)[0]

    def test_the_callback_is_registered_before_the_generic_handler(self):
        """Sinon `button_handler` verrait `medlg:` avant la navigation."""
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertLess(
            text.index('CallbackQueryHandler(media_list_page_callback, pattern=r"^medlg:")'),
            text.index("CallbackQueryHandler(button_handler)"),
        )

    def test_the_rank_is_parsed_by_the_module(self):
        self.assertIn("telegram_media.parse_media_page(query.data)", self._body())

    def test_an_unreadable_button_answers_without_touching_the_message(self):
        body = self._body()
        refusal = body.split("if offset is None:", 1)[1].split("try:", 1)[0]
        self.assertIn("await query.answer(", refusal)
        self.assertNotIn("edit_message_text", refusal)

    def test_the_page_is_recomputed_from_the_rank(self):
        """Aucun état mémorisé : une ingestion ajoute des médias en tête de liste."""
        body = self._body()
        self.assertIn("telegram_media.media_list_view, offset=offset", body)
        self.assertIn("asyncio.to_thread", body, "lecture réseau : hors de l'event loop")

    def test_the_navigation_renders_no_verdict(self):
        """Le seul geste de `/media` qui ne relit aucun média."""
        body = self._body()
        self.assertNotIn("review_media", body)
        self.assertNotIn("parse_review_callback", body)
        self.assertNotIn("_review_from_list", body)

    def test_the_reply_replaces_the_page_and_its_keyboard(self):
        body = self._body()
        self.assertIn('view["text"]', body)
        self.assertIn('reply_markup=_list_keyboard(view["keyboard"])', body)
        self.assertIn("await query.answer()", body)

    def test_the_navigation_is_documented_in_start(self):
        """Un geste qui n'est pas dans `/start` n'existe pas pour l'opérateur."""
        body = self._body("start")
        self.assertIn("/media", body)
        self.assertIn("▶️ Suivants", body)


class PendingReviewFormatTest(unittest.TestCase):
    """`format_pending_review` : ce qui reste à relire, et ce qui ne tient pas."""

    def _entry(self, name, created="2026-01-02"):
        return {
            "row": {"id": name, "media_type": "photo", "file_name": name, "created_at": created},
            "url": f"https://signed/{name}",
        }

    def test_an_empty_backlog_says_so(self):
        self.assertIn(
            "Aucune extraction en attente", telegram_media.format_pending_review([], total=0)
        )

    def test_each_entry_shows_its_signed_link(self):
        text = telegram_media.format_pending_review([self._entry("a.jpg")], total=1)
        self.assertIn("a.jpg", text)
        self.assertIn("https://signed/a.jpg", text)

    def test_the_backlog_beyond_the_page_is_announced(self):
        """Taire le reste ferait croire la revue finie alors qu'il reste des lignes."""
        entries = [self._entry(f"{i}.jpg") for i in range(3)]
        text = telegram_media.format_pending_review(entries, total=9)
        self.assertIn("9", text)
        self.assertIn("6", text)
        # « autre(s) » est la marque du reste : l'en-tête, lui, énonce l'ordre et
        # contient déjà « plus ancienne » sans qu'il reste quoi que ce soit.
        self.assertIn("autre(s)", text)

    def test_a_complete_view_does_not_announce_a_remainder(self):
        text = telegram_media.format_pending_review([self._entry("a.jpg")], total=1)
        self.assertNotIn("autre(s)", text)

    def test_the_links_expiry_is_stated(self):
        text = telegram_media.format_pending_review(
            [self._entry("a.jpg")], total=1, expires_in=120
        )
        self.assertIn("2 min", text)

    def test_a_later_page_names_the_lines_it_holds(self):
        """Sans la plage, « ▶️ Suivants » ne saurait pas où il a atterri."""
        entries = [self._entry(f"{i}.jpg") for i in range(3)]
        text = telegram_media.format_pending_review(entries, total=9, offset=3)
        self.assertIn("lignes 4–6", text)

    def test_a_page_that_holds_everything_names_no_range(self):
        entries = [self._entry(f"{i}.jpg") for i in range(3)]
        text = telegram_media.format_pending_review(entries, total=3)
        self.assertNotIn("lignes", text)

    def test_the_remainder_points_at_the_next_button(self):
        """« il en reste 6 » et un bouton qui ne les cherche pas seraient un cul-de-sac."""
        entries = [self._entry(f"{i}.jpg") for i in range(3)]
        text = telegram_media.format_pending_review(entries, total=9)
        self.assertIn("▶️ Suivants", text)


class PendingReviewViewTest(unittest.TestCase):
    """`pending_review_view` : la table balayée **en entier**, verdicts exclus."""

    def _rows(self):
        """Deux extractions très éloignées dans le temps — l'age n'est pas un filtre."""
        return [
            {
                "id": "m1",
                "storage_path": "p1",
                "media_type": "photo",
                "file_name": "a.jpg",
                "created_at": "2026-03-01",
            },
            {
                "id": "m2",
                "storage_path": "p2",
                "media_type": "document",
                "file_name": "b.pdf",
                "created_at": "2025-01-01",
                "metadata": {"channel_post": {"channel": "signals", "message_id": 7}},
            },
        ]

    def _many(self, count):
        """`count` extractions sans verdict — pour parcourir la file, pas la tenir."""
        return [
            {
                "id": f"m{i}",
                "storage_path": f"p{i}",
                "media_type": "photo",
                "file_name": f"{i}.jpg",
                "created_at": "2026-03-01",
            }
            for i in range(count)
        ]

    def _channel_rows(self, count, *, channel="signals"):
        """`count` extractions d'un **même canal** — de quoi mériter un lot."""
        return [
            {
                "id": f"c{i}",
                "storage_path": f"cp{i}",
                "media_type": "photo",
                "file_name": f"c{i}.jpg",
                "created_at": "2026-03-01",
                "metadata": {"channel_post": {"channel": channel, "message_id": i}},
            }
            for i in range(count)
        ]

    def _view(self, *, rows=None, indexed=(), list_error=None, limit=None, offset=None):
        patches = [
            mock.patch.object(
                telegram_media.media_store,
                "list_pending_review",
                return_value=self._rows() if rows is None else rows,
                **({"side_effect": list_error} if list_error else {}),
            ),
            mock.patch.object(
                telegram_media.media_store, "create_signed_url", return_value="https://signed"
            ),
            mock.patch.object(
                telegram_media.knowledge_index, "media_with_chunks", return_value=set(indexed)
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        kwargs = {}
        if limit is not None:
            kwargs["limit"] = limit
        if offset is not None:
            kwargs["offset"] = offset
        return telegram_media.pending_review_view(**kwargs)

    def test_the_view_carries_text_keyboard_and_counts(self):
        view = self._view(indexed=("m1",))
        self.assertIn("a.jpg", view["text"])
        self.assertEqual(len(view["entries"]), 2)
        self.assertEqual(len(view["keyboard"]), 2)
        self.assertEqual(view["total"], 2)
        self.assertEqual(view["shown"], 2)

    def test_each_row_keeps_its_channel_of_origin(self):
        """L'intérêt de la vue : savoir de quel canal vient l'extraction à relire."""
        view = self._view()
        self.assertIn("📡 @signals", view["text"])
        self.assertIn("msg 7", view["text"])

    def test_an_old_extraction_is_listed_like_a_recent_one(self):
        """Quel que soit leur âge : les deux lignes sont là, dans l'ordre du balayage."""
        view = self._view()
        self.assertEqual([entry["row"]["id"] for entry in view["entries"]], ["m1", "m2"])

    def test_the_buttons_carry_the_pending_prefix(self):
        """Un verdict rendu ici doit ramener **cette** vue, pas celle de `/media`."""
        view = self._view(indexed=("m1",))
        for row in view["keyboard"]:
            for _, data in row:
                self.assertTrue(data.startswith(telegram_media.PENDING_REVIEW_PREFIX + ":"))

    def test_the_view_does_not_reuse_the_bounded_media_page(self):
        """`list_media` borne la lecture : la revue en attente ne peut pas en dépendre."""
        with mock.patch.object(
            telegram_media.media_store, "list_pending_review", return_value=[]
        ), mock.patch.object(
            telegram_media.media_store,
            "list_media",
            side_effect=AssertionError("lecture bornée"),
        ):
            view = telegram_media.pending_review_view()
        self.assertIn("Aucune extraction", view["text"])

    def test_the_page_beyond_the_limit_is_announced(self):
        view = self._view(rows=self._rows(), limit=1)
        self.assertEqual(view["total"], 2)
        self.assertEqual(view["shown"], 1)
        self.assertIn("1 autre", view["text"])

    def test_an_empty_backlog_has_no_buttons(self):
        view = self._view(rows=[])
        self.assertIn("Aucune extraction", view["text"])
        self.assertEqual(view["keyboard"], [])
        self.assertEqual(view["total"], 0)

    def test_a_read_failure_gives_an_error_and_no_buttons(self):
        view = self._view(list_error=RuntimeError("db down"))
        self.assertIn("db down", view["text"])
        self.assertEqual(view["keyboard"], [])
        self.assertEqual(view["entries"], [])

    def test_the_hint_explains_the_button_numbers(self):
        self.assertIn(
            telegram_media.LIST_REVIEW_HINT, self._view(indexed=("m1",))["text"]
        )

    def test_a_backlog_that_fits_carries_no_navigation(self):
        """Un clavier qui ne mène nulle part ferait croire qu'il reste des lignes."""
        view = self._view(rows=self._many(3))
        self.assertEqual(view["shown"], 3)
        self.assertEqual(len(view["keyboard"]), 3)
        for row in view["keyboard"]:
            for label, _ in row:
                self.assertNotIn("Suivants", label)
                self.assertNotIn("Précédents", label)

    def test_the_backlog_beyond_the_page_is_reachable_by_a_button(self):
        """Le reste n'est pas seulement annoncé : on peut aller le chercher."""
        view = self._view(rows=self._many(25), limit=20)
        self.assertEqual(view["total"], 25)
        self.assertEqual(view["shown"], 20)
        self.assertEqual(len(view["keyboard"]), 21, "20 lignes de revue + la navigation")
        self.assertEqual([label for label, _ in view["keyboard"][-1]], ["▶️ Suivants"])

    def test_the_navigation_row_comes_after_the_review_rows(self):
        """Ce ne sont pas des lignes de la liste : ils se lisent après elle."""
        view = self._view(rows=self._many(25), limit=20)
        for row in view["keyboard"][:-1]:
            for _, data in row:
                self.assertTrue(
                    data.startswith(telegram_media.PENDING_REVIEW_PREFIX + ":")
                )
        self.assertEqual(
            view["keyboard"][-1][0][1], telegram_media.pending_page_data(20)
        )

    def test_asking_for_a_later_rank_shows_the_lines_beyond(self):
        """Parcourir la file : le rang demandé est celui de la première ligne affichée."""
        view = self._view(rows=self._many(25), limit=20, offset=20)
        self.assertEqual(view["offset"], 20)
        self.assertEqual(view["shown"], 5)
        self.assertEqual(
            [entry["row"]["id"] for entry in view["entries"]],
            [f"m{i}" for i in range(20, 25)],
        )
        self.assertIn("lignes 21–25", view["text"])

    def test_both_directions_are_offered_inside_the_backlog(self):
        """Une page du milieu peut revenir **et** avancer, sur une seule rangée."""
        view = self._view(rows=self._many(60), limit=20, offset=20)
        self.assertEqual(
            [label for label, _ in view["keyboard"][-1]],
            ["◀️ Précédents", "▶️ Suivants"],
        )
        self.assertEqual(
            [data for _, data in view["keyboard"][-1]],
            ["medpg:0", "medpg:40"],
        )

    def test_a_rank_past_the_end_falls_back_to_the_last_real_page(self):
        """La file rétrécit après des verdicts : la page 7 peut devenir la dernière."""
        view = self._view(rows=self._many(25), limit=20, offset=100)
        self.assertEqual(view["offset"], 5, "les 20 dernières lignes, pas une page vide")
        self.assertEqual(view["shown"], 20)
        self.assertEqual(view["total"], 25)
        self.assertEqual([label for label, _ in view["keyboard"][-1]], ["◀️ Précédents"])

    def test_navigating_renders_no_verdict(self):
        """La page suivante relit la file : elle ne touche ni la base ni l'index."""
        with mock.patch.object(
            telegram_media.media_store, "list_pending_review", return_value=self._many(25)
        ), mock.patch.object(
            telegram_media.media_store, "create_signed_url", return_value="https://signed"
        ), mock.patch.object(
            telegram_media.knowledge_index, "media_with_chunks", return_value=set()
        ), mock.patch.object(
            telegram_media, "review_media", side_effect=AssertionError("verdict rendu")
        ):
            view = telegram_media.pending_review_view(offset=20)
        self.assertEqual(view["offset"], 20)

    def test_a_channel_left_with_several_pendings_gets_a_batch_row(self):
        """Le geste qu'on veut après une panne de clef : tout un canal d'un clic."""
        view = self._view(rows=self._channel_rows(3), indexed=("c0", "c1", "c2"))
        self.assertEqual(len(view["channels"]), 1)
        self.assertEqual(
            [label for label, _ in view["keyboard"][-1]],
            ["✅ @signals (3)", "↩️ @signals (3)"],
        )
        self.assertEqual(
            [data for _, data in view["keyboard"][-1]],
            ["medc:ok:signals", "medc:re:signals"],
        )

    def test_a_private_chat_backlog_gets_no_batch_row(self):
        """Un média de chat n'a pas de canal : aucun lot ne peut le viser."""
        view = self._view(rows=self._many(3))
        self.assertEqual(view["channels"], [])
        for row in view["keyboard"]:
            for _, data in row:
                self.assertFalse(data.startswith("medc:"))

    def test_the_batch_row_counts_the_whole_file_not_the_page(self):
        """Un lot borné à la page ferait passer un canal à moitié traité pour soldé."""
        view = self._view(rows=self._channel_rows(25), limit=20, indexed=("c0",))
        self.assertEqual(view["shown"], 20)
        self.assertEqual(view["channels"][0]["count"], 25)
        self.assertEqual(view["channels"][0]["validatable"], 1)
        self.assertEqual([label for label, _ in view["keyboard"][-1]], ["✅ @signals (1)", "↩️ @signals (25)"])

    def test_the_navigation_comes_before_the_batch_row(self):
        """La page se parcourt, le lot agit plus large : l'ordre dit lequel est lequel."""
        view = self._view(rows=self._channel_rows(25), limit=20)
        self.assertEqual(
            [label for label, _ in view["keyboard"][-2]],
            ["▶️ Suivants"],
        )
        self.assertEqual(
            [label for label, _ in view["keyboard"][-1]],
            ["↩️ @signals (25)"],
        )

    def test_the_index_is_read_once_for_the_whole_file(self):
        """Deux lectures pourraient se contredire sur ce qui a du texte."""
        with mock.patch.object(
            telegram_media.media_store,
            "list_pending_review",
            return_value=self._channel_rows(25),
        ), mock.patch.object(
            telegram_media.media_store, "create_signed_url", return_value="https://signed"
        ), mock.patch.object(
            telegram_media.knowledge_index, "media_with_chunks", return_value=set()
        ) as chunks, mock.patch.object(
            telegram_media.media_store, "extraction_outcome", return_value=None
        ):
            telegram_media.pending_review_view(limit=20)
        self.assertEqual(chunks.call_count, 1)


class IndexLookupBatchingTest(unittest.TestCase):
    """L'index est lu par lots **bornés** : `/pending` interroge la file entière."""

    def _rows(self, count):
        return [{"id": f"m{i}"} for i in range(count)]

    def test_a_small_backlog_is_one_read(self):
        with mock.patch.object(
            telegram_media.knowledge_index, "media_with_chunks", return_value={"m0"}
        ) as chunks:
            found = telegram_media._indexed_media(self._rows(3))
        self.assertEqual(chunks.call_count, 1)
        self.assertEqual(found, {"m0"})

    def test_a_long_backlog_is_read_in_batches_no_longer_than_the_limit(self):
        """Un `in.(…)` trop long est refusé : la lecture doit rester par lots."""
        size = telegram_media.INDEX_IDS_PER_QUERY
        with mock.patch.object(
            telegram_media.knowledge_index, "media_with_chunks", return_value=set()
        ) as chunks:
            telegram_media._indexed_media(self._rows(size * 2 + 1))
        self.assertEqual(chunks.call_count, 3)
        for call in chunks.call_args_list:
            self.assertLessEqual(len(call.args[0]), size)

    def test_what_each_batch_found_is_merged(self):
        size = telegram_media.INDEX_IDS_PER_QUERY

        def chunks(ids):
            return {"m0", f"m{size}"} & set(ids)

        with mock.patch.object(
            telegram_media.knowledge_index, "media_with_chunks", side_effect=chunks
        ):
            found = telegram_media._indexed_media(self._rows(size + 1))
        self.assertEqual(found, {"m0", f"m{size}"})

    def test_a_failed_read_is_nothing_indexed(self):
        """Le doute coûte une réindexation de trop, jamais une relecture crue à tort."""
        with mock.patch.object(
            telegram_media.knowledge_index,
            "media_with_chunks",
            side_effect=RuntimeError("index down"),
        ):
            self.assertEqual(telegram_media._indexed_media(self._rows(3)), set())

    def test_an_empty_backlog_does_not_read_at_all(self):
        with mock.patch.object(
            telegram_media.knowledge_index, "media_with_chunks"
        ) as chunks:
            self.assertEqual(telegram_media._indexed_media([]), set())
        self.assertEqual(chunks.call_count, 0)


class PendingPageDataTest(unittest.TestCase):
    """Le rang porté par un bouton de navigation de `/pending` — et rien d'autre."""

    def test_the_prefix_is_disjoint_from_the_review_ones(self):
        """`^medp:` exige un deux-points juste après : `medpg:` reste à part."""
        for prefix in (
            telegram_media.REVIEW_PREFIX,
            telegram_media.LIST_REVIEW_PREFIX,
            telegram_media.PENDING_REVIEW_PREFIX,
        ):
            with self.subTest(prefix=prefix):
                self.assertNotEqual(prefix, telegram_media.PENDING_PAGE_PREFIX)
                self.assertFalse(
                    telegram_media.pending_page_data(3).startswith(prefix + ":")
                )

    def test_a_round_trip_gives_the_rank_back(self):
        for offset in (0, 1, 20, 1000):
            with self.subTest(offset=offset):
                self.assertEqual(
                    telegram_media.parse_pending_page(
                        telegram_media.pending_page_data(offset)
                    ),
                    offset,
                )

    def test_a_negative_rank_is_flattened_rather_than_refused(self):
        """`pending_page_data` borne à gauche : un rang négatif n'existe pas."""
        self.assertEqual(telegram_media.pending_page_data(-5), "medpg:0")

    def test_a_foreign_or_broken_payload_is_refused(self):
        """Refuser plutôt que ramener à zéro : un bouton illisible ne doit pas
        réafficher la première page en donnant l'impression d'avoir été compris."""
        for data in (
            None,
            "",
            "med:0",
            "medl:ok:x",
            "medp:0",
            "medpg",
            "medpg:",
            "medpg:x",
            "medpg:-1",
            "medpg:1:2",
            "medt:0",
            "rag:3",
        ):
            with self.subTest(data=data):
                self.assertIsNone(telegram_media.parse_pending_page(data))

    def test_nothing_to_go_to_means_no_keyboard(self):
        self.assertEqual(telegram_media.pending_buttons(offset=0, shown=1, total=1), [])
        self.assertEqual(telegram_media.pending_buttons(offset=0, shown=0, total=0), [])

    def test_the_first_page_only_goes_forward(self):
        rows = telegram_media.pending_buttons(offset=0, shown=20, total=40, page=20)
        self.assertEqual(len(rows), 1)
        self.assertEqual([label for label, _ in rows[0]], ["▶️ Suivants"])
        self.assertEqual([data for _, data in rows[0]], ["medpg:20"])

    def test_the_last_page_only_goes_back(self):
        rows = telegram_media.pending_buttons(offset=30, shown=10, total=40, page=10)
        self.assertEqual([label for label, _ in rows[0]], ["◀️ Précédents"])
        self.assertEqual([data for _, data in rows[0]], ["medpg:20"])

    def test_both_directions_share_one_row(self):
        """Ce ne sont pas des lignes de la liste : ils ne s'alignent sur aucune ligne."""
        rows = telegram_media.pending_buttons(offset=10, shown=10, total=40, page=10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            [label for label, _ in rows[0]], ["◀️ Précédents", "▶️ Suivants"]
        )
        self.assertEqual([data for _, data in rows[0]], ["medpg:0", "medpg:20"])


class PendingPageCallbackWiringTest(unittest.TestCase):
    """La navigation de `/pending` vue depuis `main.py` (non importable ici)."""

    MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"

    def _body(self, name="media_pending_page_callback") -> str:
        text = self.MAIN.read_text(encoding="utf-8")
        return text.split(f"async def {name}", 1)[1].split("\nasync def ", 1)[0]

    def test_the_callback_is_registered_before_the_generic_handler(self):
        """Sinon `button_handler` verrait `medpg:` avant la navigation."""
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertLess(
            text.index(
                'CallbackQueryHandler(media_pending_page_callback, pattern=r"^medpg:")'
            ),
            text.index("CallbackQueryHandler(button_handler)"),
        )

    def test_the_rank_is_parsed_by_the_module(self):
        self.assertIn("telegram_media.parse_pending_page(query.data)", self._body())

    def test_an_unreadable_button_answers_without_touching_the_message(self):
        body = self._body()
        refusal = body.split("if offset is None:", 1)[1].split("try:", 1)[0]
        self.assertIn("await query.answer(", refusal)
        self.assertNotIn("edit_message_text", refusal)

    def test_the_page_is_recomputed_from_the_rank(self):
        """Aucun état mémorisé : la file change dès qu'un verdict est rendu."""
        body = self._body()
        self.assertIn("telegram_media.pending_review_view, offset=offset", body)
        self.assertIn("asyncio.to_thread", body, "lecture réseau : hors de l'event loop")

    def test_the_navigation_renders_no_verdict(self):
        """C'est le seul geste de `/pending` qui ne relit aucune extraction."""
        body = self._body()
        self.assertNotIn("review_media", body)
        self.assertNotIn("parse_review_callback", body)

    def test_the_navigation_is_documented_in_start(self):
        """Un geste qui n'est pas dans `/start` n'existe pas pour l'opérateur."""
        self.assertIn("/pending seul", self._body("start"))

    def test_the_reply_replaces_the_page_and_its_keyboard(self):
        body = self._body()
        self.assertIn('reply_markup=_list_keyboard(view["keyboard"])', body)
        self.assertIn("await query.edit_message_text(", body)


class ChannelNameTest(unittest.TestCase):
    """`channel_name` : qui appartient à quel canal, selon la route d'ingestion."""

    def test_a_bot_post_names_the_username_it_carries(self):
        row = {"metadata": {"channel_post": {"channel": "signals", "message_id": 7}}}
        self.assertEqual(telegram_media.channel_name(row), "signals")

    def test_a_bot_post_falls_back_on_the_title(self):
        """Un canal privé n'a pas de pseudo : le titre est ce qu'on a."""
        row = {"metadata": {"channel_post": {"title": "Crypto Signals", "message_id": 7}}}
        self.assertEqual(telegram_media.channel_name(row), "Crypto Signals")

    def test_a_scraper_preview_names_the_channel_of_the_url(self):
        row = {"metadata": {"channel": "signals", "web_preview": True}}
        self.assertEqual(telegram_media.channel_name(row), "signals")

    def test_a_nameless_bot_post_does_not_become_a_scraper_preview(self):
        """`channel_post` présent mais muet : ce n'est pas un aperçu du scraper."""
        row = {"metadata": {"channel_post": {"message_id": 7}, "channel": "signals"}}
        self.assertIsNone(telegram_media.channel_name(row))

    def test_nothing_that_is_not_a_channel_names_nothing(self):
        for row in (
            None,
            {},
            {"metadata": {"telegram": True}},
            {"metadata": "pas un dict"},
            {"metadata": {"channel": "   "}},
        ):
            with self.subTest(row=row):
                self.assertIsNone(telegram_media.channel_name(row))

    def test_the_grouping_key_drops_the_at_sign(self):
        """`@signals` et `signals` sont le même canal : ils se regroupent."""
        bot = {"metadata": {"channel_post": {"channel": "@signals"}}}
        preview = {"metadata": {"channel": "signals"}}
        self.assertEqual(telegram_media.channel_bulk_key(bot), "signals")
        self.assertEqual(telegram_media.channel_bulk_key(preview), "signals")

    def test_a_private_chat_media_has_no_grouping_key(self):
        self.assertIsNone(telegram_media.channel_bulk_key({"metadata": {"telegram": True}}))
        self.assertIsNone(telegram_media.channel_bulk_key(None))

    def test_the_displayed_origin_still_separates_the_two_routes(self):
        """Regrouper ne doit pas effacer ce que `/media` affiche de chaque route."""
        bot = {"metadata": {"channel_post": {"channel": "signals", "message_id": 7}}}
        preview = {"metadata": {"channel": "signals", "web_preview": True}}
        self.assertEqual(telegram_media.media_origin(bot), "📡 @signals · msg 7")
        self.assertEqual(telegram_media.media_origin(preview), "📡 @signals · aperçu web")


class PendingChannelGroupsTest(unittest.TestCase):
    """Quels canaux méritent une rangée de lot, et sur combien de lignes."""

    def _bot(self, media_id, channel=None, title=None, message_id=1):
        post = {"message_id": message_id}
        if channel:
            post["channel"] = channel
        if title:
            post["title"] = title
        return {"id": media_id, "metadata": {"channel_post": post}}

    def _preview(self, media_id, channel):
        return {"id": media_id, "metadata": {"channel": channel, "web_preview": True}}

    def _chat(self, media_id):
        return {"id": media_id, "metadata": {"telegram": True}}

    def test_a_lone_pending_is_not_a_batch(self):
        """Les boutons de sa ligne font déjà le même geste : une rangée serait un doublon."""
        rows = [self._bot("m1", "signals")]
        self.assertEqual(telegram_media.pending_channel_groups(rows), [])

    def test_two_pendings_of_one_channel_become_a_row(self):
        rows = [self._bot("m1", "signals"), self._bot("m2", "signals")]
        groups = telegram_media.pending_channel_groups(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["channel"], "signals")
        self.assertEqual(groups[0]["label"], "@signals")
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(groups[0]["media_ids"], ["m1", "m2"])

    def test_both_routes_land_in_the_same_row(self):
        """Le bot et l'aperçu public déposent la même clé : un seul lot."""
        rows = [self._bot("m1", "signals"), self._preview("m2", "signals")]
        groups = telegram_media.pending_channel_groups(rows)
        self.assertEqual([group["media_ids"] for group in groups], [["m1", "m2"]])

    def test_media_of_a_private_chat_are_never_in_a_batch(self):
        rows = [self._chat("m1"), self._chat("m2")]
        self.assertEqual(telegram_media.pending_channel_groups(rows), [])

    def test_the_validatable_count_only_counts_indexed_text(self):
        """« ✅ » n'a rien à faire sur une extraction vide."""
        rows = [self._bot(f"m{i}", "signals") for i in range(3)]
        groups = telegram_media.pending_channel_groups(rows, indexed={"m1"})
        self.assertEqual(groups[0]["count"], 3)
        self.assertEqual(groups[0]["validatable"], 1)

    def test_a_row_without_identifier_counts_but_cannot_be_reviewed(self):
        rows = [self._bot("m1", "signals"), {"id": None, "metadata": {"channel": "signals"}}]
        groups = telegram_media.pending_channel_groups(rows)
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(groups[0]["media_ids"], ["m1"])

    def test_the_busiest_channel_comes_first(self):
        rows = [
            self._bot("a1", "news"),
            self._bot("a2", "news"),
            self._bot("a3", "news"),
            self._bot("b1", "signals"),
            self._bot("b2", "signals"),
        ]
        groups = telegram_media.pending_channel_groups(rows)
        self.assertEqual([group["channel"] for group in groups], ["news", "signals"])

    def test_a_tie_is_broken_by_name(self):
        """Deux clics de suite doivent présenter le même ordre."""
        rows = [
            self._bot("z1", "zulu"),
            self._bot("z2", "zulu"),
            self._bot("a1", "alpha"),
            self._bot("a2", "alpha"),
        ]
        groups = telegram_media.pending_channel_groups(rows)
        self.assertEqual([group["channel"] for group in groups], ["alpha", "zulu"])

    def test_min_count_one_lists_even_a_single_pending(self):
        rows = [self._bot("m1", "signals")]
        groups = telegram_media.pending_channel_groups(rows, min_count=1)
        self.assertEqual([group["count"] for group in groups], [1])


class ChannelBulkDataTest(unittest.TestCase):
    """Le canal porté par un bouton de lot — et rien d'autre."""

    BULK_PREFIXES = (
        "CHANNEL_BULK_PREFIX",
        "CHANNEL_BULK_CONFIRM_PREFIX",
        "CHANNEL_BULK_CANCEL_PREFIX",
    )

    def test_the_prefix_is_disjoint_from_the_other_media_ones(self):
        """`^med:` exige un deux-points juste après `med` : `medc:` reste à part."""
        for prefix in (
            telegram_media.REVIEW_PREFIX,
            telegram_media.LIST_REVIEW_PREFIX,
            telegram_media.PENDING_REVIEW_PREFIX,
            telegram_media.PENDING_PAGE_PREFIX,
            telegram_media.TRANSCRIBE_PAGE_PREFIX,
        ):
            for name in self.BULK_PREFIXES:
                with self.subTest(prefix=prefix, bulk=name):
                    self.assertNotEqual(prefix, getattr(telegram_media, name))
            self.assertFalse(
                telegram_media.channel_bulk_data("re", "signals").startswith(prefix + ":")
            )

    def test_each_phase_of_a_batch_has_its_own_prefix(self):
        """Le bouton qui **montre** ne peut pas être celui qui **écrit**.

        Les trois temps d'un lot portent des préfixes distincts, et le motif du
        handler est ancré sur le deux-points : `^medc:` ne peut donc pas attraper
        `medck:` (ni `medcx:`), comme `^medp:` ne peut pas attraper `medpg:`.
        """
        prefixes = [getattr(telegram_media, name) for name in self.BULK_PREFIXES]
        self.assertEqual(len(set(prefixes)), len(prefixes))
        payloads = telegram_media.channel_bulk_actions("re", "signals")
        for name, prefix in zip(("ask", "confirm", "cancel"), prefixes):
            with self.subTest(phase=name):
                self.assertTrue(payloads[name].startswith(prefix + ":"))
                for other in prefixes:
                    if other != prefix:
                        self.assertNotRegex(payloads[name], f"^{other}:")

    def test_the_three_times_of_a_batch_share_the_channels_encoding(self):
        """Un même canal s'encode pareil aux trois temps : seule la phase change."""
        payloads = telegram_media.channel_bulk_actions("ok", "Crypto Signals")
        self.assertEqual(
            payloads,
            {
                "ask": "medc:ok:Crypto%20Signals",
                "confirm": "medck:ok:Crypto%20Signals",
                "cancel": "medcx:ok:Crypto%20Signals",
            },
        )
        for name, prefix in (
            ("ask", telegram_media.CHANNEL_BULK_PREFIX),
            ("confirm", telegram_media.CHANNEL_BULK_CONFIRM_PREFIX),
            ("cancel", telegram_media.CHANNEL_BULK_CANCEL_PREFIX),
        ):
            with self.subTest(phase=name):
                self.assertEqual(
                    telegram_media.parse_channel_bulk(payloads[name], prefix=prefix),
                    ("ok", "Crypto Signals"),
                )

    def test_a_payload_of_another_phase_is_refused(self):
        """L'aperçu n'exécute rien, annuler n'exécute rien : chacun lit la sienne.

        Sans le préfixe en paramètre, la charge utile de l'aperçu serait relue par
        le handler qui **écrit** — un clic « voir » lancerait le lot.
        """
        ask = telegram_media.channel_bulk_data("ok", "signals")
        confirm = telegram_media.channel_bulk_data(
            "ok", "signals", prefix=telegram_media.CHANNEL_BULK_CONFIRM_PREFIX
        )
        cancel = telegram_media.channel_bulk_data(
            "ok", "signals", prefix=telegram_media.CHANNEL_BULK_CANCEL_PREFIX
        )
        self.assertIsNone(
            telegram_media.parse_channel_bulk(
                ask, prefix=telegram_media.CHANNEL_BULK_CONFIRM_PREFIX
            )
        )
        self.assertIsNone(telegram_media.parse_channel_bulk(confirm))
        self.assertIsNone(
            telegram_media.parse_channel_bulk(
                confirm, prefix=telegram_media.CHANNEL_BULK_CANCEL_PREFIX
            )
        )
        self.assertIsNone(telegram_media.parse_channel_bulk(cancel))

    def test_the_three_times_are_offered_together_or_not_at_all(self):
        """Un aperçu qu'on ne pourrait pas confirmer serait une impasse.

        `medck:` et `medcx:` portent un octet de plus que `medc:` : un nom de canal
        qui remplit tout juste les 64 octets de Telegram laisserait donc un aperçu
        avec un seul bouton inutilisable. Les trois charges utiles se jugent
        ensemble, et l'absence de l'une retire la rangée de la liste.
        """
        self.assertIsNone(telegram_media.channel_bulk_actions("re", "y" * 59))
        self.assertIsNone(telegram_media.channel_bulk_actions("re", "y" * 60))
        self.assertEqual(
            telegram_media.channel_bulk_buttons(
                [{"channel": "y" * 60, "label": "@long", "count": 3, "validatable": 3}]
            ),
            [],
        )
        payloads = telegram_media.channel_bulk_actions("re", "y" * 50)
        self.assertIsNotNone(payloads)
        self.assertEqual(payloads["cancel"], "medcx:re:" + "y" * 50)

    def test_a_round_trip_gives_the_channel_back(self):
        for channel in ("signals", "Crypto Signals", "a:b", "@signaux", "canal-100-2"):
            with self.subTest(channel=channel):
                self.assertEqual(
                    telegram_media.parse_channel_bulk(
                        telegram_media.channel_bulk_data("ok", channel)
                    ),
                    ("ok", channel),
                )

    def test_the_separator_of_a_title_does_not_split_it_in_two(self):
        """Un titre peut contenir le `:` du séparateur : d'où l'encodage."""
        data = telegram_media.channel_bulk_data("re", "nouvelles: matin")
        self.assertEqual(telegram_media.parse_channel_bulk(data), ("re", "nouvelles: matin"))

    def test_the_prefix_and_verdict_survive_the_encoding(self):
        data = telegram_media.channel_bulk_data("re", "signals")
        self.assertTrue(data.startswith("medc:re:"))

    def test_a_foreign_or_broken_payload_is_refused(self):
        """Refuser plutôt que deviner : un lot appliqué au mauvais canal est pire que rien."""
        for data in (
            None,
            "",
            "medc",
            "medc:ok:",
            "medc:ok",
            "medc::signals",
            "medc:no:signals",
            "medc:ok:signals:extra",
            "med:ok:signals",
            "medp:ok:m1",
            "medpg:20",
            "medt:10",
            "medl:ok:x",
            "rag:3",
        ):
            with self.subTest(data=data):
                self.assertIsNone(telegram_media.parse_channel_bulk(data))

    def test_a_verdict_outside_the_two_batch_actions_is_refused(self):
        """Rejeter en masse n'est pas une action de lot et ne doit pas pouvoir l'être."""
        self.assertNotIn("no", telegram_media.CHANNEL_BULK_VERDICTS)
        with self.assertRaises(ValueError):
            telegram_media.channel_bulk_data("no", "signals")
        with self.assertRaises(ValueError):
            telegram_media.channel_bulk_actions("no", "signals")


class ChannelBulkButtonsTest(unittest.TestCase):
    """La rangée de lot d'un canal : ce qu'elle propose, et à quel nombre."""

    def _group(self, **overrides):
        group = {
            "channel": "signals",
            "label": "@signals",
            "count": 5,
            "validatable": 3,
            "media_ids": ["m1"],
        }
        group.update(overrides)
        return group

    def test_the_numbers_are_the_ones_the_buttons_act_on(self):
        """« ✅ (3) » qui en validerait cinq ferait mentir son étiquette."""
        rows = telegram_media.channel_bulk_buttons([self._group()])
        self.assertEqual(
            rows, [[("✅ @signals (3)", "medc:ok:signals"), ("↩️ @signals (5)", "medc:re:signals")]]
        )

    def test_validating_disappears_when_nothing_has_text(self):
        rows = telegram_media.channel_bulk_buttons([self._group(validatable=0)])
        self.assertEqual([label for label, _ in rows[0]], ["↩️ @signals (5)"])

    def test_a_long_title_stays_readable(self):
        """Un titre de canal peut être long : le clavier reste lisible."""
        rows = telegram_media.channel_bulk_buttons([self._group(label="@" + "x" * 60)])
        name = rows[0][0][0].partition(" (")[0]
        self.assertEqual(len(name), len("✅ ") + telegram_media.CHANNEL_LABEL_CHARS)
        self.assertTrue(name.endswith("…"))

    def test_a_name_too_long_for_telegram_is_not_offered(self):
        """Un `callback_data` refusé ferait échouer l'envoi de la liste entière."""
        rows = telegram_media.channel_bulk_buttons([self._group(channel="y" * 60)])
        self.assertEqual(rows, [])

    def test_a_group_without_a_channel_is_skipped(self):
        self.assertEqual(telegram_media.channel_bulk_buttons([{"count": 3}]), [])

    def test_no_group_means_no_row(self):
        self.assertEqual(telegram_media.channel_bulk_buttons([]), [])
        self.assertEqual(telegram_media.channel_bulk_buttons(None), [])

    def test_the_row_opens_the_preview_and_neither_executes_nor_cancels(self):
        """Le clic de la liste montre ; il ne solde ni n'annule le lot."""
        rows = telegram_media.channel_bulk_buttons([self._group()])
        for _, data in rows[0]:
            with self.subTest(data=data):
                self.assertTrue(data.startswith(telegram_media.CHANNEL_BULK_PREFIX + ":"))
                self.assertNotRegex(
                    data, f"^{telegram_media.CHANNEL_BULK_CONFIRM_PREFIX}:"
                )
                self.assertNotRegex(
                    data, f"^{telegram_media.CHANNEL_BULK_CANCEL_PREFIX}:"
                )


class ChannelBulkPreviewTest(unittest.TestCase):
    """`bulk_preview_view` : ce que le lot montrerait, et ce qu'il n'a pas fait."""

    SOURCE = pathlib.Path(__file__).resolve().parents[1] / "notifications" / "telegram_media.py"

    def _bot(self, media_id, channel="signals", message_id=1):
        """Une attente d'un canal, décrite comme `media_store` la rend."""
        return {
            "id": media_id,
            "file_name": f"{media_id}.jpg",
            "media_type": "photo",
            "created_at": "2026-03-01",
            "metadata": {"channel_post": {"channel": channel, "message_id": message_id}},
        }

    def _preview(
        self,
        verdict="re",
        rows=(),
        *,
        channel="signals",
        indexed=(),
        limit=None,
        list_error=None,
        seen=None,
    ):
        """L'aperçu, avec `list_pending`/`indexed_of` injectés comme pour le lot."""

        def list_pending():
            if list_error is not None:
                raise list_error
            return list(rows)

        def indexed_of(given):
            if seen is not None:
                seen.extend(row.get("id") for row in given)
            return set(indexed)

        kwargs = {} if limit is None else {"limit": limit}
        return telegram_media.bulk_preview_view(
            channel, verdict, list_pending=list_pending, indexed_of=indexed_of, **kwargs
        )

    def _payloads(self, preview, phase):
        """Le `callback_data` d'un bouton de l'aperçu, relu par le module."""
        labels = [label for row in preview["keyboard"] for label, _ in row]
        prefix = {
            "confirm": telegram_media.CHANNEL_BULK_CONFIRM_PREFIX,
            "cancel": telegram_media.CHANNEL_BULK_CANCEL_PREFIX,
        }[phase]
        for row in preview["keyboard"]:
            for _, data in row:
                if telegram_media.parse_channel_bulk(data, prefix=prefix):
                    return telegram_media.parse_channel_bulk(data, prefix=prefix)
        self.fail(f"aucun bouton {phase} dans {labels}")

    def test_the_preview_names_every_extraction_it_visits(self):
        """L'aperçu ne dit pas « 2 » : il dit lesquelles, comme la liste."""
        rows = [self._bot("s1"), self._bot("s2"), self._bot("n1", "news")]
        preview = self._preview("re", rows)
        self.assertIn("1. photo · s1.jpg", preview["text"])
        self.assertIn("2. photo · s2.jpg", preview["text"])
        self.assertNotIn("n1.jpg", preview["text"])
        self.assertEqual([target["row"]["id"] for target in preview["targets"]], ["s1", "s2"])
        self.assertEqual(preview["requested"], 2)
        self.assertEqual(preview["waiting"], 2)

    def test_the_preview_only_counts_the_channel_it_names(self):
        """Le nom affiché groupe : `@signals` et `signals` sont le même canal."""
        rows = [
            self._bot("s1"),
            self._bot("n1", "news"),
            self._bot("s2"),
            self._bot("n2", "news"),
        ]
        preview = self._preview("re", rows, channel="@signals")
        self.assertEqual([target["row"]["id"] for target in preview["targets"]], ["s1", "s2"])
        self.assertEqual(preview["waiting"], 2)
        self.assertNotIn("n1.jpg", preview["text"])
        self.assertEqual(preview["label"], "@signals")

    def test_the_preview_says_it_has_changed_nothing(self):
        """Tout l'intérêt de l'étape : un clic qui ne fait rien doit le dire."""
        preview = self._preview("re", [self._bot("s1"), self._bot("s2")])
        self.assertIn("Rien n'a encore été modifié", preview["text"])
        self.assertIn("Confirmer le lot", preview["text"])
        self.assertNotIn("❌", preview["text"])

    def test_the_preview_reads_and_never_writes(self):
        """L'aperçu qui exécuterait serait pire que pas d'aperçu : il ferait confirmer à l'aveugle.

        Les motifs sont cherchés sur l'**arbre** du module, pas sur son texte : la
        docstring de l'aperçu nomme `bulk_review_channel` (c'est d'elle qu'il tient
        sa règle), et les noms qui se ressemblent — `format_bulk_preview`,
        `list_pending_review` — feraient échouer une recherche de sous-chaînes.
        """
        tree = ast.parse(self.SOURCE.read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        names: set = set()
        stored: set = set()
        for name in ("bulk_preview_view", "format_bulk_preview", "_preview_target"):
            for inner in ast.walk(functions[name]):
                if isinstance(inner, ast.Call):
                    names.add(_called_name(inner))
                if (
                    isinstance(inner, ast.Attribute)
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == "media_store"
                ):
                    stored.add(inner.attr)
        # Les quatre portes du module qui écrivent : l'aperçu n'en appelle aucune.
        self.assertEqual(
            names
            & {
                "bulk_review_channel",
                "review_media",
                "reprocess_media",
                "retranscribe_media",
            },
            set(),
        )
        # Et la seule porte de stockage qu'il s'autorise est celle qui **lit** la file.
        self.assertEqual(stored, {"list_pending_review"})

    def test_a_line_the_batch_would_leave_is_marked_before_the_click(self):
        """« ✅ » laisse les extractions sans texte : l'aperçu les marque, il ne les tait pas."""
        rows = [self._bot("s1"), self._bot("s2")]
        preview = self._preview("ok", rows, indexed={"s1"})
        self.assertEqual(preview["acted"], 1)
        self.assertEqual(preview["skipped"], 1)
        self.assertIn("2. ⏭️ photo · s2.jpg", preview["text"])
        self.assertIn("1. photo · s1.jpg", preview["text"])
        self.assertIn("1 des extractions visées n'ont pas de texte indexé", preview["text"])

    def test_a_reindexation_marks_what_it_cannot_read_at_all(self):
        """Une ligne sans identifiant ne peut pas être relue : le dire avant, pas après."""
        rows = [self._bot("s1"), {"id": None, "metadata": {"channel": "signals"}}]
        preview = self._preview("re", rows)
        self.assertEqual(preview["acted"], 1)
        self.assertEqual(preview["skipped"], 1)
        self.assertIn("n'ont pas d'identifiant", preview["text"])

    def test_the_confirmation_carries_the_same_channel_as_the_list(self):
        preview = self._preview("re", [self._bot("s1"), self._bot("s2")])
        self.assertEqual(self._payloads(preview, "confirm"), ("re", "signals"))
        self.assertEqual(self._payloads(preview, "cancel"), ("re", "signals"))

    def test_the_confirmation_does_not_land_on_the_preview(self):
        """Deux clics distincts : montrer puis agir, jamais montrer en boucle."""
        preview = self._preview("re", [self._bot("s1"), self._bot("s2")])
        for row in preview["keyboard"]:
            for _, data in row:
                with self.subTest(data=data):
                    self.assertIsNone(telegram_media.parse_channel_bulk(data))

    def test_the_cancel_button_is_never_readable_by_the_confirming_handler(self):
        """Un « Annuler » relu comme une confirmation exécuterait le lot qu'on refuse.

        Les deux charges utiles portent le même canal et le même verdict : seule la
        phase les sépare, et c'est donc elle que chaque handler exige.
        """
        preview = self._preview("re", [self._bot("s1"), self._bot("s2")])
        seen = 0
        for row in preview["keyboard"]:
            for label, data in row:
                with self.subTest(label=label):
                    if "Annuler" in label:
                        self.assertIsNone(
                            telegram_media.parse_channel_bulk(
                                data, prefix=telegram_media.CHANNEL_BULK_CONFIRM_PREFIX
                            )
                        )
                    else:
                        self.assertIsNone(
                            telegram_media.parse_channel_bulk(
                                data, prefix=telegram_media.CHANNEL_BULK_CANCEL_PREFIX
                            )
                        )
                seen += 1
        self.assertEqual(seen, 2)

    def test_nothing_to_confirm_leaves_only_the_way_back(self):
        """Un bouton sans effet ferait douter de tous les autres."""
        rows = [self._bot("s1"), self._bot("s2")]
        preview = self._preview("ok", rows, indexed=set())
        self.assertFalse(preview["confirmable"])
        self.assertEqual(preview["skipped"], 2)
        self.assertIn("il n'y a rien à valider", preview["text"])
        self.assertEqual(
            [label for row in preview["keyboard"] for label, _ in row], ["✖️ Annuler"]
        )
        self.assertEqual(self._payloads(preview, "cancel"), ("ok", "signals"))

    def test_a_channel_with_nothing_left_is_announced_like_the_report(self):
        """Un verdict tombé entre l'affichage et le clic : l'aperçu le dit aussi."""
        preview = self._preview("re", [self._bot("n1", "news")])
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["requested"], 0)
        self.assertEqual(preview["targets"], [])
        self.assertIn("Plus rien en attente", preview["text"])
        self.assertIn("Rien n'a été modifié", preview["text"])
        self.assertEqual(
            [label for row in preview["keyboard"] for label, _ in row], ["✖️ Annuler"]
        )

    def test_the_cap_is_announced_with_the_channels_total(self):
        """Un aperçu qui annoncerait plus de lignes que le clic n'en traite mentirait."""
        rows = [self._bot(f"s{i}") for i in range(30)]
        preview = self._preview("re", rows, limit=25)
        self.assertEqual(preview["requested"], 25)
        self.assertEqual(preview["remaining"], 5)
        self.assertEqual(preview["waiting"], 30)
        self.assertIn("25 extraction(s) visée(s) sur les 30", preview["text"])
        self.assertIn("5 laissée(s) de côté", preview["text"])
        self.assertIn(str(telegram_media.CHANNEL_BULK_MAX), preview["text"])

    def test_the_index_is_read_once_for_the_whole_channel(self):
        """Même règle que le lot : l'index se juge sur le canal, pas sur la page."""
        seen = []
        rows = [self._bot("s1"), self._bot("n1", "news"), self._bot("s2"), self._bot("s3")]
        preview = self._preview("ok", rows, indexed={"s1", "s2", "s3"}, limit=2, seen=seen)
        self.assertEqual(seen, ["s1", "s2", "s3"])
        self.assertEqual(preview["requested"], 2)
        self.assertEqual(preview["acted"], 2)
        self.assertEqual(preview["remaining"], 1)

    def test_an_unreadable_backlog_still_leaves_a_way_back(self):
        """L'aperçu ne doit pas être un cul-de-sac, même en panne."""
        preview = self._preview("re", list_error=RuntimeError("db down"))
        self.assertFalse(preview["ok"])
        self.assertEqual(preview["reason"], "lookup_failed")
        self.assertIn("db down", preview["text"])
        self.assertIn("Rien n'a été modifié", preview["text"])
        self.assertEqual(
            [label for row in preview["keyboard"] for label, _ in row], ["✖️ Annuler"]
        )
        self.assertEqual(self._payloads(preview, "cancel"), ("re", "signals"))

    def test_a_broken_channel_or_action_offers_no_keyboard(self):
        """Il n'y a rien à nommer : des boutons muets seraient pires que pas de boutons."""
        bad_channel = self._preview("re", [self._bot("s1")], channel="  ")
        bad_verdict = self._preview("no", [self._bot("s1")])
        for preview, reason in ((bad_channel, "no_channel"), (bad_verdict, "bad_verdict")):
            with self.subTest(reason=reason):
                self.assertFalse(preview["ok"])
                self.assertEqual(preview["reason"], reason)
                self.assertEqual(preview["keyboard"], [])
                self.assertIn("rien n'a été modifié", preview["text"])

    def test_a_very_long_line_stays_within_telegram(self):
        """Un nom de fichier de 500 caractères ne doit pas faire échouer l'envoi."""
        row = self._bot("s1")
        row["file_name"] = "x" * 500
        rows = [row] * 25
        rows = [dict(item, id=f"s{i}") for i, item in enumerate(rows)]
        preview = self._preview("re", rows, limit=25)
        content = preview["text"].splitlines()[2:-2]
        self.assertEqual(len(content), 25)
        for line in content:
            self.assertLessEqual(len(line), len("25. ") + telegram_media.BULK_PREVIEW_CHARS)
        self.assertLess(len(preview["text"]), 4096)

    def test_the_toast_says_nothing_has_happened_yet(self):
        """Le seul retour immédiat quand la liste met une seconde à être relue."""
        preview = self._preview("re", [self._bot("s1"), self._bot("s2")])
        self.assertIn("2 visée(s)", telegram_media.preview_toast(preview))
        self.assertIn("encore", telegram_media.preview_toast(preview))
        empty = self._preview("re", [self._bot("n1", "news")])
        self.assertIn("Plus rien en attente", telegram_media.preview_toast(empty))
        nothing = self._preview("ok", [self._bot("s1")], indexed=set())
        self.assertIn("Rien à confirmer", telegram_media.preview_toast(nothing))
        broken = self._preview("re", list_error=RuntimeError("db down"))
        self.assertIn("Aperçu impossible", telegram_media.preview_toast(broken))


class BulkReviewChannelTest(unittest.IsolatedAsyncioTestCase):
    """`bulk_review_channel` : ce qui est visé, ce qui est fait, ce qui reste."""

    def _bot(self, media_id, channel="signals", message_id=1):
        return {
            "id": media_id,
            "metadata": {"channel_post": {"channel": channel, "message_id": message_id}},
        }

    def _deps(self, rows, *, indexed=(), outcomes=None, list_error=None):
        calls = []
        results = dict(outcomes or {})

        async def review(media_id, verdict, *, reviewer=None):
            calls.append((media_id, verdict, reviewer))
            outcome = results.get(media_id, {"ok": True})
            if isinstance(outcome, Exception):
                raise outcome
            return dict(outcome)

        def list_pending():
            if list_error is not None:
                raise list_error
            return list(rows)

        return calls, {
            "list_pending": list_pending,
            "indexed_of": lambda _rows: set(indexed),
            "review": review,
        }

    async def _bulk(self, verdict, rows, *, channel="signals", limit=None, **params):
        calls, deps = self._deps(rows, **params)
        extra = {} if limit is None else {"limit": limit}
        result = await telegram_media.bulk_review_channel(
            channel, verdict, reviewer="42", **deps, **extra
        )
        return calls, result

    async def test_a_batch_only_touches_the_channel_it_names(self):
        """« Toutes les extractions d'un même canal » : pas une de plus."""
        rows = [
            self._bot("s1"),
            self._bot("s2"),
            self._bot("n1", "news"),
            self._bot("n2", "news"),
        ]
        calls, result = await self._bulk("ok", rows, indexed={"s1", "s2", "n1", "n2"})
        self.assertEqual([call[0] for call in calls], ["s1", "s2"])
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["validated"], 2)

    async def test_the_channel_is_matched_without_its_at_sign(self):
        rows = [self._bot("s1"), self._bot("s2")]
        calls, result = await self._bulk(
            "ok", rows, channel="@signals", indexed={"s1", "s2"}
        )
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["label"], "@signals")

    async def test_validating_skips_what_has_nothing_indexed(self):
        """Écrire une relecture sur une extraction vide serait l'état trompeur à éviter."""
        rows = [self._bot("s1"), self._bot("s2"), self._bot("s3")]
        calls, result = await self._bulk("ok", rows, indexed={"s2"})
        self.assertEqual([call[0] for call in calls], ["s2"])
        self.assertEqual(result["validated"], 1)
        self.assertEqual(result["skipped"], 2)

    async def test_reindexing_visits_everything_left(self):
        """La réindexation, elle, a du sens sur une extraction vide : c'est le rattrapage."""
        rows = [self._bot(f"s{i}") for i in range(3)]
        calls, result = await self._bulk("re", rows)
        self.assertEqual([call[1] for call in calls], ["re", "re", "re"])
        self.assertEqual(result["requested"], 3)

    async def test_the_verdict_is_attributed_to_the_reviewer(self):
        calls, _ = await self._bulk("re", [self._bot("s1"), self._bot("s2")])
        self.assertEqual([call[2] for call in calls], ["42", "42"])

    async def test_a_refused_row_is_named_and_does_not_stop_the_batch(self):
        rows = [self._bot("s1"), self._bot("s2")]
        calls, result = await self._bulk(
            "re", rows, outcomes={"s1": {"ok": False, "reason": "no_object"}}
        )
        self.assertEqual(len(calls), 2, "la ligne suivante doit être traitée quand même")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["issues"][0]["reason"], "fichier absent du stockage")

    async def test_an_exception_on_one_row_becomes_a_named_failure(self):
        rows = [self._bot("s1"), self._bot("s2")]
        _, result = await self._bulk(
            "ok",
            rows,
            indexed={"s1", "s2"},
            outcomes={"s1": RuntimeError("boum")},
        )
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["issues"][0]["reason"], "panne inattendue")
        self.assertEqual(result["validated"], 1)

    async def test_the_cap_says_what_it_left_behind(self):
        """Un clic ne doit pas lancer deux cents réindexations sans le dire."""
        rows = [self._bot(f"s{i}") for i in range(30)]
        calls, result = await self._bulk("re", rows, limit=25)
        self.assertEqual(result["requested"], 25)
        self.assertEqual(result["remaining"], 5)
        self.assertEqual(len(calls), 25)

    async def test_a_row_without_identifier_is_counted_apart(self):
        rows = [self._bot("s1"), {"id": None, "metadata": {"channel": "signals"}}]
        calls, result = await self._bulk("re", rows)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(calls), 1)

    async def test_the_amount_of_text_produced_is_summed(self):
        rows = [self._bot("s1"), self._bot("s2")]
        outcomes = {
            "s1": {"ok": True, "chunks": 3, "extraction": {"chars": 120}},
            "s2": {"ok": True, "chunks": 0, "extraction": {"chars": 0}},
        }
        _, result = await self._bulk("re", rows, outcomes=outcomes)
        self.assertEqual(result["reindexed"], 1)
        self.assertEqual(result["textless"], 1)
        self.assertEqual(result["chunks"], 3)
        self.assertEqual(result["chars"], 120)
        self.assertEqual(result["handled"], 2)

    async def test_a_channel_with_nothing_left_says_so(self):
        """Un verdict tombé entre l'affichage et le clic : le dire, pas le taire."""
        rows = [self._bot("n1", "news"), self._bot("n2", "news")]
        calls, result = await self._bulk("ok", rows)
        self.assertTrue(result["ok"])
        self.assertEqual(result["requested"], 0)
        self.assertEqual(calls, [])

    async def test_an_unreadable_backlog_is_a_reason_not_a_crash(self):
        _, result = await self._bulk("ok", [], list_error=RuntimeError("db down"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "lookup_failed")
        self.assertIn("db down", result["error"])

    async def test_a_verdict_outside_the_two_actions_is_refused(self):
        calls, result = await self._bulk("no", [self._bot("s1"), self._bot("s2")])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "bad_verdict")
        self.assertEqual(calls, [])


class FormatBulkReportTest(unittest.TestCase):
    """Un clic, un compte-rendu : ce qui a été fait, et ce qui reste."""

    def _result(self, **overrides):
        result = {
            "ok": True,
            "channel": "signals",
            "label": "@signals",
            "verdict": "ok",
            "requested": 5,
            "handled": 5,
            "remaining": 0,
            "validated": 5,
            "reindexed": 0,
            "textless": 0,
            "failed": 0,
            "skipped": 0,
            "chunks": 0,
            "chars": 0,
            "issues": [],
            "total_issues": 0,
            "reason": None,
        }
        result.update(overrides)
        return result

    def test_the_header_names_the_channel_and_the_batch(self):
        text = telegram_media.format_bulk_report(self._result())
        self.assertIn("@signals", text)
        self.assertIn("5", text)

    def test_a_validation_says_what_it_kept(self):
        self.assertIn("reste indexé", telegram_media.format_bulk_report(self._result()))

    def test_a_validation_counts_what_it_could_not_validate(self):
        text = telegram_media.format_bulk_report(self._result(validated=3, skipped=2))
        self.assertIn("3 validée(s)", text)
        self.assertIn("2 sans texte indexé", text)

    def test_a_reindexation_sums_the_text_it_produced(self):
        text = telegram_media.format_bulk_report(
            self._result(verdict="re", validated=0, reindexed=4, chars=1200, chunks=9)
        )
        self.assertIn("4 réindexée(s)", text)
        self.assertIn("1200 car.", text)
        self.assertIn("9 morceau(x)", text)

    def test_a_reindexation_that_read_nothing_is_not_an_error(self):
        """La réindexation a eu lieu : la compter comme un échec ferait tout relancer."""
        text = telegram_media.format_bulk_report(
            self._result(verdict="re", validated=0, reindexed=1, textless=2)
        )
        self.assertIn("2 refaite(s) sans texte extractible", text)
        self.assertNotIn("❌", text)

    def test_the_failures_are_named(self):
        text = telegram_media.format_bulk_report(
            self._result(
                validated=3,
                failed=2,
                issues=[
                    {"media_id": "abcdef1234567890", "reason": "fichier absent du stockage"},
                    {"media_id": "0987654321fedcba", "reason": "média introuvable en base"},
                ],
                total_issues=2,
            )
        )
        self.assertIn("❌ 2 échec(s)", text)
        self.assertIn("abcdef12", text)
        self.assertIn("fichier absent du stockage", text)

    def test_the_failure_list_is_capped(self):
        """Un lot de vingt-cinq pannes donnerait un message plus long que la liste."""
        issues = [
            {"media_id": f"m{i}", "reason": "panne inattendue"}
            for i in range(telegram_media.BULK_ISSUE_LINES)
        ]
        text = telegram_media.format_bulk_report(
            self._result(failed=9, issues=issues, total_issues=9)
        )
        self.assertIn("… et 4 autre(s)", text)
        self.assertEqual(text.count("panne inattendue"), telegram_media.BULK_ISSUE_LINES)

    def test_what_the_cap_left_behind_is_announced(self):
        text = telegram_media.format_bulk_report(self._result(remaining=12))
        self.assertIn("12 laissée(s) de côté", text)
        self.assertIn(str(telegram_media.CHANNEL_BULK_MAX), text)

    def test_a_channel_with_nothing_left_says_a_verdict_landed(self):
        text = telegram_media.format_bulk_report(self._result(requested=0, validated=0))
        self.assertIn("Plus rien en attente", text)
        self.assertIn("@signals", text)

    def test_an_unreadable_list_names_the_error_and_says_nothing_changed(self):
        text = telegram_media.format_bulk_report(
            self._result(ok=False, reason="lookup_failed", error="RuntimeError: db down")
        )
        self.assertIn("db down", text)
        self.assertIn("Rien n'a été modifié", text)

    def test_an_unknown_action_says_nothing_changed(self):
        text = telegram_media.format_bulk_report(
            self._result(ok=False, reason="bad_verdict")
        )
        self.assertIn("rien n'a été modifié", text)

    def test_a_healthy_report_does_not_shout(self):
        text = telegram_media.format_bulk_report(self._result())
        self.assertNotIn("❌", text)


class PendingChannelCallbackWiringTest(unittest.TestCase):
    """Le lot par canal vu depuis `main.py` (non importable ici).

    Trois handlers pour un seul geste : la liste **ouvre l'aperçu** (`medc:`),
    l'aperçu **confirme** (`medck:`, le seul qui écrit) ou **annule** (`medcx:`).
    Tout l'intérêt de l'étape tient dans cette séparation, et c'est donc elle qui se
    vérifie ici : un lot qui repartirait d'un clic sur la liste agirait encore sur
    des lignes que rien n'a montrées.
    """

    MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"
    ASK = "media_pending_channel_callback"
    CONFIRM = "media_pending_channel_confirm_callback"
    CANCEL = "media_pending_channel_cancel_callback"

    def _source(self) -> str:
        return self.MAIN.read_text(encoding="utf-8")

    def _body(self, name: str) -> str:
        text = self._source()
        return text.split(f"async def {name}", 1)[1].split("\nasync def ", 1)[0]

    def test_the_three_callbacks_are_registered_before_the_generic_handler(self):
        """Sinon `button_handler` verrait `medc:` avant le lot."""
        text = self._source()
        generic = text.index("CallbackQueryHandler(button_handler)")
        for name, prefix in (
            (self.ASK, "^medc:"),
            (self.CONFIRM, "^medck:"),
            (self.CANCEL, "^medcx:"),
        ):
            with self.subTest(prefix=prefix):
                self.assertLess(
                    text.index(f'CallbackQueryHandler({name}, pattern=r"{prefix}")'),
                    generic,
                )

    def test_the_verdict_and_the_channel_are_parsed_by_the_module(self):
        self.assertIn("telegram_media.parse_channel_bulk(query.data)", self._body(self.ASK))

    def test_an_unreadable_button_answers_without_touching_the_message(self):
        body = self._body(self.ASK)
        refusal = body.split("if not parsed:", 1)[1].split("verdict, channel = parsed", 1)[0]
        self.assertIn("await query.answer(", refusal)
        self.assertNotIn("edit_message_text", refusal)

    def test_the_list_click_opens_the_preview_and_writes_nothing(self):
        """Le cœur de l'étape : le premier clic **montre**, il n'agit pas."""
        ask = self._body(self.ASK)
        self.assertIn("telegram_media.bulk_preview_view", ask)
        self.assertIn("preview[\"text\"]", ask)
        self.assertIn('reply_markup=_list_keyboard(preview["keyboard"])', ask)
        self.assertIn("asyncio.to_thread", ask, "lecture réseau : hors de l'event loop")
        self.assertNotIn("bulk_review_channel", ask)
        self.assertNotIn("format_bulk_report", ask)

    def test_the_preview_toast_says_nothing_has_happened_yet(self):
        """L'aperçu met une seconde à se relire : sans mot, le clic semblerait perdu."""
        self.assertIn("telegram_media.preview_toast(preview)", self._body(self.ASK))

    def test_the_confirmation_reads_its_own_payload(self):
        self.assertIn(
            "prefix=telegram_media.CHANNEL_BULK_CONFIRM_PREFIX", self._body(self.CONFIRM)
        )

    def test_the_batch_is_relied_at_click_time(self):
        """Un verdict tombé entre l'affichage et le clic ne doit pas être rouvert."""
        body = self._body(self.CONFIRM)
        self.assertIn("telegram_media.bulk_review_channel(", body)
        self.assertIn("reviewer=str(update.effective_user.id)", body)

    def test_a_batch_is_not_a_per_line_verdict(self):
        """Le lot ne passe pas par le corps commun d'une ligne : il en fait plusieurs."""
        for name in (self.ASK, self.CONFIRM, self.CANCEL):
            body = self._body(name)
            with self.subTest(handler=name):
                self.assertNotIn("parse_review_callback", body)
                self.assertNotIn("review_media", body)
                self.assertNotIn("_review_from_list", body)

    def test_the_report_precedes_the_list(self):
        """Remplacer la liste obligerait à retaper `/pending` pour continuer."""
        body = self._body(self.CONFIRM)
        composed = [
            line for line in body.splitlines() if "format_bulk_report(result)" in line
        ]
        self.assertTrue(
            any("rendered['text']" in line for line in composed),
            "le compte-rendu doit être composé **avec** la liste, pas à sa place",
        )
        self.assertIn('reply_markup=_list_keyboard(rendered["keyboard"])', body)
        self.assertIn("asyncio.to_thread", body, "lecture réseau : hors de l'event loop")

    def test_a_failed_rerender_still_reports_the_batch(self):
        body = self._body(self.CONFIRM)
        self.assertIn("except Exception", body)
        self.assertIn("edit_message_text(telegram_media.format_bulk_report(result))", body)

    def test_the_result_is_toasted_like_a_list_verdict(self):
        self.assertIn("telegram_media.bulk_toast(result)", self._body(self.CONFIRM))

    def test_the_cancellation_returns_to_the_list_and_touches_nothing(self):
        """Une sortie qui ne fait rien : sans elle, l'aperçu serait un cul-de-sac."""
        body = self._body(self.CANCEL)
        self.assertIn("prefix=telegram_media.CHANNEL_BULK_CANCEL_PREFIX", body)
        self.assertIn("telegram_media.pending_review_view", body)
        self.assertIn('reply_markup=_list_keyboard(view["keyboard"])', body)
        self.assertIn("rien n'a été modifié", body)
        self.assertNotIn("bulk_review_channel", body)
        self.assertNotIn("review_media", body)

    def test_the_batch_is_documented_in_start(self):
        """Un geste qui n'est pas dans `/start` n'existe pas pour l'opérateur."""
        body = self._body("start")
        self.assertIn("@canal", body)
        self.assertIn("Confirmer le lot", body)
        self.assertIn("Annuler", body)


class MediaReviewWiringTest(unittest.TestCase):
    """`main.py` n'est pas importable ici : on vérifie son câblage à la lecture."""

    MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"

    def _body(self, name: str) -> str:
        text = self.MAIN.read_text(encoding="utf-8")
        return text.split(f"async def {name}", 1)[1].split("\nasync def ", 1)[0]

    def test_the_ingestion_reply_carries_the_review_keyboard(self):
        """Le corps vit dans `_ingest_one` depuis que les albums partagent le chemin
        (média hors album, et album réduit à un seul élément) : c'est là que la
        garantie se lit, et le routage qui y mène est vérifié juste après."""
        self.assertIn("reply_markup=_review_keyboard(result)", self._body("_ingest_one"))
        self.assertIn("_ingest_one(", self._body("_handle_telegram_media"))

    def test_the_review_callback_precedes_the_generic_handler(self):
        """Sinon `button_handler` prendrait `med:` pour un identifiant de signal."""
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertLess(
            text.index('CallbackQueryHandler(media_review_callback, pattern=r"^med:")'),
            text.index("CallbackQueryHandler(button_handler)"),
        )

    def test_the_verdict_is_attributed_to_the_reviewer(self):
        body = self._body("media_review_callback")
        self.assertIn("telegram_media.parse_review_callback(query.data)", body)
        self.assertIn("reviewer=str(update.effective_user.id)", body)

    def test_the_follow_up_keyboard_replaces_the_old_one(self):
        """Les boutons doivent changer avec l'état, pas rester figés sur « ✅ / ❌ »."""
        body = self._body("media_review_callback")
        self.assertIn("telegram_media.format_review_report(result)", body)
        self.assertIn("reply_markup=_follow_up_keyboard(result)", body)
        self.assertIn("telegram_media.follow_up_buttons(result)", self.MAIN.read_text(encoding="utf-8"))

    def test_only_reviewable_ingestions_get_buttons(self):
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertIn("telegram_media.review_target(result)", text)

    def test_the_extracted_text_follows_the_report(self):
        body = self._body("_ingest_one")
        self.assertIn("_send_extracted_text(bot, message.chat_id, result)", body)

    def test_an_album_element_is_set_aside_instead_of_answered(self):
        """C'est le côte à côte qui fait la réponse unique : un élément d'album
        entre dans le tampon, et **rien** n'est répondu à ce moment-là."""
        body = self._body("_handle_telegram_media")
        self.assertIn("telegram_media.album_group(message)", body)
        self.assertIn("_album_buffer.add(group, (message, context))", body)
        # Le média hors album, lui, est traité tout de suite.
        self.assertLess(body.index("_album_buffer.add"), body.index("_ingest_one("))

    def test_the_album_batch_is_delivered_once_under_the_last_element(self):
        body = self._body("_deliver_album")
        self.assertIn("telegram_media.ingest_album(messages, download=download)", body)
        self.assertIn("telegram_media.album_report_view", body)
        self.assertIn("reply_markup=_list_keyboard(view[\"keyboard\"])", body)
        self.assertIn("last = messages[-1]", body)
        self.assertIn("last.reply_text", body)

    def test_the_album_buffer_takes_its_window_from_the_module(self):
        """Un seuil recopié ici pourrait diverguer de celui que le module annonce."""
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertIn(
            "telegram_media.AlbumBuffer(\n    telegram_media.ALBUM_WINDOW_SECONDS, _deliver_album\n)",
            text,
        )

    def test_a_one_element_album_keeps_the_single_report(self):
        """Telegram livre parfois un seul élément : rien ne justifie alors de
        changer de compte-rendu (et le clavier de liste n'aurait qu'une ligne)."""
        body = self._body("_deliver_album")
        self.assertIn("if len(messages) == 1:", body)
        self.assertIn("_ingest_one(messages[0], download=download, bot=context.bot)", body)

    def test_a_reindexed_extraction_gets_the_same_attachment(self):
        body = self._body("media_review_callback")
        self.assertIn("_send_extracted_text(context.bot, update.effective_chat.id, result)", body)


class ExtractedTextSendingTest(unittest.TestCase):
    """Le `.txt` est envoyé par `main.py` — vérifié à la lecture (module non importable)."""

    MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"

    def _sender(self) -> str:
        text = self.MAIN.read_text(encoding="utf-8")
        return text.split("async def _send_extracted_text", 1)[1].split("\nasync def ", 1)[0]

    def test_the_document_comes_from_the_module_bytes(self):
        body = self._sender()
        self.assertIn("telegram_media.attachment_for(result)", body)
        self.assertIn("io.BytesIO(attachment[\"content\"])", body)
        self.assertIn("filename=attachment[\"filename\"]", body)

    def test_an_unsendable_file_is_announced_with_the_excerpt(self):
        """Annoncer une pièce jointe qui n'arrive pas laisserait l'utilisateur sans rien."""
        body = self._sender()
        self.assertIn("except Exception", body)
        self.assertIn("non envoyé en pièce jointe", body)
        self.assertIn("Aperçu : {excerpt}", body)
        self.assertIn('return None', body, "rien à envoyer n'est pas un échec")

    def test_input_file_is_imported(self):
        self.assertIn("import io", self.MAIN.read_text(encoding="utf-8"))
        self.assertIn(
            "from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile",
            self.MAIN.read_text(encoding="utf-8"),
        )


def _channel_message(**overrides):
    """Message de **canal** duck-typé (`chat.type == "channel"`)."""
    chat = {
        "type": "channel",
        "id": -1001234567890,
        "title": "Crypto Signals",
        "username": "crypto_signals",
    }
    chat.update(overrides.pop("chat", {}))
    base = {
        "message_id": 42,
        "chat": SimpleNamespace(**chat),
        "from_user": SimpleNamespace(id=7, full_name="Ada", username="ada"),
        "caption": None,
        "photo": [SimpleNamespace(file_id="fid", file_size=10, width=1, height=1)],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class ChannelOriginTest(unittest.TestCase):
    """Reconnaître une publication de canal, et sous quelle clé la ranger."""

    def test_a_channel_post_is_described(self):
        origin = telegram_media.channel_origin(_channel_message())
        self.assertEqual(origin["chat_id"], "crypto_signals")
        self.assertEqual(origin["channel"], "crypto_signals")
        self.assertEqual(origin["title"], "Crypto Signals")
        self.assertEqual(origin["channel_id"], -1001234567890)
        self.assertEqual(origin["message_id"], 42)
        self.assertEqual(origin["author_id"], 7)
        self.assertEqual(origin["author"], "Ada")

    def test_a_private_channel_is_keyed_by_its_id(self):
        """Sans nom public, il n'y a pas d'équivalent côté scraper : l'id suffit."""
        origin = telegram_media.channel_origin(
            _channel_message(chat={"username": None, "title": "Privé"})
        )
        self.assertEqual(origin["chat_id"], "-1001234567890")
        self.assertEqual(origin["channel"], "Privé")
        self.assertIsNone(origin["username"])

    def test_an_anonymous_post_has_no_author(self):
        """« En tant que canal » : pas d'auteur, donc aucune cible évidente."""
        origin = telegram_media.channel_origin(_channel_message(from_user=None))
        self.assertIsNone(origin["author_id"])
        self.assertIsNone(origin["author"])

    def test_a_direct_message_is_not_a_channel(self):
        for chat_type in ("private", "group", "supergroup"):
            with self.subTest(chat=chat_type):
                message = _channel_message(chat={"type": chat_type})
                self.assertIsNone(telegram_media.channel_origin(message))


class ChannelIngestionTest(unittest.IsolatedAsyncioTestCase):
    """Ingestion d'un média de canal : clé du canal, et pas de doublon croisé."""

    def _upload(self, uploads):
        def upload(data, **kwargs):
            uploads.append(kwargs)
            return {"id": "media-canal"}

        return upload

    async def _ingest(self, message, *, channel=None, find_message=None, uploads=None, **kwargs):
        async def download(file_id):
            return b"donnees"

        return await telegram_media.ingest_media(
            message,
            download=download,
            upload=self._upload(uploads if uploads is not None else []),
            extract=lambda *a, **k: {"ok": False, "reason": "no extractor"},
            store=lambda *a, **k: 0,
            record=_recorder(),
            tag_store=lambda *a, **k: {"stored": False},
            channel=channel,
            find_message=find_message,
            **kwargs,
        )

    async def test_the_media_is_filed_under_the_channel(self):
        uploads = []
        message = _channel_message()
        await self._ingest(
            message, channel=telegram_media.channel_origin(message), uploads=uploads
        )
        self.assertEqual(uploads[0]["chat_id"], "crypto_signals")
        self.assertEqual(uploads[0]["message_id"], 42)

    async def test_the_channel_is_recorded_in_the_metadata(self):
        uploads = []
        message = _channel_message()
        await self._ingest(
            message, channel=telegram_media.channel_origin(message), uploads=uploads
        )
        recorded = uploads[0]["metadata"]["channel_post"]
        self.assertEqual(recorded["channel"], "crypto_signals")
        self.assertEqual(recorded["channel_id"], -1001234567890)
        self.assertEqual(recorded["message_id"], 42)
        self.assertEqual(recorded["author_id"], 7)

    async def test_without_a_channel_the_chat_id_is_kept(self):
        """La route existante ne change pas : c'est la clé du chat qui compte."""
        uploads = []
        await self._ingest(
            _message(photo=[SimpleNamespace(file_id="fid", file_size=1, width=1, height=1)]),
            uploads=uploads,
        )
        self.assertEqual(uploads[0]["chat_id"], "12345")
        self.assertNotIn("channel_post", uploads[0]["metadata"])

    async def test_what_the_public_preview_already_took_is_not_downloaded_again(self):
        """Le scraper n'a pas de `file_id` : seule la clé (canal, message) le retrouve."""
        message = _channel_message()
        row = {"id": "media-apercu", "file_name": "photo_42.jpg", "storage_path": "x"}
        calls = []

        def find_message(chat_id, message_id):
            calls.append((chat_id, message_id))
            return row

        async def download(file_id):  # pragma: no cover - ne doit pas être appelé
            raise AssertionError("un message déjà ingéré ne doit pas être re-téléchargé")

        result = await telegram_media.ingest_media(
            message,
            download=download,
            upload=mock.Mock(side_effect=AssertionError("rien à réécrire")),
            channel=telegram_media.channel_origin(message),
            find_message=find_message,
        )

        self.assertEqual(calls, [("crypto_signals", 42)])
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["duplicate_reason"], "message")
        self.assertEqual(result["media"]["id"], "media-apercu")

    async def test_a_private_channel_has_nothing_to_compare_with(self):
        """Sans nom public, le scraper n'a rien pu lire : on ne consulte pas la base."""
        message = _channel_message(chat={"username": None})
        calls = []
        result = await self._ingest(
            message,
            channel=telegram_media.channel_origin(message),
            find_message=lambda *a: calls.append(a),
        )
        self.assertEqual(calls, [])
        self.assertTrue(result["ok"])

    async def test_a_failing_channel_lookup_does_not_block_ingestion(self):
        def find_message(chat_id, message_id):
            raise RuntimeError("base indisponible")

        message = _channel_message()
        with contextlib.redirect_stdout(io.StringIO()):
            result = await self._ingest(
                message, channel=telegram_media.channel_origin(message), find_message=find_message
            )
        self.assertTrue(result["ok"])


class ChannelReportTest(unittest.TestCase):
    """Le compte-rendu du canal, destiné à un chat privé."""

    ORIGIN = {
        "channel": "crypto_signals",
        "title": "Crypto Signals",
        "username": "crypto_signals",
        "message_id": 42,
        "author_id": 7,
        "author": "Ada",
    }

    def test_the_report_names_the_channel_and_the_message(self):
        text = telegram_media.format_channel_report(
            {"ok": True, "media_type": "document", "bytes": 2048, "media": {"mime_type": "application/pdf", "id": "m"}},
            self.ORIGIN,
        )
        self.assertIn("Crypto Signals (@crypto_signals)", text)
        self.assertIn("Message : 42", text)
        self.assertIn("Ada", text)
        self.assertIn("✅ Média enregistré", text)

    def test_the_report_says_nothing_was_published_in_the_channel(self):
        """Sinon on croit la revue visible dans le canal, alors qu'elle ne l'est pas."""
        text = telegram_media.format_channel_report({"ok": True, "media": {}}, self.ORIGIN)
        self.assertIn("Rien n'a été publié dans le canal", text)

    def test_a_duplicate_message_is_not_described_as_a_duplicate_file(self):
        text = telegram_media.format_channel_report(
            {
                "duplicate": True,
                "duplicate_reason": "message",
                "media_type": "photo",
                "media": {"id": "m", "file_name": "photo_42.jpg"},
            },
            self.ORIGIN,
        )
        self.assertIn("Message déjà ingéré depuis ce canal", text)
        self.assertNotIn("telegram_file_id", text)

    def test_a_missing_origin_does_not_break_the_report(self):
        text = telegram_media.format_channel_report({"ok": True, "media": {}}, None)
        self.assertIn("canal inconnu", text)

    def test_the_author_comes_before_the_configured_chat(self):
        self.assertEqual(telegram_media.channel_report_targets(self.ORIGIN, "123"), [7, 123])

    def test_the_configured_chat_serves_an_anonymous_post(self):
        anonymous = {**self.ORIGIN, "author_id": None}
        self.assertEqual(telegram_media.channel_report_targets(anonymous, "123"), [123])

    def test_the_same_chat_is_not_tried_twice(self):
        self.assertEqual(telegram_media.channel_report_targets(self.ORIGIN, "7"), [7])

    def test_an_unusable_chat_id_is_ignored(self):
        for junk in (None, "", "pas un id", "@canal"):
            with self.subTest(value=junk):
                self.assertEqual(telegram_media.channel_report_targets({"author_id": None}, junk), [])

    def test_an_unusable_chat_id_is_named_rather_than_silently_dropped(self):
        """Écarter une cible illisible est juste ; le taire ne l'est pas.

        Une revue qui ne part nulle part ne se voit qu'en ne la recevant pas :
        l'opérateur ne peut pas distinguer « aucune publication » de
        « TELEGRAM_ADMIN_CHAT_ID collé de travers ».
        """
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            targets = telegram_media.channel_report_targets({"author_id": None}, "@mauvais")
        self.assertEqual(targets, [])
        self.assertIn("@mauvais", captured.getvalue())
        self.assertIn("inexploitable", captured.getvalue())

    def test_an_empty_candidate_is_not_noise(self):
        """Absent n'est pas illisible : il n'y a rien à signaler."""
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            telegram_media.channel_report_targets({"author_id": None}, "")
        self.assertEqual(captured.getvalue(), "")


class ChannelPostWiringTest(unittest.TestCase):
    """`main.py` : la route canal telle qu'elle est câblée."""

    MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"

    def _body(self, name: str) -> str:
        text = self.MAIN.read_text(encoding="utf-8")
        return text.split(f"async def {name}", 1)[1].split("\nasync def ", 1)[0]

    def test_the_update_type_is_requested(self):
        """Sans `channel_post` dans `allowed_updates`, Telegram n'en livre aucun."""
        self.assertIn('"channel_post"', self.MAIN.read_text(encoding="utf-8"))

    def test_the_channel_route_precedes_the_direct_one(self):
        """Un seul handler par groupe traite une mise à jour : l'ordre décide."""
        text = self.MAIN.read_text(encoding="utf-8")
        self.assertLess(
            text.index("telegram_filters.CHANNEL_MEDIA, handle_channel_post"),
            text.index("telegram_filters.DIRECT_PHOTO, handle_photo"),
        )

    def test_the_channel_is_handed_to_the_ingestion(self):
        body = self._body("handle_channel_post")
        self.assertIn("telegram_media.channel_origin(message)", body)
        self.assertIn("channel=channel", body)

    def test_nothing_is_published_in_the_channel(self):
        """Répondre à un `channel_post` s'afficherait devant tous les abonnés."""
        # Le corps est lu **hors docstring** : la prose du module explique
        # justement pourquoi `reply_text` est proscrit ici.
        parts = self._body("handle_channel_post").split('"""')
        body = parts[2] if len(parts) > 2 else parts[0]
        self.assertNotIn("reply_text", body)
        self.assertIn("_report_channel_media", body)

    def test_the_private_routes_do_not_take_channel_posts(self):
        text = self.MAIN.read_text(encoding="utf-8")
        for handler in ("handle_photo", "handle_video", "handle_document", "handle_voice", "handle_audio"):
            with self.subTest(handler=handler):
                self.assertNotIn(f"filters, {handler}", text)
        self.assertIn("telegram_filters.DIRECT_DOCUMENT, handle_document", text)

    def test_the_report_goes_to_the_resolved_private_chats(self):
        body = self._body("_report_channel_media")
        self.assertIn("telegram_media.channel_report_targets(channel, TELEGRAM_ADMIN_CHAT_ID)", body)
        self.assertIn("telegram_media.format_channel_report(result, channel)", body)
        self.assertIn("bot.send_message", body)

    def test_an_undeliverable_report_is_announced(self):
        """L'ingestion a eu lieu : c'est la revue qui est reportée, et ça se dit."""
        body = self._body("_report_channel_media")
        self.assertIn("TELEGRAM_ADMIN_CHAT_ID", body)
        self.assertIn("compte-rendu non envoye", body)
        self.assertIn("_send_extracted_text(bot, chat_id, result)", body)

# --------------------------------------------------------------------------- #
# Albums : un envoi de plusieurs photos, traité en un seul lot
# --------------------------------------------------------------------------- #


def _stored_element(
    media_id: str = "media-1",
    *,
    asset="BTC-USD",
    asset_source: str = "caption",
    chunks: int = 4,
    excerpt: str = "Chart of BTC/USD with support at 64k",
    text: str = "range 64k",
    size: int = 812_345,
    **overrides,
) -> dict:
    """Résultat d'ingestion d'un élément **enregistré** (forme rendue par `ingest_media`)."""
    element = {
        "ok": True,
        "media_type": "photo",
        "bytes": size,
        "media": {"id": media_id, "metadata": {"telegram": True, "media_group_id": "grp-1"}},
        "asset_stored": True,
        "extraction": {
            "ok": True,
            "method": "vision",
            "chunks": chunks,
            "chars": len(text),
            "excerpt": excerpt,
            "text": text,
            "asset": asset,
            "asset_source": asset_source,
        },
    }
    element.update(overrides)
    return element


def _duplicate_element(media_id: str = "media-2", **overrides) -> dict:
    element = {"ok": True, "duplicate": True, "media_type": "photo", "media": {"id": media_id}}
    element.update(overrides)
    return element


def _failed_element(reason: str = "too_large", **overrides) -> dict:
    element = {"ok": False, "reason": reason, "media_type": "video"}
    element.update(overrides)
    return element


def _album_report(*results) -> dict:
    return {"album": True, "count": len(results), "results": list(results)}


class AlbumGroupTest(unittest.TestCase):
    """Un album se reconnaît au `media_group_id`, avant même de savoir s'il y a un média."""

    def test_a_group_is_read_as_text(self):
        self.assertEqual(telegram_media.album_group(_message(media_group_id="grp-1")), "grp-1")
        self.assertEqual(telegram_media.album_group(_message(media_group_id=7)), "7")

    def test_a_message_outside_an_album_has_no_group(self):
        for value in (None, ""):
            with self.subTest(group=value):
                self.assertIsNone(telegram_media.album_group(_message(media_group_id=value)))
        self.assertIsNone(telegram_media.album_group(_message()))


class AlbumBufferTest(unittest.IsolatedAsyncioTestCase):
    """Le tampon : un album livré d'un coup, après un silence, **hors** du handler."""

    class Gate:
        """`sleep` sous contrôle : rien ne se livre tant que le test n'a pas ouvert."""

        def __init__(self):
            self.delays: list = []
            self._event = asyncio.Event()

        async def sleep(self, delay):
            self.delays.append(delay)
            await self._event.wait()

        def release(self):
            self._event.set()

        def reset(self):
            self._event = asyncio.Event()

    def _buffer(self, gate):
        delivered: list = []

        async def deliver(group_id, batch):
            delivered.append((group_id, list(batch)))

        buffer = telegram_media.AlbumBuffer(
            telegram_media.ALBUM_WINDOW_SECONDS, deliver, sleep=gate.sleep
        )
        return buffer, delivered

    async def test_elements_arriving_together_form_a_single_batch(self):
        """Trois éléments, **une** livraison : chaque nouvel élément repousse le
        minuteur du précédent au lieu d'en créer un second — sinon un album un peu
        lent partirait en deux réponses, ce qu'on cherche justement à éviter."""
        gate = self.Gate()
        buffer, delivered = self._buffer(gate)
        for payload in ("a", "b", "c"):
            await buffer.add("grp", payload)
        self.assertEqual(delivered, [], "rien ne doit être livré pendant la fenêtre")
        gate.release()
        await buffer.drain()
        self.assertEqual(delivered, [("grp", ["a", "b", "c"])])

    async def test_an_element_arriving_once_the_window_started_joins_the_batch(self):
        """Le cas réel : le minuteur précédent **a déjà commencé** à attendre quand
        l'élément suivant arrive (1 s de fenêtre, un album étalé par le réseau).

        Les autres tests enchaînent les `add` sans jamais rendre la main à la
        boucle, donc le minuteur annulé n'a pas encore démarré et n'a rien retiré :
        un tampon qui viderait le lot **avant** l'attente passerait inaperçu. Ici
        le minuteur a démarré — s'il emporte ses éléments en se faisant annuler,
        l'album perd le premier et part en deux réponses.
        """
        gate = self.Gate()
        buffer, delivered = self._buffer(gate)
        await buffer.add("grp", "a")
        await asyncio.sleep(0)  # le minuteur atteint son attente
        self.assertEqual(
            gate.delays, [telegram_media.ALBUM_WINDOW_SECONDS], "le minuteur doit attendre"
        )
        await buffer.add("grp", "b")
        await asyncio.sleep(0)
        gate.release()
        await buffer.drain()
        self.assertEqual(delivered, [("grp", ["a", "b"])])

    async def test_two_albums_do_not_mix(self):
        gate = self.Gate()
        buffer, delivered = self._buffer(gate)
        await buffer.add("g1", "a1")
        await buffer.add("g2", "b1")
        await buffer.add("g1", "a2")
        gate.release()
        await buffer.drain()
        self.assertEqual(sorted(delivered), [("g1", ["a1", "a2"]), ("g2", ["b1"])])

    async def test_an_element_after_the_delivery_forms_a_new_batch(self):
        gate = self.Gate()
        buffer, delivered = self._buffer(gate)
        await buffer.add("grp", "a")
        gate.release()
        await buffer.drain()
        gate.reset()
        await buffer.add("grp", "b")
        gate.release()
        await buffer.drain()
        self.assertEqual(delivered, [("grp", ["a"]), ("grp", ["b"])])

    async def test_add_returns_before_the_window_elapses(self):
        """Le handler ne doit **pas** attendre la fenêtre : `python-telegram-bot`
        traite les mises à jour une par une (`max_concurrent_updates = 1`), donc un
        `add` bloquant empêcherait les éléments suivants d'arriver — et l'album se
        réduirait à son premier message, avec une réponse par photo, en retard."""
        gate = self.Gate()  # jamais ouvert : seul un add non bloquant passe
        buffer, _ = self._buffer(gate)
        await asyncio.wait_for(buffer.add("grp", "a"), timeout=0.5)

    async def test_the_window_is_the_advertised_one(self):
        gate = self.Gate()
        buffer, _ = self._buffer(gate)
        await buffer.add("grp", "a")
        gate.release()
        await buffer.drain()
        self.assertEqual(gate.delays, [telegram_media.ALBUM_WINDOW_SECONDS])

    async def test_a_failed_delivery_is_logged_and_does_not_escape(self):
        """Personne n'attend la tâche : une exception y disparaîtrait sans trace."""
        gate = self.Gate()

        async def deliver(group_id, batch):
            raise RuntimeError("Telegram indisponible")

        buffer = telegram_media.AlbumBuffer(
            telegram_media.ALBUM_WINDOW_SECONDS, deliver, sleep=gate.sleep
        )
        await buffer.add("grp", "a")
        gate.release()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            await buffer.drain()
        self.assertIn("livraison impossible", stream.getvalue())
        self.assertIn("Telegram indisponible", stream.getvalue())


class IngestAlbumTest(unittest.IsolatedAsyncioTestCase):
    """Un lot : chaque élément garde sa ligne, l'utilisateur n'a qu'une réponse."""

    def _messages(self, count: int, *, group: str = "grp-1"):
        return [
            _message(
                message_id=100 + index,
                media_group_id=group,
                photo=[SimpleNamespace(file_id=f"p{index}", file_size=4, width=1, height=1)],
            )
            for index in range(count)
        ]

    async def _ingest(self, messages, *, duplicates=(), broken=()):
        """Ingère un lot sans réseau : seuls doublons et pannes sont injectés."""
        uploads: list = []
        downloaded: list = []

        async def download(file_id):
            if file_id in broken:
                raise RuntimeError("lien expiré")
            downloaded.append(file_id)
            return b"data"

        def upload(data, **kwargs):
            uploads.append(kwargs)
            return {"id": f"media-{kwargs['telegram_file_id']}"}

        def lookup(file_id):
            return {"id": f"deja-{file_id}"} if file_id in duplicates else None

        with mock.patch.object(
            telegram_media.media_store, "find_media_by_telegram_file_id", side_effect=lookup
        ):
            report = await telegram_media.ingest_album(
                messages,
                download=download,
                upload=upload,
                extract=lambda *a, **k: {"ok": False},
                record=_recorder(),
            )
        return report, uploads, downloaded

    async def test_each_element_keeps_its_own_row(self):
        """Un album n'est pas un média à plusieurs fichiers : chaque élément a sa
        ligne, donc sa déduplication et sa revue. Seul le compte-rendu est commun."""
        report, uploads, _ = await self._ingest(self._messages(3))
        self.assertEqual(report["count"], 3)
        self.assertEqual(
            [result["media"]["id"] for result in report["results"]],
            ["media-p0", "media-p1", "media-p2"],
        )
        self.assertEqual(len(uploads), 3)
        self.assertEqual(
            [entry["metadata"]["media_group_id"] for entry in uploads], ["grp-1"] * 3
        )

    async def test_a_broken_element_does_not_stop_the_batch(self):
        """Une vidéo de 30 Mo au milieu de trois photos ne doit pas priver les
        photos de leur ingestion."""
        report, uploads, downloaded = await self._ingest(self._messages(3), broken={"p1"})
        self.assertEqual([result["ok"] for result in report["results"]], [True, False, True])
        self.assertEqual(report["results"][1]["reason"], "download_failed")
        self.assertEqual(downloaded, ["p0", "p2"])
        self.assertEqual(len(uploads), 2)

    async def test_a_replayed_element_is_reported_without_being_stored(self):
        report, uploads, _ = await self._ingest(self._messages(3), duplicates={"p1"})
        self.assertTrue(report["results"][1]["duplicate"])
        self.assertEqual(len(uploads), 2)

    async def test_an_unexpected_exception_falls_on_one_element_only(self):
        with mock.patch.object(
            telegram_media,
            "ingest_media",
            new=mock.AsyncMock(side_effect=[{"ok": True}, RuntimeError("boom")]),
        ):
            report = await telegram_media.ingest_album(
                self._messages(2), download=mock.AsyncMock()
            )
        self.assertEqual(report["count"], 2)
        self.assertTrue(report["results"][0]["ok"])
        self.assertEqual(report["results"][1]["reason"], "exception")
        self.assertIn("boom", report["results"][1]["error"])

    async def test_the_counts_cover_every_element(self):
        report, _, _ = await self._ingest(
            self._messages(3), duplicates={"p1"}, broken={"p2"}
        )
        counts = telegram_media.album_counts(report)
        self.assertEqual(counts, {"stored": 1, "duplicates": 1, "failed": 1})
        self.assertEqual(sum(counts.values()), report["count"])


class AlbumReportTest(unittest.TestCase):
    """Un compte-rendu pour tout l'album : une ligne par élément, numérotée."""

    def _lines(self, report) -> list:
        return telegram_media.format_album_report(report).splitlines()

    def _element_line(self, report, rank: int) -> str:
        return next(line for line in self._lines(report) if line.startswith(f"{rank}. "))

    def test_the_header_counts_each_category(self):
        text = telegram_media.format_album_report(
            _album_report(_stored_element(), _duplicate_element(), _failed_element())
        )
        self.assertIn("3 média(s)", text)
        self.assertIn("1 enregistré(s)", text)
        self.assertIn("1 déjà stocké(s)", text)
        self.assertIn("1 en échec", text)

    def test_every_element_has_a_numbered_line(self):
        report = _album_report(*(_stored_element(f"m{index}") for index in range(3)))
        lines = self._lines(report)
        for rank in (1, 2, 3):
            with self.subTest(rank=rank):
                self.assertTrue(
                    any(line.startswith(f"{rank}. ") for line in lines), f"ligne {rank} absente"
                )

    def test_a_stored_element_shows_its_size_its_extraction_and_its_tag(self):
        line = self._element_line(_album_report(_stored_element()), 1)
        self.assertIn("photo", line)
        self.assertIn(telegram_media._human_size(812_345), line)
        self.assertIn("✅ texte extrait (4 morceau(x))", line)
        self.assertIn("🏷️ BTC-USD (légende)", line)

    def test_the_reference_of_each_element_is_on_its_own_line(self):
        """C'est elle qui permet de rattraper un élément **précis** : la réponse au
        compte-rendu ne désigne, elle, que le dernier élément de l'album."""
        identifiers = [
            "8f14e45f-1a2b-4c3d-8e4f-556677889900",
            "2b7c9a10-3c4d-5e6f-7a8b-99aabbccddee",
        ]
        report = _album_report(
            _stored_element(identifiers[0], asset="BTC-USD"),
            _duplicate_element(identifiers[1]),
        )
        for rank, media_id in enumerate(identifiers, 1):
            with self.subTest(rank=rank):
                self.assertIn(media_id, self._element_line(report, rank))

    def test_a_duplicate_says_so(self):
        line = self._element_line(_album_report(_duplicate_element()), 1)
        self.assertIn("⏭️ déjà stocké", line)

    def test_a_duplicate_from_a_channel_says_which_route_took_it(self):
        element = _duplicate_element(duplicate_reason="message")
        self.assertIn("déjà ingéré depuis ce canal", self._element_line(_album_report(element), 1))

    def test_a_failure_names_its_cause_and_its_size(self):
        element = _failed_element("too_large", size=31_457_280)
        line = self._element_line(_album_report(element), 1)
        self.assertIn("❌ trop volumineux", line)
        self.assertIn("31.5 Mo", line)

    def test_a_technical_failure_keeps_its_reason(self):
        element = _failed_element("download_failed", error="TimedOut: lien expiré")
        line = self._element_line(_album_report(element), 1)
        self.assertIn("téléchargement impossible", line)
        self.assertIn("TimedOut: lien expiré", line)

    def test_a_multiline_reason_does_not_break_the_numbering(self):
        """Le numéro d'une ligne est le seul repère vers son bouton : un motif
        d'erreur multiligne ne doit pas créer de fausse ligne."""
        element = _failed_element("upload_failed", error="ligne 1\nligne 2")
        lines = self._lines(_album_report(element))
        self.assertEqual(len(lines), 2, "l'en-tête et une seule ligne d'élément")

    def test_an_extraction_without_text_is_flagged_with_its_reason(self):
        element = _stored_element(
            extraction={
                "ok": False,
                "reason": "GEMINI_API_KEY absente",
                "chunks": 2,
                "excerpt": "BTC 64k",
                "asset": "BTC-USD",
                "asset_source": "caption",
            }
        )
        line = self._element_line(_album_report(element), 1)
        self.assertIn("⚠️ texte non extrait (GEMINI_API_KEY absente)", line)
        self.assertIn("légende indexée seule", line)

    def test_a_long_extraction_points_to_the_txt_instead_of_a_preview(self):
        element = _stored_element(
            text="a" * (telegram_media.EXCERPT_CHARS + 50), excerpt="court"
        )
        text = telegram_media.format_album_report(_album_report(element))
        self.assertIn("→ .txt", text)
        self.assertNotIn("⤷", text, "le texte part en pièce jointe, pas en aperçu")

    def test_a_preview_is_bounded_to_one_line(self):
        report = _album_report(_stored_element(excerpt="mot " * 200))
        preview = next(line for line in self._lines(report) if line.strip().startswith("⤷"))
        self.assertLessEqual(len(preview), telegram_media.ALBUM_DETAIL_CHARS + 10)
        self.assertTrue(preview.endswith("…"))

    def test_the_absence_of_a_tag_is_said_once(self):
        tagged = _stored_element("media-1", asset="BTC-USD")
        untagged = _stored_element("media-2", asset=None)
        text = telegram_media.format_album_report(_album_report(tagged, untagged))
        self.assertEqual(text.count("Sans étiquette"), 1)
        self.assertNotIn("🏷️", self._element_line(_album_report(untagged), 1))
        self.assertNotIn("Sans étiquette", telegram_media.format_album_report(_album_report(tagged)))

    def test_an_empty_album_is_said_not_shown_empty(self):
        self.assertIn("Album vide", telegram_media.format_album_report(_album_report()))


class AlbumButtonsTest(unittest.TestCase):
    """Le clavier d'un album : une rangée par ligne, les mêmes numéros, et un
    verdict qui **ne remplace pas** le compte-rendu de l'envoi."""

    def _view(self, report, *, indexed=("media-1", "media-2", "media-3")):
        with mock.patch.object(
            telegram_media.knowledge_index, "media_with_chunks", return_value=set(indexed)
        ):
            return telegram_media.album_report_view(report)

    def _labels(self, view) -> list:
        return [label for row in view["keyboard"] for label, _data in row]

    def test_the_buttons_number_the_lines(self):
        report = _album_report(_stored_element("media-1"), _stored_element("media-2"))
        labels = self._labels(self._view(report))
        self.assertEqual(labels, ["✅ 1", "❌ 1", "✅ 2", "❌ 2"])

    def test_a_failed_element_keeps_its_rank(self):
        """Sinon le bouton 3 désignerait la ligne 4 : l'échec n'a pas de ligne
        média, donc pas de bouton, mais il garde sa place dans le compte-rendu."""
        report = _album_report(
            _stored_element("media-1"), _failed_element(), _stored_element("media-2")
        )
        labels = self._labels(self._view(report))
        self.assertIn("✅ 3", labels)
        self.assertNotIn("✅ 2", labels)

    def test_a_verdict_from_an_album_does_not_replace_the_report(self):
        """C'est tout l'intérêt du préfixe de **liste** : remplacer le message
        ferait disparaître les boutons des éléments pas encore relus."""
        report = _album_report(_stored_element("media-1"), _stored_element("media-2"))
        view = self._view(report)
        for row in view["keyboard"]:
            for _label, data in row:
                self.assertTrue(data.startswith(f"{telegram_media.LIST_REVIEW_PREFIX}:"), data)
                self.assertNotEqual(data.split(":")[0], telegram_media.REVIEW_PREFIX)

    def test_an_element_without_chunks_only_offers_a_reindex(self):
        report = _album_report(_stored_element("media-1", chunks=0))
        self.assertEqual(self._labels(self._view(report, indexed=())), ["↩️ 1"])

    def test_the_report_and_the_keyboard_are_built_from_the_same_entries(self):
        report = _album_report(*(_stored_element(f"m{index}") for index in range(3)))
        view = self._view(report, indexed={"m0", "m1", "m2"})
        in_lines = {
            int(line.split(".")[0]) for line in view["text"].splitlines() if line[:1].isdigit()
        }
        in_buttons = {int(label.split(" ")[1]) for label in self._labels(view)}
        self.assertEqual(in_buttons, in_lines)

    def test_one_query_serves_the_whole_batch(self):
        """Un album, c'est jusqu'à dix médias : demander dix fois ce qui est indexé
        ferait dix allers-retours pour un seul clavier."""
        report = _album_report(*(_stored_element(f"m{index}") for index in range(4)))
        with mock.patch.object(
            telegram_media.knowledge_index, "media_with_chunks", return_value={"m0"}
        ) as query:
            telegram_media.album_report_view(report)
        query.assert_called_once()
        self.assertEqual(sorted(query.call_args[0][0]), ["m0", "m1", "m2", "m3"])

    def test_a_batch_with_nothing_to_review_has_no_buttons(self):
        report = _album_report(_failed_element(), _failed_element("download_failed"))
        view = self._view(report)
        self.assertEqual(view["keyboard"], [])
        self.assertNotIn(telegram_media.LIST_REVIEW_HINT, view["text"])

    def test_the_review_hint_explains_the_numbered_buttons(self):
        report = _album_report(_stored_element("media-1"))
        view = self._view(report)
        self.assertIn(telegram_media.LIST_REVIEW_HINT, view["text"])
        self.assertTrue(view["text"].startswith(telegram_media.format_album_report(report)))


if __name__ == "__main__":
    unittest.main()
