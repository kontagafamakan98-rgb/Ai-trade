"""Tests du client d'embeddings Gemini (`ai/embeddings.py`).

`httpx` est remplacé par une doublure (`sys.modules`) : on vérifie l'URL du
modèle, l'en-tête `x-goog-api-key`, le `taskType` (document vs requête), la
dimension demandée (768, celle de la colonne pgvector), la normalisation L2, et
les refus (pas de clé, dimension inattendue, réponse illisible, panne réseau).
"""
from __future__ import annotations

import math
import sys
import types
import unittest
from unittest import mock

from ai import embeddings

DIM = embeddings.EMBEDDING_DIM


def _fake_httpx(body):
    module = types.ModuleType("httpx")

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return body

    class _Client:
        posts: list = []

        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def post(self, url, headers=None, json=None):
            _Client.posts.append({"url": url, "headers": headers, "json": json})
            return _Response()

    _Client.posts = []
    module.Client = _Client
    return module, _Client


def _embedding_body(scale=1.0, width=DIM):
    vector = [0.0] * width
    vector[0] = scale
    return {"embedding": {"values": vector}}


class EmbedTextsTest(unittest.TestCase):
    def test_no_key_returns_none(self):
        with mock.patch.object(embeddings, "GEMINI_API_KEY", ""):
            self.assertIsNone(embeddings.embed_texts(["bonjour"]))

    def test_empty_input_returns_empty_list(self):
        with mock.patch.object(embeddings, "GEMINI_API_KEY", "k"):
            self.assertEqual(embeddings.embed_texts([]), [])

    def test_unknown_kind_raises_even_without_key(self):
        """Une faute de frappe doit se voir tout de suite, pas passer inaperçue."""
        with mock.patch.object(embeddings, "GEMINI_API_KEY", ""):
            with self.assertRaises(ValueError):
                embeddings.embed_texts(["x"], kind="passages")

    def test_query_uses_retrieval_query_task_and_requests_768_dims(self):
        fake, client = _fake_httpx(_embedding_body(scale=3.0))
        with mock.patch.object(embeddings, "GEMINI_API_KEY", "k"), mock.patch.dict(
            sys.modules, {"httpx": fake}
        ):
            vectors = embeddings.embed_texts(["bonjour"], kind="query")

        self.assertEqual(len(vectors), 1)
        self.assertEqual(len(vectors[0]), DIM)
        # Vecteur tronqué/non unitaire -> normalisé pour la similarité cosinus.
        self.assertAlmostEqual(math.sqrt(sum(x * x for x in vectors[0])), 1.0, places=6)

        sent = client.posts[0]
        self.assertEqual(sent["url"], f"{embeddings.API_BASE_URL}/{embeddings.MODEL}:embedContent")
        self.assertEqual(sent["headers"]["x-goog-api-key"], "k")
        self.assertEqual(sent["json"]["taskType"], embeddings.TASK_QUERY)
        self.assertEqual(sent["json"]["outputDimensionality"], DIM)
        self.assertEqual(sent["json"]["model"], f"models/{embeddings.MODEL}")
        self.assertEqual(sent["json"]["content"]["parts"], [{"text": "bonjour"}])

    def test_passage_uses_retrieval_document_task(self):
        fake, client = _fake_httpx(_embedding_body())
        with mock.patch.object(embeddings, "GEMINI_API_KEY", "k"), mock.patch.dict(
            sys.modules, {"httpx": fake}
        ):
            embeddings.embed_texts(["document"], kind="passage")
        self.assertEqual(client.posts[0]["json"]["taskType"], embeddings.TASK_DOCUMENT)

    def test_one_request_per_text_in_order(self):
        fake, client = _fake_httpx(_embedding_body())
        with mock.patch.object(embeddings, "GEMINI_API_KEY", "k"), mock.patch.dict(
            sys.modules, {"httpx": fake}
        ):
            vectors = embeddings.embed_texts(["un", "deux", "trois"])

        self.assertEqual(len(vectors), 3)
        self.assertEqual(len(client.posts), 3)
        self.assertEqual(
            [post["json"]["content"]["parts"][0]["text"] for post in client.posts],
            ["un", "deux", "trois"],
        )

    def test_batch_shape_is_also_accepted(self):
        fake, _ = _fake_httpx({"embeddings": [{"values": [1.0] + [0.0] * (DIM - 1)}]})
        with mock.patch.object(embeddings, "GEMINI_API_KEY", "k"), mock.patch.dict(
            sys.modules, {"httpx": fake}
        ):
            vectors = embeddings.embed_texts(["x"])
        self.assertEqual(len(vectors[0]), DIM)

    def test_wrong_dimension_is_refused(self):
        fake, _ = _fake_httpx(_embedding_body(width=3))
        with mock.patch.object(embeddings, "GEMINI_API_KEY", "k"), mock.patch.dict(
            sys.modules, {"httpx": fake}
        ):
            self.assertIsNone(embeddings.embed_texts(["x"]))

    def test_unreadable_response_is_refused(self):
        fake, _ = _fake_httpx({"candidates": []})
        with mock.patch.object(embeddings, "GEMINI_API_KEY", "k"), mock.patch.dict(
            sys.modules, {"httpx": fake}
        ):
            self.assertIsNone(embeddings.embed_texts(["x"]))

    def test_http_failure_returns_none(self):
        module = types.ModuleType("httpx")

        class _Client:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def post(self, *_args, **_kwargs):
                raise RuntimeError("network down")

        module.Client = _Client
        with mock.patch.object(embeddings, "GEMINI_API_KEY", "k"), mock.patch.dict(
            sys.modules, {"httpx": module}
        ):
            self.assertIsNone(embeddings.embed_texts(["x"]))


class EmbedTextTest(unittest.TestCase):
    def test_single_text_returns_one_vector(self):
        fake, _ = _fake_httpx(_embedding_body())
        with mock.patch.object(embeddings, "GEMINI_API_KEY", "k"), mock.patch.dict(
            sys.modules, {"httpx": fake}
        ):
            vector = embeddings.embed_text("BTC")
        self.assertEqual(len(vector), DIM)

    def test_single_text_is_none_without_key(self):
        with mock.patch.object(embeddings, "GEMINI_API_KEY", ""):
            self.assertIsNone(embeddings.embed_text("BTC"))


class TaskTypeTest(unittest.TestCase):
    def test_known_kinds(self):
        self.assertEqual(embeddings.task_type("query"), embeddings.TASK_QUERY)
        self.assertEqual(embeddings.task_type("passage"), embeddings.TASK_DOCUMENT)

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            embeddings.task_type("autre")


if __name__ == "__main__":
    unittest.main()
