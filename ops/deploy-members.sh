#!/usr/bin/env bash
# Targeted deploy for the members service ONLY.
#
# Why this exists: deploy.sh's RAM gate (>= 700 MB available) protects
# the host during a FULL build (several Node images). When the host is
# in steady state just under the gate with no runaway process, a
# members-only rebuild is proportionate: the member-site image is a
# trivial COPY onto the already-pulled python:3.12-slim base. This
# script does NOT lower or edit deploy.sh's gate, does not rebuild or
# recreate any other service, and does not touch the gateway (its
# nginx config is unchanged — /members already proxies to :18109).
set -eu
cd /home/deploy/live-servers

echo "=== MEMORY BEFORE ==="
free -m

echo "=== BUILD + UP members only ==="
docker compose up -d --build members

echo "=== WAIT FOR members health (loopback) ==="
code="000"
for i in $(seq 1 15); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:18109/members/health)" || code="000"
  [ "${code}" = "200" ] && break
  sleep 4
done
echo "CHECK members direct ${code}"
[ "${code}" = "200" ]

echo "=== GATEWAY SPOT CHECKS (read-only) ==="
for pair in \
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
docker compose ps members
echo "=== DEPLOY-MEMBERS DONE ==="
