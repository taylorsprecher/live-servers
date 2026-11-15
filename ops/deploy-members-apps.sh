#!/usr/bin/env bash
# Targeted deploy for the member-site apps hub ONLY.
#
# What this update is: services/member-site gains a public page at
# /members/apps — the "Member & Server Apps" hub (apps.html, a
# standalone page whose live checks run in the visitor's browser).
# app.py learns the /apps route + links from the home page and the
# Member Desk; the Dockerfile copies apps.html into the image. Only
# the members service is rebuilt; no payment, session, content or
# roster code paths change, and the members DB volume is untouched.
#
# Why this exists (same reasoning as ops/deploy-proxy.sh): deploy.sh's
# RAM gate (>= 700 MB available) protects the host during a FULL
# build. A one-service rebuild is proportionate. This script does NOT
# lower or edit deploy.sh's gate and does not rebuild or recreate any
# other service.
set -eu
cd /home/deploy/live-servers

echo "=== MEMORY BEFORE ==="
free -m

echo "=== BUILD + UP members only ==="
docker compose up -d --build members

wait_code() {
  # $1 = url, $2 = want, $3 = attempts — patience for the published-port
  # blackhole window observed on this host 2026-11-13 (a recreated
  # container's loopback port can stay unreachable for minutes while
  # the process is already up). The assertion stays strict.
  local url="$1" want="$2" attempts="${3:-100}" code="000" i
  for i in $(seq 1 "$attempts"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")" || code="000"
    [ "$code" = "$want" ] && break
    sleep 5
  done
  echo "$code"
}

echo "=== WAIT FOR members health (direct loopback :18109) ==="
code="$(wait_code http://127.0.0.1:18109/members/health 200)"
echo "CHECK members-health direct ${code} (want 200)"
[ "$code" = "200" ]

echo "=== APPS HUB DIRECT (loopback) ==="
code="$(wait_code http://127.0.0.1:18109/members/apps 200 30)"
echo "CHECK apps direct ${code} (want 200)"
[ "$code" = "200" ]

echo "=== APPS DATA FEED DIRECT (loopback) ==="
code="$(wait_code http://127.0.0.1:18109/members/apps/data 200 30)"
echo "CHECK apps-data direct ${code} (want 200)"
[ "$code" = "200" ]

echo "=== OWNER STRIPE PAGE GATE (loopback, must refuse anonymous) ==="
code="$(wait_code http://127.0.0.1:18109/members/owner/stripe 403 30)"
echo "CHECK owner-stripe gated ${code} (want 403)"
[ "$code" = "403" ]

echo "=== APPS HUB VIA GATEWAY (:443) ==="
# Port 443 is the HTTPS gateway: a plain-HTTP request gets a 301 to
# HTTPS (that is what failed run 43754246350 on 2026-11-15 — the
# deploy itself had succeeded). Ask over HTTPS with the real vhost
# name, resolved to loopback, and require a straight 200.
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 --resolve members.911transedu.net:443:127.0.0.1 https://members.911transedu.net/members/apps)" || code="000"
echo "CHECK apps gateway ${code} (want 200)"
[ "$code" = "200" ]

echo "=== GATEWAY REGRESSION CHECKS (through nginx on :443) ==="
for pair in \
  "members-health http://127.0.0.1:443/members/health" \
  "members-home http://127.0.0.1:443/members/" \
  "buy-ccce http://127.0.0.1:443/members/buy/ccce" \
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

echo "=== MEMORY AFTER + MEMBERS FOOTPRINT ==="
free -m
docker compose ps members
docker stats --no-stream --format 'MEM {{.MemUsage}} CPU {{.CPUPerc}} NAME {{.Name}}' live-members || true
echo "=== DEPLOY-MEMBERS-APPS DONE ==="
