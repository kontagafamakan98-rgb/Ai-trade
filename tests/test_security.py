import unittest
import os

from core.security_utils import constant_time_equals
from core import config_runtime


class TestSecurityUtils(unittest.TestCase):

    def test_constant_time_equals_matches(self):
        self.assertTrue(constant_time_equals("abc", "abc"))

    def test_constant_time_equals_rejects_mismatch(self):
        self.assertFalse(constant_time_equals("abc", "abd"))
        self.assertFalse(constant_time_equals("abc", "abc "))

    def test_constant_time_equals_handles_none(self):
        self.assertFalse(constant_time_equals(None, "abc"))
        self.assertFalse(constant_time_equals("abc", None))
        self.assertFalse(constant_time_equals(None, None))


class TestConfigSecurityGating(unittest.TestCase):

    def setUp(self):
        config_runtime.reset_env_config()
        self._saved = {k: os.environ.get(k) for k in (
            "WEBHOOK_SECRET", "INTERNAL_API_KEY",
        )}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        config_runtime.reset_env_config()

    def test_default_webhook_secret_is_flagged(self):
        os.environ.pop("WEBHOOK_SECRET", None)
        os.environ["INTERNAL_API_KEY"] = "a-distinct-internal-key"
        cfg = config_runtime.get_env_config()
        issues = cfg.security_issues()
        self.assertTrue(any("WEBHOOK_SECRET" in i for i in issues))

    def test_missing_internal_key_is_flagged(self):
        os.environ["WEBHOOK_SECRET"] = "a-strong-webhook-secret"
        os.environ.pop("INTERNAL_API_KEY", None)
        cfg = config_runtime.get_env_config()
        self.assertTrue(any("INTERNAL_API_KEY" in i for i in cfg.security_issues()))

    def test_identical_secrets_are_flagged(self):
        os.environ["WEBHOOK_SECRET"] = "same-secret-value"
        os.environ["INTERNAL_API_KEY"] = "same-secret-value"
        cfg = config_runtime.get_env_config()
        self.assertTrue(any("identique" in i for i in cfg.security_issues()))

    def test_secure_config_has_no_issues(self):
        os.environ["WEBHOOK_SECRET"] = "a-strong-webhook-secret"
        os.environ["INTERNAL_API_KEY"] = "a-distinct-internal-key"
        cfg = config_runtime.get_env_config()
        self.assertEqual(cfg.security_issues(), [])

    def test_enforce_secure_config_raises_on_defaults(self):
        os.environ.pop("WEBHOOK_SECRET", None)
        os.environ.pop("INTERNAL_API_KEY", None)
        config_runtime.reset_env_config()
        with self.assertRaises(RuntimeError):
            config_runtime.enforce_secure_config()


if __name__ == "__main__":
    unittest.main()
