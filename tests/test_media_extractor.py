"""Tests de l'extraction de contenu des médias (`ai/media_extractor.py`).

Les fournisseurs sont remplacés par des doublures (`sys.modules`, objets factices)
pour ne jamais toucher au réseau : on vérifie le **routage** par type et le
**comportement en l'absence** de clé ou de paquet, pas l'API des fournisseurs.
"""
from __future__ import annotations

import io
import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest import mock

from ai import media_extractor as me
from core import config_runtime


class ClassifyTest(unittest.TestCase):
    def test_image_by_mime_and_by_photo_type(self):
        self.assertEqual(me.classify("photo", "image/jpeg", "p.jpg"), "image")
        self.assertEqual(me.classify(None, "image/png", None), "image")

    def test_pdf_by_mime_and_by_extension(self):
        self.assertEqual(me.classify("document", "application/pdf", "a.pdf"), "pdf")
        self.assertEqual(me.classify("document", None, "scan.PDF"), "pdf")

    def test_audio_by_mime_and_by_media_type(self):
        self.assertEqual(me.classify("voice", "audio/ogg", "v.ogg"), "audio")
        self.assertEqual(me.classify("video", "video/mp4", "v.mp4"), "audio")

    def test_unsupported_types_return_none(self):
        self.assertIsNone(me.classify("document", "text/plain", "notes.txt"))


class ExtractTextDispatchTest(unittest.TestCase):
    def test_image_routes_to_gemini_vision(self):
        with mock.patch.object(me, "extract_image_text_gemini") as vision:
            vision.return_value = {"ok": True, "text": "t", "method": "gemini_vision", "reason": None}
            out = me.extract_text(b"x", media_type="photo", mime_type="image/jpeg")
        vision.assert_called_once()
        self.assertEqual(out["method"], "gemini_vision")

    def test_pdf_routes_to_pypdf(self):
        with mock.patch.object(me, "extract_pdf_text") as pdf:
            pdf.return_value = {"ok": True, "text": "t", "method": "pypdf", "reason": None}
            out = me.extract_text(b"x", media_type="document", mime_type="application/pdf")
        pdf.assert_called_once()
        self.assertEqual(out["method"], "pypdf")

    def test_voice_routes_to_the_transcription_chooser(self):
        """L'audio passe par le **choix** (Groq ou local), plus par Groq en direct."""
        with mock.patch.object(me, "transcribe_media") as choose:
            choose.return_value = {"ok": True, "text": "t", "method": "groq_whisper", "reason": None}
            out = me.extract_text(b"x", media_type="voice", mime_type="audio/ogg", file_name="v.ogg")
        choose.assert_called_once()
        self.assertEqual(out["method"], "groq_whisper")

    def test_unsupported_returns_failure_without_calling_backends(self):
        out = me.extract_text(b"x", media_type="document", mime_type="text/plain")
        self.assertFalse(out["ok"])
        self.assertEqual(out["method"], "unsupported")
        self.assertIn("non pris en charge", out["reason"])


def _fake_httpx(body):
    module = types.ModuleType("httpx")

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return body

    class _Client:
        last = None

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def post(self, url, headers=None, json=None):
            _Client.last = {"url": url, "headers": headers, "json": json}
            return _Response()

    module.Client = _Client
    return module, _Client


class GeminiVisionTest(unittest.TestCase):
    def test_missing_key_reports_reason(self):
        with mock.patch.object(me, "GEMINI_API_KEY", ""):
            out = me.extract_image_text_gemini(b"x", mime_type="image/png")
        self.assertFalse(out["ok"])
        self.assertIn("GEMINI_API_KEY", out["reason"])

    def test_success_sends_inline_data_and_returns_text(self):
        fake, client_cls = _fake_httpx(
            {"candidates": [{"content": {"parts": [{"text": "Graphique haussier"}]}}]}
        )
        with mock.patch.object(me, "GEMINI_API_KEY", "k"), mock.patch.dict(
            sys.modules, {"httpx": fake}
        ):
            out = me.extract_image_text_gemini(b"abc", mime_type="image/png")

        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "Graphique haussier")
        sent = client_cls.last
        self.assertEqual(sent["headers"]["X-goog-api-key"], "k")
        inline = sent["json"]["contents"][0]["parts"][1]["inline_data"]
        self.assertEqual(inline["mime_type"], "image/png")

    def test_empty_response_reports_reason(self):
        fake, _ = _fake_httpx({"candidates": []})
        with mock.patch.object(me, "GEMINI_API_KEY", "k"), mock.patch.dict(
            sys.modules, {"httpx": fake}
        ):
            out = me.extract_image_text_gemini(b"abc")
        self.assertFalse(out["ok"])


class PdfExtractorTest(unittest.TestCase):
    def _fake_pypdf(self, pages):
        module = types.ModuleType("pypdf")

        class _Page:
            def __init__(self, text):
                self._text = text

            def extract_text(self):
                return self._text

        class _Reader:
            def __init__(self, _stream):
                self.pages = [_Page(p) for p in pages]

        module.PdfReader = _Reader
        return module

    def test_missing_pypdf_reports_reason(self):
        with mock.patch.dict(sys.modules, {"pypdf": None}):
            out = me.extract_pdf_text(b"x")
        self.assertFalse(out["ok"])
        self.assertIn("pypdf", out["reason"])

    def test_success_joins_pages(self):
        with mock.patch.dict(sys.modules, {"pypdf": self._fake_pypdf(["Page 1", "Page 2"])}):
            out = me.extract_pdf_text(b"%PDF-1.4")
        self.assertTrue(out["ok"])
        self.assertIn("Page 1", out["text"])
        self.assertIn("Page 2", out["text"])

    def test_scanned_pdf_without_text_reports_reason(self):
        with mock.patch.dict(sys.modules, {"pypdf": self._fake_pypdf(["", "   "])}):
            out = me.extract_pdf_text(b"%PDF-1.4")
        self.assertFalse(out["ok"])
        self.assertIn("scanné", out["reason"])


class WhisperExtractorTest(unittest.TestCase):
    def test_missing_key_reports_reason(self):
        with mock.patch.object(me, "GROQ_API_KEY", ""):
            out = me.transcribe_media_groq(b"x", file_name="v.ogg", mime_type="audio/ogg")
        self.assertFalse(out["ok"])
        self.assertIn("GROQ_API_KEY", out["reason"])

    def test_missing_groq_package_reports_reason(self):
        with mock.patch.object(me, "GROQ_API_KEY", "k"), mock.patch.dict(
            sys.modules, {"groq": None}
        ):
            out = me.transcribe_media_groq(b"x", file_name="v.ogg", mime_type="audio/ogg")
        self.assertFalse(out["ok"])
        self.assertIn("groq", out["reason"])

    def test_success_uses_whisper_large_v3(self):
        captured = {}
        fake = types.ModuleType("groq")

        class _Response:
            text = "bonjour le monde"

        class _Transcriptions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return _Response()

        class _Audio:
            def __init__(self):
                self.transcriptions = _Transcriptions()

        class _Client:
            def __init__(self, api_key=None):
                captured["api_key"] = api_key
                self.audio = _Audio()

        fake.Groq = _Client
        with mock.patch.object(me, "GROQ_API_KEY", "k"), mock.patch.dict(
            sys.modules, {"groq": fake}
        ):
            out = me.transcribe_media_groq(b"abc", file_name="note.ogg", mime_type="audio/ogg")

        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "bonjour le monde")
        self.assertEqual(captured["model"], me.GROQ_WHISPER_MODEL)
        self.assertEqual(captured["file"][0], "note.ogg")
        self.assertEqual(captured["file"][1], b"abc")

    def test_filename_gets_an_extension_when_missing(self):
        captured = {}
        fake = types.ModuleType("groq")

        class _Response:
            text = "ok"

        class _Transcriptions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return _Response()

        class _Audio:
            def __init__(self):
                self.transcriptions = _Transcriptions()

        class _Client:
            def __init__(self, api_key=None):
                self.audio = _Audio()

        fake.Groq = _Client
        with mock.patch.object(me, "GROQ_API_KEY", "k"), mock.patch.dict(
            sys.modules, {"groq": fake}
        ):
            me.transcribe_media_groq(b"abc", file_name="voice_1", mime_type="audio/ogg")
        self.assertTrue(captured["file"][0].endswith(".ogg"))


def _fake_faster_whisper(captured, *, text="bonjour le monde", error=None):
    """Faux paquet `faster_whisper` : un modèle qui rend ses segments d'avance."""
    module = types.ModuleType("faster_whisper")

    class _Segment:
        def __init__(self, value):
            self.text = value

    class _Model:
        def __init__(self, name, device=None, compute_type=None):
            if error is not None:
                raise error
            captured["construction"] = captured.get("construction", 0) + 1
            captured["model"] = (name, device, compute_type)

        def transcribe(self, audio):
            captured.setdefault("audio", []).append(audio)
            return [_Segment(part) for part in text.split(" | ")], SimpleNamespace(language="fr")

    module.WhisperModel = _Model
    return module


class LocalWhisperTest(unittest.TestCase):
    """Le repli local : `faster-whisper`, sans réseau ni compte."""

    def setUp(self) -> None:
        # Le cache des modèles est **de module** (des centaines de Mo, chargés une
        # fois) : le vider évite qu'un test hérite du modèle d'un autre.
        me._local_models.clear()
        self.addCleanup(me._local_models.clear)
        self.captured: dict = {}

    def test_missing_package_reports_reason(self):
        with mock.patch.dict(sys.modules, {"faster_whisper": None}):
            out = me.transcribe_media_local(b"x", file_name="v.ogg", mime_type="audio/ogg")
        self.assertFalse(out["ok"])
        self.assertIn("faster-whisper", out["reason"])
        self.assertIn("pip install", out["reason"])

    def test_success_joins_the_segments(self):
        fake = _fake_faster_whisper(self.captured, text="bonjour | le monde")
        with mock.patch.dict(sys.modules, {"faster_whisper": fake}):
            out = me.transcribe_media_local(b"abc", file_name="note.ogg", mime_type="audio/ogg")

        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "faster_whisper")
        self.assertEqual(out["text"], "bonjour le monde")

    def test_the_audio_is_handed_over_as_a_stream(self):
        """L'extension ne sert qu'à Groq : PyAV lit le **conteneur** du flux."""
        fake = _fake_faster_whisper(self.captured)
        with mock.patch.dict(sys.modules, {"faster_whisper": fake}):
            me.transcribe_media_local(b"abc", file_name="sans_extension", mime_type=None)
        audio = self.captured["audio"][0]
        self.assertIsInstance(audio, io.BytesIO)
        self.assertEqual(audio.getvalue(), b"abc")

    def test_the_local_model_is_loaded_once_and_reused(self):
        """Recharger les poids à chaque média rendrait le repli inutilisable."""
        fake = _fake_faster_whisper(self.captured)
        with mock.patch.dict(sys.modules, {"faster_whisper": fake}):
            me.transcribe_media_local(b"a", file_name="a.ogg")
            me.transcribe_media_local(b"b", file_name="b.ogg")
        self.assertEqual(self.captured["construction"], 1)
        self.assertEqual(len(self.captured["audio"]), 2)

    def test_another_model_name_is_a_different_model(self):
        fake = _fake_faster_whisper(self.captured)
        with mock.patch.dict(sys.modules, {"faster_whisper": fake}):
            me.transcribe_media_local(b"a", file_name="a.ogg", model_name="tiny")
            me.transcribe_media_local(b"b", file_name="b.ogg", model_name="base")
        self.assertEqual(self.captured["construction"], 2)

    def test_the_configured_model_and_its_cpu_settings_are_used(self):
        fake = _fake_faster_whisper(self.captured)
        with mock.patch.dict(sys.modules, {"faster_whisper": fake}):
            me.transcribe_media_local(b"a", file_name="a.ogg")
        name, device, compute_type = self.captured["model"]
        self.assertEqual(name, me.LOCAL_WHISPER_MODEL)
        self.assertEqual(device, me.LOCAL_WHISPER_DEVICE)
        self.assertEqual(compute_type, me.LOCAL_WHISPER_COMPUTE_TYPE)

    def test_an_unavailable_model_names_the_weights_problem(self):
        """Hors ligne **et** poids jamais téléchargés : c'est le cas à expliquer."""
        fake = _fake_faster_whisper(self.captured, error=OSError("no such model"))
        with mock.patch.dict(sys.modules, {"faster_whisper": fake}):
            out = me.transcribe_media_local(b"a", file_name="a.ogg")
        self.assertFalse(out["ok"])
        self.assertIn(me.LOCAL_WHISPER_MODEL, out["reason"])
        self.assertIn("téléchargés", out["reason"])
        # Un modèle qui ne se charge pas n'est pas mis en cache : la tentative
        # suivante doit pouvoir réussir (dossier monté après coup, par exemple).
        self.assertEqual(me._local_models, {})

    def test_an_empty_transcription_is_reported(self):
        fake = _fake_faster_whisper(self.captured, text="")
        with mock.patch.dict(sys.modules, {"faster_whisper": fake}):
            out = me.transcribe_media_local(b"a", file_name="a.ogg")
        self.assertFalse(out["ok"])
        self.assertIn("vide", out["reason"])

    def test_a_decoding_failure_is_reported_not_raised(self):
        """Le décodage se fait **pendant l'itération** : c'est là qu'il échoue."""
        module = types.ModuleType("faster_whisper")

        class _Model:
            def __init__(self, name, device=None, compute_type=None):
                pass

            def transcribe(self, audio):
                def _boom():
                    raise RuntimeError("conteneur illisible")
                    yield  # pragma: no cover - générateur jamais consommé

                return _boom(), SimpleNamespace(language=None)

        module.WhisperModel = _Model
        with mock.patch.dict(sys.modules, {"faster_whisper": module}):
            out = me.transcribe_media_local(b"a", file_name="a.ogg")
        self.assertFalse(out["ok"])
        self.assertIn("illisible", out["reason"])


class LocalTranscriptionAvailabilityTest(unittest.TestCase):
    """`local_transcription_available()` : installer, pas charger."""

    def test_an_absent_package_is_unavailable(self):
        with mock.patch.object(me.importlib.util, "find_spec", return_value=None):
            self.assertFalse(me.local_transcription_available())

    def test_a_present_package_is_available(self):
        with mock.patch.object(me.importlib.util, "find_spec", return_value=object()):
            self.assertTrue(me.local_transcription_available())

    def test_a_broken_lookup_is_a_no_not_an_exception(self):
        with mock.patch.object(
            me.importlib.util, "find_spec", side_effect=ValueError("chemin exotique")
        ):
            self.assertFalse(me.local_transcription_available())


def _groq_module(captured, *, text="depuis groq"):
    """Faux paquet `groq`, sur le même modèle que les tests ci-dessus."""
    module = types.ModuleType("groq")

    class _Response:
        pass

    class _Transcriptions:
        def create(self, **kwargs):
            captured["groq"] = kwargs
            response = _Response()
            response.text = text
            return response

    class _Audio:
        def __init__(self):
            self.transcriptions = _Transcriptions()

    class _Client:
        def __init__(self, api_key=None):
            self.audio = _Audio()

    module.Groq = _Client
    return module


class TranscriptionChoiceTest(unittest.TestCase):
    """`transcribe_media` : Groq d'abord, le repli local quand il ne peut pas."""

    FILE = dict(file_name="note.ogg", mime_type="audio/ogg")

    def test_a_configured_key_uses_groq_and_never_the_local_model(self):
        fake = _groq_module({})
        with mock.patch.object(me, "GROQ_API_KEY", "k"), mock.patch.dict(
            sys.modules, {"groq": fake}
        ), mock.patch.object(me, "transcribe_media_local") as local:
            out = me.transcribe_media(b"abc", **self.FILE)
        self.assertTrue(out["ok"])
        self.assertEqual(out["method"], "groq_whisper")
        local.assert_not_called()

    def test_no_key_skips_groq_entirely(self):
        """Sans clé, la voie nominale est inutilisable : on ne l'essaie pas."""
        with mock.patch.object(me, "GROQ_API_KEY", ""), mock.patch.object(
            me, "transcribe_media_groq"
        ) as groq, mock.patch.object(me, "transcribe_media_local") as local:
            local.return_value = {
                "ok": True, "text": "en local", "method": "faster_whisper", "reason": None
            }
            out = me.transcribe_media(b"abc", **self.FILE)
        groq.assert_not_called()
        local.assert_called_once()
        self.assertEqual(out["method"], "faster_whisper")

    def test_a_groq_failure_falls_back_to_the_local_model(self):
        """Le cas de la machine **hors ligne qui a une clé** : c'est le but."""
        with mock.patch.object(me, "GROQ_API_KEY", "k"), mock.patch.object(
            me, "transcribe_media_groq"
        ) as groq, mock.patch.object(me, "transcribe_media_local") as local:
            groq.return_value = {
                "ok": False, "text": "", "method": "groq_whisper", "reason": "réseau coupé"
            }
            local.return_value = {
                "ok": True, "text": "en local", "method": "faster_whisper", "reason": None
            }
            out = me.transcribe_media(b"abc", **self.FILE)
        groq.assert_called_once()
        local.assert_called_once()
        self.assertTrue(out["ok"])
        self.assertEqual(out["text"], "en local")

    def test_when_both_fail_the_reason_names_both_attempts(self):
        with mock.patch.object(me, "GROQ_API_KEY", "k"), mock.patch.object(
            me, "transcribe_media_groq"
        ) as groq, mock.patch.object(me, "transcribe_media_local") as local:
            groq.return_value = {
                "ok": False, "text": "", "method": "groq_whisper", "reason": "quota"
            }
            local.return_value = {
                "ok": False, "text": "", "method": "faster_whisper", "reason": "pas de modèle"
            }
            out = me.transcribe_media(b"abc", **self.FILE)
        self.assertFalse(out["ok"])
        self.assertIn("quota", out["reason"])
        self.assertIn("pas de modèle", out["reason"])

    def test_a_real_local_attempt_succeeds_without_any_key(self):
        """Sans doublure du choix : c'est bien le paquet local qui est appelé."""
        me._local_models.clear()
        self.addCleanup(me._local_models.clear)
        captured: dict = {}
        fake = _fake_faster_whisper(captured, text="transcription hors ligne")
        with mock.patch.object(me, "GROQ_API_KEY", ""), mock.patch.dict(
            sys.modules, {"groq": None, "faster_whisper": fake}
        ):
            out = me.transcribe_media(b"abc", **self.FILE)
        self.assertTrue(out["ok"], out["reason"])
        self.assertEqual(out["text"], "transcription hors ligne")
        self.assertEqual(out["method"], "faster_whisper")


class ExtractionReadinessTest(unittest.TestCase):
    """« Si je relance, ai-je une chance ? » — la question de `/transcribe`.

    Ce n'est **pas** une promesse de texte (un PDF peut être scanné, un modèle
    local peut avoir perdu ses poids) : c'est un verdict sur les ingrédients, et
    c'est pour ça qu'il ne refuse le travail que sur des manques **prouvables**.
    """

    def _ready(self, **kwargs):
        return me.extraction_readiness(**kwargs)

    def test_an_image_needs_the_gemini_key(self):
        with mock.patch.object(me, "GEMINI_API_KEY", ""):
            state = self._ready(media_type="photo", mime_type="image/jpeg")
        self.assertEqual(state["kind"], "image")
        self.assertFalse(state["ready"])
        self.assertEqual(state["missing"], ["GEMINI_API_KEY"])
        self.assertIn("GEMINI_API_KEY", state["hint"])

    def test_an_image_with_a_key_is_ready(self):
        with mock.patch.object(me, "GEMINI_API_KEY", "cle"), mock.patch.object(
            me, "_package_available", return_value=True
        ):
            state = self._ready(media_type="photo", mime_type="image/jpeg")
        self.assertTrue(state["ready"])
        self.assertEqual(state["missing"], [])
        self.assertEqual(state["hint"], "")

    def test_a_pdf_needs_only_a_local_package(self):
        """Ni clef ni réseau : c'est le seul média qu'on lit sans rien configurer."""
        with mock.patch.object(me, "_package_available", return_value=False):
            state = self._ready(
                media_type="document", mime_type="application/pdf", file_name="a.pdf"
            )
        self.assertEqual(state["kind"], "pdf")
        self.assertEqual(state["missing"], ["pypdf"])
        with mock.patch.object(me, "_package_available", return_value=True):
            self.assertTrue(self._ready(media_type="document", mime_type="application/pdf")["ready"])

    def test_audio_needs_one_of_the_two_voices(self):
        with mock.patch.object(me, "GROQ_API_KEY", ""), mock.patch.object(
            me, "local_transcription_available", return_value=False
        ):
            state = self._ready(media_type="voice", mime_type="audio/ogg")
        self.assertEqual(state["kind"], "audio")
        self.assertFalse(state["ready"])
        self.assertEqual(state["voices"], {"groq": False, "local": False})
        self.assertEqual(state["missing"], ["GROQ_API_KEY", "faster-whisper"])

    def test_audio_without_a_key_is_ready_but_says_it_will_be_slow(self):
        """Le repli local reste une réponse positive — `hint` dit à quel prix."""
        with mock.patch.object(me, "GROQ_API_KEY", ""), mock.patch.object(
            me, "local_transcription_available", return_value=True
        ):
            state = self._ready(media_type="video", mime_type="video/mp4")
        self.assertTrue(state["ready"])
        self.assertEqual(state["missing"], [])
        self.assertIn("plus lent", state["hint"])
        self.assertIn(me.LOCAL_WHISPER_MODEL, state["hint"])

    def test_audio_with_a_key_has_nothing_to_report(self):
        with mock.patch.object(me, "GROQ_API_KEY", "cle"):
            state = self._ready(media_type="voice", mime_type="audio/ogg")
        self.assertTrue(state["ready"])
        self.assertEqual(state["hint"], "")

    def test_the_voices_are_published_even_when_ready(self):
        """Le rapport dit dans quelles conditions l'extraction a tourné."""
        with mock.patch.object(me, "GROQ_API_KEY", "cle"):
            state = self._ready(media_type="voice", mime_type="audio/ogg")
        self.assertEqual(state["voices"], {"groq": True, "local": False})

    def test_an_unsupported_type_is_never_ready_and_never_pretends_to_know_what_is_missing(self):
        state = self._ready(
            media_type="document", mime_type="application/zip", file_name="a.zip"
        )
        self.assertIsNone(state["kind"])
        self.assertFalse(state["ready"])
        self.assertEqual(state["missing"], [])
        self.assertIn("non pris en charge", state["hint"])

    def test_the_classification_is_the_extractor_one(self):
        """Deux classifications divergeraient en silence : on vérifie l'identité.

        Un média classé `image` ici et `audio` là-bas refuserait (ou lancerait)
        l'extraction sur des critères qui ne sont pas ceux du travail réel.
        """
        for kwargs in (
            {"media_type": "photo", "mime_type": "image/jpeg"},
            {"media_type": "document", "mime_type": "application/pdf"},
            {"media_type": "voice", "mime_type": "audio/ogg"},
            {"media_type": "document", "mime_type": "application/zip"},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(
                    me.extraction_readiness(**kwargs)["kind"],
                    me.classify(
                        kwargs.get("media_type"),
                        kwargs.get("mime_type"),
                        kwargs.get("file_name"),
                    ),
                )


class TranscriptionHealthTest(unittest.TestCase):
    """`/preflight` dit si la transcription reste possible — les **deux** voies.

    Sans ça, la seule façon de savoir pourquoi un vocal n'est plus transcrit
    serait de lire le code : les deux valeurs fausses signifient « plus aucune
    transcription », ce que rien d'autre n'annonce.
    """

    def setUp(self) -> None:
        self._saved = os.environ.get("GROQ_API_KEY")
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved is None:
            os.environ.pop("GROQ_API_KEY", None)
        else:
            os.environ["GROQ_API_KEY"] = self._saved
        config_runtime.reset_env_config()

    def health(self, key: str, local: bool) -> dict:
        os.environ["GROQ_API_KEY"] = key
        config_runtime.reset_env_config()
        with mock.patch.object(me, "local_transcription_available", return_value=local):
            return config_runtime.get_env_config().component_health()["transcription"]

    def test_both_voices_are_published(self):
        self.assertEqual(self.health("k", True), {"groq": True, "local_fallback": True})
        self.assertEqual(self.health("", False), {"groq": False, "local_fallback": False})

    def test_without_a_key_the_local_fallback_carries_the_capability(self):
        """Sans clé Groq, c'est le repli local **seul** qui transcrit encore."""
        health = self.health("", True)
        self.assertFalse(health["groq"])
        self.assertTrue(health["local_fallback"])


if __name__ == "__main__":
    unittest.main()
