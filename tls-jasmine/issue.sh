#!/usr/bin/env bash
# TLS setup for jasmine.911transedu.net on the jasmine EC2 instance
# (i-04abda08044c43247). Runs on the host in /opt/live-servers (via
# SSM Run Command). Mirrors the audited host's tls/ pattern at a
# smaller scale: one name, host-only state, placeholder first.
#
#   ./tls-jasmine/issue.sh          create state dirs + a self-signed
#                                   placeholder cert (if none), so the
#                                   gateway-jasmine container can start
#   ./tls-jasmine/issue.sh issue    obtain the real Let's Encrypt cert
#                                   via certbot webroot (needs DNS for
#                                   jasmine.911transedu.net pointing at
#                                   this host and port 80 reachable),
#                                   install it, reload the gateway, and
#                                   install the weekly renewal cron
set -eu
cd /opt/live-servers
FQDN=jasmine.911transedu.net

mkdir -p tls-jasmine-state/certs \
         tls-jasmine-state/webroot/.well-known/acme-challenge \
         tls-jasmine-state/letsencrypt

if [ ! -f tls-jasmine-state/certs/fullchain.pem ] || [ ! -f tls-jasmine-state/certs/privkey.pem ]; then
  echo "=== SELF-SIGNED PLACEHOLDER CERT (replaced by the issue step) ==="
  openssl req -x509 -newkey rsa:2048 -nodes -days 7 \
    -keyout tls-jasmine-state/certs/privkey.pem \
    -out tls-jasmine-state/certs/fullchain.pem \
    -subj "/CN=$FQDN" \
    -addext "subjectAltName=DNS:$FQDN"
  chmod 600 tls-jasmine-state/certs/privkey.pem
else
  echo "=== EXISTING CERT KEPT ==="
fi
openssl x509 -in tls-jasmine-state/certs/fullchain.pem -noout -issuer -subject -dates

if [ "${1:-}" = "issue" ]; then
  echo "=== CERTBOT ISSUE (webroot) ==="
  docker run --rm \
    -v "$PWD/tls-jasmine-state/letsencrypt:/etc/letsencrypt" \
    -v "$PWD/tls-jasmine-state/webroot:/var/www/acme" \
    certbot/certbot certonly --webroot -w /var/www/acme \
    -d "$FQDN" --agree-tos -m taylorsprecher@gmail.com --no-eff-email
  cp "tls-jasmine-state/letsencrypt/live/$FQDN/fullchain.pem" tls-jasmine-state/certs/fullchain.pem
  cp "tls-jasmine-state/letsencrypt/live/$FQDN/privkey.pem" tls-jasmine-state/certs/privkey.pem
  chmod 600 tls-jasmine-state/certs/privkey.pem
  openssl x509 -in tls-jasmine-state/certs/fullchain.pem -noout -issuer -subject -dates
  docker-compose --profile jasmine up -d gateway-jasmine
  docker exec live-gateway-jasmine nginx -s reload || true
  echo "=== WEEKLY RENEWAL CRON ==="
  ( crontab -l 2>/dev/null | grep -v 'tls-jasmine/renew.sh' || true
    echo "17 4 * * 1 /opt/live-servers/tls-jasmine/renew.sh >> /var/log/tls-jasmine-renew.log 2>&1"
  ) | crontab -
  crontab -l | grep tls-jasmine
fi
echo "=== DONE ==="
