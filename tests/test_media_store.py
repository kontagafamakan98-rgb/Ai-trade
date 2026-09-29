"""Tests du client médias (`database/media_store.py`).

Le vrai Supabase n'est évidemment pas joignable ici : on injecte la doublure
**partagée** du dossier de tests (`tests/supabase_double.py`), qui imite le
chaînage de la bibliothèque (`supabase.table(...).select().eq().order().range()
.execute()` et `supabase.storage.from_(...).upload()`) et, contrairement aux
doublures locales qu'elle remplace, **applique les filtres** et **persiste** ce
qui est écrit. On vérifie ensuite le COMPORTEMENT du client :

* la clé d'objet dérivée est **stable** (rejouer un message ne crée pas de
  doublon) ;
* un échec d'insertion en base ne laisse pas d'**objet orphelin** dans le
  bucket — sinon le fichier ne serait référencé nulle part ;
* lister est borné et filtrable ;
* sans configuration, les fonctions **échouent franchement** au lieu de faire
  semblant d'avoir écrit (`_require_client`).

Ces tests portent sur la logique du client, pas sur l'API Supabase elle-même.
"""
from __future__ import annotations

import hashlib
import importlib
import pathlib
import re
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from database import knowledge_index, media_store
from tests import supabase_double


class MediaStoreTest(unittest.TestCase):
    """Les opérations du client, sur la doublure partagée du dossier de tests.

    `table` est la table `knowledge_media` de la doublure — ses lignes, son
    journal (`inserted`, `upserted`, `deleted`) — et `bucket` son Storage : ce
    sont les mêmes objets que ceux que `supabase.table(...)` rend au module.
    """

    def setUp(self) -> None:
        self.client = supabase_double.SupabaseDouble()
        self.table = self.client.store(media_store.TABLE)
        self.bucket = self.client.storage
        supabase_double.use_supabase(self, self.client, media_store)

    def test_upload_sends_bytes_then_describes_the_file(self):
        row = media_store.upload_media(
            b"\x01\x02\x03",
            media_type="photo",
            file_name="photo.jpg",
            chat_id="@signals",
            message_id=42,
            caption="hello",
        )
        self.assertEqual(self.bucket.bucket_name, media_store.BUCKET)
        path, data, options = self.bucket.uploads[0]
        self.assertEqual(path, "telegram/@signals/42-photo.jpg")
        self.assertEqual(data, b"\x01\x02\x03")
        self.assertEqual(options["content-type"], "image/jpeg")
        self.assertEqual(options["upsert"], "false")

        payload = self.table.inserted[0]
        self.assertEqual(payload["storage_path"], path)
        self.assertEqual(payload["file_size"], 3)
        self.assertEqual(payload["media_type"], "photo")
        self.assertEqual(payload["caption"], "hello")
        self.assertEqual(row["id"], "row-1")

    def test_the_object_path_is_stable_for_the_same_message(self):
        first = media_store.media_object_path(
            chat_id="@signals", message_id=7, file_name="a.png"
        )
        second = media_store.media_object_path(
            chat_id="@signals", message_id=7, file_name="a.png"
        )
        self.assertEqual(first, second)
        self.assertEqual(first, "telegram/@signals/7-a.png")

    def test_an_explicit_storage_path_is_respected(self):
        media_store.upload_media(b"x", storage_path="custom/here.bin")
        self.assertEqual(self.bucket.uploads[0][0], "custom/here.bin")

    def test_upsert_replaces_the_row_of_the_same_path(self):
        media_store.upload_media(b"x", storage_path="p.bin", upsert=True)
        payload, on_conflict = self.table.upserted[0]
        self.assertEqual(on_conflict, "storage_path")
        self.assertEqual(payload["storage_path"], "p.bin")
        self.assertEqual(self.bucket.uploads[0][2]["upsert"], "true")

    def test_a_failed_insert_removes_the_uploaded_object(self):
        """Sinon le bucket garde un fichier que plus aucune ligne ne référence."""
        self.table.insert_error = RuntimeError("db down")
        with self.assertRaises(RuntimeError):
            media_store.upload_media(b"x", storage_path="orphan.bin")
        self.assertEqual(self.bucket.removed, [["orphan.bin"]])

    def test_non_bytes_payload_is_rejected(self):
        with self.assertRaises(TypeError):
            media_store.upload_media("not bytes")  # type: ignore[arg-type]

    def test_unknown_media_type_is_rejected(self):
        with self.assertRaises(ValueError):
            media_store.upload_media(b"x", media_type="hologram")

    def test_list_media_is_bounded_and_filterable(self):
        # Le filtre est **appliqué**, comme en base : la ligne d'un autre canal
        # ne ressort pas de la lecture.
        self.table.rows = [
            {"id": "a", "source": "telegram", "media_type": "photo", "created_at": "03"},
            {"id": "b", "source": "telegram", "media_type": "photo", "created_at": "02"},
            {"id": "c", "source": "scraper", "media_type": "photo", "created_at": "01"},
        ]
        rows = media_store.list_media(source="telegram", media_type="photo", limit=10_000)
        # Du plus récent au plus ancien, et la ligne de l'autre provenance ne sort pas.
        self.assertEqual([row["id"] for row in rows], ["a", "b"])
        self.assertIn(("eq", "source", "telegram"), self.table.calls)
        self.assertIn(("eq", "media_type", "photo"), self.table.calls)
        # `limit` est plafonné : une table entière ne doit pas être rapatriée.
        self.assertIn(("range", 0, 999), self.table.calls)

    def test_paging_by_offset_never_repeats_or_hides_a_row_of_the_same_date(self):
        """`created_at` seul n'est pas un ordre **total**, donc pas un ordre de page.

        Deux médias d'un même album partagent leur date à la seconde : sans second
        critère, la base est libre de rendre « b » avant « a » d'une page à
        l'autre, et deux pages successives exposeraient la même ligne en en
        cachant une autre. L'`id` est unique, donc l'ordre l'est aussi.
        """
        self.table.rows = [
            {"id": "a", "created_at": "2026-01-01T00:00:00+00:00"},
            {"id": "b", "created_at": "2026-01-01T00:00:00+00:00"},
            {"id": "c", "created_at": "2026-01-01T00:00:00+00:00"},
        ]
        first = media_store.list_media(limit=2, offset=0)
        second = media_store.list_media(limit=2, offset=2)

        self.assertEqual(
            [row["id"] for row in first] + [row["id"] for row in second],
            ["c", "b", "a"],
        )
        self.assertIn(("order", "id", True), self.table.calls)

    def test_find_media_by_message_matches_the_channel_and_the_message(self):
        """Clé partagée par les deux routes d'un canal (bot et aperçu public)."""
        self.table.rows = [{"id": "x", "chat_id": "crypto_signals", "message_id": 42}]
        row = media_store.find_media_by_message("crypto_signals", 42)
        self.assertEqual(row["id"], "x")
        self.assertIn(("eq", "chat_id", "crypto_signals"), self.table.calls)
        self.assertIn(("eq", "message_id", 42), self.table.calls)
        self.assertIn(("limit", 1), self.table.calls)

    def test_find_media_by_message_returns_none_when_absent(self):
        self.assertIsNone(media_store.find_media_by_message("canal", 1))

    def test_find_media_by_message_needs_both_identifiers(self):
        """Sans canal ou sans message, il n'y a rien à comparer : pas de requête."""
        for chat_id, message_id in (("", 42), (None, 42), ("canal", None), ("canal", "")):
            with self.subTest(chat_id=chat_id, message_id=message_id):
                self.assertIsNone(media_store.find_media_by_message(chat_id, message_id))
        self.assertEqual(self.table.calls, [])

    def test_get_media_returns_none_when_absent(self):
        self.assertIsNone(media_store.get_media("missing"))
        self.table.rows = [{"id": "x", "storage_path": "p.bin"}]
        self.assertEqual(media_store.get_media("x")["id"], "x")

    def test_delete_media_removes_the_row_and_the_object(self):
        self.table.rows = [{"id": "x", "storage_path": "p.bin"}]
        self.assertTrue(media_store.delete_media("x"))
        self.assertTrue(self.table.deleted)
        self.assertEqual(self.bucket.removed, [["p.bin"]])

    def test_delete_media_is_idempotent(self):
        self.assertFalse(media_store.delete_media("absent"))
        self.assertFalse(self.table.deleted)

    def test_delete_media_can_keep_the_object(self):
        self.table.rows = [{"id": "x", "storage_path": "p.bin"}]
        media_store.delete_media("x", remove_object=False)
        self.assertEqual(self.bucket.removed, [])

    def test_download_returns_the_raw_bytes(self):
        self.bucket.files["p.bin"] = b"contenu"
        self.assertEqual(media_store.download_media("p.bin"), b"contenu")

    def test_list_storage_normalizes_objects(self):
        self.bucket.listing = [
            {"name": "a.bin"},
            SimpleNamespace(name="b.bin", id="2"),
        ]
        self.assertEqual(
            media_store.list_storage(),
            [{"name": "a.bin"}, {"name": "b.bin", "id": "2"}],
        )

    def test_create_signed_url_returns_the_url(self):
        url = media_store.create_signed_url("p.bin", expires_in=120)
        self.assertEqual(url, "https://example.test/p.bin?exp=120")

    def test_guess_mime_type_falls_back(self):
        self.assertEqual(media_store.guess_mime_type("a.png"), "image/png")
        self.assertEqual(media_store.guess_mime_type("a.unknownext"), "application/octet-stream")


class ChunksDelegationTest(unittest.TestCase):
    """`media_store` **délègue** les chunks : il n'en réimplémente pas la logique.

    Le point important est le passage des arguments : une délégation qui perd un
    filtre (`asset`, `source`, `top_k`) ou un drapeau (`embed`) donnerait des
    résultats silencieusement faux, alors que la logique sous-jacente est juste.
    """

    def test_replace_media_chunks_forwards_every_argument(self):
        with mock.patch.object(knowledge_index, "replace_chunks", return_value=3) as replace:
            result = media_store.replace_media_chunks(
                "m1", "texte", asset="BTC-USD", embed=False
            )
        self.assertEqual(result, 3)
        replace.assert_called_once_with("m1", "texte", asset="BTC-USD", embed=False)

    def test_list_media_chunks_forwards_the_media_id(self):
        chunks = [{"chunk_index": 0, "content": "a"}]
        with mock.patch.object(knowledge_index, "list_chunks", return_value=chunks) as listing:
            self.assertEqual(media_store.list_media_chunks("m1"), chunks)
        listing.assert_called_once_with("m1", columns=None)

    def test_list_media_chunks_forwards_the_projection(self):
        """Une projection perdue ferait traverser les vecteurs — sans rien casser.

        `select("*")` ramène l'`embedding` de chaque morceau (768 flottants) :
        le résultat serait juste, plus gros de quelques mégaoctets, et personne
        ne s'en apercevrait avant de regarder le réseau.
        """
        with mock.patch.object(knowledge_index, "list_chunks", return_value=[]) as listing:
            media_store.list_media_chunks("m1", columns=("content",))
        listing.assert_called_once_with("m1", columns=("content",))

    def test_search_chunks_forwards_the_filters(self):
        hits = [{"content": "zone d'achat", "similarity": 0.81}]
        with mock.patch.object(knowledge_index, "search_chunks", return_value=hits) as search:
            result = media_store.search_chunks("support", source="telegram", top_k=3)
        self.assertEqual(result, hits)
        search.assert_called_once_with("support", source="telegram", top_k=3)

    def test_search_chunks_returns_empty_without_embeddings(self):
        with mock.patch.object(knowledge_index, "search_chunks", return_value=[]):
            self.assertEqual(media_store.search_chunks("x"), [])


class MediaUpkeepRouteTest(unittest.TestCase):
    """Les appelants passent **par** les délégations — l'autre moitié du contrat.

    `ChunksDelegationTest` vérifie que `media_store` délègue bien ; il ne dit rien
    de ceux qui l'appellent. Or c'est là que le contrat se perd : un défaut de
    paramètre recopié (`store: ChunkStore = knowledge_index.replace_chunks`)
    contourne la façade sans que rien ne le signale — le code fonctionne, mais la
    durée de vie d'un média n'a plus qu'un seul module pour la porter, ce qui est
    précisément la raison d'être des délégations.

    Trois contrôles, complémentaires : le **texte** trouve un site nouveau sans
    qu'on ait à l'inscrire nulle part, l'**identité** de l'objet prouve que le
    défaut réellement embarqué par la fonction est la délégation — pas seulement
    qu'un texte y ressemble —, et l'**appariement** des deux interdit qu'un site
    ne soit vérifié que par le texte, c'est-à-dire par aucune identité
    (`test_the_identity_contract_covers_every_write_site`).
    """

    REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
    #: Les paquets où vit l'entretien d'un média (dépôt, extraction, indexation,
    #: revue, étiquetage) : c'est là qu'un défaut recopié peut apparaître.
    PACKAGES = ("ai", "core", "database", "notifications", "scrapers")
    #: Le paramètre par lequel un média écrit ses morceaux de texte. Ancrée sur le
    #: nom `store:` seul, elle ne confond pas `tag_store:` avec lui. Ce qui suit la
    #: valeur peut être une virgule **ou** la parenthèse fermante : un `store` écrit
    #: en dernier paramètre doit être lu comme les autres, sinon il échappe au
    #: contrôle textuel — et, par ricochet, à l'appariement qui le compare au
    #: contrat par identité, c'est-à-dire au seul contrôle qui regarde l'objet.
    CHUNK_STORE = re.compile(
        r"^\s*store:\s*[\w\[\]]+\s*=\s*([^,)\n]+?)\s*[,)]", re.MULTILINE
    )
    DELEGATION = "media_store.replace_media_chunks"
    #: Une tête de fonction **de module** : une fonction imbriquée est indentée,
    #: donc la dernière tête en colonne zéro avant un défaut `store:` est bien la
    #: fonction qui le porte — c'est ce qui permet de la **nommer**.
    FUNCTION_HEAD = re.compile(r"^(?:async )?def (\w+)\(", re.MULTILINE)
    #: Les routes qui écrivent les morceaux d'un média — les **six** sites de la
    #: source : les cinq de `telegram_media` (ingestion d'un message, ingestion
    #: d'un album, verdict de revue, reprise depuis le fichier stocké, relance
    #: d'une transcription) et l'extraction partagée par les deux routes
    #: d'ingestion, bot et scraper public. Cette liste est écrite à la main : c'est
    #: pourquoi `test_the_identity_contract_covers_every_write_site` exige qu'elle
    #: corresponde **exactement** aux défauts trouvés dans la source — une route
    #: oubliée ici ne serait vérifiée que par le texte.
    ROUTES = {
        "notifications.telegram_media": (
            "ingest_media",
            "ingest_album",
            "reprocess_media",
            "review_media",
            "retranscribe_media",
        ),
        "ai.media_indexing": ("extract_and_index",),
    }

    def chunk_stores(self):
        """Chaque défaut `store:` déclaré dans les paquets de l'entretien média."""
        found = []
        for package in self.PACKAGES:
            for path in sorted((self.REPO_ROOT / package).rglob("*.py")):
                for match in self.CHUNK_STORE.finditer(path.read_text(encoding="utf-8")):
                    found.append((path.relative_to(self.REPO_ROOT).as_posix(), match.group(1).strip()))
        return found

    def chunk_store_routes(self):
        """Chaque défaut `store:` **et la fonction de module qui le porte**.

        Rend un ensemble de couples (`module`, `nom de fonction`). C'est ce qui
        interdit le trou : une route ajoutée demain à la source serait bien lue
        par le texte (sa valeur serait fausse), mais resterait absente du contrôle
        par identité, qui ne regarde que ce que `ROUTES` nomme.
        """
        found = set()
        for path, _value in self.chunk_stores():
            text = (self.REPO_ROOT / path).read_text(encoding="utf-8")
            module = path[: -len(".py")].replace("/", ".")
            for match in self.CHUNK_STORE.finditer(text):
                heads = list(self.FUNCTION_HEAD.finditer(text, 0, match.start()))
                #: Un défaut hors fonction (classe, module) n'a pas de porteur
                #: nommable, donc pas de vérification par identité possible : on le
                #: dit au lieu de l'ignorer.
                self.assertTrue(heads, f"défaut `store:` hors fonction dans {path}")
                found.add((module, heads[-1].group(1)))
        return found

    def test_every_chunk_store_default_goes_through_the_delegation(self):
        found = self.chunk_stores()
        # Garde-fou : un motif qui ne trouve plus rien ferait passer ce test à vide,
        # et c'est exactement ainsi qu'une façade se perd (le nom, l'annotation ou
        # l'indentation changent, le contrôle ne regarde plus rien). La source en
        # porte six aujourd'hui ; le plancher est plus bas pour qu'un site retiré
        # se discute ailleurs (`ROUTES`), pas ici.
        self.assertGreaterEqual(
            len(found), 5, f"trop peu de défauts `store:` lus : {found}"
        )
        offenders = [f"{path} : {value}" for path, value in found if value != self.DELEGATION]
        self.assertEqual(
            offenders,
            [],
            "les morceaux d'un média s'écrivent par `media_store.replace_media_chunks`, "
            "la délégation du module qui porte la vie du média",
        )

    def test_the_routes_really_carry_the_delegation(self):
        """L'objet embarqué, et pas seulement son nom écrit quelque part."""
        for module_name, names in self.ROUTES.items():
            module = importlib.import_module(module_name)
            for name in names:
                function = getattr(module, name)
                with self.subTest(route=f"{module_name}.{name}"):
                    defaults = function.__kwdefaults__ or {}
                    self.assertIn("store", defaults, "la route n'a plus de paramètre `store`")
                    self.assertIs(
                        defaults["store"],
                        media_store.replace_media_chunks,
                        "la route contourne `media_store` et écrit dans `knowledge_index`",
                    )

    def test_the_identity_contract_covers_every_write_site(self):
        """Aucun site d'écriture ne doit être vérifié par le texte **seulement**.

        Le contrôle par identité passe par une liste écrite à la main, le contrôle
        textuel trouve tout seul : si les deux divergent, le site non nommé écrit
        ses morceaux par défaut sans que personne n'ait vérifié l'objet réellement
        embarqué — l'angle exact par lequel un défaut recopié survit. L'égalité est
        donc exigée dans les **deux** sens : un site absent de `ROUTES` comme une
        route nommée qui n'existe plus (une ancre périmée, qui laisserait le
        contrôle d'identité lever sur un nom disparu).
        """
        declared = {
            (module, name) for module, names in self.ROUTES.items() for name in names
        }
        self.assertEqual(self.chunk_store_routes(), declared)

    def test_the_delegation_is_not_a_stub(self):
        """Contrôle de la route elle-même : la façade mène bien à l'index."""
        with mock.patch.object(knowledge_index, "replace_chunks", return_value=4) as replace:
            self.assertEqual(media_store.replace_media_chunks("m1", "texte"), 4)
        replace.assert_called_once_with("m1", "texte")


class ReconciliationTest(unittest.TestCase):
    """Objets du bucket sans ligne `knowledge_media` (orphelins).

    `iter_storage_paths` doit parcourir l'arborescence **lui-même** : `list(path)`
    ne rend que les enfants directs du dossier, comme l'API Storage.
    """

    def setUp(self) -> None:
        self.client = supabase_double.SupabaseDouble(
            storage=supabase_double.Storage(
                listing={
                    "": [
                        {"name": "telegram", "id": None},
                        {"name": "loose.bin", "id": "9"},
                    ],
                    "telegram": [{"name": "@signals", "id": None}],
                    "telegram/@signals": [
                        {"name": "1-a.jpg", "id": "1"},
                        {"name": "2-b.jpg", "id": "2"},
                        {"name": "3-c.jpg", "id": "3"},
                    ],
                }
            )
        )
        self.table = self.client.store(media_store.TABLE)
        self.table.rows = [
            {"storage_path": "telegram/@signals/1-a.jpg"},
            {"storage_path": "telegram/@signals/2-b.jpg"},
        ]
        self.bucket = self.client.storage
        supabase_double.use_supabase(self, self.client, media_store)

    def test_iter_storage_paths_walks_nested_folders(self):
        paths = media_store.iter_storage_paths()
        self.assertEqual(
            paths,
            [
                "loose.bin",
                "telegram/@signals/1-a.jpg",
                "telegram/@signals/2-b.jpg",
                "telegram/@signals/3-c.jpg",
            ],
        )

    def test_iter_storage_paths_can_be_scoped_to_a_prefix(self):
        paths = media_store.iter_storage_paths("telegram/@signals")
        self.assertEqual(len(paths), 3)
        self.assertTrue(all(p.startswith("telegram/@signals/") for p in paths))

    def test_list_orphan_objects_returns_only_unreferenced(self):
        # 1-a et 2-b sont référencés ; loose.bin et 3-c ne le sont pas.
        self.assertEqual(
            media_store.list_orphan_objects(),
            ["loose.bin", "telegram/@signals/3-c.jpg"],
        )

    def test_list_orphan_objects_is_empty_when_everything_is_referenced(self):
        self.table.rows.append({"storage_path": "loose.bin"})
        self.table.rows.append({"storage_path": "telegram/@signals/3-c.jpg"})
        self.assertEqual(media_store.list_orphan_objects(), [])

    def test_delete_objects_batches_the_removal(self):
        removed = media_store.delete_objects(
            ["a", "b", "c"], batch_size=2
        )
        self.assertEqual(removed, 3)
        self.assertEqual(self.bucket.removed, [["a", "b"], ["c"]])

    def test_delete_objects_ignores_empty_paths(self):
        self.assertEqual(media_store.delete_objects(["a", "", None]), 1)
        self.assertEqual(self.bucket.removed, [["a"]])

    def test_all_media_paths_paginates(self):
        rows = [{"storage_path": f"p{i}"} for i in range(5)]
        client = supabase_double.SupabaseDouble(storage=self.bucket)
        table = client.store(media_store.TABLE)
        table.rows = list(rows)
        with mock.patch.object(media_store, "supabase", client):
            paths = media_store.all_media_paths(page_size=2)
        self.assertEqual(paths, [f"p{i}" for i in range(5)])
        # 2 + 2 + 1 : une page courte signale la fin du parcours.
        self.assertEqual(len(table.operations("range")), 3)


class MissingObjectsTest(unittest.TestCase):
    """Lignes décrites dont l'**objet a disparu** — le sens inverse.

    On rend les lignes entières (et non des chemins comme pour les orphelins) :
    réparer demande de savoir d'où re-télécharger, et cette information
    (`telegram_file_id`, `chat_id`, `message_id`) n'existe que dans la ligne.
    """

    def setUp(self) -> None:
        self.client = supabase_double.SupabaseDouble(
            storage=supabase_double.Storage(
                listing={
                    "": [{"name": "telegram", "id": None}],
                    "telegram": [{"name": "@signals", "id": None}],
                    "telegram/@signals": [{"name": "1-a.jpg", "id": "1"}],
                }
            )
        )
        self.table = self.client.store(media_store.TABLE)
        self.table.rows = [
            {
                "id": "m1",
                "storage_path": "telegram/@signals/1-a.jpg",
                "chat_id": "@signals",
                "message_id": 1,
                "telegram_file_id": "AgAC-1",
                "file_size": 10,
            },
            {
                "id": "m2",
                "storage_path": "telegram/@signals/2-b.jpg",
                "chat_id": "@signals",
                "message_id": 2,
            },
            {
                "id": "m3",
                "storage_path": "scraper/chan/3-c.jpg",
                "chat_id": "chan",
                "message_id": 3,
            },
        ]
        self.bucket = self.client.storage
        supabase_double.use_supabase(self, self.client, media_store)

    def test_the_missing_rows_are_the_ones_whose_bytes_are_gone(self):
        missing = media_store.list_missing_objects()
        # L'ordre est celui de la lecture, triée par `storage_path` (l'ordre total
        # que la réconciliation documente) : `scraper/…` passe avant `telegram/…`.
        self.assertEqual([row["id"] for row in missing], ["m3", "m2"])

    def test_the_whole_row_is_returned_not_just_the_path(self):
        """Sans `chat_id`/`message_id`, aucune réparation n'est possible."""
        row = media_store.list_missing_objects("telegram")[0]
        self.assertEqual(row["chat_id"], "@signals")
        self.assertEqual(row["message_id"], 2)

    def test_the_missing_row_carries_what_decides_the_repair(self):
        """`metadata` fait partie de la projection de réconciliation.

        C'est de là que viennent l'**empreinte** (sans elle, aucune restauration
        ne peut être prouvée) et ce que la liste annonce à l'opérateur (actif,
        verdict de revue). L'omettre ne casse rien : les trois champs deviennent
        silencieusement `null`, ce qu'aucun test ne remarquerait autrement.
        """
        self.table.rows[1] = {
            **self.table.rows[1],
            "metadata": {
                media_store.FINGERPRINT_KEY: "abc123",
                media_store.ASSET_KEY: {"value": "BTC-USD", "source": "caption"},
                media_store.REVIEW_KEY: {"status": "rejected"},
            },
        }
        row = media_store.list_missing_objects("telegram")[0]
        self.assertEqual(media_store.content_fingerprint(row), "abc123")
        self.assertEqual(media_store.asset_tag(row)[0], "BTC-USD")
        self.assertEqual(media_store.review_status(row), "rejected")
        self.assertEqual(row["storage_path"], "telegram/@signals/2-b.jpg")

    def test_a_fully_present_bucket_reports_nothing(self):
        self.table.rows = [row for row in self.table.rows if row["id"] == "m1"]
        self.assertEqual(media_store.list_missing_objects(), [])

    def test_a_scoped_read_does_not_inspect_other_folders(self):
        """Un dossier non parcouru ne doit pas faire croire à des objets perdus."""
        missing = media_store.list_missing_objects("telegram/@signals")
        self.assertEqual([row["id"] for row in missing], ["m2"])

    def test_an_empty_bucket_makes_every_row_missing(self):
        """Le pire cas n'est pas une erreur : c'est tout ce qu'il faut réparer."""
        self.bucket.listing = {}
        self.assertEqual(len(media_store.list_missing_objects()), 3)

    # -- la restauration ---------------------------------------------------- #

    def test_restore_object_deposes_the_bytes_without_touching_the_row(self):
        """Rejouer l'ingestion écraserait `metadata` : verdict, actif, extraction.

        Le dépôt se fait donc **dans Storage seulement** : la ligne garde ce que
        les tours précédents y ont enregistré, et les morceaux indexés restent en
        place — il n'y a rien à réextraire, seul l'objet avait disparu.
        """
        written = media_store.restore_object(
            "telegram/@signals/2-b.jpg", b"octets", mime_type="image/jpeg"
        )
        self.assertEqual(written, 6)
        path, data, options = self.bucket.uploads[0]
        self.assertEqual((path, data), ("telegram/@signals/2-b.jpg", b"octets"))
        self.assertEqual(options["content-type"], "image/jpeg")
        self.assertEqual(options["upsert"], "true")
        self.assertEqual(self.table.upserted, [], "la ligne ne doit pas être réécrite")
        self.assertEqual(self.table.inserted, [], "ni dupliquée")

    def test_restore_object_guesses_the_type_without_one(self):
        media_store.restore_object("telegram/@signals/2-b.pdf", b"%PDF")
        self.assertEqual(self.bucket.uploads[0][2]["content-type"], "application/pdf")

    def test_restore_object_refuses_an_empty_path(self):
        """Sans chemin, il n'y a pas d'objet à remettre — et pas de destination."""
        with self.assertRaises(ValueError):
            media_store.restore_object("", b"octets")

    def test_restore_object_refuses_something_that_is_not_bytes(self):
        with self.assertRaises(TypeError):
            media_store.restore_object("telegram/@signals/2-b.jpg", "texte")

    def test_object_exists_tells_a_file_from_a_folder(self):
        """Une entrée sans `id` est un sous-dossier, pas un fichier."""
        self.assertTrue(media_store.object_exists("telegram/@signals/1-a.jpg"))
        self.assertFalse(media_store.object_exists("telegram/@signals/2-b.jpg"))
        self.assertFalse(media_store.object_exists("telegram"))
        self.assertFalse(media_store.object_exists(""))


class FingerprintTest(unittest.TestCase):
    """L'empreinte des octets, écrite à l'ingestion et relue après réparation.

    Elle répond à une seule question : « les octets qui reviennent sont-ils ceux
    qui étaient partis ? ». La taille ne le prouve pas — l'aperçu public sert une
    copie réduite qui peut tomber juste — donc l'empreinte est la seule référence
    qui puisse attester une restauration (`core/media_repair`).
    """

    def setUp(self) -> None:
        self.client = supabase_double.SupabaseDouble()
        self.table = self.client.store(media_store.TABLE)
        supabase_double.use_supabase(self, self.client, media_store)

    def test_the_bytes_are_fingerprinted_at_ingestion(self):
        row = media_store.upload_media(
            b"octets", media_type="document", file_name="a.pdf"
        )
        expected = hashlib.sha256(b"octets").hexdigest()
        self.assertEqual(row["metadata"][media_store.FINGERPRINT_KEY], expected)
        self.assertEqual(
            self.table.inserted[0]["metadata"][media_store.FINGERPRINT_KEY], expected
        )
        self.assertEqual(media_store.content_fingerprint(row), expected)

    def test_the_fingerprint_is_computed_not_received(self):
        """Une empreinte annoncée par l'appelant attesterait d'octets déposés par
        quelqu'un d'autre — elle est donc recalculée, celle-ci écrasée."""
        row = media_store.upload_media(
            b"octets",
            metadata={
                media_store.FINGERPRINT_KEY: "0" * 64,
                media_store.ASSET_KEY: {"value": "BTC-USD"},
            },
        )
        self.assertEqual(
            row["metadata"][media_store.FINGERPRINT_KEY],
            media_store.fingerprint(b"octets"),
        )
        self.assertEqual(
            row["metadata"][media_store.ASSET_KEY],
            {"value": "BTC-USD"},
            "les autres annotations gardent leur place",
        )

    def test_two_files_of_the_same_size_do_not_share_a_digest(self):
        """Le cas qui motive l'empreinte : même taille, octets différents."""
        self.assertEqual(len(b"court"), len(b"courT"))
        self.assertNotEqual(
            media_store.fingerprint(b"court"), media_store.fingerprint(b"courT")
        )

    def test_the_key_name_is_pinned(self):
        """Le nom de la clé est un **contrat de données** : les empreintes déjà
        enregistrées vivent sous ce nom, et le renommer les orphelinerait toutes
        en silence (plus rien à comparer, sans qu'aucun test ne rougisse)."""
        self.assertEqual(media_store.FINGERPRINT_KEY, "content_sha256")

    def test_an_absent_fingerprint_is_never_read_as_a_match(self):
        """`None` veut dire « on ne peut pas prouver », jamais « ça correspond »."""
        shapes = (
            None,
            {},
            {"metadata": None},
            {"metadata": {}},
            {"metadata": {media_store.FINGERPRINT_KEY: ""}},
            {"metadata": {media_store.FINGERPRINT_KEY: "   "}},
            {"metadata": "texte libre"},
        )
        for row in shapes:
            with self.subTest(row=row):
                self.assertIsNone(media_store.content_fingerprint(row))


class ReviewStatusTest(unittest.TestCase):
    """Le verdict de revue vit dans `metadata`, sans migration ni colonne neuve."""

    def _client(self, rows):
        """La table `knowledge_media` de la doublure, semée de ces lignes."""
        client = supabase_double.SupabaseDouble()
        table = client.store(media_store.TABLE)
        table.rows = [dict(row) for row in rows]
        return table, client

    def _row(self, **metadata):
        return {"id": "m1", "metadata": dict(metadata)}

    def test_reading_is_robust_to_every_missing_shape(self):
        for row in (
            None,
            {},
            {"metadata": None},
            {"metadata": "pas un dict"},
            {"metadata": {"telegram": True}},
            {"metadata": {media_store.REVIEW_KEY: None}},
            {"metadata": {media_store.REVIEW_KEY: "validated"}},
            {"metadata": {media_store.REVIEW_KEY: {"status": "bof"}}},
        ):
            with self.subTest(row=row):
                self.assertIsNone(media_store.review_status(row))

    def test_both_verdicts_are_read_back(self):
        for status in media_store.REVIEW_STATUSES:
            with self.subTest(status=status):
                row = self._row(**{media_store.REVIEW_KEY: {"status": status}})
                self.assertEqual(media_store.review_status(row), status)

    def test_recording_a_verdict_merges_the_existing_metadata(self):
        """Écraser `metadata` ferait perdre l'origine Telegram et l'album du média."""
        table, client = self._client([self._row(telegram=True, media_group_id="7")])
        with mock.patch.object(media_store, "supabase", client):
            row = media_store.set_review_status(
                "m1", "validated", reviewer="42", chunks=3
            )

        metadata = table.updated[0]["metadata"]
        self.assertTrue(metadata["telegram"])
        self.assertEqual(metadata["media_group_id"], "7")
        review = metadata[media_store.REVIEW_KEY]
        self.assertEqual(review["status"], "validated")
        self.assertEqual(review["by"], "42")
        self.assertEqual(review["chunks"], 3)
        # Horodatage ISO daté en UTC : relisible, pas une chaîne opaque.
        self.assertIsNotNone(datetime.fromisoformat(review["at"]))
        self.assertEqual(table.where, ("id", "m1"))
        self.assertEqual(media_store.review_status(row), "validated")

    def test_optional_details_are_left_out_rather_than_guessed(self):
        table, client = self._client([self._row(telegram=True)])
        with mock.patch.object(media_store, "supabase", client):
            media_store.set_review_status("m1", "rejected", chunks=0)
        review = table.updated[0]["metadata"][media_store.REVIEW_KEY]
        self.assertNotIn("by", review, "un relecteur inconnu ne doit pas être inventé")
        self.assertEqual(review["chunks"], 0, "un rejet avec 0 morceau reste un rejet")

    def test_an_unknown_verdict_is_refused_without_touching_the_database(self):
        table, client = self._client([self._row()])
        with mock.patch.object(media_store, "supabase", client):
            with self.assertRaises(ValueError):
                media_store.set_review_status("m1", "peut-être")
        self.assertEqual(table.updated, [])

    def test_an_unknown_media_is_refused_rather_than_created(self):
        table, client = self._client([])
        with mock.patch.object(media_store, "supabase", client):
            with self.assertRaises(ValueError):
                media_store.set_review_status("inconnu", "validated")
        self.assertEqual(table.updated, [])


class ExtractionOutcomeTest(unittest.TestCase):
    """Le **résultat** de l'extraction est noté, lui aussi dans `metadata`.

    C'est ce que lit `/transcribe` pour savoir quoi rattraper : sans cette note,
    un média jamais lu (clef absente) a exactement les mêmes morceaux qu'un média
    lu correctement dès qu'il porte une légende.
    """

    SUMMARY = {
        "ok": False,
        "method": "gemini_vision",
        "chars": 21,
        "chunks": 1,
        "reason": "GEMINI_API_KEY absente : vision indisponible",
        # Ce que le compte-rendu porte en plus, et qui ne doit **pas** atterrir en
        # base : le texte entier, déjà indexé, et l'aperçu qui en est tiré.
        "text": "BTC support 64k\n\n" + "x" * 5000,
        "excerpt": "BTC support 64k…",
        "asset": "BTC-USD",
    }

    def _client(self, rows):
        """La table `knowledge_media` de la doublure, semée de ces lignes."""
        client = supabase_double.SupabaseDouble()
        table = client.store(media_store.TABLE)
        table.rows = [dict(row) for row in rows]
        return table, client

    def _row(self, **metadata):
        return {"id": "m1", "metadata": dict(metadata)}

    def test_reading_is_robust_to_every_missing_shape(self):
        for row in (
            None,
            {},
            {"metadata": None},
            {"metadata": "pas un dict"},
            {"metadata": {"telegram": True}},
            {"metadata": {media_store.OUTCOME_KEY: None}},
            {"metadata": {media_store.OUTCOME_KEY: "ok"}},
        ):
            with self.subTest(row=row):
                self.assertIsNone(media_store.extraction_outcome(row))
                self.assertIsNone(media_store.content_was_extracted(row))

    def test_the_text_is_never_written_to_the_row(self):
        """`metadata` n'est pas un endroit où garder le texte : il est indexé."""
        table, client = self._client([self._row(telegram=True)])
        with mock.patch.object(media_store, "supabase", client):
            media_store.set_extraction_outcome("m1", self.SUMMARY)

        outcome = table.updated[0]["metadata"][media_store.OUTCOME_KEY]
        # Les champs du diagnostic, plus l'horodatage — et rien d'autre.
        self.assertEqual(sorted(outcome), sorted([*media_store.OUTCOME_FIELDS, "at"]))
        self.assertNotIn("text", outcome)
        self.assertNotIn("excerpt", outcome)
        self.assertNotIn("asset", outcome, "l'étiquette a sa propre clé")

    def test_recording_merges_the_existing_metadata(self):
        table, client = self._client([self._row(telegram=True, media_group_id="7")])
        with mock.patch.object(media_store, "supabase", client):
            row = media_store.set_extraction_outcome("m1", self.SUMMARY)

        metadata = table.updated[0]["metadata"]
        self.assertTrue(metadata["telegram"])
        self.assertEqual(metadata["media_group_id"], "7")
        outcome = metadata[media_store.OUTCOME_KEY]
        self.assertFalse(outcome["ok"])
        # Le motif est conservé **tel quel** : c'est lui qui dit quoi réparer.
        self.assertEqual(outcome["reason"], self.SUMMARY["reason"])
        self.assertEqual(outcome["chunks"], 1)
        self.assertIsNotNone(datetime.fromisoformat(outcome["at"]))
        self.assertEqual(table.where, ("id", "m1"))
        self.assertFalse(media_store.content_was_extracted(row))

    def test_a_successful_extraction_carries_the_question(self):
        row = self._row(**{media_store.OUTCOME_KEY: {"ok": True, "method": "pypdf"}})
        self.assertTrue(media_store.content_was_extracted(row))

    def test_an_absent_outcome_is_not_guessed(self):
        """Sans note, on ne dit **pas** « lu » ni « à rattraper » : on ne sait pas."""
        self.assertIsNone(media_store.content_was_extracted(self._row(telegram=True)))

    def test_an_unknown_media_is_refused_rather_than_created(self):
        table, client = self._client([])
        with mock.patch.object(media_store, "supabase", client):
            with self.assertRaises(ValueError):
                media_store.set_extraction_outcome("inconnu", self.SUMMARY)
        self.assertEqual(table.updated, [])

    def test_the_outcome_and_the_verdict_do_not_overwrite_each_other(self):
        """Deux clés distinctes : la machine a lu (ou non), l'humain a validé (ou non)."""
        table, client = self._client(
            [self._row(telegram=True, **{media_store.FINGERPRINT_KEY: "abc"})]
        )
        with mock.patch.object(media_store, "supabase", client):
            media_store.set_extraction_outcome("m1", self.SUMMARY)
            row = media_store.set_review_status("m1", "rejected", reviewer="42")

        # L'empreinte de l'ingestion reste : annoter après coup ne doit pas
        # retirer la seule référence qui prouve une restauration.
        self.assertEqual(
            set(table.updated[-1]["metadata"]),
            {
                "telegram",
                media_store.FINGERPRINT_KEY,
                media_store.OUTCOME_KEY,
                media_store.REVIEW_KEY,
            },
        )
        self.assertFalse(media_store.content_was_extracted(row))
        self.assertEqual(media_store.review_status(row), "rejected")


class AssetTagStoreTest(unittest.TestCase):
    """L'actif associé vit dans `metadata`, **sans écraser** les autres clés."""

    def _client(self, rows):
        """La table `knowledge_media` de la doublure, semée de ces lignes."""
        client = supabase_double.SupabaseDouble()
        table = client.store(media_store.TABLE)
        table.rows = [dict(row) for row in rows]
        return table, client

    def test_writing_the_asset_keeps_every_other_key(self):
        row = {
            "id": "m1",
            "metadata": {
                "telegram": True,
                "media_group_id": "7",
                media_store.REVIEW_KEY: {"status": "validated"},
                media_store.FINGERPRINT_KEY: "abc",
            },
        }
        table, client = self._client([row])
        with mock.patch.object(media_store, "supabase", client):
            written = media_store.set_media_asset("m1", "BTC-USD", source="caption")

        # La ligne rendue est celle **relue après écriture** : elle porte déjà l'étiquette.
        self.assertEqual(media_store.media_asset(written), "BTC-USD")
        metadata = table.updated[0]["metadata"]
        self.assertEqual(metadata["media_group_id"], "7")
        self.assertTrue(metadata["telegram"])
        self.assertEqual(metadata[media_store.REVIEW_KEY]["status"], "validated")
        self.assertEqual(metadata[media_store.FINGERPRINT_KEY], "abc")
        self.assertEqual(
            metadata[media_store.ASSET_KEY], {"value": "BTC-USD", "source": "caption"}
        )

    def test_clearing_removes_the_key_instead_of_writing_null(self):
        """`{"value": null}` serait un état de plus à interpréter partout."""
        row = {
            "id": "m1",
            "metadata": {"telegram": True, media_store.ASSET_KEY: {"value": "BTC-USD"}},
        }
        table, client = self._client([row])
        with mock.patch.object(media_store, "supabase", client):
            media_store.set_media_asset("m1", None)
        metadata = table.updated[0]["metadata"]
        self.assertNotIn(media_store.ASSET_KEY, metadata)
        self.assertTrue(metadata["telegram"])

    def test_reading_survives_every_shape(self):
        cases = [
            (None, (None, None)),
            ({}, (None, None)),
            ({"metadata": "pas un dict"}, (None, None)),
            ({"metadata": {}}, (None, None)),
            ({"metadata": {"asset": {"value": None}}}, (None, None)),
            ({"metadata": {"asset": "   "}}, (None, None)),
            # Forme tracée (écrite par `set_media_asset`) : valeur + provenance.
            ({"metadata": {"asset": {"value": "BTC-USD", "source": "manual"}}}, ("BTC-USD", "manual")),
            # Forme nue : tolérée à la lecture (métadonnées écrites à la main).
            ({"metadata": {"asset": "ETH-USD"}}, ("ETH-USD", None)),
        ]
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertEqual(media_store.asset_tag(row), expected)
                self.assertEqual(media_store.media_asset(row), expected[0])

    def test_a_manual_label_records_who_typed_it(self):
        """Comme le `by` d'un verdict de revue : l'origine manuelle doit être lisible."""
        table, client = self._client([{"id": "m1", "metadata": {}}])
        with mock.patch.object(media_store, "supabase", client):
            media_store.set_media_asset("m1", "BTC-USD", source="manual", reviewer="42")
        self.assertEqual(
            table.updated[0]["metadata"][media_store.ASSET_KEY],
            {"value": "BTC-USD", "source": "manual", "by": "42"},
        )

    def test_an_automatic_label_has_no_author(self):
        table, client = self._client([{"id": "m1", "metadata": {}}])
        with mock.patch.object(media_store, "supabase", client):
            media_store.set_media_asset("m1", "BTC-USD", source="caption")
        self.assertNotIn("by", table.updated[0]["metadata"][media_store.ASSET_KEY])

    def test_an_unknown_media_is_refused_rather_than_created(self):
        table, client = self._client([])
        with mock.patch.object(media_store, "supabase", client):
            with self.assertRaises(ValueError):
                media_store.set_media_asset("inconnu", "BTC-USD")
        self.assertEqual(table.updated, [])

    def test_a_verdict_and_a_label_do_not_overwrite_each_other(self):
        """Le risque réel : deux modules qui écrivent `metadata` en entier."""
        table, client = self._client([{"id": "m1", "metadata": {"telegram": True}}])
        with mock.patch.object(media_store, "supabase", client):
            media_store.set_media_asset("m1", "BTC-USD", source="caption")
            first = dict(table.updated[-1]["metadata"])
            table.rows = [{"id": "m1", "metadata": first}]
            media_store.set_review_status("m1", "rejected", chunks=0)
        metadata = table.updated[-1]["metadata"]
        self.assertEqual(metadata[media_store.ASSET_KEY]["value"], "BTC-USD")
        self.assertEqual(metadata[media_store.REVIEW_KEY]["status"], "rejected")


class PendingReviewTest(unittest.TestCase):
    """Les extractions **sans verdict** : balayage entier, filtre = `review_status`.

    C'est la liste que `/media` ne peut pas donner : sa lecture est bornée aux
    derniers médias, donc une extraction ancienne jamais relue en disparaît.
    """

    def _row(self, media_id, *, verdict=None):
        metadata = {"extraction": {"ok": True}}
        if verdict is not None:
            metadata[media_store.REVIEW_KEY] = verdict
        return {
            "id": media_id,
            "storage_path": f"telegram/{media_id}",
            "created_at": "2026-01-01",
            "metadata": metadata,
        }

    def _scan(self, rows, page_size=1000):
        client = supabase_double.SupabaseDouble()
        table = client.store(media_store.TABLE)
        table.rows = [dict(row) for row in rows]
        with mock.patch.object(media_store, "supabase", client):
            pending = media_store.list_pending_review(page_size=page_size)
        return pending, table

    def test_only_rows_without_a_verdict_are_returned(self):
        rows = [
            self._row("none"),
            self._row("validated", verdict={"status": "validated"}),
            self._row("rejected", verdict={"status": "rejected"}),
        ]
        pending, _ = self._scan(rows)
        self.assertEqual([row["id"] for row in pending], ["none"])

    def test_an_unreadable_verdict_counts_as_no_verdict(self):
        """Le **même** prédicat que celui qui affiche le verdict : sinon les deux divergent."""
        rows = [
            self._row("bad-status", verdict={"status": "peut-être"}),
            self._row("not-a-dict", verdict="validated"),
            self._row("no-key"),
        ]
        pending, _ = self._scan(rows)
        self.assertEqual(
            [row["id"] for row in pending], ["bad-status", "not-a-dict", "no-key"]
        )

    def test_a_row_without_any_metadata_is_pending(self):
        client = supabase_double.SupabaseDouble()
        table = client.store(media_store.TABLE)
        table.rows = [{"id": "bare", "storage_path": "p"}]
        with mock.patch.object(media_store, "supabase", client):
            pending = media_store.list_pending_review()
        self.assertEqual([row["id"] for row in pending], ["bare"])

    def test_the_scan_covers_every_page_not_just_the_first(self):
        """L'attente ancienne d'une page lointaine doit être trouvée elle aussi."""
        rows = [self._row("v1", verdict={"status": "validated"}) for _ in range(3)]
        rows.append(self._row("old-pending"))
        pending, table = self._scan(rows, page_size=2)
        self.assertEqual([row["id"] for row in pending], ["old-pending"])
        self.assertGreaterEqual(len([call for call in table.calls if call[0] == "range"]), 2)

    def test_the_scan_reads_the_newest_first_and_asks_for_the_verdict(self):
        """Tri par date décroissante, et la projection porte `metadata` — pas la légende."""
        _, table = self._scan([self._row("a")], page_size=1)
        self.assertIn(("order", "created_at", True), table.calls)
        self.assertIn("metadata", table.selection)
        self.assertNotIn("caption", table.selection)


class AllMediaScanTest(unittest.TestCase):
    """`list_all_media` : la table **entière**, pour les listes qui ne peuvent pas être bornées.

    Deux listes en dépendent — `/pending` (verdict manquant) et `/transcribe`
    (texte à rattraper) — et elles n'en diffèrent que par le **prédicat**
    qu'elles appliquent ensuite. Le parcours, lui, doit être le même : deux scans
    séparés finiraient par ne plus voir les mêmes lignes.
    """

    def _row(self, media_id, *, verdict=None):
        metadata = {"extraction": {"ok": True}}
        if verdict is not None:
            metadata[media_store.REVIEW_KEY] = verdict
        return {
            "id": media_id,
            "storage_path": f"telegram/{media_id}",
            "created_at": "2026-01-01",
            "metadata": metadata,
        }

    def _scan(self, rows, page_size=1000):
        client = supabase_double.SupabaseDouble()
        table = client.store(media_store.TABLE)
        table.rows = [dict(row) for row in rows]
        with mock.patch.object(media_store, "supabase", client):
            scanned = media_store.list_all_media(page_size=page_size)
        return scanned, table

    def test_no_verdict_filters_anything_out(self):
        """L'inverse de `list_pending_review` : le tri se fait chez l'appelant."""
        rows = [
            self._row("none"),
            self._row("validated", verdict={"status": "validated"}),
            self._row("rejected", verdict={"status": "rejected"}),
        ]
        scanned, _ = self._scan(rows)
        self.assertEqual([row["id"] for row in scanned], ["none", "validated", "rejected"])

    def test_the_scan_covers_every_page_not_just_the_first(self):
        """Le rattrapage ancien d'une page lointaine doit être lu lui aussi."""
        rows = [self._row(f"m{i}") for i in range(5)]
        scanned, table = self._scan(rows, page_size=2)
        self.assertEqual([row["id"] for row in scanned], [f"m{i}" for i in range(5)])
        self.assertGreaterEqual(len([call for call in table.calls if call[0] == "range"]), 2)

    def test_the_scan_reads_the_newest_first_and_asks_for_the_metadata(self):
        _, table = self._scan([self._row("a")], page_size=1)
        self.assertIn(("order", "created_at", True), table.calls)
        self.assertIn("metadata", table.selection)
        self.assertNotIn("caption", table.selection)

    def test_the_pending_list_is_this_scan_filtered(self):
        """Un seul parcours de la table : `list_pending_review` ne relit pas de son côté."""
        rows = [
            self._row("pending"),
            self._row("validated", verdict={"status": "validated"}),
        ]
        client = supabase_double.SupabaseDouble()
        table = client.store(media_store.TABLE)
        table.rows = [dict(row) for row in rows]
        with mock.patch.object(media_store, "supabase", client), mock.patch.object(
            media_store, "list_all_media", wraps=media_store.list_all_media
        ) as scan:
            pending = media_store.list_pending_review()
        self.assertEqual([row["id"] for row in pending], ["pending"])
        self.assertEqual(scan.call_count, 1)


class UnconfiguredClientTest(unittest.TestCase):
    def test_operations_fail_loudly_without_a_client(self):
        with mock.patch.object(media_store, "supabase", None):
            for call in (
                lambda: media_store.upload_media(b"x"),
                lambda: media_store.list_media(),
                lambda: media_store.list_pending_review(),
                lambda: media_store.list_all_media(),
                lambda: media_store.get_media("x"),
                lambda: media_store.delete_media("x"),
                lambda: media_store.download_media("p"),
                lambda: media_store.create_signed_url("p"),
                lambda: media_store.iter_storage_paths(),
                lambda: media_store.all_media_paths(),
                lambda: media_store.list_orphan_objects(),
                lambda: media_store.list_missing_objects(),
                lambda: media_store.delete_objects(["p"]),
                lambda: media_store.restore_object("p", b"x"),
                lambda: media_store.object_exists("p"),
                lambda: media_store.set_review_status("x", "validated"),
                lambda: media_store.set_media_asset("x", "BTC-USD"),
            ):
                with self.subTest(call=call):
                    with self.assertRaises(RuntimeError):
                        call()


if __name__ == "__main__":
    unittest.main()
