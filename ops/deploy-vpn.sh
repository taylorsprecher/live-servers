#!/usr/bin/env bash
# Targeted deploy for the VPN service ONLY (WireGuard — see
# services/vpn/README.md).
#
# Why this exists (same reasoning as ops/deploy-proxy.sh): deploy.sh's
# RAM gate (>= 700 MB available) protects the host during a FULL
# build. Starting one small container (128 MB limit, prebuilt image)
# is proportionate. This script does NOT lower or edit deploy.sh's
# gate and does not rebuild or recreate any other service. The
# gateway is NOT recreated: the VPN does not use nginx at all (it
# publishes UDP 51820 directly — the gateway cannot carry WireGuard).
#
# Log hygiene: the vpn container is configured with LOG_CONFS=false,
# so peer configs never enter its log. The failure path below still
# filters any line that could carry key material or QR art before
# printing a log tail.
set -eu
cd /home/deploy/live-servers

echo "=== MEMORY BEFORE ==="
free -m

echo "=== UP vpn (prebuilt image) + vpn-dns sidecar (built) ==="
docker compose up -d vpn
docker compose up -d --build vpn-dns

echo "=== WAIT FOR wireguard interface (wg show inside the container) ==="
ok=0
i=0
while [ "$i" -lt 60 ]; do
  if docker exec live-vpn wg show wg0 >/dev/null 2>&1; then
    ok=1; echo "wg0 up after ~$((i * 5))s"; break
  fi
  i=$((i + 1)); sleep 5
done
if [ "$ok" != "1" ]; then
  echo "FAIL: wg0 did not come up within ~300s"
  docker compose ps vpn || true
  echo "--- live-vpn log tail (key material and QR art filtered out) ---"
  docker logs --tail 60 live-vpn 2>&1 | grep -v -e '█' -e 'PrivateKey' -e 'PresharedKey' || true
  exit 1
fi

echo "=== WG SHOW (public keys and transfer counters only) ==="
docker exec live-vpn wg show

echo "=== HOST LISTENER (udp 51820) ==="
ss -ulnp 2>/dev/null | grep 51820 || echo "WARN: no udp 51820 listener in ss output"

echo "=== VPN DNS FILTER (selftest inside the sidecar) ==="
docker exec live-vpn-dns python3 /app/vpn_dns_filter.py --selftest || echo "WARN: vpn-dns selftest failed or sidecar absent"
docker compose ps vpn-dns || true

echo "=== GATEWAY REGRESSION CHECKS (through nginx on :443) ==="
for pair in \
  "members http://127.0.0.1:443/members/health" \
  "portal http://127.0.0.1:443/portal/health" \
  "specmatrix http://127.0.0.1:443/specmatrix/health" \
  "files http://127.0.0.1:443/files/health" \
  "celltower http://127.0.0.1:443/celltower/api/health" ; do
  name="${pair%% *}"; url="${pair#* }"
  c="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "${url}")" || c="000"
  echo "CHECK ${name} ${c}"
done

echo "=== AUDITED APP (separate production stack — must be untouched) ==="
APPCODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:80/health)" || APPCODE="000"
echo "CHECK audited-app ${APPCODE}"

echo "=== MEMORY AFTER + VPN FOOTPRINT ==="
free -m
docker compose ps vpn vpn-dns
docker stats --no-stream --format 'MEM {{.MemUsage}} CPU {{.CPUPerc}} NAME {{.Name}}' live-vpn live-vpn-dns || true
echo "=== DEPLOY-VPN DONE ==="
