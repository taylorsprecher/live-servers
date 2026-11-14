#!/usr/bin/env bash
# Targeted deploy for the proxy service ONLY.
#
# Why this exists: deploy.sh's RAM gate (>= 700 MB available) protects
# the host during a FULL build. A proxy-only build is proportionate:
# the image is the official shadowsocks-rust server plus one plugin
# binary (tens of MB of RAM at runtime). This script does NOT lower or
# edit deploy.sh's gate and does not rebuild or recreate any other
# service.
#
# One deliberate behaviour shared with ops/deploy-syncflow.sh: the
# proxy's WebSocket route lives in the specmatrix server block of
# gateway/nginx.conf, which the gateway container bind-mounts —
# editing it does not change the gateway's compose config hash, so
# (exactly as deploy.sh does) the gateway is force-recreated
# afterwards to load the current config. The gateway is stateless; no
# TLS config, DNS, other route, the member site, or the audited
# production app is touched. See services/proxy/README.md.
set -eu
cd /home/deploy/live-servers

echo "=== MEMORY BEFORE ==="
free -m

echo "=== BUILD + UP proxy only ==="
docker compose up -d --build proxy

echo "=== GATEWAY FORCE-RECREATE (pick up current nginx.conf) ==="
docker compose up -d --force-recreate gateway

WS_HEADERS=(-H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==')

echo "=== WAIT FOR proxy WebSocket (direct loopback :18114) ==="
# 45 attempts, not the usual 15: on this host, recreating the proxy
# container leaves its published port blackholing SYNs for ~4-5
# minutes (the docker-proxy lag observed 2026-11-13 on both a cold
# build and a warm recreate) even though the container process is up
# and listening within seconds and answers 101 the moment the window
# passes. Every probe inside the window times out; none is refused.
# The assertion stays strict — only the patience is longer.
code="000"
for i in $(seq 1 45); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${WS_HEADERS[@]}" http://127.0.0.1:18114/proxy)" || code="000"
  [ "${code}" = "101" ] && break
  sleep 4
done
echo "CHECK proxy-ws direct ${code} (want 101)"
[ "${code}" = "101" ]

echo "=== PROXY VIA GATEWAY (SNI specmatrix.911transedu.net through :443) ==="
gcode="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 --resolve specmatrix.911transedu.net:443:127.0.0.1 "${WS_HEADERS[@]}" https://specmatrix.911transedu.net/proxy)" || gcode="000"
echo "CHECK proxy-ws gateway ${gcode} (want 101)"
[ "${gcode}" = "101" ]

echo "=== GATEWAY REGRESSION CHECKS (through nginx on :443) ==="
for pair in \
  "specmatrix http://127.0.0.1:443/specmatrix/health" \
  "members http://127.0.0.1:443/members/health" \
  "portal http://127.0.0.1:443/portal/health" \
  "files http://127.0.0.1:443/files/health" \
  "syncflow http://127.0.0.1:443/syncflow/health" \
  "maclan http://127.0.0.1:443/maclan/health" \
  "celltower http://127.0.0.1:443/celltower/api/health" ; do
  name="${pair%% *}"; url="${pair#* }"
  c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${url}")" || c="000"
  echo "CHECK ${name} ${c}"
done

echo "=== AUDITED APP (separate production stack — must be untouched) ==="
APPCODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:80/health)" || APPCODE="000"
echo "CHECK audited-app ${APPCODE}"

echo "=== MEMORY AFTER + PROXY FOOTPRINT ==="
free -m
docker compose ps proxy gateway
docker stats --no-stream --format 'MEM {{.MemUsage}} CPU {{.CPUPerc}} NAME {{.Name}}' live-proxy live-gateway || true
echo "=== DEPLOY-PROXY DONE ==="
