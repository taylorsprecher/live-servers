#!/usr/bin/env bash
# Targeted deploy for the aircore service ONLY.
#
# Added for the Stage 3 redesign deploy (2026-11-20): aircore had no
# targeted deploy tooling. Modeled line-for-line on ops/deploy-portal.sh.
#
# Why this exists: deploy.sh's RAM gate (>= 700 MB available) protects
# the host during a FULL build. An aircore-only rebuild is
# proportionate: the image is a trivial COPY onto the already-pulled
# python:3.12-slim base. This script does NOT lower or edit deploy.sh's
# gate and does not rebuild or recreate any other service.
#
# aircore has no /health route; its liveness contract is the AirCore
# state API (GET /aircore/api/state -> 200 JSON with routes + policy).
#
# The gateway is force-recreated afterwards (exactly as deploy.sh and
# the other targeted scripts do) so it loads the current
# locations-common.conf. The gateway is stateless; no TLS config, DNS,
# other route, the member site, or the audited production app is
# touched.
set -eu
cd /home/deploy/live-servers

echo "=== MEMORY BEFORE ==="
free -m

echo "=== BUILD + UP aircore only ==="
docker compose up -d --build aircore

echo "=== GATEWAY FORCE-RECREATE (pick up current locations-common.conf) ==="
docker compose up -d --force-recreate gateway

echo "=== WAIT FOR aircore state API (loopback) ==="
code="000"
for i in $(seq 1 15); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18105/aircore/api/state)" || code="000"
  [ "${code}" = "200" ] && break
  sleep 4
done
echo "CHECK aircore direct ${code}"
[ "${code}" = "200" ]

echo "=== GATEWAY CHECKS (through nginx on :443) ==="
for pair in \
  "aircore-page http://127.0.0.1:443/aircore/" \
  "aircore-state http://127.0.0.1:443/aircore/api/state" \
  "portal http://127.0.0.1:443/portal/health" \
  "members http://127.0.0.1:443/members/health" \
  "maclan http://127.0.0.1:443/maclan/health" \
  "specmatrix http://127.0.0.1:443/specmatrix/health" \
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
docker compose ps aircore gateway
echo "=== DEPLOY-AIRCORE DONE ==="
