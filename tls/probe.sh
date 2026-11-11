#!/usr/bin/env bash
# Read-only host probe for the members.911transedu.net TLS work.
# Verifies the pieces the TLS-ALPN-01 design depends on BEFORE any change:
#   - nginx:alpine ships the stream + ssl_preread modules
#   - the goacme/lego image is pullable and its TLS challenge can bind a
#     custom loopback port
#   - a user crontab is available for cert renewal (no sudo anywhere)
# Changes nothing on the host except pulling two docker images.
set -u

echo "=== HOST ==="
hostname; uname -a; id

echo "=== NGINX STREAM MODULE (image used by the gateway) ==="
docker run --rm nginx:alpine sh -c \
  'ls /usr/lib/nginx/modules/ | grep -i stream; nginx -V 2>&1 | grep -o "with-stream[^ ]*"' \
  || echo "STREAM PROBE FAILED"

echo "=== LEGO IMAGE + HELP ==="
if docker pull goacme/lego:latest >/dev/null 2>&1; then
  echo "lego image pulled OK"
else
  echo "LEGO PULL FAILED"
fi
docker run --rm goacme/lego:latest --help 2>&1 | sed -n '1,30p' || true
echo "--- lego 'run' challenge/port flags ---"
docker run --rm goacme/lego:latest run --help 2>&1 | grep -iE 'tls|http\.|port' | head -25 || true
echo "--- lego v5 flag survey (where do path/email/domains/accept-tos live?) ---"
docker run --rm goacme/lego:latest --help 2>&1 | grep -iE 'path|storage|email|domain|accept|server' || true
echo "[run --help matches:]"
docker run --rm goacme/lego:latest run --help 2>&1 | grep -iE '\-\-path|\-\-storage|\-\-email|\-\-domains|\-\-accept|\-\-server' || true
echo "[certificates --help matches:]"
docker run --rm goacme/lego:latest certificates --help 2>&1 | head -20 || true
echo "--- lego v5: FULL run --help (renew semantics) ---"
docker run --rm goacme/lego:latest run --help 2>&1 | grep -iE 'renew|force|days|reuse' || echo "(no renew/force/days flags in run --help)"

echo "=== CRON (deploy user) ==="command -v crontab || echo "no crontab binary"
crontab -l 2>&1 | head -5
systemctl is-active cron 2>/dev/null || systemctl is-active crond 2>/dev/null || echo "cron service state unknown"

echo "=== LISTENERS :80 / :443 ==="
ss -tlnp 2>/dev/null | grep -E ':(80|443)[[:space:]]' || ss -tln | grep -E ':(80|443)[[:space:]]'

echo "=== AUDITED APP LOOPBACK (read-only) ==="
curl -s -o /dev/null -w 'audited /health http_code=%{http_code}\n' --max-time 5 http://127.0.0.1:80/health || echo "audited check failed"

echo "=== GATEWAY LOOPBACK (read-only) ==="
curl -s -o /dev/null -w 'gateway /members/health http_code=%{http_code}\n' --max-time 5 http://127.0.0.1:443/members/health || echo "gateway check failed"

echo "=== RAM / DISK ==="
free -m
df -h /home/deploy | tail -1

echo "=== TLS-STATE TREE (diagnostic) ==="
ls -laR /home/deploy/live-servers/tls-state/ 2>&1 | head -40
find /home/deploy -name 'members.911transedu.net*' 2>/dev/null | head -10

echo "=== PROBE DONE ==="
