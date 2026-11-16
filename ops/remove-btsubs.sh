#!/usr/bin/env bash
# Remove the Braintree subscriptions service (owner direction,
# 2026-11-15: "PayPal can't be used" — Braintree is PayPal, so the
# service comes out now, the same day it was deployed).
#
# What this does, in order:
#   1. Stops and removes the live-btsubs container and its image.
#      (The repo state rsynced just before this run no longer
#      defines the btsubs service in docker-compose.yml, no longer
#      carries services/braintree-subscriptions, and the gateway no
#      longer routes /subs/.)
#   2. Rebuilds ONLY the members service (its app.py / apps.html /
#      README no longer reference the service or /subs/).
#   3. Force-recreates the gateway so the /subs/ location is gone.
#   4. Verifies: btsubs loopback port dead, /subs/ answers 404 via
#      the gateway, members + portal + files + celltower
#      regressions, and the audited production app untouched.
#
# The inert /data/btsubs.db file on the members-data volume (an
# empty database the never-configured service created at boot) is
# left in place; nothing reads it. deploy.sh's RAM gate is not
# edited or lowered; this is a one-service removal.
set -eu
cd /home/deploy/live-servers

echo "=== MEMORY BEFORE ==="
free -m

echo "=== REMOVE live-btsubs CONTAINER + IMAGE ==="
docker rm -f live-btsubs || true
docker rmi live-servers-btsubs || true
docker rmi live-btsubs || true

echo "=== BUILD + UP members only ==="
docker compose up -d --build members

echo "=== RECREATE GATEWAY (drops the /subs/ location) ==="
docker compose up -d --force-recreate gateway

wait_code() {
  # $1 = url, $2 = want, $3 = attempts — patience for the published-port
  # blackhole window observed on this host 2026-11-13 (a recreated
  # container's loopback port can stay unreachable for minutes while
  # the process is already up). The assertion stays strict.
  local url="$1" want="$2" attempts="${3:-60}" code="000" i
  for i in $(seq 1 "$attempts"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")" || code="000"
    [ "$code" = "$want" ] && break
    sleep 5
  done
  echo "$code"
}

wait_https() {
  # $1 = path on members.911transedu.net — HTTPS via loopback resolve,
  # patient like wait_code (the gateway was just force-recreated).
  local path="$1" want="$2" attempts="${3:-60}" code="000" i
  for i in $(seq 1 "$attempts"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 --resolve members.911transedu.net:443:127.0.0.1 "https://members.911transedu.net${path}")" || code="000"
    [ "$code" = "$want" ] && break
    sleep 5
  done
  echo "$code"
}

echo "=== BTSUBS PORT MUST BE DEAD (loopback :18122) ==="
code="$(wait_code http://127.0.0.1:18122/subs/health 000 12)"
echo "CHECK old-btsubs port ${code} (want 000 — nothing listening)"
[ "$code" = "000" ]

echo "=== /subs/ VIA GATEWAY MUST BE 404 ==="
code="$(wait_https /subs/ 404)"
echo "CHECK subs gateway ${code} (want 404)"
[ "$code" = "404" ]
code="$(wait_https /subs/health 404)"
echo "CHECK subs-health gateway ${code} (want 404)"
[ "$code" = "404" ]

echo "=== MEMBERS SERVICE (direct loopback :18109) ==="
code="$(wait_code http://127.0.0.1:18109/members/health 200)"
echo "CHECK members-health direct ${code} (want 200)"
[ "$code" = "200" ]
code="$(wait_code http://127.0.0.1:18109/members/apps 200 30)"
echo "CHECK apps direct ${code} (want 200)"
[ "$code" = "200" ]

echo "=== GATEWAY REGRESSION CHECKS (through nginx on :443) ==="
for pair in \
  "members-health http://127.0.0.1:443/members/health" \
  "members-home http://127.0.0.1:443/members/" \
  "buy-ccce http://127.0.0.1:443/members/buy/ccce" \
  "apps-hub http://127.0.0.1:443/members/apps" \
  "portal http://127.0.0.1:443/portal/health" \
  "files http://127.0.0.1:443/files/health" \
  "celltower http://127.0.0.1:443/celltower/api/health" ; do
  name="${pair%% *}"; url="${pair#* }"
  c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${url}")" || c="000"
  echo "CHECK ${name} ${c}"
done

echo "=== AUDITED APP (separate production stack — must be untouched) ==="
APPCODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:80/health)" || APPCODE="000"
echo "CHECK audited-app ${APPCODE}"

echo "=== MEMORY AFTER ==="
free -m
docker compose ps members gateway || true
echo "=== REMOVE-BTSUBS DONE ==="
