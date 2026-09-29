"""Remise en place des octets manquants (`core/media_repair.py`).

Ce qui se teste ici est ce qu'un rapport heureux ne montre pas :

* un objet **présent** n'est jamais réécrit — la voie du scraper rend la copie de
  l'aperçu, possiblement réduite, donc restaurer par-dessus un original intact le
  dégraderait pour réparer une panne imaginaire ;
* la **ligne n'est jamais modifiée** : le module ne connaît pas `upload_media`,
  seul chemin qui réécrirait `metadata` (verdict de revue, actif, note
  d'extraction) et repaierait une extraction déjà indexée ;
* une **vérification de présence impossible** s'abstient au lieu de déposer : ne
  pas savoir, c'est ne pas écraser ;
* l'ordre des voies est `telegram_file_id` puis l'aperçu public, et un échec
  **nomme les deux** — « le scraper n'a rien trouvé après un `file_id` refusé »
  et « aucune voie possible » n'appellent pas la même suite ;
* les octets qui reviennent sont comparés à l'**empreinte** enregistrée à
  l'ingestion : la taille ne prouve pas l'identité (une copie d'aperçu retaillée
  peut tomber juste), et « aucune empreinte » n'est pas « ça correspond ».

Aucun réseau, aucune base : les trois frontières (téléchargement par `file_id`,
lecture du canal, dépôt dans le bucket) sont injectées.
"""
from __future__ import annotations

import pathlib
import unittest

from core import media_repair
from database import media_store

ROW = {
    "id": "m1",
    "storage_path": "telegram/@signals/2-b.jpg",
    "chat_id": "@signals",
    "message_id": 2,
    "telegram_file_id": "AgAC-2",
    "mime_type": "image/jpeg",
    "file_size": 6,
}


class RouteSelectionTest(unittest.TestCase):
    """Par où les octets peuvent revenir — annoncé avant toute tentative."""

    def test_a_file_id_comes_first(self):
        self.assertEqual(media_repair.repair_route(ROW), media_repair.ROUTE_FILE_ID)

    def test_a_public_channel_is_the_second_route(self):
        row = {**ROW, "telegram_file_id": None}
        self.assertEqual(media_repair.repair_route(row), media_repair.ROUTE_CHANNEL)

    def test_a_scraper_row_has_a_channel_and_no_file_id(self):
        """C'est le cas du scraper : il n'a jamais eu de `file_id` à recevoir."""
        row = dict(ROW, telegram_file_id=None, chat_id="thehalalwinningteam")
        self.assertEqual(media_repair.channel_of(row), "thehalalwinningteam")
        self.assertEqual(media_repair.repair_route(row), media_repair.ROUTE_CHANNEL)

    def test_a_numeric_chat_id_is_not_a_public_channel(self):
        """`t.me/s/<id>` n'existe pas : l'annoncer réparable serait un mensonge."""
        row = {**ROW, "telegram_file_id": None, "chat_id": "-1001234567890"}
        self.assertIsNone(media_repair.channel_of(row))
        self.assertIsNone(media_repair.repair_route(row))

    def test_the_at_sign_is_stripped(self):
        self.assertEqual(media_repair.channel_of({**ROW, "chat_id": "@signals"}), "signals")

    def test_a_row_without_message_id_cannot_be_scraped(self):
        row = {**ROW, "telegram_file_id": None, "message_id": None}
        self.assertIsNone(media_repair.repair_route(row))

    def test_nothing_at_all_is_no_route(self):
        self.assertIsNone(media_repair.repair_route({}))
        self.assertIsNone(media_repair.repair_route(None))


class _Recorder:
    """Doublures des trois frontières, avec ce qui a été demandé."""

    def __init__(self, *, present=False, file_bytes=b"octets", scraped=None, fail_exists=None):
        self.present = present
        self.file_bytes = file_bytes
        self.scraped = scraped
        self.fail_exists = fail_exists
        self.restored: list = []
        self.file_ids: list = []
        self.channels: list = []

    async def download_file_id(self, file_id):
        self.file_ids.append(file_id)
        if isinstance(self.file_bytes, Exception):
            raise self.file_bytes
        return self.file_bytes

    async def fetch_message(self, channel, message_id, *, max_bytes=None):
        self.channels.append((channel, message_id, max_bytes))
        return self.scraped

    def restore(self, path, data, *, mime_type=None, upsert=False):
        self.restored.append((path, bytes(data), mime_type, upsert))
        return len(data)

    def exists(self, path):
        if self.fail_exists is not None:
            raise self.fail_exists
        return self.present


async def _none():
    """Doublure asynchrone qui ne trouve rien dans l'aperçu public."""
    return None


def _fingerprint_of_octets() -> dict:
    """`metadata` d'une ligne dont l'ingestion a vu `b"octets"`."""
    return {media_store.FINGERPRINT_KEY: media_store.fingerprint(b"octets")}


async def _repair(recorder, row=None, **kwargs):
    return await media_repair.repair_media(
        dict(row or ROW),
        download_file_id=recorder.download_file_id,
        fetch_message=recorder.fetch_message,
        restore=recorder.restore,
        exists=recorder.exists,
        **kwargs,
    )


class RepairMediaTest(unittest.IsolatedAsyncioTestCase):
    """Une ligne, les deux voies, et ce qui est refusé."""

    async def test_the_file_id_route_restores_at_the_same_path(self):
        recorder = _Recorder()
        result = await _repair(recorder)
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcome"], media_repair.OUTCOME_RESTORED)
        self.assertEqual(result["route"], media_repair.ROUTE_FILE_ID)
        self.assertEqual(result["bytes"], 6)
        self.assertTrue(result["size_matches"])
        self.assertEqual(recorder.file_ids, ["AgAC-2"])
        self.assertEqual(recorder.channels, [], "l'aperçu public n'est pas sollicité")
        path, data, mime, upsert = recorder.restored[0]
        self.assertEqual((path, data), ("telegram/@signals/2-b.jpg", b"octets"))
        self.assertEqual(mime, "image/jpeg")
        self.assertTrue(upsert)
        self.assertEqual(result["reason"], None)

    async def test_a_refused_file_id_falls_back_to_the_channel_preview(self):
        """Le cas du `file_id` périmé : l'aperçu public reste une chance."""
        recorder = _Recorder(file_bytes=RuntimeError("file_id inconnu"))
        recorder.scraped = {"data": b"apercu", "mime_type": "image/jpeg"}
        result = await _repair(recorder)
        self.assertTrue(result["ok"])
        self.assertEqual(result["route"], media_repair.ROUTE_CHANNEL)
        self.assertEqual(result["bytes"], 6)
        self.assertEqual(recorder.channels, [("signals", 2, media_repair.telegram_channel.MAX_MEDIA_BYTES)])
        self.assertEqual(len(result["notes"]), 1, "la tentative refusée reste lisible")
        self.assertIn("file_id inconnu", result["notes"][0])

    async def test_a_scraper_row_never_tries_the_file_id(self):
        row = {**ROW, "telegram_file_id": None}
        recorder = _Recorder(scraped={"data": b"apercu"})
        result = await _repair(recorder, row=row)
        self.assertTrue(result["ok"])
        self.assertEqual(result["route"], media_repair.ROUTE_CHANNEL)
        self.assertEqual(recorder.file_ids, [], "aucun identifiant à demander au bot")

    async def test_both_attempts_are_named_when_nothing_works(self):
        recorder = _Recorder(file_bytes=RuntimeError("quota"))
        recorder.scraped = None
        result = await _repair(recorder)
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], media_repair.OUTCOME_FAILED)
        self.assertIn("quota", result["reason"])
        self.assertIn("introuvable dans l'aperçu", result["reason"])

    async def test_a_row_with_no_route_is_skipped_not_failed(self):
        """Un échec se rejoue ; un « non réparable » ne changera pas tout seul."""
        row = {"id": "m9", "storage_path": "x/y.jpg", "chat_id": "-100", "message_id": 3}
        recorder = _Recorder()
        result = await _repair(recorder, row=row)
        self.assertEqual(result["outcome"], media_repair.OUTCOME_SKIPPED)
        self.assertEqual(result["reason"], media_repair.NO_ROUTE)
        self.assertEqual(recorder.restored, [])

    async def test_a_row_without_storage_path_is_skipped(self):
        recorder = _Recorder()
        result = await _repair(recorder, row={"id": "m1"})
        self.assertEqual(result["outcome"], media_repair.OUTCOME_SKIPPED)
        self.assertEqual(recorder.file_ids, [], "rien n'a été tenté")

    # -- ce qui protège les données ----------------------------------------- #

    async def test_an_object_already_present_is_never_overwritten(self):
        """La copie de l'aperçu peut être plus petite : ne pas écraser un original."""
        recorder = _Recorder(present=True)
        result = await _repair(recorder)
        self.assertEqual(result["outcome"], media_repair.OUTCOME_SKIPPED)
        self.assertIn("déjà présent", result["reason"])
        self.assertEqual(recorder.file_ids, [], "aucun téléchargement pour rien")
        self.assertEqual(recorder.restored, [])

    async def test_an_undecidable_presence_check_refuses_to_write(self):
        """Ne pas savoir, c'est ne pas écraser : on s'abstient et on le dit."""
        recorder = _Recorder(fail_exists=RuntimeError("Storage injoignable"))
        result = await _repair(recorder)
        self.assertEqual(result["outcome"], media_repair.OUTCOME_FAILED)
        self.assertIn("présence indéterminée", result["reason"])
        self.assertEqual(recorder.restored, [])

    async def test_a_refused_deposit_is_reported_not_raised(self):
        class _Refusing(_Recorder):
            def restore(self, path, data, *, mime_type=None, upsert=False):
                raise RuntimeError("bucket en lecture seule")

        result = await _repair(_Refusing())
        self.assertEqual(result["outcome"], media_repair.OUTCOME_FAILED)
        self.assertIn("dépôt impossible", result["reason"])

    async def test_a_smaller_preview_copy_is_reported_but_accepted(self):
        """L'aperçu peut rendre une copie réduite : on le signale, on n'échoue pas."""
        recorder = _Recorder(file_bytes=b"court")
        result = await _repair(recorder)
        self.assertTrue(result["ok"])
        self.assertFalse(result["size_matches"], "6 octets annoncés, 5 restaurés")

    async def test_an_unknown_recorded_size_is_not_a_mismatch(self):
        recorder = _Recorder()
        result = await _repair(recorder, row={**ROW, "file_size": None})
        self.assertIsNone(result["size_matches"])

    # -- l'empreinte : la preuve que c'est le même fichier ------------------- #

    async def test_the_returning_bytes_are_compared_to_the_recorded_fingerprint(self):
        """`size_matches` autorise ; l'empreinte, elle, prouve."""
        row = {**ROW, "metadata": {media_store.FINGERPRINT_KEY: media_store.fingerprint(b"octets")}}
        result = await _repair(_Recorder(), row=row)
        self.assertTrue(result["ok"])
        self.assertTrue(result["fingerprint_matches"])

    async def test_a_different_file_of_the_same_size_is_named_as_such(self):
        """Le cas qui justifie l'empreinte : 6 octets annoncés, 6 reçus, et ce
        n'est pas le même fichier — l'aperçu public sert volontiers une copie
        retaillée, qui peut tomber juste sur la taille."""
        row = {**ROW, "metadata": {media_store.FINGERPRINT_KEY: media_store.fingerprint(b"octets")}}
        recorder = _Recorder(file_bytes=RuntimeError("file_id refusé"))
        recorder.scraped = {"data": b"autre!"}
        result = await _repair(recorder, row=row)
        self.assertEqual(result["route"], media_repair.ROUTE_CHANNEL)
        self.assertTrue(result["ok"], "un autre fichier reste mieux que rien")
        self.assertTrue(result["size_matches"], "les deux font 6 octets")
        self.assertFalse(result["fingerprint_matches"])

    async def test_without_a_recorded_fingerprint_nothing_is_proved(self):
        """Média antérieur au champ : « on ne sait pas », et pas un succès."""
        result = await _repair(_Recorder())
        self.assertTrue(result["ok"])
        self.assertIsNone(result["fingerprint_matches"])

    async def test_a_failed_row_proves_nothing_about_its_bytes(self):
        recorder = _Recorder(file_bytes=RuntimeError("quota"))
        result = await _repair(recorder)
        self.assertIsNone(result["fingerprint_matches"])

    async def test_the_scraped_mime_type_wins(self):
        """Le type annoncé par le CDN décrit le fichier réellement téléchargé."""
        row = {**ROW, "telegram_file_id": None, "mime_type": "image/jpeg"}
        recorder = _Recorder(scraped={"data": b"apercu", "mime_type": "image/webp"})
        await _repair(recorder, row=row)
        self.assertEqual(recorder.restored[0][2], "image/webp")

    async def test_the_row_is_never_rewritten(self):
        """Le seul chemin qui réécrirait `metadata` n'est même pas connu du module."""
        source = pathlib.Path(media_repair.__file__).read_text(encoding="utf-8")
        self.assertNotIn("upload_media", source)
        self.assertIn("restore_object", source)


class RepairBatchTest(unittest.IsolatedAsyncioTestCase):
    """Un lot : les trois issues comptées, et un échec qui n'arrête rien."""

    async def test_the_three_outcomes_are_counted_apart(self):
        """Réparé, échoué, ignoré : trois suites différentes, trois comptes."""
        rows = [
            {**ROW, "id": "ok", "storage_path": "p/ok.jpg"},
            # Un `file_id` que Telegram refuse, et pas de canal public derrière.
            {**ROW, "id": "ko", "storage_path": "p/ko.jpg", "chat_id": "-100"},
            {
                **ROW,
                "id": "nope",
                "storage_path": "p/nope.jpg",
                "telegram_file_id": None,
                "chat_id": "-100",
            },
            {**ROW, "id": "present", "storage_path": "p/present.jpg"},
        ]
        state = {"n": 0}

        async def download_file_id(file_id):
            state["n"] += 1
            if state["n"] == 1:
                return b"octets"
            raise RuntimeError("refusé")

        report = await media_repair.repair_missing(
            rows,
            download_file_id=download_file_id,
            fetch_message=lambda channel, message_id, *, max_bytes=None: _none(),
            restore=lambda path, data, **kwargs: len(data),
            exists=lambda path: path == "p/present.jpg",
        )
        self.assertEqual(report["requested"], 4)
        self.assertEqual(report["restored"], 1, "un média récupéré par son `file_id`")
        self.assertEqual(report["failed"], 1, "un identifiant refusé, aucune autre voie")
        self.assertEqual(report["skipped"], 2, "un objet toujours là, et une ligne sans voie")
        self.assertEqual(
            [r["outcome"] for r in report["results"]],
            ["restored", "failed", "skipped", "skipped"],
        )
        self.assertEqual(report["results"][2]["reason"], media_repair.NO_ROUTE)
        self.assertIn("déjà présent", report["results"][3]["reason"])

    async def test_proved_and_diverged_restorations_are_counted_apart(self):
        """`verified` et `diverged` ne se partagent pas `restored` : ce qui reste
        est l'ignorance (aucune empreinte enregistrée), et la compter comme une
        preuve ferait passer un média jamais vérifié pour un média vérifié."""
        rows = [
            {**ROW, "id": "same", "storage_path": "p/same.jpg", "metadata": _fingerprint_of_octets()},
            {**ROW, "id": "other", "storage_path": "p/other.jpg", "metadata": _fingerprint_of_octets()},
            {**ROW, "id": "legacy", "storage_path": "p/legacy.jpg"},
        ]
        served = [b"octets", b"autre!", b"octets"]
        state = {"n": 0}

        async def download_file_id(file_id):
            data = served[state["n"]]
            state["n"] += 1
            return data

        report = await media_repair.repair_missing(
            rows,
            download_file_id=download_file_id,
            fetch_message=lambda channel, message_id, *, max_bytes=None: _none(),
            restore=lambda path, data, **kwargs: len(data),
            exists=lambda path: False,
        )
        self.assertEqual(report["restored"], 3)
        self.assertEqual(report["verified"], 1, "un fichier d'origine revenu")
        self.assertEqual(report["diverged"], 1, "un autre fichier, de même taille")
        self.assertLess(
            report["verified"] + report["diverged"],
            report["restored"],
            "le troisième n'avait aucune empreinte : ni preuve, ni divergence",
        )
        self.assertEqual(
            [r["fingerprint_matches"] for r in report["results"]],
            [True, False, None],
        )

    async def test_a_failed_row_does_not_stop_the_next_one(self):
        rows = [{**ROW, "id": "ko"}, {**ROW, "id": "ok"}]

        seen: list = []

        async def download_file_id(file_id):
            seen.append(file_id)
            if len(seen) == 1:
                raise RuntimeError("premier refusé")
            return b"octets"

        async def fetch_message(channel, message_id, *, max_bytes=None):
            return None

        report = await media_repair.repair_missing(
            rows,
            download_file_id=download_file_id,
            fetch_message=fetch_message,
            restore=lambda path, data, **kwargs: len(data),
            exists=lambda path: False,
        )
        self.assertEqual(report["requested"], 2)
        self.assertEqual(report["restored"], 1)
        self.assertEqual(report["failed"], 1)
        self.assertEqual(report["skipped"], 0)
        self.assertEqual([r["outcome"] for r in report["results"]], ["failed", "restored"])

    async def test_a_row_without_route_counts_as_skipped(self):
        rows = [{"id": "m9", "storage_path": "x/y.jpg", "chat_id": "-100", "message_id": 3}]
        report = await media_repair.repair_missing(
            rows,
            restore=lambda path, data, **kwargs: len(data),
            exists=lambda path: False,
        )
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(report["results"][0]["reason"], media_repair.NO_ROUTE)

    async def test_an_empty_batch_is_not_an_error(self):
        report = await media_repair.repair_missing([])
        self.assertEqual(report["requested"], 0)
        self.assertEqual(report["results"], [])


if __name__ == "__main__":
    unittest.main()
