#!/usr/bin/env bash
# Weekly renewal for the members.911transedu.net Let's Encrypt cert.
# Installed in the deploy user's crontab by tls/setup.sh:
#   17 4 * * 1 cd /home/deploy/live-servers && bash tls/renew.sh >> tls-state/renew.log 2>&1
# Also runnable by hand via the tls.yml workflow (action=renew).
#
# Lego renews only when the cert is within 30 days of expiry; when it
# does, the TLS-ALPN-01 challenge is answered through the gateway's
# stream layer exactly as in tls/issue.sh. On an actual renewal the new
# cert is copied into tls-state/certs/ and the gateway is reloaded.
set -u
cd /home/deploy/live-servers
mkdir -p tls-state/lego tls-state/certs

echo "--- renew run $(date -u '+%Y-%m-%dT%H:%M:%SZ') ---"
# Ownership normalization (see tls/issue.sh): lego state must stay
# deploy-owned or this script cannot read the cert it just renewed.
docker run --rm --entrypoint chown \
  -v /home/deploy/live-servers/tls-state/lego:/lego-data \
  nginx:alpine -R "$(id -u):$(id -g)" /lego-data
# Lego v5 folded renewal into 'run' (get or renew): --renew-days 30
# makes it renew only when <=30 days remain and no-op otherwise.
docker run --rm --network host --user "$(id -u):$(id -g)" \
  -v /home/deploy/live-servers/tls-state/lego:/lego-data \
  goacme/lego:latest \
  run \
  --path /lego-data \
  --email taylorsprecher@gmail.com \
  --accept-tos \
  --tls --tls.address 127.0.0.1:18444 \
  --domains members.911transedu.net \
  --renew-days 30
RC=$?
if [ $RC -ne 0 ]; then echo "RENEW FAILED rc=$RC"; exit $RC; fi

SRC=tls-state/lego/certificates/members.911transedu.net.crt
if [ -f "$SRC" ] && [ "$SRC" -nt tls-state/certs/fullchain.pem ]; then
  cp "$SRC" tls-state/certs/fullchain.pem
  cp tls-state/lego/certificates/members.911transedu.net.key tls-state/certs/privkey.pem
  chmod 600 tls-state/certs/privkey.pem
  docker exec live-gateway nginx -t && docker exec live-gateway nginx -s reload
  echo "cert renewed and gateway reloaded:"
  openssl x509 -in tls-state/certs/fullchain.pem -noout -dates
else
  echo "no renewal due — installed cert unchanged"
fi
