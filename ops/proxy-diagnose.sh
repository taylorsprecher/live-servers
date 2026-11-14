#!/usr/bin/env bash
# Read-only diagnostics for the proxy service after its first deploy
# failed the WebSocket wait (nothing answered on 127.0.0.1:18114).
# Changes nothing. The password VALUE is never printed — only whether
# the .env line exists and its length.
set -u
cd /home/deploy/live-servers

echo "=== COMPOSE PS (proxy, gateway) ==="
docker compose ps proxy gateway || true

echo "=== DOCKER PS live-proxy ==="
docker ps -a --filter name=live-proxy --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' || true

echo "=== live-proxy LOGS (tail 80) ==="
docker logs --tail 80 live-proxy 2>&1 || true

echo "=== LISTENER :18114 ==="
ss -tlnp 2>/dev/null | grep 18114 || echo "nothing listening on 18114"

echo "=== HOST .env PROXY_SS_PASSWORD presence (value never shown) ==="
if [ -f .env ]; then
  grep -E '^PROXY_SS_PASSWORD=' .env | awk -F= '{print "line present, value length=" length($2)}' || echo "line ABSENT"
else
  echo ".env missing"
fi

echo "=== ONE MANUAL WS PROBE ==="
curl -s -o /dev/null -w 'direct WS probe: %{http_code}\n' --max-time 5 \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  http://127.0.0.1:18114/proxy || echo "curl failed to connect"

echo "=== MEMORY ==="
free -m
docker stats --no-stream --format 'MEM {{.MemUsage}} NAME {{.Name}}' live-proxy 2>/dev/null || echo "live-proxy not running"
echo "=== PROXY-DIAGNOSE DONE ==="
