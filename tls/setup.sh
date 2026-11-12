#!/usr/bin/env bash
# Stage A of the gateway HTTPS setup: members.911transedu.net
# (2026-11-11), extended 2026-11-12 to the eight service subdomains
# (tls/lib.sh). Runs on the host in /home/deploy/live-servers (via the
# tls.yml workflow).
#
# What it does:
#   1. Creates the host-only TLS state dirs (tls-state/, never in git).
#   2. If no cert exists yet for a subdomain, installs a short-lived
#      SELF-SIGNED placeholder so nginx can start with the TLS server
#      blocks. tls/issue.sh replaces them with real Let's Encrypt
#      certs minutes later (per subdomain, as its DNS lands).
#   3. Validates the new gateway config in a throwaway container BEFORE
#      touching the running gateway.
#   4. Recreates ONLY the gateway container and runs the check battery:
#      the original HTTP service must still answer on :443 exactly as
#      before (now through the stream multiplexer), HTTPS must answer
#      on every subdomain SNI, and the audited app must be untouched.
#   5. Installs the weekly renewal entry in the deploy user's crontab
#      (tls/renew.sh covers every subdomain in tls/lib.sh).
set -u
cd /home/deploy/live-servers
source tls/lib.sh

echo "=== PRECONDITIONS ==="
for f in gateway/nginx.conf gateway/locations-common.conf \
         gateway/locations-members-proxy.conf gateway/locations-members-redirect.conf; do
  if [ ! -f "$f" ]; then echo "MISSING $f — aborting, gateway untouched"; exit 1; fi
done

mkdir -p tls-state/certs tls-state/lego
chmod 700 tls-state/lego

if [ ! -f tls-state/certs/fullchain.pem ] || [ ! -f tls-state/certs/privkey.pem ]; then
  echo "=== SELF-SIGNED PLACEHOLDER CERT (replaced by tls/issue.sh) ==="
  openssl req -x509 -newkey rsa:2048 -nodes -days 7 \
    -keyout tls-state/certs/privkey.pem \
    -out tls-state/certs/fullchain.pem \
    -subj "/CN=members.911transedu.net" \
    -addext "subjectAltName=DNS:members.911transedu.net" 2>&1 | tail -1
  chmod 600 tls-state/certs/privkey.pem
else
  echo "=== EXISTING CERT KEPT ==="
fi
openssl x509 -in tls-state/certs/fullchain.pem -noout -issuer -subject -dates

echo "=== SERVICE SUBDOMAIN PLACEHOLDER CERTS (2026-11-12) ==="
for name in $(tls_new_names); do
  fqdn="$(tls_fqdn "$name")"
  dir="$(tls_certdir "$name")"
  mkdir -p "$dir"
  if [ ! -f "$dir/fullchain.pem" ] || [ ! -f "$dir/privkey.pem" ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 7 \
      -keyout "$dir/privkey.pem" \
      -out "$dir/fullchain.pem" \
      -subj "/CN=$fqdn" \
      -addext "subjectAltName=DNS:$fqdn" 2>&1 | tail -1
    chmod 600 "$dir/privkey.pem"
    echo "placeholder installed for $fqdn"
  else
    echo "existing cert kept for $fqdn"
  fi
  openssl x509 -in "$dir/fullchain.pem" -noout -issuer -subject -dates
done

echo "=== CONFIG TEST (throwaway container, running gateway untouched) ==="
docker run --rm \
  -v "$PWD/gateway/nginx.conf:/etc/nginx/nginx.conf:ro" \
  -v "$PWD/gateway/locations-common.conf:/etc/nginx/locations-common.conf:ro" \
  -v "$PWD/gateway/locations-members-proxy.conf:/etc/nginx/locations-members-proxy.conf:ro" \
  -v "$PWD/gateway/locations-members-redirect.conf:/etc/nginx/locations-members-redirect.conf:ro" \
  -v "$PWD/tls-state/certs:/etc/nginx/certs:ro" \
  nginx:alpine nginx -t
if [ $? -ne 0 ]; then echo "CONFIG TEST FAILED — aborting, gateway untouched"; exit 1; fi

echo "=== GATEWAY FORCE-RECREATE ==="
docker compose up -d --force-recreate gateway
echo "gateway recreate exit code: $?"
sleep 3
docker ps --format '{{.Names}} {{.Status}}' | grep -E 'live-gateway' || true

check() {
  local name="$1" url="$2" want="$3" extra="${4:-}"
  local code i
  code="000"
  for i in $(seq 1 8); do
    code="$(curl -s $extra -o /dev/null -w '%{http_code}' --max-time 5 "$url")" || code="000"
    [ "$code" = "$want" ] && break
    sleep 3
  done
  echo "CHECK $name $code (want $want)"
  [ "$code" = "$want" ]
}
FAIL=0
echo "=== CHECKS ==="
check http-members-health  http://127.0.0.1:443/members/health   200 || FAIL=1
check http-specmatrix     http://127.0.0.1:443/specmatrix/health 200 || FAIL=1
check http-maclan         http://127.0.0.1:443/maclan/health     200 || FAIL=1
# HTTPS via SNI on loopback; -k because this stage may still be the
# self-signed placeholder. issue.sh re-verifies with full validation.
check https-members-health https://members.911transedu.net/members/health 200 \
  "--resolve members.911transedu.net:443:127.0.0.1 -k" || FAIL=1
# Service subdomains (-k: placeholders may still be in place at this
# stage; issue.sh re-verifies each with full validation).
for name in $(tls_new_names); do
  fqdn="$(tls_fqdn "$name")"
  check "https-$name" "https://$fqdn$(tls_health_path "$name")" 200 \
    "--resolve $fqdn:443:127.0.0.1 -k" || FAIL=1
done
check audited-app         http://127.0.0.1:80/health             200 || FAIL=1

echo "=== RENEWAL CRONTAB (deploy user) ==="
CRON_LINE="17 4 * * 1 cd /home/deploy/live-servers && bash tls/renew.sh >> /home/deploy/live-servers/tls-state/renew.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'tls/renew\.sh' ; echo "$CRON_LINE" ) | crontab -
crontab -l | grep 'tls/renew' || echo "WARN: renewal entry not present after install"
if systemctl is-active cron >/dev/null 2>&1; then
  echo "cron service: active"
else
  echo "WARN: cron service not reported active — renewal also runnable via tls.yml (action=renew)"
fi

echo "=== SETUP DONE (fail=$FAIL) ==="
exit $FAIL
