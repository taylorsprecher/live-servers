#!/usr/bin/env bash
# Targeted deploy for the Braintree subscriptions service ONLY
# (services/braintree-subscriptions — the fresh subscription script
# that replaces Stripe, owner direction 2026-11-15).
#
# Builds and recreates ONLY the btsubs service, REMOVES the old
# Stripe subscriptions container (live-subs) for good, then
# force-recreates the gateway so nginx loads the repointed /subs/
# location. The members service is NOT rebuilt or recreated; the
# members-data volume is shared, never reset. deploy.sh's RAM gate
# is not edited or lowered — a one-service build is proportionate,
# the same reasoning as ops/deploy-members-apps.sh.
set -eu
cd /home/deploy/live-servers

echo "=== MEMORY BEFORE ==="
free -m

echo "=== REMOVE the Stripe subscriptions container (live-subs) ==="
docker stop live-subs >/dev/null 2>&1 || true
docker rm live-subs >/dev/null 2>&1 || true
echo "live-subs removed (or was already gone)"

echo "=== BUILD + UP btsubs only ==="
docker compose up -d --build btsubs

echo "=== RECREATE gateway (loads the repointed /subs/ location) ==="
docker compose up -d --force-recreate gateway

wait_code() {
  # $1 = url, $2 = want, $3 = attempts — patience for the published-port
  # blackhole window observed on this host 2026-11-13. The assertion
  # stays strict.
  local url="$1" want="$2" attempts="${3:-100}" code="000" i
  for i in $(seq 1 "$attempts"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")" || code="000"
    [ "$code" = "$want" ] && break
    sleep 5
  done
  echo "$code"
}

wait_https() {
  # $1 = path on the members vhost, $2 = want, $3 = attempts — the
  # gateway was just force-recreated, so it needs the same patience
  # (the Stripe subs deploy run #1 on 2026-11-15 failed its gateway
  # check with 000 while the gateway was still coming up).
  local path="$1" want="$2" attempts="${3:-60}" code="000" i
  for i in $(seq 1 "$attempts"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 --resolve members.911transedu.net:443:127.0.0.1 "https://members.911transedu.net${path}")" || code="000"
    [ "$code" = "$want" ] && break
    sleep 5
  done
  echo "$code"
}

echo "=== WAIT FOR btsubs health (direct loopback :18122) ==="
code="$(wait_code http://127.0.0.1:18122/subs/health 200)"
echo "CHECK btsubs-health direct ${code} (want 200)"
[ "$code" = "200" ]

echo "=== BTSUBS PAGE DIRECT (loopback) ==="
code="$(wait_code http://127.0.0.1:18122/subs/ 200 30)"
echo "CHECK btsubs page direct ${code} (want 200)"
[ "$code" = "200" ]

echo "=== OLD STRIPE SUBS PORT MUST BE GONE (:18121) ==="
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:18121/subs/health)" || code="000"
echo "CHECK old-subs port ${code} (want 000 — nothing listening)"
[ "$code" = "000" ]

echo "=== BTSUBS VIA GATEWAY (:443) ==="
code="$(wait_https /subs/ 200)"
echo "CHECK btsubs gateway ${code} (want 200)"
[ "$code" = "200" ]

code="$(wait_https /subs/health 200 30)"
echo "CHECK btsubs-health gateway ${code} (want 200)"
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

echo "=== MEMORY AFTER + BTSUBS FOOTPRINT ==="
free -m
docker compose ps btsubs
docker stats --no-stream --format 'MEM {{.MemUsage}} CPU {{.CPUPerc}} NAME {{.Name}}' live-btsubs || true
echo "=== DEPLOY-BTSUBS DONE ==="
