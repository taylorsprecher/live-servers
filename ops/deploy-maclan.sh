#!/usr/bin/env bash
# Targeted deploy for the maclan service ONLY.
#
# Added for the Stage 3 redesign deploy (2026-11-20): maclan had no
# targeted deploy tooling. Modeled line-for-line on ops/deploy-portal.sh.
#
# Why this exists: deploy.sh's RAM gate (>= 700 MB available) protects
# the host during a FULL build. A maclan-only rebuild is proportionate:
# the frontend is PREBUILT (frontend/dist is committed; Node is not in
# the image) and the backend's pip layer is unchanged, so the build is
# COPY steps onto the already-pulled python:3.12-slim base. This script
# does NOT lower or edit deploy.sh's gate and does not rebuild or
# recreate any other service. The maclan.db volume is untouched.
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

echo "=== BUILD + UP maclan only ==="
docker compose up -d --build maclan

echo "=== GATEWAY FORCE-RECREATE (pick up current locations-common.conf) ==="
docker compose up -d --force-recreate gateway

echo "=== WAIT FOR maclan health (loopback) ==="
code="000"
for i in $(seq 1 15); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18101/maclan/health)" || code="000"
  [ "${code}" = "200" ] && break
  sleep 4
done
echo "CHECK maclan direct ${code}"
[ "${code}" = "200" ]

echo "=== GATEWAY CHECKS (through nginx on :443) ==="
for pair in \
  "maclan-page http://127.0.0.1:443/maclan/" \
  "maclan http://127.0.0.1:443/maclan/health" \
  "portal http://127.0.0.1:443/portal/health" \
  "members http://127.0.0.1:443/members/health" \
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
docker compose ps maclan gateway
echo "=== DEPLOY-MACLAN DONE ==="
