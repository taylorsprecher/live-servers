#!/usr/bin/env bash
# Targeted deploy for the Files Library service ONLY — CODE ONLY.
#
# Added for the Stage 3 redesign deploy (2026-11-20). This is NOT
# ops/deploy-files-library.sh: that script re-stages the file bytes
# into the fileslib-data volume from a delivered archive (and rebuilds
# portal alongside). The redesign changes only the fileslib code; the
# production library.json and the 747 staged files in the volume are
# data and are NOT touched here — no archive is delivered, nothing in
# the volume is deleted or rewritten.
#
# Why this exists: deploy.sh's RAM gate (>= 700 MB available) protects
# the host during a FULL build. A fileslib-only rebuild is
# proportionate: the image is a trivial COPY onto the already-pulled
# python:3.12-slim base. This script does NOT lower or edit deploy.sh's
# gate and does not rebuild or recreate any other service.
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

echo "=== BUILD + UP fileslib only (code only; volume untouched) ==="
docker compose up -d --build fileslib

echo "=== GATEWAY FORCE-RECREATE (pick up current locations-common.conf) ==="
docker compose up -d --force-recreate gateway

echo "=== WAIT FOR fileslib health (loopback) ==="
code="000"
for i in $(seq 1 15); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18113/files/health)" || code="000"
  [ "${code}" = "200" ] && break
  sleep 4
done
echo "CHECK fileslib direct ${code}"
[ "${code}" = "200" ]

echo "=== GATEWAY CHECKS (through nginx on :443) ==="
for pair in \
  "files-page http://127.0.0.1:443/files/" \
  "files-health http://127.0.0.1:443/files/health" \
  "members http://127.0.0.1:443/members/health" \
  "portal http://127.0.0.1:443/portal/" \
  "syncflow http://127.0.0.1:443/syncflow/health" ; do
  name="${pair%% *}"; url="${pair#* }"
  c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${url}")" || c="000"
  echo "CHECK ${name} ${c}"
done

echo "=== SAMPLE DOWNLOAD (f0001, want 200 and 351767 bytes) ==="
curl -s -o /dev/null -w 'CHECK sample-download %{http_code} %{size_download}\n' --max-time 30 http://127.0.0.1:443/files/dl/f0001

echo "=== AUDITED APP (separate production stack — must be untouched) ==="
APPCODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:80/health)" || APPCODE="000"
echo "CHECK audited-app ${APPCODE}"

echo "=== MEMORY AFTER ==="
free -m
docker compose ps fileslib gateway
echo "=== DEPLOY-FILESLIB DONE ==="
