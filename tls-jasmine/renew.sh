#!/usr/bin/env bash
# Weekly renewal for the jasmine.911transedu.net Let's Encrypt cert.
# Installed into the host crontab by tls-jasmine/issue.sh (issue step).
set -eu
cd /opt/live-servers
FQDN=jasmine.911transedu.net

docker run --rm \
  -v "$PWD/tls-jasmine-state/letsencrypt:/etc/letsencrypt" \
  -v "$PWD/tls-jasmine-state/webroot:/var/www/acme" \
  certbot/certbot renew --webroot -w /var/www/acme

cp "tls-jasmine-state/letsencrypt/live/$FQDN/fullchain.pem" tls-jasmine-state/certs/fullchain.pem
cp "tls-jasmine-state/letsencrypt/live/$FQDN/privkey.pem" tls-jasmine-state/certs/privkey.pem
chmod 600 tls-jasmine-state/certs/privkey.pem

docker exec live-gateway-jasmine nginx -s reload \
  || docker-compose --profile jasmine up -d gateway-jasmine
echo "renewed/reloaded $(date -u +%FT%TZ)"
