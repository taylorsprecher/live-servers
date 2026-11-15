#!/usr/bin/env bash
# Targeted deploy for the Cell Tower LAN dashboard update ONLY.
#
# What this update is: services/cell-tower-lan gains a second,
# self-contained dashboard page (frontend/dashboard2.html — the
# "Site Infrastructure Dashboard", simulated in-page data, no backend
# calls), server.js learns to serve .html files from the frontend
# directory, and the telemetry dashboard (frontend/index.html) links
# to it. Only the celltower service is rebuilt; gateway/nginx.conf is
# unchanged, so no gateway recreate is needed.
#
# Why this exists (same reasoning as ops/deploy-proxy.sh): deploy.sh's
# RAM gate (>= 700 MB available) protects the host during a FULL
# build. A one-service rebuild is proportionate — the celltower image
# is a small Node service. This script does NOT lower or edit
# deploy.sh's gate and does not rebuild or recreate any other service.
set -eu
cd /home/deploy/live-servers

echo "=== MEMORY BEFORE ==="
free -m

echo "=== BUILD + UP celltower only ==="
docker compose up -d --build celltower

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

echo "=== WAIT FOR backend health (direct loopback :18107) ==="
code="$(wait_code http://127.0.0.1:18107/celltower/api/health 200)"
echo "CHECK celltower-health direct ${code} (want 200)"
[ "$code" = "200" ]

echo "=== DASHBOARD2 DIRECT (loopback) ==="
code="$(wait_code http://127.0.0.1:18107/celltower/dashboard2.html 200 30)"
echo "CHECK dashboard2 direct ${code} (want 200)"
[ "$code" = "200" ]

echo "=== DASHBOARD2 VIA GATEWAY (:443) ==="
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:443/celltower/dashboard2.html)" || code="000"
echo "CHECK dashboard2 gateway ${code} (want 200)"
[ "$code" = "200" ]

echo "=== GATEWAY REGRESSION CHECKS (through nginx on :443) ==="
for pair in \
  "celltower-index http://127.0.0.1:443/celltower/" \
  "celltower-health http://127.0.0.1:443/celltower/api/health" \
  "members http://127.0.0.1:443/members/health" \
  "portal http://127.0.0.1:443/portal/health" \
  "specmatrix http://127.0.0.1:443/specmatrix/health" \
  "files http://127.0.0.1:443/files/health" ; do
  name="${pair%% *}"; url="${pair#* }"
  c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${url}")" || c="000"
  echo "CHECK ${name} ${c}"
done

echo "=== AUDITED APP (separate production stack — must be untouched) ==="
APPCODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:80/health)" || APPCODE="000"
echo "CHECK audited-app ${APPCODE}"

echo "=== MEMORY AFTER + CELLTOWER FOOTPRINT ==="
free -m
docker compose ps celltower
docker stats --no-stream --format 'MEM {{.MemUsage}} CPU {{.CPUPerc}} NAME {{.Name}}' live-celltower || true
echo "=== DEPLOY-CELLTOWER-DASHBOARD DONE ==="
