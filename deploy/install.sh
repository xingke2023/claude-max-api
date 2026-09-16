#!/usr/bin/env bash
set -euo pipefail
cd /home/ubuntu/claude-max-proxy
backup_dir="/var/backups/claude-max-proxy-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir" /var/www/letsencrypt /etc/letsencrypt/renewal-hooks/deploy
cp /etc/systemd/system/claude-max-proxy.service "$backup_dir/"
if [ -e /etc/nginx/sites-available/tokens.xingke888.com ]; then
    cp /etc/nginx/sites-available/tokens.xingke888.com "$backup_dir/"
fi
install -m 644 claude-max-proxy.service /etc/systemd/system/claude-max-proxy.service
systemctl daemon-reload
systemctl restart claude-max-proxy
systemctl is-active --quiet claude-max-proxy
install -m 644 deploy/tokens.xingke888.com.http.conf /etc/nginx/sites-available/tokens.xingke888.com
ln -sfn /etc/nginx/sites-available/tokens.xingke888.com /etc/nginx/sites-enabled/tokens.xingke888.com
nginx -t
systemctl reload nginx
certbot certonly --webroot -w /var/www/letsencrypt -d tokens.xingke888.com --non-interactive --agree-tos
install -m 644 deploy/tokens.xingke888.com.conf /etc/nginx/sites-available/tokens.xingke888.com
nginx -t
systemctl reload nginx
install -m 755 deploy/reload-nginx.sh /etc/letsencrypt/renewal-hooks/deploy/tokens-reload-nginx
printf 'Deployment complete. Backup: %s\n' "$backup_dir"
