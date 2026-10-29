"""Exercise model APIs in the shipped executable, with no real accounts or models."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import sys
import subprocess
import tempfile
import threading
import time
import urllib.request

import psutil

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def require(condition, detail):
    if not condition:
        raise RuntimeError(str(detail))


def channel_controls(payload):
    local = payload.get("local") if isinstance(payload, dict) else None
    return {source: local.get(source) for source in ("weixin", "feishu")} if isinstance(local, dict) else None


class MockModel(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.reply({"data": [{"id": "smoke-model"}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        require(payload["model"] == "smoke-model", payload)
        self.reply({"content": [{"type": "text", "text": "OK"}]} if self.path.endswith("/messages")
                   else {"choices": [{"message": {"role": "assistant", "content": "OK"}}]})

    def reply(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--portable", action="store_true")
    parser.add_argument("--expected-version")
    parser.add_argument("--independent-build", action="store_true")
    parser.add_argument("--reference-revision")
    parser.add_argument("--without-ca-bundle", action="store_true")
    args = parser.parse_args()
    executable = args.executable.resolve(strict=True)
    root = Path(__file__).resolve().parent.parent
    allowed = (root / "data/isolation/tmp").resolve()
    allowed.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="model-smoke-", dir=allowed)).resolve()
    servers = []
    running = []
    process = None
    bundle = executable.parent / "_internal/certifi/cacert.pem"
    hidden = bundle.with_suffix(".pem.smoke-hidden")
    moved = False
    passed = False
    try:
        http = ThreadingHTTPServer(("127.0.0.1", 0), MockModel)
        servers.append(http)
        # An untrusted local HTTPS server proves verification was not disabled.
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                .sign(key, hashes.SHA256()))
        (home / "mock.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (home / "mock.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        https = ThreadingHTTPServer(("127.0.0.1", 0), MockModel)
        servers.append(https)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(home / "mock.pem", home / "mock.key")
        https.socket = context.wrap_socket(https.socket, server_side=True)
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            running.append(server)
        with socket.socket() as probe:
            # Electron and its rendered UI use 1985; a random port only tests the backend.
            try:
                probe.bind(("127.0.0.1", 1985 if args.portable else 0))
            except OSError as error:
                raise RuntimeError("Exit the running XiaoA before Portable smoke testing (port 1985 is occupied).") from error
            port = probe.getsockname()[1]
        env = {k: v for k, v in os.environ.items() if not k.startswith(("AGENT_", "XIAOA_", "PORTABLE_"))}
        env.update(XIAOA_HOME=str(home), AGENT_SERVER_HOST="127.0.0.1", AGENT_SERVER_PORT=str(port),
                   PYTHONHOME="", PYTHONPATH="", PYTHONNOUSERSITE="1", VIRTUAL_ENV="", HF_HUB_OFFLINE="1")
        command = [str(executable)]
        if args.portable:
            # Isolate Electron's single-instance lock/profile as well as XiaoA data.
            command.append(f"--user-data-dir={home / 'electron-profile'}")
        startup = subprocess.STARTUPINFO() if os.name == "nt" else None
        if startup:
            startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = 0
        if args.without_ca_bundle:
            if args.portable or os.name != "nt" or not bundle.is_relative_to(root):
                raise RuntimeError("Missing-bundle smoke is only allowed for a Windows backend in this workspace")
            if hidden.exists():
                raise RuntimeError("An earlier smoke test has an unrestored certificate bundle")
            # Windows locks a loaded PyInstaller data file; hide it before starting the child.
            bundle.rename(hidden)
            moved = True
        process = subprocess.Popen(command, cwd=home, env=env, startupinfo=startup)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + (300 if args.portable else 60)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Executable exited before health check: {process.returncode}")
            try:
                with opener.open(base + "/health", timeout=1) as response:
                    health = json.load(response)
                break
            except (OSError, ValueError):
                time.sleep(0.5)
        else:
            raise RuntimeError("Executable failed startup health check")
        expected = args.expected_version or json.loads((root / "frontend/package.json").read_text(encoding="utf-8"))["version"]
        require(health["version"] == expected, health)
        if args.portable:
            # Check the actual EXE's expanded resources, not just win-unpacked staging.
            backend = next(child for child in psutil.Process(process.pid).children(recursive=True)
                           if child.name() == "xiaoa-backend.exe")
            from verify_release import verify
            contents = verify(root, Path(backend.exe()).parent.parent, compare_staging=not args.independent_build, reference_revision=args.reference_revision)
            print("PORTABLE_CONTENTS_VERIFIED " + json.dumps(contents), flush=True)
            # A healthy chat backend can survive failed optional provisioning.
            # Official release acceptance must also prove the plugins actually work.
            subprocess.run([
                sys.executable, str(root / "deploy/smoke_plugin_runtimes.py"),
                str(home), str(Path(backend.exe()).parent.parent),
            ], cwd=root, env=env, check=True, timeout=120)
        if os.name == "nt":
            # Exercise the windowed bootloader's inherited stdin/stdout pipes twice.
            # Invalid HWND=1 deliberately avoids reading any user application data.
            query_exe = Path(backend.exe()) if args.portable else executable
            requests = "".join(json.dumps({"id": sequence, "options": {
                "handle": 1, "max_depth": 0, "limit": 1, "include_offscreen": False,
            }}) + "\n" for sequence in (1, 2))
            query = subprocess.run([str(query_exe), "--uia-reader"], input=requests,
                                   capture_output=True, text=True, encoding="utf-8",
                                   env=env, cwd=home, timeout=20, startupinfo=startup)
            require(query.returncode == 0, "Frozen read-only reader exited unexpectedly")
            replies = [json.loads(line) for line in query.stdout.splitlines()]
            require([reply.get("id") for reply in replies] == [1, 2]
                    and all(isinstance(reply.get("result"), dict) for reply in replies),
                    "Frozen reader did not return two matched query results")
            print("PASS frozen read-only UIA process: inherited pipes and two requests", flush=True)
        token = (home / "ui_token.txt").read_text(encoding="utf-8").strip()
        def post(path, payload):
            request = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                            headers={"Content-Type": "application/json", "X-UI-Token": token})
            with opener.open(request, timeout=25) as response:
                return json.load(response)
        def get(path):
            request = urllib.request.Request(base + path, headers={"X-UI-Token": token})
            with opener.open(request, timeout=25) as response:
                return json.load(response)

        # Compare registrations to a clean source instance, not a hardcoded minimum count.
        source_env = dict(env, XIAOA_HOME=str(home / "source-inventory"), PYTHONPATH=str(root))
        inventory_code = (
            "from backend.api import server; "
            "server._register_operation_tools(); "
            "server.register_channel_tools(server.REGISTRY); "
            "server.memory_tool_module.register_memory_tools(server.REGISTRY, lambda: None); "
            "import json; print('TOOL_INVENTORY ' + json.dumps(sorted(server.REGISTRY.names())))"
        )
        inventory = subprocess.check_output([sys.executable, "-c", inventory_code],
                                            cwd=root, env=source_env, encoding="utf-8", timeout=60)
        expected_tools = json.loads(next(line[len("TOOL_INVENTORY "):]
                                        for line in inventory.splitlines() if line.startswith("TOOL_INVENTORY ")))
        catalog = get("/api/tools/catalog")
        actual_tools = sorted(tool["name"] for group in catalog["groups"] for tool in group["tools"])
        require(expected_tools == actual_tools, {
            "missing_tools": sorted(set(expected_tools) - set(actual_tools)),
            "unexpected_tools": sorted(set(actual_tools) - set(expected_tools)),
        })
        print(f"PASS frozen tool inventory matches source: {len(actual_tools)} tools", flush=True)

        controls = {"weixin": False, "feishu": False}
        require(channel_controls(get("/api/channel/control")) == controls, "initial controls incorrect")
        for source, enabled in (("weixin", False), ("feishu", False), ("weixin", True), ("feishu", True)):
            controls[source] = enabled
            result = post("/api/channel/control", {"source": source, "enabled": enabled})
            require(result.get("ok") and channel_controls(result) == controls, result)
            require(channel_controls(get("/api/channel/control")) == controls, "control readback mismatch")
        for source in ("weixin", "feishu"):
            expected_accounts = {}
            for identity, enabled in (("smoke-a", False), ("smoke-a", True),
                                      ("smoke-b", True), ("smoke-a", False)):
                expected_accounts[identity] = enabled
                result = post("/api/channel/control", {"source": source, "identity": identity, "enabled": enabled})
                require(result.get("ok"), result)
                actual = get("/api/channel/control")["local"]["accounts"][source]
                require(actual == expected_accounts, {"expected_accounts": expected_accounts, "actual_accounts": actual})
        print("PASS independent WeChat/Feishu account toggles and readback in actual frozen API", flush=True)

        for protocol in ("openai", "anthropic"):
            payload = {"protocol": protocol, "base_url": f"http://127.0.0.1:{http.server_port}", "api_key": "smoke-test-not-a-real-key"}
            result = post("/api/models/discover", payload)
            require(result == {"ok": True, "models": ["smoke-model"]}, result)
            result = post("/api/models/test", {**payload, "model": "smoke-model"})
            require(result.get("ok") and result.get("reply") == "OK", result)
            print(f"PASS {protocol}: model discovery and chat", flush=True)
        result = post("/api/models/discover", {"protocol": "openai", "base_url": f"https://127.0.0.1:{https.server_port}"})
        require(not result.get("ok") and "CERTIFICATE_VERIFY_FAILED" in result.get("error", ""), result)
        print("PASS untrusted HTTPS certificate rejected", flush=True)
        passed = True
        print(f"MODEL_SMOKE_PASSED version={expected} portable={args.portable} missing_bundle={moved}", flush=True)
    finally:
        if process and process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            else:
                process.terminate()
            process.wait(timeout=15)
        if moved and hidden.exists():
            hidden.rename(bundle)
        if not passed:
            for log in home.rglob("backend.log"):
                print(log.read_text(encoding="utf-8", errors="replace")[-4000:], flush=True)
        for server in servers:
            if server in running:
                server.shutdown()
            server.server_close()
        if home.parent != allowed or not home.is_relative_to(root):
            raise RuntimeError("Refusing to remove a smoke directory outside the isolation area")
        # Chromium may briefly retain cache handles after the process tree exits.
        for attempt in range(4):
            try:
                shutil.rmtree(home)
                break
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.5 * (attempt + 1))


if __name__ == "__main__":
    main()
