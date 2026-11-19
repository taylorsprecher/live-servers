#!/usr/bin/env bash
# Targeted deploy for the C-Port CRC pipeline ONLY (services/cport-pipeline).
#
# Directed 2026-11-19: "Download Cport-Coort and rebuild C port" — Taylor
# chose all three offered scopes, this script is the fleet-rebuild one.
#
# Why targeted (same reasoning as ops/deploy-zillion.sh): deploy.sh's
# RAM gate protects the host during a FULL build. This script builds
# only cport. It does NOT lower or edit deploy.sh's gate, does not
# rebuild any other service, and does not touch the gateway (the
# /cport/ route already exists and strips its prefix to the
# container's root /metrics, as configured at first deploy).
#
# Verification: the container's Prometheus endpoint on loopback
# :18108 must answer 200, and pipeline_packets_processed_total must
# ADVANCE between two reads ~15 s apart — the service's own live
# signal in simulate mode (5 fps), the same check used at first
# deploy on 2026-11-10.
set -eu
cd /home/deploy/live-servers

echo "=== MEMORY BEFORE ==="
free -m

echo "=== BUILD + UP cport ==="
docker compose up -d --build cport

wait_code() {
  local url="$1" want="$2" attempts="${3:-100}" code="000" i
  for i in $(seq 1 "$attempts"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")" || code="000"
    [ "$code" = "$want" ] && break
    sleep 5
  done
  echo "$code"
}

echo "=== WAIT FOR cport metrics (direct loopback :18108) ==="
code="$(wait_code http://127.0.0.1:18108/metrics 200)"
echo "CHECK cport-metrics direct ${code} (want 200)"
[ "$code" = "200" ]

echo "=== PROCESSED COUNTER MUST ADVANCE (simulate mode, 5 fps) ==="
processed() {
  curl -s --max-time 10 http://127.0.0.1:18108/metrics \
    | awk '/^pipeline_packets_processed_total/ {print $2}' | cut -d. -f1
}
p1="$(processed)"; echo "processed #1: ${p1}"
sleep 15
p2="$(processed)"; echo "processed #2: ${p2}"
[ -n "$p1" ] && [ -n "$p2" ] && [ "$p2" -gt "$p1" ]
echo "CHECK cport-processed advancing ${p1} -> ${p2}"

echo "=== REGRESSIONS ==="
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/members/health || true)"
echo "CHECK members-health gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:80/health || true)"
echo "CHECK audited-app ${code} (want 200)"
[ "$code" = "200" ]

echo "=== MEMORY AFTER ==="
free -m
docker compose ps cport
echo "DEPLOY CPORT DONE"
