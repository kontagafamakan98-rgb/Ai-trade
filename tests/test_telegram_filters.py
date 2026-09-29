"""Tests des filtres de routage des mises à jour média.

Ce fichier est le seul des tests média à importer `python-telegram-bot` : il
vérifie la **règle de routage** elle-même, qui ne peut l'être autrement —
`filters.ChatType.CHANNEL` et `filters.Document.ALL` sont des objets de la
bibliothèque, pas du code à nous. Le reste de l'ingestion se teste sans elle
(`tests/test_telegram_media.py`), et ces tests-ci se **sautent** proprement quand
la dépendance est absente : un contrôle non exécuté se dit, il ne passe pas au
vert silencieusement.

Deux propriétés sont vérifiées ici, et les deux casseraient en production :

* une **publication de canal** ne doit être prise que par la route canal. Les
  handlers de chat répondent dans le chat où le média a été envoyé : si un
  `channel_post` leur arrivait, la réponse se publierait dans le canal, devant
  tous les abonnés ;
* les noms de filtres référencés par `main.py` doivent **exister**. `filters.DOCUMENT`
  a disparu avec python-telegram-bot 22 (remplacé par `filters.Document.ALL`) et
  un nom périmé ne se voit qu'au démarrage du bot — c'est-à-dire en production,
  où `main.py` n'est pas importé par les tests.
"""
from __future__ import annotations

import pathlib
import re
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

try:  # pragma: no cover - dépend de l'environnement
    from telegram import Chat, Message, PhotoSize, Update

    from notifications import telegram_filters

    PTB_AVAILABLE = True
except Exception:  # pragma: no cover - dépend de l'environnement
    PTB_AVAILABLE = False

MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"


@unittest.skipUnless(PTB_AVAILABLE, "python-telegram-bot absent")
class ReferencedFilterTest(unittest.TestCase):
    """Les filtres nommés dans `main.py` existent-ils vraiment ?"""

    def test_every_referenced_filter_exists(self):
        names = set(re.findall(r"telegram_filters\.([A-Z_]+)", MAIN.read_text(encoding="utf-8")))
        self.assertTrue(names, "aucun filtre référencé : le test ne vérifierait rien")
        for name in sorted(names):
            with self.subTest(filter=name):
                self.assertTrue(hasattr(telegram_filters, name), f"{name} n'existe pas")

    def test_every_direct_route_covers_at_least_one_media_type(self):
        for name in ("DIRECT_PHOTO", "DIRECT_VIDEO", "DIRECT_DOCUMENT", "DIRECT_VOICE", "DIRECT_AUDIO"):
            with self.subTest(filter=name):
                self.assertTrue(hasattr(telegram_filters, name))


@unittest.skipUnless(PTB_AVAILABLE, "python-telegram-bot absent")
class ChannelMediaFilterTest(unittest.TestCase):
    """Le routage, avec de vrais objets `python-telegram-bot`."""

    def _photo(self):
        return [PhotoSize(file_id="f", file_unique_id="u", width=10, height=10)]

    def _channel_post(self, **overrides):
        return Update(
            update_id=1,
            channel_post=Message(
                message_id=42,
                date=datetime.now(timezone.utc),
                chat=Chat(id=-1001234567890, type="channel", title="Canal", username="canal"),
                caption="BTC",
                photo=self._photo(),
                **overrides,
            ),
        )

    def _direct(self, *, chat_type="private", **overrides):
        return Update(
            update_id=2,
            message=Message(
                message_id=1,
                date=datetime.now(timezone.utc),
                chat=Chat(id=42, type=chat_type),
                photo=self._photo(),
                **overrides,
            ),
        )

    def _document_update(self, *, channel: bool):
        document = SimpleNamespace(
            file_id="d", file_unique_id="u", file_name="rapport.pdf", file_size=10
        )
        message = Message(
            message_id=43,
            date=datetime.now(timezone.utc),
            chat=Chat(
                id=-1001234567890 if channel else 42,
                type="channel" if channel else "private",
                title="Canal" if channel else None,
            ),
            document=document,
        )
        return Update(update_id=3, **({"channel_post": message} if channel else {"message": message}))

    def test_a_channel_photo_is_taken_by_the_channel_route(self):
        self.assertTrue(telegram_filters.CHANNEL_MEDIA.check_update(self._channel_post()))

    def test_a_channel_document_is_taken_by_the_channel_route(self):
        """Le cas qui justifie la route : le scraper public n'a jamais le fichier."""
        self.assertTrue(telegram_filters.CHANNEL_MEDIA.check_update(self._document_update(channel=True)))

    def test_a_channel_post_is_never_taken_by_a_direct_route(self):
        for name in (
            "DIRECT_PHOTO",
            "DIRECT_VIDEO",
            "DIRECT_DOCUMENT",
            "DIRECT_VOICE",
            "DIRECT_AUDIO",
        ):
            with self.subTest(filter=name):
                self.assertFalse(getattr(telegram_filters, name).check_update(self._channel_post()))

    def test_a_direct_media_is_never_taken_by_the_channel_route(self):
        for chat_type in ("private", "group", "supergroup"):
            with self.subTest(chat=chat_type):
                self.assertFalse(telegram_filters.CHANNEL_MEDIA.check_update(self._direct(chat_type=chat_type)))

    def test_groups_keep_being_ingested(self):
        """La route directe doit rester ce qu'elle était : hors canal, pas privé."""
        self.assertTrue(telegram_filters.DIRECT_PHOTO.check_update(self._direct(chat_type="supergroup")))

    def test_a_document_from_a_chat_is_taken_by_the_direct_route(self):
        self.assertTrue(telegram_filters.DIRECT_DOCUMENT.check_update(self._document_update(channel=False)))

    def test_a_text_message_is_taken_by_nobody(self):
        """Un texte de canal ne se télécharge pas : aucun handler média ne doit le prendre."""
        update = Update(
            update_id=4,
            channel_post=Message(
                message_id=44,
                date=datetime.now(timezone.utc),
                chat=Chat(id=-1001234567890, type="channel", title="Canal"),
                text="hello",
            ),
        )
        self.assertFalse(telegram_filters.CHANNEL_MEDIA.check_update(update))


if __name__ == "__main__":
    unittest.main()
