"""Contrat de la doublure Supabase partagée (`tests/supabase_double.py`).

Une dizaine de fichiers en dépendent : si la doublure cessait
d'appliquer un filtre ou de persister une écriture, leurs vérifications
s'affaibliraient **sans échouer** — c'est exactement l'histoire que ce dépôt
raconte (plusieurs variantes locales, dont certaines ignoraient les filtres
qu'elles voyaient passer). Ces tests tiennent donc la doublure elle-même : ce qu'elle
imite, et ce qu'elle refuse d'imiter.

Quatre exigences y sont vérifiées en propre, parce qu'elles rendent les autres
tests honnêtes :

* une **lecture non interprétée échoue** (`or_` inconnu, filtre inconnu) au lieu
  de rendre des lignes en trop — une doublure plus permissive que Postgres
  ferait passer un filtre perdu pour un filtre correct ;
* un **RPC inattendu échoue** au lieu de rendre une base vide ;
* en **lecture seule**, aucune écriture ne passe — table, bucket ou RPC non
  déclaré — et le refus nomme ce qui a été tenté. C'est ce mode qui porte la
  vérification la plus utile de la sonde Supabase : lancée sur une base de
  **production**, elle ne doit rien écrire tant qu'on ne lui a pas demandé
  `--roundtrip` ;
* aucune doublure locale ne **réapparaît** dans un fichier qui utilise
  celle-ci — c'est la seule façon de ne pas rejouer la divergence qu'on vient de
défaire.
"""
from __future__ import annotations

import ast
import pathlib
import unittest

from database import media_store
from tests import supabase_double

TESTS = pathlib.Path(__file__).resolve().parent
ROOT = TESTS.parent
README = ROOT / "README.md"
SHARED = "supabase_double"

#: L'endroit du README où la promesse de la sonde est écrite — et donc l'endroit
#: où elle doit rester, à côté du script qu'elle décrit.
PROBE_SECTION = "### Vérifier le projet Supabase"

#: Les fichiers dont la doublure locale a cédé la place à celle-ci. Ils sont
#: nommés, et pas seulement déduits ci-dessous : sans cette liste, il suffirait de
#: **supprimer l'import** pour sortir du contrat.
MIGRATED = (
    "test_api_auth_integration.py",
    "test_knowledge_index.py",
    "test_learning_gd.py",
    "test_media_cycle.py",
    "test_media_reconcile.py",
    "test_media_router.py",
    "test_media_store.py",
    "test_performance_tracker.py",
    "test_supabase_config.py",
    "test_supabase_watch.py",
    "test_telegram_scan_config.py",
)


def _local_doubles(source: str) -> list:
    """Les classes qui ont la **forme** d'un client Supabase ou d'une table.

    La forme, pas le nom : c'est un `table(nom)` ou un enchaînement
    `select(…).eq(…).execute()`. Renommer la doublure ne la ferait pas disparaître
    du contrat, et inversement une classe qui emprunte ces noms sans en avoir la
    forme (un journal d'appels, un enregistreur) n'y entre pas.
    """
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        methods = {item.name for item in node.body if isinstance(item, ast.FunctionDef)}
        if "table" in methods or {"select", "execute"} <= methods:
            found.append(node.name)
    return found


class ReadTest(unittest.TestCase):
    """Les lectures : filtres, projection, tri, pagination — comme PostgREST."""

    def setUp(self) -> None:
        self.client = supabase_double.SupabaseDouble()
        self.table = self.client.store("chunks")
        self.table.rows = [
            {"id": 1, "media_id": "m1", "asset": "BTC", "chunk_index": 1},
            {"id": 2, "media_id": "m1", "asset": None, "chunk_index": 0},
            {"id": 3, "media_id": "m2", "asset": "ETH", "chunk_index": 0},
        ]

    def _read(self):
        return self.client.table("chunks").select("*").eq("media_id", "m1").execute().data

    def test_a_filter_selects_the_rows_it_names(self):
        self.assertEqual([row["id"] for row in self._read()], [1, 2])

    def test_a_row_missing_the_filtered_column_does_not_match(self):
        """C'est le piège que les doublures précédentes masquaient."""
        self.table.rows.append({"id": 4, "chunk_index": 0})
        self.assertEqual([row["id"] for row in self._read()], [1, 2])

    def test_in_filters_on_a_set_of_values(self):
        rows = self.client.table("chunks").select("*").in_("media_id", ["m2", "m9"]).execute().data
        self.assertEqual([row["id"] for row in rows], [3])

    def test_or_reads_the_postgrest_branches(self):
        rows = (
            self.client.table("chunks")
            .select("id")
            .or_("asset.is.null,asset.neq.BTC")
            .execute()
            .data
        )
        self.assertEqual([row["id"] for row in rows], [2, 3])

    def test_an_unreadable_branch_fails_loudly(self):
        with self.assertRaises(AssertionError):
            self.client.table("chunks").select("id").or_("asset.sounds.like.BTC").execute()

    def test_a_projection_drops_the_other_columns(self):
        row = self.client.table("chunks").select("id,chunk_index").eq("id", 1).execute().data[0]
        self.assertEqual(row, {"id": 1, "chunk_index": 1})

    def test_the_star_projection_keeps_everything(self):
        row = self.client.table("chunks").select("*").eq("id", 1).execute().data[0]
        self.assertEqual(sorted(row), ["asset", "chunk_index", "id", "media_id"])

    def test_order_and_range_paginate_like_the_api(self):
        page = (
            self.client.table("chunks")
            .select("id")
            .order("chunk_index")
            .range(0, 1)
            .execute()
            .data
        )
        self.assertEqual([row["id"] for row in page], [2, 3])

    def test_several_orders_compose_outermost_last(self):
        page = (
            self.client.table("chunks")
            .select("id")
            .order("chunk_index")
            .order("id", desc=True)
            .execute()
            .data
        )
        self.assertEqual([row["id"] for row in page], [3, 2, 1])

    def test_a_missing_column_sorts_last(self):
        self.table.rows.append({"id": 9, "media_id": "m9"})
        page = self.client.table("chunks").select("id").order("media_id").execute().data
        self.assertEqual(page[-1]["id"], 9)

    def test_limit_bounds_the_read(self):
        page = self.client.table("chunks").select("id").limit(2).execute().data
        self.assertEqual(len(page), 2)

    def test_a_failing_read_raises(self):
        self.table.read_error = RuntimeError("db down")
        with self.assertRaises(RuntimeError):
            self._read()


class WriteTest(unittest.TestCase):
    """Les écritures : elles **persistent**, et la réponse est la ligne écrite."""

    def setUp(self) -> None:
        self.client = supabase_double.SupabaseDouble()
        self.table = self.client.store("media")

    def test_an_insert_assigns_an_identifier_and_persists(self):
        row = self.client.table("media").insert({"storage_path": "a.bin"}).execute().data[0]

        self.assertEqual(row["id"], "row-1")
        self.assertEqual(self.table.rows, [{"storage_path": "a.bin", "id": "row-1"}])
        self.assertEqual(self.table.inserted, [{"storage_path": "a.bin"}])

    def test_an_identifier_factory_decides_what_postgres_would(self):
        self.table.id_factory = lambda: "uuid-4"
        row = self.client.table("media").insert({"storage_path": "a.bin"}).execute().data[0]
        self.assertEqual(row["id"], "uuid-4")

    def test_a_list_insert_stores_every_row(self):
        rows = self.client.table("media").insert([{"n": 1}, {"n": 2}]).execute().data
        self.assertEqual([row["n"] for row in rows], [1, 2])
        self.assertEqual(len(self.table.rows), 2)

    def test_an_upsert_replaces_the_row_of_the_same_key_and_keeps_its_id(self):
        first = self.client.table("media").insert({"storage_path": "a.bin"}).execute().data[0]
        again = (
            self.client.table("media")
            .upsert({"storage_path": "a.bin", "size": 3}, on_conflict="storage_path")
            .execute()
            .data[0]
        )

        self.assertEqual(again["id"], first["id"], "la ligne remplacée garde son identité")
        self.assertEqual(len(self.table.rows), 1)
        self.assertEqual(self.table.upserted, [({"storage_path": "a.bin", "size": 3}, "storage_path")])

    def test_an_update_returns_the_rows_as_written(self):
        self.client.table("media").insert({"storage_path": "a.bin", "metadata": {"x": 1}}).execute()
        written = (
            self.client.table("media")
            .update({"metadata": {"x": 2}})
            .eq("storage_path", "a.bin")
            .execute()
            .data
        )

        self.assertEqual(written[0]["metadata"], {"x": 2})
        self.assertEqual(self.table.rows[0]["metadata"], {"x": 2})
        self.assertEqual(self.table.updated, [{"metadata": {"x": 2}}])
        self.assertEqual(self.table.where, ("storage_path", "a.bin"))

    def test_an_update_leaves_the_other_rows_alone(self):
        self.client.table("media").insert([{"id": "a"}, {"id": "b"}]).execute()
        self.client.table("media").update({"n": 1}).eq("id", "a").execute()
        self.assertEqual([row.get("n") for row in self.table.rows], [1, None])

    def test_a_delete_removes_the_matching_rows_and_says_so(self):
        self.client.table("media").insert([{"id": "a"}, {"id": "b"}]).execute()
        removed = self.client.table("media").delete().eq("id", "a").execute().data

        self.assertEqual([row["id"] for row in removed], ["a"])
        self.assertTrue(self.table.deleted, "le journal dit qu'une suppression a eu lieu")
        self.assertEqual([row["id"] for row in self.table.rows], ["b"])

    def test_a_delete_that_matched_nothing_is_still_a_delete_that_ran(self):
        """`deleted` dit qu'une suppression a été **émise**, pas qu'elle a touché."""
        self.client.table("media").delete().eq("id", "absent").execute()
        self.assertTrue(self.table.deleted)
        self.assertEqual(self.table.rows, [])

    def test_failures_are_injected_per_operation(self):
        self.table.insert_error = RuntimeError("db down")
        with self.assertRaises(RuntimeError):
            self.client.table("media").insert({"n": 1}).execute()
        self.assertEqual(self.table.rows, [])


class JournalTest(unittest.TestCase):
    """Le journal : un flux commun, et un journal par table pour l'attribution."""

    def setUp(self) -> None:
        self.client = supabase_double.SupabaseDouble()
        self.media = self.client.store("media")
        self.chunks = self.client.store("chunks")

    def test_the_client_logs_every_operation_in_order(self):
        self.client.table("media").select("*").eq("id", "m1").limit(1).execute()

        self.assertEqual(
            self.client.calls,
            [
                ("select", ("*",), {}),
                ("eq", "id", "m1"),
                ("limit", 1),
            ],
        )

    def test_each_table_keeps_its_own_journal(self):
        self.client.table("media").select("id").eq("id", "m1").execute()
        self.client.table("chunks").select("id").eq("media_id", "m1").execute()

        self.assertEqual(self.client.calls.count(("eq", "id", "m1")), 1)
        self.assertIn(("eq", "media_id", "m1"), self.chunks.calls)
        self.assertNotIn(("eq", "media_id", "m1"), self.media.calls)

    def test_an_abandoned_query_is_still_visible(self):
        """Le journal dit ce qui a été **demandé**, pas seulement ce qui a abouti."""
        self.client.table("media").select("id").limit(5_000)
        self.assertEqual(self.client.operations("limit"), [("limit", 5_000)])

    def test_the_helpers_read_the_log(self):
        self.client.table("media").update({"n": 1}).eq("id", "a").execute()
        self.client.table("media").update({"n": 2}).eq("id", "b").execute()

        self.assertEqual(self.client.updates(), [{"n": 1}, {"n": 2}])

    def test_the_acquisition_of_a_table_is_not_an_operation(self):
        """Sinon `calls[0]` désignerait un appel qui n'existe pas côté PostgREST."""
        self.client.table("media")
        self.assertEqual(self.client.calls, [])


class RpcTest(unittest.TestCase):
    """Les fonctions Postgres : lignes fixes, gestionnaire, ou refus."""

    def setUp(self) -> None:
        self.client = supabase_double.SupabaseDouble()

    def test_a_fixed_result_is_returned(self):
        self.client.rpc_rows = [{"similarity": 0.9}]
        rows = self.client.rpc("match_knowledge_chunks", {"match_count": 5}).execute().data

        self.assertEqual(rows, [{"similarity": 0.9}])
        self.assertEqual(
            self.client.rpc_calls(), [("rpc", "match_knowledge_chunks", {"match_count": 5})]
        )

    def test_a_handler_reads_the_base_at_execution_time(self):
        """C'est ce qui permet de rejouer un RPC sur ce qui vient d'être écrit."""
        self.client.store("chunks").rows.append({"content": "après"})
        calls = []
        self.client.handle_rpc("search", lambda params: calls.append(params) or [{"ok": True}])

        rows = self.client.rpc("search", {"q": "x"}).execute().data
        self.assertEqual(rows, [{"ok": True}])
        self.assertEqual(calls, [{"q": "x"}])

    def test_an_expected_rpc_that_was_never_cabled_raises(self):
        """Rendre une base vide ferait passer un RPC oublié pour un RPC vide."""
        with self.assertRaises(AssertionError):
            self.client.rpc("match_knowledge_chunks", {})


class StorageTest(unittest.TestCase):
    """Le bucket : ce qui est déposé se relit, et ce qui est listé est scripté."""

    def setUp(self) -> None:
        self.client = supabase_double.SupabaseDouble(
            storage=supabase_double.Storage(
                listing={"": [{"name": "telegram", "id": None}], "telegram": []}
            )
        )

    def test_an_upload_is_readable_back(self):
        bucket = self.client.storage.from_("knowledge-media")
        bucket.upload("telegram/1-a.jpg", b"octets", {"upsert": "true"})

        self.assertEqual(self.client.storage.bucket_name, "knowledge-media")
        self.assertEqual(self.client.storage.buckets, ["knowledge-media"])
        self.assertEqual(self.client.storage.files["telegram/1-a.jpg"], b"octets")
        self.assertEqual(
            self.client.storage.uploads,
            [("telegram/1-a.jpg", b"octets", {"upsert": "true"})],
        )
        self.assertEqual(bucket.download("telegram/1-a.jpg"), b"octets")

    def test_downloading_an_absent_object_raises(self):
        with self.assertRaises(KeyError):
            self.client.storage.from_("knowledge-media").download("absent.bin")

    def test_a_removal_empties_the_file_and_journals_the_batch(self):
        bucket = self.client.storage.from_("knowledge-media")
        bucket.upload("a.bin", b"1")
        bucket.upload("b.bin", b"2")

        bucket.remove(["a.bin", "b.bin"])
        self.assertEqual(self.client.storage.removed, [["a.bin", "b.bin"]])
        self.assertEqual(list(self.client.storage.files), [])

    def test_the_listing_follows_the_non_recursive_api(self):
        bucket = self.client.storage.from_("knowledge-media")
        self.assertEqual(bucket.list(""), [{"name": "telegram", "id": None}])
        self.assertEqual(bucket.list("telegram"), [])
        self.assertEqual(bucket.list("inconnu"), [])

    def test_a_flat_listing_is_served_as_is(self):
        storage = supabase_double.Storage(listing=[{"name": "a.bin"}])
        self.assertEqual(storage.list(), [{"name": "a.bin"}])

    def test_a_signed_url_is_deterministic(self):
        url = self.client.storage.from_("knowledge-media").create_signed_url("a.bin", 120)
        self.assertEqual(url, {"signedURL": "https://example.test/a.bin?exp=120"})


class ReadOnlyTest(unittest.TestCase):
    """Le mode lecture seule : aucune écriture ne passe, et chacune est nommée."""

    def setUp(self) -> None:
        self.client = supabase_double.SupabaseDouble(read_only=True)

    def test_every_table_write_is_refused_and_named(self) -> None:
        attempts = {
            "insert": lambda: self.client.table("insights").insert({"n": 1}),
            "upsert": lambda: self.client.table("insights").upsert({"n": 1}),
            "update": lambda: self.client.table("insights").update({"n": 1}),
            "delete": lambda: self.client.table("insights").delete(),
        }
        for operation, attempt in attempts.items():
            with self.subTest(operation=operation):
                with self.assertRaises(supabase_double.ReadOnlyError) as raised:
                    attempt()
                self.assertIn(operation, str(raised.exception))
                self.assertIn("insights", str(raised.exception))

    def test_a_refused_write_is_in_neither_journal(self) -> None:
        """Elle n'a été ni demandée au serveur ni écrite : le refus dit tout."""
        with self.assertRaises(supabase_double.ReadOnlyError):
            self.client.table("insights").insert({"n": 1})
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.client.writes, [])
        self.assertEqual(self.client.store("insights").rows, [])

    def test_reading_still_passes_in_read_only(self) -> None:
        self.client.store("insights").rows = [{"asset": "BTC"}]
        rows = self.client.table("insights").select("*").eq("asset", "BTC").execute().data
        self.assertEqual(len(rows), 1)

    def test_the_bucket_follows_the_client(self) -> None:
        bucket = self.client.storage.from_("knowledge-media")
        with self.assertRaises(supabase_double.ReadOnlyError):
            bucket.upload("a.bin", b"octets")
        with self.assertRaises(supabase_double.ReadOnlyError):
            bucket.remove(["a.bin"])
        self.assertEqual(self.client.storage.files, {}, "rien n'a été déposé")
        self.assertEqual(self.client.storage.uploads, [])

    def test_a_late_switch_closes_the_bucket_too(self) -> None:
        """Un régime par objet serait une invitation à l'oublier."""
        client = supabase_double.SupabaseDouble(storage=supabase_double.Storage())
        client.read_only = True
        with self.assertRaises(supabase_double.ReadOnlyError):
            client.storage.from_("knowledge-media").upload("a.bin", b"octets")

    def test_a_bucket_can_be_read_only_on_its_own(self) -> None:
        storage = supabase_double.Storage(read_only=True)
        with self.assertRaises(supabase_double.ReadOnlyError):
            storage.upload("a.bin", b"octets")

    def test_an_undeclared_rpc_is_refused(self) -> None:
        """La doublure ne lit pas le corps d'une fonction Postgres : on la déclare."""
        self.client.handle_rpc("search", lambda params: [])
        with self.assertRaises(supabase_double.ReadOnlyError) as raised:
            self.client.rpc("search", {})
        self.assertIn("writes=False", str(raised.exception), "le refus dit comment déclarer")

    def test_canned_rpc_rows_are_not_an_excuse(self) -> None:
        self.client.rpc_rows = [{"ok": True}]
        with self.assertRaises(supabase_double.ReadOnlyError):
            self.client.rpc("inconnu", {})

    def test_a_declared_read_only_rpc_passes(self) -> None:
        self.client.handle_rpc(
            "match_knowledge_chunks", lambda params: [{"ok": True}], writes=False
        )
        rows = self.client.rpc("match_knowledge_chunks", {"n": 1}).execute().data
        self.assertEqual(rows, [{"ok": True}])
        self.assertEqual(self.client.rpc_calls(), [("rpc", "match_knowledge_chunks", {"n": 1})])

    def test_a_declared_writing_rpc_is_refused(self) -> None:
        self.client.handle_rpc("record", lambda params: [], writes=True)
        with self.assertRaises(supabase_double.ReadOnlyError):
            self.client.rpc("record", {})


class FailureTest(unittest.TestCase):
    """Les pannes programmées : par table et par opération, durables ou transitoires."""

    def setUp(self) -> None:
        self.client = supabase_double.SupabaseDouble()

    def test_a_failure_hits_the_named_operation_only(self) -> None:
        self.client.fail("insights", "select", RuntimeError("db down"))
        with self.assertRaises(RuntimeError):
            self.client.table("insights").select("*").execute()

        self.client.table("insights").insert({"n": 1}).execute()
        self.client.table("users").select("*").execute()

    def test_an_unknown_operation_is_refused(self) -> None:
        """Une panne qu'on croit posée et qui n'arrive pas est un test qui ne teste rien."""
        with self.assertRaises(AssertionError):
            self.client.fail("insights", "truc", RuntimeError("boom"))

    def test_a_transient_failure_is_consumed_by_the_attempt(self) -> None:
        """C'est ainsi qu'on éprouve une reprise : une fois raté, la fois d'après passe."""
        self.client.store("insights").rows = [{"asset": "PROBE"}]
        self.client.fail("insights", "delete", [RuntimeError("hoquet")])

        with self.assertRaises(RuntimeError):
            self.client.table("insights").delete().eq("asset", "PROBE").execute()

        self.client.table("insights").delete().eq("asset", "PROBE").execute()
        self.assertEqual(self.client.store("insights").rows, [])
        self.assertIsNone(self.client.store("insights").delete_error, "la panne est consommée")

    def test_an_enduring_failure_is_not_consumed(self) -> None:
        self.client.fail("insights", "delete", RuntimeError("refus"))
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                with self.assertRaises(RuntimeError):
                    self.client.table("insights").delete().execute()

    def test_the_write_journal_keeps_the_order_of_tables(self) -> None:
        client = self.client
        client.table("users").upsert({"id": "probe-1"}).execute()
        client.table("insights").insert({"asset": "PROBE"}).execute()
        client.table("pending_signals").delete().eq("user_id", "probe-1").execute()
        client.table("users").delete().eq("id", "probe-1").execute()

        self.assertEqual(
            client.writes,
            [
                ("upsert", "users"),
                ("insert", "insights"),
                ("delete", "pending_signals"),
                ("delete", "users"),
            ],
        )
        self.assertEqual(client.write_order("delete"), ["pending_signals", "users"])
        self.assertEqual(
            client.write_order(),
            ["users", "insights", "pending_signals", "users"],
        )

    def test_a_write_that_never_ran_is_not_journaled(self) -> None:
        """Bâtir une écriture n'est pas écrire — `calls` le dit, `writes` non."""
        self.client.table("insights").insert({"n": 1})
        self.assertEqual(self.client.writes, [])
        self.assertEqual(self.client.operations("insert"), [("insert", {"n": 1})])

    def test_a_failed_write_is_not_journaled(self) -> None:
        """La panne l'a empêchée : le journal des écritures ne doit pas la compter."""
        self.client.fail("insights", "insert", RuntimeError("refus"))
        with self.assertRaises(RuntimeError):
            self.client.table("insights").insert({"n": 1}).execute()
        self.assertEqual(self.client.writes, [])
        self.assertEqual(self.client.store("insights").rows, [])

    def test_a_batch_upsert_writes_every_row(self) -> None:
        """PostgREST écrit **toutes** les lignes d'une liste : `upsert_economic_events` en envoie une."""
        rows = (
            self.client.table("economic_events")
            .upsert([{"id": 1, "n": "a"}, {"id": 2, "n": "b"}], on_conflict="id")
            .execute()
            .data
        )

        self.assertEqual([row["id"] for row in rows], [1, 2])
        self.assertEqual([row["n"] for row in self.client.store("economic_events").rows], ["a", "b"])
        payload, conflict = self.client.store("economic_events").upserted[0]
        self.assertEqual(conflict, "id")
        self.assertEqual([row["id"] for row in payload], [1, 2], "le journal dit la charge envoyée")

    def test_a_batch_upsert_replaces_the_rows_of_the_same_keys(self) -> None:
        table = self.client.store("economic_events")
        table.rows = [{"id": 1, "n": "ancien", "other": True}]
        self.client.table("economic_events").upsert(
            [{"id": 1, "n": "neuf"}], on_conflict="id"
        ).execute()
        self.assertEqual(table.rows, [{"id": 1, "n": "neuf"}])


class NoLocalDoubleTest(unittest.TestCase):
    """Aucun fichier ne se re-dote d'un client à lui.

    C'est l'histoire que ce module raconte, et elle se répète volontiers : écrire
    une doublure locale est plus court que de semer celle-ci, et elle diverge
    ensuite en silence — les précédentes ignoraient les filtres qu'elles voyaient
    passer, donc leurs tests passaient sur des comportements que PostgREST ne rend
    pas. Un fichier qui utilise la doublure partagée ne doit donc plus en définir
    une autre.
    """

    def _sources(self):
        return {path.name: path.read_text(encoding="utf-8") for path in TESTS.glob("test_*.py")}

    def test_the_migrated_files_still_use_the_shared_double(self):
        sources = self._sources()
        for name in MIGRATED:
            with self.subTest(name=name):
                self.assertIn(f"from tests import {SHARED}", sources[name])

    def test_a_file_that_uses_the_shared_double_defines_no_local_one(self):
        for name, source in self._sources().items():
            if f"from tests import {SHARED}" not in source:
                continue
            with self.subTest(name=name):
                self.assertEqual(_local_doubles(source), [], "une doublure locale est réapparue")


class ReadOnlyDocumentedTest(unittest.TestCase):
    """Le mode lecture seule, et la promesse qu'il sert — dans le README.

    Ce mode existe pour une phrase : « sans `--roundtrip`, la sonde Supabase ne
    touche pas à la base qu'on lui donne ». Elle est le seul garde-fou d'un outil
    qui se lance sur une base de **production**, et une promesse qui n'est écrite
    nulle part n'est ni relue ni tenue.
    """

    def setUp(self) -> None:
        self.readme = README.read_text(encoding="utf-8")

    def test_the_mode_is_documented(self):
        """Un mode que la doublure porte sans que rien ne le dise se perd."""
        self.assertIn("read_only=True", self.readme)
        self.assertIn("ReadOnlyError", self.readme)
        self.assertIn("tests/test_supabase_config.py", self.readme)

    def test_the_promise_is_written_next_to_the_probe(self):
        """« Le chemin de lecture n'écrit rien » se lit avec la sonde, pas ailleurs."""
        start = self.readme.index(PROBE_SECTION)
        next_section = self.readme.find("\n### ", start + 1)
        section = self.readme[start:] if next_section == -1 else self.readme[start:next_section]
        self.assertIn("lecture seule", section)
        self.assertIn("test_supabase_config.py", section)


class PerRunProbeKeyDocumentedTest(unittest.TestCase):
    """La clé de sonde unique par exécution, écrite avec la sonde qu'elle protège.

    Un actif partagé ne casse rien tant qu'une seule sonde tourne — c'est
    exactement pourquoi la phrase qui dit **pourquoi** le suffixe est là doit rester
    dans la section de la sonde : la première simplification venue (« `PROBE`
    suffit ») le retire sans que rien ne rougisse le jour ordinaire, et le défaut ne
    se revoit que le jour où deux vérifications tombent ensemble.
    """

    def test_the_per_run_key_is_documented_next_to_the_probe(self):
        readme = README.read_text(encoding="utf-8")
        start = readme.index(PROBE_SECTION)
        next_section = readme.find("\n### ", start + 1)
        section = readme[start:] if next_section == -1 else readme[start:next_section]
        self.assertIn("unique à l'exécution", section)
        self.assertIn("SimultaneousRunTest", section)


class InstallTest(unittest.TestCase):
    """`use_supabase` pose le client, et le retire en fin de test."""

    def test_the_client_is_installed_and_then_removed(self):
        sentinel = media_store.supabase
        client = supabase_double.SupabaseDouble()
        supabase_double.use_supabase(self, client, media_store)
        self.assertIs(media_store.supabase, client)

        # `doCleanups` exécute ce que `use_supabase` a enregistré : sans cela, un
        # client resterait posé sur le module pour les tests suivants.
        self.doCleanups()
        self.assertIs(media_store.supabase, sentinel)


if __name__ == "__main__":
    unittest.main()
