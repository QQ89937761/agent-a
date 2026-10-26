#!/usr/bin/env bash
# 用法: sudo bash deploy/configure_tls.sh xiaoa.example.com you@example.com [hub端口]
set -euo pipefail

DOMAIN="${1:-}"
EMAIL="${2:-}"
UPSTREAM_PORT="${3:-8443}"
if [[ ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]] || [[ "$DOMAIN" != *.* ]]; then
  echo "用法: sudo bash deploy/configure_tls.sh xiaoa.example.com you@example.com [hub端口]"
  exit 1
fi
if ! [[ "$UPSTREAM_PORT" =~ ^[0-9]+$ ]] || [ "$UPSTREAM_PORT" -lt 1 ] || [ "$UPSTREAM_PORT" -gt 65535 ]; then
  echo "hub 端口必须是 1 到 65535 之间的数字。"
  exit 1
fi
if [[ ! "$EMAIL" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; then
  echo "请提供有效邮箱,用于 Let's Encrypt 到期通知。"
  exit 1
fi

apt-get update -y
apt-get install -y nginx certbot python3-certbot-nginx

cat >/etc/nginx/sites-available/xiaoa <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    client_max_body_size 32m;

    location / {
        # deploy_hub.sh 的 hub 直接在自定义端口提供自签 TLS；这里仅作本机反代。
        proxy_pass https://127.0.0.1:$UPSTREAM_PORT;
        proxy_ssl_verify off;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;
    }
}
EOF

ln -sfn /etc/nginx/sites-available/xiaoa /etc/nginx/sites-enabled/xiaoa
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
certbot --nginx --non-interactive --agree-tos --redirect -m "$EMAIL" -d "$DOMAIN"
systemctl enable --now certbot.timer

echo "HTTPS/WSS 已配置: https://$DOMAIN"
echo "worker 地址: wss://$DOMAIN/worker"
