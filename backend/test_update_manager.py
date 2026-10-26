import hashlib
import io
import json
import re
import os
import subprocess
import tempfile
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
import unittest

from backend.core import update_manager


class ReleaseManifestTests(unittest.TestCase):
    def setUp(self):
        self.repository_patch = patch.object(update_manager, "UPDATE_REPOSITORY", "test-owner/xiaoa")
        self.repository_patch.start()
        self.addCleanup(self.repository_patch.stop)

    def release(self, *, digest=None, url=None, tag="v1.2.3", release_url=None, **overrides):
        digest = digest or "a" * 64
        tag_path = update_manager.quote(tag, safe="")
        value = {
            "tag_name": tag,
            "html_url": release_url or f"https://github.com/{update_manager.UPDATE_REPOSITORY}/releases/tag/{tag_path}",
            "draft": False,
            "prerelease": False,
            "body": "Release notes",
            "assets": [{
                "name": "XiaoA.exe",
                "browser_download_url": url or f"https://github.com/{update_manager.UPDATE_REPOSITORY}/releases/download/{tag_path}/XiaoA.exe",
                "digest": f"sha256:{digest}",
            }],
        }
        value.update(overrides)
        return value

    def test_latest_release_and_asset_download_are_unauthenticated_and_hash_verified(self):
        payload = b"portable executable"
        manifest = self.release(digest=hashlib.sha256(payload).hexdigest())
        responses = [io.BytesIO(json.dumps(manifest).encode()), io.BytesIO(payload)]
        responses[1].headers = {"Content-Length": str(len(payload))}
        with patch.object(update_manager.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = responses
            fetched = update_manager.fetch_manifest()
            with tempfile.TemporaryDirectory() as directory:
                downloaded = update_manager.download_asset(fetched["portable"], directory)
                self.assertEqual(downloaded.read_bytes(), payload)
            self.assertEqual(build.return_value.open.call_count, 2)
            for call in build.return_value.open.call_args_list:
                request = call.args[0]
                self.assertIsNone(request.get_header("Authorization"))
                self.assertEqual(request.get_header("User-agent"), "XiaoA-Updater/1")
            self.assertEqual(fetched["release_url"], manifest["html_url"])
            self.assertEqual(fetched["version"], "1.2.3")

    def test_unconfigured_repository_returns_no_update_without_network_access(self):
        with patch.object(update_manager, "UPDATE_REPOSITORY", ""), \
                patch.object(update_manager.urllib.request, "build_opener") as build:
            self.assertIsNone(update_manager.fetch_manifest())
            build.assert_not_called()

    def test_unreleased_repository_returns_no_update(self):
        error = urllib.error.HTTPError(update_manager._update_api_url(), 404, "Not Found", {}, None)
        with patch.object(update_manager.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = error
            self.assertIsNone(update_manager.fetch_manifest())

    def test_only_exact_public_repository_release_urls_are_accepted(self):
        valid = self.release()
        self.assertEqual(update_manager.validate_manifest(valid)["version"], "1.2.3")
        for url in (
            "https://example.com/downloads/XiaoA.exe",
            f"https://github.com/{update_manager.UPDATE_REPOSITORY}-evil/releases/download/v1.2.3/XiaoA.exe",
            f"https://user@github.com/{update_manager.UPDATE_REPOSITORY}/releases/download/v1.2.3/XiaoA.exe",
        ):
            with self.subTest(url=url), self.assertRaises(update_manager.ReleaseError):
                update_manager.validate_manifest(self.release(url=url))
        with self.assertRaises(update_manager.ReleaseError):
            update_manager.validate_manifest(self.release(release_url="https://example.com/release"))

    def test_bad_digest_tags_missing_assets_and_prereleases_are_rejected(self):
        for value in (
            self.release(digest="not-a-sha256"),
            self.release(assets=[]),
            self.release(tag="v1.2.3/evil"),
            self.release(prerelease=True),
            self.release(draft=True),
        ):
            with self.subTest(value=value), self.assertRaises(update_manager.ReleaseError):
                update_manager.validate_manifest(value)

    def test_manifest_redirects_allow_only_https_github_asset_hosts(self):
        opener = update_manager._NoUpdateRedirect()
        request = urllib.request.Request("https://github.com/test-owner/xiaoa/releases/download/v1.2.3/XiaoA.exe")
        allowed = opener.redirect_request(request, io.BytesIO(), 302, "Found", {},
                                          "https://release-assets.githubusercontent.com/file.exe")
        self.assertIsNotNone(allowed)
        for url in ("http://github.com/file", "https://example.com/file",
                    "https://user@github.com/file", "https://github.com:8443/file"):
            with self.subTest(url=url), self.assertRaises(update_manager.ReleaseError):
                opener.redirect_request(request, io.BytesIO(), 302, "Found", {}, url)

    def test_untrusted_initial_url_is_rejected_before_network_access(self):
        for url in ("https://example.com/latest.json", "http://api.github.com/path",
                    update_manager._update_api_url() + "?redirect=https://example.com"):
            with self.subTest(url=url), patch.object(update_manager.urllib.request, "build_opener") as build:
                with self.assertRaises(update_manager.ReleaseError):
                    update_manager._open_update_url(url, 1)
                build.assert_not_called()

    def test_redirect_never_reaches_untrusted_host(self):
        opener = update_manager._NoUpdateRedirect()
        with self.assertRaises(update_manager.ReleaseError):
            opener.redirect_request(urllib.request.Request("https://github.com/test-owner/xiaoa/releases/download/v1.2.3/XiaoA.exe"),
                                    io.BytesIO(), 302, "Found", {}, "http://example.com/destination")

    def test_version_comparison_and_manifest_validation(self):
        self.assertGreater(update_manager.version_key("v1.2.3"), update_manager.version_key("1.2.2"))
        self.assertEqual("v1.2.3", update_manager.validate_manifest(self.release())["tag"])

    @unittest.skipUnless(os.name == "nt", "Windows update helper")
    def test_update_helper_does_not_bind_readonly_powershell_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "XiaoA.exe"
            target.write_bytes(b"old")
            downloaded = Path(directory) / "new.exe"
            downloaded.write_bytes(b"new")
            with patch.object(update_manager.tempfile, "mkdtemp", return_value=directory), \
                    patch.object(update_manager, "portable_update_context", return_value=(Path(directory), downloaded)), \
                    patch.object(update_manager.subprocess, "Popen"):
                update_manager.prepare_portable_update({"version": "1.0.0"}, executable=target,
                                                       parent_pid=0, backend_pid=0)
            helper = (Path(directory) / "install-update.ps1").read_text(encoding="utf-8")
            function = re.search(r"function Stop-IfAlive.*?^}", helper, re.S | re.M).group()
            result = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                                     "$ErrorActionPreference='Stop'; " + function + "; Stop-IfAlive 0"],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_version_comparison_accepts_prerelease_suffix_without_path_components(self):
        self.assertEqual((1, 2, 3), update_manager.version_key("1.2.3-rc.1"))
        with self.assertRaises(update_manager.ReleaseError):
            update_manager.version_key("1.2.3/other")


if __name__ == "__main__":
    unittest.main()
