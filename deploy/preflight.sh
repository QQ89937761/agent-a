#!/usr/bin/env bash
# 只读部署前检查；不安装、不修改系统。
set -u

if [ "$(uname -s 2>/dev/null || true)" != "Linux" ]; then
  echo "[ERROR] preflight.sh 只支持 Linux。" >&2
  exit 2
fi

PORT="${1:-8443}"
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "[ERROR] 端口必须是 1-65535 的数字。" >&2
  exit 2
fi
if [ "$PORT" = 80 ] || [ "$PORT" = 443 ]; then
  echo "[ERROR] 免域名路线不使用 80/443。" >&2
  exit 2
fi

issues=()
if ! command -v ss >/dev/null 2>&1; then issues+=("缺少 ss(iproute2)"); fi
if ! command -v python3 >/dev/null 2>&1; then issues+=("缺少 python3"); fi
if ! command -v df >/dev/null 2>&1; then issues+=("缺少 df"); fi

os_name="未知"
if [ -r /etc/os-release ]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  os_name="${PRETTY_NAME:-${ID:-未知}}"
fi

available_kb="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo 2>/dev/null || true)"
available_kb="${available_kb:-0}"
available_gib="$(awk -v kb="$available_kb" 'BEGIN {printf "%.2f", kb/1024/1024}')"
mem_note=""
if [ "$available_kb" -lt 1048576 ]; then
  issues+=("MemAvailable ${available_gib} GiB，低于 1 GiB；连 hub 也要谨慎，请先确认服务器内存余量")
  mem_note="不建议安装 Chromium，hub 也需谨慎"
elif [ "$available_kb" -lt 1572864 ]; then
  mem_note="不建议安装 Chromium"
fi

port_line=""
if command -v ss >/dev/null 2>&1; then
  port_line="$(ss -H -ltnp 2>/dev/null | awk -v p=":$PORT" '$4 ~ p"$" {print; exit}')"
  if [ -n "$port_line" ]; then
    issues+=("端口 $PORT 已被占用")
  fi
fi

python_version="$(python3 --version 2>&1 || true)"
if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  issues+=("$python_version；需要 Python 3.10+")
fi

disk_line="$(df -hP . 2>/dev/null | tail -n 1 || true)"
echo "系统: $os_name"
echo "Python: $python_version"
echo "MemAvailable: ${available_gib} GiB${mem_note:+ ($mem_note)}"
echo "磁盘: ${disk_line:-无法读取}"
if [ -n "$port_line" ]; then
  echo "端口 $PORT: 已占用"
  echo "  $port_line"
else
  echo "端口 $PORT: 未占用"
fi

if [ "${#issues[@]}" -gt 0 ]; then
  echo "先解决这些:"
  printf '  - %s\n' "${issues[@]}"
  exit 1
fi
echo "结论: 可以继续"
