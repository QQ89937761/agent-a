"""Regression coverage for legacy Feishu credential migration."""
import unittest
import tomllib

from backend.core.config import scrub_feishu_secret


class ConfigMigrationTests(unittest.TestCase):
    def test_secret_migration_preserves_remaining_configuration(self):
        original = '[llm]\napi_key = "model-key"\n[feishu]\napp_id = "app"\napp_secret = "legacy-secret"\nallowed_open_ids = ["ou_owner"]\n[auth]\npassword = "keep"\n[role]\nmode = "standalone"\n'
        expected = tomllib.loads(original)
        expected["feishu"]["app_secret"] = ""
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                result = scrub_feishu_secret(original.replace("\n", newline))
                self.assertEqual(expected, tomllib.loads(result))
                self.assertNotIn("legacy-secret", result)
        without_secret = '[feishu]\napp_id = "app"\n[other]\napp_secret = "keep"\n'
        self.assertEqual(without_secret, scrub_feishu_secret(without_secret))
