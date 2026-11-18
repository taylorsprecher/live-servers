#!/usr/bin/env bash
# Targeted deploy for BlackBerry LAN ONLY (services/blackberry-lan).
#
# What this installs: the runnable core of Taylor's BlackBerry LAN /
# Air platform design (filed 2026-11-17 as BlackBerry_LAN.pdf/.docx,
# installed the same day on the jasmine EC2 host) — the AirCore stack
# VM, the AirBridge message bus, and the AirShield policy gate —
# reachable at https://members.911transedu.net/blackberry/.
# Directed 2026-11-18: "Put BlackBerry LAN on App like others" — the
# service joins the main audited-host fleet so its Apps Hub tile works
# like the other services' tiles. The compose service and the gateway
# /blackberry/ location are already on main; this deploy starts the
# container and force-recreates the gateway to pick the route up.
#
# Why targeted (same reasoning as ops/deploy-payledger.sh): deploy.sh's
# RAM gate protects the host during a FULL build. This script builds
# only blackberry and force-recreates the gateway. It does NOT lower
# or edit deploy.sh's gate and does not rebuild any other service.
# No secrets are needed by this service and none are delivered.
set -eu
cd /home/deploy/live-servers

echo "=== MEMORY BEFORE ==="
free -m

echo "=== BUILD + UP blackberry ==="
docker compose up -d --build blackberry

wait_code() {
  local url="$1" want="$2" attempts="${3:-100}" code="000" i
  for i in $(seq 1 "$attempts"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")" || code="000"
    [ "$code" = "$want" ] && break
    sleep 5
  done
  echo "$code"
}

echo "=== WAIT FOR blackberry health (direct loopback :18124) ==="
code="$(wait_code http://127.0.0.1:18124/blackberry/health 200)"
echo "CHECK blackberry-health direct ${code} (want 200)"
[ "$code" = "200" ]

echo "=== AIRCORE VM SMOKE (direct loopback: (5+3)x2 must equal 16) ==="
vm="$(curl -s --max-time 10 -X POST http://127.0.0.1:18124/blackberry/aircore/run \
  -H 'Content-Type: application/json' \
  -d '{"program":["PUSH 5","PUSH 3","ADD","PUSH 2","MUL","HALT"]}' || true)"
echo "VM RESULT ${vm}"
echo "${vm}" | grep -q '"stack": \[16\]' || echo "${vm}" | grep -q '"stack":\[16\]'
echo "CHECK aircore-vm ok"

echo "=== GATEWAY FORCE-RECREATE (picks up /blackberry/ location) ==="
docker compose up -d --force-recreate gateway

echo "=== WAIT FOR gateway /blackberry/health (public HTTPS) ==="
code="$(wait_code https://members.911transedu.net/blackberry/health 200)"
echo "CHECK gateway blackberry-health ${code} (want 200)"
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
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:80/health || true)"
echo "CHECK audited-app ${code} (want 200)"
[ "$code" = "200" ]

echo "=== MEMORY AFTER ==="
free -m
docker compose ps blackberry
echo "DEPLOY BLACKBERRY DONE"
