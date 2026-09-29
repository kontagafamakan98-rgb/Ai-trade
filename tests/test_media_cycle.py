"""Le cycle complet d'un média, enchaîné de bout en bout sur un Supabase en mémoire.

Les trois maillons ont chacun leurs tests — `upload_media`,
`replace_media_chunks`, `search_chunks` — et aucun ne dit que l'**ensemble**
tient : qu'un objet déposé est décrit par la ligne dont l'identifiant sert
ensuite à indexer, que le texte indexé ressort d'une recherche, et que la
recherche retrouve bien **ce** média-là. C'est ce que ce fichier vérifie, en
enchaînant les trois comme l'ingestion les enchaîne.

La doublure est celle de **tout le dossier de tests**
(`tests/supabase_double.py`) — et elle imite **fidèlement** ce que le cycle
traverse, ce qui est tout l'intérêt :

* les écritures **persistent** d'un appel à l'autre (une table partagée, pas une
  réponse figée) : une indexation suivie d'une recherche lit ce qui a été écrit ;
* l'insertion attribue l'identifiant, comme Postgres — le cycle en dépend, la
  ligne média créée à l'étape 1 étant celle que l'étape 2 indexe ;
* `match_knowledge_chunks` est **rejoué** avec sa sémantique (migration `009`) :
  `embedding is not null`, jokers d'actif et de régime, classement par
  préférence puis par distance, `greatest(match_count, 1)`. Un RPC qui rend
  toujours tout passerait au vert sur un cycle cassé.

Les vecteurs sont **déterministes** (sac de mots haché, normalisé) : la
similarité cosinus est donc réelle, et c'est le sens du texte qui décide du
classement — pas l'ordre d'insertion. Le modèle réel distingue le côté
« question » du côté « passage » ; la doublure rend le même vecteur pour le même
texte, ce qui est précisément ce qui rend le cycle observable sans réseau ni clé
Gemini. `hashlib` et non `hash()` : ce dernier est salé par processus, donc le
classement ne serait pas reproductible.
"""
from __future__ import annotations

import hashlib
import math
import re
import unittest
import uuid
from unittest import mock

from ai import embeddings, media_indexing
from database import knowledge_index, media_store
from tests import supabase_double

# --------------------------------------------------------------------------- #
# Vecteurs : déterministes, normalisés, sans réseau
# --------------------------------------------------------------------------- #


def vector_of(text: str) -> list:
    """Vecteur d'un texte : chaque mot hache vers une dimension, puis normalisé."""
    vector = [0.0] * embeddings.EMBEDDING_DIM
    for token in re.findall(r"[a-z0-9]+", str(text or "").lower()):
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        vector[int(digest[:8], 16) % embeddings.EMBEDDING_DIM] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:  # texte vide : un vecteur nul n'a pas de similarité cosinus
        vector[0] = 1.0
        return vector
    return [value / norm for value in vector]


def embed_texts(texts, *, kind="passage", **_kwargs):
    """Doublure de `ai.embeddings.embed_texts` : jamais de réseau, jamais `None`.

    `embed_text` (le vecteur d'une question) passe par ici : patcher cette seule
    fonction couvre les deux côtés de la recherche.
    """
    return [vector_of(text) for text in texts]


def no_embeddings(texts, **_kwargs):
    """Doublure de l'absence de Gemini : `embed_texts` rend `None`, pas une erreur."""
    return None


# --------------------------------------------------------------------------- #
# Supabase en mémoire : la doublure partagée de `tests/supabase_double.py`
# --------------------------------------------------------------------------- #


def cosine(left, right) -> float:
    """Similarité cosinus — celle que `1 - (embedding <=> query_embedding)` calcule."""
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


def match_knowledge_chunks(
    chunks,
    query_embedding,
    match_count=5,
    filter_source=None,
    filter_asset=None,
    filter_regime=None,
):
    """Réplique de `match_knowledge_chunks` (`009_knowledge_vectors.sql`).

    Recopiée clause par clause, y compris ce qui surprend : `asset` et `regime`
    nuls sont des **jokers**, donc un morceau non étiqueté reste candidat ; et à
    similarité égale, la correspondance explicite de l'actif puis du régime passe
    devant.
    """
    candidates = [
        chunk
        for chunk in chunks
        if chunk.get("embedding") is not None
        and (filter_source is None or chunk.get("source") == filter_source)
        and (
            filter_asset is None
            or chunk.get("asset") is None
            or chunk.get("asset") == filter_asset
        )
        and (
            filter_regime is None
            or chunk.get("regime") is None
            or chunk.get("regime") == filter_regime
        )
    ]
    rows = [
        {
            "id": chunk.get("id"),
            "media_id": chunk.get("media_id"),
            "chunk_index": chunk.get("chunk_index"),
            "content": chunk.get("content"),
            "source": chunk.get("source"),
            "asset": chunk.get("asset"),
            "regime": chunk.get("regime"),
            "similarity": cosine(chunk["embedding"], query_embedding),
        }
        for chunk in candidates
    ]
    rows.sort(
        key=lambda row: (
            -int(filter_asset is not None and row["asset"] == filter_asset),
            -int(filter_regime is not None and row["regime"] == filter_regime),
            -row["similarity"],
        )
    )
    return rows[: max(int(match_count), 1)]


# --------------------------------------------------------------------------- #
# Le cycle
# --------------------------------------------------------------------------- #


CAPTION = "cassure du bitcoin, objectif 72k"
EXTRACTED = "graphique en chandeliers : cassure confirmée du bitcoin au-dessus de 70k"
#: Des octets qui ne ressemblent pas à du texte : le cycle ne doit rien en déduire.
PAYLOAD = bytes(range(256)) * 4


class MediaCycleTest(unittest.TestCase):
    """Déposer, indexer, retrouver — l'enchaînement réel, sur une base en mémoire."""

    def setUp(self) -> None:
        # La doublure partagée, avec le RPC de recherche **rejoué** sur les
        # morceaux réellement écrits : un RPC qui rendrait une réponse figée
        # passerait au vert sur un cycle cassé.
        self.client = supabase_double.SupabaseDouble(
            handlers={
                knowledge_index.MATCH_FUNCTION: lambda params: match_knowledge_chunks(
                    self.client.store(knowledge_index.TABLE).rows, **params
                )
            }
        )
        # Postgres attribue un `uuid` à `knowledge_media.id` et un compteur aux
        # morceaux : la doublure le dit, puisque c'est ce que le cycle traverse.
        self.client.store(media_store.TABLE).id_factory = lambda: uuid.uuid4().hex
        supabase_double.use_supabase(self, self.client, media_store, knowledge_index)
        embedder = mock.patch.object(embeddings, "embed_texts", embed_texts)
        embedder.start()
        self.addCleanup(embedder.stop)

    # -- helpers ---------------------------------------------------------- #

    def upload(self, *, message_id=42, file_name="graphique.jpg", caption=CAPTION, upsert=False):
        """Étape 1 : les octets dans le bucket, la description dans la table."""
        return media_store.upload_media(
            PAYLOAD,
            media_type="photo",
            file_name=file_name,
            chat_id="@signaux",
            message_id=message_id,
            caption=caption,
            telegram_file_id=f"fid-{message_id}",
            upsert=upsert,
        )

    def ingest(self, *, caption=CAPTION, extracted=EXTRACTED, asset=None, message_id=42):
        """Le cycle tel que l'ingestion le parcourt : déposer, puis indexer."""
        row = self.upload(message_id=message_id, caption=caption)
        count = media_store.replace_media_chunks(
            row["id"],
            media_indexing.indexable_text(caption, extracted),
            asset=asset,
        )
        return row, count

    # -- 1. dépôt --------------------------------------------------------- #

    def test_the_object_is_deposited_under_a_deterministic_path_and_described(self):
        row = self.upload()
        path = "telegram/@signaux/42-graphique.jpg"

        self.assertEqual(list(self.client.storage.files), [path])
        self.assertEqual(self.client.storage.buckets, [media_store.BUCKET])
        self.assertEqual(media_store.download_media(path), PAYLOAD)
        self.assertEqual(row["storage_path"], path)
        self.assertEqual(row["file_size"], len(PAYLOAD))
        self.assertEqual(row["caption"], CAPTION)
        # La ligne créée est retrouvable par l'identifiant que l'insertion a rendu :
        # c'est lui que l'indexation reçoit juste après.
        self.assertEqual(media_store.get_media(row["id"])["storage_path"], path)

    # -- 2. indexation puis recherche ------------------------------------- #

    def test_the_text_indexed_for_a_media_is_found_by_a_query(self):
        row, count = self.ingest()
        self.assertEqual(count, 1)

        hits = media_store.search_chunks("cassure du bitcoin", top_k=5)
        self.assertTrue(hits, "le texte indexé doit ressortir d'une recherche")
        best = hits[0]
        self.assertEqual(best["media_id"], row["id"])
        self.assertIn("bitcoin", best["content"])
        self.assertGreater(best["similarity"], 0.0)

        # La question est bien passée par un vecteur (et non par un raccourci).
        _kind, name, params = self.client.rpc_calls()[-1]
        self.assertEqual(name, knowledge_index.MATCH_FUNCTION)
        self.assertEqual(len(params["query_embedding"]), embeddings.EMBEDDING_DIM)
        self.assertIsNone(params["filter_source"])

        # …et le texte relu est exactement celui qui a été indexé.
        chunks = media_store.list_media_chunks(row["id"])
        self.assertEqual([chunk["content"] for chunk in chunks], [best["content"]])

    def test_a_long_text_is_indexed_in_ordered_chunks_and_stays_findable(self):
        long_text = " ".join(f"mot{i}" for i in range(900))
        row, count = self.ingest(extracted=long_text + " cassure du bitcoin")
        self.assertGreater(count, 1, "un texte long doit produire plusieurs morceaux")

        chunks = media_store.list_media_chunks(row["id"])
        self.assertEqual(
            [chunk["chunk_index"] for chunk in chunks], list(range(len(chunks)))
        )
        hits = media_store.search_chunks("cassure du bitcoin", top_k=5)
        self.assertIn(row["id"], {hit["media_id"] for hit in hits})

    def test_the_ranking_follows_meaning_not_insertion_order(self):
        """Le média sur le pétrole est indexé **en premier**, et arrive second."""
        oil, _ = self.ingest(caption="pétrole brut", extracted="graphique du pétrole", message_id=1)
        btc, _ = self.ingest(message_id=2)

        hits = media_store.search_chunks("cassure du bitcoin", top_k=5)
        self.assertEqual(hits[0]["media_id"], btc["id"])
        self.assertEqual(hits[-1]["media_id"], oil["id"])
        self.assertGreater(hits[0]["similarity"], hits[-1]["similarity"])

    def test_the_asset_filter_keeps_only_this_asset_and_the_untagged(self):
        """`asset` est **préférentiel** : il exclut, mais jamais les non étiquetés."""
        btc, _ = self.ingest(message_id=1, asset="BTC-USD")
        oil, _ = self.ingest(
            caption="pétrole brut", extracted="graphique du pétrole", message_id=2, asset="BRENT"
        )
        untagged, _ = self.ingest(caption="graphique divers", extracted="graphique", message_id=3)

        hits = media_store.search_chunks("graphique", asset="BTC-USD", top_k=5)
        found = {hit["media_id"] for hit in hits}
        self.assertIn(btc["id"], found)
        self.assertIn(untagged["id"], found, "un morceau sans actif reste candidat")
        self.assertNotIn(oil["id"], found, "un autre actif est écarté")
        self.assertEqual(hits[0]["media_id"], btc["id"], "l'actif demandé passe devant")

    def test_the_source_filter_separates_media_chunks_from_notes(self):
        knowledge_index.replace_chunks(
            None,
            "règle de money management : jamais plus de 1 % par position",
            note_source="obsidian:regles",
            source=knowledge_index.SOURCE_NOTE,
        )
        row, _ = self.ingest()

        media_hits = media_store.search_chunks("cassure du bitcoin", source=knowledge_index.SOURCE_MEDIA)
        note_hits = media_store.search_chunks("money management", source=knowledge_index.SOURCE_NOTE)

        self.assertEqual({hit["media_id"] for hit in media_hits}, {row["id"]})
        self.assertTrue(note_hits)
        self.assertTrue(all(hit["media_id"] is None for hit in note_hits))

    # -- 3. rejeu --------------------------------------------------------- #

    def test_replaying_a_message_duplicates_neither_object_nor_row_nor_chunks(self):
        """Le cycle rejoué doit être **idempotent**, jusqu'aux morceaux."""
        row, _ = self.ingest()
        again = self.upload(upsert=True)
        self.assertEqual(again["id"], row["id"], "l'`upsert` conserve l'identité du média")
        count = media_store.replace_media_chunks(
            again["id"], media_indexing.indexable_text(CAPTION, EXTRACTED)
        )

        self.assertEqual(
            list(self.client.storage.files), ["telegram/@signaux/42-graphique.jpg"]
        )
        self.assertEqual(len(self.client.store(media_store.TABLE).rows), 1)
        self.assertEqual(len(self.client.store(knowledge_index.TABLE).rows), count)
        hits = media_store.search_chunks("cassure du bitcoin", top_k=10)
        self.assertEqual(len(hits), count, "aucun morceau en double après le rejeu")

    # -- 4. les cas où le cycle se dégrade -------------------------------- #

    def test_without_embeddings_the_text_is_written_but_the_search_returns_nothing(self):
        """Sans Gemini, le texte part quand même — il sera cherchable plus tard."""
        with mock.patch.object(embeddings, "embed_texts", no_embeddings):
            row, count = self.ingest()
            self.assertEqual(count, 1)
            self.assertEqual(media_store.search_chunks("cassure du bitcoin"), [])

        chunks = media_store.list_media_chunks(row["id"])
        self.assertEqual(len(chunks), 1)
        self.assertIsNone(chunks[0].get("embedding"))
        # La recherche s'arrête faute de vecteur de question : le RPC n'est pas appelé.
        self.assertEqual(self.client.rpc_calls(), [])

    def test_a_failed_indexing_leaves_the_media_in_place(self):
        """Une indexation impossible ne remet jamais en cause l'ingestion."""
        row = self.upload()
        self.client.store(knowledge_index.TABLE).insert_error = RuntimeError("index indisponible")

        with self.assertRaises(RuntimeError):
            media_store.replace_media_chunks(row["id"], "cassure du bitcoin")

        self.assertEqual(media_store.get_media(row["id"])["id"], row["id"])
        self.assertEqual(media_store.download_media(row["storage_path"]), PAYLOAD)
        self.assertEqual(media_store.list_media_chunks(row["id"]), [])
        self.assertEqual(media_store.search_chunks("cassure du bitcoin"), [])


if __name__ == "__main__":
    unittest.main()
