#!/usr/bin/env bash
# Targeted deploy for USB-SOVEREIGN ONLY (services/usb-sovereign).
#
# Directed 2026-11-19: "Do the same for USB AND HDMI" — the C-Port
# treatment applied to USB-SOVEREIGN, including the HDMI bridge pair
# (COM_HDMI1 <-> COM_HDMI2, 115200 baud) from the 'Cport to Coort'
# spec update (2026-11-10), built into the controller in this change.
#
# Why targeted (same reasoning as ops/deploy-cport.sh): deploy.sh's
# RAM gate protects the host during a FULL build. This script builds
# only usb. It does NOT lower or edit deploy.sh's gate, does not
# rebuild any other service, and does not touch the gateway (the
# /usb/ route already exists and forwards its prefix intact).
# The AWS sink gates stay exactly as the host .env has them — this
# deploy changes no sink setting.
#
# Verification: the dashboard status API on loopback :18104 (the app
# serves under its /usb BASE_PATH) must answer 200, must list the
# COM_HDMI1/COM_HDMI2 pair, and the MQTT published counter must
# ADVANCE between two reads ~12 s apart (the bridge's live signal).
set -eu
cd /home/deploy/live-servers

echo "=== MEMORY BEFORE ==="
free -m

echo "=== BUILD + UP usb ==="
docker compose up -d --build usb

wait_code() {
  local url="$1" want="$2" attempts="${3:-100}" code="000" i
  for i in $(seq 1 "$attempts"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")" || code="000"
    [ "$code" = "$want" ] && break
    sleep 5
  done
  echo "$code"
}

echo "=== WAIT FOR usb status API (direct loopback :18104) ==="
code="$(wait_code http://127.0.0.1:18104/usb/api/status 200)"
echo "CHECK usb-status direct ${code} (want 200)"
[ "$code" = "200" ]

echo "=== HDMI PAIR PRESENT IN STATUS ==="
status1="$(curl -s --max-time 10 http://127.0.0.1:18104/usb/api/status)"
echo "$status1" | grep -q '"a": "COM_HDMI1", "b": "COM_HDMI2"'
echo "$status1" | grep -q '"COM_HDMI1"'
echo "$status1" | grep -q '"COM_HDMI2"'
echo "CHECK hdmi-pair present (COM_HDMI1 <-> COM_HDMI2)"

echo "=== BRIDGE LIVE: mqtt_published MUST ADVANCE ==="
published() {
  curl -s --max-time 10 http://127.0.0.1:18104/usb/api/status \
    | sed -n 's/.*"mqtt_published": \([0-9]*\).*/\1/p'
}
p1="$(published)"; echo "mqtt_published #1: ${p1}"
sleep 12
p2="$(published)"; echo "mqtt_published #2: ${p2}"
[ -n "$p1" ] && [ -n "$p2" ] && [ "$p2" -gt "$p1" ]
echo "CHECK usb-bridge publishing ${p1} -> ${p2}"

echo "=== REGRESSIONS ==="
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://members.911transedu.net/members/health || true)"
echo "CHECK members-health gateway ${code} (want 200)"
[ "$code" = "200" ]
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:80/health || true)"
echo "CHECK audited-app ${code} (want 200)"
[ "$code" = "200" ]

echo "=== MEMORY AFTER ==="
free -m
docker compose ps usb
echo "DEPLOY USB DONE"
