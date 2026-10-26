import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.release_files import release_data_files
from deploy.verify_release import digest, verify_files, verify_frozen_modules
from backend import __version__


class CleanReleaseTests(unittest.TestCase):
    def test_versions_and_download_name_match(self):
        root = Path(__file__).resolve().parent.parent
        package = json.loads((root / "frontend/package.json").read_text(encoding="utf-8"))
        lock = json.loads((root / "frontend/package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(__version__, package["version"])
        self.assertEqual(__version__, lock["version"])
        self.assertEqual(__version__, lock["packages"][""]["version"])
        self.assertEqual("XiaoA.exe", package["build"]["portable"]["artifactName"])
        self.assertTrue(package["build"]["portable"]["useZip"])
        self.assertNotIn("src/**/*", package["build"]["files"])
        self.assertIn("PolyForm Noncommercial License 1.0.0", (root / "LICENSE").read_text(encoding="utf-8"))
        self.assertIn("禁止商业用途", (root / "LICENSE-COMMERCIAL.md").read_text(encoding="utf-8"))
        updater = (root / "backend/core/update_manager.py").read_text(encoding="utf-8")
        self.assertIn('UPDATE_REPOSITORY = "QQ89937761/agent-a"', updater)
        release = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn('tags: ["v*"]', release)
        self.assertIn("frontend/dist/XiaoA.exe", release)
        self.assertIn("release-assets/build-manifest.json", release)
        self.assertTrue((root / "一键打包正式版.bat").exists() is False)
        self.assertTrue((root / "一键发布GitHub正式版.bat").exists() is False)
        self.assertFalse((root / "deploy/update-site").exists())

    def test_untracked_secrets_and_python_cache_never_enter_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("backend/module.py", "backend/test_fake.py", "deploy/deploy_hub.sh",
                         "frontend/src/index.html", "config.example.toml", "backend/private.json",
                         "config.toml", "backend/__pycache__/module.pyc"):
                file = root / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("fixture", encoding="utf-8")
            tracked = b"backend/module.py\0backend/test_fake.py\0deploy/deploy_hub.sh\0frontend/src/index.html\0config.example.toml\0"
            with patch("deploy.release_files.subprocess.check_output", return_value=tracked):
                data = release_data_files(root)
            names = [Path(source).relative_to(root).as_posix() for source, _ in data]
            self.assertEqual(["backend/module.py", "deploy/deploy_hub.sh", "frontend/src/index.html", "config.example.toml"], names)
            self.assertEqual("server-payload/backend", data[0][1])
            self.assertEqual(".", data[-1][1])

    def test_missing_or_generated_tracked_resources_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("missing.py", "cache.pyc", "secret.key", "../outside.py"):
                with self.subTest(name=name), patch("deploy.release_files.subprocess.check_output", return_value=(name+"\0").encode()):
                    with self.assertRaises(RuntimeError):
                        release_data_files(root)
            with patch("deploy.release_files.subprocess.check_output", return_value=b""):
                with self.assertRaisesRegex(RuntimeError, "template"):
                    release_data_files(root)


class PackageContentTests(unittest.TestCase):
    def test_released_runtime_allows_only_local_ready_marker(self):
        from deploy.smoke_plugin_runtimes import verify_released_runtime

        with tempfile.TemporaryDirectory() as tmp:
            source, runtime = Path(tmp) / "source", Path(tmp) / "released"
            for directory in (source, runtime):
                (directory / "python").mkdir(parents=True)
                (directory / "python/python313.dll").write_bytes(b"runtime-dll")
            (runtime / ".ready.json").write_text('{"version":1}', encoding="utf-8")
            self.assertEqual(1, verify_released_runtime(source, runtime))
            binary = runtime / "python/python313.dll"
            binary.write_bytes(b"changed-dll")
            with self.assertRaisesRegex(RuntimeError, "stale=.*python313.dll"):
                verify_released_runtime(source, runtime)
            binary.unlink()
            with self.assertRaisesRegex(RuntimeError, "missing=.*python313.dll"):
                verify_released_runtime(source, runtime)
            binary.write_bytes(b"runtime-dll")
            for name in ("private.key", "python/.ready.json"):
                extra = runtime / name
                extra.write_bytes(b"unexpected")
                with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "unexpected="):
                    verify_released_runtime(source, runtime)
                extra.unlink()

    def test_exact_source_hashes_are_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "module.py"
            source.write_text("reviewed-source", encoding="utf-8")
            expected = {"module.py": source}
            self.assertEqual(1, verify_files(expected, {"module.py": digest(source)}, "fixture"))
            for actual, message in (({}, "missing"),
                                    ({"module.py": "old-package"}, "stale"),
                                    ({"module.py": digest(source), "config.toml": "private"}, "unexpected")):
                with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                    verify_files(expected, actual, "fixture")

    def test_independent_build_allows_only_text_line_ending_differences(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "module.py"
            actual = Path(tmp) / "built.py"
            source.write_bytes(b"x = 1\n")
            actual.write_bytes(b"x = 1\r\n")
            self.assertNotEqual(digest(source), digest(actual))
            self.assertEqual(1, verify_files({"module.py": source},
                {"module.py": digest(actual, normalize_text=True)}, "fixture", normalize_text=True))
            actual.write_bytes(b"x = 2\r\n")
            with self.assertRaisesRegex(RuntimeError, "stale"):
                verify_files({"module.py": source}, {"module.py": digest(actual, normalize_text=True)},
                             "fixture", normalize_text=True)
            binary = Path(tmp) / "image.png"
            binary.write_bytes(b"binary\r\n")
            self.assertEqual(digest(binary), digest(binary, normalize_text=True))

    def test_current_payload_cannot_hide_stale_frozen_code(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "module.py"
            source.write_text("def allowed():\n    return False\n", encoding="utf-8")
            expected = {"backend.module": source}
            good = compile(source.read_bytes(), "different/build/module.py", "exec", dont_inherit=True)
            archive = SimpleNamespace(toc={"backend.module": ()}, extract=lambda name: good)
            self.assertEqual(1, verify_frozen_modules(expected, archive))
            archive.extract = lambda name: compile("def allowed():\n    return True\n", "old.py", "exec")
            with self.assertRaisesRegex(RuntimeError, "bytecode"):
                verify_frozen_modules(expected, archive)
            archive.toc = {}
            with self.assertRaisesRegex(RuntimeError, "missing"):
                verify_frozen_modules(expected, archive)

    def test_one_click_does_not_claim_verified_when_smoke_is_skipped(self):
        root = Path(__file__).resolve().parent.parent
        script = (root / "deploy/build_release.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("$verified = -not $SkipUpdateSmoke", script)
        self.assertIn("verified = $verified", script)
        self.assertIn('deploy\\verify_release.py', script)
        self.assertIn("Assert-WorkspacePath $distDirectory", script)

    def test_portable_checks_refuse_an_occupied_user_port(self):
        root = Path(__file__).resolve().parent.parent
        backend = (root / "deploy/smoke_backend.py").read_text(encoding="utf-8")
        update = (root / "deploy/smoke_portable_update.py").read_text(encoding="utf-8")
        release = (root / "deploy/build_release.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('1985 if args.portable else 0', backend)
        self.assertIn("probe.bind(('127.0.0.1', 1985))", update)
        runtimes = (root / "deploy/prepare_plugin_runtimes.ps1").read_text(encoding="utf-8-sig")
        self.assertIn('foreach ($name in @("t64-arm.exe", "w64-arm.exe"))', runtimes)
        self.assertIn('$armShim.StartsWith($stageRoot', runtimes)
        self.assertLess(update.index("probe.bind("), update.index("subprocess.Popen("))
        self.assertLess(release.index("$listener.Start()"), release.index('if (-not $SkipDependencyInstall)'))
        self.assertLess(release.index('Remove-Item -LiteralPath $manifestPath'), release.index('$listener.Start()'))


class LocalVersionIncrementTests(unittest.TestCase):
    def test_local_version_increment_syncs_files_and_rejects_mismatches(self):
        if os.name != "nt":
            return  # The one-click entry runs in Windows PowerShell 5.1.
        root = Path(__file__).resolve().parent.parent
        script = root / "deploy/build_release.ps1"
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            (fixture / "frontend").mkdir()
            (fixture / "backend").mkdir()
            files = [fixture / "frontend/package.json", fixture / "frontend/package-lock.json",
                     fixture / "backend/__init__.py"]
            package = {"name": "fixture", "version": "1.0.10",
                       "scripts": {"version": 'node -e "process.exit(99)"'}}
            lock = {"name": "fixture", "version": "1.0.10", "lockfileVersion": 3,
                    "packages": {"": {"name": "fixture", "version": "1.0.10"},
                                 "node_modules/fixture": {"version": "1.0.10"}}}
            files[0].write_text(json.dumps(package), encoding="utf-8")
            files[1].write_text(json.dumps(lock), encoding="utf-8")
            files[2].write_text('# 版本说明\n__version__ = "1.0.10"\n', encoding="utf-8")
            check = fixture / "check.ps1"
            check.write_text(r"""
param([string]$Script, [string]$Fixture)
$ErrorActionPreference = "Stop"
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$null, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Invoke-Checked', 'Update-ReleaseVersion')) {
    $fn = $ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name}, $true)
    if (-not $fn) { throw "Missing $name" }
    Invoke-Expression $fn.Extent.Text
}
$Repo = $Fixture
$Frontend = Join-Path $Repo 'frontend'
$version = Update-ReleaseVersion
if ($version -isnot [string]) { throw 'Version must be one string, not command output' }
Write-Output "VERSION=$version"
""", encoding="utf-8-sig")
            def run():
                return subprocess.run(
                    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(check), str(script), str(fixture)], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=60)
            for version in ("1.0.11", "1.0.12"):
                result = run()
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn(f"VERSION={version}", result.stdout)
                self.assertEqual(version, json.loads(files[0].read_text())["version"])
                changed = json.loads(files[1].read_text())
                self.assertEqual(version, changed["version"])
                self.assertEqual(version, changed["packages"][""]["version"])
                self.assertEqual("1.0.10", changed["packages"]["node_modules/fixture"]["version"])
                self.assertEqual(f'# 版本说明\n__version__ = "{version}"\n', files[2].read_text(encoding="utf-8"))
            good = [p.read_bytes() for p in files]
            for bad_file, bad_data in (
                (files[2], b'__version__ = "1.0.9"\n'),
                (files[1], json.dumps({**changed, "version": "1.0.9"}).encode()),
                (files[0], json.dumps({**package, "version": "invalid"}).encode()),
            ):
                with self.subTest(file=bad_file.name):
                    for file, data in zip(files, good):
                        file.write_bytes(data)
                    bad_file.write_bytes(bad_data)
                    before = [p.read_bytes() for p in files]
                    result = run()
                    self.assertNotEqual(0, result.returncode)
                    self.assertEqual(before, [p.read_bytes() for p in files])


class PublisherVerificationTests(unittest.TestCase):
    def test_release_build_is_tag_driven_and_keeps_ordinary_ci_read_only(self):
        root = Path(__file__).resolve().parent.parent
        build = (root / "deploy/build_release.ps1").read_text(encoding="utf-8-sig")
        workflow = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
        self.assertIn("[switch]$SkipVersionIncrement", build)
        self.assertIn("if ($SkipVersionIncrement)", build)
        self.assertIn('tags: ["v*"]', workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("-SkipVersionIncrement", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("--generate-notes", workflow)
        self.assertNotIn("deploy/publish_github_release.ps1", workflow)

    def test_release_build_hashing_works_without_powershell_modules(self):
        import hashlib
        root = Path(__file__).resolve().parent.parent
        scripts = [root / "deploy/build_release.ps1", root / "deploy/build_backend.ps1"]
        for script in scripts:
            self.assertNotIn("Get-FileHash", script.read_text(encoding="utf-8-sig"))
        if os.name != "nt":
            return
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "hash test.bin"
            artifact.write_bytes(bytes(range(256)) * 4096)
            expected = hashlib.sha256(artifact.read_bytes()).hexdigest()
            script = root / "deploy/build_release.ps1"
            check = Path(tmp) / "check.ps1"
            check.write_text(r"""
param([string]$Script, [string]$Artifact)
$ErrorActionPreference = "Stop"
$errors = $null
[System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$null, [ref]$errors) | Out-Null
if ($errors.Count) { throw "Invalid release script syntax" }
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$null, [ref]$errors)
$fn = $ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-Sha256Hex'}, $true)
Invoke-Expression $fn.Extent.Text
Get-Sha256Hex $Artifact
""", encoding="utf-8-sig")
            env = os.environ.copy()
            env["PSModulePath"] = str(Path(tmp) / "no-modules")
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(check),
                 str(script), str(artifact)], capture_output=True, text=True, env=env,
                encoding="utf-8", errors="replace", timeout=60)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(expected, result.stdout.strip())
