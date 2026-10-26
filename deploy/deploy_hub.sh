#!/usr/bin/env bash
# 小A hub 免域名部署：统一公网 HTTPS/WSS 入口 + 自动续期 IP 证书。
set -euo pipefail

if [ "${EUID:-$(id -u)}" -ne 0 ]; then exec sudo bash "$0" "$@"; fi

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
VENV="$APP_DIR/.venv"
PY="$VENV/bin/python"
SERVICE=/etc/systemd/system/xiaoa-hub.service
ENV_FILE=/etc/xiaoa-hub.env
TLS_DIR="$APP_DIR/data/hub-tls"
CERT_FILE="$TLS_DIR/hub.crt"
KEY_FILE="$TLS_DIR/hub.key"
CREDENTIAL_FILE="$APP_DIR/data/connection-string.txt"

system_runtime_ready() {
  python3 -I -B -c 'import sys, ssl, venv, ensurepip; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1 &&
    openssl version >/dev/null 2>&1
}

ensure_system_runtime() {
  if system_runtime_ready; then
    echo "XIAOA_STEP system-runtime"
    return
  fi
  echo "==> 安装缺少的 Python、venv、OpenSSL（不安装 Nginx/Certbot）"
  echo "XIAOA_STEP apt"
  apt-get update -y
  apt-get install -y python3 python3-venv openssl
  if ! system_runtime_ready; then
    echo "Python 3.10+、ssl、venv、ensurepip 或 OpenSSL 仍不可用" >&2
    return 1
  fi
}

random_secret() { openssl rand -hex 32; }
fingerprint() { openssl x509 -in "$CERT_FILE" -noout -fingerprint -sha256 | cut -d= -f2 | tr -d ':'; }

print_connection() {
  local host="$1" port="$2" token="$3" fp
  fp="$(fingerprint)"
  if [ -f "$APP_DIR/data/public-tls.json" ]; then
    port="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["web_port"])' "$APP_DIR/data/public-tls.json")"
    fp=""
  fi
  local connection
  connection="$($PY -m backend.core.connection_string "$host" "$port" "$fp" "$token")"
  umask 077
  printf 'XIAOA_CONNECTION_STRING=%s\nAGENT_WEB_PASSWORD=%s\n' \
    "$connection" "$WEB_PASSWORD" > "$CREDENTIAL_FILE"
  if [ -f "$APP_DIR/data/public-tls.json" ]; then
    "$PY" - "$APP_DIR/data/public-tls.json" >> "$CREDENTIAL_FILE" <<'PY'
import ipaddress, json, sys
value = json.load(open(sys.argv[1]))
host = str(ipaddress.ip_address(value["host"]))
if ":" in host:
    host = "[" + host + "]"
print(f"XIAOA_PUBLIC_WEB_URL=https://{host}:{int(value['web_port'])}")
PY
  fi
  chmod 600 "$CREDENTIAL_FILE"
  echo "凭据已写入 $CREDENTIAL_FILE（安装完成后由本机自动读取并删除）"
  echo "连接到: $host:$port  证书指纹: ${fp:0:12}..."
}

replace_env() {
  python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import os, sys, tempfile
path, name, value = sys.argv[1:]
lines = open(path, encoding="utf-8").read().splitlines()
for index, line in enumerate(lines):
    if line.startswith(name + "="):
        lines[index] = name + "=" + value
        break
else:
    lines.append(name + "=" + value)
fd, temp = tempfile.mkstemp(prefix=".xiaoa-hub.", dir=os.path.dirname(path), text=True)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines) + "\n")
os.chmod(temp, 0o600)
os.replace(temp, path)
PY
}

if [ "${1:-}" = "rotate-token" ]; then
  [ -f "$ENV_FILE" ] && [ -f "$CERT_FILE" ] || {
    echo "[ERROR] 尚未部署，找不到 $ENV_FILE 或 $CERT_FILE"; exit 1;
  }
  set -a; source "$ENV_FILE"; set +a
  TOKEN="$(random_secret)"
  WEB_PASSWORD="${AGENT_WEB_PASSWORD:?环境文件缺少 AGENT_WEB_PASSWORD}"
  replace_env AGENT_HUB_TOKEN "$TOKEN"
  systemctl restart xiaoa-hub
  print_connection "${AGENT_PUBLIC_HOST:?环境文件缺少 AGENT_PUBLIC_HOST}" \
    "${AGENT_SERVER_PORT:-8443}" "$TOKEN"
  echo "旧连接串已经失效；新凭据已写入 $CREDENTIAL_FILE。"
  exit 0
fi

detect_public_ip() {
  local service value
  for service in "https://api.ipify.org" "https://ifconfig.me/ip" "https://icanhazip.com"; do
    if command -v curl >/dev/null 2>&1; then
      value="$(curl -fsS --max-time 5 "$service" 2>/dev/null | tr -d '[:space:]' || true)"
    elif command -v python3 >/dev/null 2>&1; then
      value="$(python3 - "$service" <<'PY' 2>/dev/null || true
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=5) as response:
    print(response.read(128).decode("ascii").strip())
PY
)"
    else
      value=""
    fi
    if [[ "$value" =~ ^[0-9a-fA-F:.]+$ ]] && [ -n "$value" ]; then
      printf '%s' "$value"
      return 0
    fi
  done
  return 1
}

port_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -H -ltn 2>/dev/null | awk -v suffix=":$port" '$4 ~ suffix"$" {found=1} END {exit !found}'
  else
    python3 - "$port" <<'PY'
import socket, sys
sock = socket.socket()
try:
    sock.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    raise SystemExit(0)
else:
    raise SystemExit(1)
finally:
    sock.close()
PY
  fi
}

configure_firewall() {
  local port="$1"
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi '^Status: active'; then
    ufw allow "$port/tcp" >/dev/null
    ufw status 2>/dev/null | grep -Eq "${port}/tcp[[:space:]]+ALLOW"
    echo "已在 UFW 放行 TCP $port。"
    return
  fi
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="$port/tcp" >/dev/null
    firewall-cmd --reload >/dev/null
    firewall-cmd --list-ports | tr ' ' '\n' | grep -qx "$port/tcp"
    echo "已在 firewalld 放行 TCP $port。"
    return
  fi
  if command -v iptables >/dev/null 2>&1; then
    if ! iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
      iptables -I INPUT -p tcp --dport "$port" -j ACCEPT
    fi
    iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null
    echo "已在 iptables 放行 TCP $port（规则未持久化，请按发行版保存规则）。"
    return
  fi
  echo "本机没有启用 UFW、firewalld 或 iptables；只剩云厂商安全组这一层。"
}

PUBLIC_IP="${1:-}"
PORT="${2:-}"
if [ -z "$PORT" ] && [ -f "$ENV_FILE" ]; then
  PORT="$(set -a; source "$ENV_FILE"; printf '%s' "${AGENT_SERVER_PORT:-}")"
  if [[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ]; then
    echo "检测到已有部署，继续使用端口 $PORT。"
  else
    PORT=""
  fi
fi
if [ -z "$PUBLIC_IP" ]; then
  if PUBLIC_IP="$(detect_public_ip)"; then
    echo "已自动探测公网 IP: $PUBLIC_IP"
  else
    echo "[ERROR] 无法自动探测公网 IP。请重试，或手动传入: sudo bash deploy/deploy_hub.sh <公网IP> [端口]" >&2
    exit 2
  fi
fi
if [[ ! "$PUBLIC_IP" =~ ^[0-9a-fA-F:.]+$ ]] || [ -z "$PUBLIC_IP" ]; then
  echo "[ERROR] 公网 IP 格式无效，请传入 IPv4 或 IPv6 地址。" >&2
  exit 2
fi
if [ -z "$PORT" ]; then
  PORT=8443
  while [ "$PORT" -le 65535 ] && port_in_use "$PORT"; do PORT=$((PORT + 1)); done
  if [ "$PORT" -gt 65535 ]; then
    echo "[ERROR] 从 8443 到 65535 没有可用端口。" >&2
    exit 2
  fi
  echo "已选用端口 $PORT，请在安全组放行 TCP $PORT。"
fi
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "[ERROR] 端口必须在 1 到 65535 之间"; exit 2
fi
if [ "$PORT" = 80 ] || [ "$PORT" = 443 ]; then
  echo "[ERROR] 免域名路线不使用 80/443，请保留给服务器现有服务。建议 8443。"; exit 2
fi


if [ "${XIAOA_REUSE_RUNTIME:-0}" != "1" ]; then
  ensure_system_runtime
  echo "==> 建立 hub 运行环境"
  echo "XIAOA_STEP venv"
  python3 -m venv "$VENV"
  echo "XIAOA_STEP pip"
  "$PY" -m pip install --upgrade pip
  "$PY" -m pip install -r "$APP_DIR/backend/requirements-hub.txt"
fi

echo "XIAOA_STEP tls"
mkdir -p "$TLS_DIR" "$APP_DIR/data"
chmod 700 "$TLS_DIR"
if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
  echo "==> 生成 10 年有效的自签 TLS 证书（SAN=$PUBLIC_IP）"
  openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
    -keyout "$KEY_FILE" -out "$CERT_FILE" -subj "/CN=$PUBLIC_IP" \
    -addext "subjectAltName=IP:$PUBLIC_IP"
  chmod 600 "$KEY_FILE" "$CERT_FILE"
fi

echo "XIAOA_STEP config"
TOKEN="$(random_secret)"
WEB_PASSWORD="$(random_secret)"
LLM_BASE_URL="${AGENT_LLM_BASE_URL:-http://127.0.0.1:8080/v1}"
LLM_API_KEY="${AGENT_LLM_API_KEY:-}"
CHAT_MODEL="${AGENT_CHAT_MODEL:-gpt-4o}"
MODEL_OPTIONS="${AGENT_MODEL_OPTIONS:-$CHAT_MODEL}"
URL_HOST="$PUBLIC_IP"
if [[ "$PUBLIC_IP" == *:* ]]; then URL_HOST="[$PUBLIC_IP]"; fi
ALLOWED_ORIGINS="${AGENT_ALLOWED_ORIGINS:-https://$URL_HOST:$PORT}"
if [ -f "$ENV_FILE" ]; then
  set -a; source "$ENV_FILE"; set +a
  TOKEN="${AGENT_HUB_TOKEN:-$TOKEN}"
  WEB_PASSWORD="${AGENT_WEB_PASSWORD:-$WEB_PASSWORD}"
  LLM_BASE_URL="${AGENT_LLM_BASE_URL:-$LLM_BASE_URL}"
  LLM_API_KEY="${AGENT_LLM_API_KEY:-$LLM_API_KEY}"
  CHAT_MODEL="${AGENT_CHAT_MODEL:-$CHAT_MODEL}"
  MODEL_OPTIONS="${AGENT_MODEL_OPTIONS:-$MODEL_OPTIONS}"
  ALLOWED_ORIGINS="${AGENT_ALLOWED_ORIGINS:-$ALLOWED_ORIGINS}"
fi
if [ ! -f "$ENV_FILE" ]; then
cat >"$ENV_FILE" <<EOF
AGENT_ROLE=hub
AGENT_SERVER_HOST=127.0.0.1
AGENT_SERVER_PORT=$PORT
AGENT_PUBLIC_HOST=$PUBLIC_IP
AGENT_TLS_CERT=$CERT_FILE
AGENT_TLS_KEY=$KEY_FILE
AGENT_HUB_TOKEN=$TOKEN
AGENT_WEB_PASSWORD=$WEB_PASSWORD
AGENT_LLM_BASE_URL=$LLM_BASE_URL
AGENT_LLM_API_KEY=$LLM_API_KEY
AGENT_CHAT_MODEL=$CHAT_MODEL
AGENT_MODEL_OPTIONS=$MODEL_OPTIONS
AGENT_ALLOWED_ORIGINS=$ALLOWED_ORIGINS
HF_HOME=$APP_DIR/data/model-cache
EOF
else
  # Preserve unknown/current settings rather than dropping secrets on update.
  replace_env AGENT_PUBLIC_HOST "$PUBLIC_IP"
  replace_env AGENT_SERVER_PORT "$PORT"
  replace_env AGENT_SERVER_HOST "127.0.0.1"
fi
chmod 600 "$ENV_FILE"

if [ ! -f "$APP_DIR/config.toml" ]; then
  cp "$APP_DIR/config.example.toml" "$APP_DIR/config.toml"
  chmod 600 "$APP_DIR/config.toml"
fi

cat >"$SERVICE" <<EOF
[Unit]
Description=XiaoA Hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
Environment=XIAOA_HOME=$APP_DIR
ExecStart=$PY -m backend.api.server
Restart=always
RestartSec=3
UMask=0077
PrivateTmp=true
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
# /opt is writable under ProtectSystem=full. Do not bind-mount config.toml:
# a file mount makes atomic os.replace(config.tmp, config.toml) fail with EBUSY.
ReadWritePaths=$APP_DIR/data

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
if [ "${XIAOA_SKIP_START:-0}" != "1" ]; then
  systemctl enable xiaoa-hub
  systemctl restart xiaoa-hub
fi

echo "XIAOA_STEP public-tls"
python3 "$APP_DIR/deploy/configure_ip_tls.py" "$PUBLIC_IP" "$PORT"

echo "==> hub 已启动在 $PUBLIC_IP:$PORT"
echo "    安全组需放行 TCP 80（证书验证）及上面统一的 HTTPS/WSS 端口；后端仅监听本机。"
print_connection "$PUBLIC_IP" "$PORT" "$TOKEN"
echo "查看状态: systemctl status xiaoa-hub"
echo "看日志:   journalctl -u xiaoa-hub -f"
