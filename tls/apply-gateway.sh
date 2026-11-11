#!/usr/bin/env bash
# Gateway-only refresh used after config-only commits (e.g. the
# /members/ HTTP->HTTPS redirect). Recreates ONLY the gateway container
# (deploy.sh's full rebuild + RAM gate is deliberately not used for a
# config swap) and runs the full verification battery, including the
# members-only redirect and proof the other services did not move.
set -u
cd /home/deploy/live-servers

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
  C="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:443/$p/health 2>/dev/null)"
  case "$p" in
    usb)     U=http://127.0.0.1:443/usb/api/status ;;
    aircore) U=http://127.0.0.1:443/aircore/api/state ;;
    *)       U=http://127.0.0.1:443/$p/health ;;
  esac
  C="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$U")"
  expect "http $p" "$C" "200"
done
C="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:443/cport/metrics)"
expect "http cport" "$C" "200"

echo "=== HTTPS (loopback, full validation) ==="
C="$(curl -s --resolve members.911transedu.net:443:127.0.0.1 -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/members/health)"
expect "https /members/health" "$C" "200"
C="$(curl -s --resolve members.911transedu.net:443:127.0.0.1 -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/members/)"
expect "https /members/ (no redirect loop)" "$C" "200"

echo "=== AUDITED APP (separate stack) ==="
C="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:80/health)"
expect "audited /health" "$C" "200"

echo "=== APPLY DONE (fail=$FAIL) ==="
exit $FAIL
