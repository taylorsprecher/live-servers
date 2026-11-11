#!/usr/bin/env bash
# Stage B: obtain the real Let's Encrypt cert for members.911transedu.net
# via TLS-ALPN-01 and install it into the gateway.
#
# Why TLS-ALPN-01: HTTP-01 needs port 80, which belongs to the audited
# production stack (untouchable), and there are no GoDaddy API keys on
# file for DNS-01. The gateway's stream layer routes ALPN "acme-tls/1"
# on :443 to 127.0.0.1:18444, where this lego run listens (host network).
#
# On success: copies cert+key into tls-state/certs/, reloads the gateway
# (no container restart), and verifies https with FULL chain validation.
set -u
cd /home/deploy/live-servers
mkdir -p tls-state/lego tls-state/certs
chmod 700 tls-state/lego

echo "=== LEGO ISSUE (TLS-ALPN-01 via :443 stream -> 127.0.0.1:18444) ==="
ls -ld tls-state/lego tls-state/certs
# Ownership: lego's image runs as root, so its state files come out
# root-owned inside the deploy-owned (mode 700) tls-state/lego dir and
# the deploy user cannot even read them afterwards. Normalize with a
# throwaway root container, then run lego AS the deploy user.
docker run --rm --entrypoint chown \
  -v /home/deploy/live-servers/tls-state/lego:/lego-data \
  nginx:alpine -R "$(id -u):$(id -g)" /lego-data
# NOTE: the mount target is /lego-data, not /lego — in the goacme/lego
# image /lego is the binary itself, so a directory cannot mount there.
# Lego v5: all account/challenge flags belong to the 'run' subcommand
# ("get or renew"); there is no separate global flag set or renew cmd.
docker run --rm --network host --user "$(id -u):$(id -g)" \
  -v /home/deploy/live-servers/tls-state/lego:/lego-data \
  goacme/lego:latest \
  run \
  --path /lego-data \
  --email taylorsprecher@gmail.com \
  --accept-tos \
  --tls --tls.address 127.0.0.1:18444 \
  --domains members.911transedu.net
RC=$?
if [ $RC -ne 0 ]; then echo "LEGO ISSUE FAILED rc=$RC — placeholder/previous cert still in place"; exit $RC; fi

SRC=tls-state/lego/certificates
ls -l "$SRC" || true
if [ ! -f "$SRC/members.911transedu.net.crt" ]; then
  echo "LEGO reported success but $SRC/members.911transedu.net.crt is missing — aborting"
  exit 1
fi

echo "=== INSTALL CERT ==="
cp "$SRC/members.911transedu.net.crt" tls-state/certs/fullchain.pem
cp "$SRC/members.911transedu.net.key" tls-state/certs/privkey.pem
chmod 600 tls-state/certs/privkey.pem
openssl x509 -in tls-state/certs/fullchain.pem -noout -issuer -subject -dates

echo "=== GATEWAY RELOAD (cert swap, no restart) ==="
docker exec live-gateway nginx -t && docker exec live-gateway nginx -s reload
echo "reload exit code: $?"
sleep 2

echo "=== VERIFY (loopback, FULL chain validation, no -k) ==="
curl -s --resolve members.911transedu.net:443:127.0.0.1 \
  https://members.911transedu.net/members/health \
  -o /dev/null -w 'https /members/health %{http_code}\n' --max-time 10
echo "--- chain as presented ---"
echo | openssl s_client -connect 127.0.0.1:443 -servername members.911transedu.net 2>/dev/null \
  | grep -E '^(issuer|subject|  [0-9] s:|  [0-9] i:|Verify return code)' | head -12

echo "=== HTTP SERVICE STILL INTACT ==="
curl -s -o /dev/null -w 'http :443 /members/health %{http_code}\n'   --max-time 5 http://127.0.0.1:443/members/health
curl -s -o /dev/null -w 'http :443 /specmatrix/health %{http_code}\n' --max-time 5 http://127.0.0.1:443/specmatrix/health
curl -s -o /dev/null -w 'audited :80 /health %{http_code}\n'          --max-time 5 http://127.0.0.1:80/health

echo "=== ISSUE DONE ==="
