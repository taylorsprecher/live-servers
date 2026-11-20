#!/usr/bin/env bash
# Targeted deploy for the wyze service ONLY (demo image tier).
#
# Added for the Stage 3 redesign deploy (2026-11-20): wyze had no
# targeted deploy tooling. Modeled line-for-line on ops/deploy-portal.sh.
#
# Why this exists: deploy.sh's RAM gate (>= 700 MB available) protects
# the host during a FULL build. A wyze-only rebuild is proportionate:
# the compose service builds Dockerfile.demo (the credential-free mock
# status demo the fleet runs) onto the already-pulled python:3.12-slim
# base. This script does NOT lower or edit deploy.sh's gate and does
# not rebuild or recreate any other service. The real bridge code and
# its secrets are not touched; no credentials are delivered.
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

echo "=== BUILD + UP wyze only ==="
docker compose up -d --build wyze

echo "=== GATEWAY FORCE-RECREATE (pick up current locations-common.conf) ==="
docker compose up -d --force-recreate gateway

echo "=== WAIT FOR wyze health (loopback) ==="
code="000"
for i in $(seq 1 15); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18103/wyze/health)" || code="000"
  [ "${code}" = "200" ] && break
  sleep 4
done
echo "CHECK wyze direct ${code}"
[ "${code}" = "200" ]

echo "=== GATEWAY CHECKS (through nginx on :443) ==="
for pair in \
  "wyze-page http://127.0.0.1:443/wyze/" \
  "wyze http://127.0.0.1:443/wyze/health" \
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
docker compose ps wyze gateway
echo "=== DEPLOY-WYZE DONE ==="
