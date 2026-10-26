"""Single public HTTPS/WSS gateway with automatic IP certificate renewal.

sudo python3 deploy/configure_ip_tls.py PUBLIC_IP HUB_PORT [--dry-run]
HTTP-01 uses port 80. Only xiaoa-owned nginx files/ACME directories are managed.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile

BASE = Path("/opt/xiaoa-acme")
HTTP = Path("/etc/nginx/conf.d/xiaoa-ip-http.conf")
HTTPS = Path("/etc/nginx/conf.d/xiaoa-ip-https.conf")
STATE = Path("/opt/xiaoa/data/public-tls.json")
ENV = Path("/etc/xiaoa-hub.env")
FIREWALL_UNIT = Path("/etc/systemd/system/xiaoa-ip-firewall.service")


def run(*args):
    return subprocess.run(list(map(str, args)), check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600).stdout


def atomic(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".xiaoa-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def nginx_config(path, content):
    previous = path.read_text() if path.exists() else None
    atomic(path, content)
    try:
        run("nginx", "-t")
        run("systemctl", "reload", "nginx")
    except Exception:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, previous)
        run("nginx", "-t")
        run("systemctl", "reload", "nginx")
        raise


def endpoint(host, port):
    address = ipaddress.ip_address(host)
    if not address.is_global:
        raise ValueError("IP must be a globally routable IPv4/IPv6 address")
    port = int(port)
    if not 1024 <= port <= 65535:
        raise ValueError("Hub port must be between 1024 and 65535")
    return str(address), port


def select_port(hub_port, previous=0):
    candidates = [previous] if previous else range(8443, 65536)
    for port in candidates:
        if port == hub_port or not 1024 <= port <= 65535:
            continue
        with socket.socket() as sock:
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                if previous:  # Our nginx listener is expected on repeat runs.
                    text = HTTPS.read_text() if HTTPS.exists() else ""
                    if f"listen {port} ssl;" in text:
                        return port
                continue
            return port
    raise RuntimeError("No free HTTPS port; existing service was left unchanged")


def firewall(port):
    if shutil.which("ufw") and "Status: active" in run("ufw", "status"):
        run("ufw", "allow", f"{port}/tcp")
    elif shutil.which("firewall-cmd") and subprocess.run(
            ["firewall-cmd", "--state"], capture_output=True).returncode == 0:
        run("firewall-cmd", "--permanent", f"--add-port={port}/tcp")
        run("firewall-cmd", f"--add-port={port}/tcp")
    else:
        for tool in ("iptables", "ip6tables"):
            if shutil.which(tool):
                check = [tool, "-C", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]
                if subprocess.run(check, capture_output=True).returncode:
                    run(tool, "-I", "INPUT", "-p", "tcp", "--dport", port, "-j", "ACCEPT")
    # Provider security groups cannot be changed safely without provider credentials.


def http_config(host):
    return f"""# Managed by XiaoA IP TLS. Do not store credentials here.
server {{
    listen 80;
    listen [::]:80;
    server_name {host};
    location ^~ /.well-known/acme-challenge/ {{
        root {BASE}/webroot;
        try_files $uri =404;
    }}
    location / {{ return 404; }}
}}
"""


def tls_config(host, hub_port, web_port):
    return f"""# Managed by XiaoA IP TLS. One public endpoint for browsers and workers.
server {{
    listen {web_port} ssl;
    listen [::]:{web_port} ssl;
    server_name {host};
    ssl_certificate {BASE}/config/live/xiaoa-ip/fullchain.pem;
    ssl_certificate_key {BASE}/config/live/xiaoa-ip/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 32m;
    add_header X-Content-Type-Options nosniff always;
    add_header Cache-Control "private, no-store" always;
    location / {{
        proxy_pass https://127.0.0.1:{hub_port};
        proxy_ssl_trusted_certificate /opt/xiaoa/data/hub-tls/hub.crt;
        proxy_ssl_verify on;
        proxy_ssl_name {host};
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }}
}}
"""


def certbot_command(host, *, staging=False):
    config = BASE / ("staging" if staging else "config")
    return [str(BASE / "venv/bin/certbot"), "certonly", "--non-interactive",
            "--agree-tos", "--register-unsafely-without-email", "--webroot",
            "--webroot-path", str(BASE / "webroot"), "--preferred-profile", "shortlived",
            "--ip-address", host, "--cert-name", "xiaoa-ip", "--config-dir", str(config),
            "--work-dir", str(BASE / "work"), "--logs-dir", str(BASE / "logs"),
            *(["--staging"] if staging else [])]


def install_firewall(host, hub_port):
    # Persist only XiaoA's port rules; never save/overwrite the host's ruleset.
    atomic(BASE / "manage.py", Path(__file__).read_text(encoding="utf-8"), 0o700)
    atomic(FIREWALL_UNIT, f"""[Unit]
Description=XiaoA public IP web port firewall rules
After=network-pre.target
Before=nginx.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {BASE}/manage.py {host} {hub_port} --firewall-only
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
""")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "xiaoa-ip-firewall.service")
    # First deploy still holds the configuration lock; next boot restores rules.


def install_timer():
    hook = BASE / "reload-nginx.sh"
    atomic(hook, "#!/bin/sh\nset -eu\n/usr/sbin/nginx -t\n/bin/systemctl reload nginx\n", 0o700)
    atomic(Path("/etc/systemd/system/xiaoa-ip-cert-renew.service"), f"""[Unit]
Description=XiaoA public IP certificate renewal
Wants=network-online.target
After=network-online.target nginx.service

[Service]
Type=oneshot
ExecStart={BASE}/venv/bin/certbot renew --non-interactive --config-dir {BASE}/config --work-dir {BASE}/work --logs-dir {BASE}/logs --deploy-hook {hook}
# Also retry loading after a previous successful issuance whose hook failed.
ExecStartPost=/usr/sbin/nginx -t
ExecStartPost=/bin/systemctl reload nginx
TimeoutStartSec=600
UMask=0077
""")
    atomic(Path("/etc/systemd/system/xiaoa-ip-cert-renew.timer"), """[Unit]
Description=Check XiaoA IP certificate renewal every 12 hours

[Timer]
OnCalendar=*-*-* 00,12:00:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
""")
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", "xiaoa-ip-cert-renew.timer")


def configure(host, hub_port, *, dry_run=False):
    host, hub_port = endpoint(host, hub_port)
    for path in (BASE, HTTP, HTTPS, STATE, FIREWALL_UNIT):
        if path.is_symlink():
            raise RuntimeError(f"Refusing a symlink at a managed path: {path}")
    if not STATE.parent.joinpath("hub-tls/hub.crt").is_file():
        raise RuntimeError("Deploy the hub before configuring public IP HTTPS")
    if not shutil.which("nginx"):
        # Never stop another application that owns HTTP-01's port.
        with socket.socket() as sock:
            sock.bind(("0.0.0.0", 80))
        run("apt-get", "update", "-y")
        run("apt-get", "install", "-y", "nginx")
        run("systemctl", "enable", "--now", "nginx")
    for path in (HTTP, HTTPS):
        if path.exists() and not path.read_text().startswith("# Managed by XiaoA IP TLS."):
            raise RuntimeError(f"Refusing to overwrite an unmanaged nginx config: {path}")
    previous = json.loads(STATE.read_text()) if STATE.exists() else {}
    if previous and previous.get("host") != host:
        raise RuntimeError("IP changed; migrate the existing IP certificate explicitly")
    web_port = select_port(hub_port, previous.get("web_port", 0))
    # Refuse an existing IP virtual host owned by another application.
    current = run("nginx", "-T")
    foreign = current
    # nginx -T separates files; remove each owned config independently.
    for path in (HTTP, HTTPS):
        if path.exists():
            foreign = foreign.replace(path.read_text(), "")
    for names in re.findall(r"server_name\s+([^;]+);", foreign):
        if host in names.split():
            raise RuntimeError("Another nginx virtual host already owns this IP")
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    # nginx must traverse the challenge path even when deployment uses umask 077.
    os.chmod(BASE, 0o711)
    for suffix in ("webroot", "webroot/.well-known", "webroot/.well-known/acme-challenge"):
        directory = BASE / suffix
        directory.mkdir(exist_ok=True)
        directory.chmod(0o755)
    if not (BASE / "venv/bin/certbot").exists():
        run("python3", "-m", "venv", BASE / "venv")
        run(BASE / "venv/bin/pip", "install", "certbot>=5.4,<6")
    firewall(80)
    firewall(web_port)
    nginx_config(HTTP, http_config(host))
    print("HTTP-01 ready on port 80; provider security group must allow TCP 80", flush=True)
    # First issuance is tested against staging before using production rate limits.
    if dry_run or not (BASE / "config/live/xiaoa-ip/fullchain.pem").exists():
        print(run(*certbot_command(host, staging=True)), flush=True)
    if dry_run:
        return {"staging_ok": True, "host": host, "web_port": web_port}
    print(run(*certbot_command(host)), flush=True)
    nginx_config(HTTPS, tls_config(host, hub_port, web_port))
    result = {"host": host, "hub_port": hub_port, "web_port": web_port, "tls_mode": "ca"}
    atomic(STATE, json.dumps(result) + "\n", 0o600)
    # Only nginx is public; browsers and worker use its same CA-validated URL.
    text = ENV.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("AGENT_SERVER_HOST=")]
    updated = "\n".join([*lines, "AGENT_SERVER_HOST=127.0.0.1"]) + "\n"
    if updated != text:
        atomic(ENV, updated, 0o600)
        if os.getenv("XIAOA_SKIP_START") != "1":
            run("systemctl", "restart", "xiaoa-hub")
    install_firewall(host, hub_port)
    install_timer()
    display = f"[{host}]" if ":" in host else host
    print(f"XIAOA_PUBLIC_WEB_URL=https://{display}:{web_port}", flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("hub_port", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--firewall-only", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("root is required")
    import fcntl
    try:
        with open("/opt/xiaoa-ip-tls.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.firewall_only:
                host, hub_port = endpoint(args.host, args.hub_port)
                info = json.loads(STATE.read_text())
                if info["host"] != host or info["hub_port"] != hub_port:
                    raise ValueError("Firewall state does not match this deployment")
                web_port = int(info["web_port"])
                if not 1024 <= web_port <= 65535:
                    raise ValueError("Invalid web port")
                for port in (80, web_port):
                    firewall(port)
            else:
                configure(args.host, args.hub_port, dry_run=args.dry_run)
    except subprocess.CalledProcessError as exc:
        print(exc.stdout[-6000:] if exc.stdout else str(exc), flush=True)
        raise SystemExit(1)
