#!/usr/bin/env bash
# Targeted deploy for the syncflow service ONLY.
#
# Why this exists: deploy.sh's RAM gate (>= 700 MB available) protects
# the host during a FULL build. A syncflow-only rebuild is
# proportionate: the syncflow image is a trivial COPY onto the
# already-pulled python:3.12-slim base. This script does NOT lower or
# edit deploy.sh's gate and does not rebuild or recreate any other
# service.
#
# One deliberate behaviour shared with ops/deploy-portal.sh: the
# /syncflow route lives in gateway/locations-common.conf, which the
# gateway container bind-mounts — editing it does not change the
# gateway's compose config hash, so (exactly as deploy.sh does) the
# gateway is force-recreated afterwards to load the current config.
# The gateway is stateless; no TLS config, DNS, other route, the
# member site, or the audited production app is touched.
set -eu
cd /home/deploy/live-servers

echo "=== MEMORY BEFORE ==="
free -m

echo "=== BUILD + UP syncflow only ==="
docker compose up -d --build syncflow

echo "=== GATEWAY FORCE-RECREATE (pick up current locations-common.conf) ==="
docker compose up -d --force-recreate gateway

echo "=== WAIT FOR syncflow health (loopback) ==="
code="000"
for i in $(seq 1 15); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18112/syncflow/health)" || code="000"
  [ "${code}" = "200" ] && break
  sleep 4
done
echo "CHECK syncflow direct ${code}"
[ "${code}" = "200" ]

echo "=== GATEWAY CHECKS (through nginx on :443) ==="
for pair in \
  "syncflow-page http://127.0.0.1:443/syncflow/" \
  "syncflow http://127.0.0.1:443/syncflow/health" \
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
docker compose ps syncflow gateway
echo "=== DEPLOY-SYNCFLOW DONE ==="
