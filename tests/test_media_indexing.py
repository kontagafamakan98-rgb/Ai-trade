"""Tests du pipeline d'indexation partagé (`ai/media_indexing.py`).

Le module a deux consommateurs — la route du bot et celle du scraper public —, et
ce fichier vérifie **ce qu'ils partagent** : l'ordre du texte indexé (légende
d'abord), l'étiquette d'actif détectée, et le fait que les morceaux atterrissent
comme des **médias** (`source = telegram`), donc cherchables par `/search
--source medias` **et** injectables dans le contexte du moteur de décision.

Les cas ligne à ligne de l'extraction sont couverts par
`tests/test_telegram_media.py` (qui exerce les mêmes fonctions depuis la route du
bot) : ici, on ne teste que le contrat commun.
"""
from __future__ import annotations

import contextlib
import io
import unittest

from ai import media_indexing


def _descriptor(**overrides):
    base = {
        "media_type": "photo",
        "mime_type": "image/jpeg",
        "file_name": "photo_100.jpg",
        "caption": "BTC cassure des 100k confirmée",
    }
    base.update(overrides)
    return base


def _extractor(text, method="gemini_vision", ok=True):
    """Extracteur factice : la vision décrit le graphique — ou dit pourquoi elle n'a pas pu.

    `ok=False` imite le **vrai** extracteur privé de sa clef : il ne rend pas du
    texte vide en prétendant avoir réussi, il rend un échec motivé.
    """

    def extract(_data, **_kwargs):
        return {
            "ok": ok,
            "method": method,
            "text": text,
            "reason": None if ok else "vision indisponible",
        }

    return extract


class SharedPipelineTest(unittest.IsolatedAsyncioTestCase):
    """Ce que les deux routes doivent produire à l'identique."""

    async def _index(self, descriptor=None, extracted="graphique BTC, cassure nette"):
        calls = []

        def store(media_id, text, **kwargs):
            calls.append({"media_id": media_id, "text": text, "kwargs": kwargs})
            return 3

        def record(media_id, summary):
            calls.append({"recorded": media_id, "summary": summary})
            return {}

        summary = await media_indexing.extract_and_index(
            b"image",
            descriptor or _descriptor(),
            media_id="m1",
            extract=_extractor(extracted),
            store=store,
            record=record,
        )
        return summary, calls

    async def test_the_caption_precedes_the_extracted_text(self):
        """Une légende « BTC support 64k » nomme l'actif mieux que la vision."""
        _, calls = await self._index()
        text = calls[0]["text"]
        self.assertTrue(text.startswith("BTC cassure des 100k confirmée"))
        self.assertIn("graphique BTC", text)

    async def test_the_asset_is_taken_from_the_caption(self):
        summary, calls = await self._index()
        self.assertEqual(summary["asset"], "BTC-USD")
        self.assertEqual(summary["asset_source"], "caption")
        self.assertEqual(calls[0]["kwargs"]["asset"], "BTC-USD")

    async def test_a_lowercase_caption_still_labels_the_chunks(self):
        """La casse de la vie réelle : « btc support 64k », pas « BTC support 64k ».

        Sans ça, la légende — le signal le plus net — ne produisait rien, `asset`
        restait NULL, et le filtre préférentiel de `match_knowledge_chunks` n'avait
        aucun actif à classer : tous les médias restaient des jokers.
        """
        summary, calls = await self._index(_descriptor(caption="btc support 64k"))
        self.assertEqual(summary["asset"], "BTC-USD")
        self.assertEqual(summary["asset_source"], "caption")
        self.assertEqual(calls[0]["kwargs"]["asset"], "BTC-USD")

    async def test_a_word_that_looks_like_a_ticker_still_labels_nothing(self):
        """Le garde-fou tient : « le sol du graphique » n'est pas Solana."""
        summary, calls = await self._index(
            _descriptor(caption="le sol du graphique, un lien vers link"), extracted=""
        )
        self.assertIsNone(summary["asset"])
        self.assertIsNone(calls[0]["kwargs"]["asset"], "NULL reste le joker du filtre")

    async def test_the_chunks_are_indexed_as_media_not_as_a_note(self):
        """`source` n'est pas imposé : `replace_chunks` écrit la source média.

        C'est ce que filtrent `/search --source medias` et
        `get_media_context` — une source inversée rendrait le texte invisible là
        où on l'attend.
        """
        _, calls = await self._index()
        self.assertNotIn("source", calls[0]["kwargs"])
        self.assertEqual(calls[0]["media_id"], "m1")

    async def test_a_caption_alone_is_still_indexed(self):
        """Vision indisponible : la légende reste cherchable, et rien ne lève."""
        summary, calls = await self._index(extracted="")
        self.assertIn("cassure des 100k", calls[0]["text"])
        self.assertEqual(summary["chunks"], 3)
        self.assertTrue(summary["has_caption"])

    async def test_an_empty_media_is_reported_without_writing(self):
        summary, calls = await self._index(_descriptor(caption=""), extracted="")
        self.assertEqual(summary["chunks"], 0)
        self.assertEqual(
            [call for call in calls if "text" in call], [], "rien à indexer"
        )

    async def test_a_media_without_identifier_is_not_indexed(self):
        summary = await media_indexing.extract_and_index(
            b"image", _descriptor(), media_id=None, store=lambda *a, **k: 1
        )
        self.assertFalse(summary["ok"])
        self.assertIn("media_id", summary["reason"])


class ExtractionOutcomeTest(unittest.IsolatedAsyncioTestCase):
    """Chaque passage d'extraction **note son résultat** sur la ligne média.

    C'est la seule trace qui distingue plus tard « lu » de « la clef manquait » :
    un média dont l'extraction a échoué a des morceaux indexés dès qu'il porte une
    légende, donc rien dans l'index ne le dénonce. `/transcribe` vit de cette note.
    """

    def _recorder(self, calls, error=None):
        def record(media_id, summary):
            if error is not None:
                raise error
            calls.append({"media_id": media_id, "summary": summary})
            return {}

        return record

    async def _run(
        self, *, extracted="texte lu", caption="BTC", ok=True, error=None, record=None
    ):
        calls = []
        await media_indexing.extract_and_index(
            b"image",
            _descriptor(caption=caption),
            media_id="m1",
            extract=_extractor(extracted, ok=ok),
            store=lambda *a, **k: 2,
            record=record if record is not None else self._recorder(calls, error),
        )
        return calls

    async def test_a_successful_extraction_is_noted_as_such(self):
        calls = await self._run()
        self.assertEqual(calls[0]["media_id"], "m1")
        self.assertTrue(calls[0]["summary"]["ok"])
        self.assertEqual(calls[0]["summary"]["chunks"], 2)
        self.assertEqual(calls[0]["summary"]["method"], "gemini_vision")

    async def test_the_chunks_are_counted_before_the_note_is_written(self):
        """Noter avant l'indexation écrirait `chunks: 0` sur une extraction réussie."""
        calls = await self._run()
        self.assertEqual(calls[0]["summary"]["chunks"], 2)

    async def test_a_failed_extraction_is_noted_even_when_the_caption_is_indexed(self):
        """Le cas qui compte : la légende est indexée, le contenu n'a jamais été lu."""
        calls = await self._run(extracted="", caption="BTC support 64k", ok=False)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["summary"]["ok"])
        self.assertEqual(calls[0]["summary"]["reason"], "vision indisponible")

    async def test_a_media_that_yields_nothing_is_still_noted(self):
        """Rien à indexer n'est pas « rien à dire » : c'est justement le cas à rattraper."""
        calls = await self._run(extracted="", caption="", ok=False)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["summary"]["ok"])
        self.assertEqual(calls[0]["summary"]["chunks"], 0)

    async def test_a_broken_recorder_does_not_fail_the_ingestion(self):
        """Le média est stocké, son texte indexé : perdre l'annotation ne l'annule pas."""
        with contextlib.redirect_stdout(io.StringIO()) as out:
            calls = await self._run(error=RuntimeError("db down"))
        self.assertEqual(calls, [])
        self.assertIn("db down", out.getvalue())


class ExcerptTest(unittest.TestCase):
    """L'aperçu est partagé, donc défini une seule fois."""

    def test_short_text_is_returned_compact(self):
        self.assertEqual(media_indexing.excerpt("  texte \n utile "), "texte utile")

    def test_long_text_is_truncated_at_the_shared_limit(self):
        excerpt = media_indexing.excerpt("x" * 1000)
        self.assertEqual(len(excerpt), media_indexing.EXCERPT_CHARS + 1)
        self.assertTrue(excerpt.endswith("…"))

    def test_empty_text_yields_an_empty_excerpt(self):
        self.assertEqual(media_indexing.excerpt(""), "")
        self.assertEqual(media_indexing.excerpt(None), "")


if __name__ == "__main__":
    unittest.main()
