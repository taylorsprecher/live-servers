#!/usr/bin/env bash
# Gateway-only refresh used after config-only commits (e.g. the
# /members/ HTTP->HTTPS redirect). Recreates ONLY the gateway container
# (deploy.sh's full rebuild + RAM gate is deliberately not used for a
# config swap) and runs the full verification battery, including the
# members-only redirect, the per-subdomain HTTPS checks (2026-11-12),
# and proof the other services did not move.
set -u
cd /home/deploy/live-servers
source tls/lib.sh

echo "=== CONFIG TEST (throwaway container) ==="
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

FAIL=0
expect() { # $1 label, $2 actual, $3 want
  echo "CHECK $1: $2 (want $3)"
  [ "$2" = "$3" ] || FAIL=1
}

echo "=== MEMBERS REDIRECT (HTTP :443) ==="
LOC="$(curl -s -o /dev/null -D - --max-time 5 http://127.0.0.1:443/members/ | grep -i '^location:' | tr -d '\r' | awk '{print $2}')"
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:443/members/)"
expect "GET /members/ code"     "$CODE" "301"
expect "GET /members/ location" "$LOC"  "https://members.911transedu.net/members/"
LOC2="$(curl -s -o /dev/null -D - --max-time 5 http://127.0.0.1:443/members | grep -i '^location:' | tr -d '\r' | awk '{print $2}')"
expect "GET /members location"  "$LOC2" "https://members.911transedu.net/members/"

echo "=== OTHER SERVICES UNMOVED (HTTP :443) ==="
for p in specmatrix maclan xbox wyze usb aircore fmp celltower; do
  # Health paths match deploy.sh's gateway checks exactly.
  case "$p" in
    usb)       U=http://127.0.0.1:443/usb/api/status ;;
    aircore)   U=http://127.0.0.1:443/aircore/api/state ;;
    fmp)       U=http://127.0.0.1:443/fmp/api/health ;;
    celltower) U=http://127.0.0.1:443/celltower/api/health ;;
    *)         U=http://127.0.0.1:443/$p/health ;;
  esac
  C="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$U")"
  expect "http $p" "$C" "200"
done
C="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:443/cport/metrics)"
expect "http cport" "$C" "200"
# Diagnostic only: celltower's own backend, gateway bypassed (the app
# has a history of slow health responses; this separates app from path).
D="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 http://127.0.0.1:18107/celltower/api/health)"
echo "INFO direct celltower backend: $D"

echo "=== HTTPS (loopback, full validation) ==="
C="$(curl -s --resolve members.911transedu.net:443:127.0.0.1 -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/members/health)"
expect "https /members/health" "$C" "200"
C="$(curl -s --resolve members.911transedu.net:443:127.0.0.1 -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/members/)"
expect "https /members/ (no redirect loop)" "$C" "200"

echo "=== HTTPS SERVICE SUBDOMAINS (loopback, full validation) ==="
# A subdomain whose real Let's Encrypt cert is not installed yet (DNS
# still propagating at rollout time) is reported PENDING, not failed;
# once issued, the same check enforces a full-validation 200.
for name in $(tls_new_names); do
  fqdn="$(tls_fqdn "$name")"
  dir="$(tls_certdir "$name")"
  if [ -f "$dir/fullchain.pem" ] && openssl x509 -in "$dir/fullchain.pem" -noout -issuer 2>/dev/null | grep -q "Let's Encrypt"; then
    C="$(curl -s --resolve "$fqdn:443:127.0.0.1" -o /dev/null -w '%{http_code}' --max-time 15 "https://$fqdn$(tls_health_path "$name")")"
    expect "https $fqdn$(tls_health_path "$name")" "$C" "200"
  else
    echo "PENDING https $fqdn — no Let's Encrypt cert installed yet"
  fi
done

echo "=== AUDITED APP (separate stack) ==="
C="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:80/health)"
expect "audited /health" "$C" "200"

echo "=== APPLY DONE (fail=$FAIL) ==="
exit $FAIL
