"""L'anneau de clés versionnées (`utils/encryption.py`).

Trois propriétés, chacune réparant un défaut qui a coûté cher ailleurs :

* une clé **neuve** n'oblige plus à relire — ou à perdre — l'ancien chiffré : un
  jeton écrit avant la rotation s'ouvre encore après ;
* un jeton **annonce** sa version dans son en-tête, donc « reste-t-il des lignes à
  tourner ? » se lit au lieu de se deviner, et la rotation a une fin observable ;
* un chiffré que l'anneau ne rouvre pas **lève**, avec un message qui nomme la
  version manquante — jamais une clé, jamais un texte clair.

Les clés de ces tests sont fabriquées ici, jamais lues dans le `.env` du dépôt :
un test qui dépend de la clé de la machine ne teste pas la rotation, il teste la
machine.
"""
from __future__ import annotations

import base64
import secrets
import unittest
from unittest import mock

from cryptography.fernet import Fernet, InvalidToken

from utils import encryption as enc


def _key() -> str:
    """Une clé Fernet neuve, jamais réutilisée d'un test à l'autre."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


class TokenHeaderTest(unittest.TestCase):
    """L'en-tête d'un jeton : ce qui rend une rotation observable."""

    def setUp(self):
        self.key = _key()
        self.ring = enc.build_ring(self.key)

    def test_a_token_announces_its_version(self):
        token = self.ring.encrypt("gAAAAA-sans-rapport")

        self.assertEqual(enc.token_version(token), 1)
        self.assertTrue(token.startswith("v1:"))

    def test_the_plaintext_never_appears_in_the_token(self):
        """Un texte court et reconnaissable : s'il ressort, c'est qu'il n'est pas chiffré."""
        token = self.ring.encrypt("cle-api-tres-reconnaissable")

        self.assertNotIn("cle-api-tres-reconnaissable", token)

    def test_a_token_without_header_is_reported_as_legacy(self):
        """Les jetons d'avant l'anneau n'annoncent rien : on doit le dire, pas deviner."""
        self.assertIsNone(enc.token_version("gAAAAABm-jeton-historique"))
        self.assertIsNone(enc.token_version(""))
        self.assertIsNone(enc.token_version("v:pas-un-nombre"))
        self.assertIsNone(enc.token_version("version2:gAAAAA"))


class RotationTest(unittest.TestCase):
    """Rouvrir l'ancien, écrire avec le nouveau — le cœur de la rotation."""

    def setUp(self):
        self.key_v1, self.key_v2 = _key(), _key()
        self.before = enc.build_ring(self.key_v1)
        self.after = enc.build_ring(f"v2:{self.key_v2}", f"v1:{self.key_v1}")

    def test_a_token_written_before_the_rotation_is_still_readable(self):
        token = self.before.encrypt("identifiant-broker")

        self.assertEqual(self.after.decrypt(token), "identifiant-broker")

    def test_the_ring_reports_which_version_a_token_still_uses(self):
        stale, fresh = self.before.encrypt("a"), self.after.encrypt("b")

        self.assertTrue(self.after.needs_rotation(stale))
        self.assertFalse(self.after.needs_rotation(fresh))
        self.assertEqual(self.after.decrypt_with_version(stale)[0], 1)
        self.assertEqual(self.after.decrypt_with_version(fresh)[0], 2)

    def test_only_the_active_key_encrypts(self):
        """Sinon la rotation ne se termine jamais : on réécrirait toujours en v1."""
        for _ in range(5):
            token = self.after.encrypt("identifiant-broker")
            self.assertTrue(token.startswith("v2:"))
            with self.assertRaises(InvalidToken, msg="v1 ne doit plus chiffrer"):
                Fernet(self.key_v1.encode()).decrypt(token.split(":", 1)[1].encode())

    def test_a_legacy_token_is_tried_against_every_key(self):
        legacy = Fernet(self.key_v1.encode()).encrypt(b"identifiant-historique").decode()

        version, plaintext = self.after.decrypt_with_version(legacy)

        self.assertEqual((version, plaintext), (1, "identifiant-historique"))
        self.assertTrue(self.after.needs_rotation(legacy), "sans en-tête, donc à tourner")

    def test_the_ring_order_is_the_version_order(self):
        self.assertEqual(self.after.versions, [2, 1])
        self.assertEqual(self.after.primary_version, 2)
        self.assertEqual(self.after.size, 2)


class ConfigurationTest(unittest.TestCase):
    """Ce qui est accepté, ce qui est refusé — et ce qu'un message a le droit de dire."""

    def test_a_bare_key_is_version_one(self):
        """La forme qu'avaient tous les `.env` d'avant : rien à réécrire pour l'adopter."""
        ring = enc.build_ring(_key())

        self.assertEqual(ring.versions, [1])

    def test_a_prefixed_active_key_names_its_version(self):
        ring = enc.build_ring(f"v7:{_key()}")

        self.assertEqual(ring.primary_version, 7)

    def test_previous_keys_accept_commas_semicolons_and_spaces(self):
        keys = [_key() for _ in range(3)]

        for separator in (",", ";", " ", "\n"):
            with self.subTest(separator=repr(separator)):
                ring = enc.build_ring(
                    f"v4:{_key()}",
                    separator.join(
                        f"v{n}:{key}" for n, key in zip((1, 2, 3), keys, strict=True)
                    ),
                )
                self.assertEqual(ring.versions, [4, 1, 2, 3])

    def test_a_missing_active_key_refuses_to_build(self):
        with self.assertRaises(enc.EncryptionKeyMissing) as caught:
            enc.build_ring("   ")

        self.assertIn("ENCRYPTION_KEY", str(caught.exception))
        self.assertIn("generate_secrets.py", str(caught.exception))

    def test_two_keys_with_the_same_version_refuse_to_build(self):
        with self.assertRaises(enc.EncryptionKeyInvalid) as caught:
            enc.build_ring(f"v2:{_key()}", f"v2:{_key()}")

        self.assertIn("même version", str(caught.exception))

    def test_a_key_without_version_conflicts_with_explicit_v1(self):
        """Une clé nue **est** v1 : la mélanger à un `v1:` rendrait le choix ambigu."""
        with self.assertRaises(enc.EncryptionKeyInvalid):
            enc.build_ring(_key(), f"v1:{_key()}")

    def test_a_bad_version_prefix_is_named_by_position_without_the_key(self):
        secret = _key()

        with self.assertRaises(enc.EncryptionKeyInvalid) as caught:
            enc.build_ring(_key(), f"vX:{secret}")

        message = str(caught.exception)
        self.assertIn("entrée 1", message)
        self.assertIn("préfixe de version", message)
        self.assertNotIn(secret, message)

    def test_version_zero_is_refused(self):
        with self.assertRaises(enc.EncryptionKeyInvalid) as caught:
            enc.build_ring(_key(), f"v0:{_key()}")

        self.assertIn("commence à v1", str(caught.exception))

    def test_a_key_that_is_not_a_fernet_key_is_refused(self):
        bad = base64.urlsafe_b64encode(b"trop-court").decode("ascii")

        with self.assertRaises(enc.EncryptionKeyInvalid) as caught:
            enc.build_ring(_key(), f"v2:{bad}")

        self.assertIn("Fernet", str(caught.exception))
        self.assertNotIn(bad, str(caught.exception), "le message ne recopie pas la clé")

    def test_an_unreadable_token_names_the_missing_version_only(self):
        stale_key, active_key = _key(), _key()
        token = enc.build_ring(stale_key).encrypt("identifiant-broker")

        with self.assertRaises(enc.EncryptionError) as caught:
            enc.build_ring(f"v2:{active_key}").decrypt(token)

        message = str(caught.exception)
        self.assertIn("v1", message)
        self.assertIn("ENCRYPTION_KEYS_PREVIOUS", message)
        self.assertNotIn(stale_key, message)
        self.assertNotIn("identifiant-broker", message, "ni un texte clair")
        self.assertNotIn(token, message, "ni le chiffré lui-même")

    def test_a_token_whose_version_lies_is_refused(self):
        """En-tête v2, chiffré par v1 : la version annoncée n'est pas décorative."""
        v1, v2 = _key(), _key()
        token = enc.build_ring(v1).encrypt("identifiant-broker")

        with self.assertRaises(enc.EncryptionError) as caught:
            enc.build_ring(f"v2:{v2}", f"v1:{v1}").decrypt("v2:" + token.split(":", 1)[1])

        self.assertIn("ne correspondent pas", str(caught.exception))


class ModuleWiringTest(unittest.TestCase):
    """`get_ring()` lit la configuration du module, une fois, comme le reste du code."""

    def setUp(self):
        enc.reset_ring()
        self.addCleanup(enc.reset_ring)
        self.key_v1, self.key_v2 = _key(), _key()

    def _configure(self, primary: str, previous: str = "") -> None:
        mock.patch.object(enc, "ENCRYPTION_KEY", primary).start()
        mock.patch.object(enc, "ENCRYPTION_KEYS_PREVIOUS", previous).start()
        self.addCleanup(mock.patch.stopall)
        enc.reset_ring()

    def test_the_ring_comes_from_the_module_configuration(self):
        self._configure(f"v2:{self.key_v2}", f"v1:{self.key_v1}")

        ring = enc.get_ring()

        self.assertEqual((ring.versions, ring.size), ([2, 1], 2))
        self.assertEqual(ring.primary_version, 2)

    def test_the_ring_is_cached_until_it_is_reset(self):
        """Une variable d'environnement ne change pas à chaud : on ne la relit pas."""
        self._configure(self.key_v1)
        first = enc.get_ring()

        self.assertIs(enc.get_ring(), first)
        enc.reset_ring()
        self.assertIsNot(enc.get_ring(), first)

    def test_a_missing_key_raises_at_first_use_not_at_import(self):
        self._configure("")

        with self.assertRaises(enc.EncryptionKeyMissing):
            enc.get_ring()

    def test_repr_of_a_ring_key_masks_the_key(self):
        """Un `repr` qui traîne dans un journal ne doit pas y laisser la clé."""
        key = _key()

        self.assertNotIn(key, repr(enc.RingKey(version=1, key=key)))


class ConfigSurfaceTest(unittest.TestCase):
    """Le nom de la variable est lu par `config`, jusqu'à `config_runtime`."""

    def test_config_exposes_the_previous_keys_variable(self):
        import config

        self.assertTrue(hasattr(config, "ENCRYPTION_KEYS_PREVIOUS"))

    def test_the_runtime_config_reads_the_environment_variable(self):
        from core.config_runtime import EnvConfig

        with mock.patch.dict(
            "os.environ",
            {"ENCRYPTION_KEYS_PREVIOUS": "v1:abc", "ENCRYPTION_KEY": "v2:def"},
            clear=False,
        ):
            cfg = EnvConfig()

        self.assertEqual(cfg.encryption_keys_previous, "v1:abc")
        self.assertEqual(cfg.encryption_key, "v2:def")


if __name__ == "__main__":
    unittest.main()
