#!/usr/bin/env bash
# 部署后只读自检；不修改服务或配置。
set -u

if [ "$(uname -s 2>/dev/null || true)" != "Linux" ]; then
  echo "[ERROR] postcheck.sh 只支持 Linux。" >&2
  exit 2
fi
PORT="${1:-1115}"
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "[ERROR] 端口必须是 1-65535 的数字。" >&2
  exit 2
fi

failed=0
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if bash "$SCRIPT_DIR/diagnose_endpoint.sh" "$PORT"; then
  echo "本地三层: OK"
else
  echo "本地三层: FAIL"
  failed=1
fi

health="$(curl -skS --max-time 10 -w $'\n%{http_code}' "https://127.0.0.1:$PORT/health" 2>&1 || true)"
http_code="${health##*$'\n'}"
body="${health%$'\n'*}"
if [ "$http_code" != "200" ]; then
  echo "health: HTTP ${http_code:-请求失败}"
  [ -n "$body" ] && echo "health 错误: $body"
  failed=1
else
  parsed="$(printf '%s' "$body" | python3 -c '
import json, sys
try:
    value=json.load(sys.stdin)
except Exception as exc:
    print("invalid-json:" + type(exc).__name__)
    raise SystemExit(0)
worker=value.get("worker") or {}
print("version=" + str(value.get("version", "")) + " worker.online=" + str(worker.get("online", "")) + " worker_id=" + str(worker.get("worker_id", "")))
')"
  echo "health: HTTP 200 $parsed"
  case "$parsed" in
    *"version="*" worker.online="*) : ;;
    *) echo "[ERROR] health 响应缺少版本信息"; failed=1;;
  esac
fi

if [ "$failed" -ne 0 ]; then
  echo "结论: 未通过"
  exit 1
fi
echo "结论: 通过"
