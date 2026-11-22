#!/usr/bin/env bash
# Read-only VPN status check: prints `wg show` from the live-vpn
# container (public keys, handshake times, transfer counters — no
# private or preshared key material is ever printed by wg show) plus
# the container state and the host UDP listener. A peer with a
# "latest handshake" line has connected successfully end-to-end,
# which also proves the AWS security group passes UDP 51820.
#
# IP masking (Taylor's direction, 2026-11-18): every IPv4 address in
# the printed wg output is masked — both the peers' public endpoint
# IPs and the 10.8.0.x tunnel addresses — because this repo and its
# workflow logs are public. Peers are identified by their public
# keys, which stay printed. Unmasked state remains available on the
# host itself via a direct `wg show`.
set -u
cd /home/deploy/live-servers

echo "=== VPN CONTAINER ==="
docker compose ps vpn

echo "=== WG SHOW (IP addresses masked) ==="
WG_OUT="$(docker exec live-vpn wg show)" || WG_OUT="wg show failed — container not running?"
printf '%s\n' "$WG_OUT" | sed -E 's/[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/<ip masked>/g'

echo "=== VPN DNS FILTER (selftest: rules, counters, report) ==="
docker exec live-vpn-dns python3 /app/vpn_dns_filter.py --selftest || echo "vpn-dns sidecar not running"

echo "=== HOST LISTENER (udp 51820) ==="
ss -ulnp 2>/dev/null | grep 51820 || echo "no udp 51820 listener in ss output"

echo "=== MEMORY ==="
free -m
echo "=== VERIFY-VPN DONE ==="
