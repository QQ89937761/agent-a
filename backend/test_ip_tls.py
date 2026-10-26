"""Offline checks for certificate scoping, unified endpoint and nginx rollback."""
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from deploy import configure_ip_tls as tls
from backend.core import cloud_deploy
from backend.core.connection_string import make_connection_string, parse_connection_string


class IPTLSTests(unittest.TestCase):
    def test_public_address_and_ports_are_validated(self):
        self.assertEqual(("8.8.8.8", 8443), tls.endpoint("8.8.8.8", 8443))
        for host, port in (("127.0.0.1", 8443), ("192.168.1.2", 8443),
                           ("example.com", 8443), ("8.8.8.8", 80), ("8.8.8.8", 65536)):
            with self.subTest(host=host, port=port), self.assertRaises(ValueError):
                tls.endpoint(host, port)

    def test_ca_issuance_is_isolated_and_uses_shortlived_ip_profile(self):
        production = tls.certbot_command("8.8.8.8")
        staging = tls.certbot_command("8.8.8.8", staging=True)
        self.assertNotIn("--staging", production)
        self.assertIn("--staging", staging)
        self.assertEqual("shortlived", production[production.index("--preferred-profile") + 1])
        self.assertEqual("8.8.8.8", production[production.index("--ip-address") + 1])
        self.assertNotEqual(production[production.index("--config-dir") + 1],
                            staging[staging.index("--config-dir") + 1])
        self.assertNotIn("/etc/letsencrypt", " ".join(production))

    def test_gateway_serves_worker_and_browser_on_one_public_port(self):
        text = tls.tls_config("8.8.8.8", 8451, 8452)
        self.assertIn("listen 8452 ssl;", text)
        self.assertIn("proxy_pass https://127.0.0.1:8451;", text)
        self.assertNotIn("location = /worker", text)
        self.assertIn("proxy_ssl_verify on;", text)
        self.assertIn("proxy_set_header X-Forwarded-For $remote_addr;", text)
        self.assertIn("proxy_set_header Host $http_host;", text)
        self.assertNotIn("hub.key", text)
        challenge = tls.http_config("8.8.8.8")
        self.assertIn("listen 80;", challenge)
        self.assertIn("location / { return 404; }", challenge)

    def test_bad_nginx_config_restores_original(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "xiaoa.conf"
            config.write_text("old config")
            with patch.object(tls, "run", side_effect=[RuntimeError("invalid"), "", ""]):
                with self.assertRaisesRegex(RuntimeError, "invalid"):
                    tls.nginx_config(config, "new config")
            self.assertEqual("old config", config.read_text())

    def test_public_port_prefers_1115_and_skips_occupied_or_internal_ports(self):
        with patch.object(tls.socket, "socket") as factory, \
                patch.object(tls, "HTTPS", Path("nonexistent-ip-nginx.conf")):
            sock = factory.return_value.__enter__.return_value
            self.assertEqual(1115, tls.select_port(8443))
            sock.bind.assert_called_once_with(("0.0.0.0", 1115))
            sock.bind.reset_mock()
            sock.bind.side_effect = [OSError("occupied"), None]
            self.assertEqual(1116, tls.select_port(8443))
            self.assertEqual([(("0.0.0.0", 1115),), (("0.0.0.0", 1116),)],
                             [call.args for call in sock.bind.call_args_list])
            sock.bind.side_effect = None
            sock.bind.reset_mock()
            self.assertEqual(1116, tls.select_port(1115))
            sock.bind.assert_called_once_with(("0.0.0.0", 1116))

    def test_repeat_deployment_reuses_selected_port_only_for_managed_nginx(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "xiaoa.conf"
            config.write_text(tls.tls_config("8.8.8.8", 8443, 1116))
            with patch.object(tls, "HTTPS", config), patch.object(tls.socket, "socket") as factory:
                sock = factory.return_value.__enter__.return_value
                sock.bind.side_effect = OSError("occupied")
                self.assertEqual(1116, tls.select_port(8443, 1116))
                sock.bind.assert_called_once_with(("0.0.0.0", 1116))
                config.write_text("# Another application\nlisten 1116 ssl;")
                sock.bind.side_effect = [OSError("occupied"), None]
                self.assertEqual(1115, tls.select_port(8443, 1116))

    def test_credentials_share_browser_port_with_ca_validated_worker(self):
        connection = make_connection_string("8.8.8.8", 8444, "", "t" * 32)
        raw = f"XIAOA_CONNECTION_STRING={connection}\nAGENT_WEB_PASSWORD={'w' * 32}\n"
        value = cloud_deploy._parse_credentials(raw + "XIAOA_PUBLIC_WEB_URL=https://8.8.8.8:8444")
        self.assertEqual(8444, parse_connection_string(value["connection_string"]).port)
        with tempfile.TemporaryDirectory() as temp:
            cloud_deploy._save_local_credentials(temp, value, "8.8.8.8", "root")
            saved = json.loads((Path(temp) / "data/cloud-ssh/target.json").read_text())
        self.assertEqual("https://8.8.8.8:8444", saved["web_url"])
        for url in ("https://other.example:8444", "http://8.8.8.8:8444", "https://8.8.8.8:8445",
                    "https://user:pass@8.8.8.8:8444", "https://8.8.8.8:8444/path"):
            with self.subTest(url=url), self.assertRaises(Exception):
                cloud_deploy._parse_credentials(raw + "XIAOA_PUBLIC_WEB_URL=" + url)


class RenewedCertificateTests(unittest.TestCase):
    def test_ca_accepts_renewed_ip_certificates_and_rejects_untrusted_or_wrong_ip(self):
        from datetime import datetime, timedelta, timezone
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import ipaddress
        import ssl
        import threading
        import urllib.request
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        from backend.core.bridge import hub_ssl_context

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test CA")])
        now = datetime.now(timezone.utc)
        ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
              .public_key(key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=2))
              .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
              .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
              .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
              .sign(key, hashes.SHA256()))

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'ok')

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ca_path = root / 'ca.pem'
            ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
            context = hub_ssl_context()
            context.load_verify_locations(cafile=str(ca_path))
            for address, days, should_pass in [('127.0.0.1', 1, True), ('127.0.0.1', 2, True),
                                               ('127.0.0.2', 1, False), ('127.0.0.1', -1, False)]:
                # Renewal changes both key and certificate, with no saved leaf pin.
                leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test leaf")])
                leaf = (x509.CertificateBuilder().subject_name(leaf_name).issuer_name(name)
                        .public_key(leaf_key.public_key()).serial_number(x509.random_serial_number())
                        .not_valid_before(now - timedelta(days=2)).not_valid_after(now + timedelta(days=days))
                        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]), False)
                        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False)
                        .sign(key, hashes.SHA256()))
                (root / 'leaf.pem').write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
                (root / 'leaf.key').write_bytes(leaf_key.private_bytes(
                    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                server_context.load_cert_chain(root / 'leaf.pem', root / 'leaf.key')
                server.socket = server_context.wrap_socket(server.socket, server_side=True)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                         urllib.request.HTTPSHandler(context=context))
                    url = f'https://127.0.0.1:{server.server_port}/'
                    if should_pass:
                        with opener.open(url, timeout=3) as response:
                            self.assertEqual(b'ok', response.read())
                        untrusted = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                            urllib.request.HTTPSHandler(context=hub_ssl_context()))
                        with self.assertRaises(OSError):
                            untrusted.open(url, timeout=3)
                    else:
                        with self.assertRaises(OSError):
                            opener.open(url, timeout=3)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join()


if __name__ == "__main__":
    unittest.main()
