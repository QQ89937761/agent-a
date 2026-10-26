#!/usr/bin/env bash
# Diagnose the three local layers needed before checking a cloud security group.
set -u

if [ "$(uname -s 2>/dev/null || true)" != "Linux" ]; then
  echo "[ERROR] diagnose_endpoint.sh 只支持 Linux。" >&2
  exit 2
fi
PORT="${1:-1115}"
if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "[ERROR] 端口必须是 1-65535 的数字。" >&2
  exit 2
fi

failed=0
if systemctl is-active --quiet xiaoa-hub; then
  echo "服务层: OK - xiaoa-hub active (running)"
else
  echo "服务层: FAIL - xiaoa-hub 未运行；执行 sudo systemctl status xiaoa-hub"
  failed=1
fi

listeners="$(ss -H -ltn 2>/dev/null || true)"
if printf '%s\n' "$listeners" | awk -v p=":$PORT" '$4 ~ (p "$") {ok=1} END {exit !ok}'; then
  bound="$(printf '%s\n' "$listeners" | awk -v p=":$PORT" '$4 ~ (p "$") {print $4; exit}')"
  case "$bound" in
    0.0.0.0:"$PORT"|\*:"$PORT"|\[::\]:"$PORT")
      echo "监听层: OK - $bound（公网地址）" ;;
    *)
      echo "监听层: FAIL - $bound（仅回环/特定地址）；检查 AGENT_SERVER_HOST，应为 0.0.0.0";
      failed=1 ;;
  esac
else
  echo "监听层: FAIL - 没有进程监听 TCP $PORT；检查端口和服务日志"
  failed=1
fi

firewall_ok=0
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi '^Status: active'; then
  if ufw status 2>/dev/null | grep -Eq "${PORT}/tcp[[:space:]]+ALLOW"; then
    echo "防火墙层: OK - UFW 已放行 TCP $PORT"
    firewall_ok=1
  else
    echo "防火墙层: FAIL - UFW 已启用但未放行 TCP $PORT；执行 sudo ufw allow $PORT/tcp"
  fi
elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  if firewall-cmd --list-ports | tr ' ' '\n' | grep -qx "$PORT/tcp"; then
    echo "防火墙层: OK - firewalld 已放行 TCP $PORT"
    firewall_ok=1
  else
    echo "防火墙层: FAIL - firewalld 已启用但未放行 TCP $PORT；执行 sudo firewall-cmd --permanent --add-port=$PORT/tcp && sudo firewall-cmd --reload"
  fi
elif command -v iptables >/dev/null 2>&1; then
  policy="$(iptables -S INPUT 2>/dev/null | awk '$1=="-P" {print $3; exit}')"
  if [ "$policy" = "ACCEPT" ] || iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; then
    echo "防火墙层: OK - iptables 允许 TCP $PORT"
    firewall_ok=1
  else
    echo "防火墙层: FAIL - iptables 未发现 TCP $PORT 的允许规则；执行 sudo iptables -I INPUT -p tcp --dport $PORT -j ACCEPT"
  fi
else
  echo "防火墙层: OK - 本机无 UFW/firewalld/iptables，只剩云厂商安全组这一层"
  firewall_ok=1
fi
[ "$firewall_ok" -eq 1 ] || failed=1

if [ "$failed" -ne 0 ]; then
  echo "结论: 未通过。若以上三层全 OK 但外网仍不通，请到云厂商控制台放行 TCP $PORT。"
  exit 1
fi
echo "结论: 三层正常；若外网仍不通，问题在云厂商安全组，请到控制台放行 TCP $PORT。"
