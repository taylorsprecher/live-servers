#!/usr/bin/env bash
# Targeted deploy for FastAPI App ONLY (services/fastapi-app).
#
# What this installs: a minimal FastAPI service scaffolded
# 2026-11-18 around the Dockerfile Taylor uploaded that day
# (python:3.12-slim, uvicorn app.main:app on port 8000), reachable
# at https://members.911transedu.net/fastapi/. Directed 2026-11-18:
# "Start a new service with it". The service name is a placeholder;
# the wiring (compose service, gateway /fastapi/ location, Apps Hub
# tile) is real. This deploy starts the container and
# force-recreates the gateway to pick the route up.
#
# Why targeted (same reasoning as ops/deploy-payledger.sh): deploy.sh's
# RAM gate protects the host during a FULL build. This script builds
# only fastapi-app and force-recreates the gateway. It does NOT
# lower or edit deploy.sh's gate and does not rebuild any other
# service.
# Secrets: this service needs none and none are delivered.
set -eu
cd /home/deploy/live-servers

echo "=== MEMORY BEFORE ==="
free -m

echo "=== BUILD + UP fastapi-app ==="
docker compose up -d --build fastapi-app

wait_code() {
  local url="$1" want="$2" attempts="${3:-100}" code="000" i
  for i in $(seq 1 "$attempts"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")" || code="000"
    [ "$code" = "$want" ] && break
    sleep 5
  done
  echo "$code"
}

echo "=== WAIT FOR fastapi-app health (direct loopback :18127) ==="
code="$(wait_code http://127.0.0.1:18127/health 200)"
echo "CHECK fastapi-app-health direct ${code} (want 200)"
[ "$code" = "200" ]

echo "=== HEALTH SMOKE (direct loopback: /health + /) ==="
health="$(curl -s --max-time 10 http://127.0.0.1:18127/health || true)"
echo "HEALTH ${health}"
echo "${health}" | grep -q '"status": *"ok"'
echo "${health}" | grep -q '"service": *"fastapi-app"'
index="$(curl -s --max-time 10 http://127.0.0.1:18127/ || true)"
echo "INDEX ${index}"
echo "${index}" | grep -q '"service": *"fastapi-app"'
echo "CHECK fastapi-app-smoke ok"

echo "=== GATEWAY FORCE-RECREATE (picks up /fastapi/ location) ==="
docker compose up -d --force-recreate gateway

echo "=== WAIT FOR gateway /fastapi/health (public HTTPS) ==="
code="$(wait_code https://members.911transedu.net/fastapi/health 200)"
echo "CHECK gateway fastapi-health ${code} (want 200)"
[ "$code" = "200" ]

echo "=== REGRESSIONS ==="
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/members/health || true)"
echo "CHECK members-health gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/portal/ || true)"
echo "CHECK portal gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/files/ || true)"
echo "CHECK files gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/ledger/health || true)"
echo "CHECK ledger gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/blackberry/health || true)"
echo "CHECK blackberry gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/zillion/health || true)"
echo "CHECK zillion gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:80/health || true)"
echo "CHECK audited-app ${code} (want 200)"
[ "$code" = "200" ]

echo "=== MEMORY AFTER ==="
free -m
docker compose ps fastapi-app
echo "DEPLOY FASTAPI-APP DONE"
