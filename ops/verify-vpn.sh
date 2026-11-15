#!/usr/bin/env bash
# Read-only VPN status check: prints `wg show` from the live-vpn
# container (public keys, handshake times, transfer counters — no
# private or preshared key material is ever printed by wg show) plus
# the container state and the host UDP listener. A peer with a
# "latest handshake" line has connected successfully end-to-end,
# which also proves the AWS security group passes UDP 51820.
set -u
cd /home/deploy/live-servers

echo "=== VPN CONTAINER ==="
docker compose ps vpn

echo "=== WG SHOW ==="
docker exec live-vpn wg show || echo "wg show failed — container not running?"

echo "=== HOST LISTENER (udp 51820) ==="
ss -ulnp 2>/dev/null | grep 51820 || echo "no udp 51820 listener in ss output"

echo "=== MEMORY ==="
free -m
echo "=== VERIFY-VPN DONE ==="
