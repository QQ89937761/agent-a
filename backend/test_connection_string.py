from __future__ import annotations

import hashlib
import unittest
import ssl
from types import SimpleNamespace

from backend.core.bridge import verify_peer_fingerprint, hub_ssl_context
from backend.core.connection_string import (
    make_connection_string, masked_preview, parse_connection_string,
)


class ConnectionStringTests(unittest.TestCase):
    def setUp(self):
        self.fp = hashlib.sha256(b"certificate").hexdigest()
        self.token = "token-" + "x" * 31

    def test_round_trip_and_masked_preview(self):
        value = make_connection_string("203.0.113.10", 8443, self.fp, self.token)
        parsed = parse_connection_string(value)
        self.assertEqual("wss://203.0.113.10:8443/worker", parsed.hub_url)
        preview = masked_preview(parsed)
        self.assertEqual("203.0.113.10", preview["host"])
        self.assertNotIn(self.token, str(preview))

    def test_new_connection_uses_ca_validation_without_leaf_pin(self):
        value = make_connection_string("8.8.8.8", 8444, "", self.token)
        self.assertTrue(value.startswith("XIAOA2."))
        details = parse_connection_string(value)
        self.assertEqual("", details.fingerprint)
        self.assertEqual("wss://8.8.8.8:8444/worker", details.hub_url)
        context = hub_ssl_context(details.fingerprint)
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
        self.assertTrue(context.check_hostname)
        self.assertGreater(len(context.get_ca_certs()), 0)
        self.assertEqual(ssl.CERT_NONE, hub_ssl_context(self.fp).verify_mode)

    def test_one_character_change_is_rejected(self):
        value = make_connection_string("hub.example.com", 8443, self.fp, self.token)
        changed = value[:-1] + ("0" if value[-1] != "0" else "1")
        with self.assertRaisesRegex(ValueError, "校验失败"):
            parse_connection_string(changed)

    def test_bad_fingerprint_and_token_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "指纹"):
            make_connection_string("1.2.3.4", 8443, "bad", self.token)
        with self.assertRaisesRegex(ValueError, "口令"):
            make_connection_string("1.2.3.4", 8443, self.fp, "short")

    def test_fingerprint_mismatch_is_rejected(self):
        certificate = b"peer-certificate"
        ssl_object = SimpleNamespace(getpeercert=lambda binary_form=False: certificate)
        ws = SimpleNamespace(transport=SimpleNamespace(
            get_extra_info=lambda name: ssl_object if name == "ssl_object" else None,
        ))
        with self.assertRaisesRegex(Exception, "指纹不匹配"):
            verify_peer_fingerprint(ws, "0" * 64)
        verify_peer_fingerprint(ws, hashlib.sha256(certificate).hexdigest())


if __name__ == "__main__":
    unittest.main()
