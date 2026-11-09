#!/usr/bin/env bash
# Host-side deploy for the live-servers demo set.
# Runs in /home/deploy/live-servers on the AWS host. Invoked by the GitHub
# Actions deploy workflow over SSH after rsync.
#
# Safety: a RAM gate runs FIRST. If the host has < 700 MB available,
# nothing is changed and the script exits 1.
set -u
cd /home/deploy/live-servers

echo "=== RAM GATE ==="
free -m
AVAIL="$(free -m | awk '/^Mem:/ {print $7}')"
echo "Available RAM: ${AVAIL} MB (gate: >= 700 MB required)"
if [ -z "${AVAIL}" ] || [ "${AVAIL}" -lt 700 ]; then
  echo "RAM GATE FAILED: only ${AVAIL:-unknown} MB available — no changes made"
  exit 1
fi
echo "RAM GATE PASSED"

if [ ! -f .env ]; then
  echo "Creating /home/deploy/live-servers/.env with fresh deploy-time secrets (values are never printed)"
  umask 077
  {
    echo "XSAVE_XSTS_SIGNING_KEY=$(openssl rand -hex 32)"
    echo "XSAVE_DEV_SECRET=$(openssl rand -hex 32)"
    echo "CELL_LAN_MASTER_KEY=$(openssl rand -hex 32)"
  } > .env
  chmod 600 .env
else
  echo ".env already present — keeping existing secrets"
fi

echo "=== BUILD + UP (project: live-servers) ==="
docker compose up -d --build
COMPOSE_RC=$?
echo "docker compose up exit code: ${COMPOSE_RC}"

echo "=== LISTENER DIAGNOSTICS (read-only) ==="
echo "--- who holds :443 / :80 / :181xx on this host ---"
ss -tlnp 2>/dev/null | grep -E ':(443|80|18101|18102|18103|18104|18105|18106|18107)[[:space:]]' || ss -tln
echo "--- all containers on this host (all projects, read-only) ---"
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

check() {
  # $1 = label, $2 = url, $3 = line prefix (CHECK/DIRECT), $4 = attempts
  local name="$1" url="$2" kind="${3:-CHECK}" attempts="${4:-8}" code="000" i
  for i in $(seq 1 "$attempts"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url")" || code="000"
    case "$code" in
      200|301|302) break ;;
    esac
    sleep 4
  done
  echo "$kind $name $code"
}

echo "=== DIRECT SERVICE CHECKS (loopback, gateway bypassed) ==="
check maclan    http://127.0.0.1:18101/maclan/health    DIRECT 15
check xbox      http://127.0.0.1:18102/xbox/health      DIRECT 15
check wyze      http://127.0.0.1:18103/wyze/health      DIRECT 15
check usb       http://127.0.0.1:18104/usb/api/status   DIRECT 15
check aircore   http://127.0.0.1:18105/aircore/api/state DIRECT 15
check fmp       http://127.0.0.1:18106/fmp/api/health   DIRECT 15
check celltower http://127.0.0.1:18107/celltower/api/health DIRECT 15

echo "=== GATEWAY CHECKS (through nginx on :443) ==="
check maclan    http://127.0.0.1:443/maclan/health
check xbox      http://127.0.0.1:443/xbox/health
check wyze      http://127.0.0.1:443/wyze/health
check usb       http://127.0.0.1:443/usb/api/status
check aircore   http://127.0.0.1:443/aircore/api/state
check fmp       http://127.0.0.1:443/fmp/api/health
check celltower http://127.0.0.1:443/celltower/api/health

echo "=== AUDITED APP (separate production stack — must be untouched) ==="
APPCODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:80/health)" || APPCODE="000"
echo "CHECK audited-app ${APPCODE}"

echo "=== COMPOSE PS ==="
docker compose ps

echo "=== MEMORY AFTER ==="
free -m

echo "=== CONTAINER LOG TAILS (diagnostics) ==="
docker compose logs --tail 50 --no-log-prefix

echo "=== DEPLOY.SH DONE (compose rc=${COMPOSE_RC}) ==="
exit ${COMPOSE_RC}
