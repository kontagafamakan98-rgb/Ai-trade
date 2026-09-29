"""Tests du script `scripts/reconcile_media.py`.

Le script ne doit **jamais** supprimer sans `--delete` : c'est la garantie qui
rend son exécution sûre sur un bucket de production. On vérifie aussi les codes
de sortie, pour qu'un échec d'accès à Supabase soit visible dans un cron.
"""
from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from database import media_store
from scripts import reconcile_media


class ReconcileMediaCliTest(unittest.TestCase):
    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = reconcile_media.main(argv)
        return code, out.getvalue()

    def test_lists_orphans_without_deleting_by_default(self):
        with mock.patch.object(
            media_store, "list_orphan_objects", return_value=["a.bin", "b.bin"]
        ) as listing, mock.patch.object(media_store, "delete_objects") as delete:
            code, out = self._run([])
        self.assertEqual(code, 0)
        self.assertIn("a.bin", out)
        self.assertIn("b.bin", out)
        self.assertIn("--delete", out)
        delete.assert_not_called()
        listing.assert_called_once_with("")

    def test_delete_flag_removes_the_orphans(self):
        with mock.patch.object(
            media_store, "list_orphan_objects", return_value=["a.bin", "b.bin"]
        ), mock.patch.object(media_store, "delete_objects", return_value=2) as delete:
            code, out = self._run(["--delete"])
        self.assertEqual(code, 0)
        self.assertIn("2 objet(s) supprimé(s)", out)
        self.assertEqual(delete.call_args[0][0], ["a.bin", "b.bin"])
        self.assertEqual(delete.call_args[1]["batch_size"], media_store.REMOVE_BATCH_SIZE)

    def test_prefix_is_forwarded(self):
        with mock.patch.object(
            media_store, "list_orphan_objects", return_value=[]
        ) as listing:
            code, out = self._run(["--prefix", "telegram/canal"])
        self.assertEqual(code, 0)
        self.assertIn("Aucun objet orphelin", out)
        listing.assert_called_once_with("telegram/canal")

    def test_limit_bounds_the_work(self):
        with mock.patch.object(
            media_store, "list_orphan_objects", return_value=["a", "b", "c"]
        ), mock.patch.object(media_store, "delete_objects", return_value=1) as delete:
            code, _ = self._run(["--delete", "--limit", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(delete.call_args[0][0], ["a"])

    def test_inaccessible_backend_exits_1(self):
        with mock.patch.object(
            media_store, "list_orphan_objects", side_effect=RuntimeError("db down")
        ):
            code, out = self._run([])
        self.assertEqual(code, 1)
        self.assertIn("db down", out)

    def test_deletion_failure_exits_1(self):
        with mock.patch.object(
            media_store, "list_orphan_objects", return_value=["a.bin"]
        ), mock.patch.object(
            media_store, "delete_objects", side_effect=RuntimeError("storage down")
        ):
            code, out = self._run(["--delete"])
        self.assertEqual(code, 1)
        self.assertIn("storage down", out)


if __name__ == "__main__":
    unittest.main()
